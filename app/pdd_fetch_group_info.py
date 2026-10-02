# -*- coding: utf-8 -*-
# ==============================================================================
# [功能摘要]
# 基于动态账号池调度与单标签(Tab)细粒度抓取的拼多多增量采集器，具备账号冷却与 Mongo 幂等去重。
#
# [输入数据]
# 1. pdd_browser_data_list: 外部提供的本地浏览器用户数据目录路径列表 (List of Strings)。
# 2. 目标页面 XHR 接口 (brand-group-home/home/goods_list) 返回的未清洗 JSON 结构。
#
# [数据流转/交互]
# 1. 主控器查询 MongoDB 账号状态，筛选已满足 30 分钟冷却期的可用账号。
# 2. 调度账号访问主页，注入 JS 提取页面顶部 Tab 列表特征 (含 name 与 DOM index)。
# 3. 将 (账号, 目标Tab) 指派给采集引擎，引擎拦截网络请求进行 JSON 数据清洗去重。
# 4. 触发风控时，立即封存当前账号时间戳并打断流程，外层调度器无缝切换新账号接力。
#
# [输出数据]
# 增量写入 MongoDB products 集合，商品按 (platform, product_id) 唯一标识。
# 账号冷却时间写入 crawler_account_status 集合；浏览器目录与异常快照仍保留本地。
# ==============================================================================

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from common.playwright_utils import launch_persistent_context
from common.common_utils import get_config
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager, AccountStatusManager

# ------------------------------------------------------------------------------
# 日志配置：重塑为高可读性、去噪格式
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s'
)
logger = logging.getLogger("pdd_scraper")

# ------------------------------------------------------------------------------
# 核心全局配置
# ------------------------------------------------------------------------------
GLOBAL_CONFIG = {
    "target_url": "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel",
    "platform": "pdd",
    "account_cooldown_minutes": 30,
    "wait_no_account_seconds": 60,
    "max_scrolls_per_tab": -1,  # 修改为 -1 表示直到连续10次无新请求则视为到底
    "headless_mode": True,
    "scroll_step_y": 6000,
    "scroll_interval": 2.0,
}


# ==============================================================================
# 基础工具与存储逻辑
# ==============================================================================

class StorageError(RuntimeError):
    """业务持久化失败，必须停止采集，不能按页面错误切号重试。"""


def normalize_goods(item, tab_name):
    """将拼多多频道商品转换为统一存储字段，价格单位为元。"""
    goods_id = item.get("goods_id")
    if isinstance(goods_id, bool) or not isinstance(goods_id, (str, int)):
        return None
    goods_id = str(goods_id).strip()
    if not goods_id:
        return None

    return {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": goods_id,
        "category": tab_name,
        "name": item.get("goods_name", ""),
        "brand": item.get("brand_name", ""),
        "original_price": (item.get("origin_price") or 0) / 100,
        "activity_price": (item.get("activity_price") or 0) / 100,
        "saved_price": (item.get("group_order_price_reduce") or 0) / 100,
        "sales_tip": item.get("sales_tip", ""),
        "product_url": urljoin("https://mobile.pinduoduo.com/", item.get("link_url") or ""),
        "image_url": item.get("hd_thumb_url", ""),
    }


# ==============================================================================
# 账号状态与调度调度
# ==============================================================================

def get_available_account(account_list, account_manager):
    """查询满足冷却时长的账号，按配置顺序返回；数据库故障直接抛出。"""
    status_dict = account_manager.get_last_used_times(GLOBAL_CONFIG["platform"], account_list)
    now = datetime.now(timezone.utc)
    cooldown_delta = timedelta(minutes=GLOBAL_CONFIG["account_cooldown_minutes"])

    for account in account_list:
        last_used_time = status_dict.get(account)
        if last_used_time is None:
            return account
        if not isinstance(last_used_time, datetime):
            logger.warning("[调度/校验] 发现脏数据时间戳 | 账号: [%s] | 动作: 强制重置可用", os.path.basename(account))
            return account
        # Mongo 的无时区 BSON 日期也表示 UTC，兼容其他客户端写入的数据。
        if last_used_time.tzinfo is None:
            last_used_time = last_used_time.replace(tzinfo=timezone.utc)
        if now - last_used_time >= cooldown_delta:
            return account

    return None


def update_account_usage_time(account, account_manager):
    """刷新指定账号的 Mongo 使用时间，成功后再记录工作状态。"""
    account_manager.touch_account(GLOBAL_CONFIG["platform"], account)
    logger.info("[调度/锁定] 账号已切入工作态 | 账号: [%s] | 冷却倒计时: [%d 分钟]",
                os.path.basename(account), GLOBAL_CONFIG["account_cooldown_minutes"])


# ==============================================================================
# 核心抓取与 DOM 交互引擎
# ==============================================================================

def save_error_snapshot(page, tab_name, reason):
    """【新增】保存异常页面截图与网页源码"""
    try:
        error_dir = "error_data"
        os.makedirs(error_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 过滤掉tab_name中的非法文件路径字符
        safe_tab_name = "".join([c for c in tab_name if c.isalnum() or c in ("_", "-")])
        if not safe_tab_name: safe_tab_name = "unknown"

        base_path = os.path.join(error_dir, f"{timestamp}_{safe_tab_name}_{reason}")

        page.screenshot(path=f"{base_path}.png", full_page=True)
        with open(f"{base_path}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info(f"[异常/快照] 已保存现场截图与源码: {base_path}")
    except Exception as e:
        logger.error(f"[异常/快照] 尝试保存现场失败: {str(e)}")


def check_risk_control(page):
    """通过探查 DOM 核心元素判定是否遭遇风控拦截。"""
    try:
        if page.get_by_text("活动陆续开放中", exact=True).is_visible(): return True
        if page.get_by_text("回到首页", exact=True).is_visible(): return True
    except Exception:
        pass
    return False


def get_tab_list(user_data_dir):
    """
    启动无头浏览器探查全局可用 Tab。
    出参形貌: [ {"index": 0, "name": "精选"}, {"index": 1, "name": "手机"} ... ] 失败返回 None
    """
    acc_name = os.path.basename(user_data_dir)
    logger.info("[探测/路由] 开始获取全局Tab字典 | 探路账号: [%s]", acc_name)

    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # 【修改 1】：干掉 networkidle，改为 domcontentloaded，DOM 结构出来就放行
            page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")

            # 【修改 2】：显式等待核心导航栏出现，而不是等网络完全安静
            page.locator('#brand-first-nav').wait_for(state="visible", timeout=15000)
            time.sleep(3)

            if check_risk_control(page):
                logger.warning("[探测/拦截] 遭遇首页风控 | 账号: [%s] | 动作: 中断探查退回调度", acc_name)
                return None

            js_extract_code = """
            () => {
                const tabs = document.querySelectorAll('#brand-first-nav > div');
                return Array.from(tabs).map((tab, index) => {
                    let name = tab.innerText.replace('\\n', '').trim();
                    if (!name) {
                        const img = tab.querySelector('img');
                        // 【修改 3】：增强鲁棒性，优先取 aria-label 属性，防止纯图片 Tab 全部变成"图片标签"
                        name = img ? (img.getAttribute('aria-label') || img.getAttribute('alt') || "图片标签_" + index) : "";
                    }
                    return { index: index, name: name.trim() || `未知标签_${index}` };
                });
            }
            """

            tab_list = page.evaluate(js_extract_code)
            logger.info("[探测/路由] Tab解析完成 | 账号: [%s] | 结果: 提取到 [%d] 个分类", acc_name, len(tab_list))
            return tab_list

        except Exception as e:
            logger.error("[探测/异常] DOM拉取或注入失败 | 账号: [%s] | 原因: 网络超时或节点未渲染, %s", acc_name,
                         str(e))
            return None
        finally:
            context.close()


def scrape_single_tab(user_data_dir, tab_info, product_manager):
    """
    单 Tab 深度遍历模块。
    """
    target_index = tab_info["index"]
    tab_name = tab_info["name"]

    round_info = tab_info.get("round", 1)
    tab_idx = tab_info.get("tab_index", 1)
    total_tabs = tab_info.get("total_tabs", 1)
    tab_display = f"第{round_info}轮-第{tab_idx}/{total_tabs}个({tab_name})"

    acc_name = os.path.basename(user_data_dir)
    logger.info("[采集/初始化] 开启专项抓取 | 目标Tab: [%s] | 执行账号: [%s]", tab_display, acc_name)

    storage_error = None
    session_new_count = 0
    session_update_count = 0
    hit_risk = False
    api_response_count = 0
    scroll_count = 0

    def raise_storage_error():
        if storage_error is not None:
            raise StorageError(f"Tab [{tab_display}] 商品写入失败，已停止采集") from storage_error

    def handle_response(response):
        nonlocal session_new_count, session_update_count, hit_risk, api_response_count, storage_error
        if storage_error is not None:
            return
        if "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return

        api_response_count += 1

        try:
            data = response.json()
            if not isinstance(data, dict) or not data.get("success") or "result" not in data:
                return
            result_data = data["result"]
            if not isinstance(result_data, dict):
                return
            if "risk" in str(data).lower():
                hit_risk = True
            goods_list = result_data.get("goods_list", [])
            if not goods_list:
                return
            records = []
            for item in goods_list:
                if not isinstance(item, dict):
                    continue
                parsed_item = normalize_goods(item, tab_name)
                if parsed_item is not None:
                    records.append(parsed_item)
        except Exception as e:
            logger.error("[采集/拦截] 解析数据流发生未捕获异常 | Tab: [%s] | 错误: %s", tab_display, str(e))
            return

        if not records:
            return
        try:
            counts = product_manager.upsert_products(records)
        except Exception as e:
            # Playwright 回调内不直接抛异常，交由页面流程清理浏览器后统一上抛。
            storage_error = e
            logger.error("[存储/失败] 商品批量写入失败 | Tab: [%s] | 错误: %s", tab_display, str(e))
            return

        session_new_count += counts["new"]
        session_update_count += counts["update"]
        logger.info("[采集/入库] Mongo批次写入成功 | Tab: [%s] | 本批新增: [%d] 本批更新: [%d]",
                    tab_display, counts["new"], counts["update"])

    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", handle_response)

        try:
            # 【修改 4】：干掉 networkidle，改为 domcontentloaded
            page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
            raise_storage_error()

            # 【修改 5】：改为显式等待核心容器渲染
            nav_container = page.locator('#brand-first-nav')
            nav_container.wait_for(state="visible", timeout=15000)
            time.sleep(3)
            raise_storage_error()

            if check_risk_control(page):
                logger.warning("[采集/阻断] 入口页校验未通过 | 账号: [%s] | 结论: 已触发严格风控", acc_name)
                if api_response_count < 10:
                    save_error_snapshot(page, tab_name, "风控拦截_请求不足10次")
                return "RISK_CONTROL", {"scrolls": scroll_count, "requests": api_response_count,
                                        "new": session_new_count, "update": session_update_count}

            clear_popups(page)

            # 重新确保容器处于可见态（弹窗清理后可能会有一瞬间遮挡）
            nav_container.wait_for(state="visible", timeout=5000)
            target_tab_element = nav_container.locator('> div').nth(target_index)

            # 【修改 6】：彻底删掉 scroll_into_view_if_needed()！
            # 因为原生 CSS `overflow: hidden` 会导致它抛出超时异常。
            # 下方的 evaluate("node => node.click()") 是原生 JS 注入点击，完全不需要元素在可视区域内。
            target_tab_element.evaluate("node => node.click()")
            time.sleep(3.5)
            raise_storage_error()

            if page.locator("input[type='search']").count() > 0 and page.locator(
                    "input[type='search']").first.is_visible():
                logger.warning("[采集/偏航] 点击Tab后误入搜索页面，判定UI交互失败 | 账号: [%s] | Tab: [%s]", acc_name,
                               tab_display)
                save_error_snapshot(page, tab_name, "异常跑偏_误入搜索页")
                return "PAGE_MISMATCH", {"scrolls": scroll_count, "requests": api_response_count,
                                         "new": session_new_count, "update": session_update_count}

            max_scrolls = GLOBAL_CONFIG["max_scrolls_per_tab"]
            no_new_req_count = 0
            last_api_count = api_response_count

            while True:
                raise_storage_error()
                if max_scrolls != -1 and scroll_count >= max_scrolls:
                    break

                if check_risk_control(page) or hit_risk:
                    logger.warning("[采集/阻断] 滚动链路遭受拦截 | 账号: [%s] | 中断节点: 第 [%d] 次滑动", acc_name,
                                   scroll_count + 1)
                    if api_response_count < 10:
                        save_error_snapshot(page, tab_name, "滑动拦截_请求不足10次")
                    return "RISK_CONTROL", {"scrolls": scroll_count, "requests": api_response_count,
                                            "new": session_new_count, "update": session_update_count}

                page.mouse.wheel(0, GLOBAL_CONFIG["scroll_step_y"])
                scroll_count += 1

                if scroll_count % 10 == 0:
                    if max_scrolls == -1:
                        logger.info("[采集/滚动] 向下加载推进中 | Tab: [%s] | 进度: [已滑 %d 次, 连续空转 %d 次]",
                                    tab_display, scroll_count, no_new_req_count)
                    else:
                        logger.info("[采集/滚动] 向下加载推进中 | Tab: [%s] | 进度: [%d/%d]", tab_display, scroll_count,
                                    max_scrolls)

                time.sleep(GLOBAL_CONFIG["scroll_interval"])
                raise_storage_error()

                if max_scrolls == -1:
                    if api_response_count > last_api_count:
                        no_new_req_count = 0
                        last_api_count = api_response_count
                    else:
                        no_new_req_count += 1

                    if no_new_req_count >= 10:
                        logger.info("[采集/完毕] 连续10次滑动未监控到新请求，判定该Tab数据拉取完毕 | Tab: [%s]",
                                    tab_display)
                        break

            if api_response_count < 10:
                save_error_snapshot(page, tab_name, "正常结束_请求不足10次")

        except StorageError:
            raise
        except Exception as e:
            raise_storage_error()
            logger.error("[采集/崩溃] 页面渲染或交互异常 | Tab: [%s] | 错误详情: %s", tab_display, str(e))
            save_error_snapshot(page, tab_name, "代码崩溃异常")
            return "ERROR", {"scrolls": scroll_count, "requests": api_response_count, "new": session_new_count,
                             "update": session_update_count}
        finally:
            try:
                context.close()
            finally:
                # 所有提前返回及关闭时触发的响应回调，都不能掩盖持久化失败。
                raise_storage_error()

    if api_response_count == 0:
        logger.error("[采集/空转] 整个生命周期未拦截到任何目标请求，疑似页面跑偏 | Tab: [%s]", tab_display)
        return "PAGE_MISMATCH", {"scrolls": scroll_count, "requests": api_response_count, "new": session_new_count,
                                 "update": session_update_count}

    logger.info(
        "[采集/完毕] 单一Tab节点作业结束 | Tab: [%s] | 汇总 -> 下滑次数: [%d], 捕捉请求: [%d], 新增: [%d], 更新: [%d]",
        tab_display, scroll_count, api_response_count, session_new_count, session_update_count)

    return "SUCCESS", {"scrolls": scroll_count, "requests": api_response_count, "new": session_new_count,
                       "update": session_update_count}

def clear_popups(page):
    """
    具备多重降级策略的弹窗自动化清理函数 (适配动态混淆DOM)
    """
    logger.info("[UI交互] 正在执行多维度弹窗检测与清理...")

    # 核心特征词汇，用于判断弹窗存在，以及作为定位锚点
    popup_keywords = ["多人团限时优惠", "立即抢购", "限时优惠", "爆款商品"]

    try:
        # 给可能存在的弹窗动画预留足够渲染时间
        page.wait_for_timeout(1500)

        # --- 步骤 1：嗅探弹窗是否存在 ---
        active_keyword = None
        for keyword in popup_keywords:
            if page.locator(f"text='{keyword}'").count() > 0 and page.locator(f"text='{keyword}'").first.is_visible():
                active_keyword = keyword
                break

        if not active_keyword:
            logger.info("[UI交互] 未检测到已知弹窗特征，页面环境安全")
            return

        logger.warning(f"[UI交互] 嗅探到活动弹窗阻塞 (关键字: {active_keyword})，启动清理链路")

        # --- 步骤 2：策略 A - 基于DOM结构的精准狙击 (针对无特征的关闭图片) ---
        # 逻辑：找到包含目标文字的弹窗大容器，然后去点容器里面的第一个 <img> 标签
        logger.info("[UI交互] 尝试使用结构定位点击关闭图标...")

        # 找到包含特定文字的 div 块，往上找一层容器，然后抓取里面的 img
        # 注意：使用 Playwright 的 filter 功能过滤含有文本的区块
        popup_container = page.locator("div").filter(has_text=active_keyword).last
        close_img = popup_container.locator("img").first

        if close_img.count() > 0 and close_img.is_visible():
            close_img.click(force=True)  # 这里的 force=True 是安全的，因为是我们明确找出的关闭按钮
            page.wait_for_timeout(1000)

            # 校验是否关闭成功
            if not page.locator(f"text='{active_keyword}'").is_visible():
                logger.info("[UI交互] 结构定位关闭弹窗成功！")
                return

        # --- 步骤 3：策略 B - 备用常见选择器盲猜 ---
        logger.info("[UI交互] 结构定位失效，尝试常见通用关闭特征...")
        close_selectors = [
            "text='关闭'", "text='跳过'",
            "[class*='close' i]", "[class*='Close' i]", ".am-modal-close"
        ]
        for sel in close_selectors:
            elements = page.locator(sel)
            if elements.count() > 0 and elements.first.is_visible():
                elements.first.click(force=True)
                page.wait_for_timeout(800)
                if not page.locator(f"text='{active_keyword}'").is_visible():
                    return

        # --- 步骤 4：策略 C - 键盘 ESC 退出 ---
        logger.info("[UI交互] 按钮规则均未命中，尝试 ESC 退出")
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        if not page.locator(f"text='{active_keyword}'").is_visible():
            return

        # --- 步骤 5：策略 D - 物理遮罩层盲狙 (终极手段) ---
        logger.info("[UI交互] 启动终极手段：尝试点击遮罩层盲区")
        # 弹窗外的左上角(10, 10)通常是半透明遮罩层，点击即可触发关闭
        page.mouse.click(10, 10)
        page.wait_for_timeout(800)

        if page.locator(f"text='{active_keyword}'").is_visible():
            page.mouse.click(10, 200)  # 再换个侧边位置尝试

    except Exception as e:
        logger.error(f"[UI交互] 弹窗清理过程发生异常: {str(e)}")


# ==============================================================================
# 顶级进程控制器
# ==============================================================================

def main_controller():
    """守护进程总入口，负责调度宏观生命周期。"""
    pdd_browser_data_list = get_config("pdd_browser_data_list")
    if not pdd_browser_data_list:
        logger.error("[系统/启动] 致命错误: 未配置账号数据池(pdd_browser_data_list), 系统退出。")
        return

    db_instance = gen_db_object()
    try:
        db_instance.ping()
        product_manager = ProductManager(db_instance)
        account_manager = AccountStatusManager(db_instance)
        logger.info("[系统/启动] Mongo存储已就绪 | 容量: [%d] 个活跃账号待命", len(pdd_browser_data_list))
        run_collection_rounds(pdd_browser_data_list, product_manager, account_manager)
    except Exception:
        logger.exception("[系统/终止] 采集器异常退出，未完成的任务不会标记成功")
        raise
    finally:
        db_instance.close()


def run_collection_rounds(pdd_browser_data_list, product_manager, account_manager):
    """循环采集各分类；存储异常向上抛出，页面异常保留原有切号重试。"""
    round_count = 0  # 追踪大循环轮次

    while True:
        round_count += 1
        logger.info(f"[调度/主环] ========== 开始全局新世代遍历 (第 {round_count} 轮) ==========")
        tab_list = None

        round_tab_stats = []

        while not tab_list:
            acc = get_available_account(pdd_browser_data_list, account_manager)
            if not acc:
                wait_sec = GLOBAL_CONFIG["wait_no_account_seconds"]
                logger.info("[调度/等待] 全员进入冷却状态 | 动作: 线程挂起待机 [%d] 秒", wait_sec)
                time.sleep(wait_sec)
                continue

            update_account_usage_time(acc, account_manager)
            tab_list = get_tab_list(acc)

            if tab_list is None:
                logger.warning("[调度/阻断] 探路者被风控拦截 | 策略: 丢弃结果，准备切号重试")
                time.sleep(5)

        if not tab_list:
            logger.warning("[调度/重试] 有效Tab提取量为0 | 可能原因: 页面结构巨变或偶发白屏 | 策略: 挂起重试")
            time.sleep(60)
            continue

        logger.info(f"[调度/分发] 全局路由表生成完毕 | 目标数量: {len(tab_list)} 个 为：{tab_list}")

        for tab_idx, tab in enumerate(tab_list, 1):
            # 将上下文追送入 tab，供内层 scrape_single_tab 使用
            tab["round"] = round_count
            tab["tab_index"] = tab_idx
            tab["total_tabs"] = len(tab_list)

            # 供主控器日志输出使用
            tab_display_main = f"第{round_count}轮-第{tab_idx}/{len(tab_list)}个({tab['name']})"
            tab_completed = False

            tab_total_scrolls = 0
            tab_total_requests = 0
            tab_total_new = 0
            tab_total_update = 0

            # 【新增：熔断机制】定义该 Tab 允许的最大重试次数，防止无限死磕导致整个任务停滞
            tab_retry_count = 0
            MAX_RETRY_PER_TAB = 3

            while not tab_completed:
                current_acc = get_available_account(pdd_browser_data_list, account_manager)

                if not current_acc:
                    wait_sec = GLOBAL_CONFIG["wait_no_account_seconds"]
                    logger.info("[调度/排队] 当前无可用兵力攻坚 | 目标Tab: [%s] | 挂起时长: [%d] 秒", tab_display_main,
                                wait_sec)
                    time.sleep(wait_sec)
                    continue

                update_account_usage_time(current_acc, account_manager)

                status, stats = scrape_single_tab(current_acc, tab, product_manager)

                tab_total_scrolls += stats.get("scrolls", 0)
                tab_total_requests += stats.get("requests", 0)
                tab_total_new += stats.get("new", 0)
                tab_total_update += stats.get("update", 0)

                if status == "SUCCESS":
                    tab_completed = True

                # 【修改：将所有非 SUCCESS 状态全部统一收口为最多3次重试】
                else:
                    tab_retry_count += 1

                    if status == "RISK_CONTROL":
                        reason = "触发风控"
                    elif status == "PAGE_MISMATCH":
                        reason = "UI跑偏或空转"
                    elif status == "ERROR":
                        reason = "系统崩溃异常"
                    else:
                        reason = f"未知异常状态({status})"

                    if tab_retry_count >= MAX_RETRY_PER_TAB:
                        logger.error(
                            "[调度/熔断] 执行体连续挫败已达上限(%d次) | 原因: [%s] | 目标Tab: [%s] | 策略: 强行标记完成，止损跳过",
                            MAX_RETRY_PER_TAB, reason, tab_display_main)
                        tab_completed = True  # 强行终止此 Tab，推进到下一个
                    else:
                        logger.warning(
                            "[调度/切换] 执行体失利 | 原因: [%s] | 牺牲账号: [%s] | 目标Tab: [%s] | 进度: 重试 [%d/%d] | 动作: 申请新账号接力",
                            reason, os.path.basename(current_acc), tab_display_main, tab_retry_count, MAX_RETRY_PER_TAB)

                time.sleep(2)

            round_tab_stats.append({
                "tab_name": tab['name'],
                "scrolls": tab_total_scrolls,
                "requests": tab_total_requests,
                "new": tab_total_new,
                "update": tab_total_update
            })

        logger.info(f"[系统/概览数据] ========== 第 {round_count} 轮 各Tab数据汇总概览 ==========")
        total_scrolls = total_requests = total_new = total_update = 0
        for s in round_tab_stats:
            logger.info(
                f"  -> Tab: [{s['tab_name']}] | 滑动次数: {s['scrolls']} | 捕捉请求: {s['requests']} | 新增个数: {s['new']} | 更新个数: {s['update']}")
            total_scrolls += s['scrolls']
            total_requests += s['requests']
            total_new += s['new']
            total_update += s['update']

        logger.info(
            f"[系统/概览数据] 第 {round_count} 轮 总体大盘统计 -> 滑动总计: {total_scrolls} | 捕捉总计: {total_requests} | 新增总计: {total_new} | 更新总计: {total_update}")
        logger.info(
            f"[系统/阶段里程碑] 🎉 ========== 第 {round_count} 轮 全量路由矩阵遍历成功！准备开启下一轮镜像增量 ========== ")


if __name__ == "__main__":
    main_controller()
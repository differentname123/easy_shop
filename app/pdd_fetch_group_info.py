# [功能摘要] 使用冷却账号池逐分类采集拼多多商品，沿用原有重试规则并增量写入 MongoDB。
# [输入数据] 配置中的浏览器目录列表；导航 DOM；goods_list 接口的 success/result.goods_list JSON。
# [数据流转/交互] 查询账号时间 → 调度时 touch → 探测分类 → 监听 HTTP 200 响应 → 清洗 → 批量 upsert；
#                 页面失败按原规则换号，持久化失败在释放浏览器后上抛；每轮完成后继续下一轮。
# [输出数据] 商品字段交给 ProductManager，沿用 (platform, product_id) 去重约定；账号使用时间交给
#            AccountStatusManager；返回分类状态及统计，按原规则保存本地 PNG/HTML 异常现场。

import logging
import os
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from common.playwright_utils import launch_persistent_context
from common.common_utils import get_config
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager, AccountStatusManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("pdd_scraper")

GLOBAL_CONFIG = {
    "target_url": "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel",
    "platform": "pdd",
    "account_cooldown_minutes": 30,
    "wait_no_account_seconds": 60,
    "max_scrolls_per_tab": -1,
    "headless_mode": True,
    "scroll_step_y": 6000,
    "scroll_interval": 2.0,
}
STAT_KEYS = ("scrolls", "requests", "new", "update")
MAX_ATTEMPTS_PER_TAB = 3
STATUS_REASONS = {
    "RISK_CONTROL": "触发风控",
    "PAGE_MISMATCH": "进入搜索页或未捕获目标响应",
    "ERROR": "页面加载或交互异常",
}


class StorageError(RuntimeError):
    """持久化或写入回执异常必须终止采集，不能当作页面故障换号重试。"""


def normalize_goods(item, tab_name):
    """输入商品 dict，核心键为 goods_id、origin_price/activity_price/group_order_price_reduce（分）。
    输出含 platform/product_id/category 与三项元价格的 dict；非法 ID 返回 None。
    """
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


def get_available_account(account_list, account_manager):
    """入参为目录列表；管理器返回 {目录: datetime/None}；按原顺序返回可用目录或 None。"""
    statuses = account_manager.get_last_used_times(GLOBAL_CONFIG["platform"], account_list)
    now = datetime.now(timezone.utc)
    cooldown = timedelta(minutes=GLOBAL_CONFIG["account_cooldown_minutes"])
    for account in account_list:
        last_used = statuses.get(account)
        if last_used is None:
            return account
        # : 原逻辑将非法时间戳直接视为可用，不修复数据库；可能绕过冷却，需业务确认。
        if not isinstance(last_used, datetime):
            logger.warning("[调度/时间校验] 使用时间格式异常，沿用原规则允许使用 | 账号: [%s] | 排查: [账号状态记录]",
                           os.path.basename(account))
            return account
        if last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=timezone.utc)
        if now - last_used >= cooldown:
            return account
    return None


def update_account_usage_time(account, account_manager):
    """记录目录对应账号的使用时间；数据库错误向上传播，成功后才记录调度日志。"""
    # : 冷却从任务开始计时，风控或结束时不刷新；长任务结束后可能立即再次可用。
    account_manager.touch_account(GLOBAL_CONFIG["platform"], account)
    logger.info("[调度/分配] 账号使用时间已记录 | 账号: [%s] | 冷却: [%d 分钟，自此刻起算]",
                os.path.basename(account), GLOBAL_CONFIG["account_cooldown_minutes"])


def wait_for_account(account_list, account_manager, task_name):
    """入参为目录列表与任务描述；统一冷却等待及 touch，返回已记录使用时间的目录。"""
    while True:
        account = get_available_account(account_list, account_manager)
        if account:
            update_account_usage_time(account, account_manager)
            return account
        wait_seconds = GLOBAL_CONFIG["wait_no_account_seconds"]
        logger.info("[调度/等待] 暂无可用账号 | 任务: [%s] | 再次检查: [%d 秒后]", task_name, wait_seconds)
        time.sleep(wait_seconds)


def save_error_snapshot(page, tab_name, reason):
    """page 为 Playwright Page；尽力保存 PNG/HTML，沿用快照失败不阻断主流程的设计。"""
    try:
        os.makedirs("error_data", exist_ok=True)
        safe_name = "".join(c for c in f"{tab_name}_{reason}" if c.isalnum() or c in "_-") or "unknown"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base_path = os.path.join("error_data", f"{timestamp}_{safe_name}")
        page.screenshot(path=f"{base_path}.png", full_page=True)
        with open(f"{base_path}.html", "w", encoding="utf-8") as snapshot:
            snapshot.write(page.content())
        logger.info("[现场/保存] 截图与源码已保存 | 分类: [%s] | 原因: [%s] | 文件前缀: [%s]",
                    tab_name, reason, base_path)
    except Exception as exc:
        logger.warning("[现场/失败] 保存异常页面失败，继续原流程 | 分类: [%s] | 错误: [%s] | 排查: [页面是否关闭、目录权限]",
                       tab_name, exc)


def check_risk_control(page):
    """根据原有两个文案判断风控，明确选择首个匹配项，避免重复节点触发严格定位异常。"""
    try:
        return any(page.get_by_text(text, exact=True).first.is_visible()
                   for text in ("活动陆续开放中", "回到首页"))
    except Exception as exc:
        # : 沿用检测失败返回 False 的规则；页面关闭或 DOM 异常时可能漏报风控。
        logger.warning("[风控/检测] 无法读取提示文案，沿用未命中结果 | 错误: [%s] | 排查: [页面状态、文案节点]", exc)
        return False


def get_tab_list(user_data_dir):
    """探测导航；返回 [{index: DOM 下标, name: 分类名}]，原有页面失败返回 None、空导航返回 []。"""
    account_name = os.path.basename(user_data_dir)
    started = time.monotonic()
    logger.info("[导航/探测] 开始读取分类 | 账号: [%s]", account_name)
    with sync_playwright() as p, closing(launch_persistent_context(
            p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
            # : 沿用先等待导航、再查风控的顺序；没有导航的拦截页仍走页面异常路径。
            page.locator("#brand-first-nav").wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(3000)
            if check_risk_control(page):
                logger.warning("[导航/拦截] 首页出现风控提示，交回调度重试 | 账号: [%s] | 排查: [账号访问限制]", account_name)
                return None
            tabs = page.evaluate(r"""
                () => Array.from(document.querySelectorAll('#brand-first-nav > div')).map((tab, index) => {
                    let name = tab.innerText.replace('\n', '').trim();
                    if (!name) {
                        const img = tab.querySelector('img');
                        name = img ? (img.getAttribute('aria-label') || img.getAttribute('alt')
                                      || '图片标签_' + index) : '';
                    }
                    return {index, name: name.trim() || `未知标签_${index}`};
                })
            """)
            if not tabs:
                logger.warning("[导航/空结果] 未找到分类，按原规则继续探测 | 账号: [%s] | 排查: [导航结构变化、页面尚未渲染]",
                               account_name)
                return tabs
            logger.info("[导航/完成] 分类读取成功 | 账号: [%s] | 分类: [%s] | 耗时: [%.1f 秒]",
                        account_name, "; ".join(f"{tab['index']}:{tab['name']}" for tab in tabs), time.monotonic() - started)
            return tabs
        except Exception as exc:
            logger.warning("[导航/失败] 读取分类失败，交回调度重试 | 账号: [%s] | 错误: [%s] "
                           "| 排查: [网络、导航节点、脚本执行]",
                           account_name, exc)
            return None


def clear_popups(page):
    """page 为 Playwright Page；按原顺序尝试图片、按钮、ESC、遮罩，保留清理失败后继续采集的容错。"""
    try:
        page.wait_for_timeout(1500)
        for keyword in ("多人团限时优惠", "立即抢购", "限时优惠", "爆款商品"):
            popup = page.locator(f"text='{keyword}'").first
            if popup.is_visible():
                break
        else:
            return

        # : 保留“含文案的末个 div 内首张图片”规则；它可能是商品图片，需确认关闭按钮特征。
        close_image = page.locator("div").filter(has_text=keyword).last.locator("img").first
        if close_image.is_visible():
            close_image.click(force=True)
            page.wait_for_timeout(1000)
            if not popup.is_visible():
                logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [容器图片]", keyword)
                return

        for selector in ("text='关闭'", "text='跳过'", "[class*='close' i]", ".am-modal-close"):
            button = page.locator(selector).first
            if not button.is_visible():
                continue
            button.click(force=True)
            page.wait_for_timeout(800)
            if not popup.is_visible():
                logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [%s]", keyword, selector)
                return

        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        if not popup.is_visible():
            logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [ESC]", keyword)
            return
        # : 保留原有固定坐标点击；页面布局变化时可能误点，第二次点击后仍不额外等待确认。
        page.mouse.click(10, 10)
        page.wait_for_timeout(800)
        if not popup.is_visible():
            logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [遮罩坐标]", keyword)
            return
        page.mouse.click(10, 200)
        logger.warning("[弹窗/待确认] 常规关闭方式未成功，已尝试备用坐标 | 文案: [%s] | 排查: [弹窗结构、遮罩位置]", keyword)
    except Exception as exc:
        logger.warning("[弹窗/失败] 自动关闭发生异常，沿用继续采集规则 | 错误: [%s] | 排查: [按钮定位、页面跳转]", exc)


def scrape_single_tab(user_data_dir, tab_info, product_manager):
    """tab_info 必含 index/name，可含 round/tab_index/total_tabs；返回 (状态, {scrolls, requests, new, update})。
    商品列表交给原 upsert_products，回执必含 new/update；存储异常延迟到浏览器释放后上抛。
    """
    tab_name = tab_info["name"]
    tab_display = (f"第{tab_info.get('round', 1)}轮-第{tab_info.get('tab_index', 1)}/"
                   f"{tab_info.get('total_tabs', 1)}个({tab_name})")
    account_name = os.path.basename(user_data_dir)
    stats = dict.fromkeys(STAT_KEYS, 0)
    storage_error = None
    hit_risk = False
    started = time.monotonic()
    logger.info("[采集/开始] 准备加载目标分类 | 分类: [%s] | 账号: [%s]", tab_display, account_name)

    def raise_storage_error():
        """将回调保存的异常交回同步流程，防止页面重试或提前返回掩盖持久化故障。"""
        if storage_error is not None:
            raise StorageError(f"账号 [{account_name}] 分类 [{tab_display}] 入库或回执处理失败；"
                               "请检查 Mongo 连接、写入权限及 upsert_products 的 new/update 回执") from storage_error

    def handle_response(response):
        """Response JSON 核心形貌为 {success, result: {goods_list: [dict]}}；就地更新四项 stats。"""
        nonlocal hit_risk, storage_error
        if storage_error is not None:
            return
        if "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return
        # : requests 沿用目标接口 HTTP 200 响应数，包含无效 JSON、失败业务响应及空商品批次。
        stats["requests"] += 1
        # : 保留价格异常等解析错误导致整批跳过的规则，不擅自转换字段或仅丢弃单个商品。
        try:
            data = response.json()
            if not isinstance(data, dict) or not data.get("success") or "result" not in data:
                return
            result = data["result"]
            if not isinstance(result, dict):
                return
            # : 保留成功结构中全文匹配 risk 的规则；商品文字可能误报，失败响应中的风控可能漏报。
            if "risk" in str(data).lower():
                hit_risk = True
            records = []
            for item in result.get("goods_list", []) or []:
                if not isinstance(item, dict):
                    continue
                record = normalize_goods(item, tab_name)
                if record is not None:
                    records.append(record)
        except Exception as exc:
            logger.warning("[采集/解析] 本批数据未入库，按原规则继续监听 | 分类: [%s] | 错误: [%s] | 排查: [JSON结构、价格字段]",
                           tab_display, exc)
            return
        if not records:
            return
        try:
            counts = product_manager.upsert_products(records)
            totals = (stats["new"] + counts["new"], stats["update"] + counts["update"])
        except Exception as exc:
            storage_error = exc
            return
        stats.update(new=totals[0], update=totals[1])
        logger.debug("[存储/批次] 商品写入成功 | 分类: [%s] | 新增: [%d] | 更新: [%d]",
                     tab_display, counts["new"], counts["update"])

    try:
        with sync_playwright() as p, closing(launch_persistent_context(
                p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
            page = context.pages[0] if context.pages else context.new_page()
            # : 保留点击分类前注册监听的时机；首页及迟到响应仍归入目标分类，需确认请求归属。
            page.on("response", handle_response)
            try:
                page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
                raise_storage_error()
                # : 保留先等待导航、再查风控；缺失导航的拦截页仍按页面 ERROR 处理。
                navigation = page.locator("#brand-first-nav")
                navigation.wait_for(state="visible", timeout=15000)
                # 保留原等待时长，同时让同步 Playwright 调度响应回调。
                page.wait_for_timeout(3000)
                raise_storage_error()
                if check_risk_control(page):
                    if stats["requests"] < 10:
                        save_error_snapshot(page, tab_name, "风控拦截_请求不足10次")
                    return "RISK_CONTROL", stats

                clear_popups(page)
                raise_storage_error()
                navigation.wait_for(state="visible", timeout=5000)
                # : 沿用探测账号的 DOM 下标；不同账号的分类顺序若不同，可能点错分类。
                navigation.locator(":scope > div").nth(tab_info["index"]).evaluate("node => node.click()")
                page.wait_for_timeout(3500)
                raise_storage_error()
                if page.locator("input[type='search']").first.is_visible():
                    save_error_snapshot(page, tab_name, "异常跑偏_误入搜索页")
                    return "PAGE_MISMATCH", stats

                max_scrolls = GLOBAL_CONFIG["max_scrolls_per_tab"]
                stop_reason = "达到配置滑动上限"
                idle_scrolls = 0
                last_response_count = stats["requests"]
                while True:
                    raise_storage_error()
                    if max_scrolls != -1 and stats["scrolls"] >= max_scrolls:
                        break
                    if check_risk_control(page) or hit_risk:
                        if stats["requests"] < 10:
                            save_error_snapshot(page, tab_name, "滑动拦截_请求不足10次")
                        return "RISK_CONTROL", stats
                    page.mouse.wheel(0, GLOBAL_CONFIG["scroll_step_y"])
                    stats["scrolls"] += 1
                    page.wait_for_timeout(GLOBAL_CONFIG["scroll_interval"] * 1000)
                    raise_storage_error()

                    if max_scrolls == -1:
                        idle_scrolls = 0 if stats["requests"] > last_response_count else idle_scrolls + 1
                        last_response_count = stats["requests"]
                        # : 保留十次无新 HTTP 200 响应即结束的规则；慢响应或网络故障可能被误判为到底。
                        if idle_scrolls >= 10:
                            stop_reason = "连续十次滑动无新目标 HTTP 200 响应"
                            break
                    if stats["scrolls"] % 10 == 0:
                        limit = "不限" if max_scrolls == -1 else max_scrolls
                        logger.info("[采集/进度] 分类加载中 | 分类: [%s] | 滑动: [%d/%s] | 空转: [%d] "
                                    "| 响应: [%d] | 新增/更新: [%d/%d]",
                                    tab_display, stats["scrolls"], limit, idle_scrolls,
                                    stats["requests"], stats["new"], stats["update"])
                if stats["requests"] < 10:
                    save_error_snapshot(page, tab_name, "正常结束_请求不足10次")
            except StorageError:
                raise
            except Exception as exc:
                raise_storage_error()
                logger.warning("[采集/异常] 页面作业失败，交回调度重试 | 分类: [%s] | 账号: [%s] | 错误: [%s] | 排查: [网络、导航或点击目标]",
                               tab_display, account_name, exc)
                save_error_snapshot(page, tab_name, "代码崩溃异常")
                return "ERROR", stats
    finally:
        # 共享 stats 保留关闭时的回调计数；存储故障优先于提前返回及浏览器关闭异常。
        raise_storage_error()

    if stats["requests"] == 0:
        return "PAGE_MISMATCH", stats
    # : 原逻辑以存在目标 HTTP 200 响应判成功；即使商品为空或全部解析失败，仍可能返回 SUCCESS。
    logger.info("[采集/完成] 分类作业结束 | 分类: [%s] | 账号: [%s] | 滑动/响应: [%d/%d] "
                "| 新增/更新: [%d/%d] | 耗时: [%.1f 秒] | 结束依据: [%s]",
                tab_display, account_name, stats["scrolls"], stats["requests"],
                stats["new"], stats["update"], time.monotonic() - started, stop_reason)
    return "SUCCESS", stats


def run_collection_rounds(pdd_browser_data_list, product_manager, account_manager):
    """入参为目录列表与原管理器；分类 dict 必含 index/name，跨账号累计 scrolls/requests/new/update。"""
    round_count = 0
    while True:
        round_count += 1
        started = time.monotonic()
        logger.info("[调度/轮次开始] 准备探测并遍历分类 | 轮次: [%d]", round_count)
        tabs = None
        while not tabs:
            account = wait_for_account(pdd_browser_data_list, account_manager, "探测分类")
            tabs = get_tab_list(account)
            if tabs is None:
                time.sleep(5)

        round_stats = []
        for tab_index, tab in enumerate(tabs, 1):
            tab_info = dict(tab, round=round_count, tab_index=tab_index, total_tabs=len(tabs))
            tab_display = f"第{round_count}轮-第{tab_index}/{len(tabs)}个({tab['name']})"
            totals = dict.fromkeys(STAT_KEYS, 0)
            # : 保留最多三次失败即结束当前分类的规则；跳过仍推进下一分类，不代表采集成功。
            for attempt in range(1, MAX_ATTEMPTS_PER_TAB + 1):
                account = wait_for_account(pdd_browser_data_list, account_manager, tab_display)
                status, stats = scrape_single_tab(account, tab_info, product_manager)
                for key in STAT_KEYS:
                    totals[key] += stats.get(key, 0)
                if status != "SUCCESS":
                    reason = STATUS_REASONS.get(status, f"未知状态({status})")
                    if attempt == MAX_ATTEMPTS_PER_TAB:
                        logger.error("❌ [调度/跳过] 分类失败达到上限，结束本分类 | 分类: [%s] "
                                     "| 账号: [%s] | 失败: [%d 次] | 原因: [%s] | 排查: [账号限制、页面结构或加载情况]",
                                     tab_display, os.path.basename(account), attempt, reason)
                    else:
                        logger.warning("[调度/重试] 分类尚未完成，重新申请可用账号 | 分类: [%s] "
                                       "| 账号: [%s] | 尝试: [%d/%d] | 原因: [%s] | 排查: [风控提示、异常快照]",
                                       tab_display, os.path.basename(account), attempt, MAX_ATTEMPTS_PER_TAB, reason)
                time.sleep(2)
                if status == "SUCCESS":
                    break
            round_stats.append({"tab_name": tab["name"], "status": status, **totals})

        totals = {key: sum(item[key] for item in round_stats) for key in STAT_KEYS}
        successful = sum(item["status"] == "SUCCESS" for item in round_stats)
        details = "; ".join(
            f"{item['tab_name']}[状态={item['status']}, 滑动/响应={item['scrolls']}/{item['requests']}, "
            f"新增/更新={item['new']}/{item['update']}]" for item in round_stats)
        logger.info("[调度/轮次完成] 分类遍历结束，进入下一轮 | 轮次: [%d] | 成功/跳过: [%d/%d] "
                    "| 滑动/响应: [%d/%d] | 新增/更新: [%d/%d] | 耗时: [%.1f 秒] | 分类明细: [%s]",
                    round_count, successful, len(round_stats) - successful, totals["scrolls"], totals["requests"],
                    totals["new"], totals["update"], time.monotonic() - started, details)


def main_controller():
    """读取 pdd_browser_data_list 目录列表并启动守护循环；数据库资源在正常及异常退出时关闭。"""
    try:
        accounts = get_config("pdd_browser_data_list")
        if not accounts:
            logger.error("❌ [系统/启动失败] 未配置采集账号 | 配置项: [pdd_browser_data_list] | 排查: [浏览器目录列表]")
            return
        with closing(gen_db_object()) as db_instance:
            db_instance.ping()
            product_manager = ProductManager(db_instance)
            account_manager = AccountStatusManager(db_instance)
            logger.info("[系统/就绪] 数据库连接成功，开始调度 | 配置账号: [%d]", len(accounts))
            run_collection_rounds(accounts, product_manager, account_manager)
    except Exception as exc:
        logger.exception("❌ [系统/终止] 采集器异常退出，本轮未完成 | 错误: [%s] "
                         "| 排查: [异常链中的配置、数据库、回执或浏览器初始化错误]", exc)
        raise


if __name__ == "__main__":
    main_controller()
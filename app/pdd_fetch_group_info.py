# -*- coding: utf-8 -*-
# ==============================================================================
# [功能摘要]
# 基于动态账号池调度与单标签(Tab)细粒度抓取的拼多多增量采集器，具备防风控冷却与断点续采能力。
#
# [输入数据]
# 1. pdd_browser_data_list: 外部提供的本地浏览器用户数据目录路径列表 (List of Strings)。
# 2. 目标页面 XHR 接口 (brand-group-home/home/goods_list) 返回的未清洗 JSON 结构。
#
# [数据流转/交互]
# 1. 主控器查询 account_status.json，筛选冷却期大于 30 分钟的可用账号。
# 2. 调度账号访问主页，注入 JS 提取页面顶部 Tab 列表特征 (含 name 与 DOM index)。
# 3. 将 (账号, 目标Tab) 指派给采集引擎，引擎拦截网络请求进行 JSON 数据清洗去重。
# 4. 触发风控时，立即封存当前账号时间戳并打断流程，外层调度器无缝切换新账号接力。
#
# [输出数据]
# 更新落盘到 pdd_group_items.csv 文件中，包含商品核心字段（价格、名称、销量、URL等）。
# ==============================================================================

import os
import time
import csv
import logging
import json
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

from common.playwright_utils import launch_persistent_context
from common.common_utils import get_config

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
    "output_csv": "pdd_group_items.csv",
    "account_status_json": "account_status.json",
    "account_cooldown_minutes": 30,
    "wait_no_account_seconds": 60,
    "max_scrolls_per_tab": -1,  # 修改为 -1 表示直到连续10次无新请求则视为到底
    "headless_mode": True,
    "scroll_step_y": 3000,
    "scroll_interval": 2.0,
}


# ==============================================================================
# 基础工具与存储逻辑
# ==============================================================================

def read_json(json_path):
    """读取 JSON 文件。返回空字典以兜底文件不存在或损坏的情况。"""
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error("[配置/读取] JSON文件解析异常 | 文件: [%s] | 错误: [%s]", json_path, str(e))
        return {}


def save_json(json_path, data):
    """持久化 JSON 数据，保证原子性或尽力写入。"""
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error("[配置/写入] JSON文件写入失败 | 文件: [%s] | 错误: [%s]", json_path, str(e))


def load_existing_goods_dict(filename):
    """
    加载历史商品数据以支持增量去重。
    返回形态: { "商品ID_str": {商品完整属性Dict} }
    注：此处发生异常采用降级策略(静默返回已有或空字典)，防止文件被锁导致主进程崩溃。
    """
    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        return {}

    existing_goods = {}
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                goods_id = str(row.get('商品ID', '')).strip()
                if goods_id:
                    existing_goods[goods_id] = row
    except Exception as e:
        logger.error("[存储/读取] 本地历史CSV读取崩溃 | 文件: [%s] | 原因: [%s]", filename, str(e))

    return existing_goods


def save_to_csv_atomic(goods_dict, filename):
    """
    原子化保存 CSV 文件。通过 tmp 后缀替换，防止写入中断导致全量数据丢失。
    注：发生异常仅拦截打印，允许当前批次落盘失败(数据仍在内存中)，等待下批重试。
    """
    if not goods_dict:
        return

    fieldnames = ["商品ID", "所属标签", "商品名称", "品牌", "原价(元)", "补贴价(元)", "立省(元)", "销量提示",
                  "商品链接", "主图链接", "更新时间"]
    tmp_filename = f"{filename}.tmp"

    try:
        with open(tmp_filename, 'w', encoding='utf-8-sig', newline='') as output_file:
            dict_writer = csv.DictWriter(output_file, fieldnames=fieldnames, extrasaction='ignore')
            dict_writer.writeheader()
            dict_writer.writerows(goods_dict.values())
        os.replace(tmp_filename, filename)
    except Exception as e:
        logger.error("[存储/写入] CSV原子替换失败 | 目标文件: [%s] | 原因: [%s]", filename, str(e))


# ==============================================================================
# 账号状态与调度调度
# ==============================================================================

def get_available_account(account_list):
    """
    查询满足冷却时长的空闲账号。
    出参: 返回可用账号的物理路径 (String)，全忙碌则返回 None。
    """
    status_dict = read_json(GLOBAL_CONFIG["account_status_json"])
    now = datetime.now()
    cooldown_delta = timedelta(minutes=GLOBAL_CONFIG["account_cooldown_minutes"])

    for account in account_list:
        last_used_str = status_dict.get(account)
        if not last_used_str:
            return account  # 全新账号，无历史记录

        try:
            last_used_time = datetime.strptime(last_used_str, "%Y-%m-%d %H:%M:%S")
            if now - last_used_time >= cooldown_delta:
                return account
        except ValueError:
            logger.warning("[调度/校验] 发现脏数据时间戳 | 账号: [%s] | 动作: 强制重置可用", os.path.basename(account))
            return account

    return None


def update_account_usage_time(account):
    """刷新指定账号的心跳时间戳。"""
    status_dict = read_json(GLOBAL_CONFIG["account_status_json"])
    status_dict[account] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(GLOBAL_CONFIG["account_status_json"], status_dict)
    logger.info("[调度/锁定] 账号已切入工作态 | 账号: [%s] | 冷却倒计时: [%d 分钟]",
                os.path.basename(account), GLOBAL_CONFIG["account_cooldown_minutes"])


# ==============================================================================
# 核心抓取与 DOM 交互引擎
# ==============================================================================

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
            page.goto(GLOBAL_CONFIG["target_url"])
            page.wait_for_load_state("networkidle")
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
                        name = img ? (img.getAttribute('alt') || "图片标签") : "";
                    }
                    return { index: index, name: name || `未知标签_${index}` };
                });
            }
            """

            page.locator('#brand-first-nav').wait_for(state="visible", timeout=10000)
            tab_list = page.evaluate(js_extract_code)
            logger.info("[探测/路由] Tab解析完成 | 账号: [%s] | 结果: 提取到 [%d] 个分类", acc_name, len(tab_list))
            return tab_list

        except Exception as e:
            logger.error("[探测/异常] DOM拉取或注入失败 | 账号: [%s] | 原因: 网络超时或节点未渲染, %s", acc_name,
                         str(e))
            return None
        finally:
            context.close()


def scrape_single_tab(user_data_dir, tab_info):
    """
    单 Tab 深度遍历模块。
    入参 `tab_info` 必须包含核心 Key: ["index", "name"]，并预期携带 ["round", "tab_index", "total_tabs"] 等上下文。
    出参: 状态码字符串 "SUCCESS" / "RISK_CONTROL" / "ERROR"
    """
    target_index = tab_info["index"]
    tab_name = tab_info["name"]

    # 构造带轮次和序号的显示用名称，以便更好排查日志
    round_info = tab_info.get("round", 1)
    tab_idx = tab_info.get("tab_index", 1)
    total_tabs = tab_info.get("total_tabs", 1)
    tab_display = f"第{round_info}轮-第{tab_idx}/{total_tabs}个({tab_name})"

    acc_name = os.path.basename(user_data_dir)
    output_csv = GLOBAL_CONFIG["output_csv"]

    logger.info("[采集/初始化] 开启专项抓取 | 目标Tab: [%s] | 执行账号: [%s]", tab_display, acc_name)

    seen_goods_dict = load_existing_goods_dict(output_csv)

    # 提前定义统计变量，保证最后打印日志时可直接调用
    session_new_count = 0
    session_update_count = 0
    hit_risk = False
    api_response_count = 0  # 追踪有效拦截到的目标 API 数量
    scroll_count = 0  # 追踪总下滑次数

    def handle_response(response):
        """XHR 拦截闭包，利用早期返回（Early Return）过滤无效报文"""
        nonlocal session_new_count, session_update_count, hit_risk, api_response_count

        # 卫语句：拦截非目标与失败请求
        if "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return

        api_response_count += 1  # 无论是否带有新数据，都记录拦截到了目标请求

        try:
            data = response.json()
        except Exception:
            return  # 容错：服务端返回脏JSON无法解析，直接跳过

        # 卫语句：拦截无效结构
        if not data.get("success") or "result" not in data:
            return

        if "risk" in str(data).lower():
            hit_risk = True

        goods_list = data["result"].get("goods_list", [])
        if not goods_list:
            return

        # 核心逻辑全局屏障：防止服务端极度畸形数据引发抛错，导致后台异步监听器彻底崩溃
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            batch_new, batch_update = 0, 0

            for item in goods_list:
                goods_id = str(item.get("goods_id", "")).strip()
                if not goods_id:
                    continue

                # 防御性转换：防止服务端返回 None 导致计算崩溃
                origin_price_raw = item.get("origin_price") or 0
                activity_price_raw = item.get("activity_price") or 0
                reduce_price_raw = item.get("group_order_price_reduce") or 0

                parsed_item = {
                    "商品ID": goods_id,
                    "所属标签": tab_name,  # 数据入库时，依旧保持纯净标签名
                    "商品名称": item.get("goods_name", ""),
                    "品牌": item.get("brand_name", ""),
                    "原价(元)": origin_price_raw / 100,
                    "补贴价(元)": activity_price_raw / 100,
                    "立省(元)": reduce_price_raw / 100,
                    "销量提示": item.get("sales_tip", ""),
                    "商品链接": "https://mobile.pinduoduo.com/" + str(item.get("link_url", "")),
                    "主图链接": item.get("hd_thumb_url", ""),
                    "更新时间": current_time
                }

                if goods_id in seen_goods_dict:
                    session_update_count += 1
                    batch_update += 1
                else:
                    session_new_count += 1
                    batch_new += 1

                seen_goods_dict[goods_id] = parsed_item

            if batch_new > 0 or batch_update > 0:
                logger.info("[采集/拦截] 解析有效数据流 | Tab: [%s] | 本批新增: [%d] 本批更新: [%d] | 库容: [%d]",
                            tab_display, batch_new, batch_update, len(seen_goods_dict))

                save_to_csv_atomic(seen_goods_dict, output_csv)

        except Exception as e:
            logger.error("[采集/拦截] 解析数据流发生未捕获异常 | Tab: [%s] | 错误: %s", tab_display, str(e))

    # ---------------- 页面导航与滑动主流程 ----------------
    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", handle_response)

        try:
            page.goto(GLOBAL_CONFIG["target_url"])
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            if check_risk_control(page):
                logger.warning("[采集/阻断] 入口页校验未通过 | 账号: [%s] | 结论: 已触发严格风控", acc_name)
                return "RISK_CONTROL"

            nav_container = page.locator('#brand-first-nav')
            nav_container.wait_for(state="visible", timeout=10000)

            target_tab_element = nav_container.locator('> div').nth(target_index)
            target_tab_element.scroll_into_view_if_needed()
            target_tab_element.click(force=True)
            time.sleep(3.5)

            max_scrolls = GLOBAL_CONFIG["max_scrolls_per_tab"]
            no_new_req_count = 0
            last_api_count = api_response_count

            while True:
                # 定量滑动退出条件
                if max_scrolls != -1 and scroll_count >= max_scrolls:
                    break

                if check_risk_control(page) or hit_risk:
                    logger.warning("[采集/阻断] 滚动链路遭受拦截 | 账号: [%s] | 中断节点: 第 [%d] 次滑动", acc_name,
                                   scroll_count + 1)
                    return "RISK_CONTROL"

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

                # 当模式为 -1 时，利用 XHR 请求计数判定是否到底
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

        except Exception as e:
            logger.error("[采集/崩溃] 页面渲染或交互异常 | Tab: [%s] | 错误详情: %s", tab_display, str(e))
            return "ERROR"
        finally:
            context.close()

    # 此处在原基础上增加了 [下滑次数: %d] 和 [捕捉请求: %d]
    logger.info(
        "[采集/完毕] 单一Tab节点作业结束 | Tab: [%s] | 汇总 -> 下滑次数: [%d], 捕捉请求: [%d], 新增: [%d], 更新: [%d]",
        tab_display, scroll_count, api_response_count, session_new_count, session_update_count)
    return "SUCCESS"


# ==============================================================================
# 顶级进程控制器
# ==============================================================================

def main_controller():
    """守护进程总入口，负责调度宏观生命周期。"""
    pdd_browser_data_list = get_config("pdd_browser_data_list")
    if not pdd_browser_data_list:
        logger.error("[系统/启动] 致命错误: 未配置账号数据池(pdd_browser_data_list), 系统退出。")
        return

    logger.info("[系统/启动] 守护引擎已挂载 | 容量: [%d] 个活跃账号待命", len(pdd_browser_data_list))

    round_count = 0  # 追踪大循环轮次

    while True:
        round_count += 1
        logger.info(f"[调度/主环] ========== 开始全局新世代遍历 (第 {round_count} 轮) ==========")
        tab_list = None

        while not tab_list:
            acc = get_available_account(pdd_browser_data_list)
            if not acc:
                wait_sec = GLOBAL_CONFIG["wait_no_account_seconds"]
                logger.info("[调度/等待] 全员进入冷却状态 | 动作: 线程挂起待机 [%d] 秒", wait_sec)
                time.sleep(wait_sec)
                continue

            update_account_usage_time(acc)
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

            while not tab_completed:
                current_acc = get_available_account(pdd_browser_data_list)

                if not current_acc:
                    wait_sec = GLOBAL_CONFIG["wait_no_account_seconds"]
                    logger.info("[调度/排队] 当前无可用兵力攻坚 | 目标Tab: [%s] | 挂起时长: [%d] 秒", tab_display_main,
                                wait_sec)
                    time.sleep(wait_sec)
                    continue

                update_account_usage_time(current_acc)
                status = scrape_single_tab(current_acc, tab)

                if status == "SUCCESS":
                    tab_completed = True
                elif status == "RISK_CONTROL":
                    logger.warning(
                        "[调度/切换] 执行体被击溃(风控) | 牺牲账号: [%s] | 未竟目标: [%s] | 动作: 申请新账号接力",
                        os.path.basename(current_acc), tab_display_main)
                elif status == "ERROR":
                    logger.error("[调度/跳过] 执行体遭遇毁灭性系统报错 | 目标Tab: [%s] | 策略: 强制标记为完成并跳过",
                                 tab_display_main)
                    tab_completed = True

                time.sleep(2)

        logger.info(
            f"[系统/阶段里程碑] 🎉 ========== 第 {round_count} 轮 全量路由矩阵遍历成功！准备开启下一轮镜像增量 ========== ")


if __name__ == "__main__":
    main_controller()
# -*- coding: utf-8 -*-
# ==============================================================================
# [功能摘要] 基于动态账号池调度与单标签(Tab)细粒度抓取的拼多多增量采集器。
# [重构亮点]
#   1. 单Tab任务拆分：避免单一账号长时间驻留页面触发风控。
#   2. 智能账号调度：通过 JSON 记录使用时间，按 30 分钟冷却期动态分配空闲账号。
#   3. Tab 级容错：遇风控自动切换新账号重试当前 Tab。
#   4. 强兼容：图片型 Tab 名称智能提取。
# ==============================================================================

import os
import time
import csv
import logging
import json
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

from common.playwright_utils import launch_persistent_context
from common.common_utils import get_config  # 假设你原始环境有这个函数

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger("pdd_scraper")

# ==============================================================================
# 1. 核心全局配置 (参数集中管理，方便灵活修改)
# ==============================================================================
GLOBAL_CONFIG = {
    "target_url": "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel",
    "output_csv": "pdd_group_items.csv",
    "account_status_json": "account_status.json",  # 记录账号上次使用时间的文件
    "account_cooldown_minutes": 30,  # 账号强制冷却时间(分钟)
    "wait_no_account_seconds": 60,  # 无可用账号时的轮询等待时间(秒)
    "max_scrolls_per_tab": 100,  # 每个 Tab 的最大滚动次数
    "headless_mode": True,  # 是否无头模式运行
    "scroll_step_y": 3000,  # 每次滚动的像素距
    "scroll_interval": 2.0,  # 每次滚动间隔(秒)
}


# ==============================================================================
# 2. 基础工具函数 (JSON与CSV读写)
# ==============================================================================
def read_json(json_path):
    """读取JSON文件"""
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取 JSON 失败 [%s]: %s", json_path, e)
        return {}


def save_json(json_path, data):
    """保存JSON文件"""
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error("写入 JSON 失败 [%s]: %s", json_path, e)


def load_existing_goods_dict(filename):
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
        logger.error("读取本地历史CSV异常: %s", e)
    return existing_goods


def save_to_csv_atomic(goods_dict, filename):
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
        logger.error("CSV 文件原子保存失败: %s", e)


# ==============================================================================
# 3. 账号动态调度逻辑
# ==============================================================================
def get_available_account(account_list):
    """
    遍历账号池，查询有无满足冷却条件(默认30分钟)的账号。
    返回: 可用账号路径(str) 或 None
    """
    status_dict = read_json(GLOBAL_CONFIG["account_status_json"])
    now = datetime.now()

    for account in account_list:
        last_used_str = status_dict.get(account)
        if not last_used_str:
            # 记录里没有，说明是全新账号，直接可用
            return account

        last_used_time = datetime.strptime(last_used_str, "%Y-%m-%d %H:%M:%S")
        cooldown_delta = timedelta(minutes=GLOBAL_CONFIG["account_cooldown_minutes"])

        if now - last_used_time >= cooldown_delta:
            return account

    return None


def update_account_usage_time(account):
    """更新某个账号的最后使用时间为当前时间"""
    status_dict = read_json(GLOBAL_CONFIG["account_status_json"])
    status_dict[account] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(GLOBAL_CONFIG["account_status_json"], status_dict)
    logger.info("已更新账号 [%s] 的使用时间。", os.path.basename(account))


# ==============================================================================
# 4. Playwright 页面控制与数据抓取核心模块
# ==============================================================================
def check_risk_control(page):
    """通用风控检测逻辑"""
    try:
        risk_notice = page.get_by_text("活动陆续开放中", exact=True)
        home_btn = page.get_by_text("回到首页", exact=True)
        if risk_notice.is_visible() or home_btn.is_visible():
            return True
    except Exception:
        pass
    return False


def get_tab_list(user_data_dir):
    """
    获取页面顶部 Tab 列表（兼容图片型 Tab）。
    返回结构: [{"index": 0, "name": "精选"}, {"index": 1, "name": "手机"}, ...]
    如果遇到风控或加载失败，返回 None
    """
    logger.info("正在使用账号 [%s] 获取最新 Tab 列表...", os.path.basename(user_data_dir))
    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(GLOBAL_CONFIG["target_url"])
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            if check_risk_control(page):
                logger.warning("获取 Tab 列表时遭遇风控拦截！")
                return None

            # 通过 JS 在浏览器上下文中一次性高效提取，完美兼容纯图片节点
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
            try:
                page.locator('#brand-first-nav').wait_for(state="visible", timeout=10000)
                tab_list = page.evaluate(js_extract_code)
                logger.info("成功获取到 %s 个 Tab 节点。", len(tab_list))
                return tab_list
            except Exception as e:
                logger.error("定位或提取 Tab 导航失败: %s", e)
                return []

        except Exception as e:
            logger.error("获取 Tab 列表异常: %s", e)
            return None
        finally:
            context.close()


def scrape_single_tab(user_data_dir, tab_info):
    """
    使用指定账号，仅仅抓取传入的指定的某一个 Tab。
    返回状态码: "SUCCESS", "RISK_CONTROL", "ERROR"
    """
    target_index = tab_info["index"]
    tab_name = tab_info["name"]
    output_csv = GLOBAL_CONFIG["output_csv"]

    logger.info(">>> 开始定向采集 Tab: [%s] | 调度账号: [%s]", tab_name, os.path.basename(user_data_dir))

    seen_goods_dict = load_existing_goods_dict(output_csv)
    session_new_count, session_update_count = 0, 0
    hit_risk = False

    def handle_response(response):
        nonlocal session_new_count, session_update_count, hit_risk
        if "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return

        try:
            data = response.json()
            if not data.get("success") or "result" not in data:
                return

            # 部分风控可能会在 API 返回数据中体现
            if "risk" in str(data).lower():
                hit_risk = True

            goods_list = data["result"].get("goods_list", [])
            if not goods_list:
                return

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in goods_list:
                goods_id = str(item.get("goods_id", "")).strip()
                if not goods_id: continue

                parsed_item = {
                    "商品ID": goods_id,
                    "所属标签": tab_name,
                    "商品名称": item.get("goods_name", ""),
                    "品牌": item.get("brand_name", ""),
                    "原价(元)": item.get("origin_price", 0) / 100,
                    "补贴价(元)": item.get("activity_price", 0) / 100,
                    "立省(元)": item.get("group_order_price_reduce", 0) / 100,
                    "销量提示": item.get("sales_tip", ""),
                    "商品链接": "https://mobile.pinduoduo.com/" + str(item.get("link_url", "")),
                    "主图链接": item.get("hd_thumb_url", ""),
                    "更新时间": current_time
                }

                if goods_id in seen_goods_dict:
                    session_update_count += 1
                else:
                    session_new_count += 1
                seen_goods_dict[goods_id] = parsed_item

            # 增量落地
            save_to_csv_atomic(seen_goods_dict, output_csv)
        except Exception:
            pass

    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", handle_response)

        try:
            page.goto(GLOBAL_CONFIG["target_url"])
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            if check_risk_control(page):
                logger.warning("🚨 [风控拦截] 进入页面时触发风控！")
                return "RISK_CONTROL"

            # 定位并点击对应的 Tab
            nav_container = page.locator('#brand-first-nav')
            nav_container.wait_for(state="visible", timeout=10000)

            target_tab_element = nav_container.locator('> div').nth(target_index)
            target_tab_element.scroll_into_view_if_needed()
            target_tab_element.click(force=True)
            time.sleep(3.5)  # 等待数据刷新与DOM渲染

            # 持续下拉动作
            for _ in range(GLOBAL_CONFIG["max_scrolls_per_tab"]):
                # 滚动中再次检测风控
                if check_risk_control(page) or hit_risk:
                    logger.warning("🚨 [风控拦截] 滑动过程中触发风控！")
                    return "RISK_CONTROL"

                page.mouse.wheel(0, GLOBAL_CONFIG["scroll_step_y"])
                time.sleep(GLOBAL_CONFIG["scroll_interval"])

        except Exception as e:
            logger.error("采集单 Tab 发生异常: %s", e)
            return "ERROR"
        finally:
            context.close()

    logger.info("✅ Tab [%s] 采集结束 | 新增: %s | 更新: %s | 总库容: %s",
                tab_name, session_new_count, session_update_count, len(seen_goods_dict))
    return "SUCCESS"


# ==============================================================================
# 5. 主控循环引擎 (守护进程)
# ==============================================================================
def main_controller():
    # 假设你通过此函数获取了包含所有 user_data_dir 的列表
    pdd_browser_data_list = get_config("pdd_browser_data_list")
    if not pdd_browser_data_list:
        logger.error("未找到配置的账号数据目录(pdd_browser_data_list)，程序退出。")
        return

    logger.info("🚀 启动主守护进程，发现 %d 个配置账号。", len(pdd_browser_data_list))

    while True:
        logger.info("========== [主循环] 准备拉取最新 Tab 列表 ==========")
        tab_list = None

        # 1. 阻塞式获取 Tab 列表 (若账号均风控，则持续尝试)
        while not tab_list:
            acc = get_available_account(pdd_browser_data_list)
            if not acc:
                logger.info("获取Tab阶段：所有账号均在冷却中，等待 %s 秒...", GLOBAL_CONFIG["wait_no_account_seconds"])
                time.sleep(GLOBAL_CONFIG["wait_no_account_seconds"])
                continue

            update_account_usage_time(acc)  # 记录账号已使用
            tab_list = get_tab_list(acc)

            if tab_list is None:
                # 遭遇风控，跳过该账号，下个循环换新账号查
                logger.warning("拉取 Tab 列表失败，进入下一次账号轮询。")
                time.sleep(5)

        if not tab_list:
            logger.warning("本轮获取到的 Tab 列表为空，可能是网络或页面结构变动，重试。")
            time.sleep(60)
            continue

        logger.info("🎯 成功加载到 %d 个分类，开始分配账号依次采集...", len(tab_list))

        # 2. 遍历拉取每一个 Tab
        for tab in tab_list:
            tab_completed = False

            # 死循环保证这个 Tab 必须被成功拉取完
            while not tab_completed:
                current_acc = get_available_account(pdd_browser_data_list)

                if not current_acc:
                    # 池中没有空闲账号，等待 1 分钟后重新查询
                    logger.debug("抓取 Tab [%s] 时无可用空闲账号，休眠 %s 秒后重试...",
                                 tab["name"], GLOBAL_CONFIG["wait_no_account_seconds"])
                    time.sleep(GLOBAL_CONFIG["wait_no_account_seconds"])
                    continue

                # 找到了可用账号，第一时间更新时间（不管最终成功还是风控，都进入 30分钟冷却）
                update_account_usage_time(current_acc)

                # 开始执行抓取
                status = scrape_single_tab(current_acc, tab)

                if status == "SUCCESS":
                    tab_completed = True
                elif status == "RISK_CONTROL":
                    # 遭遇风控，由于上边已经 update 了使用时间，该账号会自动休息30分钟。
                    # tab_completed 依旧是 False，外层 while 会在下一秒去寻找下一个可用账号继续拉取当前 Tab
                    logger.warning("⚠️ 账号 [%s] 触发风控，已令其休眠30分钟，正在寻找接力账号继续抓取 [%s]...",
                                   os.path.basename(current_acc), tab["name"])
                elif status == "ERROR":
                    # 页面崩溃等异常，可配置直接跳过或重试，当前策略为：标记为完成强行跳过，防止死循环
                    logger.error("❌ 发生不可恢复的报错，跳过 Tab [%s] 抓取。", tab["name"])
                    tab_completed = True

                    # 避免频繁请求给予极短缓冲
                time.sleep(2)

        # 当这个 for 循环跑完，说明所有的 tab_list 都至少被成功抓取了一遍（或者异常跳过）
        logger.info("🎉 ========== 恭喜，全量 Tab 轮询完成，重新拉取最新列表，开始下一大回合！ ==========")


if __name__ == "__main__":
    main_controller()
# -*- coding: utf-8 -*-
# ==============================================================================
# [功能摘要] 基于 Playwright 网络拦截的拼多多多人团商品自动化增量采集器。
# [输入数据] 目标网页 URL 以及本地遗留的历史 CSV 数据（以商品ID为基准进行增量对比）。
# [数据流转/交互]
#   1. 读取历史 CSV，初始化本地字典池，构建以 商品ID 为键的去重结构；
#   2. Playwright 驱动浏览器持续滚动，拦截并捕获 `goods_list` 接口响应；
#   3. 解析 JSON 提取商品明细（完成价格转换、构造直达链接、注入当前时间戳）；
#   4. 内存字典覆盖更新（实现无缝 ID 去重与状态刷新）；
#   5. 全量字典写入临时 CSV 文件，成功后原子替换目标文件。
# [输出数据] 包含最新商品状态的完整 CSV 文件，不断累积与刷新商品特征记录。
# ==============================================================================

import os
import time
import csv
import logging
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

from common.playwright_utils import launch_persistent_context

from common.common_utils import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("pdd_scraper")


def load_existing_goods_dict(filename):
    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        logger.info("[数据加载/预处理] 未发现有效历史数据文件，将作为全新任务执行 | 目标文件: [%s] | 结果: 空字典启动",
                    filename)
        return {}
    existing_goods = {}
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                goods_id = str(row.get('商品ID', '')).strip()
                if goods_id:
                    existing_goods[goods_id] = row
        logger.info("[数据加载/预处理] 历史数据预加载完毕 | 目标文件: [%s] | 结果: 成功载入 [%s] 条历史记录", filename,
                    len(existing_goods))
    except Exception as e:
        logger.error("[数据加载/异常] 预加载本地历史文件失败 | 目标文件: [%s] | 异常: %s", filename, e)
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
        logger.debug("[文件落盘/存储] 原子替换保存成功 | 当前总写入条数: [%s]", len(goods_dict))
    except PermissionError:
        logger.warning("[文件落盘/文件占用] 替换原文件失败，跳过本次替换 | 临时文件路径: [%s]", tmp_filename)
    except Exception as e:
        logger.error("[文件落盘/未预期异常] 写入 CSV 时发生严重底层异常 | 异常: %s", e, exc_info=True)
        raise


def scrape_pdd_group_items(user_data_dir, target_url, max_scrolls=50, output_csv="pdd_goods.csv"):
    logger.info("[任务执行/启动采集] 开始拉起内核采集数据 | 目标URL: [%s]", target_url)
    seen_goods_dict = load_existing_goods_dict(output_csv)
    session_new_count, session_update_count = 0, 0
    current_tab_name = "默认精选"

    def handle_response(response):
        nonlocal session_new_count, session_update_count
        if "brand-group-home/home/goods_list" not in response.url:
            return
        if response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        if not data.get("success") or "result" not in data:
            return

        goods_list = data["result"].get("goods_list", [])
        if not goods_list:
            return

        new_batch, update_batch = 0, 0
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for item in goods_list:
            goods_id = str(item.get("goods_id", "")).strip()
            if not goods_id:
                continue

            parsed_item = {
                "商品ID": goods_id,
                "所属标签": current_tab_name,
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
                update_batch += 1
                session_update_count += 1
            else:
                new_batch += 1
                session_new_count += 1
            seen_goods_dict[goods_id] = parsed_item

        logger.info("[网络拦截/数据合流] 解析: [%s] | 本次新增: [%s], 更新: [%s] | 历史总库容: [%s]", current_tab_name,
                    new_batch, update_batch, len(seen_goods_dict))
        save_to_csv_atomic(seen_goods_dict, output_csv)

    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", handle_response)

        try:
            page.goto(target_url)
            page.wait_for_load_state("networkidle")
            time.sleep(3)  # DOM渲染

            # ==============================================================================
            # 【新增逻辑】：风控状态页面特征检测 (健壮增强版)
            # ==============================================================================
            try:
                # 优雅实现：抛弃不稳定的 CSS 类名（如 zYwyl9D7），使用精确文本定位
                # is_visible() 不会发生阻塞等待，检测极快，能完美应对 DOM 瞬时状态
                risk_notice = page.get_by_text("活动陆续开放中", exact=True)
                home_btn = page.get_by_text("回到首页", exact=True)

                # 只要任意一个核心文案在视口中可见，即判定命中风控拦截页
                if risk_notice.is_visible() or home_btn.is_visible():
                    logger.warning("🚨 [风控预警] 抓取到风控页面核心文案，当前环境已被系统识别并降级限制！")
                    context.close()
                    # 返回特定状态码交由外层处理
                    return "RISK_CONTROL"
            except Exception as e:
                logger.debug("[风控检测] 检测DOM特征时发生忽略异常: %s", e)
            # ==============================================================================

        except Exception as e:
            logger.error("[浏览器控制] 页面加载失败: %s", e, exc_info=True)
            raise

        logger.info("[浏览器控制/页面交互] 开始获取标签...")
        tabs = []
        try:
            nav_container = page.locator('#brand-first-nav')
            nav_container.wait_for(state="visible", timeout=10000)
            tabs = nav_container.locator('> div').all()
        except Exception as e:
            logger.warning("寻找标签导航容器失败，进行降级处理。")
            tabs = [None]

        for index, tab in enumerate(tabs):
            if tab is not None:
                try:
                    raw_text = tab.inner_text().replace('\n', '').strip()
                    current_tab_name = raw_text if raw_text else f"未知标签_{index}"
                    tab.scroll_into_view_if_needed()
                    tab.click(force=True)
                    time.sleep(3.5)
                except Exception as e:
                    continue
            else:
                current_tab_name = "默认页(定位失败)"

            for i in range(max_scrolls):
                try:
                    page.mouse.wheel(0, 3000)
                    time.sleep(2)
                except Exception:
                    break

            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1.5)

        logger.info("[任务执行/收尾总结] 采集收尾 | 新增 [%s]，刷新 [%s] | 库容: [%s]", session_new_count,
                    session_update_count, len(seen_goods_dict))
        try:
            context.close()
        except Exception:
            pass

        return "SUCCESS"


# ==============================================================================
#                                   执行守护进程
# ==============================================================================
if __name__ == "__main__":
    TEST_USER_DATA_DIR = os.path.join(r'W:\project\python_project\easy_shop\temp_data\browser_data', "pdd_browser_data")
    TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"
    logger.info("[守护进程/初始化] 已拉起主定时轮询模式")
    pdd_browser_data_list = get_config("pdd_browser_data_list")
    TEST_USER_DATA_DIR = pdd_browser_data_list[1]
    while True:
        wait_seconds = 1800

        try:
            logger.info("[守护进程/周期调度] ▶️ 开始派发执行本轮次采集任务...")

            status = scrape_pdd_group_items(
                user_data_dir=TEST_USER_DATA_DIR,
                target_url=TEST_URL,
                max_scrolls=2,
                output_csv="pdd_group_items.csv"
            )

            if status == "RISK_CONTROL":
                logger.warning(
                    "🛡️ [策略调整] 接收到风控阻断信号，为保护账号/IP，本轮次休眠时间将由 30分钟 延长至 1小时 (3600秒)！")
                wait_seconds = 3600

        except Exception as e:
            logger.error("[守护进程/异常阻断] ❌ 本轮采集任务发生崩溃级异常已被阻断，放弃当前轮次 | 异常详情: %s", e,
                         exc_info=True)

        next_run = (datetime.now() + timedelta(seconds=wait_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        logger.info("[守护进程/休眠等待] 💤 阶段任务闭环，进入长休眠 | 倒计时(秒): [%s] | 预计下一次自动苏醒: [%s]",
                    wait_seconds, next_run)
        time.sleep(wait_seconds)
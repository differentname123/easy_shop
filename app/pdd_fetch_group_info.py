# -- coding: utf-8 --
""":authors:
    zhuxiaohu
:create_date:
    2026/8/21 23:32
:last_date:
    2026/8/22 00:45
:description:
    修改版2：支持启动前预加载去重、追加时间戳、优化关闭异常拦截、精细化统计口径。
"""
# -*- coding: utf-8 -*-
import os
import time
import csv
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright

# 请确保你的模块路径正确
from common_utils.playwright_utils import launch_persistent_context

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] %(message)s')
logger = logging.getLogger("pdd_scraper")


def load_existing_goods_dict(filename: str) -> dict:
    """
    [修改] 启动前预先加载本地已存在的商品信息，以字典形式存储（ID为Key），以便覆盖更新
    """
    existing_goods = {}
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        try:
            with open(filename, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if '商品ID' in row and row['商品ID']:
                        # 统一转换为字符串以防类型不匹配，作为字典的Key
                        existing_goods[str(row['商品ID']).strip()] = row
            logger.info(f"📁 预加载本地历史数据成功 | 发现已有商品数: {len(existing_goods)}")
        except Exception as e:
            logger.error(f"❌ 预加载本地历史 CSV 失败 (可能文件损坏或被占用): {e}")
    else:
        logger.info("📁 本地无历史数据或文件为空，将作为全新任务执行。")
    return existing_goods


def save_to_csv_atomic(goods_dict: dict, filename: str):
    """
    [修改] 全量原子保存 CSV，先写入 .tmp 文件，成功后再瞬间替换原文件
    """
    if not goods_dict:
        return

    # 锁定表头，确保无论历史数据是否缺失字段，统一以此为标准
    fieldnames = [
        "商品ID", "商品名称", "品牌", "原价(元)", "补贴价(元)", "立省(元)",
        "销量提示", "商品链接", "主图链接", "更新时间"
    ]
    tmp_filename = filename + ".tmp"

    try:
        with open(tmp_filename, 'w', encoding='utf-8-sig', newline='') as output_file:
            # extrasaction='ignore' 自动忽略不需要的多余历史字段
            dict_writer = csv.DictWriter(output_file, fieldnames=fieldnames, extrasaction='ignore')
            dict_writer.writeheader()
            dict_writer.writerows(goods_dict.values())

        # 原子替换文件 (Windows上如果原文件被Excel锁定会抛出 PermissionError)
        os.replace(tmp_filename, filename)
        logger.debug(f"💾 原子保存落盘成功，当前总写入 {len(goods_dict)} 条数据。")
    except PermissionError:
        logger.error(
            f"❌ 写入失败：文件【{filename}】被占用！请检查是否正在用 Excel 打开此文件。临时数据保存在 {tmp_filename}")
    except Exception as e:
        logger.error(f"❌ 写入 CSV 时发生未预期的严重异常: {e}", exc_info=True)


def scrape_pdd_group_items(user_data_dir: str, target_url: str, max_scrolls: int = 50,
                           output_csv: str = "pdd_goods.csv"):
    """
    基于 Playwright 网络拦截机制，全自动滚动并抓取拼多多多人团商品
    """
    logger.info(f"🚀 开始采集任务 | 目标URL: {target_url} | 结果将增量保存至: {output_csv}")

    # 1. 先加载已有的商品字典，用于全局去重和信息更新覆盖
    seen_goods_dict = load_existing_goods_dict(output_csv)

    # 记录本次运行中实际新抓到的总数与更新总数
    session_new_count = 0
    session_update_count = 0

    def handle_response(response):
        nonlocal session_new_count, session_update_count
        """
        拦截器：专门监听并提取 goods_list 接口的数据
        """
        if "brand-group-home/home/goods_list" in response.url:

            if response.status != 200:
                logger.warning(f"⚠️ 拦截到目标接口，但状态码异常: {response.status} | URL: {response.url}")
                return

            try:
                data = response.json()
            except Exception as e:
                # [核心修正]: 拦截程序退出时的 TargetClosedError
                if "Target page, context or browser has been closed" in str(e):
                    logger.debug("💤 浏览器关闭中断了最后一次未完成的请求，正常收尾，忽略此报错。")
                else:
                    logger.error(f"❌ 接口返回的数据无法解析为JSON: {e} | URL: {response.url}")
                return

            try:
                if data.get("success") and "result" in data:
                    goods_list = data["result"].get("goods_list", [])

                    parsed_count = len(goods_list)
                    new_batch_count = 0
                    update_batch_count = 0

                    for item in goods_list:
                        # 从 JSON 中提取 ID 并统一转为字符串进行比对
                        goods_id = str(item.get("goods_id", "")).strip()

                        if not goods_id:
                            continue

                        # 获取当前抓取时间
                        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                        parsed_item = {
                            "商品ID": goods_id,
                            "商品名称": item.get("goods_name", ""),
                            "品牌": item.get("brand_name", ""),
                            "原价(元)": item.get("origin_price", 0) / 100,
                            "补贴价(元)": item.get("activity_price", 0) / 100,
                            "立省(元)": item.get("group_order_price_reduce", 0) / 100,
                            "销量提示": item.get("sales_tip", ""),
                            "商品链接": "https://mobile.pinduoduo.com/" + str(item.get("link_url", "")),
                            "主图链接": item.get("hd_thumb_url", ""),
                            "更新时间": current_time  # [新增] 时间戳字段
                        }

                        # 判断该商品是属于覆盖更新还是新增
                        if goods_id in seen_goods_dict:
                            update_batch_count += 1
                            session_update_count += 1
                        else:
                            new_batch_count += 1
                            session_new_count += 1

                        # [关键修改] 无视是否存在，直接覆盖/写入最新信息至字典
                        seen_goods_dict[goods_id] = parsed_item

                    if parsed_count > 0:
                        # [统计口径优化]: 打印当次解析、新增、更新、累计以及历史总库数量
                        logger.info(
                            f"✅ 接口捕获成功! 解析商品数: {parsed_count:>2} 个 | "
                            f"新增: {new_batch_count:>2} 个, 覆盖更新: {update_batch_count:>2} 个 | "
                            f"本次累计新增: {session_new_count:>3} 个, 累计更新: {session_update_count:>3} 个 | "
                            f"📦 历史总库容量: {len(seen_goods_dict)} 个"
                        )
                        # 调用更健壮的原子写入
                        save_to_csv_atomic(seen_goods_dict, output_csv)
                else:
                    logger.warning(f"⚠️ 接口返回了合法的JSON，但缺少必要的 success 或 result 字段。")

            except Exception as e:
                logger.error(f"❌ 提取商品字段时发生异常: {e}", exc_info=True)

    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        page.on("response", handle_response)

        try:
            page.goto(target_url)
            page.wait_for_load_state("networkidle")
        except Exception as e:
            logger.error(f"❌ 初始页面加载失败或超时: {e}", exc_info=True)

        logger.info("⏳ 页面加载完成，开始自动向下拉动触发数据加载...")

        for i in range(max_scrolls):
            try:
                page.mouse.wheel(0, 3000)
                logger.info(f"向下滚动第 {i + 1}/{max_scrolls} 次...")
                time.sleep(2)
            except Exception as e:
                logger.error(f"❌ 页面滑动时发生异常（可能页面已崩溃或被手动关闭）: {e}")
                break

        # [收尾优化]: 给最后一次请求留 1 秒缓冲，避免暴力截断
        time.sleep(1)
        logger.info(
            f"🛑 采集结束！本次任务共计为您扩充了 {session_new_count} 个新商品，更新了 {session_update_count} 个已有商品。商品总库现已有 {len(seen_goods_dict)} 个。"
        )

        try:
            context.close()
        except Exception:
            pass


# ==============================================================================
#                                   执行入口
# ==============================================================================
if __name__ == "__main__":
    from datetime import timedelta  # 用于计算下一次执行的时间显示

    TEST_USER_DATA_DIR = os.path.join(r'W:\project\python_project\easy_shop\temp_data\browser_data', "pdd_browser_data")
    TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"

    logger.info("🔄 启动定时轮询模式：每 30 分钟自动执行一次。")

    while True:
        try:
            logger.info("▶️ 开始执行本轮采集任务...")
            scrape_pdd_group_items(
                user_data_dir=TEST_USER_DATA_DIR,
                target_url=TEST_URL,
                max_scrolls=10,  # 正式运行时建议调大，如 100
                output_csv="pdd_group_items.csv"
            )
        except Exception as e:
            # 最外层兜底：捕获并记录所有未知致命异常，确保 while 循环永不中断
            logger.error(f"❌ 本轮采集任务发生未处理异常，任务已跳过，等待下一轮: {e}", exc_info=True)

        # 休眠 30 分钟 (30 * 60 = 1800 秒)
        wait_seconds = 1800
        next_run = (datetime.now() + timedelta(seconds=wait_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"💤 本轮结束。进入休眠... 预计下一次采集启动时间: {next_run}")
        time.sleep(wait_seconds)
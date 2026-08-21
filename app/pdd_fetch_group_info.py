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

from common_utils.playwright_utils import launch_persistent_context

# 重新规范化日志配置，保持最精简整洁的时间戳与级别
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("pdd_scraper")


def load_existing_goods_dict(filename):
    """
    加载历史商品数据到内存字典，用于去重与最新状态的覆盖更新。
    [出参 Shape]: { "商品ID(字符串)": {"商品名称": "...", "补贴价(元)": 15.5, ...} }
    """
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
        logger.error("[数据加载/异常] 预加载本地历史文件失败，可能文件损坏或权限不足 | 目标文件: [%s] | 异常信息: %s",
                     filename, e)

    return existing_goods


def save_to_csv_atomic(goods_dict, filename):
    """
    通过临时文件写入再替换的方式，实现原子化覆盖保存全量商品数据，避免脏读写。
    [入参 Shape]: goods_dict 必须符合 load_existing_goods_dict 的出参形态，键为商品ID，值为数据行字典。
    """
    if not goods_dict:
        return

    # 【修改点】：增加 "所属标签" 字段
    fieldnames = [
        "商品ID", "所属标签", "商品名称", "品牌", "原价(元)", "补贴价(元)", "立省(元)",
        "销量提示", "商品链接", "主图链接", "更新时间"
    ]
    tmp_filename = f"{filename}.tmp"

    try:
        with open(tmp_filename, 'w', encoding='utf-8-sig', newline='') as output_file:
            # extrasaction='ignore' 防御脏数据字段溢出
            dict_writer = csv.DictWriter(output_file, fieldnames=fieldnames, extrasaction='ignore')
            dict_writer.writeheader()
            dict_writer.writerows(goods_dict.values())

        # 原生原子替换
        os.replace(tmp_filename, filename)
        logger.debug("[文件落盘/存储] 原子替换保存成功 | 当前总写入条数: [%s]", len(goods_dict))

    except PermissionError:
        # 特别兼容 Windows 环境下目标文件正被 Excel 打开锁定的场景，跳过本次替换以保护程序不崩溃
        logger.warning(
            "[文件落盘/文件占用] 替换原文件失败，文件正在被使用(请检查是否用Excel打开了此文件) | 临时文件路径: [%s] | 结果: 跳过本次替换",
            tmp_filename)
    except Exception as e:
        logger.error("[文件落盘/未预期异常] 写入 CSV 时发生严重底层异常 | 目标文件: [%s] | 异常信息: %s", filename, e,
                     exc_info=True)
        # 严格抛出，不允许静默吞噬严重 IO 错误
        raise


def scrape_pdd_group_items(user_data_dir, target_url, max_scrolls=50, output_csv="pdd_goods.csv"):
    """
    核心执行器：拉起浏览器、注入拦截器并在指定 URL 内执行翻页操作，收集所拦截到的目标数据。
    """
    logger.info("[任务执行/启动采集] 开始拉起内核采集数据 | 目标URL: [%s] | 期望存储介质: [%s]", target_url, output_csv)

    seen_goods_dict = load_existing_goods_dict(output_csv)
    session_new_count = 0
    session_update_count = 0

    # 【修改点】：新增状态变量，供拦截器读取当前在哪个标签下
    current_tab_name = "默认精选"

    def handle_response(response):
        nonlocal session_new_count, session_update_count

        # [卫语句1]: 精准过滤非目标 URL
        if "brand-group-home/home/goods_list" not in response.url:
            return

        # [卫语句2]: 过滤非 200 状态码
        if response.status != 200:
            logger.warning("[网络拦截/响应异常] 拦截到目标接口但状态码不符合预期 | 状态码: [%s] | 目标接口: [%s]",
                           response.status, response.url)
            return

        # [卫语句3]: JSON 解析安全防护
        try:
            data = response.json()
        except Exception as e:
            if "Target page, context or browser has been closed" in str(e):
                logger.debug("[网络拦截/正常终止] 浏览器已主动关闭，请求终止 | 结果: 忽略关闭期的无害报错")
            else:
                logger.error("[网络拦截/数据损坏] 接口返回了无法解析为 JSON 的数据 | 异常: %s", e)
            return

        # [卫语句4]: 业务字段合法性防护
        if not data.get("success") or "result" not in data:
            logger.warning("[网络拦截/字段缺失] JSON结构缺失 success 或 result 基础字段 | 结果: 已放弃当前数据包提取")
            return

        goods_list = data["result"].get("goods_list", [])
        if not goods_list:
            return

        new_batch_count = 0
        update_batch_count = 0
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 数据清洗与覆盖写入循环
        for item in goods_list:
            goods_id = str(item.get("goods_id", "")).strip()
            if not goods_id:
                continue

            parsed_item = {
                "商品ID": goods_id,
                "所属标签": current_tab_name,  # 【修改点】：注入当前的标签名称
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

            # 统计业务：判断属新增还是更新
            if goods_id in seen_goods_dict:
                update_batch_count += 1
                session_update_count += 1
            else:
                new_batch_count += 1
                session_new_count += 1

            # 直接覆盖入库
            seen_goods_dict[goods_id] = parsed_item

        logger.info(
            "[网络拦截/数据合流] 成功解包合并单批次接口数据 | 当前标签: [%s] | 解析总数: [%s] | 本次新增: [%s], 本次更新: [%s] | 任务累计新增: [%s], 任务累计更新: [%s] | 历史总库容: [%s]",
            current_tab_name, len(goods_list), new_batch_count, update_batch_count,
            session_new_count, session_update_count, len(seen_goods_dict)
        )

        # 触发持久化
        save_to_csv_atomic(seen_goods_dict, output_csv)

    # 开始组装 Playwright 链路
    with sync_playwright() as p:
        context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        page.on("response", handle_response)

        try:
            page.goto(target_url)
            page.wait_for_load_state("networkidle")
            time.sleep(3)  # 给予DOM充分渲染时间
        except Exception as e:
            logger.error("[浏览器控制/页面加载] 首屏初始页面加载失败或严重超时 | 目标URL: [%s] | 异常信息: %s",
                         target_url, e, exc_info=True)
            # 严格异常流转：不再往下死磕滚动，立刻抛出交由上游终止本轮采集
            raise

        # 【修改点】：加入标签识别、点击与嵌套遍历滑动的逻辑
        logger.info("[浏览器控制/页面交互] 首屏加载成功，开始通过核心容器ID获取所有标签...")

        tabs = []
        try:
            nav_container = page.locator('#brand-first-nav')
            nav_container.wait_for(state="visible", timeout=10000)
            tabs = nav_container.locator('> div').all()
            logger.info("[页面交互] 成功锁定导航容器，共识别到 [%s] 个栏目标签。", len(tabs))
        except Exception as e:
            logger.error("寻找标签导航容器失败: %s", e)
            logger.info("降级处理：不进行标签切换，仅拉取当前默认页面。")
            tabs = [None]

            # 外层循环：遍历所有标签并点击切换
        for index, tab in enumerate(tabs):
            if tab is not None:
                try:
                    raw_text = tab.inner_text().replace('\n', '').strip()

                    if raw_text:
                        current_tab_name = raw_text
                    else:
                        if tab.locator('img').count() > 0:
                            current_tab_name = f"大促活动(图片)_{index}"
                        else:
                            current_tab_name = f"未知标签_{index}"

                    tab.scroll_into_view_if_needed()
                    logger.info("[页面交互] 正在切换并准备采集 ➡️ : [%s] (%s/%s)", current_tab_name, index + 1,
                                len(tabs))
                    tab.click(force=True)
                    time.sleep(3.5)  # 给接口请求、响应拦截以及DOM重新渲染预留时间
                except Exception as e:
                    logger.error("点击标签 [%s] 发生异常: %s", index, e)
                    continue
            else:
                current_tab_name = "默认页(定位失败)"

            logger.info("[页面交互] 开始在标签 [%s] 下进行自动鼠标下滚以触发数据懒加载...", current_tab_name)

            # 内层循环：在当前标签下执行到底部的持续滚动
            for i in range(max_scrolls):
                try:
                    page.mouse.wheel(0, 3000)
                    logger.info("[浏览器控制/页面交互] 执行向下滚动触底操作 | 当前标签: [%s] | 进度: [%s/%s]",
                                current_tab_name, i + 1, max_scrolls)
                    time.sleep(2)
                except Exception as e:
                    logger.warning("[浏览器控制/页面交互] 页面滑动发生中断 (可能页面已崩溃或被手动强制关闭) | 异常: %s",
                                   e)
                    break

            # 当前标签滚到底之后，滚回最顶部，防止干扰下一个标签的点击定位
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1.5)

        logger.info(
            "[任务执行/收尾总结] 本轮浏览器采集任务执行收尾完毕 | 成果: 新增抓取 [%s] 个，刷新状态 [%s] 个 | 总库最终容量: [%s] 个",
            session_new_count, session_update_count, len(seen_goods_dict))

        try:
            context.close()
        except Exception as e:
            logger.debug("[浏览器控制/资源回收] 关闭浏览器上下文时遇到无害残留异常 | 结果: 已忽略并正常放行 | 异常: %s",
                         e)


# ==============================================================================
#                                   执行守护进程
# ==============================================================================
if __name__ == "__main__":
    # 配置工作区与入口参数
    TEST_USER_DATA_DIR = os.path.join(r'W:\project\python_project\easy_shop\temp_data\browser_data', "pdd_browser_data")
    TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"

    logger.info("[守护进程/初始化] 已拉起主定时轮询模式 | 轮询策略: 每 [30] 分钟自动拉起执行一次采集")

    while True:
        try:
            logger.info("[守护进程/周期调度] ▶️ 开始派发执行本轮次采集任务...")
            scrape_pdd_group_items(
                user_data_dir=TEST_USER_DATA_DIR,
                target_url=TEST_URL,
                max_scrolls=100,
                output_csv="pdd_group_items.csv"
            )
        except Exception as e:
            # 最外层兜底：捕获并记录下层溢出的所有致命异常，确保 while 守护循环永不崩溃退出
            logger.error(
                "[守护进程/异常阻断] ❌ 本轮采集任务发生崩溃级异常已被阻断，放弃当前轮次 | 结果: 等待下一轮唤醒 | 异常详情: %s",
                e, exc_info=True)

        wait_seconds = 1800
        next_run = (datetime.now() + timedelta(seconds=wait_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            "[守护进程/休眠等待] 💤 阶段任务闭环，进入长休眠 | 倒计时(秒): [%s] | 预计下一次自动苏醒采集时间: [%s]",
            wait_seconds, next_run)
        time.sleep(wait_seconds)
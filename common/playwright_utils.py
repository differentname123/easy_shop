# -*- coding: utf-8 -*-
"""
=========================================================================================
[功能摘要] 多多进宝全链路自动化采集与风控突破工具箱
[输入数据] search_key_list(搜索关键词列表), user_data_dir(本地持久化浏览器缓存路径)
[数据流转/交互]
    1. 环境初始化：加载本地浏览器缓存，携带登录态进入多多进宝。
    2. 风控对抗：如遇拼多多安全盾，自动截取背景与滑块图，利用OpenCV匹配缺口并模拟人类轨迹拖拽。
    3. 数据截获：注入页面并填入关键词，拦截底层 /network/api/common/goodsList 接口响应。
    4. 自动翻页：滚动页面点击下一页，循环拦截数据直至达到 limit_count。
    5. 数据融合：模拟点击"全选并导出"，拦截异步生成的 Excel 下载地址。将下载的Excel转为DataFrame，
       并通过「商品ID」与之前拦截的JSON进行全量字段聚合。
[输出数据] dict 格式的采集结果。Key 为关键词，Value 包含 JSON 列表与 Excel 直链。
=========================================================================================
"""

import os
import shutil
import time
import json
import logging
import traceback
import io
import random
import base64
from datetime import datetime

import requests
import cv2
import numpy as np

try:
    import pandas as pd
except ImportError:
    pd = None

from playwright.sync_api import sync_playwright

# 极致规范的结构化日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] %(message)s')
logger = logging.getLogger("playwright_utils")


def clean_browser_cache(user_data_dir):
    """
    清理浏览器冗余缓存，防止 RPA 目录体积爆仓。
    保留 Cookie/LocalStorage 等核心登录凭证。
    """
    if not os.path.exists(user_data_dir):
        return

    garbage_folders = (
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "Service Worker", "CacheStorage")
    deleted_count = 0

    for base in (user_data_dir, os.path.join(user_data_dir, "Default")):
        for name in garbage_folders:
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                deleted_count += 1
            except Exception:
                pass

    logger.info(f"[环境/瘦身] 冗余缓存清理完毕 | 目录: <{user_data_dir}> | 释放项数: 【{deleted_count}】")


def launch_persistent_context(p, user_data_dir, args=None, viewport=None, hide_automation=True, headless=False):
    """
    全局统一的持久化上下文启动器。
    """
    default_args = args or ['--disable-blink-features=AutomationControlled', '--start-maximized']
    kwargs = {
        "channel": "chrome",
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": default_args
    }

    if viewport:
        kwargs["viewport"] = viewport
    else:
        kwargs["no_viewport"] = True

    if hide_automation:
        kwargs["ignore_default_args"] = ["--enable-automation"]

    return p.chromium.launch_persistent_context(**kwargs)


def login_and_save_session(user_data_dir, login_url):
    """
    阻断式弹窗，由人类接管完成登录后固化会话。
    """
    logger.info(f"[环境/固化] 准备手动登录 | 存储路径: <{user_data_dir}> | 目标网站: [{login_url}]")
    clean_browser_cache(user_data_dir)

    with sync_playwright() as p:
        context = None
        try:
            context = launch_persistent_context(p, user_data_dir=user_data_dir, hide_automation=False, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url)

            input(f"\n[环境/固化] 正在挂起等待人类操作 | 动作: [请在弹出的浏览器中登录，成功后按 【Enter】 键退出] ...")
            logger.info("[环境/固化] 会话环境已保存 | 结果: [Success]")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass


def open_browser_for_manual_use(user_data_dir, home_url):
    """
    启动并移交控制权给人类，监控直到窗口被手动关闭。
    """
    logger.info(f"\n{'=' * 60}\n[环境/接管] 启动本地浏览器 | 目录: <{user_data_dir}>\n{'=' * 60}")
    with sync_playwright() as p:
        context = None
        try:
            args = ['--disable-blink-features=AutomationControlled', '--start-maximized', '--window-position=0,0']
            context = launch_persistent_context(p, user_data_dir=user_data_dir, args=args, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.bring_to_front()
            page.goto(home_url)

            logger.info("[环境/接管] ✅ 浏览器已就绪 | 提示: [直接关闭浏览器窗口，程序将自动结束]")
            page.wait_for_event("close", timeout=0)
        except Exception as e:
            logger.warning(f"[环境/接管] 运行异常退出 | 原因: [环境损坏或窗口被强杀: {e}]")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info("[环境/接管] 👋 窗口已关闭，系统资源已释放\n")


def robust_click(locator):
    """
    保底点击机制：常态点击 -> 穿透遮挡 -> 原生 JS 强杀。
    """
    for attempt in ("normal", "force"):
        try:
            locator.click(timeout=1500, force=(attempt == "force"))
            return
        except Exception:
            continue
    locator.evaluate("node => node.click()")


def save_forensics(page, tag, save_dir="forensics_logs", extra_info=None):
    """
    故障现场留存：截取关键快照，提取完整 DOM 树与排查参数。
    静默失败：日志收集绝不能反向拖垮主业务。
    """
    try:
        os.makedirs(save_dir, exist_ok=True)
        base_name = f"forensic_{tag}_{int(time.time() * 1000)}"
        base_path = os.path.join(save_dir, base_name)

        try:
            page.screenshot(path=f"{base_path}.png", full_page=False)
        except Exception:
            pass

        try:
            with open(f"{base_path}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass

        try:
            payload = dict(extra_info or {})
            payload["url"] = page.url
            with open(f"{base_path}.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

        logger.warning(f"[系统/排障] 现场快照已落盘 | 文件前缀: <{base_path}>")
        return base_path
    except Exception:
        pass


def download_and_merge_excel(excel_url, goods_list):
    """
    [数据Shape约束]:
      入参 goods_list: [{"goodsId": 123, "goodsName": "A", ...}]
      出参返回: [{"goodsId": 123, "goodsName": "A", "短链接": "http...", "佣金": "x", ...}]
    """
    if not excel_url or not goods_list:
        return goods_list

    if pd is None:
        logger.error(
            "[数据/融合] ❌ 缺少 Pandas 引擎 | 建议: [请运行 pip install pandas requests xlrd lxml] | 结果: [放弃融合]")
        return goods_list

    logger.info("[数据/融合] 正在内存下载 Excel 进行高维字段补全...")
    try:
        resp = requests.get(excel_url, timeout=30)
        resp.raise_for_status()
        file_bytes = io.BytesIO(resp.content)

        try:
            df = pd.read_excel(file_bytes, engine="xlrd")
        except Exception as e1:
            logger.debug(f"[数据/融合] 降级使用 HTML 表格解析器 | 原解析失败原因: [{e1}]")
            file_bytes.seek(0)
            dfs = pd.read_html(file_bytes, encoding='utf-8')
            df = dfs[0] if dfs else pd.DataFrame()

        if df.empty or '商品ID' not in df.columns:
            logger.warning("[数据/融合] Excel 解析异常 | 异常: [数据为空或缺失'商品ID'列] | 结果: [放弃融合]")
            return goods_list

        df['商品ID'] = df['商品ID'].astype(str)
        excel_records = {
            str(row['商品ID']): {k: v for k, v in row.items() if pd.notna(v)}
            for row in df.to_dict(orient="records")
        }

        merged_count = 0
        for goods in goods_list:
            g_id = str(goods.get("goodsId", ""))
            if g_id in excel_records:
                goods.update(excel_records[g_id])
                merged_count += 1

        logger.info(f"[数据/融合] ✅ Excel 字段补全完毕 | 命中条数: 【{merged_count}/{len(goods_list)}】")
    except Exception as e:
        logger.error(f"[数据/融合] ❌ 融合失败 | 原因: [{e}] | 结果: [返回原数据]")

    return goods_list


def generate_drag_tracks(distance):
    """拟人化缓动轨迹计算"""
    track = []
    current, v, t, mid = 0, 0, 0.2, distance * 4 / 5

    while current < distance:
        a = random.randint(2, 5) if current < mid else -random.randint(3, 5)
        v0 = v
        v = v0 + a * t
        move = v0 * t + 1 / 2 * a * t * t
        current += move
        track.append(round(move))

    offset = sum(track) - distance
    if offset > 0:
        track.extend([-1] * int(offset))
    elif offset < 0:
        track.extend([1] * int(abs(offset)))

    track.extend([random.randint(1, 2), -random.randint(1, 2), 0])
    return track


def calculate_slider_distance_cv2(bg_bytes, item_bytes, debug_path=None):
    """OpenCV 剔除滑块透明通道并匹配背景缺口边缘"""
    bg_np = np.frombuffer(bg_bytes, np.uint8)
    bg_img = cv2.imdecode(bg_np, cv2.IMREAD_COLOR)
    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)

    item_np = np.frombuffer(item_bytes, np.uint8)
    item_img = cv2.imdecode(item_np, cv2.IMREAD_UNCHANGED)

    if item_img.shape[2] == 4:
        alpha_channel = item_img[:, :, 3]
        y_coords, x_coords = np.where(alpha_channel > 0)
        if len(x_coords) == 0:
            raise ValueError("提取滑块实体失败：检测到全透明图片")
        x_min, x_max = np.min(x_coords), np.max(x_coords)
        y_min, y_max = np.min(y_coords), np.max(y_coords)
        cropped_item = item_img[y_min:y_max + 1, x_min:x_max + 1]
    else:
        cropped_item = item_img
        x_min = 0

    cropped_item_gray = cv2.cvtColor(cropped_item[:, :, :3], cv2.COLOR_BGR2GRAY)

    bg_edge = cv2.Canny(bg_gray, 100, 200)
    item_edge = cv2.Canny(cropped_item_gray, 100, 200)

    res = cv2.matchTemplate(bg_edge, item_edge, cv2.TM_CCOEFF_NORMED)
    _, _, _, max_loc = cv2.minMaxLoc(res)

    target_x, target_y = max_loc[0], max_loc[1]
    actual_distance = target_x - x_min

    if debug_path:
        debug_img = bg_img.copy()
        h, w = cropped_item_gray.shape
        cv2.rectangle(debug_img, (target_x, target_y), (target_x + w, target_y + h), (0, 0, 255), 2)
        cv2.putText(debug_img, f"Move: {actual_distance}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        cv2.imwrite(debug_path, debug_img)

    return actual_distance, bg_img.shape[1]


def handle_pdd_captcha(page):
    """
    单点突破验证组件。探测 -> 计算 -> 拟人化拖拽。
    """
    try:
        # : 此处重度依赖“安全验证”四个中文字符进行判断，若业务方文案变动将导致判断失效。
        verify_btn = page.locator('button:has-text("安全验证")').first
        if verify_btn.is_visible(timeout=2000):
            logger.info("[风控/对抗] 拦截到首层验证按钮 | 动作: [准备自动点击] ...")
            robust_click(verify_btn)
            page.wait_for_timeout(1500)
    except Exception:
        pass

    try:
        slider_bg_img = page.locator('.slider-img-bg').first
        if not slider_bg_img.is_visible(timeout=2000):
            return True
    except Exception:
        return True

    logger.warning("[风控/对抗] 🚨 触发拼图滑块屏障 | 动作: [切入机器视觉自动破解模式]")
    debug_dir = "debug_captcha"
    os.makedirs(debug_dir, exist_ok=True)

    for attempt in range(1, 6):
        try:
            if not slider_bg_img.is_visible():
                logger.info("[风控/对抗] ✅ 滑块消退 | 结果: [验证成功]")
                return True

            logger.info(f"[风控/对抗] 执行视觉识别 | 进度: [尝试第 {attempt}/5 次]")
            page.wait_for_timeout(1000)

            bg_src = slider_bg_img.get_attribute('src')
            item_src = page.locator('.slider-item').first.get_attribute('src')
            if not bg_src or "base64," not in bg_src:
                page.wait_for_timeout(1000)
                continue

            bg_bytes = base64.b64decode(bg_src.split("base64,")[1])
            item_bytes = base64.b64decode(item_src.split("base64,")[1])

            debug_path = os.path.join(debug_dir, f"cv2_match_{datetime.now().strftime('%H%M%S')}_{attempt}.png")
            raw_dist, natural_width = calculate_slider_distance_cv2(bg_bytes, item_bytes, debug_path)

            box = slider_bg_img.bounding_box()
            scale_ratio = box['width'] / natural_width
            final_drag_distance = raw_dist * scale_ratio

            slider_btn = page.locator('#slide-button').first
            btn_box = slider_btn.bounding_box()
            start_x = btn_box['x'] + btn_box['width'] / 2
            start_y = btn_box['y'] + btn_box['height'] / 2
            tracks = generate_drag_tracks(final_drag_distance)

            page.mouse.move(start_x, start_y)
            page.mouse.down()

            current_x, current_y = start_x, start_y
            for step in tracks:
                current_x += step
                current_y += random.uniform(-1.0, 1.0)
                page.mouse.move(current_x, current_y)
                time.sleep(random.uniform(0.01, 0.02))

            page.mouse.up()
            page.wait_for_timeout(2500)

            if not slider_bg_img.is_visible():
                logger.info("[风控/对抗] ✅ 拖拽精准落位 | 结果: [安全验证放行]")
                return True

        except Exception as e:
            logger.error(f"[风控/对抗] 拖拽流程中断 | 异常: [{e}]")
            page.mouse.up()
            page.wait_for_timeout(2000)

    logger.error("[风控/对抗] ❌ 次数耗尽，破解失败 | 结果: [页面已被锁定]")
    return False


def search_goods_and_intercept(search_key_list, user_data_dir, limit_count=500, debug=False):
    """
    [数据Shape约束]:
      入参 search_key_list: ["可乐", "雪碧"]
      出参返回: {
          "可乐": {
              "goodsList": [{"goodsId": 123, "goodsName": "..."}],
              "excelUrl": "https://..."
          }
      }
    """
    final_results = {}
    if not search_key_list:
        logger.warning("[业务/采集] 关键词列表为空 | 结果: [跳过执行]")
        return final_results

    target_url = "https://jinbao.pinduoduo.com/promotion/single-promotion"
    api_target = "/network/api/common/goodsList"

    logger.info(
        f"\n{'=' * 60}\n[业务/采集] 启动批量拉取任务 | 核心参数: 词数: 【{len(search_key_list)}】 | 阈值: 【{limit_count or '不限'}】 | 调试: 【{debug}】\n{'=' * 60}")

    with sync_playwright() as p:
        context = None
        try:
            args = ['--disable-blink-features=AutomationControlled', '--start-maximized']
            if debug: args.append('--window-position=0,0')

            context = launch_persistent_context(p, user_data_dir=user_data_dir, args=args, headless=not debug)
            page = context.pages[0] if context.pages else context.new_page()
            if debug: page.bring_to_front()

            logger.info(f"[业务/游览] 初始化目标页面 | URL: [{target_url}]")

            # --- 核心修复区域：成功则跳出，否则统一硬等10s ---
            logger.info("[业务/游览] 等待最多10s探活初始接口，仅当成功响应时跳过等待...")

            start_time = time.time()
            target_wait_sec = 10.0
            skip_wait = False  # 控制是否跳过等待的唯一开关

            try:
                # 设置 10s 超时去捕获请求，将 page.goto 裹在上下文里防止错失
                with page.expect_response(
                        lambda r: "/network/api/common/goodsList" in r.url and r.request.method == "POST",
                        timeout=int(target_wait_sec * 1000)
                ) as response_info:
                    page.goto(target_url, wait_until="domcontentloaded")

                resp = response_info.value

                try:
                    resp_json = resp.json()
                    # 【核心逻辑】：只有明确拿到 success: True，才允许打开跳过开关
                    if resp_json.get("success") is True:
                        logger.info("[业务/游览] ✅ 明确捕捉到成功的数据流，获得特权，提前跳出 10s 等待")
                        skip_wait = True
                    else:
                        logger.info("[业务/游览] ⚠️ 响应状态非成功(疑似被风控驳回)，必须等满 10 秒")
                except Exception:
                    logger.warning("[业务/游览] ⚠️ 响应无法解析为有效的 JSON，必须等满 10 秒")

            except Exception as e:
                # 捕获 Playwright 超时或页面崩溃等异常
                logger.info("[业务/游览] ⏳ 10秒内未捕捉到指定网络请求或发生异常，必须等满 10 秒")

            # 【强制时间补偿】：如果没拿到特权，计算已经过去的时间，强行把剩下的时间睡满
            if not skip_wait:
                elapsed = time.time() - start_time
                remain_time = target_wait_sec - elapsed
                if remain_time > 0:
                    logger.info(f"[业务/游览] ⏳ 强制挂起，正在补齐剩余的 {remain_time:.2f} 秒等待时间...")
                    page.wait_for_timeout(int(remain_time * 1000))

            # 无论如何，最后切入验证码探测分支
            handle_pdd_captcha(page)

            # ------------------------------------------------

            # --- 内部状态操作算子 ---
            def do_batch_select_all():
                try:
                    # : 高危 CSS 依赖，平台迭代随时可能让此定位器失效。
                    select_all_label = page.locator('.single-promotion-batch-part label').first
                    if select_all_label.is_visible():
                        robust_click(select_all_label)
                        page.wait_for_timeout(300)
                except Exception as e:
                    logger.warning(f"[业务/算子] 全选执行异常 | 原因: [{e}]")

            def do_export_and_intercept_url():
                try:
                    export_btn = page.locator('.single-promotion-batch-part button:has-text("导出Excel")').first
                    if not export_btn.is_visible():
                        logger.warning("[业务/算子] 未发现导出按钮 | 原因: [可能未勾选商品数据]")
                        return ""
                    robust_click(export_btn)

                    # : 高危 test-id 依赖。
                    confirm_btn = page.locator(
                        'div[data-testid="beast-core-modal-inner"] button:has-text("确定")').first
                    confirm_btn.wait_for(state="visible", timeout=3000)

                    def is_excel_request(response):
                        return "/network/api/promotion/generateExcelBygoodsIdList" in response.url and response.request.method == "POST"

                    logger.info("[业务/算子] 触发 Excel 异步生成，挂起等待底层响应...")
                    with page.expect_response(is_excel_request, timeout=20000) as response_info:
                        robust_click(confirm_btn)

                    res_json = response_info.value.json()
                    if res_json.get("success"):
                        excel_url = res_json.get("result", {}).get("excelUrl", "")
                        logger.info(f"[业务/算子] ✅ 直链剥离成功 | URL: [<{excel_url}>]")
                        return excel_url
                    else:
                        logger.error(f"[业务/算子] ❌ 后端驳回导出 | 驳回原因: [{res_json.get('errorMsg')}]")
                except Exception as e:
                    logger.error(f"[业务/算子] ❌ 导出流断裂 | 异常: [{e}]")
                return ""

            search_input = page.locator('.search-bar-input input[placeholder="请输入商品名称或短链"]')
            search_btn = page.locator('.search-bar-btn', has_text="搜索")
            next_btn_locator = page.locator('li[data-testid="beast-core-pagination-next"]')
            search_input.wait_for(state="visible", timeout=20000)

            # --- 核心采集大循环 ---
            for search_key in search_key_list:
                logger.info(f"\n--- [任务调度] 切入业务词: 【{search_key}】 ---")

                # 初始化空数据进行占位保护
                final_results[search_key] = {"goodsList": [], "excelUrl": ""}
                all_goods = []

                try:
                    search_input.clear()
                    search_input.fill(search_key)

                    def is_target_request(response):
                        if api_target not in response.url or response.request.method != "POST": return False
                        try:
                            payload = response.request.post_data_json
                            return payload and payload.get("keyword") == search_key
                        except Exception:
                            return False

                    logger.info(f"[业务/采集] 击发搜索条件，探底首屏数据流...")
                    with page.expect_response(is_target_request, timeout=20000) as response_info:
                        robust_click(search_btn)

                    json_data = response_info.value.json()
                    current_goods = json_data.get("result", {}).get("goodsList", []) if isinstance(json_data,
                                                                                                   dict) else []
                    all_goods.extend(current_goods)

                    logger.info(f"[业务/采集] 首屏剥离完毕 | 新增: [{len(current_goods)}] | 累计: [{len(all_goods)}]")
                    page.wait_for_timeout(1000)

                    batch_btn = page.locator('button:has-text("批量管理")').first
                    if batch_btn.is_visible():
                        robust_click(batch_btn)
                        page.wait_for_timeout(500)
                    do_batch_select_all()

                    # 翻页状态机
                    page_num = 1
                    error_retry_count = 0  # 🚀新增：连续错误重试计数器
                    MAX_RETRIES = 3        # 🚀新增：最大连续重试熔断阈值

                    while True:
                        if limit_count > 0 and len(all_goods) >= limit_count:
                            logger.info(
                                f"[业务/状态] 数据池达标 | 当前: [{len(all_goods)}] >= 阈值: [{limit_count}] | 停止翻页")
                            break

                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(500)

                        if not next_btn_locator.is_visible() or "PGT_disabled" in (
                                next_btn_locator.get_attribute("class") or ""):
                            logger.info("[业务/状态] 底层触底 (页尾/禁点) | 停止翻页")
                            break

                        page_num += 1
                        logger.info(f"[业务/采集] 驱进第 【{page_num}】 页...")

                        try:
                            with page.expect_response(is_target_request, timeout=20000) as response_info:
                                robust_click(next_btn_locator)

                            json_data = response_info.value.json()
                            current_goods = json_data.get("result", {}).get("goodsList", []) if isinstance(json_data, dict) else []

                            if not current_goods:
                                logger.info("[业务/状态] 遭遇空报文 | 停止翻页")
                                break

                            all_goods.extend(current_goods)
                            logger.info(
                                f"[业务/采集] 报文剥离完毕 | 轮次: [{page_num}] | 新增: [{len(current_goods)}] | 累计: [{len(all_goods)}]")

                            # 🚀请求成功，清零重试计数器
                            error_retry_count = 0

                            page.wait_for_timeout(500)
                            do_batch_select_all()
                            page.wait_for_timeout(1000)

                        except Exception as e:
                            logger.warning(f"[业务/异常] 翻页数据流断裂 | 异常: [{e}]")

                            # 🚀明确探测是否真的是因为验证码引发的超时
                            has_captcha = False
                            try:
                                if page.locator('button:has-text("安全验证")').first.is_visible(timeout=1000) or \
                                   page.locator('.slider-img-bg').first.is_visible(timeout=1000):
                                    has_captcha = True
                            except Exception:
                                pass

                            if has_captcha:
                                logger.info("[业务/状态] 检测到安全盾，准备切入风控对抗逻辑...")
                                if handle_pdd_captcha(page):
                                    logger.info("[业务/状态] ✅ 安全盾解除，准备重试该页...")
                                    page_num -= 1  # 🚀补偿：刚才那一页没翻成功，退回页码以免无意义累加
                                    continue
                                else:
                                    logger.error("[业务/异常] ❌ 风控强阻拦无法突破，强制截断当前翻页链路。")
                                    break
                            else:
                                # 🚀如果没有验证码，说明是纯粹的网络超时或被平台软拦截（不给数据）
                                error_retry_count += 1
                                if error_retry_count >= MAX_RETRIES:
                                    logger.error(f"[业务/异常] ❌ 连续 {MAX_RETRIES} 次无验证码超时，触发硬性熔断，防止死循环！")
                                    break

                                logger.warning(f"[业务/状态] 未检测到安全盾，执行常规网络重试 ({error_retry_count}/{MAX_RETRIES})...")
                                page_num -= 1  # 🚀补偿：退回页码
                                page.wait_for_timeout(2000) # 稍作喘息再次请求
                                continue

                    if limit_count > 0:
                        all_goods = all_goods[:limit_count]

                    logger.info(f"[业务/收尾] 启动收官融合矩阵 | 词: 【{search_key}】...")
                    excel_url = do_export_and_intercept_url()
                    all_goods = download_and_merge_excel(excel_url, all_goods)

                    final_results[search_key] = {
                        "goodsList": all_goods,
                        "excelUrl": excel_url
                    }
                    logger.info(
                        f"[业务/交付] ✅ 单体流转跑通 | 词: 【{search_key}】 | 终态数量: [{len(all_goods)}] | 总进度: 【{len(final_results)}/{len(search_key_list)}】")

                except Exception as inner_e:
                    logger.error(f"[业务/交付] ❌ 单体崩溃 | 词: 【{search_key}】 | 现场信息:\n{traceback.format_exc()}")
                    save_forensics(page, f"search_intercept_fail_{search_key}")

                    if not handle_pdd_captcha(page):
                        logger.error("[系统/灾难] 🚨 安全盾硬性锁定，主动熔断整场采集计划！")
                        return final_results
                    page.wait_for_timeout(2000)

        except Exception as global_e:
            logger.error(f"[系统/灾难] 🚨 骨干链路熔断:\n{traceback.format_exc()}")
            if context and context.pages:
                save_forensics(context.pages[0], "search_intercept_fatal_error")

        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info(f"[系统/退出] 🚀 Playwright 沙盒已销毁 | 闭环交付总量: 【{len(final_results)}】\n")

    return final_results

# ==============================================================================
#                                   使用示例
# ==============================================================================
if __name__ == "__main__":

    # # 打开拼多多网页版
    # USER_DATA_DIR = r"W:\\project\\python_project\\easy_shop\\temp_data\\browser_data\\pdd_browser_data"
    # TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"
    #
    # # 场景二：携带环境自由操作 (按需打开) 拼多多 多人团 网页版
    # open_browser_for_manual_use(
    #     user_data_dir=USER_DATA_DIR,
    #     home_url=TEST_URL
    # )


    TEST_URL = "https://jinbao.pinduoduo.com/promotion/single-promotion"
    USER_DATA_DIR = r"W:\temp\biance_pdd_myself"



    # # 场景二：携带环境自由操作 (按需打开) 多多进宝
    # open_browser_for_manual_use(
    #     user_data_dir=USER_DATA_DIR,
    #     home_url=TEST_URL
    # )


    result = search_goods_and_intercept(
        search_key_list=["方便面"],
        user_data_dir=USER_DATA_DIR,
        debug=True,
        limit_count=100
    )

    for key, data_dict in result.items():
        goods = data_dict.get("goodsList", [])
        excel_url = data_dict.get("excelUrl", "")
        unique_goods_ids = {item.get("goodsId") for item in goods if "goodsId" in item}

        print(f"关键字: 【{key}】 | 净商品数量: [{len(unique_goods_ids)}]")
        if excel_url:
            print(f"🔥 Excel 导出直链: {excel_url}")

        if goods:
            sample_goods = goods[0]
            print(f"👉 混合呈现 - 商品名称: {sample_goods.get('goodsName', sample_goods.get('商品名称'))}")
            print(f"👉 混合呈现 - 专属短链接: {sample_goods.get('短链接', '无')}")
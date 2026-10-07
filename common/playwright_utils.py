# -*- coding: utf-8 -*-
"""
=========================================================================================
[Playwright 通用基础工具箱]
提取原则：绝对通用、不夹带任何特定网站业务逻辑、高度可复用。
=========================================================================================
"""
import os
import shutil
import time
import json
import logging
import traceback
import io
import requests

try:
    import pandas as pd
except ImportError:
    pd = None

from playwright.sync_api import sync_playwright

USER_DATA_DIR = r"W:\temp\biance_pdd_myself"

# 初始化基础日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] %(message)s')
logger = logging.getLogger("playwright_utils")


def clean_browser_cache(user_data_dir: str):
    """
    [通用] 清理浏览器冗余缓存目录，保留 Cookie/LocalStorage 等登录凭证。
    适用于长期运行的 RPA 项目，防止用户目录体积无限膨胀。
    """
    if not os.path.exists(user_data_dir):
        return

    garbage = ("Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "Service Worker", "CacheStorage")
    deleted = 0
    for base in (user_data_dir, os.path.join(user_data_dir, "Default")):
        for name in garbage:
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                deleted += 1
            except Exception:
                pass
    logger.info(f"[缓存清理] 瘦身完成 | 目录: <{user_data_dir}> | 清理冗余项: 【{deleted}】")


def launch_persistent_context(p, user_data_dir: str, args: list = None, viewport=None, hide_automation=True,
                              headless=False):
    """
    [通用] 统一的持久化上下文启动口，收敛繁杂的 Launch 配置。
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
        kwargs["no_viewport"] = True  # 跟随窗口大小

    if hide_automation:
        kwargs["ignore_default_args"] = ["--enable-automation"]

    return p.chromium.launch_persistent_context(**kwargs)


def login_and_save_session(user_data_dir: str, login_url: str):
    """
    [通用] 打开可见浏览器供人工手动登录，回车后关闭并把会话(Cookie/Token)固化到本地目录。
    """
    logger.info(f"[环境/保存] 准备手动登录 | 存储路径: <{user_data_dir}> | 目标网站: {login_url}")
    clean_browser_cache(user_data_dir)

    with sync_playwright() as p:
        context = None
        try:
            context = launch_persistent_context(
                p,
                user_data_dir=user_data_dir,
                hide_automation=False,
                headless=False
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url)

            # 阻塞程序，等待人工在浏览器中完成登录
            input(f"\n[环境/保存] 等待操作 | 请在弹出的浏览器中登录，登录成功后，请按 【Enter】 键关闭并保存会话...")
            logger.info("[环境/保存] 会话已固化到本地 | 结果: [Success]")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass


def open_browser_for_manual_use(user_data_dir: str, home_url: str):
    """
    [通用] 携带已保存的本地环境，启动可见浏览器交由人工自由操作/核验。
    程序会一直挂起，直到用户手动关闭浏览器窗口。
    """
    logger.info(f"\n{'=' * 60}\n[环境/使用] 启动本地浏览器交接控制权 | 目录: <{user_data_dir}>\n{'=' * 60}")
    with sync_playwright() as p:
        context = None
        try:
            # 强制窗口位置归零，防止多屏幕下离屏坐标缓存导致窗口找不到
            args = ['--disable-blink-features=AutomationControlled', '--start-maximized', '--window-position=0,0']
            context = launch_persistent_context(p, user_data_dir=user_data_dir, args=args, headless=False)

            page = context.pages[0] if context.pages else context.new_page()
            page.bring_to_front()
            page.goto(home_url)

            logger.info("[环境/使用] ✅ 浏览器已就绪，控制权已交接 | 🛑 退出方式: 【请直接关闭浏览器窗口，程序将自动结束】")
            # 阻塞等待窗口被关闭
            page.wait_for_event("close", timeout=0)
        except Exception as e:
            logger.warning(f"[环境/使用] 浏览器运行异常 | 可能原因: 【环境损坏或窗口被手动强杀: {e}】")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info("[环境/使用] 👋 窗口已关闭，控制权收回，系统资源已释放。\n")


def robust_click(locator):
    """
    [通用附赠] 三段降级点击：常规 -> 强制穿透遮挡 -> JS 原生绕过。
    极其通用的解决 Playwright 经常报 "element is intercepted by..." 的痛点。
    """
    for attempt in ("normal", "force"):
        try:
            locator.click(timeout=1500, force=(attempt == "force"))
            return
        except Exception:
            continue
    # 终极保底：通过原生 JS 触发点击
    locator.evaluate("node => node.click()")


def save_forensics(page, tag: str, save_dir: str = "forensics_logs", extra_info: dict = None):
    """
    [通用附赠] 案发现场固化：当发生异常时，统一落地 截图 + HTML源码 + JSON排查信息。
    """
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception:
        pass

    base_name = f"forensic_{tag}_{int(time.time() * 1000)}"
    base_path = os.path.join(save_dir, base_name)

    # 1. 保存当前视口截图
    try:
        page.screenshot(path=f"{base_path}.png", full_page=False)
    except Exception:
        pass

    # 2. 保存当前 DOM 树
    try:
        with open(f"{base_path}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

    # 3. 保存额外诊断信息
    try:
        payload = dict(extra_info or {})
        payload["url"] = page.url
        with open(f"{base_path}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    logger.warning(f"[故障排查] 故障现场已落盘 | 文件前缀: <{base_path}>")
    return base_path


def download_and_merge_excel(excel_url: str, goods_list: list) -> list:
    """
    [数据/融合] 在内存中下载导出的 Excel 表格，将所有列数据拼接到原本的 JSON 商品列表中。
    按 '商品ID' 与 'goodsId' 匹配。
    """
    if not excel_url or not goods_list:
        return goods_list

    if pd is None:
        logger.error("[数据/融合] ⚠️ 缺少 pandas 依赖，无法解析 Excel。请运行: pip install pandas requests xlrd lxml")
        return goods_list

    logger.info(f"[数据/融合] 正在后台下载 Excel 并进行数据融合...")
    try:
        # 1. 下载文件，直接存入内存不落盘
        resp = requests.get(excel_url, timeout=30)
        resp.raise_for_status()
        file_bytes = io.BytesIO(resp.content)

        # 2. 解析 Excel
        try:
            # 尝试用标准 xlrd 读取 (针对原生的 .xls)
            df = pd.read_excel(file_bytes, engine="xlrd")
        except Exception as e1:
            # 兼容处理：部分平台导出的 .xls 本质是 HTML 表格
            logger.debug(f"[数据/融合] 标准读取失败({e1})，尝试以 HTML 结构重解析...")
            file_bytes.seek(0)
            dfs = pd.read_html(file_bytes, encoding='utf-8')
            df = dfs[0] if dfs else pd.DataFrame()

        if df.empty:
            logger.warning("[数据/融合] ⚠️ 解析到的 Excel 提取数据为空。")
            return goods_list

        if '商品ID' not in df.columns:
            logger.warning(f"[数据/融合] ⚠️ Excel 表头中未发现 '商品ID'，放弃融合。现有列: {df.columns.tolist()}")
            return goods_list

        # 3. 将 DataFrame 转换为方便查询的字典字典，键为转为字符型的 商品ID
        df['商品ID'] = df['商品ID'].astype(str)
        # 过滤掉 NaN 的空值，保持数据清爽
        excel_records = {
            str(row['商品ID']): {k: v for k, v in row.items() if pd.notna(v)}
            for row in df.to_dict(orient="records")
        }

        # 4. 全量字段融合
        merged_count = 0
        for goods in goods_list:
            g_id = str(goods.get("goodsId", ""))
            if g_id in excel_records:
                # 将 Excel 中的列(如短链接、佣金比例等) 无缝 update 到 JSON 字典里
                goods.update(excel_records[g_id])
                merged_count += 1

        logger.info(f"[数据/融合] ✅ Excel 字段无缝融合完毕！成功匹配条数: {merged_count}/{len(goods_list)}")

    except Exception as e:
        logger.error(f"[数据/融合] ❌ 下载或融合过程发生异常: {e}")

    return goods_list


def search_goods_and_intercept(search_key_list: list, user_data_dir: str, limit_count: int = 500,
                               debug: bool = False) -> dict:
    """
    [业务/查询] 访问多多进宝单品推广页，支持同一窗口下连续查询多个关键字，并精准拦截底层的 goodsList 数据。
    支持自动翻页直到满足指定数量或到达最后一页。如遇风控安全验证拦截，则立即终止并返回已抓取数据。

    :param search_key_list: 搜索关键字列表 (例如: ["可乐", "雪碧"])
    :param user_data_dir: 浏览器本地持久化缓存目录
    :param limit_count: 单个关键词需要的最小商品数量。0表示一直拉取直到最后一页。
    :param debug: 调试模式。True则显示浏览器界面，False则静默后台运行
    :return: 包含所有查询结果的字典，格式如 {"关键字": {"goodsList": [...], "excelUrl": "..."}}
    """
    target_url = "https://jinbao.pinduoduo.com/promotion/single-promotion"
    api_target = "/network/api/common/goodsList"

    logger.info(
        f"\n{'=' * 60}\n[业务/查询] 开始批量搜索 | 关键字数: {len(search_key_list)} | 目标数量: {'不限' if limit_count == 0 else limit_count} | 调试模式: {debug}\n{'=' * 60}")

    final_results = {}

    if not search_key_list:
        logger.warning("[业务/查询] 搜索关键字列表为空，直接返回。")
        return final_results

    with sync_playwright() as p:
        context = None
        try:
            # 动态控制 headless 模式
            headless_mode = not debug
            args = ['--disable-blink-features=AutomationControlled', '--start-maximized']
            if debug:
                args.append('--window-position=0,0')

            # 启动浏览器上下文
            context = launch_persistent_context(
                p,
                user_data_dir=user_data_dir,
                args=args,
                headless=headless_mode
            )

            page = context.pages[0] if context.pages else context.new_page()
            if debug:
                page.bring_to_front()

            logger.info(f"[业务/查询] 正在加载基础页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded")

            # ================= 辅助内部函数：高内聚处理页面UI操作 =================
            def do_batch_select_all():
                """尝试点击[本页全选]复选框"""
                try:
                    # 避免类名混淆，依靠父容器的语义化 class 和子元素 label 来安全定位
                    select_all_label = page.locator('.single-promotion-batch-part label').first
                    if select_all_label.is_visible():
                        robust_click(select_all_label)
                        page.wait_for_timeout(300)  # 给前端Vue响应状态的时间
                except Exception as e:
                    logger.warning(f"[业务/勾选] '本页全选' 操作未能成功: {e}")

            def do_export_and_intercept_url() -> str:
                """尝试点击[导出Excel]、处理弹窗并拦截接口获取真实下载链接"""
                extracted_url = ""
                try:
                    export_btn = page.locator('.single-promotion-batch-part button:has-text("导出Excel")').first
                    if not export_btn.is_visible():
                        logger.warning("[业务/导出] 未发现 '导出Excel' 按钮，可能是未成功勾选任何商品。")
                        return ""

                    robust_click(export_btn)

                    # 定位并等待弹窗中的“确定”按钮出现 (依据 data-testid 定位弹窗容器，规避动态哈希class)
                    modal_confirm_btn = page.locator(
                        'div[data-testid="beast-core-modal-inner"] button:has-text("确定")').first
                    modal_confirm_btn.wait_for(state="visible", timeout=3000)

                    # 定义目标拦截请求
                    def is_excel_request(response):
                        return "/network/api/promotion/generateExcelBygoodsIdList" in response.url and response.request.method == "POST"

                    logger.info("[业务/导出] 已触发导出确认，等待服务器生成 Excel...")
                    with page.expect_response(is_excel_request, timeout=20000) as response_info:
                        robust_click(modal_confirm_btn)

                    res_json = response_info.value.json()
                    if res_json.get("success"):
                        extracted_url = res_json.get("result", {}).get("excelUrl", "")
                        logger.info(f"[业务/导出] ✅ 成功获取 Excel 下载链接: {extracted_url}")
                    else:
                        logger.error(f"[业务/导出] ❌ 服务端返回失败信息: {res_json.get('errorMsg')}")

                except Exception as e:
                    logger.error(f"[业务/导出] ❌ 执行导出 Excel 过程发生异常: {e}")

                return extracted_url

            # ====================================================================

            # 定位输入框与搜索按钮
            search_input = page.locator('.search-bar-input input[placeholder="请输入商品名称或短链"]')
            search_btn = page.locator('.search-bar-btn', has_text="搜索")
            # 定位下一页按钮
            next_btn_locator = page.locator('li[data-testid="beast-core-pagination-next"]')

            # 等待输入框出现，确保页面加载完成
            search_input.wait_for(state="visible", timeout=20000)

            # ================= 核心：循环执行并发查询 =================
            for search_key in search_key_list:
                logger.info(f"--- 开始处理关键字: <{search_key}> ---")
                try:
                    all_goods_for_current_key = []

                    # 每次搜索前确保输入框清空并填入新词
                    search_input.clear()
                    search_input.fill(search_key)
                    logger.info(f"[业务/查询] 已填入: {search_key}")

                    # 定义严格的请求匹配规则
                    def is_target_request(response):
                        if api_target not in response.url or response.request.method != "POST":
                            return False
                        try:
                            payload = response.request.post_data_json
                            if payload and payload.get("keyword") == search_key:
                                return True
                        except Exception:
                            pass
                        return False

                    logger.info(f"[业务/查询] 触发首屏搜索，正在进行深度拦截验证...")

                    # 开启拦截等待首屏数据
                    with page.expect_response(is_target_request, timeout=20000) as response_info:
                        robust_click(search_btn)

                    # 提取 JSON 数据获取首屏商品
                    response = response_info.value
                    json_data = response.json()
                    current_goods = json_data.get("result", {}).get("goodsList", []) if isinstance(json_data,
                                                                                                   dict) else []
                    all_goods_for_current_key.extend(current_goods)

                    logger.info(
                        f"[业务/查询] 首屏获取完成 | 新增数量: {len(current_goods)} | 累计数量: {len(all_goods_for_current_key)}")
                    page.wait_for_timeout(1000)

                    # --- 【新增流程】首屏开启“批量管理”并全选当前页 ---
                    batch_manage_btn = page.locator('button:has-text("批量管理")').first
                    if batch_manage_btn.is_visible():
                        robust_click(batch_manage_btn)
                        page.wait_for_timeout(500)  # 等待UI变为复选框形态
                    do_batch_select_all()

                    # ========== 自动翻页逻辑 ==========
                    page_num = 1
                    while True:
                        # 退出条件 1: 达到指定数量 (且不为0)
                        if limit_count > 0 and len(all_goods_for_current_key) >= limit_count:
                            logger.info(f"[业务/查询] 已满足目标数量限制 ({limit_count})，停止翻页。")
                            break

                        # 将页面滚动到底部，确保分页组件进入视图并加载完毕
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(500)

                        if not next_btn_locator.is_visible():
                            logger.info("[业务/查询] 未在页面上找到下一页按钮，可能数据仅有一页，停止翻页。")
                            break

                        # 退出条件 2: 下一页按钮存在 "PGT_disabled" 样式（最后一页）
                        btn_class = next_btn_locator.get_attribute("class") or ""
                        if "PGT_disabled" in btn_class:
                            logger.info("[业务/查询] 下一页按钮已置灰（到达最后一页），停止翻页。")
                            break

                        page_num += 1
                        logger.info(f"[业务/查询] 正在翻页，请求第 {page_num} 页数据...")

                        try:
                            # 点击下一页并拦截请求
                            with page.expect_response(is_target_request, timeout=20000) as response_info:
                                robust_click(next_btn_locator)

                            response = response_info.value
                            json_data = response.json()
                            current_goods = json_data.get("result", {}).get("goodsList", []) if isinstance(json_data,
                                                                                                           dict) else []

                            if not current_goods:
                                logger.info("[业务/查询] 本页返回商品为空，停止翻页。")
                                break

                            all_goods_for_current_key.extend(current_goods)
                            logger.info(
                                f"[业务/查询] 第 {page_num} 页获取完成 | 新增数量: {len(current_goods)} | 累计数量: {len(all_goods_for_current_key)}")

                            # --- 【新增流程】新的一页加载完毕后，继续点击全选 ---
                            page.wait_for_timeout(500)
                            do_batch_select_all()

                            # 翻页防风控缓冲
                            page.wait_for_timeout(1000)

                        except Exception as e:
                            logger.warning(f"[业务/查询] 翻页过程中发生超时或异常，停止当前关键词翻页。异常信息: {e}")

                            # 翻页时检测是否命中风控弹窗
                            try:
                                if page.locator('text="安全验证"').first.is_visible() or page.locator(
                                        'text="完成拼多多官方验证"').first.is_visible():
                                    logger.error(
                                        f"[业务/拦截] 🚨 翻页触发安全验证风控！提前终止全局爬取，直接返回现有数据。")
                                    # 此时遇到风控，可能无法进行UI导出操作，直接返回已有数据。
                                    if limit_count > 0:
                                        all_goods_for_current_key = all_goods_for_current_key[:limit_count]
                                    final_results[search_key] = {
                                        "goodsList": all_goods_for_current_key,
                                        "excelUrl": ""
                                    }
                                    return final_results
                            except Exception:
                                pass
                            break
                    # ==================================

                    # 最终处理：如果设定了 limit_count，截断多余的数据
                    if limit_count > 0:
                        all_goods_for_current_key = all_goods_for_current_key[:limit_count]

                    # --- 【新增流程】无论是因为满额还是翻到底，都在收尾阶段触发 Excel 导出 ---
                    logger.info(f"[业务/收尾] 正在为关键词 <{search_key}> 导出所选商品的 Excel...")
                    current_excel_url = do_export_and_intercept_url()

                    # --- 【新增流程】下载 Excel 并融合数据 ---
                    all_goods_for_current_key = download_and_merge_excel(current_excel_url, all_goods_for_current_key)

                    # 组装混合数据返回结构
                    final_results[search_key] = {
                        "goodsList": all_goods_for_current_key,
                        "excelUrl": current_excel_url
                    }

                    logger.info(
                        f"[业务/查询] ✅ 关键字 <{search_key}> 处理完毕 | 最终采收数量: {len(all_goods_for_current_key)} | 总体进度: {len(final_results)}/{len(search_key_list)}")

                except Exception as inner_e:
                    error_trace = traceback.format_exc()
                    logger.error(f"[业务/查询] ❌ 关键字 <{search_key}> 执行或拦截失败，异常详情:\n{error_trace}")

                    # 发生错误时，尽量保留已爬取的数据
                    if search_key not in final_results:
                        final_results[search_key] = {"goodsList": [], "excelUrl": ""}

                    save_forensics(page, f"search_intercept_fail_{search_key}")

                    # 首次搜索时检测是否命中风控弹窗
                    try:
                        if page.locator('text="安全验证"').first.is_visible() or page.locator(
                                'text="完成拼多多官方验证"').first.is_visible():
                            logger.error(f"[业务/拦截] 🚨 搜索首屏即触发安全验证风控！放弃后续关键字，直接返回当前数据。")
                            return final_results
                    except Exception:
                        pass

                    page.wait_for_timeout(2000)

        except Exception as global_e:
            global_trace = traceback.format_exc()
            logger.error(f"[业务/查询] 🚨 发生全局致命错误，流程中断:\n{global_trace}")
            if context and context.pages:
                save_forensics(context.pages[0], "search_intercept_fatal_error")

        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info(f"[业务/查询] 🚀 浏览器资源已释放，共完成 {len(final_results)} 个关键字查询任务。\n")

    return final_results


# ==============================================================================
#                                   使用示例
# ==============================================================================
if __name__ == "__main__":

    # 打开拼多多网页版
    USER_DATA_DIR = r"W:\\project\\python_project\\easy_shop\\temp_data\\browser_data\\pdd_browser_data"
    TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"

    # 场景二：携带环境自由操作 (按需打开)
    open_browser_for_manual_use(
        user_data_dir=USER_DATA_DIR,
        home_url=TEST_URL
    )



    # 配置测试环境目录与目标网址
    TEST_URL = "https://jinbao.pinduoduo.com/promotion/single-promotion"
    USER_DATA_DIR = r"W:\temp\biance_pdd_myself"

    # 执行搜索并拦截
    result = search_goods_and_intercept(search_key_list=["方便面"], user_data_dir=USER_DATA_DIR, debug=True,
                                        limit_count=100)

    # 提取并解析数据
    for key, data_dict in result.items():
        goods = data_dict.get("goodsList", [])
        excel_url = data_dict.get("excelUrl", "")

        unique_goods_ids = {item["goodsId"] for item in goods if "goodsId" in item}

        print(f"关键字: {key} | 不重复商品ID数量: {len(unique_goods_ids)}")
        if excel_url:
            print(f"🔥 获取到导出的 Excel 表格直链: {excel_url}")

        # 演示一下融合后的结果，随便取一条包含【短链接】的商品数据打印
        if goods:
            sample_goods = goods[0]
            print(f"👉 融合示例 - 商品名称: {sample_goods.get('goodsName', sample_goods.get('商品名称'))}")
            print(f"👉 融合示例 - 短链接: {sample_goods.get('短链接', '无')}")

    # 场景二：携带环境自由操作 (按需打开)
    open_browser_for_manual_use(
        user_data_dir=USER_DATA_DIR,
        home_url=TEST_URL
    )



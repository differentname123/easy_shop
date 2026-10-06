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


def search_goods_and_intercept(search_key: str, user_data_dir: str) -> dict:
    """
    [业务/查询] 访问多多进宝单品推广页，输入关键字搜索并拦截底层 goodsList 数据接口。

    :param search_key: 搜索关键字 (例如: "可乐")
    :param user_data_dir: 浏览器本地持久化缓存目录
    :return: 拦截并解析后的 JSON 数据字典；若失败则返回 None
    """
    target_url = "https://jinbao.pinduoduo.com/promotion/single-promotion"
    api_target = "/network/api/common/goodsList"

    logger.info(f"\n{'=' * 60}\n[业务/查询] 开始执行搜索并拦截 | 关键字: <{search_key}>\n{'=' * 60}")

    with sync_playwright() as p:
        context = None
        try:
            # 复用基础工具箱：启动带有登录凭证的持久化浏览器上下文
            args = ['--disable-blink-features=AutomationControlled', '--start-maximized', '--window-position=0,0']
            context = launch_persistent_context(p, user_data_dir=user_data_dir, args=args, headless=False)

            page = context.pages[0] if context.pages else context.new_page()
            page.bring_to_front()

            logger.info(f"[业务/查询] 正在加载页面: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded")

            # 依据提供的 DOM 结构定位输入框与搜索按钮
            search_input = page.locator('.search-bar-input input[placeholder="请输入商品名称或短链"]')
            search_btn = page.locator('.search-bar-btn', has_text="搜索")

            # 等待输入框出现，清空并填入搜索词
            search_input.wait_for(state="visible", timeout=15000)
            search_input.clear()
            search_input.fill(search_key)
            logger.info(f"[业务/查询] 已填入搜索关键字: {search_key}")

            # 开启拦截等待：要求 URL 包含目标路径，且必须是 POST 请求
            logger.info(f"[业务/查询] 触发搜索，正在监听并拦截目标接口: {api_target} ...")
            with page.expect_response(
                    lambda response: api_target in response.url and response.request.method == "POST",
                    timeout=20000
            ) as response_info:
                # 复用基础工具箱：执行鲁棒性极高的点击操作
                robust_click(search_btn)

            # 获取拦截到的响应对象
            response = response_info.value
            logger.info(f"[业务/查询] ✅ 成功拦截底层请求 | 状态码: {response.status}")

            # 解析并返回 JSON，支持直接提取目标字段
            json_data = response.json()
            return json_data

        except Exception as e:
            logger.error(f"[业务/查询] 搜索或拦截过程发生异常: {e}")
            if context and context.pages:
                # 复用基础工具箱：异常时保存当前屏幕快照和DOM，用于案发现场排查
                save_forensics(context.pages[0], f"search_intercept_fail_{search_key}")
            return None

        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info("[业务/查询] 🚀 浏览器资源已释放，查询任务结束。\n")

# ==============================================================================
#                                   使用示例
# ==============================================================================
if __name__ == "__main__":
    # 配置测试环境目录与目标网址
    TEST_URL = "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel"
    TEST_URL = "https://jinbao.pinduoduo.com/promotion/single-promotion"

    USER_DATA_DIR = r"W:\temp\biance_pdd_myself"

    # 执行搜索并拦截
    result = search_goods_and_intercept(search_key="零食", user_data_dir=USER_DATA_DIR)

    if result and result.get("success"):
        goods_list = result["result"]["goodsList"]
        print(f"成功获取到 {len(goods_list)} 条商品数据！")


    # # 场景一：初始化/更新环境凭证
    # login_and_save_session(
    #     user_data_dir=TEST_USER_DATA_DIR,
    #     login_url=TEST_URL
    # )

    # 场景二：携带环境自由操作
    open_browser_for_manual_use(
        user_data_dir=USER_DATA_DIR,
        home_url=TEST_URL
    )
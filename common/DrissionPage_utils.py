# -*- coding: utf-8 -*-
import time

from DrissionPage import ChromiumPage, ChromiumOptions


def init_browser(user_data_dir: str, port: int = 9222, headless: bool = False) -> ChromiumPage:
    """
    初始化并返回一个浏览器对象
    :param user_data_dir: 浏览器缓存持久化目录 (极其重要，用于保存登录态)
    :param port: 浏览器调试端口 (多进程并发时必须不同)
    :param headless: 是否使用无头模式 (调试时建议False，稳定后可视风控情况开启)
    """
    co = ChromiumOptions()
    # 使用本地正常的 Chrome 路径
    co.set_browser_path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')

    # 设置缓存路径与端口隔离
    co.set_user_data_path(user_data_dir)
    co.set_local_port(port)

    # 无头模式配置 (如果需要隐藏界面)
    if headless:
        co.headless()

    # 伪装为移动端 (过拼多多 H5 风控的核心)
    mobile_ua = 'Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.162 Mobile Safari/537.36'
    co.set_user_agent(mobile_ua)

    # 启动浏览器
    page = ChromiumPage(co)
    return page


def open_browser_for_manual_use(user_data_dir: str):
    """
    启动浏览器交由人类手动操作（用于登录、过滑块等固化 Cookie）
    """
    print(f"[*] 正在启动手动接管模式，数据目录: {user_data_dir}")
    page = init_browser(user_data_dir=user_data_dir, port=9222)

    # 跳转到登录页或首页
    page.get('https://mobile.pinduoduo.com/')

    # 阻塞主线程，直到手动操作完成
    input("\n[!] 请在弹出的浏览器中完成登录操作。\n[!] 操作完成后，在此控制台按下【Enter】键关闭浏览器并保存环境...")

    # 显式关闭浏览器
    page.quit()
    print("[*] 浏览器已关闭，登录态已保存。")


def fetch_pdd_goods_info(user_data_dir: str, goods_id: str):
    """
    执行自动化采集业务的主函数
    """
    print(f"[*] 开始抓取商品: {goods_id}")
    page = init_browser(user_data_dir=user_data_dir, port=9222)

    try:
        url = f'https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}'
        page.get(url)

        # 智能等待页面元素加载 (比强行 time.sleep 更好)
        # 假设拼多多价格类名为 .price，根据实际情况修改
        if page.wait.ele_loaded('.price', timeout=10):
            price_ele = page.ele('.price')
            print(f"[+] 提取成功！商品 {goods_id} 的价格为: {price_ele.text}")
        else:
            print(f"[-] 提取失败！未在超时时间内找到价格元素，可能触发了风控。")

        # 这里可以加入截图给 AI 处理的逻辑
        # page.get_screenshot(path=f'goods_{goods_id}.png')

    except Exception as e:
        print(f"[x] 运行出现异常: {e}")
    finally:
        # 【解答问题1】: 无论成功失败，确保退出时关闭当前浏览器进程
        page.quit()
        print(f"[*] 浏览器资源已释放。")


if __name__ == '__main__':
    # 定义您的缓存目录
    MY_USER_DATA_DIR = r"W:\temp\drission_page_myself"

    # 场景一：初次使用，先手动登录保存 Cookie (取消注释即可运行)
    open_browser_for_manual_use(MY_USER_DATA_DIR)

    # 场景二：自动化抓取指定商品
    fetch_pdd_goods_info(MY_USER_DATA_DIR, '993843123351')
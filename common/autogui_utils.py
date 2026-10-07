import uiautomation as auto
import pyautogui
import pyperclip
import time


def fetch_sku_via_search(goods_url):
    print("[*] 正在查找拼多多窗口...")
    # ==========================================
    # 第一步：控场 - 锁定并激活窗口
    # ==========================================
    pdd_window = auto.WindowControl(searchDepth=1, Name='拼多多')

    if not pdd_window.Exists(3):
        print("[-] 未找到拼多多小程序窗口，请确保已在微信中打开一次")
        return False

    # 激活并利用置顶机制强行拉起窗口
    pdd_window.SetActive()
    pdd_window.SetTopmost(True)
    time.sleep(0.5)
    pdd_window.SetTopmost(False)

    # 每次操作前动态获取最新窗口坐标，防止窗口被拖动
    rect = pdd_window.BoundingRectangle

    # ==========================================
    # 第二步：首页 - 点击顶部搜索入口 (参考截图1)
    # ==========================================
    print("[*] 正在点击首页搜索栏...")
    # 计算搜索框所在的屏幕相对坐标：X居中，Y向下偏移 65 像素
    search_entry_x = rect.left + (rect.right - rect.left) // 2
    search_entry_y = rect.top + 65

    auto.Click(search_entry_x, search_entry_y)
    time.sleep(1.5)  # 等待搜索输入页面完全加载

    # ==========================================
    # 第三步：搜索页 - 粘贴链接并触发搜索 (参考截图2)
    # ==========================================
    print("[*] 正在输入商品链接并搜索...")
    pyperclip.copy(goods_url)
    # 此时焦点通常会自动落在输入框中，直接发送粘贴和回车指令
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.5)
    auto.SendKeys('{Enter}')

    print("[*] 正在跳转至商品详情页，等待网络加载 (5秒)...")
    time.sleep(5)  # 详情页图片和数据较多，必须给足加载时间

    # ==========================================
    # 第四步：详情页 - 点击购买按钮展开 SKU (参考截图3)
    # ==========================================
    print("[*] 尝试点击底部购买按钮...")

    # 策略 A：尝试使用 UI 接口正规查找（虽然大概率被框架屏蔽，但作为优先尝试）
    buy_btn_clicked = False
    buy_btn = pdd_window.Control(searchDepth=20, Name="发起拼单")
    if not buy_btn.Exists(1):
        buy_btn = pdd_window.Control(searchDepth=20, Name="单独购买")

    if buy_btn.Exists(1):
        try:
            buy_btn.Invoke()
            buy_btn_clicked = True
        except LookupError:
            buy_btn.Click()
            buy_btn_clicked = True

    # 策略 B：兜底方案 - 相对坐标点击 (强烈推荐)
    # 无论商品长什么样，“发起拼单”或购买按钮永远固定在窗口右下角
    if not buy_btn_clicked:
        print("[*] UI 元素不可见，启用右下角坐标盲点模式...")
        # 重新获取坐标以防万一
        rect = pdd_window.BoundingRectangle
        # 计算右下角坐标：距离右边缘约 60 像素，距离底部约 25 像素
        buy_x = rect.right - 60
        buy_y = rect.bottom - 25
        auto.Click(buy_x, buy_y)

    print("[+] 已触发购买按钮，等待 SKU 面板弹出...")
    time.sleep(2)  # 等待 SKU 抽屉向上滑出的动画结束 (参考截图4)

    # ==========================================
    # 第五步：收网取证 - 局部截图
    # ==========================================
    screenshot_path = "current_sku_panel.png"
    rect = pdd_window.BoundingRectangle
    region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    pyautogui.screenshot(screenshot_path, region=region)
    print(f"[+] SKU 面板截图成功，已精准截取小程序区域并保存至 {screenshot_path}")

    # : 接入您的 AI 大模型进行图文提取

    return True


# 运行测试
if __name__ == "__main__":
    # 使用您截图中的测试链接
    url = "https://mobile.yangkeduo.com/goods.html?goods_id=997025592944"
    fetch_sku_via_search(url)
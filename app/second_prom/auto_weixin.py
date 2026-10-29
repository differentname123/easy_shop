# -- coding: utf-8 --
import io
import os
import time
from PIL import Image
import pyperclip
import uiautomation as auto
import win32clipboard
import win32con
import win32gui

# ================= 固定参数 =================
TARGET_GROUP = "好货分享"  # 目标好友/群聊名称
TEXT_CONTENT = "测试数据"
IMAGE_PATH = r"E:\chrome\1759238147.png"
# ===========================================


def set_clipboard_image(file_path):
    """将本地图片以位图格式塞入剪贴板，支持直接 Ctrl+V 到微信输入框"""
    image = Image.open(file_path)
    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # 剥离 BMP 头 14 字节即为 DIB 格式
    output.close()

    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()


def get_wechat_window():
    """精确兼容微信 4.0 (Qt架构) 与 老版本微信"""
    # 策略 1: 直接匹配你的微信 4.0 (ClassName: Qt51514QWindowIcon, Name: 微信)
    wx_win = auto.WindowControl(
        searchDepth=1, Name="微信", ClassName="Qt51514QWindowIcon"
    )
    if wx_win.Exists(maxSearchSeconds=1):
        return wx_win

    # 策略 2: 仅按窗口标题 "微信" 查找 (兼容以后的 Qt 小版本升级)
    wx_win = auto.WindowControl(searchDepth=1, Name="微信")
    if wx_win.Exists(maxSearchSeconds=1):
        return wx_win

    # 策略 3: 老版微信类名兜底
    wx_win = auto.WindowControl(searchDepth=1, ClassName="WeChatMainWndForPC")
    if wx_win.Exists(maxSearchSeconds=1):
        return wx_win

    return None


def activate_window(window_ctrl):
    """强制将 Qt 微信窗口激活并唤起到前台焦点"""
    hwnd = window_ctrl.NativeWindowHandle

    # 如果窗口处于最小化，先恢复
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

    # 绕过 Windows 前台锁定限制并激活窗口
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # 有时 Windows 限制跨进程抢焦点，按一下 Alt 键即可绕过限制
        auto.SendKeys("{Alt}")
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    window_ctrl.SetActive()
    time.sleep(0.4)


def main():
    # 1. 检查图片是否存在
    if not os.path.exists(IMAGE_PATH):
        print(f"[-] 错误：找不到图片，请检查路径: {IMAGE_PATH}")
        return

    # 2. 定位并唤醒微信窗口
    print("[*] 正在定位微信 4.0 窗口...")
    wx_window = get_wechat_window()

    if not wx_window:
        print("[-] 错误：未检测到微信，请确保微信主界面已打开！")
        return

    print(
        f"[+] 成功捕获微信: [{wx_window.Name}] | ClassName: [{wx_window.ClassName}]"
    )

    # 强制激活窗口到前台
    activate_window(wx_window)

    # 3. 按 Ctrl+F 搜索目标群聊并进入
    print(f"[*] 正在搜索群聊 [{TARGET_GROUP}]...")
    pyperclip.copy(TARGET_GROUP)

    # 触发搜索
    auto.SendKeys("{Ctrl}f")
    time.sleep(0.4)

    # 粘贴搜索文本
    auto.SendKeys("{Ctrl}v")
    # Qt 界面本地搜索渲染需要稍作等待
    time.sleep(1.2)

    # 回车选中进入群聊
    auto.SendKeys("{Enter}")
    time.sleep(0.8)

    # 4. 发送图片
    print("[*] 正在发送图片...")
    set_clipboard_image(IMAGE_PATH)
    time.sleep(0.2)
    auto.SendKeys("{Ctrl}v")
    time.sleep(1.0)  # 等待图片上屏到输入框
    auto.SendKeys("{Enter}")
    time.sleep(0.8)

    # 5. 发送文本
    print("[*] 正在发送文字...")
    pyperclip.copy(TEXT_CONTENT)
    auto.SendKeys("{Ctrl}v")
    time.sleep(0.4)
    auto.SendKeys("{Enter}")

    print("[+] 全部操作执行完成！")


if __name__ == "__main__":
    main()
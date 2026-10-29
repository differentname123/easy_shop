# -- coding: utf-8 --
import ctypes
import os
import time
import numpy as np
from PIL import ImageGrab
import pyperclip
from rapidocr_onnxruntime import RapidOCR
import uiautomation as auto
import win32clipboard
import win32con
import win32gui
import win32process

# ================= 启用 Windows 原生高 DPI 感知 (防止坐标偏移) =================
try:
    # 启用 Per-Monitor DPI Aware (Windows 8.1+)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
# =========================================================================

# ================= 固定测试参数 =================
TARGET_GROUP = "好货分享"  # 目标好友/群聊名称
TEXT_CONTENT = "测试数据"
IMAGE_LIST = [r"E:\chrome\1759238147.png"]
# ===============================================

ocr = RapidOCR()


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", ctypes.c_uint32),
        ("pt_x", ctypes.c_int32),
        ("pt_y", ctypes.c_int32),
        ("fNC", ctypes.c_int32),
        ("fWide", ctypes.c_int32),
    ]


def set_clipboard_files(file_paths):
    """通过 CF_HDROP 将文件列表写入剪贴板（原生文件传输，杜绝 PNG 黑底）"""
    valid_paths = [os.path.abspath(p) for p in file_paths if os.path.exists(p)]
    if not valid_paths:
        return False

    files_str = "\0".join(valid_paths) + "\0\0"
    encoded_data = files_str.encode("utf-16le")

    df = DROPFILES()
    df.pFiles = ctypes.sizeof(DROPFILES)
    df.fWide = 1
    payload = bytes(df) + encoded_data

    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, payload)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.1)
    return False


def get_wechat_main_window():
    """动态捕获微信主窗口句柄（兼容 Qt 4.0 与旧版）"""
    candidates = []

    def enum_windows_proc(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        cls_name = win32gui.GetClassName(hwnd)

        if (
            cls_name.startswith("Qt5")
            or cls_name.startswith("Qt6")
            or cls_name == "WeChatMainWndForPC"
            or title == "微信"
        ):
            try:
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w > 500 and h > 400:
                    ctrl = auto.ControlFromHandle(hwnd)
                    candidates.append((hwnd, ctrl))
            except Exception:
                pass
        return True

    win32gui.EnumWindows(enum_windows_proc, None)
    return candidates[0] if candidates else (None, None)


def activate_window(hwnd, window_ctrl):
    """将窗口强行提升至当前活动焦点"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        auto.SendKeys("{Alt}")
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    if window_ctrl:
        window_ctrl.SetActive()
    time.sleep(0.25)


def get_window_dynamic_roi(hwnd, roi_type="search"):
    """精准适配微信 4.0 Qt 界面的局部视口计算"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w = right - left
    h = bottom - top

    if roi_type == "search":
        # 搜索下拉列表（左侧导航条宽约 60px，列表宽约 320px）
        x1 = left + 65
        y1 = top + 90
        x2 = min(left + 380, right - 200)
        y2 = min(top + 350, bottom - 100)

    elif roi_type == "header":
        # 右侧聊天标题栏（从列表分隔线 370px 处起，纵深下沉至 45px~130px 之间）
        x1 = left + 360
        y1 = top + 45
        x2 = min(left + 820, right - 120)
        y2 = min(top + 130, bottom - 100)

    else:
        x1, y1, x2, y2 = left, top, right, bottom

    x1, x2 = max(0, x1), max(x1 + 10, x2)
    y1, y2 = max(0, y1), max(y1 + 10, y2)

    screenshot = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
    return screenshot, np.array(screenshot)


def verify_text_with_ocr(hwnd, roi_type, expected_text, timeout=2.0):
    """带调试截图输出的 OCR 轮询校验"""
    start_time = time.time()
    clean_target = expected_text.strip()
    last_img = None
    last_detected = ""

    while time.time() - start_time < timeout:
        pil_img, img_np = get_window_dynamic_roi(hwnd, roi_type)
        last_img = pil_img
        result, _ = ocr(img_np)

        if result:
            detected_lines = [
                line[1].strip() for line in result if len(line) >= 2
            ]
            last_detected = "".join(detected_lines)

            # 支持包含匹配（自动兼容类似 '好货分享(2)' 中的群人数后缀）
            if clean_target in last_detected or any(
                clean_target in line for line in detected_lines
            ):
                return True, last_detected

        time.sleep(0.2)

    # 若最终未匹配，保存当时的局部截图供排查
    debug_filename = f"debug_{roi_type}_failed.png"
    if last_img:
        last_img.save(debug_filename)
        print(f"[!] 校验未命中，已保存现场截屏至: {os.path.abspath(debug_filename)}")

    return False, last_detected


def locate_and_enter_chat_safe(hwnd, wx_ctrl, target_group):
    """结合 OCR 安全拦截的会话导航流程"""
    activate_window(hwnd, wx_ctrl)

    print(f"[*] 正在检索目标: [{target_group}]...")
    # 唤醒搜索框并清空
    auto.SendKeys("{Ctrl}f")
    time.sleep(0.15)
    auto.SendKeys("{Ctrl}a{Delete}")
    time.sleep(0.15)

    pyperclip.copy(target_group)
    auto.SendKeys("{Ctrl}v")
    time.sleep(0.5)  # 缓冲搜索结果渲染

    # 1. 校验搜索列表
    matched, detected = verify_text_with_ocr(
        hwnd, roi_type="search", expected_text=target_group, timeout=2.0
    )
    if not matched:
        print(
            f"[-] 安全阻断：搜索下拉列表中未匹配到 [{target_group}]（识别到: '{detected}'），防止误发！"
        )
        auto.SendKeys("{Esc}")
        return False

    print(f"[+] 搜索列表校验通过 (命中: {detected})，回车进入会话...")
    auto.SendKeys("{Enter}")
    time.sleep(0.6)  # 留足时间让会话 Header 和聊天记录完成渲染

    # 2. 校验顶部 Header 标题
    header_matched, header_text = verify_text_with_ocr(
        hwnd, roi_type="header", expected_text=target_group, timeout=2.0
    )
    if not header_matched:
        print(
            f"[-] 安全阻断：会话进入异常！顶部 Header 识别为 '{header_text}'，与目标不一致！"
        )
        return False

    print(f"[+] 目标会话确认就绪: [{target_group}] (Header: {header_text})")

    # 激活并聚焦输入框
    edit_box = wx_ctrl.EditControl(searchDepth=8)
    if edit_box.Exists(maxSearchSeconds=0.5):
        edit_box.Click(simulateMove=False)
        time.sleep(0.15)

    return True


def send_wechat_payload(
    target_group, text_content=None, image_list=None, batch_images=True
):
    if not target_group:
        print("[-] 目标群名不能为空")
        return False

    print("[*] 正在捕获微信主窗口...")
    hwnd, wx_ctrl = get_wechat_main_window()
    if not hwnd:
        print("[-] 未能找到微信主界面，请确认微信已启动登录！")
        return False

    rect = win32gui.GetWindowRect(hwnd)
    print(
        f"[+] 成功锁定微信 (HWND: {hwnd}) | 坐标: ({rect[0]}, {rect[1]}) 宽高: {rect[2]-rect[0]}x{rect[3]-rect[1]}"
    )

    if not locate_and_enter_chat_safe(hwnd, wx_ctrl, target_group):
        print(f"[-] 进入目标群聊 [{target_group}] 失败，流程中断。")
        return False

    # 1. 发送图片（保留透明度通道）
    if image_list:
        valid_images = [p for p in image_list if os.path.exists(p)]
        if not valid_images:
            print("[-] 本地未找到有效图片路径")
        elif batch_images:
            print(f"[*] 批量导入 {len(valid_images)} 张图片至剪贴板...")
            if set_clipboard_files(valid_images):
                time.sleep(0.1)
                auto.SendKeys("{Ctrl}v")
                time.sleep(0.8)
                auto.SendKeys("{Enter}")
                time.sleep(0.4)
                print("[+] 图片批量发送完成")
            else:
                print("[-] 图片写入剪贴板失败")
        else:
            for idx, img in enumerate(valid_images, start=1):
                print(f"[*] 发送第 {idx}/{len(valid_images)} 张图片: {img}")
                if set_clipboard_files([img]):
                    time.sleep(0.1)
                    auto.SendKeys("{Ctrl}v")
                    time.sleep(0.6)
                    auto.SendKeys("{Enter}")
                    time.sleep(0.4)

    # 2. 发送文本
    if text_content:
        print("[*] 正在输入并发送文本...")
        pyperclip.copy(text_content)
        time.sleep(0.1)
        auto.SendKeys("{Ctrl}v")
        time.sleep(0.2)
        auto.SendKeys("{Enter}")
        time.sleep(0.3)
        print("[+] 文本发送完成")

    print(f"[+] 任务结束：已向 [{target_group}] 完成全部投递。")
    return True


if __name__ == "__main__":
    send_wechat_payload(
        target_group=TARGET_GROUP,
        text_content=TEXT_CONTENT,
        image_list=IMAGE_LIST,
        batch_images=True,
    )
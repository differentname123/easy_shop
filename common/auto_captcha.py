import ctypes

# 强行开启进程 DPI 感知，防止屏幕 125%/150% 缩放导致坐标错位
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import os
import uiautomation as auto
import time
import cv2
import numpy as np
import win32api
import win32con
import win32gui
import random
from mss import mss

sct = mss()


def llm_recognize_best_match(image_paths):
    """
    【预留位置】多模态大模型统一识别函数
    :param image_paths: 获取到的所有20张图片路径列表
    :return: 最佳匹配的图片索引 (0 到 len-1)
    """
    print(f"⏳ 模拟多模态大模型统一分析中... (共 {len(image_paths)} 张图片，等待 20 秒)")
    time.sleep(20)

    # 模拟逻辑：直接认定中间的那张（例如20张里的第10张，索引为9）为最佳匹配
    best_index = (len(image_paths) - 1) // 2

    best_path = image_paths[best_index]
    print(f"✅ 大模型返回结果：第 {best_index + 1} 张图片 ({best_path}) 最匹配！")
    return best_index


def force_bring_to_front(hwnd):
    if not hwnd: return
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception as e:
        print(f"[WARN] 置顶失败: {e}")


def test_pdd_captcha():
    print("=" * 50)
    print("🚀 开始测试拼多多滑块验证码解锁 (均匀截图+统一识别模式)")
    print("=" * 50)

    window = auto.WindowControl(searchDepth=1, Name="拼多多")
    if not window.Exists(3, 1):
        print("❌ 未找到名为【拼多多】的窗口，请确保小程序已打开。")
        return

    hwnd = window.NativeWindowHandle
    force_bring_to_front(hwnd)
    time.sleep(0.5)

    title_ctrl = window.TextControl(Name="拼多多安全验证")
    slider_text_ctrl = window.TextControl(Name="拖动滑块完成上方拼图")

    if not (title_ctrl.Exists(2, 1) and slider_text_ctrl.Exists(2, 1)):
        print("❌ 无法精确定位验证码文本。请确认当前处于验证码界面。")
        return

    t_rect = title_ctrl.BoundingRectangle

    # 向上遍历父节点，直到找到包裹整个滑块+文字的灰色外框容器
    track_ctrl = slider_text_ctrl.GetParentControl()
    track_rect = track_ctrl.BoundingRectangle

    # 只要容器宽度不够大(比如小于180像素)，就继续向外找
    while (track_rect.right - track_rect.left) < 180:
        track_ctrl = track_ctrl.GetParentControl()
        if not track_ctrl:
            break
        track_rect = track_ctrl.BoundingRectangle

    img_left = track_rect.left
    img_top = t_rect.bottom
    img_width = track_rect.right - track_rect.left
    img_height = track_rect.top - t_rect.bottom

    if img_width <= 0 or img_height <= 0:
        print("❌ 提取的区域尺寸异常，计算失败。")
        return

    print(f"✅ 精准定位图片区域: 宽={img_width}, 高={img_height}")

    monitor = {"left": int(img_left), "top": int(img_top), "width": int(img_width), "height": int(img_height)}

    # ==================== 核心逻辑：均匀截图 + 统一大模型识别 ====================
    num_steps = 10  # 默认参数，均匀分
    # 计算总滑动距离：轨道总宽度减去滑块按钮自身的宽度(估算约50像素)
    total_distance = (track_rect.right - track_rect.left) - 50
    step_size = total_distance / num_steps

    if not os.path.exists('temp_data'):
        os.makedirs('temp_data')

    # 起始点修正：以整个轨道的左边缘向右偏移约25像素（按钮的正中心）
    start_x = track_rect.left + 25
    start_y = (track_rect.top + track_rect.bottom) // 2

    print("🤖 开始执行底层物理鼠标拖拽 (均匀截图模式)...")
    win32api.SetCursorPos((int(start_x), int(start_y)))
    time.sleep(0.5)

    # 【关键】按下鼠标左键，此后整个截图和识别期间绝不松手，防止验证码背景重置
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.2)

    current_x, current_y = float(start_x), float(start_y)
    image_paths = []
    step_positions = []  # 记录每一步的X坐标，用于大模型识别后的位置微调

    for step in range(1, num_steps + 1):
        # 1. 均匀滑动到当前步
        current_x += step_size
        current_y += random.uniform(-1.0, 1.0)  # 加一点Y轴微扰，模拟人手
        win32api.SetCursorPos((int(current_x), int(current_y)))

        # 2. 滑动后保持，稍微停顿一下再截图，确保画面渲染完成
        time.sleep(random.uniform(0.05, 0.1))

        # 3. 截图并保存到 temp_data
        sct_img = sct.grab(monitor)
        step_img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
        img_path = f"temp_data/step_{step}.png"
        cv2.imwrite(img_path, step_img_cv)
        image_paths.append(img_path)
        step_positions.append(int(current_x))

        print(f"📸 第 {step}/{num_steps} 步截图已保存: {img_path}")

    print("\n✅ 20张截图全部获取完毕，鼠标保持按压状态。")
    print("🔄 准备统一调用多模态大模型进行识别...")

    # 4. 统一调用大模型识别函数 (识别期间鼠标一直保持按下状态)
    best_index = llm_recognize_best_match(image_paths)

    # 5. 根据大模型返回的结果，将滑块微调至最佳匹配的位置
    target_x = step_positions[best_index]
    print(f"\n🎯 大模型指示最佳位置为第 {best_index + 1} 步，准备将滑块微调至 X={target_x}...")

    # 移动鼠标到目标位置 (由于是按住的，这里相当于把滑块拖动回正确的位置)
    win32api.SetCursorPos((target_x, int(current_y)))
    time.sleep(0.5)  # 稍微停顿一下再松手，显得更真实

    # 6. 识别并调整完毕，松开鼠标
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print("🏁 鼠标已松开，验证流程结束。")
    # ====================================================================


if __name__ == "__main__":
    test_pdd_captcha()
import ctypes

from common.model_api import generate_content

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

import re
import os
import time


def llm_recognize_best_match(image_paths):
    """
    【预留位置】多模态大模型统一识别函数
    输入滑块验证码逐帧截图，识别拼图与缺口对齐最佳的帧
    :param image_paths: list - 所有截图的文件路径列表，按滑动顺序排列
    :param debug_mode: bool - 调试模式，开启后不调用真实大模型，等待20秒后返回中间位置模拟结果
    :return: int - 最佳匹配的图片索引（0 到 len(image_paths)-1）
    """
    total = len(image_paths)
    # ---------- 前置输入校验 ----------
    if not isinstance(image_paths, list) or total == 0:
        print("[ERROR] 图片路径列表为空或格式非法，返回默认索引0")
        return 0
    if total == 1:
        print("[INFO] 仅1张图片，直接返回索引0")
        return 0

    # ---------- 兜底默认值（中间位置，兼容原模拟逻辑） ----------
    fallback_index = (total - 1) // 2

    # ---------- 优化提示词：强约束输出格式，降低解析难度 ----------
    prompt = (
        "这些图片是滑块验证码滑动过程中的逐帧截图，拼图在左侧，缺口在右侧。"
        "请找出拼图块与缺口完全对齐、匹配度最高的那一张图片。"
        "**严格要求**：只返回对应图片的完整文件名（例如 step_05.png），"
        "禁止输出任何解释、说明、序号、标点或其他多余内容。"
    )

    max_retries = 2  # 最大重试次数，应对偶发接口故障
    best_index = None

    # ---------- 带重试的大模型调用 + 全异常捕获 ----------
    for attempt in range(max_retries + 1):
        try:
            print(f"[INFO] 第 {attempt + 1} 次调用多模态大模型识别...")
            result = generate_content(
                prompt=prompt,
                model="gemini-web-3.6-flash-thinking-max",
                file_paths=image_paths
            )
            result = result.get("content")
            # 空结果/非字符串结果校验
            if not result or not isinstance(result, str):
                print(f"[WARN] 第 {attempt + 1} 次返回结果为空或格式错误")
                time.sleep(0.5)
                continue

            result_text = result.strip()
            text_lower = result_text.lower()

            # ---------- 多层鲁棒解析 ----------
            # 第1层：匹配文件名（支持带/不带后缀、不区分大小写）
            for idx, path in enumerate(image_paths):
                filename = os.path.basename(path)
                name_no_ext = os.path.splitext(filename)[0]
                if filename.lower() in text_lower or name_no_ext.lower() in text_lower:
                    best_index = idx
                    break

            # 第2层：提取数字序号（兼容返回「第X张」「X号」等不规范格式）
            if best_index is None:
                num_matches = re.findall(r'\d+', result_text)
                for num_str in num_matches:
                    num = int(num_str)
                    if 1 <= num <= total:
                        best_index = num - 1  # 转为0-based索引
                        break

            # 解析成功直接跳出重试
            if best_index is not None:
                break
            else:
                print(f"[WARN] 第 {attempt + 1} 次返回结果无法解析：{result_text[:50]}...")
                time.sleep(0.5)

        except Exception as e:
            print(f"[ERROR] 第 {attempt + 1} 次大模型调用异常：{str(e)}")
            time.sleep(1)  # 异常重试间隔加长，避开限流/波动
            continue

    # ---------- 最终结果 & 降级兜底 ----------
    if best_index is not None:
        best_path = image_paths[best_index]
        print(f"✅ 大模型识别成功：第 {best_index + 1} 张图片 ({os.path.basename(best_path)}) 匹配度最高")
    else:
        best_index = fallback_index
        best_path = image_paths[best_index]
        print(
            f"[WARN] 大模型识别/解析全部失败，启用兜底策略：默认中间位置第 {best_index + 1} 张 ({os.path.basename(best_path)})")

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


def solve_pdd_captcha(max_retries=5):
    print("=" * 50)
    print(f"🚀 开始测试拼多多滑块验证码解锁 (最大重试次数: {max_retries})")
    print("=" * 50)

    for attempt in range(1, max_retries + 1):
        print(f"\n--- 第 {attempt}/{max_retries} 次尝试 ---")

        # 每次重试都需要重新获取窗口和控件，防止验证失败后拼多多重置界面导致句柄失效
        window = auto.WindowControl(searchDepth=1, Name="拼多多")
        if not window.Exists(3, 1):
            print("❌ 未找到名为【拼多多】的窗口，请确保小程序已打开。")
            return False

        hwnd = window.NativeWindowHandle
        force_bring_to_front(hwnd)
        time.sleep(0.5)

        title_ctrl = window.TextControl(Name="拼多多安全验证")
        slider_text_ctrl = window.TextControl(Name="拖动滑块完成上方拼图")

        if not (title_ctrl.Exists(2, 1) and slider_text_ctrl.Exists(2, 1)):
            print("❌ 无法精确定位验证码文本。请确认当前处于验证码界面。")
            return False

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
            return False

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

        print("🔄 准备统一调用多模态大模型进行识别...")

        # 4. 统一调用大模型识别函数 (识别期间鼠标一直保持按下状态)
        best_index = llm_recognize_best_match(image_paths)

        # 5. 根据大模型返回的结果，将滑块微调至最佳匹配的位置
        target_x = step_positions[best_index]
        print(f"\n🎯 大模型指示最佳位置为第 {best_index + 1} 步，准备将滑块微调至 X={target_x}...")

        # ==================== 【修复1】焦点抢占修复 ====================
        force_bring_to_front(hwnd)
        time.sleep(0.3)  # 给窗口一点时间重新获取焦点和渲染

        # ==================== 【修复2】稳健拟人化微调 ====================
        phys_x, phys_y = win32api.GetCursorPos()

        if abs(phys_x - current_x) > 20:
            print(f"⚠️ 检测到用户手动移动过鼠标(当前X:{phys_x})，先平滑移回记录位置(X:{int(current_x)})")
            steps_back = 15
            dx_back = (current_x - phys_x) / steps_back
            curr_x = float(phys_x)
            for i in range(steps_back):
                curr_x += dx_back
                win32api.SetCursorPos((int(curr_x), int(current_y + random.uniform(-1.0, 1.0))))
                time.sleep(random.uniform(0.01, 0.03))
            time.sleep(0.1)

        print(f"🎢 开始拟人化平滑微调，从 X={int(current_x)} 移动到 X={target_x}...")
        steps_forward = 25
        dx_forward = (target_x - current_x) / steps_forward
        curr_x = float(current_x)

        for i in range(steps_forward):
            curr_x += dx_forward
            # 加入 Y 轴微扰和随机时间间隔，高度模拟人手拖拽
            y_offset = random.uniform(-1.5, 1.5)
            win32api.SetCursorPos((int(curr_x), int(current_y + y_offset)))
            time.sleep(random.uniform(0.01, 0.04))

        # 最后确保精准落在 target_x，消除浮点数累积误差
        win32api.SetCursorPos((target_x, int(current_y)))
        time.sleep(random.uniform(0.2, 0.4))  # 稍微停顿一下再松手，显得更真实

        # 6. 识别并调整完毕，松开鼠标
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        print("🏁 鼠标已松开，等待服务器响应与结果判定...")
        # ====================================================================

        # ==================== 新增：滑动校验结果检测 ====================
        time.sleep(1.5)  # 等待网络请求及页面动画反馈

        check_window = auto.WindowControl(searchDepth=1, Name="拼多多")
        # 如果整个拼多多窗口消失（比如验证成功后跳转/关闭），视为通过
        if not check_window.Exists(1, 0.5):
            print("✅ 验证通过（窗口已关闭或跳转）！")
            return True

        # 检测核心校验文本是否还在
        check_slider_text = check_window.TextControl(Name="拖动滑块完成上方拼图")
        if not check_slider_text.Exists(1, 0.5):
            print("✅ 滑动校验文本已消失，验证通过！")
            return True
        else:
            print(f"❌ 第 {attempt} 次尝试失败，滑动校验依然存在。")
            if attempt < max_retries:
                print("⏳ 准备进行下一次重试...")
                time.sleep(1.0)  # 稍作停顿再开始下一轮
            else:
                print("❌ 已达到最大重试次数，验证失败。")
                return False

    return False


if __name__ == "__main__":
    solve_pdd_captcha(max_retries=5)
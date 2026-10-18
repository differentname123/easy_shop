import ctypes
from common.model_api import generate_content

# 强行开启进程 DPI 感知，防止屏幕 125%/150% 缩放导致坐标错位
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import os
import time
import cv2
import numpy as np
import win32api
import win32con
import win32gui
import random
from mss import mss
import re
from rapidocr_onnxruntime import RapidOCR

sct = mss()
# 全局初始化OCR引擎，避免重复加载
ocr_engine = RapidOCR()


def ocr_detect_region(monitor):
    """
    对指定屏幕区域做OCR识别
    :param monitor: 截图区域字典 {"left", "top", "width", "height"} 屏幕绝对坐标
    :return: tuple - (识别结果列表, 截图的cv2图像)
    """
    sct_img = sct.grab(monitor)
    img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
    result, _ = ocr_engine(img_cv)
    if not result:
        return [], img_cv
    return result, img_cv


def find_text_position(ocr_results, target_text, fuzzy_match=True):
    """
    从OCR结果中查找指定文本的中心坐标
    :param ocr_results: OCR识别结果列表
    :param target_text: 目标文本关键词
    :param fuzzy_match: 是否模糊匹配（包含关键词即匹配）
    :return: (center_x, center_y, bbox) 相对截图区域坐标 或 None
    """
    for line in ocr_results:
        bbox = line[0]
        text = line[1]
        if fuzzy_match:
            if target_text in text:
                # 计算外接矩形和中心点
                x_list = [p[0] for p in bbox]
                y_list = [p[1] for p in bbox]
                left, right = min(x_list), max(x_list)
                top, bottom = min(y_list), max(y_list)
                center_x = (left + right) // 2
                center_y = (top + bottom) // 2
                return int(center_x), int(center_y), (int(left), int(top), int(right), int(bottom))
        else:
            if text == target_text:
                x_list = [p[0] for p in bbox]
                y_list = [p[1] for p in bbox]
                left, right = min(x_list), max(x_list)
                top, bottom = min(y_list), max(y_list)
                center_x = (left + right) // 2
                center_y = (top + bottom) // 2
                return int(center_x), int(center_y), (int(left), int(top), int(right), int(bottom))
    return None


def save_debug_image(img_cv, ocr_results, title_bbox, track_bbox, start_x_rel, start_y_rel):
    """
    将识别结果和计算出的点击位置画在图片上，用于排查未点中滑块的问题
    """
    debug_img = img_cv.copy()

    # 1. 画出所有 OCR 识别到的文字框 (蓝色)
    for line in ocr_results:
        bbox = line[0]
        text = line[1]
        pts = np.array(bbox, np.int32)
        cv2.polylines(debug_img, [pts], True, (255, 0, 0), 2)
        # cv2.putText 默认不支持中文，此处中文可能会显示为方块，但不影响排查框的位置
        cv2.putText(debug_img, text, (int(bbox[0][0]), int(bbox[0][1]) - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0),
                    2)

    # 2. 画出"安全验证"标题框 (绿色)
    if title_bbox:
        l, t, r, b = title_bbox
        cv2.rectangle(debug_img, (l, t), (r, b), (0, 255, 0), 2)

    # 3. 画出"向右滑动"轨道文字框 (青色)
    if track_bbox:
        l, t, r, b = track_bbox
        cv2.rectangle(debug_img, (l, t), (r, b), (255, 255, 0), 2)

    # 4. 画出鼠标实际点击的起始点 (黄色实心圆) -> 核心排查点！
    if start_x_rel is not None and start_y_rel is not None:
        cv2.circle(debug_img, (int(start_x_rel), int(start_y_rel)), 15, (0, 255, 255), -1)
        cv2.putText(debug_img, "Click Here", (int(start_x_rel) + 20, int(start_y_rel)), cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (0, 255, 255), 2)

    debug_path = "debug_slider_detection.png"
    cv2.imwrite(debug_path, debug_img)
    print(f"✅ 已保存可视化调试图片: {debug_path} (请查看黄点是否准确落在滑块按钮上)")



def locate_slider_by_ocr(window_rect):
    """
    【改造2】通过OCR定位滑块轨道与按钮位置
    1. 固定起点：以包含"滑动"关键字的OCR区域左侧、同一行的那个OCR区域（即"》"滑块按钮）的中心为准
    2. 截图区域：除开窗口最上方1/6和最下方1/6的区域后，剩下所有识别内容的最小包围框
    3. 滑块长度：由起点到该最小包围框右侧（调用方用 img_monitor 右边缘计算）
    :param window_rect: 拼多多窗口坐标 (left, top, right, bottom) 屏幕绝对坐标
    :return: (img_monitor, track_rect, start_x, start_y) 全部为屏幕绝对坐标 或 None
    """
    win_left, win_top, win_right, win_bottom = window_rect
    window_monitor = {
        "left": int(win_left),
        "top": int(win_top),
        "width": int(win_right - win_left),
        "height": int(win_bottom - win_top)
    }
    ocr_results, img_cv = ocr_detect_region(window_monitor)

    # 1. 找包含"滑动"关键字的滑块提示文本（兼容"向右滑动"/"拖动滑块"等文案）
    track_pos = find_text_position(ocr_results, "滑动")
    if not track_pos:
        print("[ERROR] OCR未识别到包含'滑动'关键字的滑块提示文本")
        return None

    _, track_center_y_rel, track_bbox_rel = track_pos
    track_center_y = track_center_y_rel + win_top  # 转屏幕绝对坐标
    track_left = track_bbox_rel[0] + win_left
    track_top = track_bbox_rel[1] + win_top
    track_right = track_bbox_rel[2] + win_left
    track_bottom = track_bbox_rel[3] + win_top

    # 2. 固定起点：取"滑动"区域左侧、且垂直方向与其重叠(同一行)的OCR区域(即"》"按钮图标)，
    #    若有多个取最靠近"滑动"区域的一个，以其中心作为拖拽起点
    start_pos_rel = None
    for line in ocr_results:
        bbox = line[0]
        x_list = [p[0] for p in bbox]
        y_list = [p[1] for p in bbox]
        left, right = min(x_list), max(x_list)
        top, bottom = min(y_list), max(y_list)
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        # 条件：整个区域在"滑动"区域左边，且与"滑动"区域垂直重叠（同一行）
        if right <= track_bbox_rel[0] and top < track_bbox_rel[3] and bottom > track_bbox_rel[1]:
            if start_pos_rel is None or center_x > start_pos_rel[0]:
                start_pos_rel = (center_x, center_y)
    if not start_pos_rel:
        print("[ERROR] OCR未识别到'滑动'区域左侧的滑块按钮区域('》')")
        return None
    start_x = start_pos_rel[0] + win_left  # 转屏幕绝对坐标
    start_y = start_pos_rel[1] + win_top

    # 3. 截图区域：除开窗口最上方1/6和最下方1/6的区域后，剩下所有识别内容的最小包围框
    win_height = win_bottom - win_top
    top_cut = win_height / 6.0  # 最上方1/6分界线（相对坐标）
    bottom_cut = win_height * 5.0 / 6.0  # 最下方1/6分界线（相对坐标）
    rects = []
    for line in ocr_results:
        x_list = [p[0] for p in line[0]]
        y_list = [p[1] for p in line[0]]
        left, right = min(x_list), max(x_list)
        top, bottom = min(y_list), max(y_list)
        center_y = (top + bottom) / 2.0
        # 仅保留中心点落在中间4/6区域内的识别内容
        if top_cut <= center_y <= bottom_cut:
            rects.append((top, bottom, left, right))
    if not rects:
        print("[ERROR] 除开最上和最下1/6区域后无有效识别内容")
        return None
    content_left_rel = min(r[2] for r in rects)
    content_top_rel = min(r[0] for r in rects)
    content_right_rel = max(r[3] for r in rects)
    content_bottom_rel = max(r[1] for r in rects)

    # 4. 计算验证码图片截图区域（屏幕绝对坐标），即上述最小包围框
    img_monitor = {
        "left": int(content_left_rel + win_left),
        "top": int(content_top_rel + win_top),
        "width": int(content_right_rel - content_left_rel),
        "height": int(content_bottom_rel - content_top_rel)
    }

    # 轨道完整区域（屏幕绝对坐标）
    track_rect = (track_left, track_top, track_right, track_bottom)

    # 触发保存调试图片
    save_debug_image(
        img_cv=img_cv,
        ocr_results=ocr_results,
        title_bbox=None,
        track_bbox=track_bbox_rel,
        start_x_rel=start_pos_rel[0],
        start_y_rel=start_pos_rel[1]
    )

    print(f"✅ OCR定位成功：图片区域{img_monitor}, 轨道区域{track_rect}, 滑块起始点({start_x},{start_y})")
    return img_monitor, track_rect, start_x, start_y


def llm_recognize_best_match(image_paths):
    """
    多模态大模型统一识别函数
    输入滑块验证码逐帧截图，识别拼图与缺口对齐最佳的帧
    :param image_paths: list - 所有截图的文件路径列表，按滑动顺序排列
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
    """强制窗口置顶并获取焦点"""
    if not hwnd:
        return
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


def get_pdd_window_rect():
    """获取拼多多窗口的坐标矩形（屏幕绝对坐标）"""
    hwnd = win32gui.FindWindow(None, "拼多多")
    if not hwnd:
        return None, None
    rect = win32gui.GetWindowRect(hwnd)
    return hwnd, rect


def llm_recognize_math_captcha(image_path):
    """
    调用多模态大模型识别算术验证码图片并计算结果
    :param image_path: str - 算术验证码截图路径
    :return: str - 计算结果字符串（如 "14"），识别失败兜底返回 "0"
    """
    if not os.path.exists(image_path):
        print(f"[ERROR] 算术验证码图片不存在: {image_path}")
        return "0"

    prompt = (
        "这是一张拼多多安全验证的算术验证码截图。"
        "请忽略背景中浅色、细小的干扰字母和数字，只识别前景中字体最大、颜色最深的数学算式（例如 '2 * 7 = ?'）。"
        "注意运算符可能是加(+)、减(-)、乘(*或×)、除(/或÷)。"
        "请直接计算出该算式的最终结果。"
        "**严格要求**：只输出最终计算结果的纯数字（例如 14），禁止输出算式过程、解释、标点符号或任何其他多余文字。"
    )

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            print(f"[INFO] 第 {attempt + 1} 次调用大模型识别算术验证码...")
            result = generate_content(
                prompt=prompt,
                model="gemini-web-3.6-flash-thinking-max",
                file_paths=[image_path]
            )
            content = result.get("content") if isinstance(result, dict) else str(result)
            if not content or not isinstance(content, str):
                print(f"[WARN] 第 {attempt + 1} 次算术识别返回结果为空")
                time.sleep(0.5)
                continue

            result_text = content.strip()
            # 提取所有整数（支持负数），取最后一个数字（防止模型连同算式一起输出如 '2 * 7 = 14'）
            num_matches = re.findall(r'-?\d+', result_text)
            if num_matches:
                calc_answer = num_matches[-1]
                print(f"✅ 大模型算术验证码识别成功，计算结果: {calc_answer} (原始输出: {result_text})")
                return calc_answer
            else:
                print(f"[WARN] 第 {attempt + 1} 次返回结果未包含有效数字: {result_text}")
                time.sleep(0.5)

        except Exception as e:
            print(f"[ERROR] 第 {attempt + 1} 次大模型算术识别调用异常: {str(e)}")
            time.sleep(1.0)

    print("[WARN] 大模型算术识别全部失败，兜底返回 '0'")
    return "0"


def check_captcha_exist(window_rect):
    """
    【改造1】通过OCR判断验证码是否存在：
    若遇到算术验证码("请输入计算结果" / "验证不成功，请重试")，
    则自动截图保存 -> 调用大模型识别算术结果 -> 点击输入框 -> 输入结果 -> 点击确认，
    使其切换至滑动验证界面。
    :param window_rect: 拼多多窗口的坐标 (left, top, right, bottom) 屏幕绝对坐标
    :return: bool
    """
    left, top, right, bottom = window_rect
    monitor = {
        "left": int(left),
        "top": int(top),
        "width": int(right - left),
        "height": int(bottom - top)
    }

    if not os.path.exists('temp_data'):
        os.makedirs('temp_data')

    max_calc_attempts = 3
    cached_input_rel_pos = None  # 缓存输入框相对坐标，防止重试时占位符消失无法定位

    for calc_attempt in range(max_calc_attempts):
        ocr_results, img_cv = ocr_detect_region(monitor)
        ocr_texts = [line[1] for line in ocr_results]
        print(f"[INFO] OCR识别结果: {ocr_texts}")

        # 1. 检测是否存在"安全验证"弹窗
        pos = find_text_position(ocr_results, "安全验证")
        if pos is None:
            return False

        # 2. 如果已经出现"滑动"关键字，说明已成功进入滑动验证界面，直接放行
        if find_text_position(ocr_results, "滑动") is not None:
            print("✅ 已检测到滑块验证界面，准备开始滑动验证...")
            return True

        # 3. 检查是否处于算术验证阶段（兼容首次"请输入计算结果"及重试时的"验证不成功"/"请重试"）
        input_pos = find_text_position(ocr_results, "请输入计算结果")
        retry_pos = find_text_position(ocr_results, "验证不成功") or find_text_position(ocr_results, "请重试")
        confirm_pos = find_text_position(ocr_results, "确认") or find_text_position(ocr_results, "确定")

        if input_pos is not None or retry_pos is not None or confirm_pos is not None:
            print(f"🔢 检测到算术验证界面，开始执行第 {calc_attempt + 1}/{max_calc_attempts} 次识别与输入...")

            # (1) 截图保存
            math_img_path = f"temp_data/math_captcha_{calc_attempt + 1}.png"
            cv2.imwrite(math_img_path, img_cv)
            print(f"📸 算术验证码截图已保存: {math_img_path}")

            # (2) 调用大模型识别计算结果
            calc_answer = llm_recognize_math_captcha(math_img_path)

            # (3) 确定输入框点击位置（优先用"请输入计算结果"，其次用缓存坐标或"验证不成功"位置，最后用"确认"按钮上方偏移）
            if input_pos is not None:
                cached_input_rel_pos = (input_pos[0], input_pos[1])
            elif cached_input_rel_pos is None:
                if retry_pos is not None:
                    cached_input_rel_pos = (retry_pos[0], retry_pos[1])
                elif confirm_pos is not None:
                    # 输入框位于"确认"按钮正上方约 65 像素处
                    cached_input_rel_pos = (confirm_pos[0], max(0, confirm_pos[1] - 65))

            if cached_input_rel_pos is None:
                print("[ERROR] 无法定位算术验证码输入框位置")
                return False

            input_x = int(left) + cached_input_rel_pos[0]
            input_y = int(top) + cached_input_rel_pos[1]

            # 点击输入框获取焦点
            win32api.SetCursorPos((input_x, input_y))
            time.sleep(0.2)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.3)

            # (4) 连续按 Backspace 和 Delete 彻底清空可能残留的上次输入内容
            for _ in range(6):
                win32api.keybd_event(win32con.VK_BACK, 0, 0, 0)
                time.sleep(0.02)
                win32api.keybd_event(win32con.VK_BACK, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(win32con.VK_DELETE, 0, 0, 0)
                time.sleep(0.02)
                win32api.keybd_event(win32con.VK_DELETE, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.15)

            # (5) 逐字符输入大模型计算出的结果（支持多位数及负数）
            for ch in str(calc_answer):
                if ch == '-':
                    vk_code = 0xBD  # 主键盘减号/负号 VK_OEM_MINUS
                elif ch.isdigit():
                    vk_code = ord(ch)
                else:
                    continue
                win32api.keybd_event(vk_code, 0, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.08)
            time.sleep(0.3)

            # (6) 查找"确认"或"确定"按钮并点击
            if confirm_pos is not None:
                confirm_x = int(left) + confirm_pos[0]
                confirm_y = int(top) + confirm_pos[1]
                win32api.SetCursorPos((confirm_x, confirm_y))
                time.sleep(0.2)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                print(f"✅ 已输入计算结果 '{calc_answer}' 并点击确认按钮，等待切换至滑动验证...")
                # 等待界面刷新切换为滑块验证码
                time.sleep(1.5)
            else:
                print("[ERROR] 识别到算术验证界面，但未找到'确认'/'确定'按钮")
                return False
        else:
            # 既没有算术验证特征，存在"安全验证"，直接放行交给后续滑块定位
            return True

    # 循环结束后最后确认一次"安全验证"是否依然存在
    ocr_results, _ = ocr_detect_region(monitor)
    return find_text_position(ocr_results, "安全验证") is not None

def solve_pdd_captcha(max_retries=5):
    print("=" * 50)
    print(f"🚀 开始测试拼多多滑块验证码解锁 (最大重试次数: {max_retries})")
    print("=" * 50)

    for attempt in range(1, max_retries + 1):
        print(f"\n--- 第 {attempt}/{max_retries} 次尝试 ---")

        # 1. 获取拼多多窗口
        hwnd, window_rect = get_pdd_window_rect()
        if not hwnd:
            print("❌ 未找到名为【拼多多】的窗口，请确保小程序已打开。")
            return False

        force_bring_to_front(hwnd)
        time.sleep(0.5)

        # 2. 【改造点1】OCR判断验证码是否存在（若为算术验证码会自动输入0切换至滑块）
        if not check_captcha_exist(window_rect):
            print("❌ 未检测到'安全验证'，当前不在验证码界面。")
            return False

        # 3. 【改造点2】OCR定位滑块轨道与图片区域
        locate_result = locate_slider_by_ocr(window_rect)
        if not locate_result:
            print("❌ OCR定位滑块区域失败")
            if attempt < max_retries:
                time.sleep(1)
                continue
            return False

        img_monitor, track_rect, start_x, start_y = locate_result
        track_left, track_top, track_right, track_bottom = track_rect

        # 校验区域尺寸
        if img_monitor["width"] <= 0 or img_monitor["height"] <= 0:
            print("❌ 提取的图片区域尺寸异常")
            return False

        print(f"✅ 验证码图片区域: 宽={img_monitor['width']}, 高={img_monitor['height']}")

        # ==================== 核心逻辑：均匀截图 + 统一大模型识别 ====================
        num_steps = 10
        # 计算总滑动距离：滑块起点 到 最小包围框右侧边缘
        total_distance = (img_monitor["left"] + img_monitor["width"]) - start_x
        step_size = total_distance / num_steps

        if not os.path.exists('temp_data'):
            os.makedirs('temp_data')

        current_x, current_y = float(start_x), float(start_y)
        image_paths = []
        step_positions = []  # 记录每一步的X坐标（屏幕绝对坐标），用于大模型识别后的位置微调

        print("🤖 开始执行底层物理鼠标拖拽 (均匀截图模式)...")
        win32api.SetCursorPos((int(current_x), int(current_y)))
        time.sleep(0.5)

        # 按下鼠标左键，全程不松手
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.2)

        for step in range(1, num_steps + 1):
            # 1. 均匀滑动到当前步
            current_x += step_size
            current_y += random.uniform(-1.0, 1.0)  # Y轴微扰，模拟人手
            win32api.SetCursorPos((int(current_x), int(current_y)))

            # 2. 停顿等待画面渲染
            time.sleep(random.uniform(0.05, 0.1))

            # 3. 截图保存
            sct_img = sct.grab(img_monitor)
            step_img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
            img_path = f"temp_data/step_{step}.png"
            cv2.imwrite(img_path, step_img_cv)
            image_paths.append(img_path)
            step_positions.append(int(current_x))

            print(f"📸 第 {step}/{num_steps} 步截图已保存: {img_path}")

        print("🔄 准备统一调用多模态大模型进行识别...")
        # 4. 大模型识别最佳匹配帧
        best_index = llm_recognize_best_match(image_paths)

        # 5. 微调至最佳位置
        target_x = step_positions[best_index]
        print(f"\n🎯 大模型指示最佳位置为第 {best_index + 1} 步，准备将滑块微调至 X={target_x}...")

        # 焦点修复
        force_bring_to_front(hwnd)
        time.sleep(0.3)

        # 检测鼠标是否被手动移动
        phys_x, phys_y = win32api.GetCursorPos()
        if abs(phys_x - current_x) > 20:
            print("⚠️ 检测到用户手动移动过鼠标，先平滑移回记录位置")
            steps_back = 15
            dx_back = (current_x - phys_x) / steps_back
            curr_x = float(phys_x)
            for i in range(steps_back):
                curr_x += dx_back
                win32api.SetCursorPos((int(curr_x), int(current_y + random.uniform(-1.0, 1.0))))
                time.sleep(random.uniform(0.01, 0.03))
            time.sleep(0.1)

        # 拟人化平滑微调
        print(f"🎢 开始拟人化平滑微调，从 X={int(current_x)} 移动到 X={target_x}...")
        steps_forward = 25
        dx_forward = (target_x - current_x) / steps_forward
        curr_x = float(current_x)

        for i in range(steps_forward):
            curr_x += dx_forward
            y_offset = random.uniform(-1.5, 1.5)
            win32api.SetCursorPos((int(curr_x), int(current_y + y_offset)))
            time.sleep(random.uniform(0.01, 0.04))

        # 最终精准落位
        win32api.SetCursorPos((target_x, int(current_y)))
        time.sleep(random.uniform(0.2, 0.4))

        # 6. 松开鼠标
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        print("🏁 鼠标已松开，等待服务器响应与结果判定...")

        # ==================== 结果校验 ====================
        time.sleep(1.5)

        # 重新获取窗口，判断是否还存在
        check_hwnd, check_rect = get_pdd_window_rect()
        if not check_hwnd:
            print("✅ 验证通过（窗口已关闭或跳转）！")
            return True

        # OCR校验：滑块提示文本或安全验证是否消失
        check_ocr_results, _ = ocr_detect_region({
            "left": int(check_rect[0]),
            "top": int(check_rect[1]),
            "width": int(check_rect[2] - check_rect[0]),
            "height": int(check_rect[3] - check_rect[1])
        })
        track_text_exist = find_text_position(check_ocr_results, "滑动") is not None \
                           or find_text_position(check_ocr_results, "请输入计算结果") is not None

        if not track_text_exist:
            print("✅ 验证提示文本已消失，验证通过！")
            return True
        else:
            print(f"❌ 第 {attempt} 次尝试失败，验证码依然存在。")
            if attempt < max_retries:
                print("⏳ 准备进行下一次重试...")
                time.sleep(1.0)
            else:
                print("❌ 已达到最大重试次数，验证失败。")
                return False

    return False

if __name__ == "__main__":
    solve_pdd_captcha(max_retries=5)
import uiautomation as auto
import pyperclip
import time
import os
import re
import itertools
from datetime import datetime, timedelta, timezone
from contextlib import closing

from common.auto_captcha import solve_pdd_captcha
from common.goods_common_utils import gen_goods_segment_info, setup_logger

logger = setup_logger(app_name="pdd_automation")

from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager
import win32gui
import win32con
import win32process
import win32api

# === 高性能依赖 ===
import numpy as np
import cv2
from mss import mss
from rapidocr_onnxruntime import RapidOCR

from common.common_utils import read_json

# ==========================================
# ⚙️ 全局配置区
# ==========================================
TRANSFER_APP_NAME = "wxid_2lr36gkzn2io22的小程序测试号"
TRANSFER_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\wxid_2lr36gkzn2io22的小程序测试号.lnk"

# 合力汇ID:wx86b334b9c84d4b21
# nana中转：wx06fd7cc205f912f2

TRANSFER_APP_NAME = "合力汇"
TRANSFER_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\合力汇.lnk"
TARGET_APP_NAME = "拼多多"

DEBUG_MODE = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKU_DEBUG_DIR = os.path.join(BASE_DIR, "sku_debug_images")
ERROR_DIR = os.path.join(BASE_DIR, "results_error")

for d in [SKU_DEBUG_DIR, ERROR_DIR]:
    os.makedirs(d, exist_ok=True)

# 实例化电竞级截图抓取器 (全局单例，极速)
sct = mss()

logger.info("[SYSTEM] 正在初始化 RapidOCR 极速引擎与强力引擎...")
# 1. 极速引擎：负责高频轮询与全屏稳定切框，0 粘连、速度快
ocr_fast = RapidOCR()

# 2. 强力引擎：高分辨率 + 高召回率，专门对付红字、微小价格、难缠字体
ocr_strong = RapidOCR(
    params={
        "det_limit_side_len": 2000,
        "det_limit_type": "max",
        "det_db_unclip_ratio": 1.75,
        "det_db_box_thresh": 0.45,
    }
)

# ==========================================
# 🔍 SKU 规格与价格极速提取模块
# ==========================================
def _capture_full_window(window):
    """直接截取小程序完整页面及窗口左上角屏幕坐标"""
    rect = window.BoundingRectangle
    left, top = int(rect.left), int(rect.top)
    w, h = int(rect.right - left), int(rect.bottom - top)
    if w <= 0 or h <= 0:
        raise ValueError("窗口尺寸异常")
    sct_img = sct.grab({"left": left, "top": top, "width": w, "height": h})
    return cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR), left, top


def _run_ocr(img_bgr, engine=ocr_fast):
    """执行 OCR 并返回结构化文本块列表"""
    res, _ = engine(img_bgr, use_cls=False)
    items = []
    for line in (res or []):
        pts, text = line[0], str(line[1]).strip()
        if text:
            x, y, w, h = cv2.boundingRect(np.array(pts, dtype=np.int32))
            items.append({"box": (x, y, w, h), "cx": x + w / 2.0, "cy": y + h / 2.0, "text": text})
    return items


def _is_box_selected(img_bgr, box):
    """通过浅粉底色特征判断单个 SKU 框是否已选中"""
    x, y, w, h = box
    roi = img_bgr[max(0, y):y + h, max(0, x):x + w]
    if roi.size == 0:
        return False
    b, g, r = cv2.split(roi)
    pink = (r >= 244) & (g >= 224) & (g <= 248) & (b >= 224) & (b <= 248) & (
                (r.astype(np.int16) - b.astype(np.int16)) >= 5)
    return float(np.mean(pink)) > 0.30


def _extract_price_fast(img_bgr, price_anchor, save_path=None):
    """先用常规引擎整行快速识别，若失败立即激活强力配置引擎重检（采用老代码切片逻辑，不截断券后价等文字）"""
    h_img, w_img = img_bgr.shape[:2]
    px, py, pw, ph = price_anchor

    # 基础裁剪
    crop = img_bgr[max(0, py - 6):min(h_img, py + ph + 6), max(0, px - 8):int(w_img * 0.85)]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    non_white_cols = np.where(np.min(gray, axis=0) < 235)[0]
    if len(non_white_cols) > 0:
        crop = crop[:, max(0, int(non_white_cols[0]) - 4):min(crop.shape[1], int(non_white_cols[-1]) + 6)]

    crop_2x = cv2.resize(crop, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(crop_2x, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))

    # ==================== 1. 尝试常规极速通道 (ocr_fast) ====================
    res, _ = ocr_fast(padded, use_det=False, use_cls=False)
    if res:
        # 【修改点 1】：兼容 ¥、?、？ 并统一归一化为 ￥
        text = re.sub(r"[¥?？]", "￥", str(res[0][0]).strip())
        if re.match(r"^\d", text):
            text = "￥" + text

        # 还原老代码切片逻辑：保留第一个 ￥ 及其之后所有内容（如 "￥36券后￥34"）
        idx = text.find("￥")
        if idx != -1 and re.search(r"\d", text[idx:]):
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return text[idx:].replace(" ", "").strip()

    # ==================== 2. 【激活强力配置】 (ocr_strong 介入) ====================
    res_strong, _ = ocr_strong(padded, use_det=True, use_cls=False)
    if res_strong:
        # 按水平坐标从左向右排序，防止碎片文本颠倒
        try:
            res_strong.sort(key=lambda line: line[0][0][0])
        except Exception:
            pass
        # 【修改点 2】：强力通道同样将 ¥、?、？ 统一归一化为 ￥
        full_text = "".join([re.sub(r"[¥?？]", "￥", str(line[1]).strip()) for line in res_strong])

        if re.match(r"^\d", full_text):
            full_text = "￥" + full_text

        # 还原老代码切片逻辑
        idx = full_text.find("￥")
        if idx != -1 and re.search(r"\d", full_text[idx:]):
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return full_text[idx:].replace(" ", "").strip()

        # 兜底纯数字
        nums = re.findall(r"\d+\.\d+|\d+", full_text)
        if nums:
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return f"￥{nums[0]}"

    return ""

def _is_box_disabled(img_bgr, box):
    """判断未选中的 SKU 框是否处于缺货/置灰不可选状态（灰底浅灰字，无深色前景像素）"""
    if _is_box_selected(img_bgr, box):
        return False
    x, y, w, h = box
    # 内缩 4 像素避开边框干扰，仅检测内部文字颜色
    roi = img_bgr[max(0, y + 4):y + h - 4, max(0, x + 4):x + w - 4]
    if roi.size == 0:
        return True
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # 正常可选规格文字为深黑色(灰度<150)，缺货规格文字为浅灰(灰度≈204)
    return int(np.sum(gray < 150)) < 10


def _parse_detail_page(img_bgr, min_cat_top=None):
    """验证详情页并一次性解析价格锚点、规格分类、所有 SKU 框及是否完全展开状态"""
    h_img, w_img = img_bgr.shape[:2]
    # 全屏常规扫描依然使用 ocr_fast，保证规格框不粘连
    ocr_items = _run_ocr(img_bgr, engine=ocr_fast)

    confirm_item = next((i for i in ocr_items if "确定" in i["text"] and i["cy"] > h_img * 0.80), None)
    confirm_y = confirm_item["box"][1] if confirm_item else None
    confirm_h = confirm_item["box"][3] if confirm_item else None

    # 1. 常规寻找价格
    price_boxes = [
        i["box"] for i in ocr_items
        if ("￥" in i["text"] or "¥" in i["text"] or bool(re.search(r"[?？]\s*\d", i["text"])))
           and (h_img * 0.18 < i["cy"] < h_img * 0.55)
    ]

    # 2. 【核心隔离唤醒】：如果连确定按钮都在，但唯独 price_boxes 瞎了，让 ocr_strong 介入
    if confirm_y is not None and not price_boxes:
        strong_items = _run_ocr(img_bgr[int(h_img * 0.18):int(h_img * 0.55), :], engine=ocr_strong)
        for item in strong_items:
            bx, by, bw, bh = item["box"]
            real_box = (bx, int(by + h_img * 0.18), bw, bh)
            t = item["text"]
            if any(sym in t for sym in ["￥", "¥", "?", "？", "券后", "折后", "拼单"]) and re.search(r"\d", t):
                price_boxes.append(real_box)

    if confirm_y is None or not price_boxes:
        return False, None, [], False

    price_boxes.sort(key=lambda b: (b[1] // 15, b[0]))
    price_anchor = price_boxes[0]
    _, py, _, _ = price_anchor

    sku_top = py + 140
    for item in ocr_items:
        if any(w in item["text"] for w in ("已选", "请选择")) and py < item["cy"] < py + 150:
            sku_top = max(sku_top, int(item["box"][1] + item["box"][3] + 35))
        if item["text"].strip() in ("-", "+", "－", "＋") and py < item["cy"] < py + 180:
            sku_top = max(sku_top, int(item["box"][1] + item["box"][3] + 10))
    if min_cat_top is not None:
        sku_top = max(sku_top, int(min_cat_top))

    b, g, r = cv2.split(img_bgr)
    pink_mask = ((r >= 244) & (g >= 224) & (g <= 248) & (b >= 224) & (b <= 248) & (
            (r.astype(np.int16) - b.astype(np.int16)) >= 5)).astype(np.uint8) * 255
    max_c, min_c = np.maximum(np.maximum(b, g), r), np.minimum(np.minimum(b, g), r)
    gray_mask = ((min_c >= 240) & (max_c <= 249) & ((max_c - min_c) <= 4)).astype(np.uint8) * 255
    combined_sku_mask = cv2.bitwise_or(pink_mask, gray_mask)

    raw_boxes = []
    kernel = np.ones((5, 5), np.uint8)
    for mask in (pink_mask, gray_mask):
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if sku_top <= y and (y + h) <= confirm_y and 42 < w < w_img * 0.96 and 25 <= h <= 88:
                if float(np.mean(mask[y:y + h, x:x + w] > 0)) > 0.35:
                    if not any(abs(x - rx) < 10 and abs(y - ry) < 10 for rx, ry, _, _ in raw_boxes):
                        raw_boxes.append((x, y, w, h))

    raw_boxes.sort(key=lambda b: (b[1] // 18, b[0]))

    used_ocr = set()
    sku_items = []
    for bx, by, bw, bh in raw_boxes:
        frags = []
        for idx, item in enumerate(ocr_items):
            ox, oy, ow, oh = item["box"]
            ix, iy = max(0, min(bx + bw, ox + ow) - max(bx, ox)), max(0, min(by + bh, oy + oh) - max(by, oy))
            if (ix * iy) / float(max(1, ow * oh)) >= 0.35:
                frags.append((oy, ox, item["text"]))
                used_ocr.add(idx)
        frags.sort(key=lambda f: (f[0] // 16, f[1]))
        sku_items.append({"box": (bx, by, bw, bh), "cy": by + bh / 2.0, "text": "".join(t for _, _, t in frags)})

    # 【新增判断】：检测距离“确定”最近的文字，其与“确定”之间的垂直间距是否超过“确定”本身的高度
    is_fully_expanded = False
    if confirm_item:
        above_items = [i for i in ocr_items if i is not confirm_item and i["cy"] < confirm_y]
        if above_items:
            nearest_bottom = max(i["box"][1] + i["box"][3] for i in above_items)
            gap = confirm_y - nearest_bottom
            if gap > confirm_h:
                is_fully_expanded = True

    if not sku_items:
        return True, price_anchor, [], is_fully_expanded

    cat_top = max(sku_top - 25, min(i["box"][1] for i in sku_items) - 65)
    if min_cat_top is not None:
        cat_top = max(cat_top, int(min_cat_top))

    categories = []
    for idx, item in enumerate(ocr_items):
        if idx not in used_ocr and cat_top <= item["cy"] <= confirm_y and item["box"][0] < w_img * 0.25:
            ox, oy, ow, oh = item["box"]
            roi_mask = combined_sku_mask[max(0, oy):min(h_img, oy + oh), max(0, ox):min(w_img, ox + ow)]
            if roi_mask.size > 0 and float(np.mean(roi_mask > 0)) > 0.20:
                continue
            if not any(w in item["text"] for w in ("已选", "请选择", "抢光", "恢复")):
                categories.append({"name": item["text"].rstrip(":：").strip(), "cy": item["cy"], "items": []})

    categories.sort(key=lambda c: c["cy"])
    if not categories:
        return True, price_anchor, [], is_fully_expanded

    for sku in sku_items:
        target_cat = None
        for cat in categories:
            if cat["cy"] < sku["cy"]:
                target_cat = cat
        if target_cat is not None:
            target_cat["items"].append(sku)

    return True, price_anchor, [c for c in categories if c["items"]], is_fully_expanded


def get_pdd_sku_info(id, debug=True):
    """
    获取所有有效（非缺货）SKU 规格组合、价格及截图
    根据规格完全展开判断自动跳过不必要的上下滑动探测，大幅提升简单商品抓取速度。
    :param id: 截图命名前缀（保存在 SKU_DEBUG_DIR 下，如 id_1.png, id_2.png）
    :param debug: 默认为 True，保存每次选择的完整页面截图
    :return: 字典列表，任何异常或非详情页均稳定返回 []
    """
    try:
        window = auto.WindowControl(searchDepth=1, Name=TARGET_APP_NAME)
        if not window.Exists(2, 0.3):
            return []

        hwnd = window.NativeWindowHandle
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.08)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass

        # 1. 连续两次校验是否处于详情规格页（初始顶部画面）
        is_detail, price_anchor, top_categories, current_img = False, None, [], None
        win_left, win_top = 0, 0
        is_fully_expanded = False
        for attempt in range(2):
            current_img, win_left, win_top = _capture_full_window(window)
            is_detail, price_anchor, top_categories, is_fully_expanded = _parse_detail_page(current_img)
            if is_detail:
                break
            if attempt == 0:
                time.sleep(0.3)

        if not is_detail or not top_categories:
            return []

        # 以首页第一次识别到的首个规格名的位置作为最高上限
        first_cat_top = int(top_categories[0]["cy"] - 15)

        h_img, w_img = current_img.shape[:2]
        top_cat_names = {c["name"] for c in top_categories}
        pre_selected_specs = {}  # 保存底部预选成功的规格，如 {"口味": "500g 【39粒】"}
        categories = top_categories

        # =========================================================================
        # 步骤 1.5 & 1.8：根据规格展开状态决定是否执行长列表探测
        # =========================================================================
        if not is_fully_expanded:
            scroll_x = int(win_left + w_img * 0.5)
            scroll_y = int(win_top + h_img * 0.65)

            # 【先下滑探测底部规格】
            auto.MoveTo(scroll_x, scroll_y, waitTime=0)
            auto.WheelDown(wheelTimes=8, waitTime=0.03)
            time.sleep(0.25)

            btm_img, win_left, win_top = _capture_full_window(window)
            is_detail_btm, _, btm_categories, _ = _parse_detail_page(btm_img, min_cat_top=first_cat_top)

            if is_detail_btm and btm_categories:
                for btm_cat in btm_categories:
                    cat_name = btm_cat["name"]
                    if cat_name not in top_cat_names or btm_cat is btm_categories[-1]:
                        selected_text = None
                        for sku in btm_cat["items"]:
                            if sku["text"] and _is_box_selected(btm_img, sku["box"]):
                                selected_text = sku["text"]
                                break

                        if not selected_text:
                            for sku in btm_cat["items"]:
                                if not sku["text"] or _is_box_disabled(btm_img, sku["box"]):
                                    continue
                                bx, by, bw, bh = sku["box"]
                                auto.Click(int(win_left + bx + bw // 2), int(win_top + by + bh // 2), waitTime=0)
                                time.sleep(0.08)
                                btm_img, win_left, win_top = _capture_full_window(window)
                                if _is_box_selected(btm_img, (bx, by, bw, bh)):
                                    selected_text = sku["text"]
                                    break

                        if selected_text:
                            pre_selected_specs[cat_name] = selected_text
                        elif cat_name not in top_cat_names:
                            logger.info(f"[WARN] 底部隐藏规格分类 '{cat_name}' 无可用选项可锁定，为防脏数据已中止提取")
                            return []

            # 【上滑复位】滑回顶部，重新解析顶部规格坐标
            auto.MoveTo(scroll_x, scroll_y, waitTime=0)
            auto.WheelUp(wheelTimes=10, waitTime=0.03)
            time.sleep(0.25)

            current_img, win_left, win_top = _capture_full_window(window)
            is_detail, price_anchor, categories, _ = _parse_detail_page(current_img, min_cat_top=first_cat_top)
            if not is_detail or not categories:
                return []
        else:
            logger.info("[INFO] ⚡ 检测到规格已完全展开，已智能跳过上下滑动探测！")

        cat_names = [c["name"] for c in categories]
        all_combos = list(itertools.product(*(c["items"] for c in categories)))
        results = []

        # 2. 极速遍历顶部所有规格组合（自动跳过缺货规格）
        for idx, combo in enumerate(all_combos, start=1):
            try:
                combo_valid = True
                for target_sku in combo:
                    if not target_sku["text"]:
                        combo_valid = False
                        break

                    bx, by, bw, bh = target_sku["box"]
                    if _is_box_selected(current_img, (bx, by, bw, bh)):
                        continue

                    if _is_box_disabled(current_img, (bx, by, bw, bh)):
                        combo_valid = False
                        break

                    auto.Click(int(win_left + bx + bw // 2), int(win_top + by + bh // 2), waitTime=0)
                    time.sleep(0.05)
                    current_img, win_left, win_top = _capture_full_window(window)

                    if not _is_box_selected(current_img, (bx, by, bw, bh)):
                        combo_valid = False
                        break

                if not combo_valid or not all(_is_box_selected(current_img, s["box"]) for s in combo):
                    continue

                screenshot_path = ""
                price_screenshot_path = ""
                if debug:
                    screenshot_path = os.path.join(SKU_DEBUG_DIR, f"{id}_{idx}.png")
                    price_screenshot_path = os.path.join(SKU_DEBUG_DIR, f"{id}_{idx}_price.png")
                    ok, buf = cv2.imencode(".png", current_img, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if ok:
                        buf.tofile(screenshot_path)

                price_text = _extract_price_fast(
                    current_img,
                    price_anchor,
                    save_path=price_screenshot_path if debug else None
                )

                if not price_text or any(sym in price_text for sym in ("-", "~", "起")):
                    continue

                record = {name: sku["text"] for name, sku in zip(cat_names, combo)}

                for pre_cat, pre_val in pre_selected_specs.items():
                    if pre_cat not in record:
                        record[pre_cat] = pre_val

                record["价格"] = price_text
                record["截图地址"] = screenshot_path
                results.append(record)
            except Exception:
                continue

        return results
    except Exception:
        return []


# ==========================================
# 🤖 RPA 核心引擎 (极致性能与安全防御层)
# ==========================================
class UIActionEngine:
    def __init__(self, window_name):
        self.window_name = window_name
        self.window = None
        self.hwnd = 0

    @staticmethod
    def release_modifier_keys():
        """【防按键粘连核心】强制向系统发送 Ctrl/Shift/Alt 的 KeyUp 弹起事件，彻底解决 Ctrl 粘连"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 0x11: VK_CONTROL, 0x10: VK_SHIFT, 0x12: VK_MENU (Alt)
            for vk in (0x11, 0x10, 0x12):
                user32.keybd_event(vk, 0, 2, 0)  # 2: KEYEVENTF_KEYUP
        except Exception:
            pass

    def is_foreground(self):
        """【最高安全防护锁】判定当前获得 Windows 前台焦点的窗口是否为本目标窗口或其子窗口"""
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return False
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd == self.hwnd:
            return True
        # 兼容子窗口句柄
        try:
            temp = fg_hwnd
            while temp:
                if temp == self.hwnd:
                    return True
                temp = win32gui.GetParent(temp)
        except Exception:
            pass
        return False

    def find_window(self, timeout=3):
        self.window = auto.WindowControl(searchDepth=1, Name=self.window_name)
        if self.window.Exists(timeout, 1):
            self.hwnd = self.window.NativeWindowHandle
            # 严格核验句柄在 Windows 中是否存活
            if win32gui.IsWindow(self.hwnd):
                return True
        self.hwnd = 0
        return False

    def force_bring_to_front(self):
        """【工业级穿透置顶】通过 AttachThreadInput 与模拟 Alt 穿透 Windows 焦点防窃取拦截"""
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return False
        try:
            # 1. 强行还原最小化窗口
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)

            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd == self.hwnd:
                return True

            # 2. 线程注入绑定，突破 Windows 权限限制
            fg_thread_id, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
            curr_thread_id = win32api.GetCurrentThreadId()

            attached = False
            if fg_thread_id != curr_thread_id and fg_thread_id != 0:
                try:
                    import ctypes
                    ctypes.windll.user32.AttachThreadInput(curr_thread_id, fg_thread_id, True)
                    attached = True
                except Exception:
                    pass

            try:
                # 3. 模拟虚拟 Alt 键解锁焦点权限
                import ctypes
                user32 = ctypes.windll.user32
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, 2, 0)

                # 4. 置顶并激活前台
                win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                win32gui.SetForegroundWindow(self.hwnd)
                time.sleep(0.04)
                win32gui.SetWindowPos(self.hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            finally:
                if attached:
                    try:
                        ctypes.windll.user32.AttachThreadInput(curr_thread_id, fg_thread_id, False)
                    except Exception:
                        pass

            return self.is_foreground()
        except Exception as e:
            logger.info(f"[WARN] 窗口置顶失败: {e}")
            return False

    def safe_click(self, x, y, desc=""):
        """无感瞬发点击，剥离所有冗余等待"""
        if desc:
            logger.info(f"[INFO] 执行点击: {desc} ({x}, {y})")
        auto.Click(int(x), int(y))

    def safe_input(self, text, desc=""):
        """极速输入：带焦点安全校验与修饰键自动安全释放"""
        if not self.is_foreground():
            logger.info(f"[ABORT] 焦点不在目标窗口，拦截外部输入: {desc}")
            return
        if desc:
            logger.info(f"[INFO] 执行输入: {desc}")
        try:
            self.release_modifier_keys()
            pyperclip.copy(text)
            auto.SendKeys('{Ctrl}v')
            time.sleep(0.01)
            auto.SendKeys('{Enter}')
        finally:
            self.release_modifier_keys()

    def fast_screenshot_save(self, save_path, region=None):
        """使用 mss 极速保存全图/区域截图"""
        if region is None:
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

        monitor = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
        sct_img = sct.grab(monitor)
        img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
        cv2.imwrite(save_path, img_cv)

    def safe_ocr_wait(self, target_texts, timeout=5, region=None, ocr=ocr_fast):
        """【性能核心】基于 mss 的超高频视觉捕捉！毫秒级响应"""
        if isinstance(target_texts, str):
            target_texts = [target_texts]

        start_time = time.time()

        if region is None:
            rect = self.window.BoundingRectangle
            capture_region = (int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top))
        else:
            capture_region = tuple(map(int, region))

        monitor = {"left": capture_region[0], "top": capture_region[1],
                   "width": capture_region[2], "height": capture_region[3]}

        while time.time() - start_time < timeout:
            try:
                sct_img = sct.grab(monitor)
                img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
                result, _ = ocr(img_cv)

                if result:
                    for line in result:
                        text = line[1] if len(line) >= 2 else ""
                        for target in target_texts:
                            if target in text:
                                return True, target
            except Exception:
                pass
        return False, None


# ==========================================
# 🏢 双桥梁业务逻辑层
# ==========================================
class PddAutomation(UIActionEngine):
    def __init__(self):
        super().__init__(TRANSFER_APP_NAME)

    def click_home_button(self):
        """点击拼多多小程序左上角小房子，复位回首页"""
        if self.window and self.window.Exists(0, 0):
            try:
                # 先按一次 ESC，防止 SKU 弹窗拦截点击
                auto.SendKeys('{Esc}')
                time.sleep(0.05)
                rect = self.window.BoundingRectangle
                # 点击左上角小房子图标（高度与关闭按钮一致）
                self.safe_click(rect.left + 30, rect.top + 55, "点击拼多多回到首页")
                time.sleep(0.3)
            except Exception as e:
                logger.info(f"[WARN] 点击回到首页失败: {e}")

    def close_current_window(self):
        """通用窗口关闭，用于清理拼多多窗口，保持桌面整洁"""
        if self.window and self.window.Exists(0, 0):
            rect = self.window.BoundingRectangle
            self.safe_click(rect.right - 25, rect.top + 60, f"关闭 {self.window_name} 窗口")
            time.sleep(0.3)

    def get_control_text(self, ctrl):
        """获取控件文本，优先 ValuePattern，兜底 LegacyIAccessible 或 Name"""
        if not ctrl:
            return ""
        try:
            vp = ctrl.GetValuePattern()
            if vp:
                return vp.Value or ""
        except Exception:
            pass
        try:
            lp = ctrl.GetLegacyIAccessiblePattern()
            if lp:
                return lp.Value or ""
        except Exception:
            pass
        return ctrl.Name or ""

    def ensure_transfer_clean_state(self, max_retries=3):
        """
        【自愈核心】检测非期望弹窗并自动消除，强行将中转站恢复至【情况2】干净状态
        """
        for _ in range(max_retries):
            cleaned = False

            # 1. 检查并修复【情况3】：不支持拖入文件弹窗
            title_unsupported = self.window.TextControl(Name="当前小程序不支持拖入文件")
            if title_unsupported.Exists(0, 0):
                btn_ok = self.window.TextControl(Name="确定")
                if btn_ok.Exists(0.5, 0):
                    try:
                        btn_ok.GetInvokePattern().Invoke()
                    except Exception:
                        btn_ok.Click(simulateMove=False)
                    logger.info("[HEAL] 检测到'当前小程序不支持拖入文件'弹窗，已自动点击'确定'")
                    cleaned = True
                    time.sleep(0.1)

            # 2. 检查并修复【情况1】：遗留的跳转弹窗
            title_jump = self.window.TextControl(Name="即将打开“拼多多”小程序")
            if title_jump.Exists(0, 0):
                btn_cancel = self.window.TextControl(Name="取消")
                if btn_cancel.Exists(0.5, 0):
                    try:
                        btn_cancel.GetInvokePattern().Invoke()
                    except Exception:
                        btn_cancel.Click(simulateMove=False)
                    logger.info("[HEAL] 检测到残留的'即将打开拼多多'弹窗，已自动点击'取消'")
                    cleaned = True
                    time.sleep(0.1)

            # 3. 检查是否有遮挡的 wrap 容器弹窗
            wrap_modal = self.window.GroupControl(ClassName="wrap")
            if wrap_modal.Exists(0, 0):
                btn_cancel = wrap_modal.TextControl(Name="取消")
                btn_ok = wrap_modal.TextControl(Name="确定")
                if btn_cancel.Exists(0.2, 0):
                    try:
                        btn_cancel.GetInvokePattern().Invoke()
                    except Exception:
                        btn_cancel.Click(simulateMove=False)
                    cleaned = True
                elif btn_ok.Exists(0.2, 0):
                    try:
                        btn_ok.GetInvokePattern().Invoke()
                    except Exception:
                        btn_ok.Click(simulateMove=False)
                    cleaned = True
                time.sleep(0.1)

            if not cleaned:
                break
        time.sleep(0.3)
        wrap_modal = self.window.GroupControl(ClassName="wrap")
        if wrap_modal.Exists(0, 0):
            return False
        return True

    def set_transfer_goods_id(self, goods_id, max_attempts=3):
        """
        【输入与双向绑定保障】严格防误击穿安全锁 + 防粘连清空 + 真实按键驱动 + 严格值比对
        """
        target_str = str(goods_id).strip()
        edit_box = self.window.EditControl()
        if not edit_box.Exists(2, 1):
            return False, "无法定位中转小程序的输入框"

        current_val = ""
        for attempt in range(1, max_attempts + 1):
            try:
                self.release_modifier_keys()

                # 【防误击穿核心防御】：输入前强制确保当前焦点确实属于中转小程序，绝不允许敲击其他应用！
                if not self.is_foreground():
                    if not self.force_bring_to_front():
                        return False, "【安全熔断】中转站未在前台激活，已紧急中止键盘输入，防止误改其他软件！"

                edit_box.Click(simulateMove=False)
                time.sleep(0.02)

                # 1. 前置读取：如果当前值已完全等于目标值，无需重复输入
                cur_before = self.get_control_text(edit_box).strip()
                if cur_before == target_str:
                    return True, ""

                # 2. 深度强制清空，杜绝字符串追加粘连
                try:
                    vp = edit_box.GetValuePattern()
                    if vp:
                        vp.SetValue("")
                except Exception:
                    pass

                # 再次确认处于前台才可按下全选
                if not self.is_foreground():
                    return False, "【安全熔断】输入框失焦，中止清空击键"

                auto.SendKeys('{Ctrl}a{Delete}')
                self.release_modifier_keys()
                time.sleep(0.02)

                # 校验是否清空彻底，若依然有残留字符，采用退格键强行抹除
                remained = self.get_control_text(edit_box).strip()
                if remained:
                    auto.SendKeys('{End}' + '{Back}' * (len(remained) + 3))
                    time.sleep(0.02)

                # 3. 写入目标内容并触发双向绑定
                pyperclip.copy(target_str)
                auto.SendKeys('{Ctrl}v')
                self.release_modifier_keys()
                time.sleep(0.02)

                # 触发 Vue / 小程序数据绑定变更事件
                auto.SendKeys(' {Back}')
                time.sleep(0.03)

                # 4. 严格二次校验
                current_val = self.get_control_text(edit_box).strip()
                if current_val == target_str:
                    return True, ""

                logger.info(
                    f"[WARN] 输入校验不匹配 (期望: '{target_str}', 实际: '{current_val}')，尝试第 {attempt} 次全按键重输")

                # 重试方案：再次彻底清空重试
                if not self.is_foreground():
                    return False, "【安全熔断】重输前检测到窗口失焦，中止操作"

                edit_box.Click(simulateMove=False)
                time.sleep(0.02)
                auto.SendKeys('{Ctrl}a{Delete}')
                self.release_modifier_keys()
                time.sleep(0.02)
                auto.SendKeys(target_str)
                auto.SendKeys(' {Back}')
                time.sleep(0.03)

                current_val = self.get_control_text(edit_box).strip()
                if current_val == target_str:
                    return True, ""

            finally:
                self.release_modifier_keys()

        return False, f"中转站输入框赋值校验失败: 界面当前值为 '{current_val}'，并非目标商品 ID '{target_str}'"

    def process_single_goods(self, goods_id, index, consecutive_successes=0, ocr=ocr_fast):
        extra_info = {"is_group": False, "force_retry_count": None}

        # --- 步骤 1：确立中转站据点 ---
        self.window_name = TRANSFER_APP_NAME

        # 严格窗口检查：窗口不存在或非激活态则唤醒
        if not self.find_window(timeout=1):
            logger.info(f"[SYSTEM] 正在唤醒中转小程序: {TRANSFER_APP_NAME}")
            os.startfile(TRANSFER_SHORTCUT_PATH)
            if not self.find_window(timeout=5):
                return False, [], "中转小程序启动失败", extra_info

        # 【核心安全防护】：若未能成功置顶到前台，重新尝试唤醒拉起，绝不盲目进行输入！
        if not self.force_bring_to_front():
            logger.info("[WARN] 中转小程序置顶受阻，尝试重新调起快捷方式...")
            os.startfile(TRANSFER_SHORTCUT_PATH)
            time.sleep(1)
            self.find_window(timeout=3)
            if not self.force_bring_to_front():
                return False, [], "中转小程序无法激活到前台，已终止操作以保护系统其他文件！", extra_info

        # --- 步骤 1.5：环境自愈（确保恢复至【情况2】） ---
        if not self.ensure_transfer_clean_state():
            return False, [], "中转小程序存在无法自动关闭的弹窗，环境未能恢复至正常状态", extra_info

        # --- 步骤 2：精确写入商品 ID 并严格比对 ---
        logger.info(f"[TASK] [{index}] 步骤 1/4: 向中转站写入商品 ID")
        input_ok, input_err = self.set_transfer_goods_id(goods_id)
        if not input_ok:
            return False, [], input_err, extra_info

        logger.info(f"[TASK] [{index}] 步骤 2/4: 触发 API 跳转")
        jump_btn = self.window.ButtonControl(Name="跳转到该商品")
        if not jump_btn.Exists(1, 1):
            return False, [], "无法定位中转小程序的跳转按钮", extra_info

        try:
            jump_btn.GetInvokePattern().Invoke()
        except Exception:
            jump_btn.Click(simulateMove=False)

        # --- 步骤 2.5：处理微信跳转授权弹窗 ---
        logger.info(f"[TASK] [{index}] 步骤 2.5/4: 处理微信跳转授权弹窗")
        allow_btn = self.window.TextControl(Name="允许")
        if allow_btn.Exists(2, 0.2):
            try:
                allow_btn.GetInvokePattern().Invoke()
            except Exception:
                allow_btn.Click(simulateMove=False)
            logger.info("[INFO] 已自动点击“允许”跳转")
        else:
            logger.info("[INFO] 未检测到“允许”弹窗（可能已静默放行或系统延迟）")

        # --- 步骤 3 & 4：接管拼多多并进行状态机轮询 ---
        logger.info(f"[TASK] [{index}] 步骤 3/4: 状态机轮询 (详情页识别 -> 动态点击 -> SKU捕获)")
        self.window_name = TARGET_APP_NAME

        if not self.find_window(timeout=5):
            return False, [], "未检测到拼多多窗口弹出（跳转可能失败）", extra_info

        self.force_bring_to_front()

        # 【核心防护 1】：给予刚唤醒的 Chromium 内核 0.3 秒缓冲，避免在 DOM 重建期访问抛出 COM 异常
        time.sleep(0.3)

        # 【核心防护 2】：加装专用 try-except 容错，安全检测 close_btn
        try:
            close_btn = self.window.GroupControl(ClassName="close_btn")
            if close_btn.Exists(0.5, 0.1):
                logger.info("[HEAL] 检测到拼多多存在 'close_btn' 超时弹窗，正在执行消除...")
                try:
                    close_btn.Click(simulateMove=False)
                except Exception:
                    btn_rect = close_btn.BoundingRectangle
                    if btn_rect.width() > 0:
                        self.safe_click((btn_rect.left + btn_rect.right) // 2,
                                        (btn_rect.top + btn_rect.bottom) // 2,
                                        "点击 close_btn")
                time.sleep(0.3)
        except Exception as e:
            logger.info(f"[DEBUG] 检测 close_btn 时 UI 树正在重绘，已安全绕过: {e}")

        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top

        # 【改动 1】：识别区域调整为下半部分 (高度为 1/2)
        capture_region = (int(rect.left), int(rect.bottom - h // 2), int(w), int(h // 2))
        monitor = {
            "left": capture_region[0],
            "top": capture_region[1],
            "width": capture_region[2],
            "height": capture_region[3]
        }

        start_time = time.time()
        detail_entered = False
        sku_ready = False
        last_click_time = 0.0
        click_count = 0
        CLICK_RETRY_INTERVAL = 1.3  # 冷却重试时间间隔（秒）

        # 闭环状态机轮询：最多尝试 10 秒
        while time.time() - start_time < 10:
            try:
                sct_img = sct.grab(monitor)
                img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
                result, _ = ocr(img_cv)

                if result:
                    detected_text = "".join([line[1] for line in result if len(line) >= 2])

                    # 1. 【优先检查终止态】：只要底部出现了“确定”或“确认”，说明 SKU 面板已成功展开
                    if detail_entered and any(kw in detected_text for kw in ["确定", "确认"]):
                        sku_ready = True
                        logger.info(f"[INFO] ✅ 检测到 SKU 面板已完全展开 (耗时: {time.time() - start_time:.2f}s)")
                        break


                    if "人团" in detected_text:
                        extra_info["is_group"] = True
                        logger.info("[INFO] 检测到'人团'标识，将标记为 group 商品")

                    if "不支持配送" in detected_text:
                        extra_info["force_retry_count"] = 10
                        logger.info("[WARN] 检测到'不支持配送'，立即熔断终止轮询")
                        return False, [], "不支持配送", extra_info

                    # 2. 【异常商品快速熔断】：检测到缺货/下架立即终止，防止无谓等待 10 秒
                    if any(bad_kw in detected_text for bad_kw in ["已售罄", "已下架", "商品不存在", "全部售罄"]):
                        logger.info(f"[WARN] 检测到商品异常状态 ({detected_text[:20]})，提前终止轮询")
                        return False, [], f"商品不可售({detected_text[:10]})", extra_info

                    # 3. 【检测原生详情页并支持周期性重拍】
                    # 画面中至少存在 客服/店铺/收藏 中的两个，且没有“确定”，说明当前依然停留在详情页底栏
                    if sum(1 for kw in ["客服", "店铺", "收藏"] if kw in detected_text) >= 2 and (
                            "确定" not in detected_text):
                        now = time.time()
                        # 首次进入详情页，或距离上次点击已超过 1.3 秒但面板仍未弹出 -> 触发点击
                        if not detail_entered or (now - last_click_time >= CLICK_RETRY_INTERVAL):
                            if not detail_entered:
                                # 【核心防护】：首次识别到底部栏，微等 0.15 秒让小程序绑定事件，杜绝早产死点击
                                time.sleep(0.15)
                            else:
                                # 重试点击前，防范延迟弹出的优惠券遮挡页面
                                try:
                                    close_btn = self.window.GroupControl(ClassName="close_btn")
                                    if close_btn.Exists(0.1, 0):
                                        close_btn.Click(simulateMove=False)
                                        logger.info("[HEAL] 轮询中消除延迟弹出的 close_btn 遮挡弹窗")
                                        time.sleep(0.2)
                                except Exception:
                                    pass

                            # 动态获取最新窗口坐标，防止窗口发生微移
                            rect = self.window.BoundingRectangle
                            # 直接使用固定右下角点击位
                            click_x = rect.right - 60 - (click_count % 2) * 15
                            click_y = rect.bottom - 25

                            click_count += 1
                            detail_entered = True
                            last_click_time = time.time()
                            self.safe_click(click_x, click_y, f"动态点击购买 (第 {click_count} 次)")
                            time.sleep(0.3)
                            continue

            except Exception:
                pass

            # 释放 80ms CPU 时间片，避免 OCR 满载拖慢小程序的 UI 渲染进程
            time.sleep(0.08)

        # 轮询结束，进行结果清算
        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            return False, [], "进入详情页失败", extra_info

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            return False, [], "SKU面板未完全展开或点击全部失效", extra_info

        # --- 步骤 4：使用新逻辑遍历提取所有 SKU 规格组合及价格 ---
        logger.info(f"[TASK] [{index}] 步骤 4/4: 正在遍历提取所有 SKU 规格组合与价格...")
        sku_info = get_pdd_sku_info(id=goods_id, debug=DEBUG_MODE)
        if not sku_info:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            return False, [], "未能提取到有效的SKU规格组合数据", extra_info

        logger.info(f"[TASK] [{index}] 🎯 成功提取 {len(sku_info)} 个规格组合。")

        # 连续成功清理策略
        if (consecutive_successes + 1) % 500 == 0:
            self.close_current_window()
            logger.info("[INFO] 循环连轴转达到 500 次，重启(关闭)拼多多小程序释放资源。")

        return True, sku_info, "", extra_info

# ==========================================
# 🚦 任务调度引擎 (直接对接数据库)
# ==========================================
def batch_runner(product_list):
    logger.info("=" * 60)
    logger.info("[SYSTEM] 🚀 桥接级 RPA 引擎启动 (通过wxid_2lr36gkzn2io22的小程序测试号中转)")
    logger.info(f"[INFO] 📊 待执行任务数: {len(product_list)}")
    logger.info("=" * 60)

    if not product_list:
        return

    bot = PddAutomation()
    batch_counts = {"success": 0, "failed": 0, "skipped": 0}
    consecutive_successes = 0
    consecutive_failures = 0

    for i, product in enumerate(product_list, 1):
        goods_id = product["product_id"]
        logger.info("-" * 40)
        logger.info(f"[INFO] ▶▶▶ 开始处理 [{i}/{len(product_list)}] goods_id: {goods_id}")

        try:
            success, sku_info, error_msg, extra_info = bot.process_single_goods(goods_id, i, consecutive_successes)

            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

                # 处理 _source_api 追加 "group" 逻辑
                source_api_update = None
                if extra_info.get("is_group"):
                    current_source = product.get("_source_api")
                    if isinstance(current_source, list):
                        new_source = list(current_source)
                        if "group" not in new_source:
                            new_source.append("group")
                    elif isinstance(current_source, str):
                        new_source = [current_source] if current_source != "group" else ["group"]
                        if "group" not in new_source:
                            new_source.append("group")
                    else:
                        new_source = ["group"]
                    source_api_update = new_source

                if success and sku_info:
                    update_data = {
                        "platform": product.get("platform", "pdd"),
                        "product_id": product["product_id"],
                        "need_reformat": True,
                        "need_update_sku": False,  # 【核心闭环】：成功更新完整规格，不再重复拉起抓取
                        "sku_info": sku_info,
                        "sku_status": "success",
                        "sku_error": "",
                        "sku_updated_at": datetime.now(timezone.utc),
                    }
                    if source_api_update is not None:
                        update_data["_source_api"] = source_api_update

                    saved = product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments={"sku_retry_count": 0},
                        upsert=False
                    )
                    if saved.get("update"):
                        batch_counts["success"] += 1
                    else:
                        batch_counts["skipped"] += 1

                    logger.info(f"[SUCCESS] ✅ 处理成功并已写入数据库 ({goods_id})")
                    consecutive_successes += 1
                    consecutive_failures = 0
                else:
                    update_data = {
                        "platform": product.get("platform", "pdd"),
                        "product_id": product["product_id"],
                        "sku_status": "failed",
                        "sku_error": error_msg or "获取SKU列表为空",
                        "sku_updated_at": datetime.now(timezone.utc),
                    }
                    if source_api_update is not None:
                        update_data["_source_api"] = source_api_update

                    # 处理不支持配送时，直接将重试次数置为 10
                    force_retry = extra_info.get("force_retry_count")
                    if force_retry is not None:
                        update_data["sku_retry_count"] = force_retry
                        inc_dict = {}
                    else:
                        inc_dict = {"sku_retry_count": 1}

                    product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments=inc_dict,
                        upsert=False
                    )
                    logger.info(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg}")

                    # 【核心修复时序】：
                    # 1. 此时拼多多仍停留在失败现场（滑块页面未被提前摧毁），先执行滑块破解
                    solve_pdd_captcha()
                    # 2. 验证码处理完毕后，再将拼多多复位回首页，确保下次任务不受旧页面干扰
                    bot.click_home_button()
                    bot.close_current_window()

                    logger.info("-" * 40)
                    time.sleep(10)
                    batch_counts["failed"] += 1
                    consecutive_successes = 0
                    consecutive_failures += 1

        except Exception as e:
            logger.info(f"[FATAL] 💥 发生严重异常: {str(e)}")
            bot.release_modifier_keys()  # 异常发生时兜底释放修饰键
            batch_counts["failed"] += 1
            consecutive_successes = 0
            consecutive_failures += 1

            try:
                with closing(gen_db_object()) as db_instance:
                    db_instance.ping()
                    product_manager = ProductManager(db_instance)
                    update_data = {
                        "platform": product.get("platform", "pdd"),
                        "product_id": product["product_id"],
                        "sku_status": "failed",
                        "sku_error": str(e),
                        "sku_updated_at": datetime.now(timezone.utc),
                    }
                    product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments={"sku_retry_count": 1},
                        upsert=False
                    )
            except Exception as db_err:
                logger.info(f"[ERROR] 数据库异常状态更新失败: {db_err}")

            try:
                bot.window_name = TARGET_APP_NAME
                if bot.find_window(timeout=1):
                    bot.close_current_window()
            except Exception:
                pass

        # 判断是否连续失败 10 次
        if consecutive_failures >= 10:
            logger.info(
                f"[WARN] ⚠️ 已连续发生 {consecutive_failures} 次失败，主动触发熔断机制，跳过本轮剩余的 {len(product_list) - i} 个任务！")
            break

    logger.info("=" * 50)
    logger.info(
        f"[SYSTEM] 🎉 批量任务完毕！ 成功: {batch_counts['success']} | 跳过: {batch_counts['skipped']} | 失败: {batch_counts['failed']}")
    logger.info("=" * 50)

def get_data_updated_within_24h(hours=12, limit=0, extra_query=None, projection=None):
    """
    查询指定时间范围内的拼多多商品数据（默认 hours=12，基准数据可传 hours=72）。
    """
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=hours)

    query_condition = {
        "platform": "pdd",
        "updated_at": {"$gte": time_threshold}
    }

    if extra_query and isinstance(extra_query, dict):
        query_condition = {**query_condition, **extra_query}
        query_condition["platform"] = extra_query.get("platform", "pdd")

    with closing(gen_db_object()) as db_instance:
        db_instance.ping()
        product_manager = ProductManager(db_instance)

        results = product_manager.query(
            query_condition,
            projection=projection,
            sort=[("updated_at", -1)],
            limit=limit
        )

    return results
import math


# ==========================================
# 📊 性价比计算与商品筛选核心模块
# ==========================================

# ==========================================
# 📊 性价比计算与商品筛选核心模块
# ==========================================

def calc_product_best_unit_prices(format_info):
    """
    计算单个商品在各个 unit (单位) 下的最佳性价比（即最低单位单价，元/单位）。
    单价越低，性价比越高。
    :param format_info: 商品的 format_info 字典
    :return: dict, 形如 {"瓶": 1.76, "ml": 0.00352}
    """
    if not isinstance(format_info, dict):
        return {}

    best_unit_prices = {}

    for sku_id, sku_data in format_info.items():
        if not isinstance(sku_data, dict):
            continue

        price = sku_data.get("price")
        if price is None:
            continue
        try:
            price = float(price)
            if price <= 0:
                continue
        except (ValueError, TypeError):
            continue

        pricing_basis = sku_data.get("pricing_basis") or {}
        if not isinstance(pricing_basis, dict):
            continue

        # 1. 收集当前 SKU 下各 unit 的总数量（英文字母统一转小写，如 KG -> kg, mL -> ml，防止分裂）
        unit_quantities = {}
        structure = pricing_basis.get("structure") or []
        if isinstance(structure, list):
            for item in structure:
                if not isinstance(item, dict):
                    continue
                u = item.get("unit")
                v = item.get("value")
                if u and v is not None:
                    try:
                        v_float = float(v)
                        if v_float > 0:
                            unit_quantities[str(u).strip().lower()] = v_float
                    except (ValueError, TypeError):
                        pass

        # 2. 如果存在 base_unit 与 total_value，修正对应 base_unit 的真实总容量/总量
        base_unit = pricing_basis.get("base_unit")
        total_val = pricing_basis.get("total_value")
        if base_unit and total_val is not None:
            try:
                t_float = float(total_val)
                if t_float > 0:
                    unit_quantities[str(base_unit).strip().lower()] = t_float
            except (ValueError, TypeError):
                pass

        # 3. 计算该规格下各 unit 的单价，并更新当前商品的最低单价
        for u, qty in unit_quantities.items():
            if qty > 0:
                unit_price = price / qty
                if unit_price <= 0:
                    continue
                if u not in best_unit_prices or unit_price < best_unit_prices[u]:
                    best_unit_prices[u] = unit_price

    return best_unit_prices


def is_product_confirmed_sku(product):
    """根据业务规范判定该商品是否已完成规格抓取与重洗（已确认规格）"""
    sku_info_list = product.get("sku_info") if isinstance(product.get("sku_info"), list) else []
    need_reformat = product.get("need_reformat")
    need_update_sku = product.get("need_update_sku")
    sku_status = product.get("sku_status")

    return bool(
        sku_info_list
        and (need_update_sku is False or sku_status == "success")
        and (need_reformat is False or need_reformat == "false")
    )


def filter_products_by_cost_effectiveness(
        candidate_products,
        confirmed_products,
        target_configs,
        max_top=10,
        ratio=0.01,
        benchmark_rank=5,
        only_top_unit=False,
        **kwargs
):
    """
    业务逻辑：
    1. 在 confirmed_products (72小时) 中按关键词提取已确认规格的商品；
    2. 计算每种 unit 的前 benchmark_rank (默认前5) 性价比，以第 5 名的单价作为【合格门槛】；
    3. 在 candidate_products 中提取候选商品，剔除性价比低于基准门槛（即单价高于基准单价）的商品；
    4. 对达标的商品按 unit 计算：按比例 (默认 0.01) 向上取整，最多不超过 max_top 个；
    5. 最后进行全局去重。
    """
    # 兼容历史调用中可能传入的 top_n 参数
    if "top_n" in kwargs:
        max_top = kwargs["top_n"]

    total_slots_selected = 0
    final_selected_map = {}  # 全局去重: {product_id: product}

    for config in target_configs:
        kw = config["search_keyword"]
        words = config["words"]
        max_price = config["max_price"]

        logger.info("-" * 60)
        logger.info(f"[FILTER] ▶ 开始处理关键词【{kw}】")

        # =========================================================================
        # 步骤 1：基于 72 小时内的已确认商品建立各 unit 的第 5 名性价比基准门槛
        # =========================================================================
        matched_confirmed = []
        for item in confirmed_products:
            if not is_product_confirmed_sku(item):
                continue

            if is_group_product(item):
                continue
            item_name = (item.get("name") or "").lower()
            if not words or not item_name:
                continue
            if sum(1 for w in words if w.lower() in item_name) < math.ceil(len(words) * 4 / 4):
                continue
            matched_confirmed.append(item)

        # 归集已确认商品的各 unit 单价
        confirmed_unit_prices = {}
        for item in matched_confirmed:
            unit_prices = calc_product_best_unit_prices(item.get("format_info"))
            for u, u_price in unit_prices.items():
                if u not in confirmed_unit_prices:
                    confirmed_unit_prices[u] = []
                confirmed_unit_prices[u].append(u_price)

        # 确立每个 unit 的基准价格（取升序排列第 benchmark_rank 名的单价）
        unit_benchmarks = {}
        for u, prices in confirmed_unit_prices.items():
            prices.sort()  # 单价从小到大（性价比从高到低）
            rank_idx = min(len(prices), benchmark_rank) - 1
            threshold = prices[rank_idx]
            unit_benchmarks[u] = {
                "threshold": threshold,
                "rank_used": rank_idx + 1,
                "total_confirmed": len(prices)
            }
            logger.info(
                f"  ├─ [72H基准] 单位 [{u}]: 命中 {len(prices)} 个已确认商品, "
                f"锁定第 {rank_idx + 1} 名单价作为门槛: {threshold:.4f}元/{u} (候选需 <= 此单价)"
            )

        # =========================================================================
        # 步骤 2：筛选出符合关键词分词与价格上限的候选商品
        # =========================================================================
        matched_candidates = []
        for item in candidate_products:
            if not bool(item.get("need_update_sku")):
                continue

            retry_count = item.get("sku_retry_count") or 0
            # 核心修改：只放行大于3且小于6的商品，其余跳过
            if not (3 < retry_count < 6):
                continue
            item_name = (item.get("name") or "").lower()
            if not words or not item_name:
                continue

            if sum(1 for w in words if w.lower() in item_name) < math.ceil(len(words) * 4 / 4):
                continue

            act_price = item.get("activity_price")
            if max_price > 0 and (act_price is None or act_price > max_price):
                continue

            matched_candidates.append(item)

        if not matched_candidates:
            logger.info(f"  └─ [候选] 未命中任何待抓取候选商品")
            continue

        # =========================================================================
        # 步骤 3：按 unit 归集，并执行【基准门槛过滤】
        # =========================================================================
        raw_unit_buckets = {}  # {unit: [(price, item)]} 原始候选
        qualified_unit_buckets = {}  # {unit: [(price, item)]} 达标候选

        for item in matched_candidates:
            cand_unit_prices = calc_product_best_unit_prices(item.get("format_info"))
            for u, u_price in cand_unit_prices.items():
                if u not in raw_unit_buckets:
                    raw_unit_buckets[u] = []
                    qualified_unit_buckets[u] = []
                raw_unit_buckets[u].append((u_price, item))

                # 判定是否超越基准线（单价 <= 基准单价）
                if u in unit_benchmarks:
                    benchmark_val = unit_benchmarks[u]["threshold"]
                    if u_price <= benchmark_val:
                        qualified_unit_buckets[u].append((u_price, item))
                else:
                    # 无 72H 基准历史数据的单位，默认放行
                    qualified_unit_buckets[u].append((u_price, item))

        # 打印各 unit 门槛过滤战报
        logger.info(f"  ├─ [候选池] 命中 {len(matched_candidates)} 个待抓取候选商品，门槛过滤结果:")
        for u, all_cands in raw_unit_buckets.items():
            pass_count = len(qualified_unit_buckets[u])
            eliminated = len(all_cands) - pass_count
            base_info = f"(基准: {unit_benchmarks[u]['threshold']:.4f}元/{u})" if u in unit_benchmarks else "(无72H基准)"
            logger.info(
                f"  │    └─ 单位 [{u}] {base_info}: 候选 {len(all_cands)} 个 -> 达标 {pass_count} 个, 淘汰 {eliminated} 个")

        # 剔除完全没有商品达标的 unit
        active_units = {u: items for u, items in qualified_unit_buckets.items() if items}
        if not active_units:
            logger.info(f"  └─ ⚠️ 该关键词下所有候选商品均未达到 72H 第 {benchmark_rank} 名性价比基准，本轮全部拦截！")
            continue

        # =========================================================================
        # 步骤 4：选取主流单位（可选）与 动态比例(向上取整)+Max Top 优选
        # =========================================================================
        if only_top_unit:
            dominant_u = max(active_units.keys(), key=lambda u: len(active_units[u]))
            logger.info(
                f"  ├─ [主流锁定] 仅采纳达标商品数最多的单位 [{dominant_u}] (达标商品: {len(active_units[dominant_u])} 个)")
            target_units = {dominant_u: active_units[dominant_u]}
        else:
            target_units = active_units

        kw_selected_count = 0
        for u, items_with_price in target_units.items():
            items_with_price.sort(key=lambda x: x[0])

            # 【核心修改点】：按达标数量 * ratio 向上取整计算，并以 max_top 为封顶上限
            qualified_count = len(items_with_price)
            calc_n = math.ceil(qualified_count * ratio)
            actual_top_n = min(calc_n, max_top) if max_top and max_top > 0 else calc_n

            top_items = items_with_price[:actual_top_n]

            kw_selected_count += len(top_items)
            total_slots_selected += len(top_items)

            for u_price, p in top_items:
                pid = p["product_id"]
                final_selected_map[pid] = p

            logger.info(
                f"  │    ★ 单位 [{u}]: 达标 {qualified_count} 个 -> 按比例({ratio})计算取 {calc_n} 个 "
                f"(上限 {max_top} -> 实取 Top {len(top_items)}), "
                f"入选最佳: {top_items[0][0]:.4f}元/{u}, 最末: {top_items[-1][0]:.4f}元/{u}"
            )

        logger.info(f"  └─ 关键词【{kw}】最终放行名额: {kw_selected_count} 个")

    result_list = list(final_selected_map.values())
    duplicates_removed = total_slots_selected - len(result_list)

    logger.info("=" * 60)
    logger.info(f"[FILTER-REPORT] 🎯 完整筛选统计报告:")
    logger.info(f"  ├─ 基准门槛规则: 72小时已确认商品 Top {benchmark_rank} 性价比")
    logger.info(f"  ├─ 比例抽选规则: 达标量 × {ratio} (向上取整, 上限: {max_top})")
    logger.info(f"  ├─ 各关键词及单位达标入选总名额: {total_slots_selected} 个")
    logger.info(f"  ├─ 跨单位/跨关键词去重合并名额: {duplicates_removed} 个")
    logger.info(f"  └─ 最终放行待抓取的唯一商品数: {len(result_list)} 个")
    logger.info("=" * 60)

    return result_list
# ==========================================
# 🚦 主程序入口
# ==========================================
import math

# ==========================================
# 🚦 主程序入口
# ==========================================
import math


def is_group_product(product):
    """
    判断商品是否属于带有门槛的 group 商品
    若不存在或为空，按业务逻辑默认当作 group 处理
    """
    _source_api = product.get("_source_api")
    if not _source_api:
        return True  # 缺失或为 None 默认视为 group 门槛商品

    if isinstance(_source_api, (list, tuple, set)):
        return "group" in _source_api
    return "group" in str(_source_api)


if __name__ == "__main__":
    while True:
        try:
            monitored_products = read_json(
                r'W:\project\python_project\easy_shop\app\web_front\monitored_products.json') or {}

            target_configs = []
            for search_keyword, conf in monitored_products.items():
                if not isinstance(conf, dict) or not conf.get('is_monitoring'):
                    continue
                kw_clean = (search_keyword or "").strip()
                if not kw_clean:
                    continue

                words = gen_goods_segment_info(name=kw_clean)
                max_price = conf.get('max_price') or 0
                target_configs.append({
                    "search_keyword": kw_clean,
                    "words": words,
                    "max_price": max_price
                })

            if target_configs:
                common_projection = {
                    "_id": 1,
                    "platform": 1,
                    "product_id": 1,
                    "name": 1,
                    "activity_price": 1,
                    "sku_info": 1,
                    "format_info": 1,
                    "need_update_sku": 1,
                    "need_reformat": 1,
                    "sku_status": 1,
                    "sku_retry_count": 1,
                    "_source_api": 1  # <--- 【修改 1】：必须加上这个字段投影！

                }

                # 1. 查询待抓取的二次兜底候选商品 (重试次数大于3且小于6，即 retry=4, 5)
                candidate_results = get_data_updated_within_24h(
                    hours=24,
                    limit=0,
                    extra_query={
                        "format_status": "success",
                        "need_update_sku": True,
                        "sku_retry_count": {"$gt": 3, "$lt": 6}  # 核心修改：只查大于3且小于6的
                    },
                    projection=common_projection
                )

                # 2. 查询 72 小时内所有已完成洗标的已确认商品，作为基准门槛计算池
                confirmed_results = get_data_updated_within_24h(
                    hours=72,
                    limit=0,
                    extra_query={
                        "format_status": "success",
                        "sku_info": {"$exists": True, "$ne": []},
                        "_source_api": {"$exists": True, "$ne": "group"}  # <--- 【优化】：在 DB 层初筛排除 group
                    },
                    projection=common_projection
                )

                # 3. 执行：基于 72H 第 5 名性价比基准门槛过滤 -> Top 10 优选 -> 全局去重
                filtered_results = filter_products_by_cost_effectiveness(
                    candidate_products=candidate_results,
                    confirmed_products=confirmed_results,
                    target_configs=target_configs,
                    max_top=10,  # 原 top_n=1 改为期望的最大截取上限（如 10 或 5）
                    ratio=1,  # 0.01 比例，达标 134 个时 134 * 0.01 -> ceil -> 2 个
                    benchmark_rank=500,
                    only_top_unit=False
                )

                if filtered_results:
                    batch_runner(filtered_results)
                else:
                    logger.info("[INFO] 所有候选商品均被基准门槛淘汰或无可用候选，跳过本轮执行")
            else:
                logger.info("[INFO] 当前无有效监控关键词，跳过本轮 SKU 抓取")
        except Exception as e:
            logger.info(f"[FATAL] 💥 主程序异常退出: {str(e)}")

        time.sleep(60)
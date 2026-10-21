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

logger.info("[SYSTEM] 正在初始化 RapidOCR 轻量级引擎...")
ocr = RapidOCR()


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


def _run_ocr(img_bgr):
    """执行 OCR 并返回结构化文本块列表（关闭方向分类器以提速）"""
    res, _ = ocr(img_bgr, use_cls=False)
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
    pink = (r >= 244) & (g >= 224) & (g <= 248) & (b >= 224) & (b <= 248) & ((r.astype(np.int16) - b.astype(np.int16)) >= 5)
    return float(np.mean(pink)) > 0.30


def _extract_price_fast(img_bgr, price_anchor, save_path=None):
    """截取价格单行区域，自适应切除右侧空白后直接整行识别（不切框、零特判）"""
    h_img, w_img = img_bgr.shape[:2]
    px, py, pw, ph = price_anchor

    # 1. 上下留 4px 余量防切边，初筛截取到窗口 0.85 宽度（避开最右侧关闭按钮 'X'）
    crop = img_bgr[max(0, py - 4):min(h_img, py + ph + 4), max(0, px - 4):int(w_img * 0.85)]

    # 2. 自适应切除右侧纯白背景：无论这行字有多长，精准裁到最后一个非白像素为止
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    non_white_cols = np.where(np.min(gray, axis=0) < 235)[0]
    if len(non_white_cols) > 0:
        crop = crop[:, :min(crop.shape[1], int(non_white_cols[-1]) + 6)]

    # =====================【仅在这里加这一行，放大 2 倍】=====================
    crop = cv2.resize(crop, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    # =====================================================================

    padded = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))

    # 3. 保存用于识别价格的图片，方便排查
    if save_path:
        ok, buf = cv2.imencode(".png", padded, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if ok:
            buf.tofile(save_path)

    # 4. 核心：use_det=False 关闭切框检测，整行直接识别（完全保持你原来的代码）
    res, _ = ocr(padded, use_det=False, use_cls=False)
    if not res:
        return ""

    text = str(res[0][0]).strip().replace("¥", "￥")
    idx = text.find("￥")
    return text[idx:].strip() if idx != -1 else text


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


def _parse_detail_page(img_bgr):
    """验证详情页并一次性解析价格锚点、规格分类及所有 SKU 框完整内容"""
    h_img, w_img = img_bgr.shape[:2]
    ocr_items = _run_ocr(img_bgr)

    confirm_y = next((i["box"][1] for i in ocr_items if "确定" in i["text"] and i["cy"] > h_img * 0.80), None)
    price_boxes = [i["box"] for i in ocr_items if ("￥" in i["text"] or "¥" in i["text"]) and (h_img * 0.18 < i["cy"] < h_img * 0.55)]

    if confirm_y is None or not price_boxes:
        return False, None, []

    price_boxes.sort(key=lambda b: (b[1] // 15, b[0]))
    price_anchor = price_boxes[0]
    _, py, _, _ = price_anchor

    # 确定规格区上边界：跳过价格、促销框、“已选”及数量加减行
    sku_top = py + 140
    for item in ocr_items:
        if "已选" in item["text"] and py < item["cy"] < py + 150:
            sku_top = max(sku_top, int(item["box"][1] + item["box"][3] + 50))

    # 分别构建已选中（浅粉）与未选中（浅灰）掩码
    b, g, r = cv2.split(img_bgr)
    pink_mask = ((r >= 244) & (g >= 224) & (g <= 248) & (b >= 224) & (b <= 248) & ((r.astype(np.int16) - b.astype(np.int16)) >= 5)).astype(np.uint8) * 255
    max_c, min_c = np.maximum(np.maximum(b, g), r), np.minimum(np.minimum(b, g), r)
    gray_mask = ((min_c >= 240) & (max_c <= 249) & ((max_c - min_c) <= 4)).astype(np.uint8) * 255

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

    # 匹配每个 SKU 框内的所有 OCR 文本块，按先上后下、先左后右排序拼接（兼容多行规格文本）
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

    if not sku_items:
        return True, price_anchor, []

    # 提取左侧未被 SKU 框消费的文字作为规格类型标题
    cat_top = max(py + 120, min(i["box"][1] for i in sku_items) - 65)
    categories = []
    for idx, item in enumerate(ocr_items):
        if idx not in used_ocr and cat_top <= item["cy"] <= confirm_y and item["box"][0] < w_img * 0.35:
            if not any(w in item["text"] for w in ("已选", "抢光", "恢复")):
                categories.append({"name": item["text"].rstrip(":：").strip(), "cy": item["cy"], "items": []})

    categories.sort(key=lambda c: c["cy"])
    if not categories:
        categories = [{"name": "规格", "cy": 0, "items": []}]

    for sku in sku_items:
        target_cat = categories[0]
        for cat in categories:
            if cat["cy"] < sku["cy"]:
                target_cat = cat
        target_cat["items"].append(sku)

    return True, price_anchor, [c for c in categories if c["items"]]


def get_pdd_sku_info(id, debug=True):
    """
    获取所有有效（非缺货）SKU 规格组合、价格及截图
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
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.08)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass

        # 1. 连续两次校验是否处于详情规格页
        is_detail, price_anchor, categories, current_img = False, None, [], None
        win_left, win_top = 0, 0
        for attempt in range(2):
            current_img, win_left, win_top = _capture_full_window(window)
            is_detail, price_anchor, categories = _parse_detail_page(current_img)
            if is_detail:
                break
            if attempt == 0:
                time.sleep(0.3)

        if not is_detail or not categories:
            return []

        cat_names = [c["name"] for c in categories]
        all_combos = list(itertools.product(*(c["items"] for c in categories)))
        results = []

        # 2. 极速遍历所有规格组合（自动跳过缺货规格）
        for idx, combo in enumerate(all_combos, start=1):
            try:
                combo_valid = True
                for target_sku in combo:
                    # 若规格文字为空（如初始极浅灰字未被OCR检出），直接跳过防止脏数据
                    if not target_sku["text"]:
                        combo_valid = False
                        break

                    bx, by, bw, bh = target_sku["box"]
                    if _is_box_selected(current_img, (bx, by, bw, bh)):
                        continue

                    # 点击前检测：若该规格当前已处于缺货置灰状态，直接放弃该组合
                    if _is_box_disabled(current_img, (bx, by, bw, bh)):
                        combo_valid = False
                        break

                    auto.Click(int(win_left + bx + bw // 2), int(win_top + by + bh // 2), waitTime=0)
                    time.sleep(0.05)
                    current_img, win_left, win_top = _capture_full_window(window)

                    # 点击后校验：若点击后并未变为浅粉选中状态（说明不可选或缺货），放弃该组合
                    if not _is_box_selected(current_img, (bx, by, bw, bh)):
                        combo_valid = False
                        break

                # 最终复核：确保当前组合中的所有规格均同时处于选中状态（防止子规格因父规格切换而被自动取消选中）
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

                record = {name: sku["text"] for name, sku in zip(cat_names, combo)}
                record["价格"] = price_text
                record["截图地址"] = screenshot_path
                results.append(record)
            except Exception:
                continue

        return results
    except Exception:
        return []


# ==========================================
# 🤖 RPA 核心引擎 (极致性能层)
# ==========================================
class UIActionEngine:
    def __init__(self, window_name):
        self.window_name = window_name
        self.window = None
        self.hwnd = 0

    def find_window(self, timeout=3):
        self.window = auto.WindowControl(searchDepth=1, Name=self.window_name)
        if self.window.Exists(timeout, 1):
            self.hwnd = self.window.NativeWindowHandle
            return True
        return False

    def force_bring_to_front(self):
        """【关键需求】基于 Win32 API 的绝对霸道置顶，防任何软件抢焦点"""
        if not self.hwnd:
            return
        try:
            # 1. 强行恢复窗口（如果被最小化）
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            # 2. 强行提到最前并设为 Topmost
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            # 3. 强行接管键盘焦点
            win32gui.SetForegroundWindow(self.hwnd)
            time.sleep(0.02)  # 给 Windows 窗口管理器 20ms 喘息时间
            # 4. 取消 Topmost 锁定（防止遮挡其他报错提示，但焦点已经稳了）
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception as e:
            logger.info(f"[DEBUG] 置顶操作受到系统保护拦截: {e}")

    def safe_click(self, x, y, desc=""):
        """无感瞬发点击，剥离所有冗余等待"""
        if desc:
            logger.info(f"[INFO] 执行点击: {desc} ({x}, {y})")
        auto.Click(int(x), int(y))

    def safe_input(self, text, desc=""):
        """极速输入：通过剪贴板瞬发，规避敲击延迟"""
        if desc:
            logger.info(f"[INFO] 执行输入: {desc}")
        pyperclip.copy(text)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.01)
        auto.SendKeys('{Enter}')

    def fast_screenshot_save(self, save_path, region=None):
        """使用 mss 极速保存全图/区域截图"""
        if region is None:
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

        monitor = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
        sct_img = sct.grab(monitor)
        img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
        cv2.imwrite(save_path, img_cv)

    def safe_ocr_wait(self, target_texts, timeout=5, region=None):
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

        wrap_modal = self.window.GroupControl(ClassName="wrap")
        if wrap_modal.Exists(0, 0):
            return False
        return True

    def set_transfer_goods_id(self, goods_id, max_attempts=3):
        """
        【输入与双向绑定保障】前置读取与强制清空 + 无Ctrl真实按键驱动 + 严格二次校验重试
        """
        import ctypes
        target_str = str(goods_id).strip()
        edit_box = self.window.EditControl()
        if not edit_box.Exists(2, 1):
            return False, "无法定位中转小程序的输入框"

        def _force_clear_box():
            """内部辅助：优先用 ValuePattern 清空，再读取实际残留值并用物理退格彻底清零"""
            try:
                vp = edit_box.GetValuePattern()
                if vp:
                    vp.SetValue("")
                    time.sleep(0.02)
            except Exception:
                pass

            residual = self.get_control_text(edit_box).strip()
            if residual:
                edit_box.Click(simulateMove=False)
                time.sleep(0.03)
                clear_len = len(residual) + 6
                auto.SendKeys('{End}' + '{Back}' * clear_len + '{Delete}' * 6, interval=0.005)
                time.sleep(0.03)

        current_val = ""
        try:
            for attempt in range(1, max_attempts + 1):
                edit_box.Click(simulateMove=False)
                time.sleep(0.04)

                # 1. 输入前绝不盲信全选：先读取/使用 ValuePattern 与退格强制清空输入框
                _force_clear_box()

                # 2. 直接通过真实按键输入目标ID（不使用 Ctrl+V，彻底规避 Ctrl 卡死风险）
                auto.SendKeys(target_str, interval=0.01)
                time.sleep(0.02)

                # 3. 敲入空格再退格，确保触发微信小程序的 bindinput 双向数据绑定
                auto.SendKeys(' {Back}')
                time.sleep(0.04)

                # 4. 严格进行二次校验
                current_val = self.get_control_text(edit_box).strip()
                if current_val == target_str:
                    return True, ""

                logger.info(
                    f"[WARN] 输入校验不匹配 (期望: '{target_str}', 实际: '{current_val}')，准备清空并执行第 {attempt} 次重试")
                _force_clear_box()
                time.sleep(0.05)

            return False, f"中转站输入框赋值校验失败: 界面当前值为 '{current_val}'，并非目标商品 ID '{target_str}'"
        finally:
            # 硬件级兜底：强制释放左右 Ctrl 键（VK_CONTROL=0x11, VK_LCONTROL=0xA2, VK_RCONTROL=0xA3, KEYEVENTF_KEYUP=0x0002）
            try:
                for vk in (0x11, 0xA2, 0xA3):
                    ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
            except Exception:
                pass

    def process_single_goods(self, goods_id, index, consecutive_successes=0):
        # --- 步骤 1：确立中转站据点 ---
        self.window_name = TRANSFER_APP_NAME
        if not self.find_window(timeout=1):
            logger.info(f"[SYSTEM] 正在唤醒中转小程序: {TRANSFER_APP_NAME}")
            os.startfile(TRANSFER_SHORTCUT_PATH)
            if not self.find_window(timeout=5):
                return False, [], "中转小程序启动失败"

        self.force_bring_to_front()

        # --- 步骤 1.5：环境自愈（确保恢复至【情况2】） ---
        if not self.ensure_transfer_clean_state():
            return False, [], "中转小程序存在无法自动关闭的弹窗，环境未能恢复至正常状态"

        # --- 步骤 2：精确写入商品 ID 并严格比对 ---
        logger.info(f"[TASK] [{index}] 步骤 1/4: 向中转站写入商品 ID")
        input_ok, input_err = self.set_transfer_goods_id(goods_id)
        if not input_ok:
            return False, [], input_err

        logger.info(f"[TASK] [{index}] 步骤 2/4: 触发 API 跳转")
        jump_btn = self.window.ButtonControl(Name="跳转到该商品")
        if not jump_btn.Exists(1, 1):
            return False, [], "无法定位中转小程序的跳转按钮"

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
            return False, [], "未检测到拼多多窗口弹出（跳转可能失败）"

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

        # 将区域放大为底部 1/4，这样既能盖住详情页的“收藏/客服/店铺”，也能盖住 SKU 弹窗底部的“确定”
        capture_region = (int(rect.left), int(rect.bottom - h // 4), int(w), int(h // 4))
        monitor = {"left": capture_region[0], "top": capture_region[1],
                   "width": capture_region[2], "height": capture_region[3]}

        start_time = time.time()
        detail_entered = False
        sku_ready = False

        # 极限轮询：最多尝试 10 秒
        while time.time() - start_time < 10:
            try:
                sct_img = sct.grab(monitor)
                img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
                result, _ = ocr(img_cv)

                if result:
                    detected_text = "".join([line[1] for line in result if len(line) >= 2])

                    # 状态 B：如果有 收藏/客服/店铺 中的一个且没有 确定，说明在详情页，需要点击购买
                    if any(kw in detected_text for kw in ["收藏", "客服", "店铺"]) and "确定" not in detected_text:
                        detail_entered = True
                        self.safe_click(rect.right - 60, rect.bottom - 25, "动态点击购买")
                        time.sleep(0.3)
                        continue

                    # 状态 A：点击过购买后，识别到 确定/请选择/已选，说明已展开 SKU 详情页
                    if detail_entered and any(kw in detected_text for kw in ["确定", "请选择", "已选"]):
                        sku_ready = True
                        break

            except Exception:
                pass

        # 轮询结束，进行结果清算
        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.click_home_button()
            return False, [], "进入详情页失败"

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.click_home_button()
            return False, [], "SKU面板未完全展开或点击全部失效"

        # --- 步骤 4：使用新逻辑遍历提取所有 SKU 规格组合及价格 ---
        logger.info(f"[TASK] [{index}] 步骤 4/4: 正在遍历提取所有 SKU 规格组合与价格...")
        sku_info = get_pdd_sku_info(id=goods_id, debug=DEBUG_MODE)
        if not sku_info:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            return False, [], "未能提取到有效的SKU规格组合数据"

        logger.info(f"[TASK] [{index}] 🎯 成功提取 {len(sku_info)} 个规格组合。")

        # 连续成功清理策略
        if (consecutive_successes + 1) % 500 == 0:
            self.close_current_window()
            logger.info("[INFO] 循环连轴转达到 500 次，重启(关闭)拼多多小程序释放资源。")

        return True, sku_info, ""

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
            success, sku_info, error_msg = bot.process_single_goods(goods_id, i, consecutive_successes)

            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

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
                    product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments={"sku_retry_count": 1},
                        upsert=False
                    )
                    logger.info(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg}")
                    solve_pdd_captcha()
                    logger.info("-" * 40)
                    time.sleep(10)
                    batch_counts["failed"] += 1
                    consecutive_successes = 0
                    consecutive_failures += 1

        except Exception as e:
            logger.info(f"[FATAL] 💥 发生严重异常: {str(e)}")
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

        # 判断是否连续失败 5 次
        if consecutive_failures >= 5:
            logger.info(f"[WARN] ⚠️ 已连续发生 5 次失败，主动触发熔断机制，跳过本轮剩余的 {len(product_list) - i} 个任务！")
            break

    logger.info("=" * 50)
    logger.info(f"[SYSTEM] 🎉 批量任务完毕！ 成功: {batch_counts['success']} | 跳过: {batch_counts['skipped']} | 失败: {batch_counts['failed']}")
    logger.info("=" * 50)

def get_data_updated_within_24h(limit=0, extra_query=None, projection=None):
    """
    查询最近更新的拼多多商品数据（基于 updated_at 字段，强制过滤 platform='pdd' 防止非拼多多商品引发 RPA 熔断）。
    """
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=12)

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

if __name__ == "__main__":
    while True:
        try:
            monitored_products = read_json(
                r'W:\project\python_project\easy_shop\app\web_front\monitored_products.json') or {}
            search_keywords = [
                k for k, v in monitored_products.items()
                if isinstance(v, dict) and v.get('is_monitoring')
            ]

            target_category_list = []
            for search_keyword in search_keywords:
                kw_clean = (search_keyword or "").strip().lower()
                if not kw_clean:
                    continue
                words = gen_goods_segment_info(name=search_keyword)
                clean_words = []
                if isinstance(words, (list, tuple, set)):
                    clean_words = [str(w).strip().lower() for w in words if str(w).strip()]
                if not clean_words:
                    clean_words = [kw_clean]
                target_category_list.append(clean_words)

            logger.info(target_category_list)

            if target_category_list:
                results = get_data_updated_within_24h(
                    limit=0,
                    extra_query={
                        "format_status": "success",
                        "need_update_sku": True,
                        "$or": [
                            {"sku_retry_count": {"$lte": 3}},
                            {"sku_retry_count": {"$exists": False}}
                        ]
                    },
                    projection={
                        "_id": 1,
                        "platform": 1,
                        "product_id": 1,
                        "name": 1,
                        "sku_info": 1,
                        "need_update_sku": 1,
                        "sku_retry_count": 1
                    }
                )

                # 过滤逻辑：
                # 1. 满足任意一组非空分词规则（统一小写匹配，与前端搜索保持一致）
                # 2. need_update_sku 为 True 且 sku_retry_count <= 3
                filtered_results = [
                    item for item in results
                    if bool(item.get("need_update_sku"))
                       and (item.get("sku_retry_count") or 0) <= 3
                       and (item_name := (item.get("name") or "").lower())
                       and any(
                        all(keyword in item_name for keyword in keywords_group)
                        for keywords_group in target_category_list
                    )
                ]

                batch_runner(filtered_results)
            else:
                logger.info("[INFO] 当前无有效监控关键词，跳过本轮 SKU 抓取")
        except Exception as e:
            logger.info(f"[FATAL] 💥 主程序异常退出: {str(e)}")
        time.sleep(600)
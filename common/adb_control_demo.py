import os
import re
import time
import math
import struct
import subprocess
import itertools
from datetime import datetime, timedelta, timezone
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

# === 高性能图像与OCR依赖 ===
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR
from common.common_utils import read_json, setup_logger

# === 项目内公共依赖 ===
logger = setup_logger(app_name="pdd_automation_adb")

from common.goods_common_utils import gen_goods_segment_info

from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

# ==========================================
# ⚙️ 全局配置区
# ==========================================
ADB_PATH = r"E:\chrome\platform-tools-latest-windows\platform-tools\adb.exe"
VIA_PACKAGE = "mark.via"
PDD_PACKAGE = "com.xunmeng.pinduoduo"

DEBUG_MODE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKU_DEBUG_DIR = os.path.join(BASE_DIR, "sku_debug_images")
ERROR_DIR = os.path.join(BASE_DIR, "results_error_adb")

for d in [SKU_DEBUG_DIR, ERROR_DIR]:
    os.makedirs(d, exist_ok=True)

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

# 全局异步写盘线程池（用于后台极速保存 1800x2880 调试截图，不阻塞 RPA 点击主线程）
_img_save_executor = ThreadPoolExecutor(max_workers=2)


def _async_save_png(img_bgr, save_path):
    """后台线程异步编码并保存 PNG 图像"""
    try:
        ok, buf = cv2.imencode(".png", img_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if ok:
            buf.tofile(save_path)
    except Exception:
        pass


# ==========================================
# 📱 ADB 基础控制层 (支持多应用与极速感知)
# ==========================================
def get_foreground_package():
    """
    通过 Window Focus 严格获取当前唯一获得前台焦点的应用（绝不误判后台任务，耗时<0.1s）
    """
    try:
        # 直接抓取当前正与屏幕交互的焦点窗口 (mCurrentFocus / mFocusedApp)
        res = subprocess.run(
            [ADB_PATH, "shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=1.5
        )
        out = res.stdout or ""
        if PDD_PACKAGE in out:
            return PDD_PACKAGE
        if VIA_PACKAGE in out:
            return VIA_PACKAGE
    except Exception:
        pass

    # 极少数机型不支持 grep 时，轻量兜底 displays
    try:
        res = subprocess.run(
            [ADB_PATH, "shell", "dumpsys", "window", "displays"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=1.5
        )
        if res.stdout:
            for line in res.stdout.splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    if PDD_PACKAGE in line:
                        return PDD_PACKAGE
                    if VIA_PACKAGE in line:
                        return VIA_PACKAGE
                    break
    except Exception:
        pass

    return ""


def is_app_in_foreground(package_name):
    """检测指定应用是否为当前前台应用"""
    return get_foreground_package() == package_name


def stop_app(package_name):
    """仅强制停止应用（耗时 <0.1s，配合 am start VIEW 一步冷启动，省去 monkey 唤醒耗时）"""
    subprocess.run(
        f'"{ADB_PATH}" shell am force-stop {package_name}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def ensure_app_foreground(package_name):
    """保证应用处于前台"""
    if not is_app_in_foreground(package_name):
        logger.info(f"[ADB] 正在唤起/切至前台: {package_name}")
        cmd = f'"{ADB_PATH}" shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1'
        subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")

        for _ in range(10):
            if is_app_in_foreground(package_name):
                time.sleep(0.5)
                break
            time.sleep(0.5)


def restart_app(package_name):
    """强制停止与重启应用"""
    logger.info(f"[ADB] 正在强制停止并重启: {package_name}")
    subprocess.run(
        f'"{ADB_PATH}" shell am force-stop {package_name}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    subprocess.run(
        f'"{ADB_PATH}" shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    for _ in range(10):
        if is_app_in_foreground(package_name):
            time.sleep(0.5)
            break
        time.sleep(0.5)


# ==========================================
# 🔍 SKU 规格与价格极速提取模块
# ==========================================
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


def _get_box_color_metrics(img_bgr, box):
    """
    计算单个 SKU 框的各项颜色特征占比，供选中态判断与日志排查使用：
    - pink_ratio: 微信小程序浅粉背景占比
    - theme_right_ratio: 框体右侧 70% 区域的主题色(鲜红/黑金棕色)像素占比
    - black_text_ratio: 框体内部深黑色文字像素占比（排除彩色文字）
    """
    x, y, w, h = box
    roi = img_bgr[max(0, y):y + h, max(0, x):x + w]
    if roi.size == 0 or w <= 8 or h <= 8:
        return 0.0, 0.0, 0.0

    b, g, r = cv2.split(roi)

    # 1. 小程序浅粉底色特征
    pink_mask = (r >= 244) & (g >= 220) & (g <= 248) & (b >= 220) & (b <= 248) & (
            (r.astype(np.int16) - b.astype(np.int16)) >= 5
    )
    pink_ratio = float(np.mean(pink_mask))

    # 2. App 端主题字/边框特征（取右侧 70%，支持普通鲜红 + 旗舰店黑金/金棕色）
    x_offset = int(w * 0.30)
    r_right = r[:, x_offset:]
    g_right = g[:, x_offset:]
    b_right = b[:, x_offset:]

    r_r16, g_r16, b_r16 = r_right.astype(np.int16), g_right.astype(np.int16), b_right.astype(np.int16)
    # 标准红色特征
    is_std_red = (r_right >= 170) & (g_right <= 120) & (b_right <= 120) & ((r_r16 - g_r16) >= 60)
    # 旗舰店黑金/金棕色特征 (R明显大于B，且R略高于G)
    is_gold_brown = (r_right >= 110) & (b_right <= 135) & ((r_r16 - b_r16) >= 35) & ((r_r16 - g_r16) >= 15)

    theme_right_mask = is_std_red | is_gold_brown
    theme_right_ratio = float(np.mean(theme_right_mask)) if r_right.size > 0 else 0.0

    # 3. 内部纯深黑色文字特征（内缩避开边框）
    # 核心安全优化：真正的黑色无明显色偏(abs(r-b)小)，排除了带有红棕色调的深色文字
    pad_y = max(2, int(h * 0.15))
    r_inner = r[pad_y:h - pad_y, x_offset:max(x_offset + 1, w - 4)]
    g_inner = g[pad_y:h - pad_y, x_offset:max(x_offset + 1, w - 4)]
    b_inner = b[pad_y:h - pad_y, x_offset:max(x_offset + 1, w - 4)]

    r_i16 = r_inner.astype(np.int16)
    b_i16 = b_inner.astype(np.int16)
    black_mask = (r_inner < 105) & (g_inner < 105) & (b_inner < 105) & (np.abs(r_i16 - b_i16) < 25)
    black_text_ratio = float(np.mean(black_mask)) if r_inner.size > 0 else 0.0

    return pink_ratio, theme_right_ratio, black_text_ratio


def _is_box_selected(img_bgr, box):
    """
    同时兼容多种选中态视觉风格：
    1. 微信小程序风格：浅粉底色填充 (pink_ratio > 0.30)
    2. 拼多多 App 风格：白底 + 红框/金棕框 + 对应主题色字
    """
    pink_ratio, theme_right_ratio, black_text_ratio = _get_box_color_metrics(img_bgr, box)
    if pink_ratio > 0.30:
        return True
    if theme_right_ratio > 0.010 and black_text_ratio < 0.015:
        return True
    return False


def _is_box_disabled(img_bgr, box):
    """判断未选中的 SKU 框是否处于缺货/置灰不可选状态（灰底浅灰字，无深色或红色前景像素）"""
    if _is_box_selected(img_bgr, box):
        return False
    x, y, w, h = box
    pad = max(4, int(min(w, h) * 0.1))
    roi = img_bgr[max(0, y + pad):y + h - pad, max(0, x + pad):x + w - pad]
    if roi.size == 0:
        return True
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # 正常可选规格文字为深黑色(灰度<150)，缺货规格文字为浅灰(灰度≈204)
    return int(np.sum(gray < 150)) < 10


def _extract_price_fast(img_bgr, price_anchor, save_path=None):
    """先用常规引擎整行快速识别，若失败立即激活强力配置引擎重检（自适应高分屏边距）"""
    h_img, w_img = img_bgr.shape[:2]
    px, py, pw, ph = price_anchor

    # 自适应高分屏上下左右边距，防止切换规格后价格字号微变导致截断
    pad_y = max(8, int(ph * 0.35))
    pad_x = max(10, int(ph * 0.40))
    crop = img_bgr[max(0, py - pad_y):min(h_img, py + ph + pad_y), max(0, px - pad_x):int(w_img * 0.85)]
    if crop.size == 0:
        return ""

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    non_white_cols = np.where(np.min(gray, axis=0) < 235)[0]
    if len(non_white_cols) > 0:
        crop = crop[:, max(0, int(non_white_cols[0]) - 6):min(crop.shape[1], int(non_white_cols[-1]) + 8)]

    crop_2x = cv2.resize(crop, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(crop_2x, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=(255, 255, 255))

    # ==================== 1. 尝试常规极速通道 (ocr_fast) ====================
    res, _ = ocr_fast(padded, use_det=False, use_cls=False)
    if res:
        text = re.sub(r"[¥?？]", "￥", str(res[0][0]).strip())
        if re.match(r"^\d", text):
            text = "￥" + text

        idx = text.find("￥")
        if idx != -1 and re.search(r"\d", text[idx:]):
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return text[idx:].replace(" ", "").strip()

    # ==================== 2. 激活强力配置 (ocr_strong 介入) ====================
    res_strong, _ = ocr_strong(padded, use_det=True, use_cls=False)
    if res_strong:
        try:
            res_strong.sort(key=lambda line: line[0][0][0])
        except Exception:
            pass
        full_text = "".join([re.sub(r"[¥?？]", "￥", str(line[1]).strip()) for line in res_strong])

        if re.match(r"^\d", full_text):
            full_text = "￥" + full_text

        idx = full_text.find("￥")
        if idx != -1 and re.search(r"\d", full_text[idx:]):
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return full_text[idx:].replace(" ", "").strip()

        nums = re.findall(r"\d+\.\d+|\d+", full_text)
        if nums:
            if save_path:
                cv2.imencode(".png", padded)[1].tofile(save_path)
            return f"￥{nums[0]}"

    return ""


def _parse_detail_page(img_bgr, min_cat_top=None):
    """验证详情页并一次性解析价格锚点、规格分类、所有 SKU 框及是否完全展开状态（带详细诊断日志与错误原因）"""
    h_img, w_img = img_bgr.shape[:2]
    scale = max(1.0, w_img / 500.0)

    ocr_items = _run_ocr(img_bgr, engine=ocr_fast)

    # 兼容“确定”、“再来一单”、“确认”、“立即支付”等不同场景下的底部确认按钮
    CONFIRM_KEYWORDS = ("确定", "再来一单")
    confirm_item = next(
        (
            i
            for i in ocr_items
            if any(kw in i["text"] for kw in CONFIRM_KEYWORDS) and i["cy"] > h_img * 0.80
        ),
        None,
    )
    confirm_y = confirm_item["box"][1] if confirm_item else None
    confirm_h = confirm_item["box"][3] if confirm_item else None

    # 1. 常规寻找价格
    price_boxes = [
        (i["box"], i["text"]) for i in ocr_items
        if ("￥" in i["text"] or "¥" in i["text"] or bool(re.search(r"[?？]\s*\d", i["text"])))
           and (h_img * 0.18 < i["cy"] < h_img * 0.55)
    ]

    # 2. 如果确定按钮存在但 price_boxes 漏检，让 ocr_strong 介入
    if confirm_y is not None and not price_boxes:
        logger.info("[PARSE-DEBUG] 常规 OCR 未检出价格锚点，正在唤醒 ocr_strong 重检价格区...")
        strong_items = _run_ocr(img_bgr[int(h_img * 0.18):int(h_img * 0.55), :], engine=ocr_strong)
        for item in strong_items:
            bx, by, bw, bh = item["box"]
            real_box = (bx, int(by + h_img * 0.18), bw, bh)
            t = item["text"]
            if any(sym in t for sym in ["￥", "¥", "?", "？", "券后", "折后", "拼单"]) and re.search(r"\d", t):
                price_boxes.append((real_box, t))

    if confirm_y is None or not price_boxes:
        logger.info(
            f"[PARSE-WARN] 页面校验未通过: confirm_y={confirm_y}, price_boxes数量={len(price_boxes)} "
            f"(画面尺寸: {w_img}x{h_img})"
        )
        return False, None, [], False, "页面校验未通过(未找到价格或确定按钮)"

    price_boxes.sort(key=lambda b: (b[0][1] // max(15, int(h_img * 0.015)), b[0][0]))
    price_anchor, price_init_text = price_boxes[0]
    px, py, pw, ph = price_anchor

    # 基础下界：价格锚点下方约 5.5% 屏幕高度
    sku_top = py + int(h_img * 0.055)
    for item in ocr_items:
        # 锚定“已选 / 请选择”行底部
        if any(w in item["text"] for w in ("已选", "请选择")) and py < item["cy"] < py + int(h_img * 0.12):
            sku_top = max(sku_top, int(item["box"][1] + item["box"][3] + int(h_img * 0.012)))
        # 锚定顶部卡片内加减号 "- 1 +" 行底部
        if item["text"].strip() in ("-", "+", "－", "＋") and py < item["cy"] < py + int(h_img * 0.14):
            sku_top = max(sku_top, int(item["box"][1] + item["box"][3] + int(h_img * 0.008)))

    if min_cat_top is not None:
        sku_top = max(sku_top, int(min_cat_top))

    logger.info(
        f"[PARSE-INFO] 基础锚点定位成功: 分辨率={w_img}x{h_img} | 价格锚点='{price_init_text}' {price_anchor} | "
        f"确定按钮Y={confirm_y} | 计算得 sku_top={sku_top}"
    )

    # 三通道掩码切框（浅灰未选框 + 小程序浅粉选中框 + App红框/黑金棕框选中框）
    b, g, r = cv2.split(img_bgr)
    r_i16, g_i16, b_i16 = r.astype(np.int16), g.astype(np.int16), b.astype(np.int16)

    pink_mask = ((r >= 244) & (g >= 224) & (g <= 248) & (b >= 224) & (b <= 248) & (
            (r_i16 - b_i16) >= 5)).astype(np.uint8) * 255
    max_c, min_c = np.maximum(np.maximum(b, g), r), np.minimum(np.minimum(b, g), r)
    gray_mask = ((min_c >= 238) & (max_c <= 250) & ((max_c - min_c) <= 5)).astype(np.uint8) * 255

    # 兼容标准红框与黑金/旗舰店金棕色边框
    is_std_red = (r >= 160) & (g <= 135) & (b <= 135) & ((r_i16 - g_i16) >= 50)
    is_gold_brown = (r >= 110) & (b <= 135) & ((r_i16 - b_i16) >= 35) & ((r_i16 - g_i16) >= 15)
    theme_border_mask = (is_std_red | is_gold_brown).astype(np.uint8) * 255

    combined_sku_mask = cv2.bitwise_or(pink_mask, gray_mask)

    raw_boxes = []
    kernel = np.ones((5, 5), np.uint8)
    min_box_w = max(42, int(w_img * 0.035))
    min_box_h = max(25, int(h_img * 0.018))
    max_box_h = max(90, int(h_img * 0.055))
    dedup_tol = max(10, int(w_img * 0.015))

    # 1) 提取实心填充类规格框 (gray_mask / pink_mask)
    for mask_name, mask in (("gray", gray_mask), ("pink", pink_mask)):
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if sku_top <= y and (y + h) <= confirm_y and min_box_w < w < w_img * 0.96 and min_box_h <= h <= max_box_h:
                if float(np.mean(mask[y:y + h, x:x + w] > 0)) > 0.35:
                    if not any(abs(x - rx) < dedup_tol and abs(y - ry) < dedup_tol for rx, ry, _, _ in raw_boxes):
                        raw_boxes.append((x, y, w, h))

    # 2) 提取 App 端主题色(红框/金棕框)白底类已选中规格框
    theme_kernel = np.ones((7, 7), np.uint8)
    closed_theme = cv2.morphologyEx(theme_border_mask, cv2.MORPH_CLOSE, theme_kernel)
    cnts_theme, _ = cv2.findContours(closed_theme, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts_theme:
        x, y, w, h = cv2.boundingRect(c)
        if sku_top <= y and (y + h) <= confirm_y and min_box_w < w < w_img * 0.96 and min_box_h <= h <= max_box_h:
            if _is_box_selected(img_bgr, (x, y, w, h)):
                if not any(abs(x - rx) < dedup_tol and abs(y - ry) < dedup_tol for rx, ry, _, _ in raw_boxes):
                    raw_boxes.append((x, y, w, h))

    row_tol = max(18, int(h_img * 0.012))
    raw_boxes.sort(key=lambda b: (b[1] // row_tol, b[0]))

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
        frags.sort(key=lambda f: (f[0] // max(16, int(h_img * 0.01)), f[1]))
        sku_text = "".join(t for _, _, t in frags)
        sku_items.append({"box": (bx, by, bw, bh), "cy": by + bh / 2.0, "text": sku_text})

    logger.info(f"[PARSE-INFO] 共切出 {len(sku_items)} 个候选 SKU 规格框: {[s['text'] for s in sku_items]}")

    is_fully_expanded = False
    if confirm_item:
        above_items = [i for i in ocr_items if i is not confirm_item and i["cy"] < confirm_y]
        if above_items:
            nearest_bottom = max(i["box"][1] + i["box"][3] for i in above_items)
            gap = confirm_y - nearest_bottom
            if gap > confirm_h:
                is_fully_expanded = True
            logger.info(
                f"[PARSE-INFO] 展开态检测: 最近文字底边={nearest_bottom}, 确定顶边={confirm_y}, "
                f"间距gap={gap}, 确定高度confirm_h={confirm_h} -> is_fully_expanded={is_fully_expanded}"
            )

    if not sku_items:
        logger.info("[PARSE-WARN] 未切出任何 SKU 规格框！")
        return True, price_anchor, [], is_fully_expanded, "未切出任何 SKU 规格框"

    min_sku_y = min(i["box"][1] for i in sku_items)
    cat_top = max(py + ph, min(sku_top - int(h_img * 0.025), min_sku_y - int(h_img * 0.055)))
    if min_cat_top is not None:
        cat_top = max(cat_top, int(min_cat_top))

    categories = []
    for idx, item in enumerate(ocr_items):
        if idx not in used_ocr and cat_top <= item["cy"] <= confirm_y and item["box"][0] < w_img * 0.25:
            ox, oy, ow, oh = item["box"]
            roi_mask = combined_sku_mask[max(0, oy):min(h_img, oy + oh), max(0, ox):min(w_img, ox + ow)]
            if roi_mask.size > 0 and float(np.mean(roi_mask > 0)) > 0.20:
                continue
            if not any(w in item["text"] for w in ("已选", "请选择", "抢光", "恢复", "正品", "保障", "品牌")):
                categories.append({"name": item["text"].rstrip(":：").strip(), "cy": item["cy"], "items": []})

    categories.sort(key=lambda c: c["cy"])
    if not categories:
        logger.info(f"[PARSE-WARN] 未识别到任何规格分类名 (cat_top={cat_top})")
        return True, price_anchor, [], is_fully_expanded, "未识别到任何规格分类名"

    unassigned_skus = []
    for sku in sku_items:
        target_cat = None
        for cat in categories:
            if cat["cy"] < sku["cy"]:
                target_cat = cat
        if target_cat is not None:
            target_cat["items"].append(sku)
        else:
            unassigned_skus.append(sku["text"])

    if unassigned_skus:
        logger.info(f"[PARSE-WARN] 存在高于首个分类 '{categories[0]['name']}' 的未归属规格框: {unassigned_skus}")

    valid_categories = [c for c in categories if c["items"]]
    if not valid_categories:
        logger.info("[PARSE-WARN] 所有分类下的规格选项均为空")
        return True, price_anchor, [], is_fully_expanded, "分类下的有效规格框为空"

    for c in valid_categories:
        item_summaries = []
        for s in c["items"]:
            sel = _is_box_selected(img_bgr, s["box"])
            dis = _is_box_disabled(img_bgr, s["box"]) if not sel else False
            status_tag = "已选" if sel else ("缺货" if dis else "可选")
            item_summaries.append(f"{s['text']}({status_tag})")
        logger.info(f"[PARSE-CAT] 分类 【{c['name']}】 (cy={c['cy']:.0f}) -> {len(c['items'])} 个选项: {item_summaries}")

    return True, price_anchor, valid_categories, is_fully_expanded, ""

def get_pdd_sku_info(bot, id, debug=True, initial_img=None, extra_info=None):
    """
    通过 ADB 截图与点击获取所有有效（非缺货）SKU 规格组合、价格及截图
    支持复用状态机已捕获的首帧高清图 initial_img，并使用异步存图加速遍历
    """
    try:
        # 1. 校验是否处于详情规格页（优先复用传入的 initial_img，省去首次重复截屏）
        is_detail, price_anchor, top_categories, current_img = False, None, [], None
        is_fully_expanded = False
        last_parse_err = ""

        for attempt in range(2):
            if attempt == 0 and initial_img is not None:
                current_img = initial_img
            else:
                current_img = bot.get_screenshot_cv()

            if current_img is not None:
                is_detail, price_anchor, top_categories, is_fully_expanded, parse_err = _parse_detail_page(current_img)
                last_parse_err = parse_err
                if is_detail and top_categories:
                    break
            if attempt == 0:
                logger.info("[SKU-INFO] 第 1 次解析未获取到完整分类，等待 0.25s 后重截重试...")
                time.sleep(0.25)

        if not is_detail or not top_categories or current_img is None:
            err_msg = last_parse_err or "初始详情规格页解析失败或分类为空"
            logger.info(f"[SKU-ERROR] {err_msg}，终止提取")
            if extra_info is not None:
                extra_info["sku_error"] = err_msg
                # 命中“未切出任何 SKU 规格框”，设置不可重试标记
                if "未切出任何 SKU 规格框" in err_msg:
                    extra_info["no_sku_boxes"] = True
                    extra_info["force_retry_count"] = 10
            return []

        h_img, w_img = current_img.shape[:2]
        first_cat_top = int(top_categories[0]["cy"] - int(h_img * 0.02))

        top_cat_names = {c["name"] for c in top_categories}
        pre_selected_specs = {}  # 保存底部预选成功的规格
        categories = top_categories

        # =========================================================================
        # 步骤 1.5 & 1.8：根据规格展开状态决定是否执行长列表上下滑动探测
        # =========================================================================
        if not is_fully_expanded:
            logger.info("[SKU-SCROLL] 规格未完全展开，开始下滑探测底部隐藏规格...")
            bot.scroll_sku_down()
            time.sleep(0.30)

            btm_img = bot.get_screenshot_cv()
            if btm_img is not None:
                is_detail_btm, _, btm_categories, _, _ = _parse_detail_page(btm_img, min_cat_top=first_cat_top)

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
                                    bot.click_point(
                                        int(bx + bw // 2),
                                        int(by + bh // 2),
                                        desc=f"预选底部规格 [{cat_name}:{sku['text']}]"
                                    )
                                    time.sleep(0.10)
                                    btm_img = bot.get_screenshot_cv()
                                    if btm_img is not None and _is_box_selected(btm_img, (bx, by, bw, bh)):
                                        selected_text = sku["text"]
                                        break

                            if selected_text:
                                pre_selected_specs[cat_name] = selected_text
                                logger.info(f"[SKU-SCROLL] 已锁定底部规格: {cat_name} = {selected_text}")
                            elif cat_name not in top_cat_names:
                                logger.info(f"[WARN] 底部隐藏规格分类 '{cat_name}' 无可用选项可锁定，为防脏数据已中止提取")
                                return []

            # 【上滑复位】滑回顶部，重新解析顶部规格坐标
            logger.info("[SKU-SCROLL] 正在上滑复位回顶部规格列表...")
            bot.scroll_sku_up()
            time.sleep(0.30)

            current_img = bot.get_screenshot_cv()
            if current_img is None:
                return []
            is_detail, price_anchor, categories, _, _ = _parse_detail_page(current_img, min_cat_top=first_cat_top)
            if not is_detail or not categories:
                logger.info("[SKU-ERROR] 上滑复位后无法解析顶部规格分类")
                return []
        else:
            logger.info("[INFO] ⚡ 检测到规格已完全展开，已智能跳过上下滑动探测！")

        cat_names = [c["name"] for c in categories]
        all_combos = list(itertools.product(*(c["items"] for c in categories)))
        logger.info(f"[SKU-TRAVERSE] 准备遍历规格维度 {cat_names}，共计 {len(all_combos)} 种笛卡尔积组合")
        results = []

        # 2. 极速遍历顶部所有规格组合（自动跳过缺货规格）
        for idx, combo in enumerate(all_combos, start=1):
            try:
                combo_desc = {name: s["text"] for name, s in zip(cat_names, combo)}
                logger.info(f"[SKU-COMBO] [{idx}/{len(all_combos)}] 尝试组合: {combo_desc}")

                combo_valid = True
                for cat_name, target_sku in zip(cat_names, combo):
                    if not target_sku["text"]:
                        logger.info(f"  ├─ [跳过] 分类 '{cat_name}' 下存在空文本规格框: {target_sku['box']}")
                        combo_valid = False
                        break

                    bx, by, bw, bh = target_sku["box"]
                    if _is_box_selected(current_img, (bx, by, bw, bh)):
                        logger.info(f"  ├─ [保持已选] [{cat_name}: {target_sku['text']}] 当前已处于选中态")
                        continue

                    if _is_box_disabled(current_img, (bx, by, bw, bh)):
                        logger.info(f"  ├─ [跳过缺货] [{cat_name}: {target_sku['text']}] 处于置灰缺货状态")
                        combo_valid = False
                        break

                    bot.click_point(
                        int(bx + bw // 2),
                        int(by + bh // 2),
                        desc=f"选择规格 [{cat_name}: {target_sku['text']}]"
                    )
                    time.sleep(0.06)
                    current_img = bot.get_screenshot_cv()
                    if current_img is None:
                        logger.info("  ├─ [异常] 点击后获取截图失败")
                        combo_valid = False
                        break

                    if not _is_box_selected(current_img, (bx, by, bw, bh)):
                        p_ratio, r_ratio, b_ratio = _get_box_color_metrics(current_img, (bx, by, bw, bh))
                        logger.info(
                            f"  ├─ [未选中/不可点] 点击 [{cat_name}: {target_sku['text']}] 后未检测到选中态特征 "
                            f"(pink={p_ratio:.3f}, red_right={r_ratio:.3f}, black_text={b_ratio:.3f})"
                        )
                        combo_valid = False
                        break

                if not combo_valid:
                    continue

                if not all(_is_box_selected(current_img, s["box"]) for s in combo):
                    logger.info("  ├─ [跳过] 组合联动互斥，部分规格在点击后失去选中态")
                    continue

                screenshot_path = ""
                price_screenshot_path = ""
                if debug:
                    screenshot_path = os.path.join(SKU_DEBUG_DIR, f"{id}_{idx}.png")
                    price_screenshot_path = os.path.join(SKU_DEBUG_DIR, f"{id}_{idx}_price.png")
                    _img_save_executor.submit(_async_save_png, current_img.copy(), screenshot_path)

                price_text = _extract_price_fast(
                    current_img,
                    price_anchor,
                    save_path=price_screenshot_path if debug else None
                )

                if not price_text or any(sym in price_text for sym in ("-", "~", "起")):
                    logger.info(f"  └─ [价格无效] 提取到价格为 '{price_text}' (包含区间符号或为空)，已过滤")
                    continue

                record = {name: sku["text"] for name, sku in zip(cat_names, combo)}

                for pre_cat, pre_val in pre_selected_specs.items():
                    if pre_cat not in record:
                        record[pre_cat] = pre_val

                record["价格"] = price_text
                record["截图地址"] = screenshot_path
                results.append(record)
                logger.info(f"  └─ [提取成功] ✅ 组合 {combo_desc} -> 价格: {price_text}")
            except Exception as e:
                logger.info(f"  └─ [组合异常] 遍历第 {idx} 个组合时报错: {e}")
                continue

        return results
    except Exception as e:
        logger.info(f"[SKU-FATAL] get_pdd_sku_info 发生未捕获异常: {e}")
        return []

# ==========================================
# 🤖 RPA 核心引擎 (极速 Raw 截屏 + 统一导航状态机版)
# ==========================================
class PddAdbBot:
    def __init__(self):
        self.width, self.height = self.get_device_resolution()
        if not self.width:
            raise RuntimeError("无法获取手机分辨率，请检查 ADB 连接！")
        logger.info(f"[SYSTEM] 设备已连接，屏幕分辨率: {self.width} x {self.height}")

    def get_device_resolution(self):
        res = subprocess.run(
            f'"{ADB_PATH}" shell wm size',
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        if "Physical size" in res.stdout:
            w, h = map(int, res.stdout.strip().split()[-1].split('x'))
            return w, h
        return 0, 0

    def click_point(self, x, y, desc=""):
        """点击绝对像素坐标（列表传参，防止 input tap 卡住主线程）"""
        x, y = int(x), int(y)
        if desc:
            logger.info(f"[INFO] 执行点击: {desc} ({x}, {y})")
        try:
            subprocess.run(
                [ADB_PATH, "shell", "input", "tap", str(x), str(y)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"[ADB-TIMEOUT] 点击 {desc} 超时")

    def scroll_sku_down(self):
        x = int(self.width * 0.5)
        y_start = int(self.height * 0.72)
        y_end = int(self.height * 0.35)
        try:
            subprocess.run(
                f'"{ADB_PATH}" shell input swipe {x} {y_start} {x} {y_end} 260',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.5
            )
        except subprocess.TimeoutExpired:
            logger.warning("[ADB-TIMEOUT] 滑动操作超时")

    def click_relative(self, pct_x, pct_y, desc=""):
        """使用相对比例点击屏幕，适配所有分辨率"""
        x = int(self.width * pct_x)
        y = int(self.height * pct_y)
        self.click_point(x, y, desc=desc)

    def scroll_sku_up(self):
        x = int(self.width * 0.5)
        y_start = int(self.height * 0.35)
        y_end = int(self.height * 0.75)
        try:
            subprocess.run(
                f'"{ADB_PATH}" shell input swipe {x} {y_start} {x} {y_end} 260',
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5
            )
        except Exception:
            pass

    def get_screenshot_cv(self):
        """
        极速 Raw RGBA 读取屏幕（彻底修复 Windows 下 shell=True 与 proc.communicate 导致的管道死锁）
        """
        proc = None
        try:
            # 1. 核心修复：坚决不使用 shell=True，直接以列表方式启动真实 adb.exe 进程
            proc = subprocess.Popen(
                [ADB_PATH, "exec-out", "screencap"],
                shell=False,  # 禁用 cmd.exe 中介，避免产生无主孤儿进程
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            raw_bytes, _ = proc.communicate(timeout=2.5)

            if raw_bytes and len(raw_bytes) > 16:
                w, h = struct.unpack("<II", raw_bytes[:8])
                expected_bytes = w * h * 4
                header_size = len(raw_bytes) - expected_bytes
                if w > 0 and h > 0 and header_size in (12, 16):
                    arr = np.frombuffer(raw_bytes, dtype=np.uint8, offset=header_size).reshape((h, w, 4))
                    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        except subprocess.TimeoutExpired:
            logger.warning("[ADB-TIMEOUT] exec-out screencap 读取超时，正在强制清理进程树...")
            if proc:
                try:
                    # Windows 下强杀整个进程树 (/F 强制, /T 包含子进程)
                    subprocess.run(
                        f"taskkill /F /T /PID {proc.pid}",
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0
                    )
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[ADB-ERROR] screencap 异常: {e}")
        finally:
            # 确保管道彻底关闭，释放系统句柄
            if proc:
                try:
                    if proc.stdout: proc.stdout.close()
                    if proc.stderr: proc.stderr.close()
                except Exception:
                    pass

        # 2. 兜底方案：screencap -p (同样严格禁用 shell=True，严格防死锁)
        proc_p = None
        try:
            proc_p = subprocess.Popen(
                [ADB_PATH, "exec-out", "screencap", "-p"],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            raw_png, _ = proc_p.communicate(timeout=3.0)
            if raw_png:
                nparr = np.frombuffer(raw_png, np.uint8)
                return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        except subprocess.TimeoutExpired:
            logger.warning("[ADB-TIMEOUT] screencap -p 兜底亦超时，直接放弃当前帧...")
            if proc_p:
                try:
                    subprocess.run(
                        f"taskkill /F /T /PID {proc_p.pid}",
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0
                    )
                except Exception:
                    try:
                        proc_p.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if proc_p:
                try:
                    if proc_p.stdout: proc_p.stdout.close()
                    if proc_p.stderr: proc_p.stderr.close()
                except Exception:
                    pass

        return None

    def _fast_region_ocr(self, img_region, scale=0.45, engine=ocr_fast):
        """
        【导航轮询专用极速 OCR】对 1800x2880 的半屏区域做 0.45 倍降采样后再识别，
        耗时从 2.0s 降至 0.15s，并将识别框坐标自动还原为原图比例
        """
        if img_region is None or img_region.size == 0:
            return []
        small_img = cv2.resize(img_region, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        res, _ = engine(small_img, use_cls=False)
        items = []
        inv_scale = 1.0 / scale
        for line in (res or []):
            pts, text = line[0], str(line[1]).strip()
            if text:
                x, y, w, h = cv2.boundingRect(np.array(pts, dtype=np.int32))
                items.append({
                    "box": (int(x * inv_scale), int(y * inv_scale), int(w * inv_scale), int(h * inv_scale)),
                    "cx": (x + w / 2.0) * inv_scale,
                    "cy": (y + h / 2.0) * inv_scale,
                    "text": text
                })
        return items

    def save_error_screenshot(self, save_path, img=None):
        """保存错误现场截图"""
        if img is None:
            img = self.get_screenshot_cv()
        if img is not None:
            ok, buf = cv2.imencode(".png", img)
            if ok:
                buf.tofile(save_path)

    def process_single_goods(self, promotion_url, goods_id, index, consecutive_successes=0, ocr=ocr_fast):
        extra_info = {"is_group": False, "force_retry_count": None, "no_sku_boxes": False}

        # --- 步骤 1：极速重置 Via 浏览器并一步注入链接 ---
        stop_app(VIA_PACKAGE)
        target_link = promotion_url if promotion_url else f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        logger.info(f"[TASK] [{index}] 步骤 1/3: 极速冷启动 Via 并打开链接 -> {target_link}")

        cmd = f'"{ADB_PATH}" shell am start -a android.intent.action.VIEW -d "{target_link}" {VIA_PACKAGE}'
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # --- 步骤 2：动态感知最上层应用 ---
        time.sleep(0.6)
        wait_start = time.time()
        via_stay_start = time.time()
        via_refresh_count = 0
        current_fg_pkg = ""

        while time.time() - wait_start < 3.0:
            current_fg_pkg = get_foreground_package()
            if current_fg_pkg == PDD_PACKAGE:
                logger.info(
                    f"[NAV-FAST] ⚡ 检测到最上层窗口已切入拼多多 (仅耗时: {time.time() - wait_start + 0.6:.2f}s)")
                time.sleep(0.35)
                break
            time.sleep(0.25)

        # --- 步骤 3：统一导航与状态机轮询 (中间页穿透 -> 动态点击购买 -> SKU捕获) ---
        logger.info(f"[TASK] [{index}] 步骤 2/3: 启动统一状态机轮询 (中间页穿透 -> 动态点击购买 -> SKU捕获)")
        state_start_time = time.time()
        detail_entered = False
        sku_ready = False
        last_buy_click_time = 0.0
        buy_click_count = 0
        CLICK_RETRY_INTERVAL = 3.5  # 提升重试点击间隔，严防误下单

        last_mid_click_time = 0.0
        MID_CLICK_COOLDOWN = 5.0
        CONFIRM_KEYWORDS = ("确定", "再来一单")

        last_pkg_check_time = time.time()
        final_img = None

        while time.time() - state_start_time < 26.0:
            try:
                # 1. 前台包名轻量探测与 Via 防卡死
                if not detail_entered:
                    now_time = time.time()
                    if now_time - last_pkg_check_time >= 1.5:
                        current_fg_pkg = get_foreground_package()
                        last_pkg_check_time = now_time

                    if current_fg_pkg == VIA_PACKAGE:
                        if time.time() - via_stay_start >= 4.0:
                            via_refresh_count += 1
                            if via_refresh_count <= 2:
                                logger.info(
                                    f"[NAV-CHECK] 停留在 Via 超过 4s 未跳转，执行第 {via_refresh_count} 次刷新...")
                                self.click_relative(0.547, 0.043, f"Via浏览器刷新按钮 (第 {via_refresh_count} 次)")
                            else:
                                logger.info(f"[NAV-CHECK] 连续刷新无效，重新发送 Intent 激活唤醒...")
                                subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=2.0)
                                via_refresh_count = 0
                            via_stay_start = time.time()
                        time.sleep(0.4)
                        continue

                # 2. 极速获取屏幕截图
                img_cv = self.get_screenshot_cv()
                if img_cv is None:
                    time.sleep(0.2)
                    continue

                final_img = img_cv
                h, w = img_cv.shape[:2]

                # 3. 对下半屏做极速 OCR
                bottom_half = img_cv[h // 2:h, 0:w]
                btm_items = self._fast_region_ocr(bottom_half, scale=0.45, engine=ocr)
                detected_text = "".join([item["text"] for item in btm_items])

                # A. 【优先检查终止态】：底部出现确认词，说明 SKU 面板已完全展开
                if detail_entered and any(kw in detected_text for kw in CONFIRM_KEYWORDS):
                    sku_ready = True
                    time.sleep(0.15)
                    stable_img = self.get_screenshot_cv()
                    if stable_img is not None:
                        final_img = stable_img
                    logger.info(
                        f"[INFO] ✅ SKU 面板已完全展开并锁定首帧 (轮询耗时: {time.time() - state_start_time:.2f}s)")
                    break

                # B. 【自动消解阻碍浮层】
                if any(kw in detected_text for kw in ["优惠详情", "店铺优惠"]):
                    logger.info("[NAV-POPUP] 检测到'优惠详情'抽屉遮挡底栏，正在轻触顶部遮罩区关闭...")
                    self.click_relative(0.50, 0.25, "关闭优惠详情浮层")
                    time.sleep(0.3)
                    continue

                # C. 业务特征与异常商品熔断检查
                if "人团" in detected_text:
                    extra_info["is_group"] = True
                    logger.info("[INFO] 检测到'人团'标识，将标记为 group 商品")

                if "不支持配送" in detected_text:
                    extra_info["force_retry_count"] = 10
                    logger.info("[WARN] 检测到'不支持配送'，立即熔断终止轮询")
                    return False, [], "不支持配送", extra_info

                if any(bad_kw in detected_text for bad_kw in ["已售罄", "已下架", "商品不存在", "全部售罄"]):
                    logger.info(f"[WARN] 检测到商品异常状态 ({detected_text[:20]})，提前终止轮询")
                    return False, [], f"商品不可售({detected_text[:10]})", extra_info

                # D. 【检测到原生详情页底栏】：动态点击购买与防误触安全观测
                has_bottom_bar = sum(1 for kw in ["客服", "店铺", "收藏"] if kw in detected_text) >= 2
                if has_bottom_bar and not any(kw in detected_text for kw in CONFIRM_KEYWORDS):
                    now = time.time()
                    if not detail_entered or (now - last_buy_click_time >= CLICK_RETRY_INTERVAL):
                        if not detail_entered:
                            time.sleep(0.10)

                        pct_x = 0.85 - (buy_click_count % 2) * 0.03
                        pct_y = 0.96

                        buy_click_count += 1
                        detail_entered = True
                        last_buy_click_time = time.time()
                        self.click_relative(pct_x, pct_y, f"动态点击购买 (第 {buy_click_count} 次)")

                        observe_start = time.time()
                        while time.time() - observe_start < 3.5:
                            time.sleep(0.35)
                            chk_cv = self.get_screenshot_cv()
                            if chk_cv is None:
                                continue

                            chk_btm = self._fast_region_ocr(chk_cv[h // 2:h, 0:w], scale=0.45, engine=ocr)
                            chk_text = "".join([item["text"] for item in chk_btm])

                            if any(kw in chk_text for kw in CONFIRM_KEYWORDS):
                                sku_ready = True
                                time.sleep(0.10)
                                stable_img = self.get_screenshot_cv()
                                final_img = stable_img if stable_img is not None else chk_cv
                                logger.info(
                                    f"[INFO] ✅ SKU 面板已完全展开并锁定 (安全观测耗时: {time.time() - observe_start:.2f}s)")
                                break

                        if sku_ready:
                            break
                        continue

                # E. 【尚未进入详情页】：中间页跳转
                if not detail_entered:
                    now = time.time()
                    if now - last_mid_click_time >= MID_CLICK_COOLDOWN:
                        top_half = img_cv[0:h // 2, 0:w]
                        top_items = self._fast_region_ocr(top_half, scale=0.45, engine=ocr)
                        mid_target = None
                        for item in top_items:
                            t = item["text"]
                            if any(btn_kw in t for btn_kw in ("立即购买", "抢券购买")):
                                mid_target = item
                                break

                        if mid_target:
                            last_mid_click_time = time.time()
                            btn_text = mid_target["text"]
                            self.click_point(int(mid_target["cx"]), int(mid_target["cy"]),
                                             f"中间页跳转按钮 [{btn_text}]")

                            sense_start = time.time()
                            while time.time() - sense_start < 3.0:
                                check_cv = self.get_screenshot_cv()
                                if check_cv is not None:
                                    chk_btm = self._fast_region_ocr(check_cv[h // 2:h, 0:w], scale=0.45, engine=ocr)
                                    chk_btm_text = "".join([t["text"] for t in chk_btm])

                                    if sum(1 for kw in ["客服", "店铺", "收藏"] if kw in chk_btm_text) >= 2 or any(
                                            kw in chk_btm_text for kw in CONFIRM_KEYWORDS):
                                        logger.info(
                                            f"[NAV-SENSE] ⚡ 动态捕获页面成功跃迁 (耗时: {time.time() - sense_start:.2f}s)")
                                        break

                                    chk_top = self._fast_region_ocr(check_cv[0:h // 2, 0:w], scale=0.45, engine=ocr)
                                    chk_top_text = "".join([t["text"] for t in chk_top])
                                    if not any(btn_kw in chk_top_text for btn_kw in ("立即购买", "抢券购买")):
                                        logger.info(
                                            f"[NAV-SENSE] ⚡ 中间页已切出 (耗时: {time.time() - sense_start:.2f}s)")
                                        break

                                time.sleep(0.10)
                            continue

            except Exception as e:
                logger.info(f"[NAV-DEBUG] 状态机轮询异常: {e}")

            time.sleep(0.12)

        # 轮询结束，清算结果
        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.save_error_screenshot(path, final_img)
            return False, [], "进入详情页失败", extra_info

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.save_error_screenshot(path, final_img)
            return False, [], "SKU面板未完全展开或点击全部失效", extra_info

        # --- 步骤 4：遍历提取所有 SKU 规格组合及价格 ---
        logger.info(f"[TASK] [{index}] 步骤 3/3: 正在极速遍历提取所有 SKU 规格组合与价格...")
        sku_info = get_pdd_sku_info(bot=self, id=goods_id, debug=DEBUG_MODE, initial_img=final_img,
                                    extra_info=extra_info)
        if not sku_info:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.save_error_screenshot(path)
            err_msg = extra_info.get("sku_error") or "未能提取到有效的SKU规格组合数据"
            return False, [], err_msg, extra_info

        logger.info(f"[TASK] [{index}] 🎯 成功提取 {len(sku_info)} 个规格组合。")

        if (consecutive_successes + 1) % 500 == 0:
            restart_app(PDD_PACKAGE)
            logger.info("[INFO] 循环连轴转达到 500 次，重启拼多多客户端释放资源。")

        return True, sku_info, "", extra_info

# ==========================================
# 🚦 任务调度引擎 (直接对接数据库)
# ==========================================
def batch_runner(product_list):
    logger.info("=" * 60)
    logger.info("[SYSTEM] 🚀 Via桥接-直连真机 RPA 引擎启动")
    logger.info(f"[INFO] 📊 待执行任务数: {len(product_list)}")
    logger.info("=" * 60)

    if not product_list:
        return

    logger.info("[INFO] 🔧 初始化启动：重启拼多多客户端...")
    restart_app(PDD_PACKAGE)

    bot = PddAdbBot()
    batch_counts = {"success": 0, "failed": 0, "skipped": 0}
    consecutive_successes = 0
    consecutive_failures = 0

    for i, product in enumerate(product_list, 1):
        goods_id = product["product_id"]
        promotion_url = product.get("promotion_url", "")
        logger.info("-" * 40)
        logger.info(f"[INFO] ▶▶▶ 开始处理 [{i}/{len(product_list)}] goods_id: {goods_id}")

        try:
            success, sku_info, error_msg, extra_info = bot.process_single_goods(
                promotion_url, goods_id, i, consecutive_successes
            )

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
                        "need_update_sku": False,
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
                    # 判断是否为不可重试的致命状态（未切出任何 SKU 框、不支持配送等）
                    force_retry = extra_info.get("force_retry_count")
                    is_fatal_sku_error = extra_info.get("no_sku_boxes") or (force_retry is not None and force_retry >= 10)

                    update_data = {
                        "platform": product.get("platform", "pdd"),
                        "product_id": product["product_id"],
                        "sku_status": "failed",
                        "sku_error": error_msg or "获取SKU列表为空",
                        "sku_updated_at": datetime.now(timezone.utc),
                    }

                    # 核心改动：不再重试的商品，显式将 need_update_sku 置为 False，彻底移出候选查询
                    if is_fatal_sku_error:
                        update_data["need_update_sku"] = False

                    if source_api_update is not None:
                        update_data["_source_api"] = source_api_update

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
                    logger.info(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg} (已调整数据库状态，标记为不再重试)")

                    restart_app(PDD_PACKAGE)

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
                restart_app(VIA_PACKAGE)
                restart_app(PDD_PACKAGE)
            except Exception:
                pass

        if consecutive_failures >= 10:
            logger.info(
                f"[WARN] ⚠️ 已连续发生 {consecutive_failures} 次失败，主动触发熔断机制，跳过本轮剩余的 {len(product_list) - i} 个任务！"
            )
            break

    logger.info("=" * 50)
    logger.info(
        f"[SYSTEM] 🎉 批量任务完毕！ 成功: {batch_counts['success']} | 跳过: {batch_counts['skipped']} | 失败: {batch_counts['failed']}"
    )
    logger.info("=" * 50)

# ==========================================
# 📊 性价比计算与商品筛选核心模块
# ==========================================
def get_data_updated_within_24h(hours=12, limit=0, extra_query=None, projection=None):
    """查询指定时间范围内的拼多多商品数据（默认 hours=12，基准数据可传 hours=72）"""
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


def calc_product_best_unit_prices(format_info):
    """计算单个商品在各个 unit (单位) 下的最佳性价比（即最低单位单价，元/单位）"""
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

        base_unit = pricing_basis.get("base_unit")
        total_val = pricing_basis.get("total_value")
        if base_unit and total_val is not None:
            try:
                t_float = float(total_val)
                if t_float > 0:
                    unit_quantities[str(base_unit).strip().lower()] = t_float
            except (ValueError, TypeError):
                pass

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


def is_group_product(product):
    """判断商品是否属于带有门槛的 group 商品"""
    _source_api = product.get("_source_api")
    if not _source_api:
        return True

    if isinstance(_source_api, (list, tuple, set)):
        return "group" in _source_api
    return "group" in str(_source_api)


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
    if "top_n" in kwargs:
        max_top = kwargs["top_n"]

    total_slots_selected = 0
    final_selected_map = {}

    for config in target_configs:
        kw = config["search_keyword"]
        words = config["words"]
        max_price = config["max_price"]

        logger.info("-" * 60)
        logger.info(f"[FILTER] ▶ 开始处理关键词【{kw}】")

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

        confirmed_unit_prices = {}
        for item in matched_confirmed:
            unit_prices = calc_product_best_unit_prices(item.get("format_info"))
            for u, u_price in unit_prices.items():
                if u not in confirmed_unit_prices:
                    confirmed_unit_prices[u] = []
                confirmed_unit_prices[u].append(u_price)

        unit_benchmarks = {}
        for u, prices in confirmed_unit_prices.items():
            prices.sort()
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

        matched_candidates = []
        for item in candidate_products:
            if not bool(item.get("need_update_sku")):
                continue
            if (item.get("sku_retry_count") or 0) > 3:
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

        raw_unit_buckets = {}
        qualified_unit_buckets = {}

        for item in matched_candidates:
            cand_unit_prices = calc_product_best_unit_prices(item.get("format_info"))
            for u, u_price in cand_unit_prices.items():
                if u not in raw_unit_buckets:
                    raw_unit_buckets[u] = []
                    qualified_unit_buckets[u] = []
                raw_unit_buckets[u].append((u_price, item))

                if u in unit_benchmarks:
                    benchmark_val = unit_benchmarks[u]["threshold"]
                    if u_price <= benchmark_val:
                        qualified_unit_buckets[u].append((u_price, item))
                else:
                    qualified_unit_buckets[u].append((u_price, item))

        logger.info(f"  ├─ [候选池] 命中 {len(matched_candidates)} 个待抓取候选商品，门槛过滤结果:")
        for u, all_cands in raw_unit_buckets.items():
            pass_count = len(qualified_unit_buckets[u])
            eliminated = len(all_cands) - pass_count
            base_info = f"(基准: {unit_benchmarks[u]['threshold']:.4f}元/{u})" if u in unit_benchmarks else "(无72H基准)"
            logger.info(
                f"  │    └─ 单位 [{u}] {base_info}: 候选 {len(all_cands)} 个 -> 达标 {pass_count} 个, 淘汰 {eliminated} 个"
            )

        active_units = {u: items for u, items in qualified_unit_buckets.items() if items}
        if not active_units:
            logger.info(f"  └─ ⚠️ 该关键词下所有候选商品均未达到 72H 第 {benchmark_rank} 名性价比基准，本轮全部拦截！")
            continue

        if only_top_unit:
            dominant_u = max(active_units.keys(), key=lambda u: len(active_units[u]))
            logger.info(
                f"  ├─ [主流锁定] 仅采纳达标商品数最多的单位 [{dominant_u}] (达标商品: {len(active_units[dominant_u])} 个)"
            )
            target_units = {dominant_u: active_units[dominant_u]}
        else:
            target_units = active_units

        kw_selected_count = 0
        for u, items_with_price in target_units.items():
            items_with_price.sort(key=lambda x: x[0])

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
if __name__ == "__main__":
    while True:
        try:
            monitored_products = read_json(
                r'W:\project\python_project\easy_shop\app\web_front\monitored_products.json'
            ) or {}

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
                # break

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
                    "promotion_url": 1,
                    "_source_api": 1
                }

                candidate_results = get_data_updated_within_24h(
                    hours=24,
                    limit=0,
                    extra_query={
                        "format_status": "success",
                        "need_update_sku": True,
                        "$or": [
                            {"sku_retry_count": {"$lte": 3}},
                            {"sku_retry_count": {"$exists": False}}
                        ]
                    },
                    projection=common_projection
                )

                confirmed_results = get_data_updated_within_24h(
                    hours=72,
                    limit=0,
                    extra_query={
                        "format_status": "success",
                        "sku_info": {"$exists": True, "$ne": []},
                        "_source_api": {"$exists": True, "$ne": "group"}
                    },
                    projection=common_projection
                )

                filtered_results = filter_products_by_cost_effectiveness(
                    candidate_products=candidate_results,
                    confirmed_products=confirmed_results,
                    target_configs=target_configs,
                    max_top=10,
                    ratio=0.01,
                    benchmark_rank=5,
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
import os
import re
import time
import itertools
import cv2
import numpy as np
import win32gui
import win32con
import uiautomation as auto
from mss import mss
from rapidocr_onnxruntime import RapidOCR

TARGET_APP_NAME = "拼多多"
_sct = mss()
_ocr = RapidOCR()


def _capture_full_window(window):
    """直接截取小程序完整页面及窗口左上角屏幕坐标"""
    rect = window.BoundingRectangle
    left, top = int(rect.left), int(rect.top)
    w, h = int(rect.right - left), int(rect.bottom - top)
    if w <= 0 or h <= 0:
        raise ValueError("窗口尺寸异常")
    sct_img = _sct.grab({"left": left, "top": top, "width": w, "height": h})
    return cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR), left, top


def _run_ocr(img_bgr):
    """执行 OCR 并返回结构化文本块列表（关闭方向分类器以提速）"""
    res, _ = _ocr(img_bgr, use_cls=False)
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


def _extract_price_fast(img_bgr, price_anchor):
    """只截取固定 ￥ 符号及其右侧同行窄条区域进行极速识别，绝不触碰下一行"""
    h_img, w_img = img_bgr.shape[:2]
    px, py, pw, ph = price_anchor

    # 纵向严格限制在 [py, py + ph] 内，避免切入下方紧贴的“即将恢复xx元”标签
    crop = img_bgr[max(0, py):min(h_img, py + ph), max(0, px - 2):int(w_img * 0.88)]
    padded = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))

    items = _run_ocr(padded)
    items.sort(key=lambda i: i["box"][0])
    text = "".join(i["text"] for i in items).replace("¥", "￥")
    text = re.sub(r"券后\s*[?？¥￥]?\s*", "券后￥", text)
    idx = text.find("￥")
    return text[idx:].strip() if idx != -1 else text.strip()


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

    # 匹配每个 SKU 框内的所有 OCR 文本块，按 X 坐标排序拼接保证内容完整
    used_ocr = set()
    sku_items = []
    for bx, by, bw, bh in raw_boxes:
        frags = []
        for idx, item in enumerate(ocr_items):
            ox, oy, ow, oh = item["box"]
            ix, iy = max(0, min(bx + bw, ox + ow) - max(bx, ox)), max(0, min(by + bh, oy + oh) - max(by, oy))
            if (ix * iy) / float(max(1, ow * oh)) >= 0.35:
                frags.append((ox, item["text"]))
                used_ocr.add(idx)
        frags.sort(key=lambda f: f[0])
        sku_items.append({"box": (bx, by, bw, bh), "cy": by + bh / 2.0, "text": "".join(t for _, t in frags)})

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
    对外唯一主函数：获取所有 SKU 规格组合、价格及截图
    :param id: 截图命名前缀（如 id_1.png, id_2.png）
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

        # 2. 极速遍历所有规格组合
        for idx, combo in enumerate(all_combos, start=1):
            try:
                clicked = False
                for target_sku in combo:
                    bx, by, bw, bh = target_sku["box"]
                    if not _is_box_selected(current_img, (bx, by, bw, bh)):
                        auto.Click(int(win_left + bx + bw // 2), int(win_top + by + bh // 2), waitTime=0)
                        clicked = True
                        time.sleep(0.05)

                if clicked:
                    current_img, win_left, win_top = _capture_full_window(window)

                price_text = _extract_price_fast(current_img, price_anchor)
                screenshot_path = ""
                if debug:
                    screenshot_path = f"{id}_{idx}.png"
                    ok, buf = cv2.imencode(".png", current_img, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if ok:
                        buf.tofile(screenshot_path)

                record = {name: sku["text"] for name, sku in zip(cat_names, combo)}
                record["价格"] = price_text
                record["截图地址"] = screenshot_path
                results.append(record)
            except Exception:
                continue

        return results
    except Exception:
        return []


if __name__ == "__main__":
    t0 = time.time()
    data = get_pdd_sku_info(id="test_item", debug=True)
    print(f"耗时: {time.time() - t0:.2f}s | 共采集到 {len(data)} 个规格组合：")
    for row in data:
        print(row)
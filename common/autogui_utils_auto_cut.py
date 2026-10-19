import os
import time
import cv2
import numpy as np
import win32gui
import win32con
import uiautomation as auto
from mss import mss
from rapidocr_onnxruntime import RapidOCR

# ==========================================
# ⚙️ 配置区
# ==========================================
TARGET_APP_NAME = "拼多多"
BEFORE_UNSELECT_PATH = "pdd_1_before_unselect.png"
AFTER_UNSELECT_PATH = "pdd_2_after_unselect.png"

sct = mss()
ocr = RapidOCR()


def auto_crop_pdd_content(img_bgr):
    """
    自动剔除微信小程序四周的透明阴影边框，只保留拼多多实体窗口画面
    返回: (裁剪后的图像, offset_x, offset_y)
    """
    h, w = img_bgr.shape[:2]
    b, g, r = cv2.split(img_bgr)

    # 拼多多顶部导航栏与下半部分 SKU 面板为纯白底 (BGR > 240)，底部确定按钮为红底
    white_mask = (b > 240) & (g > 240) & (r > 240)
    red_btn_mask = (r > 190) & (g < 80) & (b < 80)
    valid_panel_mask = white_mask | red_btn_mask

    # 1. 利用下半部分（SKU 弹窗面板宽度占满整个小程序可视区）确定精准的左右边界与下边界
    lower_half = valid_panel_mask[h // 2:, :]
    col_ratio = np.mean(lower_half, axis=0)
    valid_cols = np.where(col_ratio > 0.35)[0]

    if len(valid_cols) < w * 0.5:
        # 若未处于标准白色面板状态，不裁剪直接原图返回
        return img_bgr, 0, 0

    x1, x2 = int(valid_cols[0]), int(valid_cols[-1]) + 1

    # 2. 在精准的左右边界 [x1:x2] 内，寻找顶部纯白导航栏的上边界 y1 和底部红按钮的下边界 y2
    row_ratio = np.mean(valid_panel_mask[:, x1:x2], axis=1)
    valid_rows = np.where(row_ratio > 0.50)[0]

    if len(valid_rows) == 0:
        return img_bgr, 0, 0

    y1, y2 = int(valid_rows[0]), int(valid_rows[-1]) + 1

    cropped = img_bgr[y1:y2, x1:x2].copy()
    return cropped, x1, y1


def capture_pdd_window(window, crop_shadow=True):
    """
    截取拼多多窗口，并返回:
    - img_bgr: 裁剪干净的图像
    - origin_x, origin_y: 该图像左上角对应屏幕的绝对坐标（用于后续精准点击）
    """
    rect = window.BoundingRectangle
    win_left, win_top = int(rect.left), int(rect.top)
    monitor = {
        "left": win_left,
        "top": win_top,
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top)
    }
    sct_img = sct.grab(monitor)
    raw_bgr = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)

    if crop_shadow:
        cropped_bgr, off_x, off_y = auto_crop_pdd_content(raw_bgr)
        return cropped_bgr, win_left + off_x, win_top + off_y

    return raw_bgr, win_left, win_top


def detect_pdd_sku_boxes(img_bgr, use_ocr=False):
    """
    识别拼多多 SKU 面板中的【被选中框】、【未选中规格框】及其内部的【文字框】
    """
    h_img, w_img = img_bgr.shape[:2]
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    b, g, r = cv2.split(img_bgr)

    # 1. 红色边框/文字掩码
    red_mask1 = cv2.inRange(hsv, np.array([0, 85, 160]), np.array([12, 255, 255]))
    red_mask2 = cv2.inRange(hsv, np.array([168, 85, 160]), np.array([180, 255, 255]))
    red_mask = cv2.bitwise_or(red_mask1, red_mask2)

    # 2. 浅粉红底色掩码 (#FDEFEE, BGR ≈ [238, 239, 253])
    pink_cond = (
        (r >= 245) & (g >= 225) & (g <= 247) & (b >= 225) & (b <= 247) &
        ((r.astype(np.int16) - b.astype(np.int16)) >= 6)
    )
    pink_mask = np.where(pink_cond, 255, 0).astype(np.uint8)

    # 3. 浅灰色未选中底色掩码 (#F5F5F5, BGR ≈ [245, 245, 245])
    max_c = np.maximum(np.maximum(b, g), r)
    min_c = np.minimum(np.minimum(b, g), r)
    gray_cond = (min_c >= 242) & (max_c <= 248) & ((max_c - min_c) <= 3)
    gray_mask = np.where(gray_cond, 255, 0).astype(np.uint8)

    # --- 提取【被选中的规格框】 ---
    selected_items = []
    kernel_small = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    red_closed = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kernel_small)
    cnts_red, _ = cv2.findContours(red_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for c in cnts_red:
        x, y, w, h = cv2.boundingRect(c)
        if w > 42 and 28 <= h <= 85 and w < w_img * 0.96:
            area = float(w * h)
            pink_ratio = np.sum(pink_mask[y:y+h, x:x+w] > 0) / area
            red_ratio = np.sum(red_mask[y:y+h, x:x+w] > 0) / area

            if pink_ratio > 0.35 and red_ratio < 0.50:
                pad = 4
                text_box = None
                if h > 2 * pad and w > 2 * pad:
                    inner_red = red_mask[y+pad:y+h-pad, x+pad:x+w-pad]
                    pts = cv2.findNonZero(inner_red)
                    if pts is not None:
                        tx, ty, tw, th = cv2.boundingRect(pts)
                        text_box = (x + pad + tx, y + pad + ty, tw, th)

                selected_items.append({
                    "box": (x, y, w, h),
                    "text_box": text_box,
                    "pink_ratio": round(pink_ratio, 3),
                    "ocr_text": ""
                })

    # --- 提取【未选中的规格框】 ---
    unselected_items = []
    cnts_gray, _ = cv2.findContours(gray_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for c in cnts_gray:
        x, y, w, h = cv2.boundingRect(c)
        if w > 48 and 28 <= h <= 85:
            area = float(w * h)
            gray_ratio = np.sum(gray_mask[y:y+h, x:x+w] > 0) / area

            if 0.45 < gray_ratio < 0.92:
                pad = 4
                text_box = None
                if h > 2 * pad and w > 2 * pad:
                    inner_fg = cv2.bitwise_not(gray_mask[y+pad:y+h-pad, x+pad:x+w-pad])
                    pts = cv2.findNonZero(inner_fg)
                    if pts is not None:
                        tx, ty, tw, th = cv2.boundingRect(pts)
                        text_box = (x + pad + tx, y + pad + ty, tw, th)

                unselected_items.append({
                    "box": (x, y, w, h),
                    "text_box": text_box,
                    "gray_ratio": round(gray_ratio, 3),
                    "ocr_text": ""
                })

    selected_items.sort(key=lambda item: (item["box"][1] // 15, item["box"][0]))
    unselected_items.sort(key=lambda item: (item["box"][1] // 15, item["box"][0]))

    if use_ocr:
        ocr_res, _ = ocr(img_bgr)
        if ocr_res:
            all_items = selected_items + unselected_items
            for line in ocr_res:
                pts, text = line[0], line[1]
                ox, oy, ow, oh = cv2.boundingRect(np.array(pts, dtype=np.int32))
                cx, cy = ox + ow // 2, oy + oh // 2
                for item in all_items:
                    bx, by, bw, bh = item["box"]
                    if bx <= cx <= bx + bw and by <= cy <= by + bh:
                        item["ocr_text"] += text
                        if item["text_box"] is None:
                            item["text_box"] = (ox, oy, ow, oh)
                        break

    return selected_items, unselected_items


def draw_detection_results(img_bgr, selected_items, unselected_items):
    """把识别出的规格外框与文字内框画在图上"""
    vis = img_bgr.copy()
    for idx, item in enumerate(unselected_items, 1):
        x, y, w, h = item["box"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (255, 120, 0), 2)
        if item["text_box"]:
            tx, ty, tw, th = item["text_box"]
            cv2.rectangle(vis, (tx, ty), (tx + tw, ty + th), (255, 255, 0), 1)
        cv2.putText(vis, f"SKU-{idx}", (x + 2, y - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 100, 0), 1, cv2.LINE_AA)

    for idx, item in enumerate(selected_items, 1):
        x, y, w, h = item["box"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 220, 0), 3)
        if item["text_box"]:
            tx, ty, tw, th = item["text_box"]
            cv2.rectangle(vis, (tx, ty), (tx + tw, ty + th), (0, 215, 255), 2)
        cv2.putText(vis, f"SELECTED-{idx} (pink:{item['pink_ratio']})", (x + 2, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 180, 0), 2, cv2.LINE_AA)
    return vis


def ensure_all_sku_unselected(window_name=TARGET_APP_NAME, save_debug=True):
    """
    【核心封装函数】
    1. 置顶拼多多窗口并截取无黑边画面
    2. 检测所有被选中的规格框 (selected_items)
    3. 如果存在被选中框，依次点击它们的中心使其【取消选中】
    4. 重新截图验证，返回最终无选中状态的干净截图
    """
    window = auto.WindowControl(searchDepth=1, Name=window_name)
    if not window.Exists(3, 1):
        raise RuntimeError(f"❌ 未找到【{window_name}】窗口！")

    hwnd = window.NativeWindowHandle
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:
        pass

    # 1. 初次截图并检测选中框
    img_before, origin_x, origin_y = capture_pdd_window(window, crop_shadow=True)
    selected_items, unselected_items = detect_pdd_sku_boxes(img_before, use_ocr=False)

    if save_debug:
        vis_before = draw_detection_results(img_before, selected_items, unselected_items)
        cv2.imwrite(BEFORE_UNSELECT_PATH, vis_before)
        print(f"[INFO] 取消选中前的标注图已保存: {BEFORE_UNSELECT_PATH}")

    # 2. 如果检测到被选中的规格框，依次点击它取消选中
    if selected_items:
        print(f"[ACTION] 🔍 检测到 {len(selected_items)} 个被选中的规格框，正在执行点击取消选中...")
        for idx, item in enumerate(selected_items, 1):
            bx, by, bw, bh = item["box"]
            # 换算为屏幕绝对坐标（图片左上角屏幕坐标 + 框中心点偏移）
            click_x = int(origin_x + bx + bw // 2)
            click_y = int(origin_y + by + bh // 2)
            print(f"   -> 点击第 {idx} 个选中框中心: 屏幕坐标 ({click_x}, {click_y})")
            auto.Click(click_x, click_y)
            time.sleep(0.15)  # 给小程序 UI 渲染留出 150ms 响应时间

        # 将鼠标移到窗口顶部标题栏空白处，避免鼠标悬停在规格框上影响视觉或触发提示
        auto.MoveTo(int(origin_x + 150), int(origin_y + 20))
        time.sleep(0.15)

        # 3. 再次截图复核
        img_after, _, _ = capture_pdd_window(window, crop_shadow=True)
        sel_after, unsel_after = detect_pdd_sku_boxes(img_after, use_ocr=False)
        print(f"[VERIFY] ✅ 点击完成！当前剩余选中框数量: {len(sel_after)} 个 | 未选中规格框数量: {len(unsel_after)} 个")

        if save_debug:
            vis_after = draw_detection_results(img_after, sel_after, unsel_after)
            cv2.imwrite(AFTER_UNSELECT_PATH, vis_after)
            print(f"[INFO] 取消选中后的标注图已保存: {AFTER_UNSELECT_PATH}")
            return img_after, vis_before, vis_after

        return img_after, None, None
    else:
        print("[INFO] ✨ 当前没有任何规格框处于选中状态，无需点击。")
        return img_before, vis_before if save_debug else None, vis_before if save_debug else None


if __name__ == "__main__":
    clean_img, vis_before, vis_after = ensure_all_sku_unselected(TARGET_APP_NAME, save_debug=True)

    # 将点击前后的对比图并排拼接展示，直观观察“去黑边 + 取消选中”的效果
    if vis_before is not None and vis_after is not None:
        h1, w1 = vis_before.shape[:2]
        h2, w2 = vis_after.shape[:2]
        if h1 == h2 and w1 == w2:
            combined = np.hstack([vis_before, vis_after])
            cv2.imshow("Left: Before Click (Selected)  |  Right: After Click (Unselected)", combined)
        else:
            cv2.imshow("After Click (Unselected)", vis_after)

        print("👀 预览窗口已弹出（左侧为点击前，右侧为点击取消选中后），按任意键关闭...")
        cv2.waitKey(0)
        cv2.destroyAllWindows()
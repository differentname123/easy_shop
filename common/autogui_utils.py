import uiautomation as auto
import pyperclip
import time
import os
import datetime
import win32gui
import win32con

# === 高性能依赖 ===
import numpy as np
import cv2
from mss import mss
from rapidocr_onnxruntime import RapidOCR

from common.common_utils import read_json, save_json

# ==========================================
# ⚙️ 全局配置区
# ==========================================
# 新增中转小程序配置
TRANSFER_APP_NAME = "合力汇"
TRANSFER_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\合力汇.lnk"
TARGET_APP_NAME = "拼多多"

DEBUG_MODE = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUCCESS_DIR = os.path.join(BASE_DIR, "results_success")
ERROR_DIR = os.path.join(BASE_DIR, "results_error")
STATE_FILE = os.path.join(BASE_DIR, "goods_state.json")

for d in [SUCCESS_DIR, ERROR_DIR]:
    os.makedirs(d, exist_ok=True)

# 实例化电竞级截图抓取器 (全局单例，极速)
sct = mss()


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}")


log("[SYSTEM] 正在初始化 RapidOCR 轻量级引擎...")
ocr = RapidOCR()


# ==========================================
# 💾 状态管理
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return read_json(STATE_FILE)
        except Exception as e:
            pass
    return {}


def save_state(state):
    save_json(STATE_FILE, state)


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
            log(f"[DEBUG] 置顶操作受到系统保护拦截: {e}")

    def safe_click(self, x, y, desc=""):
        """无感瞬发点击，剥离所有冗余等待"""
        if desc: log(f"[INFO] 执行点击: {desc} ({x}, {y})")
        # 直接使用 uiautomation 的瞬发点击，比 pyautogui 移动鼠标要快且稳
        auto.Click(int(x), int(y))

    def safe_input(self, text, desc=""):
        """极速输入：通过剪贴板瞬发，规避敲击延迟"""
        if desc: log(f"[INFO] 执行输入: {desc}")
        pyperclip.copy(text)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.01)  # 极短间隔，确保黏贴缓冲
        auto.SendKeys('{Enter}')

    def fast_screenshot_save(self, save_path, region=None):
        """使用 mss 极速保存全图/区域截图"""
        if region is None:
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

        monitor = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
        sct_img = sct.grab(monitor)
        # mss 抓出来是 BGRA，需要转 BGR 保存
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

        # 转换 mss 需要的格式
        monitor = {"left": capture_region[0], "top": capture_region[1],
                   "width": capture_region[2], "height": capture_region[3]}

        # 去除固定 sleep，只要机器性能允许，全速轮询 (能达 20-30 FPS)
        while time.time() - start_time < timeout:
            try:
                # 1. mss 极速截图 (仅需 1-3 ms)
                sct_img = sct.grab(monitor)
                # 2. 转为 numpy 数组 (BGRA -> BGR)
                img_cv = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)

                # 3. OCR 推理
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
# 🏢 双桥梁业务逻辑层 (已重构)
# ==========================================
class PddAutomation(UIActionEngine):
    def __init__(self):
        # 初始目标设为中转小程序
        super().__init__(TRANSFER_APP_NAME)

    def close_current_window(self):
        """通用窗口关闭，用于清理拼多多窗口，保持桌面整洁"""
        if self.window and self.window.Exists(0, 0):
            rect = self.window.BoundingRectangle
            self.safe_click(rect.right - 25, rect.top + 60, f"关闭 {self.window_name} 窗口")
            time.sleep(0.3)

    def process_single_goods(self, goods_id, index, consecutive_successes=0):
        # --- 步骤 1：确立中转站据点 ---
        self.window_name = TRANSFER_APP_NAME
        if not self.find_window(timeout=1):
            log(f"[SYSTEM] 正在唤醒中转小程序: {TRANSFER_APP_NAME}")
            os.startfile(TRANSFER_SHORTCUT_PATH)
            if not self.find_window(timeout=5):
                return False, "", "中转小程序启动失败"

        # 严格保留置顶操作，防止输入框失去焦点
        self.force_bring_to_front()

        # --- 步骤 2：精确操控控件进行传参 ---
        log(f"[TASK] [{index}] 步骤 1/4: 向中转站写入商品 ID")
        edit_box = self.window.EditControl()
        if not edit_box.Exists(2, 1):
            return False, "", "无法定位中转小程序的输入框"

        # 【修复点】：小程序必须触发底层 bindinput 事件。不能用 SetValue。
        # simulateMove=False 保证了鼠标不会出现慢速滑动的动画，而是瞬间点击
        edit_box.Click(simulateMove=False)
        time.sleep(0.05)

        # 物理级全选并删除，确保触发框架的数据更新
        auto.SendKeys('{Ctrl}a')
        time.sleep(0.05)
        auto.SendKeys('{Delete}')
        time.sleep(0.05)

        # 写入新的 goods_id
        pyperclip.copy(str(goods_id))
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.05)

        log(f"[TASK] [{index}] 步骤 2/4: 触发 API 跳转")
        jump_btn = self.window.ButtonControl(Name="跳转到该商品")
        if not jump_btn.Exists(1, 1):
            return False, "", "无法定位中转小程序的跳转按钮"

        # 按钮点击可以使用底层接口，不会有事件丢失问题
        try:
            jump_btn.GetInvokePattern().Invoke()
        except Exception:
            jump_btn.Click(simulateMove=False)

        # --- 步骤 2.5：处理微信跳转授权弹窗 ---
        log(f"[TASK] [{index}] 步骤 2.5/4: 处理微信跳转授权弹窗")
        allow_btn = self.window.TextControl(Name="允许")
        if allow_btn.Exists(2, 1):
            try:
                allow_btn.GetInvokePattern().Invoke()
            except Exception:
                allow_btn.Click(simulateMove=False)
            log("[INFO] 已自动点击“允许”跳转")
        else:
            log("[INFO] 未检测到“允许”弹窗（可能已静默放行或系统延迟）")

        # --- 步骤 3：接管弹出的拼多多目标窗口 ---
        log(f"[TASK] [{index}] 步骤 3/4: 接管并校验拼多多详情页")
        # 切换句柄目标，确保接下来的操作只在拼多多小程序内进行，不跟合力汇串台
        self.window_name = TARGET_APP_NAME

        if not self.find_window(timeout=5):
            return False, "", "未检测到拼多多窗口弹出（跳转可能失败）"

        # 严格保留置顶操作，确保拼多多窗口拿到最高控制权，后续点击不被拦截
        self.force_bring_to_front()
        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top

        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)
        is_detail, _ = self.safe_ocr_wait(["客服", "店铺"], timeout=10, region=region_bottom)

        if not is_detail:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.close_current_window()  # 没成功的时候重启(关闭)拼多多
            return False, path, "进入详情页失败"

        # --- 步骤 4：闭环后续操作 ---
        self.safe_click(rect.right - 60, rect.bottom - 25, "点击购买")
        log(f"[TASK] [{index}] 步骤 4/4: 校验 SKU 界面并快照")

        region_sku = (rect.left, rect.bottom - h // 2, w, h // 2)
        is_sku_ready, _ = self.safe_ocr_wait(["确定", "请选择", "可选", "已选"], timeout=5, region=region_sku)

        if not is_sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.close_current_window()  # 没成功的时候重启(关闭)拼多多
            return False, path, "SKU面板未完全展开"

        success_path = os.path.join(SUCCESS_DIR, f"{goods_id}.png")
        self.fast_screenshot_save(success_path)
        log(f"[TASK] [{index}] 🎯 成功生成最终截图。")

        # 【优化】只有连续成功达到 50 次，才主动关闭重启拼多多
        if (consecutive_successes + 1) % 50 == 0:
            self.close_current_window()
            log(f"[INFO] 循环连轴转达到 50 次，重启(关闭)拼多多小程序释放资源。")

        return True, success_path, ""


# ==========================================
# 🚦 任务调度引擎
# ==========================================
def batch_runner(goods_id_list):
    state = load_state()

    filtered_list = [gid for gid in goods_id_list
                     if not state.get(gid, {}).get("success", False)
                     and state.get(gid, {}).get("attempts", 0) < 30]

    print(f"\n{'=' * 60}")
    log("[SYSTEM] 🚀 桥接级 RPA 引擎启动 (通过合力汇中转)")
    log(f"[INFO] 📊 待执行任务数: {len(filtered_list)}")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        return

    bot = PddAutomation()
    success_count, fail_count = 0, 0
    consecutive_successes = 0  # 追踪连续成功次数，用于按频次重启

    for i, goods_id in enumerate(filtered_list, 1):
        print("\n" + "-" * 40)
        log(f"[INFO] ▶▶▶ 开始处理 [{i}/{len(filtered_list)}] goods_id: {goods_id}")

        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1

        try:
            # 透传 consecutive_successes 参数给方法评估是否达到 50 次关闭阈值
            success, img_path, error_msg = bot.process_single_goods(goods_id, i, consecutive_successes)
            state[goods_id].update({"success": success, "image_path": img_path, "error_msg": error_msg})

            if success:
                log(f"[SUCCESS] ✅ 处理成功 ({goods_id})")
                success_count += 1
                consecutive_successes += 1
            else:
                log(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg}")
                fail_count += 1
                consecutive_successes = 0  # 失败即清零

        except Exception as e:
            log(f"[FATAL] 💥 发生严重异常: {str(e)}")
            fail_count += 1
            consecutive_successes = 0  # 发生异常即清零
            # 异常时进行保护性环境清理（没成功时重启）
            try:
                bot.window_name = TARGET_APP_NAME
                if bot.find_window(timeout=1):
                    bot.close_current_window()
            except:
                pass

        finally:
            save_state(state)

    print(f"\n{'=' * 50}")
    log(f"[SYSTEM] 🎉 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    while True:
        try:
            need_sku_product_id_list = read_json("need_sku_product_id.json")
            batch_runner(need_sku_product_id_list)
        except Exception as e:
            log(f"[FATAL] 💥 主程序异常退出: {str(e)}")
        time.sleep(3600)
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
PDD_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\拼多多.lnk"
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
# 🏢 拼多多业务逻辑层
# ==========================================
class PddAutomation(UIActionEngine):
    def __init__(self):
        super().__init__('拼多多')

    def restart_mini_program(self):
        log("[SYSTEM] 正在唤醒/重启小程序...")
        if self.find_window(timeout=1):
            rect = self.window.BoundingRectangle
            self.safe_click(rect.right - 25, rect.top + 60, "关闭旧窗口")
            time.sleep(0.5)

        try:
            os.startfile(PDD_SHORTCUT_PATH)
        except Exception as e:
            log(f"[ERROR] 快捷方式启动失败: {e}")
            return False

        if not self.find_window(timeout=10):
            return False
        return True

    def prepare_home_page(self):
        if not self.find_window(timeout=0.5):
            return self.restart_mini_program()

        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top
        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)

        # 极速检测首页
        is_home, _ = self.safe_ocr_wait("首页", timeout=0.2, region=region_bottom)
        if is_home:
            return True

        log("[ACTION] 触发返回上一层...")
        start_time = time.time()
        # 由于我们响应速度极快，这里可以狂点返回，直到看见首页
        while time.time() - start_time < 3:
            self.safe_click(rect.left + 20, rect.top + 60)
            is_home, _ = self.safe_ocr_wait("首页", timeout=0.3, region=region_bottom)
            if is_home:
                return True

        log("[WARN] 返回超时，执行兜底重启...")
        return self.restart_mini_program()

    def process_single_goods(self, goods_id, index):
        goods_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"

        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top

        # --- 步骤 1 ---
        log(f"[TASK] [{index}] 步骤 1/4: 输入商品链接")
        self.safe_click(rect.left + w // 2, rect.top + 65, "激活搜索框")
        self.safe_input(goods_url, "粘贴并回车")

        # --- 步骤 2 ---
        log(f"[TASK] [{index}] 步骤 2/4: 校验详情页状态")
        # 优化区域：缩小 OCR 扫描范围，只扫描底部按键区域，成倍提升 OCR 帧率
        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)
        is_detail, keyword = self.safe_ocr_wait(["客服", "店铺"], timeout=10, region=region_bottom)

        if not is_detail:
            path = os.path.join(ERROR_DIR, f"DETAIL_Error_{index}.png")
            self.fast_screenshot_save(path)
            return False, path, "进入详情页失败"

        # --- 步骤 3 ---
        # OCR 捕捉到的瞬间，立刻发起点击！无需任何多余等待！
        log(f"[TASK] [{index}] 步骤 3/4: 点击购买面板")
        self.safe_click(rect.right - 60, rect.bottom - 25, "点击购买")

        # --- 步骤 4 ---
        log(f"[TASK] [{index}] 步骤 4/4: 校验 SKU 界面")
        # 优化区域：只扫描中间偏上的弹窗标题区，极大提升识别速度

        region_sku = (rect.left, rect.bottom - h // 2, w, h // 2)
        is_sku_ready, _ = self.safe_ocr_wait(["确定", "请选择", "可选", "已选"], timeout=5, region=region_sku)
        if not is_sku_ready:
            path = os.path.join(ERROR_DIR, f"SKU_Error_{index}.png")
            self.fast_screenshot_save(path)
            return False, path, "SKU面板未完全展开"

        # --- 步骤 5 ---
        success_path = os.path.join(SUCCESS_DIR, f"SUCCESS_{goods_id}_{int(time.time())}.png")
        self.fast_screenshot_save(success_path)
        log(f"[TASK] [{index}] 🎯 成功生成最终截图。")
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
    log("[SYSTEM] 🚀 PDD 极速版 RPA 引擎启动")
    log(f"[INFO] 📊 待执行任务数: {len(filtered_list)}")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        return

    bot = PddAutomation()
    success_count, fail_count = 0, 0

    for i, goods_id in enumerate(filtered_list, 1):
        print("\n" + "-" * 40)

        if not bot.find_window(timeout=1):
            bot.restart_mini_program()

        # 【核心约束实现】拉取商品 ID 和执行前，使用底层 Win32 API 霸道置顶焦点
        bot.force_bring_to_front()

        log(f"[INFO] ▶▶▶ 开始处理 [{i}/{len(filtered_list)}] goods_id: {goods_id}")

        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1

        try:
            if not bot.prepare_home_page():
                log("[WARN] 首页初始化失败跳过")
                fail_count += 1
                continue

            success, img_path, error_msg = bot.process_single_goods(goods_id, i)
            state[goods_id].update({"success": success, "image_path": img_path, "error_msg": error_msg})

            if success:
                log(f"[SUCCESS] ✅ 处理成功 ({goods_id})")
                success_count += 1
            else:
                log(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg}")
                fail_count += 1

        except Exception as e:
            log(f"[FATAL] 💥 发生严重异常: {str(e)}")
            fail_count += 1

        finally:
            save_state(state)

    print(f"\n{'=' * 50}")
    log(f"[SYSTEM] 🎉 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    test_goods_ids = [
        "997025592944",
        "702868469934"
    ]
    batch_runner(test_goods_ids)
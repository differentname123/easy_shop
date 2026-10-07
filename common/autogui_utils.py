import uiautomation as auto
import pyautogui
import pyperclip
import time
import os
import traceback
import json
import datetime

# === 轻量级 OCR 依赖 ===
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR

# ==========================================
# ⚙️ 全局配置区
# ==========================================
PDD_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\拼多多.lnk"
DEBUG_MODE = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUCCESS_DIR = os.path.join(BASE_DIR, "results_success")
ERROR_DIR = os.path.join(BASE_DIR, "results_error")
TRACE_DIR = os.path.join(BASE_DIR, "results_trace")
STATE_FILE = os.path.join(BASE_DIR, "goods_state.json")

# 确保目录存在
for d in [SUCCESS_DIR, ERROR_DIR, TRACE_DIR]:
    os.makedirs(d, exist_ok=True)


# ==========================================
# 📝 带时间戳的精准日志系统
# ==========================================
def log(msg, level="INFO"):
    """标准化日志输出，带精确时间戳"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{level}] {msg}")


log("正在初始化 RapidOCR 轻量级引擎...", "SYSTEM")
ocr = RapidOCR()


# ==========================================
# 💾 状态管理
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"读取状态文件失败: {e}，将初始化空状态", "WARN")
    return {}


def save_state(state):
    # 使用临时文件写入后重命名，防止写入过程中断电/崩溃导致 JSON 损坏 (原子写入)
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, STATE_FILE)


# ==========================================
# 🤖 RPA 核心引擎 (UI基础动作层)
# ==========================================
class UIActionEngine:
    """封装所有基础 UI 交互，确保每次动作前绝对置顶"""

    def __init__(self, window_name):
        self.window_name = window_name
        self.window = None

    def find_window(self, timeout=3):
        self.window = auto.WindowControl(searchDepth=1, Name=self.window_name)
        return self.window.Exists(timeout, 1)

    def _force_active(self):
        """内部核心：强制窗口置顶，穿透任何焦点抢占"""
        if self.window and self.window.Exists(0, 0):
            try:
                self.window.SetActive()
                self.window.SetTopmost(True)
                time.sleep(0.02)  # 给系统极短的渲染时间
                self.window.SetTopmost(False)  # 保持在顶层但解除锁定，防止卡死其他应用
            except Exception as e:
                log(f"窗口置顶受阻: {e}", "DEBUG")

    def safe_click(self, x, y, desc=""):
        """安全点击"""
        self._force_active()
        if desc: log(f"执行点击: {desc} ({x}, {y})")
        auto.Click(int(x), int(y))
        time.sleep(0.1)  # 点击后的基础硬直时间

    def safe_input(self, text, desc=""):
        """安全粘贴与输入"""
        self._force_active()
        if desc: log(f"执行输入: {desc}")
        pyperclip.copy(text)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.1)
        auto.SendKeys('{Enter}')

    def safe_screenshot(self, save_path, region=None):
        """安全截图"""
        self._force_active()
        if region is None:
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

        # 确保 region 都是整数，避免 pyautogui 报错
        region = tuple(map(int, region))
        return pyautogui.screenshot(save_path, region=region)

    def safe_ocr_wait(self, target_texts, timeout=5, region=None, interval=0.2):
        """安全 OCR 断言识别 (支持多关键词，任意匹配即成功)"""
        if isinstance(target_texts, str):
            target_texts = [target_texts]

        start_time = time.time()
        while time.time() - start_time < timeout:
            self._force_active()

            if region is None:
                rect = self.window.BoundingRectangle
                capture_region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            else:
                capture_region = tuple(map(int, region))

            try:
                img = pyautogui.screenshot(region=capture_region)
                img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                result, _ = ocr(img_cv)

                if result:
                    for line in result:
                        text = line[1] if len(line) >= 2 else ""
                        for target in target_texts:
                            if target in text:
                                return True, target  # 返回布尔值和匹配到的词
            except Exception as e:
                log(f"OCR捕获异常: {e}", "DEBUG")

            time.sleep(interval)

        return False, None


# ==========================================
# 🏢 拼多多业务逻辑层
# ==========================================
class PddAutomation(UIActionEngine):
    def __init__(self):
        super().__init__('拼多多')

    def restart_mini_program(self):
        log("准备环境，检查是否需要清理旧窗口...", "SYSTEM")
        if self.find_window(timeout=1):
            rect = self.window.BoundingRectangle
            self.safe_click(rect.right - 25, rect.top + 60, "关闭残留窗口")
            time.sleep(1.0)  # 等待动画彻底消失

        try:
            log("正在唤醒小程序...", "SYSTEM")
            os.startfile(PDD_SHORTCUT_PATH)
        except Exception as e:
            log(f"致命错误，快捷方式启动失败: {e}", "ERROR")
            return False

        if not self.find_window(timeout=10):
            log("启动超时，小程序主窗口未出现。", "ERROR")
            return False

        log("小程序已就绪。", "SUCCESS")
        return True

    def prepare_home_page(self):
        if not self.find_window(timeout=0.5):
            log("窗口丢失，执行硬重启...", "WARN")
            return self.restart_mini_program()

        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top
        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)

        # 优化点：先做一次免等待的 OCR 检查，如果在首页，直接跳过点击返回
        log("检测当前是否已在首页...")
        is_home, _ = self.safe_ocr_wait("首页", timeout=0.5, region=region_bottom)
        if is_home:
            log("当前已处于首页，无需返回。", "SUCCESS")
            return True

        log("尝试通过点击返回图标回到首页...", "ACTION")
        start_time = time.time()
        while time.time() - start_time < 8:
            self.safe_click(rect.left + 20, rect.top + 60, "点击返回按键")
            # 点击后马上断言
            is_home, _ = self.safe_ocr_wait("首页", timeout=1.0, region=region_bottom)
            if is_home:
                log("确认回到首页。", "SUCCESS")
                return True

        log("软返回超时，执行兜底硬重启...", "WARN")
        return self.restart_mini_program()

    def record_checkpoint(self, index, step_name):
        if not DEBUG_MODE: return
        filename = f"Step_{step_name}_{index}_{int(time.time())}.png"
        path = os.path.join(TRACE_DIR, filename)
        self.safe_screenshot(path)

    def process_single_goods(self, goods_id, index):
        goods_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top

        # --- 步骤 1 ---
        log(f"[{index}] 步骤 1/4: 输入商品链接", "TASK")
        search_entry_x = rect.left + w // 2
        search_entry_y = rect.top + 65

        self.safe_click(search_entry_x, search_entry_y, "激活搜索框")
        self.safe_input(goods_url, "粘贴链接并回车")
        self.record_checkpoint(index, "1_输入搜索")

        # --- 步骤 2 ---
        log(f"[{index}] 步骤 2/4: 校验详情页状态", "TASK")
        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)
        # 支持传列表，任意匹配一个即成功，代码更整洁
        is_detail, keyword = self.safe_ocr_wait(["客服", "店铺"], timeout=20, region=region_bottom)

        if not is_detail:
            path = os.path.join(ERROR_DIR, f"DETAIL_Error_{index}_{int(time.time())}.png")
            self.safe_screenshot(path)
            return False, path, "进入详情页失败或超时"
        self.record_checkpoint(index, "2_进入详情")

        # --- 步骤 3 ---
        log(f"[{index}] 步骤 3/4: 点击购买面板", "TASK")
        buy_x = rect.right - 60
        buy_y = rect.bottom - 25
        self.safe_click(buy_x, buy_y, "点击右下角购买")
        self.record_checkpoint(index, "3_点击购买")

        # --- 步骤 4 ---
        log(f"[{index}] 步骤 4/4: 校验 SKU 界面", "TASK")
        region_sku = (rect.left, rect.bottom - h // 2, w, h // 2)
        is_sku_ready, _ = self.safe_ocr_wait(["确定", "请选择"], timeout=5, region=region_sku)

        if not is_sku_ready:
            path = os.path.join(ERROR_DIR, f"SKU_Error_{index}_{int(time.time())}.png")
            self.safe_screenshot(path)
            return False, path, "SKU面板未完全展开"

        # --- 步骤 5 ---
        log(f"[{index}] 🎯 断言全部通过！生成最终截图...", "TASK")
        success_path = os.path.join(SUCCESS_DIR, f"SUCCESS_{goods_id}_{int(time.time())}.png")
        self.safe_screenshot(success_path)
        return True, success_path, ""


# ==========================================
# 🚦 任务调度引擎
# ==========================================
def batch_runner(goods_id_list):
    state = load_state()

    # 过滤机制
    filtered_list = [gid for gid in goods_id_list
                     if not state.get(gid, {}).get("success", False)
                     and state.get(gid, {}).get("attempts", 0) < 30]

    print(f"\n{'=' * 60}")
    log(f"🚀 PDD 自动化 RPA 任务启动", "SYSTEM")
    log(f"📊 总任务: {len(goods_id_list)} | 跳过: {len(goods_id_list) - len(filtered_list)} | 待执行: {len(filtered_list)}",
        "INFO")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        log("✅ 所有任务均已完成或到达重试上限。", "SUCCESS")
        return

    bot = PddAutomation()
    success_count, fail_count = 0, 0

    for i, goods_id in enumerate(filtered_list, 1):
        print("\n" + "-" * 40)
        log(f"▶▶▶ 开始处理 [{i}/{len(filtered_list)}] goods_id: {goods_id}")

        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1
        save_state(state)

        try:
            if not bot.prepare_home_page():
                log(f"系统异常跳过", "WARN")
                state[goods_id]["error_msg"] = "系统级启动失败"
                fail_count += 1
                continue

            success, img_path, error_msg = bot.process_single_goods(goods_id, i)

            state[goods_id].update({
                "success": success,
                "image_path": img_path,
                "error_msg": error_msg
            })

            if success:
                log(f"✅ 处理成功 -> {img_path}", "SUCCESS")
                success_count += 1
            else:
                log(f"❌ 处理失败 -> {error_msg} 截图: {img_path}", "ERROR")
                fail_count += 1

        except Exception as e:
            log(f"💥 发生严重异常: {str(e)}", "FATAL")
            traceback.print_exc()
            fail_count += 1

            # 尝试记录崩溃现场
            try:
                crash_path = os.path.join(ERROR_DIR, f"CRASH_{goods_id}_{int(time.time())}.png")
                bot.safe_screenshot(crash_path)
                state[goods_id].update({
                    "success": False,
                    "image_path": crash_path,
                    "error_msg": f"代码异常: {str(e)}"
                })
            except:
                pass

        finally:
            save_state(state)
            time.sleep(0.3)  # 任务间缓冲

    print(f"\n{'=' * 50}")
    log(f"🎉 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}", "SYSTEM")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    test_goods_ids = [
        "997025592944",
        "702868469934"
    ]
    batch_runner(test_goods_ids)
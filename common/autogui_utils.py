import uiautomation as auto
import pyautogui
import pyperclip
import time
import os
import traceback
import json
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR
from typing import Tuple, Optional


# ==========================================
# ⚙️ 全局配置与初始化
# ==========================================
class Config:
    PDD_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\拼多多.lnk"
    DEBUG_MODE = True

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    SUCCESS_DIR = os.path.join(BASE_DIR, "results_success")
    ERROR_DIR = os.path.join(BASE_DIR, "results_error")
    TRACE_DIR = os.path.join(BASE_DIR, "results_trace")
    STATE_FILE = os.path.join(BASE_DIR, "goods_state.json")


# 确保目录存在
for d in [Config.SUCCESS_DIR, Config.ERROR_DIR, Config.TRACE_DIR]:
    os.makedirs(d, exist_ok=True)


# ==========================================
# 🧠 核心模块 1：状态管理器 (解耦本地存储)
# ==========================================
class StateManager:
    @staticmethod
    def load() -> dict:
        if os.path.exists(Config.STATE_FILE):
            try:
                with open(Config.STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[⚠️ 状态加载失败] {e}")
        return {}

    @staticmethod
    def save(state: dict):
        # 使用临时文件写入后重命名，防止写入中断导致 JSON 损坏
        temp_file = f"{Config.STATE_FILE}.tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=4)
        os.replace(temp_file, Config.STATE_FILE)


# ==========================================
# 👁️ 核心模块 2：视觉与 OCR 引擎
# ==========================================
class VisionEngine:
    def __init__(self):
        print("\n[⚙️ 系统] 正在初始化 RapidOCR 轻量级引擎...")
        self.ocr = RapidOCR()

    def wait_for_text(self, target_texts: list, capture_region: tuple, timeout: float = 5.0) -> bool:
        """
        动态视觉断言：只要识别到 target_texts 中的任意一个词即返回 True
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # 极速截图并转为 BGR 格式
                img = pyautogui.screenshot(region=capture_region)
                img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

                result, _ = self.ocr(img_cv)
                if result:
                    for line in result:
                        if len(line) >= 2:
                            text = line[1]
                            if any(t in text for t in target_texts):
                                return True
            except Exception as e:
                pass
            time.sleep(0.05)  # 缩短重试间隔，发现目标瞬间放行 (速度优化)
        return False


# ==========================================
# 🤖 核心模块 3：拼多多 RPA 机器人
# ==========================================
class PddAutomation:
    def __init__(self, vision_engine: VisionEngine):
        self.window = None
        self.vision = vision_engine

    def _safe_copy(self, text: str):
        """安全剪贴板操作，防止 Windows 剪贴板占用冲突"""
        for _ in range(3):
            try:
                pyperclip.copy(text)
                return
            except:
                time.sleep(0.1)
        raise Exception("剪贴板被其他程序锁死")

    def ensure_focus(self):
        """获取并锁定焦点，如果失败立刻抛出异常，防止盲点桌面的灾难"""
        if not self.window or not self.window.Exists(0.1, 0):
            raise RuntimeError("窗口不存在，无法获取焦点")
        try:
            # 判断是否已经在前台，减少无意义的 SetTopmost 闪烁
            if not self.window.IsTopmost:
                self.window.SetActive()
                self.window.SetTopmost(True)
                self.window.SetTopmost(False)
        except Exception as e:
            raise RuntimeError(f"无法置顶窗口，可能被安全软件拦截: {e}")

    def get_window_rect(self) -> Tuple[int, int, int, int]:
        self.ensure_focus()
        rect = self.window.BoundingRectangle
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def restart_mini_program(self) -> bool:
        """硬重启环境"""
        print("\n[⚙️ 系统] 准备环境，清理旧窗口...")
        old_window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if old_window.Exists(0.5, 0):
            old_window.SetActive()
            rect = old_window.BoundingRectangle
            close_x, close_y = rect.right - 25, rect.top + 20  # 修正右上角关闭按钮位置
            auto.Click(close_x, close_y)
            time.sleep(1.0)  # 等待窗口彻底消失

        try:
            print("  [🚀 启动] 正在唤醒小程序...")
            os.startfile(Config.PDD_SHORTCUT_PATH)
        except FileNotFoundError:
            print(f"  [❌ 致命错误] 未找到快捷方式: {Config.PDD_SHORTCUT_PATH}")
            return False

        self.window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if not self.window.Exists(10, 1):
            print("  [❌ 启动超时] 小程序主窗口未出现。")
            return False

        self.ensure_focus()
        print("  [✅ 启动成功] 小程序已就绪。")
        return True

    def prepare_home_page(self) -> bool:
        """软重启：动态校验首页，避免不必要的硬重启，大幅提高速度"""
        if not self.window or not self.window.Exists(0.5, 0):
            self.window = auto.WindowControl(searchDepth=1, Name='拼多多')
            if not self.window.Exists(0.5, 0):
                return self.restart_mini_program()

        print("\n  [🔄 准备状态] 校验并返回首页...")
        start_time = time.time()

        while time.time() - start_time < 8:
            left, top, w, h = self.get_window_rect()

            # 动态判断是否在首页 (底部 1/6 区域包含"首页")
            region_bottom = (left, top + h - (h // 6), w, h // 6)
            if self.vision.wait_for_text(["首页"], region_bottom, timeout=0.3):
                print("  [✅ 确认首页] 当前处于首页，准备执行。")
                return True

            # 不在首页，点击左上角返回
            try:
                auto.Click(left + 20, top + 60)
            except:
                pass
            time.sleep(0.3)

        print("  [⚠️ 超时] 未能通过返回键到达首页，执行兜底硬重启...")
        return self.restart_mini_program()

    def take_screenshot(self, save_dir: str, prefix: str, index: int) -> str:
        """统一截图方法"""
        left, top, w, h = self.get_window_rect()
        filename = f"{prefix}_{index}_{int(time.time())}.png"
        path = os.path.join(save_dir, filename)
        pyautogui.screenshot(path, region=(left, top, w, h))
        return path

    def process_single_goods(self, goods_id: str, index: int) -> Tuple[bool, str, str]:
        goods_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
        left, top, w, h = self.get_window_rect()

        # --- 步骤 1：输入搜索 ---
        print(f"[{index}] 步骤 1/4: 输入商品链接...")
        search_entry_x = left + w // 2
        search_entry_y = top + 65

        self.ensure_focus()
        auto.Click(search_entry_x, search_entry_y)

        self._safe_copy(goods_url)
        time.sleep(0.1)  # 等待焦点和剪贴板就绪
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.1)
        auto.SendKeys('{Enter}')

        if Config.DEBUG_MODE: self.take_screenshot(Config.TRACE_DIR, f"Step1_{index}", index)

        # --- 步骤 2：校验详情页 ---
        print(f"[{index}] 步骤 2/4: 校验详情页状态...")
        region_bottom = (left, top + h - (h // 6), w, h // 6)

        if not self.vision.wait_for_text(["客服", "店铺"], region_bottom, timeout=5):
            path = self.take_screenshot(Config.ERROR_DIR, "DETAIL_Error", index)
            return False, path, "进入商品详情页超时"

        if Config.DEBUG_MODE: self.take_screenshot(Config.TRACE_DIR, f"Step2_{index}", index)

        # --- 步骤 3：点击购买 ---
        print(f"[{index}] 步骤 3/4: 唤起 SKU 面板...")
        buy_x, buy_y = left + w - 60, top + h - 25
        self.ensure_focus()
        auto.Click(buy_x, buy_y)

        # --- 步骤 4：校验 SKU 面板 ---
        print(f"[{index}] 步骤 4/4: 校验 SKU 界面是否就绪...")
        region_sku = (left, top + h - (h // 2), w, h // 2)

        if not self.vision.wait_for_text(["确定", "请选择"], region_sku, timeout=5):
            print(f"[{index}] ❌ SKU 面板未就绪")
            path = self.take_screenshot(Config.ERROR_DIR, "SKU_Error", index)
            return False, path, "SKU 面板未展开"

        # --- 步骤 5：成功截图 ---
        print(f"[{index}] 🎯 验证通过！保存 SKU 截图...")
        path = self.take_screenshot(Config.SUCCESS_DIR, "SUCCESS_SKU", index)
        return True, path, ""


# ==========================================
# ⚙️ 任务调度器
# ==========================================
def batch_runner(goods_id_list: list):
    state = StateManager.load()

    # 过滤任务：跳过成功或超过阈值的任务
    filtered_list = [gid for gid in goods_id_list if
                     not state.get(gid, {}).get("success", False) and state.get(gid, {}).get("attempts", 0) < 5]

    print(f"\n{'=' * 60}")
    print(f"🚀 拼多多自动化 RPA 启动 (待执行 {len(filtered_list)} / 总计 {len(goods_id_list)})")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        print("✅ 所有任务均已完成，流程结束。")
        return

    vision = VisionEngine()
    bot = PddAutomation(vision)
    success_count, fail_count = 0, 0

    for i, goods_id in enumerate(filtered_list, 1):
        print(f"\n▶▶ [任务进度 {i}/{len(filtered_list)}] 处理商品: {goods_id}")

        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1
        StateManager.save(state)

        try:
            if not bot.prepare_home_page():
                raise Exception("系统级异常：无法到达首页")

            success, img_path, error_msg = bot.process_single_goods(goods_id, i)

            state[goods_id].update({"success": success, "image_path": img_path, "error_msg": error_msg})
            StateManager.save(state)

            if success:
                success_count += 1
            else:
                fail_count += 1

        except Exception as e:
            print(f"\n[💥 异常] {e}")
            fail_count += 1
            try:
                crash_path = bot.take_screenshot(Config.ERROR_DIR, "CRASH", i)
                state[goods_id].update({"success": False, "image_path": crash_path, "error_msg": str(e)})
                StateManager.save(state)
            except:
                pass

    print(f"\n{'=' * 50}\n✅ 任务完毕！成功: {success_count} | 失败: {fail_count}\n{'=' * 50}")


if __name__ == "__main__":
    test_goods_ids = ["997025592944", "702868469934"]
    batch_runner(test_goods_ids)
import uiautomation as auto
import pyautogui
import pyperclip
import time
import os
import traceback

# === 轻量级 OCR 依赖 ===
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR

# ==========================================
# ⚙️ 全局配置区
# ==========================================
PDD_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\拼多多.lnk"

# 调试模式开关：开启时，每一步都会强制保存截图供分析；稳定后可改为 False
DEBUG_MODE = True

# 初始化数据分类目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUCCESS_DIR = os.path.join(BASE_DIR, "results_success")
ERROR_DIR = os.path.join(BASE_DIR, "results_error")
TRACE_DIR = os.path.join(BASE_DIR, "results_trace")

for d in [SUCCESS_DIR, ERROR_DIR, TRACE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# 🚀 初始化轻量级 RapidOCR 引擎
print("\n[⚙️ 系统] 正在初始化 RapidOCR 轻量级引擎...")
ocr = RapidOCR()


class PddAutomation:
    def __init__(self):
        self.window = None

    def restart_mini_program(self):
        """基于物理坐标的硬重启，抹平异常状态"""
        print("\n[⚙️ 系统] 准备环境，检查是否需要清理旧窗口...")
        old_window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if old_window.Exists(1):
            old_window.SetActive()
            time.sleep(0.5)
            rect = old_window.BoundingRectangle
            close_x = rect.right - 25
            close_y = rect.top + 60
            print(f"  [🧹 清理] 发现残留窗口，点击关闭 ({close_x}, {close_y})...")
            auto.Click(close_x, close_y)
            time.sleep(1.5)

        try:
            print("  [🚀 启动] 正在唤醒小程序...")
            os.startfile(PDD_SHORTCUT_PATH)
        except FileNotFoundError:
            print(f"  [❌ 致命错误] 未找到快捷方式: {PDD_SHORTCUT_PATH}")
            return False

        self.window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if not self.window.Exists(10, 1):
            print("  [❌ 启动超时] 小程序主窗口未出现。")
            return False

        self.window.SetActive()
        self.window.SetTopmost(True)
        time.sleep(0.5)
        self.window.SetTopmost(False)
        print("  [✅ 启动成功] 小程序已就绪。")
        return True

    def record_checkpoint(self, index, step_name, force_record=False):
        """
        【视觉检查点】：保存当前画面的 Screenshot。让你能复盘每一步的实际页面状态。
        """
        if not (DEBUG_MODE or force_record):
            return

        timestamp = int(time.time())
        screenshot_name = f"Step{step_name}_{index}_{timestamp}.png"
        screenshot_path = os.path.join(TRACE_DIR, screenshot_name)

        print(f"  [📸 快照记录] 正在导出 {step_name} 的界面截图...")

        try:
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            pyautogui.screenshot(screenshot_path, region=region)
            print(f"    └─ ✅ 截图保存成功: {screenshot_name}")
        except Exception as e:
            print(f"    └─ ⚠️ 截图失败: {e}")

    def wait_for_ocr_text(self, target_text, timeout=5):
        """
        【核心方案：轻量级 OCR 视觉断言】
        动态截取当前窗口，直接扫描并提取画面上的文本字符，彻底解决黑盒不稳定的问题。
        """
        print(f"  [👁️ OCR扫描] 正在画面中识别文字: '{target_text}' (限时{timeout}s)...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                rect = self.window.BoundingRectangle
                region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

                # 截取局部画面并转换为 OpenCV 格式供模型读取
                img = pyautogui.screenshot(region=region)
                img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

                # 进行轻量级 OCR 识别
                result, elapse = ocr(img_cv)
                print(result)
                if result:
                    for line in result:
                        # RapidOCR 返回的 line 格式为 [box, text, score]
                        if len(line) >= 2:
                            text = line[1]  # 提取识别出的真实文本
                            if target_text in text:
                                return True
            except Exception as e:
                pass
            time.sleep(0.5)
        return False

    def take_final_screenshot(self, save_dir, filename_prefix, index):
        """保存最终结果的截图"""
        rect = self.window.BoundingRectangle
        screenshot_name = f"{filename_prefix}_{index}_{int(time.time())}.png"
        save_path = os.path.join(save_dir, screenshot_name)
        region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        pyautogui.screenshot(save_path, region=region)
        return save_path

    def process_single_goods(self, goods_url, index):
        rect = self.window.BoundingRectangle

        # ==========================================
        # 步骤 1：输入搜索
        # ==========================================
        print(f"[{index}] 步骤 1/4: 准备输入商品链接...")
        search_entry_x = rect.left + (rect.right - rect.left) // 2
        search_entry_y = rect.top + 65
        auto.Click(search_entry_x, search_entry_y)
        time.sleep(1)
        pyperclip.copy(goods_url)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.5)
        auto.SendKeys('{Enter}')

        # 🎯 捕捉点 1：输入链接后，点击搜索后马上记录截图
        time.sleep(2)  # 等待网络加载
        self.record_checkpoint(index, "1_点击搜索后")

        # ==========================================
        # 步骤 2：校验是否成功进入详情页
        # ==========================================
        print(f"[{index}] 步骤 2/4: 校验详情页状态...")

        # 【OCR 断言】
        is_detail_page = self.wait_for_ocr_text("客服", timeout=5) or self.wait_for_ocr_text("店铺", timeout=2)

        # 🎯 捕捉点 2：点击购买前，记录当前的真实详情页状态截图
        self.record_checkpoint(index, "2_点击购买前")

        # ==========================================
        # 步骤 3：点击购买按钮
        # ==========================================
        print(f"[{index}] 步骤 3/4: 点击右下角唤起 SKU 面板...")
        rect = self.window.BoundingRectangle
        buy_x = rect.right - 60
        buy_y = rect.bottom - 25
        auto.Click(buy_x, buy_y)
        time.sleep(2)  # 等待抽屉动画弹出

        # 🎯 捕捉点 3：点击购买后，记录唤起 SKU 面板后的瞬间截图
        self.record_checkpoint(index, "3_点击购买后")

        # ==========================================
        # 步骤 4：严格校验 SKU 面板是否完全展开
        # ==========================================
        print(f"[{index}] 步骤 4/4: 校验 SKU 界面是否完全就绪...")

        # 【OCR 断言】：直接扫描画面中是否存在“确定”或“请选择”字样
        is_sku_ready = self.wait_for_ocr_text("确定", timeout=5) or self.wait_for_ocr_text("请选择", timeout=2)

        if not is_sku_ready:
            print(f"[{index}] ❌ SKU 面板校验失败！可能是被挡住了，或者不是标准商品。")
            path = self.take_final_screenshot(ERROR_DIR, "SKU_Error", index)
            print(f"  └─ 📸 错误现场已保存至: {path}")
            return False

        # ==========================================
        # 步骤 5：一切验证通过，执行成功截图
        # ==========================================
        print(f"[{index}] 🎯 断言全部通过！执行最终 SKU 截图...")
        path = self.take_final_screenshot(SUCCESS_DIR, "SUCCESS_SKU", index)
        print(f"[{index}] ✅ 商品处理完美收官，真 SKU 图: {path}")
        return True


def batch_runner(url_list):
    bot = PddAutomation()
    success_count = 0
    fail_count = 0

    print(f"\n{'=' * 50}")
    print(f"🚀 拼多多自动化 RPA 任务启动 | 总计任务: {len(url_list)}")
    print(f"🔍 DEBUG模式状态: {'开启 (记录每步UI截图)' if DEBUG_MODE else '关闭'}")
    print(f"{'=' * 50}\n")

    for i, url in enumerate(url_list, 1):
        print(f"\n▶▶▶ [任务进度 {i}/{len(url_list)}] 正在处理链接: {url[-15:]}...")
        try:
            if not bot.restart_mini_program():
                print(f"[-] 第 {i} 个任务因系统级异常跳过")
                fail_count += 1
                continue

            if bot.process_single_goods(url, i):
                success_count += 1
            else:
                fail_count += 1

        except Exception as e:
            print(f"\n[💥 未知崩溃] 第 {i} 个商品发生严重异常!")
            traceback.print_exc()
            fail_count += 1
            try:
                crash_path = bot.take_final_screenshot(ERROR_DIR, "CRASH", i)
                print(f"  └─ 📸 崩溃现场图: {crash_path}")
            except:
                pass
        finally:
            print(f"⏹ 准备进行下一个 (冷却2秒)...")
            time.sleep(2)

    print(f"\n{'=' * 50}")
    print(f"✅ 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    test_urls = [
        "https://mobile.yangkeduo.com/goods.html?goods_id=997025592944",
        "https://mobile.yangkeduo.com/goods.html?goods_id=702868469934"
    ]
    batch_runner(test_urls)
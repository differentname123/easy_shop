import uiautomation as auto
import pyautogui
import pyperclip
import time
import os
import traceback
import json

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
STATE_FILE = os.path.join(BASE_DIR, "goods_state.json")

for d in [SUCCESS_DIR, ERROR_DIR, TRACE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# 🚀 初始化轻量级 RapidOCR 引擎
print("\n[⚙️ 系统] 正在初始化 RapidOCR 轻量级引擎...")
ocr = RapidOCR()


def load_state():
    """加载本地 JSON 状态文件"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_state(state):
    """保存状态到本地 JSON"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=4)


class PddAutomation:
    def __init__(self):
        self.window = None

    def activate_window(self):
        """核心封装：强制将窗口拉到最前，保证任何后续的点击或截图操作不被遮挡"""
        if self.window and self.window.Exists(0.1, 0):
            try:
                self.window.SetActive()
                self.window.SetTopmost(True)
                time.sleep(0.05)  # 给系统一点响应时间置顶
                self.window.SetTopmost(False)  # 恢复普通状态但已经在最上层
            except:
                pass

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

        self.activate_window()
        print("  [✅ 启动成功] 小程序已就绪。")
        return True

    def prepare_home_page(self):
        """软重启：尝试通过点击返回图标回到首页，10s超时后才硬重启"""
        # 1. 如果尚未绑定窗口句柄，先主动在桌面上寻找已存在的小程序窗口
        if not self.window:
            self.window = auto.WindowControl(searchDepth=1, Name='拼多多')

        # 2. 确认窗口是否存在
        if not self.window.Exists(0.5, 0):
            print("  [⚠️ 窗口丢失] 找不到拼多多窗口，直接执行硬重启...")
            return self.restart_mini_program()

        print("\n  [🔄 准备状态] 尝试返回首页...")
        start_time = time.time()

        while time.time() - start_time < 10:
            self.activate_window()  # 获取坐标前强制置顶
            rect = self.window.BoundingRectangle
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            try:
                print("  [🔙 返回] 尝试点击返回图标...")
                auto.Click(rect.left + 20, rect.top + 60)
                print("  [⏱️ 等待] 等待 0.5s 后再次检测首页...")
            except:
                pass




            # 精确裁剪小程序最下面的 1/6 区域进行 OCR 识别"首页"
            region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)
            if self.wait_for_ocr_text("首页", timeout=0.5, region=region_bottom):
                print("  [✅ 确认首页] 当前处于首页，准备执行下一步。")
                return True



            time.sleep(0.5)

        print("  [⚠️ 超时] 10s内未检测到首页，执行兜底硬重启...")
        return self.restart_mini_program()

    def record_checkpoint(self, index, step_name, force_record=False):
        """保存当前画面的 Screenshot"""
        if not (DEBUG_MODE or force_record):
            return

        timestamp = int(time.time())
        screenshot_name = f"Step{step_name}_{index}_{timestamp}.png"
        screenshot_path = os.path.join(TRACE_DIR, screenshot_name)

        try:
            self.activate_window()  # 截图记录前强制置顶，防止截到其他窗口
            rect = self.window.BoundingRectangle
            region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            pyautogui.screenshot(screenshot_path, region=region)
        except Exception as e:
            pass

    def wait_for_ocr_text(self, target_text, timeout=5, region=None):
        """
        【核心方案：轻量级 OCR 视觉断言】
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                self.activate_window()  # OCR 抓图前强制置顶，防止截到其他软件界面引发误判

                if region is None:
                    rect = self.window.BoundingRectangle
                    capture_region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
                else:
                    capture_region = region

                img = pyautogui.screenshot(region=capture_region)
                img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

                result, elapse = ocr(img_cv)
                if result:
                    for line in result:
                        if len(line) >= 2:
                            text = line[1]
                            if target_text in text:
                                return True
            except Exception as e:
                pass
            time.sleep(0.1)  # 缩短识别间隔，尽速放行
        return False

    def take_final_screenshot(self, save_dir, filename_prefix, index):
        """保存最终结果的截图"""
        self.activate_window()  # 最终留档截图前强制置顶
        rect = self.window.BoundingRectangle
        screenshot_name = f"{filename_prefix}_{index}_{int(time.time())}.png"
        save_path = os.path.join(save_dir, screenshot_name)
        region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        pyautogui.screenshot(save_path, region=region)
        return save_path

    def process_single_goods(self, goods_id, index):
        goods_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"

        self.activate_window()  # 获取初始窗口坐标前置顶
        rect = self.window.BoundingRectangle
        w = rect.right - rect.left
        h = rect.bottom - rect.top

        # ==========================================
        # 步骤 1：输入搜索
        # ==========================================
        print(f"[{index}] 步骤 1/4: 准备输入商品链接...")
        search_entry_x = rect.left + w // 2
        search_entry_y = rect.top + 65

        self.activate_window()  # 鼠标点击搜索框前强制置顶
        auto.Click(search_entry_x, search_entry_y)
        time.sleep(0.2)  # 给搜索框一点获取焦点的反应时间

        self.activate_window()  # 键盘输入前强制置顶，防止焦点丢失
        pyperclip.copy(goods_url)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.2)
        auto.SendKeys('{Enter}')

        self.record_checkpoint(index, "1_点击搜索后")

        # ==========================================
        # 步骤 2：校验是否成功进入详情页
        # ==========================================
        print(f"[{index}] 步骤 2/4: 校验详情页状态...")
        region_bottom = (rect.left, rect.bottom - h // 6, w, h // 6)
        is_detail_page = self.wait_for_ocr_text("客服", timeout=5, region=region_bottom) or \
                         self.wait_for_ocr_text("店铺", timeout=2, region=region_bottom)

        if not is_detail_page:
            path = self.take_final_screenshot(ERROR_DIR, "DETAIL_Error", index)
            return False, path, "进入商品详情页失败或超时"

        self.record_checkpoint(index, "2_点击购买前")

        # ==========================================
        # 步骤 3：点击购买按钮
        # ==========================================
        print(f"[{index}] 步骤 3/4: 点击右下角唤起 SKU 面板...")
        buy_x = rect.right - 60
        buy_y = rect.bottom - 25

        self.activate_window()  # 点击购买按钮前强制置顶
        auto.Click(buy_x, buy_y)

        self.record_checkpoint(index, "3_点击购买后")

        # ==========================================
        # 步骤 4：严格校验 SKU 面板是否完全展开
        # ==========================================
        print(f"[{index}] 步骤 4/4: 校验 SKU 界面是否完全就绪...")
        region_sku = (rect.left, rect.bottom - h // 2, w, h // 2)
        is_sku_ready = self.wait_for_ocr_text("确定", timeout=5, region=region_sku) or \
                       self.wait_for_ocr_text("请选择", timeout=2, region=region_sku)

        if not is_sku_ready:
            print(f"[{index}] ❌ SKU 面板校验失败！可能是被挡住了，或者不是标准商品。")
            path = self.take_final_screenshot(ERROR_DIR, "SKU_Error", index)
            return False, path, "SKU 面板未完全展开或找不到确定按钮"

        # ==========================================
        # 步骤 5：一切验证通过，执行成功截图
        # ==========================================
        print(f"[{index}] 🎯 断言全部通过！执行最终 SKU 截图...")
        path = self.take_final_screenshot(SUCCESS_DIR, "SUCCESS_SKU", index)
        print(f"[{index}] ✅ 商品处理完美收官，真 SKU 图: {path}")
        return True, path, ""


def batch_runner(goods_id_list):
    state = load_state()
    initial_count = len(goods_id_list)

    # 执行过滤机制
    filtered_list = []
    for gid in goods_id_list:
        info = state.get(gid, {})
        if info.get("success", False) or info.get("attempts", 0) >= 30:
            continue
        filtered_list.append(gid)

    print(f"\n{'=' * 60}")
    print(f"🚀 拼多多自动化 RPA 任务启动")
    print(
        f"📊 过滤统计: 总任务 {initial_count} 个 | 过滤跳过 {initial_count - len(filtered_list)} 个 (已成功或超次) | 实际待执行 {len(filtered_list)} 个")
    print(f"🔍 DEBUG模式状态: {'开启' if DEBUG_MODE else '关闭'}")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        print("✅ 所有任务均已完成或到达重试上限，流程结束。")
        return

    bot = PddAutomation()
    success_count = 0
    fail_count = 0

    for i, goods_id in enumerate(filtered_list, 1):
        print(f"\n▶▶▶ [任务进度 {i}/{len(filtered_list)}] 正在处理 goods_id: {goods_id}...")

        # 初始化 JSON 中的商品状态结构
        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1
        save_state(state)  # 立即保存尝试次数，防止崩溃时丢失

        try:
            # 开始不再无脑硬启动，而是尝试确定处于首页
            if not bot.prepare_home_page():
                print(f"[-] 第 {i} 个任务因系统级异常跳过")
                state[goods_id]["error_msg"] = "系统级异常/无法启动"
                save_state(state)
                fail_count += 1
                continue

            # 处理单个商品流程
            success, img_path, error_msg = bot.process_single_goods(goods_id, i)

            # 更新状态并写入 JSON
            state[goods_id]["success"] = success
            state[goods_id]["image_path"] = img_path
            state[goods_id]["error_msg"] = error_msg
            save_state(state)

            if success:
                success_count += 1
            else:
                fail_count += 1

        except Exception as e:
            print(f"\n[💥 未知崩溃] 第 {i} 个商品发生严重异常!")
            traceback.print_exc()
            fail_count += 1
            try:
                crash_path = bot.take_final_screenshot(ERROR_DIR, "CRASH", i)
                state[goods_id]["success"] = False
                state[goods_id]["image_path"] = crash_path
                state[goods_id]["error_msg"] = f"代码运行崩溃: {str(e)}"
                save_state(state)
                print(f"  └─ 📸 崩溃现场图: {crash_path}")
            except:
                pass

        # 为了稳定，短暂无脑间隔即可，无需要硬性等待2s
        time.sleep(0.5)

    print(f"\n{'=' * 50}")
    print(f"✅ 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    # 输入参数改为纯 goods_id 列表
    test_goods_ids = [
        "997025592944",
        "702868469934"
    ]
    batch_runner(test_goods_ids)
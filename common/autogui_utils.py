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
        【自愈核心】检测非期望弹窗并自动消除，强行将中转站恢复至【情况2】干净状态：
        - 遇到【情况1】残留的拼多多跳转弹窗 -> 点击“取消”（防止带错商品参数）
        - 遇到【情况3】不支持拖入文件弹窗 -> 点击“确定”消除
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
                    log("[HEAL] 检测到'当前小程序不支持拖入文件'弹窗，已自动点击'确定'")
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
                    log("[HEAL] 检测到残留的'即将打开拼多多'弹窗，已自动点击'取消'")
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

        # 最终校验是否已进入洁净的【情况2】
        wrap_modal = self.window.GroupControl(ClassName="wrap")
        if wrap_modal.Exists(0, 0):
            return False
        return True

    def set_transfer_goods_id(self, goods_id, max_attempts=3):
        """
        【输入与双向绑定保障】真实按键事件驱动 + 严格值校验，彻底规避双向绑定不更新及张冠李戴
        """
        target_str = str(goods_id).strip()
        edit_box = self.window.EditControl()
        if not edit_box.Exists(2, 1):
            return False, "无法定位中转小程序的输入框"

        for attempt in range(1, max_attempts + 1):
            # 1. 真实点击聚焦输入框
            edit_box.Click(simulateMove=False)
            time.sleep(0.02)

            # 2. 彻底清空内容
            auto.SendKeys('{Ctrl}a{Delete}')
            time.sleep(0.02)

            # 3. 极速粘贴并附加真实键盘按键，强行触发小程序底层的 bindinput 监听
            pyperclip.copy(target_str)
            auto.SendKeys('{Ctrl}v')
            time.sleep(0.02)

            # 核心步骤：产生真实的按键输入流（空格+退格），确保小程序数据模型彻底响应
            auto.SendKeys(' {Back}')
            time.sleep(0.03)

            # 4. 严格值校验
            current_val = self.get_control_text(edit_box).strip()
            if current_val == target_str:
                return True, ""

            log(f"[WARN] 输入校验不匹配 (期望: '{target_str}', 实际: '{current_val}')，尝试第 {attempt} 次全按键重输")

            # 兜底方案：纯按键逐字敲击输入
            edit_box.Click(simulateMove=False)
            time.sleep(0.02)
            auto.SendKeys('{Ctrl}a{Delete}')
            time.sleep(0.02)
            auto.SendKeys(target_str)
            time.sleep(0.03)

            current_val = self.get_control_text(edit_box).strip()
            if current_val == target_str:
                return True, ""

        return False, f"中转站输入框赋值校验失败: 界面当前值为 '{current_val}'，并非目标商品 ID '{target_str}'"

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

        # --- 步骤 1.5：环境自愈（确保恢复至【情况2】） ---
        if not self.ensure_transfer_clean_state():
            return False, "", "中转小程序存在无法自动关闭的弹窗，环境未能恢复至正常状态"

        # --- 步骤 2：精确写入商品 ID 并严格比对 ---
        log(f"[TASK] [{index}] 步骤 1/4: 向中转站写入商品 ID")
        input_ok, input_err = self.set_transfer_goods_id(goods_id)
        if not input_ok:
            # 校验失败严禁点击跳转，彻底切断张冠李戴可能
            return False, "", input_err

        log(f"[TASK] [{index}] 步骤 2/4: 触发 API 跳转")
        jump_btn = self.window.ButtonControl(Name="跳转到该商品")
        if not jump_btn.Exists(1, 1):
            return False, "", "无法定位中转小程序的跳转按钮"

        try:
            jump_btn.GetInvokePattern().Invoke()
        except Exception:
            jump_btn.Click(simulateMove=False)

        # --- 步骤 2.5：处理微信跳转授权弹窗 ---
        log(f"[TASK] [{index}] 步骤 2.5/4: 处理微信跳转授权弹窗")
        allow_btn = self.window.TextControl(Name="允许")
        if allow_btn.Exists(2, 0.2):
            try:
                allow_btn.GetInvokePattern().Invoke()
            except Exception:
                allow_btn.Click(simulateMove=False)
            log("[INFO] 已自动点击“允许”跳转")
        else:
            log("[INFO] 未检测到“允许”弹窗（可能已静默放行或系统延迟）")

        # --- 步骤 3 & 4：接管拼多多并进行状态机轮询 ---
        log(f"[TASK] [{index}] 步骤 3/4: 状态机轮询 (详情页识别 -> 动态点击 -> SKU捕获)")
        self.window_name = TARGET_APP_NAME

        if not self.find_window(timeout=5):
            return False, "", "未检测到拼多多窗口弹出（跳转可能失败）"

        self.force_bring_to_front()
        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top

        # 将区域放大为底部 1/4，这样既能盖住详情页的“客服/店铺”，也能盖住 SKU 弹窗底部的“确定”
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
                    # 把识别到的文字拼接起来，方便进行多关键词判断
                    detected_text = "".join([line[1] for line in result if len(line) >= 2])

                    # 状态 A：必须是在判断过 "客服" 和 "店铺" (detail_entered 为 True) 之后，才能判断 SKU
                    if detail_entered and any(kw in detected_text for kw in ["确定", "请选择", "已选"]):
                        sku_ready = True
                        break

                    # 状态 B：如果依然停留在详情页，则动态点击购买
                    if any(kw in detected_text for kw in ["客服", "店铺"]):
                        detail_entered = True
                        # 无延迟点击目标位置，如果被吞了，下一次 while 循环又会进来重新点击
                        self.safe_click(rect.right - 60, rect.bottom - 25, "动态点击购买")
                        # 仅做极小延时，防止 UI 线程被点死
                        time.sleep(0.3)

            except Exception:
                pass

        # 轮询结束，进行结果清算
        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.close_current_window()
            return False, path, "进入详情页失败"

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            self.fast_screenshot_save(path)
            self.close_current_window()
            return False, path, "SKU面板未完全展开或点击全部失效"

        # 流程圆满成功，保存截图
        success_path = os.path.join(SUCCESS_DIR, f"{goods_id}.png")
        self.fast_screenshot_save(success_path)
        log(f"[TASK] [{index}] 🎯 成功生成最终截图。")

        # 连续成功清理策略
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
                     and state.get(gid, {}).get("attempts", 0) < 3]

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

from datetime import datetime, timedelta, timezone
from contextlib import closing
# 确保你的文件中已经导入了下面这两个模块
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager
def get_data_updated_within_24h(limit=0, extra_query=None, projection=None):
    """
    查询最近 24 小时内更新的商品数据（基于 updated_at 字段）。

    :param limit: 返回的最大文档数，0 表示不限制。
    :param extra_query: dict, 额外的 MongoDB 查询条件。例如: {"format_status": "success"}。
    :param projection: dict, 需要返回的字段映射。例如: {"_id": 1, "product_id": 1, "name": 1}。
    :return: list, 包含查询结果的字典列表。
    """
    # 1. 计算 24 小时前的时间阈值（使用 UTC 时间，与项目时区保持一致）
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=12)

    # 2. 构建基础查询条件
    query_condition = {
        "updated_at": {"$gte": time_threshold}
    }

    # 3. 合并额外查询条件（如果不为空）
    if extra_query and isinstance(extra_query, dict):
        # 避免直接覆盖原字典引发引用冲突
        query_condition = {**query_condition, **extra_query}

    # 4. 获取数据库连接并执行查询
    # 使用 closing 语法糖，确保哪怕查询中途发生异常，数据库连接也能被正确关闭
    with closing(gen_db_object()) as db_instance:
        db_instance.ping()  # 探活
        product_manager = ProductManager(db_instance)

        # 执行查询，默认按更新时间倒序排列（最新的在最前）
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
            need_sku_product_id_list = read_json("mihoutao_sku_product_id.json")

            results = get_data_updated_within_24h(limit=0, extra_query={"format_status": "success"}, projection={"product_id": 1, "_id": 0})
            need_sku_product_id_list = [item["product_id"] for item in results]

            batch_runner(need_sku_product_id_list)
        except Exception as e:
            log(f"[FATAL] 💥 主程序异常退出: {str(e)}")
        time.sleep(3600)
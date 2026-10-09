import os
import time
import subprocess
import random
import io
from datetime import datetime, timedelta, timezone
from contextlib import closing

# === 图像与OCR依赖 ===
import numpy as np
import cv2
from rapidocr_onnxruntime import RapidOCR

# === 项目内依赖 (请确保这些模块在你的环境中正常存在) ===
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager
from common.common_utils import read_json, save_json

# ==========================================
# ⚙️ 全局配置区
# ==========================================
ADB_PATH = r"E:\chrome\platform-tools-latest-windows\platform-tools\adb.exe"
PACKAGE_NAME = "com.xunmeng.pinduoduo"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUCCESS_DIR = os.path.join(BASE_DIR, "results_success_adb")
ERROR_DIR = os.path.join(BASE_DIR, "results_error_adb")
STATE_FILE = os.path.join(BASE_DIR, "goods_state_abd.json")

for d in [SUCCESS_DIR, ERROR_DIR]:
    os.makedirs(d, exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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
        except Exception:
            pass
    return {}


def save_state(state):
    save_json(STATE_FILE, state)


# ==========================================
# 📱 ADB 基础控制层
# ==========================================
def is_pdd_in_foreground():
    """检测拼多多是否为当前前台应用"""
    cmd = f'"{ADB_PATH}" shell dumpsys window'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if not res.stdout:
        return False
    for line in res.stdout.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            if PACKAGE_NAME in line:
                return True
    return False


def ensure_pdd_foreground():
    """保证应用处于前台（保活/唤起）"""
    if not is_pdd_in_foreground():
        log("[ADB] 检测到拼多多未在前台，正在唤起/切至前台...")
        cmd = f'"{ADB_PATH}" shell monkey -p {PACKAGE_NAME} -c android.intent.category.LAUNCHER 1'
        subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        time.sleep(2.5)


def restart_pdd():
    """强制停止与重启"""
    log("[ADB] 正在强制停止并重启拼多多...")
    # 添加 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL 来屏蔽 monkey 唤起时的系统日志
    subprocess.run(
        f'"{ADB_PATH}" shell am force-stop {PACKAGE_NAME}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1.5)
    subprocess.run(
        f'"{ADB_PATH}" shell monkey -p {PACKAGE_NAME} -c android.intent.category.LAUNCHER 1',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(4)


# ==========================================
# 🤖 RPA 核心引擎 (ADB 版)
# ==========================================
class PddAdbBot:
    def __init__(self):
        self.width, self.height = self.get_device_resolution()
        if not self.width:
            raise RuntimeError("无法获取手机分辨率，请检查 ADB 连接！")
        log(f"[SYSTEM] 设备已连接，屏幕分辨率: {self.width} x {self.height}")

    def get_device_resolution(self):
        res = subprocess.run(f'"{ADB_PATH}" shell wm size', shell=True, capture_output=True, text=True,
                             encoding="utf-8", errors="ignore")
        if "Physical size" in res.stdout:
            w, h = map(int, res.stdout.strip().split()[-1].split('x'))
            return w, h
        return 0, 0

    def click(self, x, y, desc=""):
        if desc: log(f"[ACTION] 点击 {desc} ({x}, {y})")
        subprocess.run(f'"{ADB_PATH}" shell input tap {x} {y}', shell=True)

    def input_text(self, text, desc=""):
        if desc: log(f"[ACTION] 输入 {desc}: {text}")
        # ADB input text 传输 URL 时，为了防止 Android shell 误将 ? 或 & 当作命令符，需要进行转义
        escaped_text = text.replace('?', r'\?').replace('=', r'\=').replace('&', r'\&')
        cmd = f'"{ADB_PATH}" shell input text "{escaped_text}"'
        subprocess.run(cmd, shell=True)

    def get_screenshot_cv(self):
        """极速获取 ADB 截图并转换为 OpenCV BGR 格式，不落盘"""
        proc = subprocess.Popen(
            f'"{ADB_PATH}" exec-out screencap -p',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        raw_bytes, _ = proc.communicate()
        if not raw_bytes:
            return None

        # 将 raw_bytes 转为 numpy array，再解码为 cv2 图像
        nparr = np.frombuffer(raw_bytes, np.uint8)
        img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img_cv

    def ensure_home(self):
        """【自愈与归位机制】确保当前在拼多多首页，不在则点击左上角返回"""
        start_time = time.time()

        while time.time() - start_time < 10:
            img = self.get_screenshot_cv()
            if img is None:
                continue

            # 截取底部 1/6 区域进行 OCR，判断是否含有"首页"
            h, w = img.shape[:2]
            bottom_region = img[int(h * 5 / 6):h, 0:w]

            result, _ = ocr(bottom_region)
            text = "".join([line[1] for line in result if len(line) >= 2]) if result else ""

            if "首页" in text:
                return True

            # 如果不是首页，点击左上角返回按钮 (换算自坐标 52, 131)
            back_x = int(w * 0.029)
            back_y = int(h * 0.0455)
            self.click(back_x, back_y, "左上角返回")
            time.sleep(0.8)  # 留出页面动画退出的时间

        # 5秒内未能回到首页，直接重启
        log("[WARN] 5秒内未能返回首页，强制重启拼多多")
        restart_pdd()
        return False

    def process_single_goods(self, goods_id, index, consecutive_successes=0):
        ensure_pdd_foreground()

        # --- 步骤 1：确保处于首页 ---
        log(f"[TASK] [{index}] 步骤 1/4: 确保处于拼多多首页")
        if not self.ensure_home():
            return False, "", "无法定位到首页，已记录为失败"

        # --- 步骤 2：点击搜索框 ---
        log(f"[TASK] [{index}] 步骤 2/4: 点击顶部搜索框并输入")
        # Y轴 控制在 4.5% - 6.5% 之间，取 5.5%，X轴取屏幕正中
        search_bar_y = int(self.height * 0.0455)

        search_bar_x = self.width // 2
        self.click(search_bar_x, search_bar_y, "首页搜索框")
        time.sleep(0.8)  # 等待唤起输入键盘

        # 拼接商品链接并输入
        target_link = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        self.input_text(target_link, "商品跳转链接")
        time.sleep(0.5)

        # --- 步骤 3：点击搜索按钮 ---
        log(f"[TASK] [{index}] 步骤 3/4: 点击搜索确认按钮")
        # 搜索按钮在最右侧，适当往左边偏一点点 (约 90% 宽度处)
        search_btn_x = int(self.width * 0.95)
        self.click(search_btn_x, search_bar_y, "搜索按钮")

        # --- 步骤 4：状态机轮询 (详情页识别 -> 动态点击 -> SKU捕获) ---
        log(f"[TASK] [{index}] 步骤 4/4: 状态机轮询抓取 SKU")
        start_time = time.time()
        detail_entered = False
        sku_ready = False
        final_img = None

        while time.time() - start_time < 10:
            img = self.get_screenshot_cv()
            if img is None:
                continue

            final_img = img
            h, w = img.shape[:2]

            # 为了提高 OCR 速度，只识别屏幕底部 1/4 区域
            bottom_region = img[int(h * 0.75):h, 0:w]
            result, _ = ocr(bottom_region)
            detected_text = "".join([line[1] for line in result if len(line) >= 2]) if result else ""

            # 状态 A：识别到 SKU 弹窗特征
            if detail_entered and any(kw in detected_text for kw in ["确定", "请选择", "已选"]):
                sku_ready = True
                break

            # 状态 B：识别到处于详情页底部栏，动态点击发起购买
            if any(kw in detected_text for kw in ["客服", "店铺", "收藏"]):
                detail_entered = True
                # 点击右下角触发购买 SKU (约 85% 宽度, 95% 高度处)
                buy_x = int(w * 0.85)
                buy_y = int(h * 0.95)
                self.click(buy_x, buy_y, "底部购买/发起拼单按钮")
                time.sleep(0.5)

        # 轮询结束，清算结果
        if final_img is None:
            return False, "", "无法获取到屏幕截图"

        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            cv2.imwrite(path, final_img)
            return False, path, "未能成功跳转到商品详情页"

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            cv2.imwrite(path, final_img)
            return False, path, "SKU面板未弹出或点击失效"

        # 成功流程
        success_path = os.path.join(SUCCESS_DIR, f"{goods_id}.png")
        cv2.imwrite(success_path, final_img)
        log(f"[TASK] [{index}] 🎯 成功生成最终截图。")

        # 连续成功清理策略
        if (consecutive_successes + 1) % 50 == 0:
            log(f"[INFO] 循环连轴转达到 50 次，重启拼多多释放内存。")
            restart_pdd()

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
    log("[SYSTEM] 🚀 ADB 级直连真机 RPA 引擎启动")
    log(f"[INFO] 📊 待执行任务数: {len(filtered_list)}")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        return

    bot = PddAdbBot()
    success_count, fail_count = 0, 0
    consecutive_successes = 0

    for i, goods_id in enumerate(filtered_list, 1):
        print("\n" + "-" * 40)
        log(f"[INFO] ▶▶▶ 开始处理 [{i}/{len(filtered_list)}] goods_id: {goods_id}")

        if goods_id not in state:
            state[goods_id] = {"success": False, "image_path": "", "error_msg": "", "attempts": 0}

        state[goods_id]["attempts"] += 1

        try:
            success, img_path, error_msg = bot.process_single_goods(goods_id, i, consecutive_successes)
            state[goods_id].update({"success": success, "image_path": img_path, "error_msg": error_msg})

            if success:
                log(f"[SUCCESS] ✅ 处理成功 ({goods_id})")
                success_count += 1
                consecutive_successes += 1
            else:
                log(f"[ERROR] ❌ 处理失败 ({goods_id}) -> {error_msg}")
                fail_count += 1
                consecutive_successes = 0  # 失败清零

        except Exception as e:
            log(f"[FATAL] 💥 发生异常: {str(e)}")
            fail_count += 1
            consecutive_successes = 0

            # 异常发生后，尝试强制恢复真机环境
            restart_pdd()

        finally:
            save_state(state)

    print(f"\n{'=' * 50}")
    log(f"[SYSTEM] 🎉 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


# ==========================================
# 💾 数据库查询与入口
# ==========================================
def get_data_updated_within_24h(limit=0, extra_query=None, projection=None):
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=12)

    query_condition = {
        "updated_at": {"$gte": time_threshold}
    }

    if extra_query and isinstance(extra_query, dict):
        query_condition = {**query_condition, **extra_query}

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


if __name__ == "__main__":
    while True:
        try:
            target_category_list = ["可乐", "洗洁精", "洗衣液", "冲牙器"]

            results = get_data_updated_within_24h(
                limit=0,
                extra_query={"format_status": "success"},
                projection={"product_id": 1, "name": 1, "sku_info": 1, "_id": 0}
            )

            filtered_results = [
                item for item in results
                if any(keyword in item.get("name", "") for keyword in target_category_list)
            ]

            filtered_results = [
                item for item in filtered_results
                if not item.get("sku_info")
            ]

            need_sku_product_id_list = [item["product_id"] for item in filtered_results]

            batch_runner(need_sku_product_id_list)

        except Exception as e:
            log(f"[FATAL] 💥 主程序异常: {str(e)}")

        time.sleep(3600)
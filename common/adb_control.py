import os
import time
import subprocess
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
VIA_PACKAGE = "mark.via"
PDD_PACKAGE = "com.xunmeng.pinduoduo"

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
# 📱 ADB 基础控制层 (支持多应用)
# ==========================================
def is_app_in_foreground(package_name):
    """检测指定应用是否为当前前台应用"""
    cmd = f'"{ADB_PATH}" shell dumpsys window'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if not res.stdout:
        return False
    for line in res.stdout.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            if package_name in line:
                return True
    return False


def ensure_app_foreground(package_name):
    """保证应用处于前台"""
    if not is_app_in_foreground(package_name):
        log(f"[ADB] 正在唤起/切至前台: {package_name}")
        cmd = f'"{ADB_PATH}" shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1'
        subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")

        # 动态等待：一旦检测到应用已在前台即刻放行
        for _ in range(10):
            if is_app_in_foreground(package_name):
                time.sleep(0.5)  # 稍微缓冲等待UI渲染完成
                break
            time.sleep(0.5)


def restart_app(package_name):
    """强制停止与重启"""
    log(f"[ADB] 正在强制停止并重启: {package_name}")
    subprocess.run(
        f'"{ADB_PATH}" shell am force-stop {package_name}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)
    subprocess.run(
        f'"{ADB_PATH}" shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    # 动态等待替代原先死等的 time.sleep(4)
    for _ in range(10):
        if is_app_in_foreground(package_name):
            time.sleep(0.5)  # 页面出现后稍作缓冲即可
            break
        time.sleep(0.5)


# ==========================================
# 🤖 RPA 核心引擎 (Via浏览器桥接版)
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

    def click_relative(self, pct_x, pct_y, desc=""):
        """使用相对比例点击屏幕，适配所有分辨率"""
        x = int(self.width * pct_x)
        y = int(self.height * pct_y)
        if desc: log(f"[ACTION] 点击 {desc} (相对: {pct_x:.3f}, {pct_y:.3f} -> 绝对: {x}, {y})")
        subprocess.run(f'"{ADB_PATH}" shell input tap {x} {y}', shell=True)

    def input_text(self, text, desc=""):
        if desc: log(f"[ACTION] 输入 {desc}: {text}")
        escaped_text = text.replace('?', r'\?').replace('=', r'\=').replace('&', r'\&')
        cmd = f'"{ADB_PATH}" shell input text "{escaped_text}"'
        subprocess.run(cmd, shell=True)

    def get_screenshot_cv(self):
        proc = subprocess.Popen(
            f'"{ADB_PATH}" exec-out screencap -p',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        raw_bytes, _ = proc.communicate()
        if not raw_bytes:
            return None
        nparr = np.frombuffer(raw_bytes, np.uint8)
        img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img_cv

    def process_single_goods(self, goods_id, index, consecutive_successes=0):
        # 步骤 1: 直接重启 Via 浏览器
        log(f"[TASK] [{index}] 步骤 1/3: 重启 Via 浏览器")
        restart_app(VIA_PACKAGE)

        # 步骤 2: 瞬间注入商品链接 (利用底层 Intent 替代 UI 点击与打字)
        log(f"[TASK] [{index}] 步骤 2/3: 瞬间唤起浏览器并打开商品链接")
        target_link = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"

        # 🚀 核心优化：直接通过 am start 将 URL 传给 Via 浏览器，瞬间打开，告别逐字输入
        cmd = f'"{ADB_PATH}" shell am start -a android.intent.action.VIEW -d "{target_link}" {VIA_PACKAGE}'
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 给予浏览器响应和触发拼多多跳转的缓冲时间
        time.sleep(1.5)

        # 步骤 3: 状态机轮询等待拼多多拉起 -> 详情页识别 -> 购买点击 -> SKU捕获
        log(f"[TASK] [{index}] 步骤 3/3: 等待应用跳转并抓取 SKU")
        start_time = time.time()
        detail_entered = False
        sku_ready = False
        final_img = None

        # 因为有跨应用跳转过程，超时时间稍微放宽至 15 秒
        while time.time() - start_time < 15:
            img = self.get_screenshot_cv()
            if img is None:
                continue

            final_img = img
            h, w = img.shape[:2]

            # 识别屏幕底部 1/4 区域
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
                self.click_relative(0.85, 0.95, "底部购买/发起拼单")
                time.sleep(0.8)

        # 清算结果
        if final_img is None:
            return False, "", "无法获取到屏幕截图"

        if not detail_entered:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            cv2.imwrite(path, final_img)
            return False, path, "未能跳转到商品详情页(超时)"

        if not sku_ready:
            path = os.path.join(ERROR_DIR, f"{goods_id}.png")
            cv2.imwrite(path, final_img)
            return False, path, "SKU面板未弹出或点击失效"

        # 成功流程
        success_path = os.path.join(SUCCESS_DIR, f"{goods_id}.png")
        cv2.imwrite(success_path, final_img)
        log(f"[TASK] [{index}] 🎯 成功生成最终截图。")

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
    log("[SYSTEM] 🚀 Via桥接-直连真机 RPA 引擎启动")
    log(f"[INFO] 📊 待执行任务数: {len(filtered_list)}")
    print(f"{'=' * 60}\n")

    if not filtered_list:
        return

    # 最开始重启一下拼多多
    log("[INFO] 🔧 初始化启动：重启拼多多客户端...")
    restart_app(PDD_PACKAGE)

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
                consecutive_successes = 0

        except Exception as e:
            log(f"[FATAL] 💥 发生异常: {str(e)}")
            fail_count += 1
            consecutive_successes = 0
            # 异常发生后，尝试强制恢复 Via 浏览器
            restart_app(VIA_PACKAGE)

        finally:
            save_state(state)

            # 每处理 10 个 ID 重启一下拼多多
            if i % 10 == 0 and i != len(filtered_list):
                log(f"[INFO] ♻️ 已处理 {i} 个任务，定期重启拼多多客户端清理内存...")
                restart_app(PDD_PACKAGE)

    print(f"\n{'=' * 50}")
    log(f"[SYSTEM] 🎉 批量任务完毕！ 成功: {success_count} | 失败: {fail_count}")
    print(f"{'=' * 50}")


# ==========================================
# 💾 数据库查询与入口
# ==========================================
def get_data_updated_within_24h(limit=0, extra_query=None, projection=None):
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=12)
    query_condition = {"updated_at": {"$gte": time_threshold}}

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
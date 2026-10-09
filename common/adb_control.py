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
        time.sleep(2.5)


def restart_app(package_name):
    """强制停止与重启"""
    log(f"[ADB] 正在强制停止并重启: {package_name}")
    subprocess.run(
        f'"{ADB_PATH}" shell am force-stop {package_name}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1.5)
    subprocess.run(
        f'"{ADB_PATH}" shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(4)


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

    def clear_input_box(self):
        """高效清空输入框文本：移动光标至末尾并发送多次删除指令"""
        log("[ACTION] 清空搜索框内容...")
        # KEYCODE_MOVE_END (123) 移动到末尾
        subprocess.run(f'"{ADB_PATH}" shell input keyevent 123', shell=True)
        # 连续发送35次 KEYCODE_DEL (67)
        del_events = " ".join(["67"] * 35)
        subprocess.run(f'"{ADB_PATH}" shell input keyevent {del_events}', shell=True)
        time.sleep(1)

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
        # 步骤 1: 确保 Via 浏览器在前台
        log(f"[TASK] [{index}] 步骤 1/4: 确保 Via 浏览器处于前台")
        ensure_app_foreground(VIA_PACKAGE)
        time.sleep(1)

        # 步骤 2: 点击 Via 搜索框并清空内容
        log(f"[TASK] [{index}] 步骤 2/4: 点击 Via 搜索框并校验")
        # 搜索框中心点转换: 1800x2880下 (150+1400)/2=775 -> 43.06%, (75+160)/2=117.5 -> 4.08%
        self.click_relative(0.4306, 0.0408, "Via 搜索框")
        time.sleep(1)
        self.clear_input_box()

        # 截图校验清空是否彻底
        img = self.get_screenshot_cv()
        if img is None:
            return False, "", "无法获取截图校验搜索框"

        # 搜索框区域边界相对换算:
        # X: 150/1800=0.0833, 1400/1800=0.7778
        # Y: 75/2880=0.0260, 160/2880=0.0556
        x1, x2 = int(self.width * 0.0833), int(self.width * 0.7778)
        y1, y2 = int(self.height * 0.0260), int(self.height * 0.0556)

        # 截取对应区域进行OCR
        search_box_img = img[y1:y2, x1:x2]
        result, _ = ocr(search_box_img)
        detected_text = "".join([line[1] for line in result if len(line) >= 2]) if result else ""

        log(f"[OCR] 搜索框当前识别文本内容: '{detected_text}'")
        if len(detected_text) > 5:
            log("[WARN] 搜索框未清空干净 (字数>5)，重启 Via 浏览器并记为失败。")
            restart_app(VIA_PACKAGE)
            return False, "", "输入框未清空干净"

        # 步骤 3: 输入链接并点击"访问网址"
        log(f"[TASK] [{index}] 步骤 3/4: 输入商品链接并访问")
        target_link = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        self.input_text(target_link, "商品链接")
        time.sleep(0.5)

        # 访问网址按钮转换: 1741/1800=96.72%, 123/2880=4.27%
        self.click_relative(0.9672, 0.0427, "访问网址 按钮")

        # 步骤 4: 状态机轮询等待拼多多拉起 -> 详情页识别 -> 购买点击 -> SKU捕获
        log(f"[TASK] [{index}] 步骤 4/4: 等待应用跳转并抓取 SKU")
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

        # 内存释放策略: 连续50次成功后清理 Via 和 拼多多
        if (consecutive_successes + 1) % 50 == 0:
            log(f"[INFO] 循环连轴转达到 50 次，执行垃圾回收(重启应用)。")
            subprocess.run(f'"{ADB_PATH}" shell am force-stop {PDD_PACKAGE}', shell=True)
            restart_app(VIA_PACKAGE)

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

    bot = PddAdbBot()
    success_count, fail_count = 0, 0
    consecutive_successes = 0

    for i, goods_id in enumerate(filtered_list, 1):
        # 修复了原代码中的 "-" 40 语法错误
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
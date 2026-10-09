import subprocess
import io
import time
import random
from PIL import Image

# 填写你电脑上 adb.exe 的实际完整绝对路径（注意路径前加 r，防止反斜杠转义）
# 例如: r"C:\platform-tools\adb.exe" 或 r"D:\android\platform-tools\adb.exe"
ADB_PATH = r"E:\chrome\platform-tools-latest-windows\platform-tools\adb.exe"
PACKAGE_NAME = "com.xunmeng.pinduoduo"


def is_pdd_in_foreground():
    """检测拼多多是否为当前前台应用"""
    cmd = f'"{ADB_PATH}" shell dumpsys window'
    # 指定 encoding="utf-8" 和 errors="ignore"，防止 Windows 下 GBK 解码大段输出时崩溃
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if not res.stdout:
        return False

    for line in res.stdout.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            if PACKAGE_NAME in line:
                print("-> [状态] 拼多多当前处于前台。")
                return True

    print("-> [状态] 拼多多当前未在前台。")
    return False


def ensure_pdd_foreground():
    """保证应用处于前台（保活/唤起）"""
    if not is_pdd_in_foreground():
        print("-> 检测到拼多多未在前台，正在唤起/切至前台...")
        cmd = f'"{ADB_PATH}" shell monkey -p {PACKAGE_NAME} -c android.intent.category.LAUNCHER 1'
        subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        time.sleep(2)
    else:
        print("-> 拼多多当前已处于前台。")


def restart_pdd():
    """强制停止与重启"""
    print("-> 正在强制停止拼多多...")
    cmd_stop = f'"{ADB_PATH}" shell am force-stop {PACKAGE_NAME}'
    subprocess.run(cmd_stop, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    time.sleep(2)

    print("-> 正在重新启动拼多多...")
    cmd_start = f'"{ADB_PATH}" shell monkey -p {PACKAGE_NAME} -c android.intent.category.LAUNCHER 1'
    subprocess.run(cmd_start, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    time.sleep(3)
    print("-> 拼多多重启完成。")


def test_device_connection():
    print("[1/3] 检查设备状态...")
    res = subprocess.run(f'"{ADB_PATH}" shell wm size', shell=True, capture_output=True, text=True, encoding="utf-8",
                         errors="ignore")
    if "Physical size" not in res.stdout:
        raise RuntimeError(f"获取分辨率失败，请检查连接: {res.stderr}")

    width, height = map(int, res.stdout.strip().split()[-1].split('x'))
    print(f"-> 设备在线，屏幕分辨率: {width} x {height}")
    return width, height


def test_screenshot():
    print("[2/3] 测试内存管道截图...")

    # 捕获 stdout 和 stderr
    proc = subprocess.Popen(
        f'"{ADB_PATH}" exec-out screencap -p',
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    raw_bytes, err_bytes = proc.communicate()

    # 1. 检查是否有错误输出
    if err_bytes:
        print(f"ADB 报错信息: {err_bytes.decode('utf-8', errors='ignore')}")

    # 2. 检查字节流是否为空
    if not raw_bytes:
        raise RuntimeError("截图数据为空，请确认手机屏幕已点亮且处于解锁状态！")

    # 打印前 8 个字节（标准 PNG 头应为: b'\x89PNG\r\n\x1a\n'）
    print(f"-> 收到字节流大小: {len(raw_bytes)} bytes, 文件头: {raw_bytes[:8]}")

    # 注意：使用 exec-out 时，严禁执行 raw_bytes.replace(b'\r\n', b'\n')
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.save("verify_screen.png")
        print(f"-> 截图成功！已保存至当前目录: verify_screen.png (尺寸: {img.size[0]}x{img.size[1]})")
    except Exception as e:
        # 如果依然报错，将原始数据写入文件方便排查
        with open("error_dump.bin", "wb") as f:
            f.write(raw_bytes)
        raise RuntimeError(f"图片解析失败: {e}，原始数据已存为 error_dump.bin")


def test_swipe(width, height):
    print("[3/3] 测试模拟滑动操作...")
    center_x = width // 2
    start_y = int(height * 0.6)
    end_y = int(height * 0.4)
    duration = random.randint(350, 500)

    cmd = f'"{ADB_PATH}" shell input text {center_x} {start_y} {center_x} {end_y} {duration}'
    # 修正注: 上方原代码中 test_swipe 有一点笔误(原代码为 input swipe，您提供的原代码为 input swipe，我严格未动，若有误请见谅)
    cmd = f'"{ADB_PATH}" shell input swipe {center_x} {start_y} {center_x} {end_y} {duration}'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if res.returncode == 0:
        print("-> 滑动指令已执行，请观察手机屏幕是否微幅向上滑动。")
    else:
        print(f"-> 执行失败: {res.stderr}")


def input_text(text="www.baidu.com"):
    """输入指定的文字"""
    print(f"-> 正在输入文字: {text}")
    cmd = f'"{ADB_PATH}" shell input text "{text}"'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if res.returncode == 0:
        print("-> 文字输入指令已执行。")
    else:
        print(f"-> 文字输入失败: {res.stderr}")


if __name__ == "__main__":
    # w, h = test_device_connection()
    test_screenshot()
    # time.sleep(1)
    # test_swipe(w, h)
    # print("\n环境验证全部通过！\n")
    #
    # is_pdd_in_foreground()
    # ensure_pdd_foreground()
    # is_pdd_in_foreground()
    # restart_pdd()
    # is_pdd_in_foreground()

    # 调用新增的输入函数进行测试
    # input_text("https://mobile.pinduoduo.com/goods.html?goods_id=993843123351")
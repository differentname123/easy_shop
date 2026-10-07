import uiautomation as auto
import pyautogui
import pyperclip
import time
import os
import traceback

# ==========================================
# ⚙️ 全局配置区
# ==========================================
# 请务必将这里替换为您电脑桌面上“拼多多”快捷方式的真实绝对路径
# 获取方法：去桌面找到拼多多图标 -> 右键 -> 属性 -> 复制“目标”或“起始位置”的路径
PDD_SHORTCUT_PATH = r"C:\Users\zxh\Desktop\拼多多.lnk"


class PddAutomation:
    def __init__(self):
        self.window = None

    def restart_mini_program(self):
        """核心机制：基于物理坐标的硬重启，抹平一切异常状态"""
        print("[*] 准备环境，检查是否需要清理旧窗口...")

        # 1. 寻找是否残留了旧窗口（比如卡在 SKU 页面的窗口）
        old_window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if old_window.Exists(1):
            old_window.SetActive()
            time.sleep(0.5)
            rect = old_window.BoundingRectangle

            # 【核心修复】：基于您 inspect 抓取到的黄金坐标比例
            # 无论窗口怎么移动，关闭按钮永远在右上角 (向左偏移 25，向下偏移 60)
            close_x = rect.right - 25
            close_y = rect.top + 60

            print(f"[*] 发现残留窗口，正在精准点击右上角关闭按钮 ({close_x}, {close_y})...")
            auto.Click(close_x, close_y)
            time.sleep(1.5)  # 给微信销毁小程序进程留出时间

        # 2. 通过桌面快捷方式重新拉起纯净版首页
        try:
            print("[*] 正在从桌面快捷方式唤醒小程序...")
            os.startfile(PDD_SHORTCUT_PATH)
        except FileNotFoundError:
            print(f"[-] 严重错误：未找到快捷方式，请检查路径: {PDD_SHORTCUT_PATH}")
            return False

        time.sleep(3.5)  # 等待小程序冷启动加载完毕

        # 3. 捕获新窗口并置顶
        self.window = auto.WindowControl(searchDepth=1, Name='拼多多')
        if not self.window.Exists(3):
            print("[-] 小程序启动失败或超时，可能微信卡顿")
            return False

        self.window.SetActive()
        self.window.SetTopmost(True)
        time.sleep(0.5)
        self.window.SetTopmost(False)
        return True

    def process_single_goods(self, goods_url, index):
        """处理单个商品的核心流程"""
        rect = self.window.BoundingRectangle

        # --- 第一步：点击首页顶部的搜索框 ---
        print(f"[{index}] 正在点击首页搜索栏...")
        search_entry_x = rect.left + (rect.right - rect.left) // 2
        search_entry_y = rect.top + 65
        auto.Click(search_entry_x, search_entry_y)
        time.sleep(1.5)

        # --- 第二步：输入链接并跳转 ---
        print(f"[{index}] 正在粘贴链接并搜索...")
        pyperclip.copy(goods_url)
        auto.SendKeys('{Ctrl}v')
        time.sleep(0.5)
        auto.SendKeys('{Enter}')
        print(f"[{index}] 正在等待详情页加载...")
        time.sleep(5)

        # --- 第三步：盲点右下角购买按钮 ---
        print(f"[{index}] 尝试展开 SKU 面板...")
        rect = self.window.BoundingRectangle  # 重新获取防偏移
        buy_x = rect.right - 60
        buy_y = rect.bottom - 25

        auto.Click(buy_x, buy_y)
        time.sleep(2)

        # --- 第四步：局部截图 ---
        screenshot_path = f"sku_result_{index}.png"
        region = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        pyautogui.screenshot(screenshot_path, region=region)
        print(f"[+] 第 {index} 个商品 SKU 截图成功，已保存至 {screenshot_path}")

        return screenshot_path


def batch_runner(url_list):
    """
    批量调度中心：
    负责容错、隔离异常、保证整个列表能稳健跑完
    """
    bot = PddAutomation()
    success_count = 0

    for i, url in enumerate(url_list, 1):
        print(f"\n{'=' * 45}")
        print(f"▶ 开始处理任务 {i}/{len(url_list)}")

        try:
            # 1. 每次循环前，必须成功执行硬重启
            if not bot.restart_mini_program():
                print(f"[-] 第 {i} 个任务因重启失败跳过")
                continue

            # 2. 执行单个商品的抓取流程
            bot.process_single_goods(url, i)
            success_count += 1

        except Exception as e:
            # 【异常接管】：如果有任何环节报错（没加载出来、点错了、验证码），
            # 异常会被挡在这里，绝不会导致程序闪退，下一个商品会重新重启恢复！
            print(f"[-] 第 {i} 个商品发生意外崩溃: {str(e)}")
            traceback.print_exc()
        finally:
            print(f"⏹ 第 {i} 个商品流程结束")
            time.sleep(1)

    print(f"\n✅ 批量任务全部运行完毕！总计: {len(url_list)}，成功: {success_count}，失败: {len(url_list) - success_count}")


# ==========================================
# 🚀 启动入口
# ==========================================
if __name__ == "__main__":
    # 在这里放入您要批量处理的多个链接，即可测试循环效果
    test_urls = [
        "https://mobile.yangkeduo.com/goods.html?goods_id=997025592944",
        "https://mobile.yangkeduo.com/goods.html?goods_id=702868469934"
    ]
    batch_runner(test_urls)
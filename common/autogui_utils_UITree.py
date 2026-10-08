import uiautomation as auto
import time
import os


def export_ui_tree():
    app_name = "合力汇"
    print(f"[1/3] 正在寻找名为【{app_name}】的窗口...")

    # 限制查找深度，提升速度
    window = auto.WindowControl(searchDepth=1, Name=app_name)

    # 【修复】去掉 timeout 关键字，直接传：最大等待3秒，每1秒轮询一次
    if not window.Exists(3, 1):
        print(f"❌ 找不到名为【{app_name}】的窗口，请确保小程序已打开并显示在屏幕上。")
        return

    # 强行置顶激活，确保内部元素完成渲染
    try:
        window.SetActive()
        window.SetTopmost(True)
        time.sleep(1)
    except Exception as e:
        print(f"置顶窗口时出现小提示（可忽略）: {e}")

    print(f"[2/3] 窗口已捕获！正在深度遍历内部 UI 树（可能需要几秒钟）...")

    output_file = "ui_tree_dump.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        # 使用 auto.WalkControl 遍历
        for control, depth in auto.WalkControl(window, includeTop=True, maxDepth=15):
            indent = "  " * depth

            # 提取边界坐标，只要元素存在，坐标就一定存在
            rect = control.BoundingRectangle
            rect_str = f"[{rect.left},{rect.top},{rect.right},{rect.bottom}]" if rect else "[无坐标]"

            # 拼接日志字符串
            line = f"{indent}- type:{control.ControlTypeName} | name:'{control.Name}' | class:'{control.ClassName}' | rect:{rect_str}\n"
            print(line, end="")
            f.write(line)

    try:
        window.SetTopmost(False)
    except:
        pass

    print(f"\n[3/3] 🎉 UI 树已成功导出！文件保存在: {os.path.abspath(output_file)}")


if __name__ == '__main__':
    export_ui_tree()
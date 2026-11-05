"""
demo.py — Demo 阶段一键演示脚本（第 3 步成果）

一条命令跑通"观察→判断→操作"完整闭环：
    确保模拟器运行 → 等安卓就绪 → 自动校准偏移 → 截图 → 匹配模板 → 点击

用法：
    python demo.py <index> <模板.png> [--threshold 0.85] [--package 包名]

    --package：匹配前先启动指定 APP（游戏包名，如 com.sqw.fzdl.dldlcc_bz）

配合模板采集（先把画面框选存成模板）：
    python -m core.vision cut <index> <x> <y> <w> <h> <模板.png>
"""

import sys

from core import capture, ldplayer, vision


def main():
    if len(sys.argv) < 3:
        print("用法：python demo.py <index> <模板.png> [--threshold 0.85] [--package 包名]")
        sys.exit(1)

    idx = int(sys.argv[1])
    tpl_path = sys.argv[2]
    threshold = 0.85
    package = None
    for i, arg in enumerate(sys.argv[3:]):
        if arg.startswith("--threshold"):
            threshold = float(arg.split("=", 1)[1] if "=" in arg else sys.argv[i + 4])
        if arg == "--package":
            package = sys.argv[i + 4]

    # 1. 确保实例在运行
    inst = ldplayer.get_instance(idx)
    if inst is None:
        print(f"找不到实例 {idx}")
        sys.exit(1)
    if not inst.running:
        print("实例未运行，正在启动...")
        ldplayer.launch(idx)
    print("等待安卓就绪...", end=" ")
    if ldplayer.wait_until_ready(idx):
        print("OK")
    else:
        print("超时！")
        sys.exit(1)

    # 2. （可选）启动游戏
    if package:
        print(f"启动 APP：{package}")
        ldplayer.runapp(idx, package)

    # 3. 截图 + 自动校准
    screen, calib = vision.get_screen(idx)
    if screen is None:
        sys.exit(1)
    if calib:
        print(f"校准完成：安卓画面偏移=({calib['offset_x']},{calib['offset_y']}) "
              f"相似度={calib['score']:.4f}")

    # 4. 读模板 → 匹配
    tpl = capture.load_image_cn(tpl_path)
    if tpl is None:
        print(f"模板读不出来：{tpl_path}")
        sys.exit(1)
    print(f"匹配模板 {tpl_path}（阈值 {threshold}）...")
    hit = vision.match_template(screen, tpl, threshold)
    if hit is None:
        print("未找到。提示：画面可能不在预期页面，或阈值太高/模板已过期")
        sys.exit(1)

    # 5. 点击模板中心
    cx, cy = hit["x"] + tpl.shape[1] // 2, hit["y"] + tpl.shape[0] // 2
    print(f"找到！相似度={hit['score']:.4f}  点击中心 ({cx},{cy})")
    ldplayer.adb_shell(idx, f"input tap {cx} {cy}")
    print("已点击。观察模拟器画面是否出现了预期反应")


if __name__ == "__main__":
    main()

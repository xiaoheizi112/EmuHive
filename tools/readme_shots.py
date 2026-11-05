# 生成 README「界面速览」截图（真 windows 平台渲染以拿到中文字体，窗口一闪而过；
# 不碰模拟器、不写真配置——雷电枚举打桩、配置走临时单例）
# 用法：python tools/readme_shots.py
# 产物：docs/imgs/shot_main.png（主界面）/ shot_workbench.png（工作台流程画布）
# 说明：用临时配置单例 + 演示流程（取自 examples/示例流程包.zip 并加料），
#       保证截图里出现的流程名/模板名都是合成演示数据，不含任何真实游戏资产。
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ["QT_QPA_PLATFORM"] = "windows"  # offscreen 拿不到中文字体（截出豆腐块），必须真平台

import core.config_manager as cm

_tmp = tempfile.mkdtemp()
cm._config = cm.ConfigManager(os.path.join(_tmp, "config.json"))

# 演示模板（PNG + 账本）从示例包解出：节点缩略图与校验栏都认账本
import core.template_manager as tm
tm.TEMPLATES_DIR.mkdir(exist_ok=True)
with zipfile.ZipFile(ROOT / "examples" / "示例流程包.zip") as z:
    for info in z.infolist():
        rel = info.filename.split("templates/", 1)[-1]
        if not rel or rel.endswith("/"):
            continue
        dest = tm.TEMPLATES_DIR / rel
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(z.read(info))

# 装两条演示流程：示例包原样 + 一条多类型混合（判断/计算/信号等，展示画布全貌）
with zipfile.ZipFile(ROOT / "examples" / "示例流程包.zip") as z:
    pack = json.loads(z.read("flows.json"))
flows = dict(pack["custom_flows"])
flows["演示·判断与分支"] = {
    "version": 2,
    "nodes": [
        {"id": "n1", "type": "wait_template", "x": 360, "y": 40,
         "label": "等【开始按钮】出现（15 秒超时）",
         "category": "示例", "name": "按钮_开始", "timeout": 15,
         "exists": True, "on_timeout": "fail"},
        {"id": "n2", "type": "branch", "x": 360, "y": 180, "label": "判断：弹窗在不在？",
         "cond": {"kind": "template", "category": "示例", "name": "按钮_开始"},
         "timeout": 1},
        {"id": "n3", "type": "click_template", "x": 180, "y": 330, "label": "点【开始按钮】",
         "category": "示例", "name": "按钮_开始", "wait_before": False,
         "timeout": 5, "on_timeout": "fail", "jitter_px_min": 0, "jitter_px_max": 5},
        {"id": "n4", "type": "sleep", "x": 540, "y": 330, "label": "等 3 秒",
         "seconds": 3},
        {"id": "n5", "type": "compute", "x": 180, "y": 470, "label": "count = count + 1",
         "expr": "count + 1", "var_name": "count"},
        {"id": "n6", "type": "signal", "x": 540, "y": 470, "label": "发信号：完成",
         "name": "完成"},
        {"id": "n7", "type": "stop_flow", "x": 360, "y": 600, "label": "结束",
         "scope": "end"},
    ],
    "edges": [
        {"from": "n1", "port": "out", "to": "n2", "to_port": "in"},
        {"from": "n2", "port": "true", "to": "n3", "to_port": "in"},
        {"from": "n2", "port": "false", "to": "n4", "to_port": "in"},
        {"from": "n3", "port": "out", "to": "n5", "to_port": "in"},
        {"from": "n4", "port": "out", "to": "n6", "to_port": "in"},
        {"from": "n5", "port": "out", "to": "n7", "to_port": "in"},
        {"from": "n6", "port": "out", "to": "n7", "to_port": "in"},
    ],
}
cfg = cm._config
cfg.set("custom_flows", flows)
cfg.set("flow_categories", {k: "演示" for k in flows})  # 结构：{流程名: 组名}
cfg.set("flow_category_order", ["演示"])
cfg.set("task_bar", list(flows))
cfg.save()

import core.capture as capture
capture.find_render_window = lambda h: None  # 绝不枚举真窗口句柄

# 预览假帧：540×960 合成"游戏主界面"（渐变底 + 面板框 + 把示例按钮贴正中，
# 与流程节点找的目标同款——截图里预览画面和画布等的是同一张图，故事自洽）
import cv2
import numpy as np
_bg = np.zeros((960, 540, 3), np.uint8)
for _y in range(960):
    _bg[_y] = (60 + _y // 12, 40 + _y // 16, 90 + _y // 8)
cv2.rectangle(_bg, (30, 40), (510, 150), (120, 80, 40), -1)
cv2.rectangle(_bg, (30, 40), (510, 150), (180, 140, 90), 2)
cv2.rectangle(_bg, (30, 700), (510, 760), (90, 60, 30), -1)
for _i in range(5):
    cv2.circle(_bg, (75 + _i * 100, 850), 28, (100, 90, 60), -1)
_btn_p = tm.TEMPLATES_DIR / "示例" / "按钮_开始.png"
_btn = cv2.imdecode(np.fromfile(str(_btn_p), dtype=np.uint8), cv2.IMREAD_COLOR)
if _btn is not None:
    _x, _y = 540 // 2 - _btn.shape[1] // 2, 960 // 2 - _btn.shape[0] // 2
    _bg[_y:_y + _btn.shape[0], _x:_x + _btn.shape[1]] = _btn
capture.adb_frame = lambda idx, **kw: _bg.copy()

# 雷电侧全部打桩：假实例（0/1 运行、2/3 未启）+ 假安装路径，
# 截图绝不带出本机真实模拟器名/真实盘符。假 ldconsole.exe 落盘（存在性校验要过），
# 用完即删，只删自己建的文件
FAKE_DIR = Path(r"D:\leidian\LDPlayer9")
FAKE_CONSOLE = FAKE_DIR / "ldconsole.exe"
_we_made_it = False
if not FAKE_CONSOLE.exists():
    FAKE_DIR.mkdir(parents=True, exist_ok=True)
    FAKE_CONSOLE.write_bytes(b"placeholder for README screenshot")
    _we_made_it = True
import core.ldplayer as ld
ld.start_instance_poller = lambda *a, **k: None
ld.LD_CONSOLE = str(FAKE_CONSOLE)
ld._resolve_console = lambda: str(FAKE_CONSOLE)
ld.list_instances = lambda: [
    ld.InstanceInfo(index=i, title=f"模拟器 {i}", top_hwnd=0, bind_hwnd=0,
                    android_started=1 if i < 2 else 0, pid=1000 + i if i < 2 else 0,
                    extra=0)
    for i in range(4)
]

# 建表 + 灌几行合成演示账号（data/ 被 gitignore，仅本机截图用）
import core.database as db
db.init_db()
if not db.list_accounts():
    for i in range(4):
        db.add_account(i, name=f"演示{i + 1}号", account=f"demo00{i + 1}",
                       password="******", server="示例服")

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

IMG_DIR = ROOT / "docs" / "imgs"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def pump(ms=600):
    """让布局/绘制真正跑一会儿（含各 0.25s 定时器至少一拍）。"""
    t0 = time.time()
    while time.time() - t0 < ms / 1000.0:
        app.processEvents()
        time.sleep(0.02)


from ui.main_window import MainWindow

win = MainWindow()
win.resize(1240, 800)
win.feature_panel.spn_count.setValue(4)  # 目标数量与假实例台数对齐（免 5/4 打架）
win.show()
pump(900)
win.grab().save(str(IMG_DIR / "shot_main.png"))
print("saved shot_main.png")

from ui.workbench import WorkbenchDialog

wb = WorkbenchDialog()
wb.resize(1340, 850)
wb.show()
pump(500)
wb.view_flow("演示·判断与分支")
pump(900)
wb.grab().save(str(IMG_DIR / "shot_workbench.png"))

# 第三张：切到「模板采集」页（page_stack 第 1 页 = tpl_panel）
wb.page_stack.setCurrentIndex(1)
pump(500)
wb.grab().save(str(IMG_DIR / "shot_collect.png"))
wb.page_stack.setCurrentIndex(0)
print("saved shots")

win.close()
wb.close()
if _we_made_it:  # 只清自己建的东西
    FAKE_CONSOLE.unlink(missing_ok=True)
    try:
        FAKE_DIR.rmdir()          # 目录空才删得掉，不空说明是别人的，留着
        FAKE_DIR.parent.rmdir()
    except OSError:
        pass
os._exit(0)  # 截图已落盘，跳过 Qt 拆机（拆机线程噪声会污染退出码）

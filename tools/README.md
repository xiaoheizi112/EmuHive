# tools/ 命令速查（AI 做流程流水线）

命令速查**唯一出处**——参数以各脚本 argparse 实测为准，改参数只改这一页。
流水线语义与迭代打法见 docs/AI做流程流水线.md；此处只列命令。

| 步 | 命令与参数 |
|---|---|
| 第0步 对时 | `python tools/vid_align.py 窗口录屏.mp4 --probe` 抽首帧画 20px 网格、人读安卓内容区盒子；`python tools/vid_align.py 窗口录屏.mp4 --box x,y,w,h [--out 出片.mp4]` 按视频像素裁出内容区并缩回 540×960，产出 `<名>_aligned.mp4`（帧坐标=基准坐标）。整窗录制（OBS 等）才需要；盒子不落配置，同窗口大小照抄上次值复用 |
| ① 抽帧 | `python tools/vid_frames.py 视频.mp4 [--fps 1] [--scene 0.3] [--out 目录]` 定距+换场两路，默认落 `_ai_work/<视频名>/frames/` 附 manifest.json；`--at 12.5` 只取该秒一帧（放大看瞬间） |
| ② 裁模板 | `python tools/frame_cut.py 帧.png 模板名 --lib 库 (--rect x,y,w,h \| --snap x0,y0,x1,y1) [--threshold 0.85] [--base 540x960] [--force]`。`--rect` 目测框直裁；`--snap` 粗种子区贴边（core.vision.snap_bbox 吸真边）并回帧自检、分 <0.95 打 WARN（旧 frame_snap 已并入此路）。坐标一律 540×960 基准；同名拒写，--force 覆盖需用户确认 |
| ③ 写流程 | `python tools/flow_apply.py 流程.json --name 流程名 [--force]` 编译校验（bot.tasks.flow.compile_graph）后写 config 的 custom_flows；坏图/同名（无 --force）当场拒、不落盘 |
| ④ 试跑 | `python tools/flow_run.py 流程名 [--index 0] [--max-seconds 180]` 无头同步跑，日志直打；退出码 0=跑完/主动结束 2=失败 3=超时截停/环境不对；失败现场截图在 logs/fail_capture |
| 探场备用 | `python -m core.vision match 0 模板.png [--threshold 0.85] [--click]` 单测找图（--click 找到即点）；`python -m core.vision shot 0 现场.png` 存当前画面 |
| 死图圈定 | `python -m core.vision health <index> [--frame 画面.png] [--lib 库名] [--json out.json]` 整屏逐张扫全库模板、列死图（分数<各自阈值）；改版当天先跑，--json 报告给 AI 流水线圈图重采 |
| 容量压测 | `python tools/bench_capacity.py --indexes 0-11 [--rounds 20] [--budget 0.5] [--json bench.json]` 实测"能挂几台"（实例须已启动；判据超拍率≤5% 且 p95≤1.5×预算）；`--demo` 合成负载只验链路 |

注：旧 `tools/frame_snap.py` 已删除（功能并入 frame_cut `--snap`），勿再新增引用。

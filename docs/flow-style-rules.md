# AI 写流程规范（照用户手工习惯提炼，2026-10-04 从 20 条自有流程统计）

AI 生成 custom_flows 时**必须**遵守以下惯例。数据来源：登录/黑市/成神之路/人物点赞/
单次点赞等全部手写流程的节点参数统计与接线模式。

## 1. 状态幂等：动作前先 branch 闸门（最重要的一条）

每个点击动作前插一个 `branch`（cond=template，**timeout 1**）判断「目标状态是否已达成/
入口是否可见」，true 才点，false 走下一步或悬空。整条流程**从任意中间状态都能起**，
重跑不点错（菜单已展开就不再点展开）。实测统计：branch 条件 61/64 是 template，
timeout=1 占 40 处（瞬时查状态）、timeout=5 占 16 处（进新页面后等动画落定再查锚点）。

```
branch(按钮_展开, timeout 1) ──true──> click(按钮_展开) ──> 下一步
        └──────────────false──────────────────────────────> 下一步
```

## 2. 动作节点一律默认参数，不要自创

click_template / wait_template：`wait_timeout: 8`、`on_timeout: "fail"`、
`jitter_px_min: 0, jitter_px_max: 5`——手写 63 处全部如此，一个例外没有。别改。

## 3. 列表批量处理 = multi_scan 遍历 + 单次子流程，不是手写循环+滑动

对一列人逐个做同一套动作时，用 `multi_scan`：
- 锚模板选**每行都有的稳定小图标**（如 标签_战斗力图标），`roi` 竖条锁图标列；
- `mode: "enter"` + `offset_x/offset_y` 偏移到真正入口（头像）；
- `flow_name` 指向「单次XX」子流程——扫到一个、进一个、跑一遍子流程；
- `confirm_tpl` + `confirm_timeout: 10`：子流程回来必须确认还停在列表页才扫下一个；
- `settle_secs: 0.8`、`gap_secs: 0.5`、`max_hits: 20`、`var_name` 记遍历命中数。

「单次XX」独立成流程：开头 branch（锚点_页面, **timeout 5**）确认页面就绪，true 才做
动作链，false 悬空=本轮静默收工。主流程只管调度，不掺具体动作。

## 4. 文字型页签/按钮优先 server_pick 识字点

页签这类「文字稳定、样式会变」的目标用 `server_pick`（text + roi 锁条带，
min_sim 60，on_timeout 按场景 exit/fail），比模板匹配抗皮肤/抗换字。

## 5. 命名与库

- 模板名前缀体系：`锚点_`（状态判据）、`入口_`、`按钮_`、`标签_`、`红点_`、`关闭窗口_`。
- 手工库名=功能流程名（人物点赞、黑市…）；AI 生成一律进 `AI生成-*` 库（红线不变），
  但命名照前缀体系。
- 流程名直接用人话功能名；子流程叫「单次XX」。

## 6. 少用 sleep 和 watchdog

手写流程 0 处 watchdog、sleep 仅登录段出现——等待一律靠 branch/wait_template 的
条件闸门消化，不用硬等。循环固定次数（loop max_times）也不是你的习惯，能靠
multi_scan「扫到没有为止」就不数次数。

## 7. 端口接线

false 口常接「跳过本动作继续主线」（33 处），fail 口少用（6 处，只在真需要
失败兜底路径时接）。out/true 是主干。

# 流程 JSON 速查（AI 直写 custom_flows 用）

人看的制作思路在 flow-authoring-guide.md；这份是**机器口径**：字段名、默认值、
端口语义，全部来自 ui/flow_editor.new_node 与 bot/tasks/flow.compile_graph 实测。

## 图骨架

```json
{ "version": 2,
  "nodes": [ {"id": "a1", "type": "wait_template", "x": 0, "y": 0, ...参数} ],
  "edges": [ {"from": "a1", "port": "out", "to": "b2", "to_port": "in"} ] }
```

- `id`：流程内唯一随便起（编辑器用 8 位 hex）。`x/y` 只是画布摆位，不影响执行。
- **入口 = 零入边的节点，开局即就绪**；多条出线 = 下游并行；多条入边 = 汇合点，
  全部入边有结论才执行。
- `port` 取值：`out`（普通成功口）/ `true` `false`（判断节点专属）/ `fail`
  （动作节点的失败口，只有 FAILABLE 类型认，接了线失败才不判死整图）。
- 成环 = 循环执行（引擎不拒环）；`watch`/`timer` 是哨兵永不收口、零端口不用接线。
- 顶层可选 `"watchdog_sec": 120`：整图连续这么多秒没节点收口 → 判失败。

## 节点类型与默认参数（照抄即可，缺键编译侧有兜底）

| type | 干什么 | 关键参数（默认值） |
|---|---|---|
| `click_template` | 找到图就点它 | `category`库名 `name`模板名 `wait_before`(true) `wait_timeout`(8) `on_timeout`("fail") `jitter_px_min/max`(0/5) |
| `wait_template` | 等图出现/消失 | `category` `name` `timeout`(8) `exists`(true) `on_timeout`("fail") |
| `click` | 盲点固定坐标 | `tap_x` `tap_y`（540×960 基准空间）；或坐标来自变量：`coord`（求值成 (x,y) 元组/列表、或 `"x,y"` 字符串）或 `x_expr`/`y_expr` 分轴表达式（变量名直写或算术），两者存在即覆盖 tap_x/y；解析失败只 WARN 跳过不点 |
| `swipe` | 滑动 | `x1 y1 x2 y2 ms`(500) |
| `sleep` | 硬等 | `seconds`(1) |
| `compute` | 计算赋值：算表达式写流程变量 | `expr`（支持变量名直写、`+ - * / // % **`、`abs/min/max/round/int/float/len`、`{账号.列}`）`var_name`（写入的流程变量名）；表达式非法只 WARN 跳过不拦路 |
| `branch` | 判断分叉（true/false 两口） | `cond`{kind:"template"/"color"/"text"/"var"…,category,name} `timeout`(1)；多条件：`logic`("all"/"any")+`extra_conds`[]，每条可 `negate`。var 条件 `op` 可取 `== != > >= < <=` 或区间 `between`/`not_between`（配 `value`=下界、`value2`=上界，含端点、反填自动归一） |
| `subflow` | 调另一条流程 | `flow_name`；前置门同 click_template（`wait_before` 等）；可选 `params`[{"name"变量名,"expr"实参表达式}]——进入前逐项求值写流程变量（形参名即变量名，子流程内 `{名字}` 直接用），非法/空名只 WARN 跳过 |
| `loop` | 循环容器 | `steps`[]（行清单顺序执行成一轮）`cond`（每轮尾查，满足即出）`max_times`(0=不限) |
| `multi_scan` | 一帧扫全部命中逐个处置 | `mode`("tap"/"enter"/"read") `source`("template"/"text") `roi` `max_hits`(20) `gap_secs`(0.5)… |
| `server_pick` | 识字取物（OCR 找字点它） | `text` `roi`(null=全屏) `timeout`(15) `min_sim`(60) |
| `save_assets` | 把变量读数存资产账 | `items`[{"var","name"}] |
| `debug_print` | 打日志（调试用） | `text` `level`("INFO") |
| `stop_flow` | 结束流程（只有入口） | `stop_scope`("local"/"global"/"continue")。local=结束所在层；global=整条急停；continue=跳过本轮（仅循环体有意义，等价循环 continue，放循环外=收尾整条） |
| `watch` | 并行弹窗哨兵（不接线） | `rows`[{"category","name","flow_name"}] `cooldown_secs`(5) `fail_limit`(3) |
| `timer` | 定时哨兵（不接线） | `rows`[{"interval_min","flow_name"}] |

模板引用一律 `category`（= templates/ 下库目录名）+ `name`（模板名）。
**缺图不炸流程**：编译期降级成 WARN 日志跳过，所以先 frame_cut 入库、再写流程。

## 最小示例：等登录按钮 → 点它

```json
{"version": 2,
 "nodes": [
  {"id": "w1", "type": "wait_template", "x": 0, "y": 0, "label": "等登录",
   "category": "AI生成-登录", "name": "按钮_登录", "timeout": 15,
   "exists": true, "on_timeout": "fail"},
  {"id": "c1", "type": "click_template", "x": 260, "y": 0, "label": "点登录",
   "category": "AI生成-登录", "name": "按钮_登录", "wait_before": false,
   "wait_timeout": 8, "on_timeout": "fail", "jitter_px_min": 0, "jitter_px_max": 5}
 ],
 "edges": [{"from": "w1", "port": "out", "to": "c1", "to_port": "in"}]}
```

写完用 `python tools/flow_apply.py 流程.json --name 名` 校验落账
（编译不过当场拒），再 `python tools/flow_run.py 名` 试跑。

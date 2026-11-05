"""
ui/window_geometry.py — 窗口位置/大小的记忆（全项目几何存取的单一出口）

主窗口 / 工作台 / 状态窗 / 日志窗过去各抄一份同构的「读 dict→resize→move /
写 x,y,w,h」样板，四处一漂移。收这里两份函数，调用点各压成一行。

约定（行为与四份旧样板逐字对齐）：
- restore：配置里是无 "w" 键的 dict → 走默认尺寸；有 "w" 才 resize；有 "x" 才 move。
- save：写 {x, y, w, h} 四键。⚠ "只存当前可见窗" 这种调用侧闸门语义由各调用点
  自己保留（main_window.closeEvent 里 isVisible 判断），本模块不掺和可见性。
"""


def restore(win, cfg, key: str, default_wh):
    """按 key 还原窗口位置大小；无存档或存档残缺用 default_wh。"""
    geo = cfg.get(key)
    if isinstance(geo, dict) and "w" in geo:
        win.resize(geo.get("w", default_wh[0]), geo.get("h", default_wh[1]))
        if "x" in geo:
            win.move(geo["x"], geo["y"])
    else:
        win.resize(*default_wh)


def save(win, cfg, key: str):
    """把窗口当前位置大小写进 key。"""
    cfg.set(key, {"x": win.x(), "y": win.y(),
                  "w": win.width(), "h": win.height()})

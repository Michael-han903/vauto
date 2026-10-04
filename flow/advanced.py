# -*- coding: utf-8 -*-
"""高级设置面板：把 RunConfig 的**全部字段**摆出来让你改（默认值 = 当前能跑的默认值）。

设计：
* 自动枚举 dataclass 字段（以后 config 里加了新字段，这里会**自动出现**，不用改代码）；
* 主面板已经负责的字段（阶段/轮数/车数/循环/真按键/共享代码/时长/窗口名）在这里跳过；
* 值用 ast.literal_eval 解析：数字、字符串、元组 (583, 355) 都能填；填错标红不生效；
* 「保存到文件 / 从文件载入」→ vauto_config.json（启动时会自动载入它）。
"""
from __future__ import annotations

import ast
import json
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any, Callable, Dict

# 主面板负责的字段（不在这里重复出现）
EXCLUDE = {
    "title_key", "phase", "rounds", "cars", "cycles", "enter_event",
    "dry_run", "share_code", "round_minutes", "hotkey",
    # 【2026-10-04】replay 是引擎内部开关（回放/自检用）：误在实跑时打开会让程序
    # "只回放不动手"，看起来像"点了启动却没反应"。不给它出现在面板里。
    "replay",
}

# 中文标签（没有的字段直接显示英文名，不影响使用）
CN = {
    "hold_key": "挑战中按住", "retry_key": "结算重试键", "leave_event_key": "结算继续键",
    "round_timeout": "单轮超时(秒)", "round_settle_before": "载入稳定等待(秒)",
    "round_active_wait": "等比赛开始上限(秒)", "poll": "轮询间隔(秒)",
    "max_polls_per_round": "单轮轮询上限(0=自动)", "ack_timeout": "Esc 确认超时(秒)",
    "hint_wait": "精通页提示等待(秒)",
    "watchdog_idle": "卡死看门狗(秒,0=关)",
    "car_find_tries": "找 22B 扫描列上限", "car_scan_report": "扫描进度每 N 列报一次",
    "list_search_roi": "车辆列表区域(x,y,w,h)", "brand_next_click": "品牌翻页箭头坐标",
    "grid_origin": "车格首格左上角", "grid_pitch": "车格间距",
    "back_to_22b": "刷完挑战换回 22B(默认关)", "frame_expect": "期望分辨率(w,h)",
    "cars_cap": "B 阶段车数安全上限(防死循环保险丝)", "car_load_wait": "上车加载等待(秒)",
    "tab_creativity_click": "创意中心标签坐标", "dialog_cancel_key": "关弹窗按键",
    "code_clear_backspaces": "输代码前退格次数", "search_timeout": "等搜索结果超时(秒)",
    "entry_load_timeout": "进赛事加载超时(秒)",
    "tab_vehicle_click": "车辆标签坐标", "unlock_key": "解锁键(Y)", "confirm_key": "确认/上车键",
    "max_cars_per_session": "单次 B 会话最多车数", "nav_budget": "走格子步数预算",
    "nav_max_fail": "连续走不到上限", "nav_repeat_limit": "连续撞到旧车上限",
    "fav_key": "加收藏菜单下移键", "park_pointer": "鼠标用完归位左上角",
    "park_at": "归位坐标", "after_enter_car_wait": "上车后固定缓冲(秒)",
    "ensure_caps_lock": "开跑前自动开大写锁定", "grid_area": "车格区域(l,t,r,b)",
    "caps_lock_follow_focus": "切出游戏关大写/切回自动开",
    "event_menu_resume_key": "比赛菜单里回比赛的键(默认Esc)",
    "event_menu_auto_resume": "比赛里认到菜单就自动回比赛",
    "race_rehold_after": "比赛画面静止多少秒后补按一次 W(0=关)",
    "caps_off_on_exit": "退出程序时关掉大写锁定",
    "park_on_focus_return": "切回来先把鼠标归位",
    "focus_resync": "切回来重新判一次当前界面",
    "tile_w_min": "车格宽度下限", "tile_w_max": "车格宽度上限",
    "tile_h_min": "车格高度下限", "tile_h_max": "车格高度上限",
    "heart_off": "♥ 搜索区左上角", "heart_roi": "♥ 搜索区大小",
    "heart_roi2": "当前车 ♥ 左侧位区", "drive_badge_roi": "「驾驶中」图标搜索区",
    "grid_walk_max": "走到目标格按键上限", "grid_walk_dwell": "方向键后等待(秒)",
    "grid_scroll_dwell": "翻列表等待(秒,调小=更快)",
    "use_mouse_select": "用鼠标点车格选车(实测命中率7%,默认关)",
    "walk_confirm_heart": "到站后再抓一帧复核有没有♥",
    "tab_ensure_budget": "确保车辆页时间预算(秒)",
    "brand_jump_max": "每轮最多翻几个品牌",
    "auto_recover": "卡住时自动退回已知界面",
    "recover_rounds": "自愈最多按几轮 Esc",
    "grid_walk_keys": "方向键顺序", "fp_same_tol": "同车指纹阈值",
    "fp_align_px": "指纹对齐像素", "esc_dwell": "Esc 后等待(秒)",
    "page_timeout": "等页面出现超时(秒)", "car_change_timeout": "换车加载超时(秒)",
    "nav_watch_roi": "变化检测区域", "nav_change_blocks": "变化检测分块",
    "nav_change_threshold": "变化检测阈值",
    "ledger_path": "账本路径", "log_dir": "日志目录",
    "max_runtime_min": "总时长上限(分钟,0=不限)", "require_car_22b": "A 前校验当前车=22B",
    "use_manufacturer_panel": "找22B用制造商面板",
    "cycles": "主循环次数(0=一直循环)", "farm_first": "开局先刷后花",
    "unfav_cap": "取消收藏阶段台数上限",
    "keep_awake": "运行期间防系统睡眠",
    "keep_display_on": "运行期间屏幕也常亮(默认否)",
    "power_plan_guard": "顺便托管电源计划(退出还原)",
    "black_wake_nudge": "黑屏时轻推鼠标唤醒屏幕",
    "black_wait_max": "全黑最多等多少秒(0=无限等)",
}


def config_path_near_exe() -> Path:
    """配置文件放在 exe（或本仓库）旁边。"""
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "vauto_config.json"
    return Path(__file__).resolve().parent.parent / "vauto_config.json"


def _fmt(v: Any) -> str:
    if isinstance(v, tuple):
        return "(" + ", ".join(str(x) for x in v) + ")"
    return str(v)


def _parse(text: str, like: Any) -> Any:
    """按字段原来的类型解析输入。失败抛 ValueError。"""
    t = text.strip()
    if isinstance(like, bool):
        low = t.lower()
        if low in ("1", "true", "是", "yes", "on"):
            return True
        if low in ("0", "false", "否", "no", "off"):
            return False
        raise ValueError("布尔值请填 是/否（或 true/false）")
    if isinstance(like, tuple):
        v = ast.literal_eval(t if t.startswith("(") else f"({t})")
        if not isinstance(v, tuple):
            raise ValueError("需要元组，如 (583, 355)")
        return v
    if isinstance(like, str):
        v = ast.literal_eval(t) if (t[:1] in "\"'" and t[-1:] == t[:1]) else t
        return v
    if isinstance(like, int):
        return int(float(ast.literal_eval(t)))
    if isinstance(like, float):
        return float(ast.literal_eval(t))
    raise ValueError(f"不认识的值类型 {type(like).__name__}")


def open_advanced(master, base, overrides: Dict[str, Any],
                  on_apply: Callable[[Dict[str, Any], str], None]) -> None:
    """打开高级设置窗口。base = 当前 RunConfig（取默认值/当前值），overrides = 已改的值。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    win = tk.Toplevel(master)
    win.title("高级设置 —— 全部可调参数（默认值 = 当前能跑的值）")
    win.geometry("760x620+80+60")
    win.transient(master)

    flds = [f for f in dc_fields(base) if f.name not in EXCLUDE]

    top = tk.Frame(win); top.pack(fill="x", padx=10, pady=6)
    tk.Label(top, text="改完点「应用」即生效（下一次启动用）。填错的行会标红、不影响其它项。",
             anchor="w", fg="#666666").pack(side="left")

    mid = tk.Frame(win); mid.pack(fill="both", expand=True, padx=10)
    canvas = tk.Canvas(mid, highlightthickness=0)
    vsb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    rows: Dict[str, Any] = {}
    for i, f in enumerate(flds):
        cur = overrides.get(f.name, getattr(base, f.name))
        tk.Label(inner, text=CN.get(f.name, f.name), width=26, anchor="w").grid(
            row=i, column=0, sticky="w", pady=1)
        if isinstance(cur, bool):
            var = tk.BooleanVar(value=bool(cur))
            tk.Checkbutton(inner, variable=var).grid(row=i, column=1, sticky="w")
            rows[f.name] = ("bool", var)
        else:
            e = tk.Entry(inner, width=26)
            e.insert(0, _fmt(cur))
            e.grid(row=i, column=1, sticky="w", pady=1)
            rows[f.name] = ("entry", e)
        tk.Label(inner, text=f.name, anchor="w", fg="#aaaaaa").grid(
            row=i, column=2, sticky="w", padx=8)

    def _collect() -> Dict[str, Any]:
        out, errs = dict(overrides), []
        for f in flds:
            kind, w = rows[f.name]
            try:
                val = bool(w.get()) if kind == "bool" else _parse(w.get(), getattr(base, f.name))
                out[f.name] = val
                if kind == "entry":
                    w.configure(bg="white")
            except Exception:
                errs.append(CN.get(f.name, f.name))
                if kind == "entry":
                    w.configure(bg="#ffd7d7")
        if errs:
            raise ValueError("、".join(errs))
        return out

    def do_apply() -> None:
        try:
            vals = _collect()
        except ValueError as exc:
            messagebox.showerror("有填错的行", f"请检查标红的项：\n{exc}", parent=win)
            return
        on_apply(vals, "已应用（下次启动生效）")

    def do_default() -> None:
        d = base.__class__()
        for f in flds:
            kind, w = rows[f.name]
            v = getattr(d, f.name)
            if kind == "bool":
                w.set(bool(v))
            else:
                w.delete(0, "end"); w.insert(0, _fmt(v)); w.configure(bg="white")
        messagebox.showinfo("恢复默认", "已填回出厂默认值；记得点「应用」。", parent=win)

    def do_save() -> None:
        try:
            vals = _collect()
        except ValueError as exc:
            messagebox.showerror("有填错的行", str(exc), parent=win)
            return
        cfg = base.__class__()
        for k, v in vals.items():
            setattr(cfg, k, v)
        cfg.__post_init__()
        from flow.config import save_config
        p = config_path_near_exe()
        save_config(cfg, p)
        messagebox.showinfo("已保存", f"配置已写入：\n{p}\n（启动时会自动载入）", parent=win)

    def do_load() -> None:
        from flow.config import load_config
        p = filedialog.askopenfilename(parent=win, initialfile="vauto_config.json",
                                       filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if not p:
            return
        cfg = load_config(p)
        for f in flds:
            kind, w = rows[f.name]
            v = getattr(cfg, f.name)
            if kind == "bool":
                w.set(bool(v))
            else:
                w.delete(0, "end"); w.insert(0, _fmt(v)); w.configure(bg="white")
        messagebox.showinfo("已载入", f"已读取 {p}；点「应用」生效。", parent=win)

    bot = tk.Frame(win); bot.pack(fill="x", padx=10, pady=8)
    tk.Button(bot, text="应用", width=10, command=do_apply).pack(side="right", padx=3)
    tk.Button(bot, text="恢复默认", width=10, command=do_default).pack(side="right", padx=3)
    tk.Button(bot, text="从文件载入…", width=12, command=do_load).pack(side="right", padx=3)
    tk.Button(bot, text="保存到文件…", width=12, command=do_save).pack(side="right", padx=3)
    tk.Label(bot, text=f"共 {len(flds)} 项", fg="#999999").pack(side="left")

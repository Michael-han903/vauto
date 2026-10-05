# -*- coding: utf-8 -*-
"""图形控制台（Launcher）：设置 + 启动/停止 + 状态监测 + 日志尾巴。

设计：Tk 在主线程；跑流程的 Runner 在**工作线程**里；
两个线程只通过 queue.Queue（日志）和普通属性（状态快照）通信，避免跨线程碰 Tk。

日志的来路：把工作线程里的 sys.stdout 包一层 _Tee → 既照常打控制台，
也塞进 Queue → 主线程每 250ms 取出来填到窗口里（所以窗口里看到的
就是终端里那套中文日志，而不是原始 JSONL）。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from typing import Optional

_STATE_STYLE = {
    "idle":        ("待命", "#888888"),
    "run":         ("运行中", "#1f9d55"),
    "wait_screen": ("等待屏幕恢复", "#dd8800"),
    "stopped":     ("已停止", "#cc3333"),
    "done":        ("已结束", "#3366cc"),
}


class _Tee:
    """把 stdout 同时写到真终端和队列（给 GUI 显示）。"""

    def __init__(self, real, push):
        self._real = real
        self._push = push

    def write(self, s: str) -> int:
        try:
            if self._real is not None:
                self._real.write(s)
        except Exception:
            pass
        if s:
            self._push(s)
        return len(s) if s else 0

    def flush(self) -> None:
        try:
            if self._real is not None:
                self._real.flush()
        except Exception:
            pass

    def isatty(self) -> bool:            # 有些库会问
        return False


def pick_window_title(titles, cur: str = "") -> tuple:
    """纯逻辑：根据"当前记得的标题"和"现在可见的标题列表"决定下拉框该显示什么、给什么提示。

    返回 `(新标题, 提示语)`。规则来自 2026-10-06 用户实测的一次崩溃：
      * **只认 forza/地平线 是游戏**；
      * **绝不**"挑不到就选列表第一个窗口" —— 用户先开控制台后开游戏时，下拉框被塞进一个
        无关窗口（他遇到的是 `104310437/12960`），一按「开始」就崩在 build_stack
        "找不到标题包含 X 的窗口"；现在宁可为空 + 提示，也不乱挑；
      * 当前值还在列表里 → 保留；若它不是游戏而列表里有游戏 → 自动换成游戏（并说明）；
      * 当前值不在了 → 有游戏就换游戏，没游戏就留空 + 提示。
    """
    titles = [t for t in (titles or []) if (t or "").strip()]
    cur = (cur or "").strip()

    def _is_game(t: str) -> bool:
        t = (t or "").lower()
        return "forza" in t or "地平线" in t

    forza = next((t for t in titles if _is_game(t)), "")
    if cur and cur in titles:
        if forza and not _is_game(cur):
            return forza, f"[i] 自动把目标窗口从「{cur}」改成了「{forza}」（那才是游戏）\n"
        if not _is_game(cur) and not forza:
            # 没有游戏窗口、当前选的又不是游戏 → **不替你改**（你自己选的），但说一句
            return cur, f"[!] 当前选的目标窗口「{cur}」看着不是游戏 —— 确认没选错？\n"
        return cur, ""
    if forza:
        msg = f"[i] 原来的「{cur}」已经不在窗口列表里 → 自动切到「{forza}」\n" if cur else ""
        return forza, msg
    if cur:
        return "", (f"[!] 窗口列表里找不到「{cur}」，也没有 Forza 窗口 —— 游戏开着吗？"
                    "开起来后点「刷新」再选\n")
    return "", "[!] 没找到 Forza 窗口 —— 先把游戏开起来，再点「刷新」\n"


class Launcher:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        # 【2026-10-05 命名规范】对外产品名 = 基于挑战蓝图的地平线六刷技能点软件；
        # `vauto` 保留为代号/包名/仓库名/可执行文件名（技术标识，不动：牵涉 import、CI、
        # 已有快捷方式与说明）。版本号取自 vauto.__version__，避免"标题里写死一个版本"。
        from vauto import __version__ as _ver
        self.root.title(f"基于挑战蓝图的地平线六刷技能点软件  v{_ver} —— 控制台")
        self.root.geometry("780x640+40+40")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._t0 = time.monotonic()      # 【修·真根】轮询要用它算"本次/累计时长" ✗：
                                         # 之前 Launcher 没定义 _t0 + import time 被误删 →
                                         # 轮询每次都在这里抛 NameError 被 except 吞掉 →
                                         # 按钮永不刷新（停下后没法再点开始 ✗✗）。
        self.q: "queue.Queue[str]" = queue.Queue()
        self.running = False
        self.runner = None
        self.stack = None
        self.worker: Optional[threading.Thread] = None
        self.done_flag = False
        self._live_started = False        # 这次会话是否真的按过键（关窗口时据此收掉大写锁定）
        self.base_cfg = None              # 载入的配置（没有就是 RunConfig 默认）
        self.overrides: dict = {}         # 「高级设置」里的覆盖项

        self._build_ui()
        self._refresh_windows()
        self._load_saved()
        self.root.after(250, self._pump)
        self.root.after(300, self._tick_status)

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        pad = dict(padx=10, pady=4)
        frm = tk.Frame(self.root)
        frm.pack(fill="x", **pad)

        # 目标窗口
        row = tk.Frame(frm); row.pack(fill="x", pady=3)
        tk.Label(row, text="目标窗口", width=10, anchor="w").pack(side="left")
        self.var_title = tk.StringVar()
        self.cmb_title = ttk.Combobox(row, textvariable=self.var_title, width=52)
        self.cmb_title.pack(side="left", fill="x", expand=True)
        tk.Button(row, text="刷新", width=6, command=self._refresh_windows).pack(side="left", padx=6)

        # 阶段
        row = tk.Frame(frm); row.pack(fill="x", pady=3)
        tk.Label(row, text="阶段", width=10, anchor="w").pack(side="left")
        self.var_phase = tk.StringVar(value="both")
        for val, txt in (("both", "全自动（花↔刷 循环往复）"),
                         ("spend", "只刷 B（车库加点）"),
                         ("unfav", "只取消收藏（逐辆检查、不上车）"),
                         ("farm", "只跑 A（挑战刷点）")):
            tk.Radiobutton(row, text=txt, variable=self.var_phase, value=val).pack(side="left", padx=4)

        # 数字项
        row = tk.Frame(frm); row.pack(fill="x", pady=3)
        tk.Label(row, text="挑战轮数", width=10, anchor="w").pack(side="left")
        self.var_rounds = tk.StringVar(value="4")
        tk.Spinbox(row, from_=1, to=99, width=5, textvariable=self.var_rounds).pack(side="left")
        tk.Label(row, text="   最多处理车数（0 = 一直解到点数不足）", anchor="w").pack(side="left")
        self.var_cars = tk.StringVar(value="0")
        tk.Spinbox(row, from_=0, to=9999, width=6, textvariable=self.var_cars).pack(side="left")
        tk.Label(row, text="   循环次数(0=一直循环)", anchor="w").pack(side="left")
        self.var_cycles = tk.StringVar(value="0")
        tk.Spinbox(row, from_=0, to=99, width=4, textvariable=self.var_cycles).pack(side="left")

        # 挑战设置（共享代码 / 标称时长）
        row = tk.Frame(frm); row.pack(fill="x", pady=3)
        tk.Label(row, text="挑战设置", width=10, anchor="w").pack(side="left")
        tk.Label(row, text="共享代码").pack(side="left")
        self.var_code = tk.StringVar(value="161047605")
        tk.Entry(row, textvariable=self.var_code, width=14).pack(side="left", padx=(2, 14))
        tk.Label(row, text="挑战时长(分钟)").pack(side="left")
        self.var_minutes = tk.StringVar(value="8")
        tk.Spinbox(row, from_=1, to=120, width=5, textvariable=self.var_minutes).pack(side="left")
        tk.Label(row, text="（时长只影响超时与估时；比赛实际时长由蓝图决定）",
                 fg="#999999").pack(side="left", padx=8)

        # 开关
        row = tk.Frame(frm); row.pack(fill="x", pady=3)
        self.var_event = tk.BooleanVar(value=True)
        self.var_awake = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="自动进赛事（主菜单→创意中心→EventLab→搜索共享代码）",
                       variable=self.var_event).pack(side="left", padx=2)
        tk.Checkbutton(row, text="防息屏/防休眠", variable=self.var_awake).pack(side="left", padx=8)

        row = tk.Frame(frm); row.pack(fill="x", pady=6)
        self.var_live = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="★ 真的按键（不勾 = 演练模式，只判不按）",
                       variable=self.var_live, fg="#cc0000",
                       font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=2)
        self.btn_start = tk.Button(row, text="启动", width=10, bg="#1f9d55", fg="white",
                                   font=("Microsoft YaHei UI", 11, "bold"), command=self._start)
        self.btn_start.pack(side="right", padx=4)
        self.btn_stop = tk.Button(row, text="停止", width=10, state="disabled", command=self._stop)
        self.btn_stop.pack(side="right", padx=4)
        tk.Button(row, text="高级设置…", width=12,
                  command=self._open_advanced).pack(side="right", padx=4)
        tk.Button(row, text="打开日志文件夹", width=12,
                  command=self._open_logs).pack(side="right", padx=4)

        # 状态
        box = tk.LabelFrame(self.root, text="状态")
        box.pack(fill="x", **pad)
        self.lbl_state = tk.Label(box, text="● 待命", font=("Microsoft YaHei UI", 16, "bold"),
                                  anchor="w", fg="#888888")
        self.lbl_state.pack(fill="x", padx=8, pady=(4, 0))
        self.lbl_body = tk.Label(box, text="", anchor="w", justify="left",
                                 font=("Microsoft YaHei UI", 10))
        self.lbl_body.pack(fill="x", padx=8)
        self.lbl_last = tk.Label(box, text="", anchor="w", fg="#666666",
                                 font=("Microsoft YaHei UI", 10))
        self.lbl_last.pack(fill="x", padx=8, pady=(0, 6))

        # 日志
        box2 = tk.LabelFrame(self.root, text="运行日志（和终端同款）")
        box2.pack(fill="both", expand=True, **pad)
        from tkinter import scrolledtext
        self.txt = scrolledtext.ScrolledText(box2, height=14, wrap="none",
                                             font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt.configure(state="disabled")

        tk.Label(self.root, text="F1 = 急停（任何时候）· 目标窗口不在前台时动作会自动暂停",
                 anchor="w", fg="#999999").pack(fill="x", padx=10, pady=(0, 6))

    # ---------------- 交互 ----------------
    def _load_saved(self) -> None:
        """启动时自动载入 exe 旁边的 vauto_config.json（有就用；没有 = 出厂默认）。"""
        from flow.config import RunConfig
        try:
            from flow.advanced import config_path_near_exe
            from flow.config import load_config
            p = config_path_near_exe()
            self.base_cfg = load_config(p) if p.is_file() else RunConfig()
            cfg = self.base_cfg
            if p.is_file():
                self._say(f"[i] 已载入配置 {p}\n")
            # 基本项回填主界面（真按键永不自动勾——安全第一）
            self.var_phase.set(cfg.phase)
            self.var_rounds.set(str(cfg.rounds)); self.var_cycles.set(str(cfg.cycles))
            self.var_cars.set(str(cfg.cars)); self.var_event.set(bool(cfg.enter_event))
            self.var_code.set(cfg.share_code); self.var_minutes.set(f"{cfg.round_minutes:g}")
            # 其余字段进 overrides
            from dataclasses import fields as _df
            basic = {"title_key", "phase", "rounds", "cars", "cycles", "enter_event",
                     "dry_run", "share_code", "round_minutes", "hotkey", "replay"}
            self.overrides = {f.name: getattr(cfg, f.name) for f in _df(cfg)
                              if f.name not in basic}
        except Exception as exc:
            self._say(f"[i] 配置载入跳过: {exc}\n")
            self.base_cfg = RunConfig(); self.overrides = {}

    def _sync_buttons(self) -> None:
        """按 running 状态刷新两个按钮（单独的 try，坏不了别人）。"""
        try:
            self.btn_start.config(state=("disabled" if self.running else "normal"))
            self.btn_stop.config(state=("normal" if self.running else "disabled"))
        except Exception:
            pass

    def _open_advanced(self) -> None:
        from flow.advanced import open_advanced
        from flow.config import RunConfig
        base = self.base_cfg or RunConfig()

        def _on_apply(vals, msg):
            self.overrides = vals
            self._say(f"[i] 高级设置{msg}（共 {len(vals)} 项）\n")

        open_advanced(self.root, base, self.overrides or {}, _on_apply)

    def _open_logs(self) -> None:
        d = os.path.join(os.getcwd(), "logs")
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)                 # type: ignore[attr-defined]
        except Exception:
            pass

    def _refresh_windows(self) -> None:
        titles = []
        try:
            from vauto import enable_dpi_awareness, list_windows
            enable_dpi_awareness()
            for _hwnd, t, _cls in list_windows():
                t = (t or "").strip()
                if t and t not in titles:
                    titles.append(t)
        except Exception as exc:
            titles = [f"(列举窗口失败: {exc})"]
        self.cmb_title["values"] = titles
        # 【2026-10-06 修·用户实测崩溃根因】"挑不到 Forza 就选列表第一个窗口"的兜底害死人：
        # 用户先开控制台、后开游戏时，下拉框被塞进一个**无关窗口**（他遇到的是
        # `104310437/12960`），一按「开始」就崩在 build_stack"找不到标题包含 X 的窗口"。
        # 现在规则抽成纯函数 pick_window_title()（自检里也钉住了），**绝不乱挑**。
        pick, msg = pick_window_title(titles, self.var_title.get())
        if msg:
            self._say(msg)
        self.var_title.set(pick)

    def _title_exists(self, title: str) -> bool:
        """窗口列表里有没有标题包含 `title` 的窗口（用来在"发车"前拦住崩掉的调用）。"""
        try:
            from vauto import list_windows
            t = (title or "").lower()
            return any(t in (w or "").lower() for _h, w, _c in list_windows())
        except Exception:
            return True          # 列举失败就别挡着用户

    def _start(self) -> None:
        # 【2026-10-03 修】原来 `if self.running: return` 是静默无响应 ✗：
        # 线程已结束但标志没复位时，按钮看着能点、点了没反应 → 只能退出重进 ✗。
        # 现在按"工作线程是否真的还活着"判断；死了就强制复位，让你能直接再启动 ✓。
        if self.running and self.worker is not None and self.worker.is_alive():
            self._say("[i] 上一轮还在收尾，稍等一下再点；若一直如此按 F1 或关掉窗口重开" + chr(10))
            return
        self.running = False
        title = self.var_title.get().strip()
        # 【2026-10-06 修】发车前**先刷新一次窗口列表**（控制台常比游戏先开）→ 自动切到游戏；
        # 再**校验窗口真的存在** → 不存在就给出人话提示、不发车（以前是直接崩在 build_stack）。
        try:
            self._refresh_windows()
            title = self.var_title.get().strip() or title
        except Exception:
            pass
        if not title:
            self._say("[!] 先选目标窗口（游戏开起来 → 点「刷新」→ 下拉选 Forza Horizon 6）\n")
            return
        if not self._title_exists(title):
            self._say(f"[!] 窗口列表里找不到标题包含「{title}」的窗口 —— 游戏开着吗？"
                      "开起来后点「刷新」重选（不再直接崩）\n")
            return
        try:
            int(self.var_rounds.get()); int(self.var_cars.get()); int(self.var_cycles.get())
            if not self.var_code.get().strip():
                raise ValueError("共享代码不能为空")
            float(self.var_minutes.get())
        except Exception:
            self._say("[!] 轮数/车数/循环 必须是数字；共享代码不能为空、时长填分钟数\n")
            return
        if self.var_live.get():
            from tkinter import messagebox
            ok = messagebox.askyesno(
                "确认真的按键？",
                "程序会真的操作你的鼠标键盘。\n\n"
                "· 运行中随时 F1 急停，或点窗口里的「停止」\n"
                "· 目标窗口不在前台时全部动作自动暂停\n"
                "· 第一次建议先不勾「真的按键」演练一遍\n\n"
                "确定开始？")
            if not ok:
                return
        self.running = True
        self.done_flag = False
        self.runner = None
        self.stack = None
        # 【2026-10-04】记住"这次真的按过键"，以便关窗口时把大写锁定收掉（dry-run 不动键盘）
        try:
            self._live_started = bool(self.var_live.get())
        except Exception:
            self._live_started = False
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._say("=" * 70 + "\n")
        self.worker = threading.Thread(target=self._worker, daemon=True, name="vauto-flow")
        self.worker.start()

    def _stop(self) -> None:
        st = self.stack
        ev = getattr(getattr(st, "stop", None), "event", None) if st is not None else None
        if ev is not None:
            try:
                ev.set()
                self._say("\n[停止] 已点「停止」→ 正在中止（等同按 F1）…\n")
                return
            except Exception:
                pass
        self._say("\n[停止] 请直接按 F1 急停（这个阶段还没拿到停止句柄）\n")

    def _on_close(self) -> None:
        if self.running:
            from tkinter import messagebox
            if not messagebox.askyesno("还在运行", "任务还在跑，确定要关窗口吗？\n（等同于点「停止」）"):
                return
            self._stop()
        # 【2026-10-04 用户要求："关闭程序的时候也关掉 caps"】
        # 这里紧接着就是 os._exit —— 工作线程是 daemon，Runner 的 finally 跑不到，
        # 所以必须在这条路径上自己把大写锁定收掉（只在真的按过键的那次会话里做）。
        try:
            if getattr(self, "_live_started", False):
                from vauto.keystate import force_caps_off
                _left = force_caps_off()
                if not _left:
                    self._say("\n[收尾] 大写锁定已关（用户要求）\n")
                else:
                    self._say("\n[收尾] 想让大写锁定关掉，但系统没接受（请手动按一下 CapsLock）\n")
        except Exception:
            pass
        # 先把两个流刷干净再 _exit（否则管道/重定向下最后一段输出会丢）
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)                          # 规避 tkinter 收尾杂音（同 run_vauto）

    # ---------------- 工作线程 ----------------
    def _worker(self) -> None:
        real_stdout = sys.stdout
        sys.stdout = _Tee(real_stdout, self._push_text)
        ka = None
        try:
            from vauto import enable_dpi_awareness
            enable_dpi_awareness()
            from flow.config import RunConfig
            from flow.runner import Runner, build_stack
            from flow.advanced import config_path_near_exe
            from flow.config import load_config as _lc
            _p = config_path_near_exe()
            cfg = _lc(_p) if _p.is_file() else RunConfig()
            cfg.title_key = self.var_title.get().strip()
            cfg.phase = self.var_phase.get()
            cfg.rounds = int(self.var_rounds.get())
            cfg.cars = int(self.var_cars.get())
            cfg.cycles = int(self.var_cycles.get())
            cfg.enter_event = bool(self.var_event.get())
            cfg.share_code = self.var_code.get().strip()
            cfg.round_minutes = float(self.var_minutes.get())
            cfg.__post_init__()                 # 让时长立即推导单轮超时
            for _k, _v in (self.overrides or {}).items():
                try:
                    setattr(cfg, _k, _v)
                except Exception:
                    pass
            cfg.__post_init__()                 # 高级项生效（含超时推导）
            # 挂机防睡（2026-10-04 用户口径：屏幕允许变黑，程序必须继续跑）
            if self.var_awake.get() and bool(getattr(cfg, "keep_awake", True)):
                from flow.keepawake import keep_awake
                from pathlib import Path as _P
                _disp = bool(getattr(cfg, "keep_display_on", False))
                ka = keep_awake(True, strict=True, keep_display_on=_disp,
                                plan_guard=bool(getattr(cfg, "power_plan_guard", True)),
                                backup_path=_P(cfg.ledger_path).parent / "power_plan_backup.json")
                print("  [准备] 挂机模式：" + "，".join(
                    ["防系统睡眠" + ("✓" if ka.active else "✗"),
                     ("屏幕常亮=开" if _disp else "屏幕可黑=是（15 分钟规则保留）"),
                     "电源计划托管=" + str(getattr(ka, "plan_status", "?"))]))
            cfg.dry_run = not bool(self.var_live.get())
            print(f"[+] 配置：阶段={cfg.phase} 轮数={cfg.rounds} 车数={cfg.cars} "
                  f"循环={cfg.cycles} 进赛事={cfg.enter_event} 代码={cfg.share_code} 时长={cfg.round_minutes:g}分 "
                  f"模式={'真按键' if not cfg.dry_run else '演练'}")
            stack = build_stack(cfg, title_key=cfg.title_key)
            self.stack = stack
            print(f"[+] 目标窗口 hwnd={stack.hwnd}")
            with stack.stop:
                runner = Runner(stack, cfg)
                self.runner = runner
                runner.run()
            stack.capture.close()
            print("\n[+] 流程结束")
        except Exception as exc:
            print("\n[崩溃] 未预期异常：")
            traceback.print_exc(file=sys.stdout)
            _ = exc
        finally:
            if ka is not None:
                ka.stop()
            self.running = False
            self.done_flag = True
            sys.stdout = real_stdout
            # 【2026-10-03 修·用户实测"停下后没法再次点开始"】收尾时**直接**把按钮恢复：
            # 原来只靠 300ms 轮询刷按钮，一旦轮询里某次更新抛异常（被 except 吞掉 ✗）
            # 或轮询线程死掉，按钮就永久停在"禁用"状态 ✗ → 只能退出重进 ✗。
            try:
                self.root.after(0, self._sync_buttons)
            except Exception:
                pass

    # ---------------- 主线程轮询 ----------------
    def _push_text(self, s: str) -> None:
        self.q.put(s)

    def _say(self, s: str) -> None:
        self.q.put(s)

    def _pump(self) -> None:
        buf = []
        try:
            while True:
                buf.append(self.q.get_nowait())
        except queue.Empty:
            pass
        if buf:
            self.txt.configure(state="normal")
            self.txt.insert("end", "".join(buf))
            # 只保留最近 ~600 行，免得越跑越卡
            try:
                n = int(self.txt.index("end-1c").split(".")[0])
                if n > 600:
                    self.txt.delete("1.0", f"{n - 600}.0")
            except Exception:
                pass
            self.txt.see("end")
            self.txt.configure(state="disabled")
        try:
            self.root.after(250, self._pump)
        except Exception:
            pass

    def _tick_status(self) -> None:
        try:
            self._tick_body()
        except Exception:
            pass
        finally:
            try:
                self.root.after(300, self._tick_status)
            except Exception:
                pass

    def _tick_body(self) -> None:
        st = {"state": "idle"}
        if self.runner is not None:
            st = dict(getattr(self.runner, "status", {}) or {})
            st["cars_done"] = self.runner.stats.get("cars_done", 0)
            st["rounds_done"] = self.runner.stats.get("rounds", 0)
        if self.done_flag:
            st["state"] = "done"
        label, color = _STATE_STYLE.get(str(st.get("state")), ("运行中", "#1f9d55"))
        try:
            self.lbl_state.config(text=f"● {label}", fg=color)
            def _d(sec: float) -> str:
                sec = max(0, int(sec))
                return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

            _el = time.monotonic() - self._t0
            _tot = float(st.get("total_before_sec", 0.0)) + _el
            self.lbl_body.config(text=(
                f"阶段   {st.get('phase', '-')}\n"
                f"轮次   第 {st.get('round', '-')} 轮    循环 {st.get('cycle', '-')}\n"
                f"成绩   已解锁 {st.get('cars_done', 0)} 台 · 已跑 {st.get('rounds_done', 0)} 轮\n"
                f"时长   本次 {_d(_el)} · 累计 {_d(_tot)}"
            ))
            self.lbl_last.config(text=f"最近动作: {st.get('last', '-')}")
            self._sync_buttons()
        except Exception:
            pass

    # ---------------- 入口 ----------------
    def run(self) -> None:
        self.root.mainloop()

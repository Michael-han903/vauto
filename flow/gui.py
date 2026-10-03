# -*- coding: utf-8 -*-
"""实跑状态小窗（tkinter，标准库，无新依赖）。

用户要求（2026-10-03）："加一点额外的修饰功能了比如一个gui，写一下轮次信息，运行时间状态什么的"。

显示：状态灯 / 阶段 / 轮次+循环 / 已解锁·已跑轮数 / 运行时长 / 最近动作。

设计要点：
* 独立线程跑 Tk（Tk 只能在"拥有它"的线程里更新 → 用 after() 定时刷）；
* 每 300ms 从 runner.status 读一个**快照**渲染（显示用途，允许轻微竞态）；
* **置顶但不抢游戏焦点**：不调用 focus_force / grab_set；窗口创建后由主流程把游戏切回前台；
* 关掉窗口只是关显示，**不影响运行**（停止仍然只能靠 F1 / Ctrl+C）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict

_STATE_STYLE: Dict[str, tuple] = {
    "init":        ("初始化…", "#999999"),
    "run":         ("运行中", "#1f9d55"),
    "wait_screen": ("等待屏幕恢复", "#dd8800"),
    "stopped":     ("已停止", "#cc3333"),
}


class StatusWindow:
    """用法：
        gui = StatusWindow(lambda: runner.status, t0=runner._t0)
        if gui.start():  ...
        gui.close()
    """

    def __init__(self, get_status: Callable[[], Dict[str, Any]], t0: float):
        self._get = get_status
        self._t0 = t0
        self._root = None
        self._ready = threading.Event()

    # ---- 外部接口 ----
    def start(self) -> bool:
        try:
            import tkinter  # noqa: F401  （探测是否可用；真正的 Tk 在子线程里建）
        except Exception:
            return False
        self._thread = threading.Thread(target=self._main, daemon=True,
                                        name="vauto-status-gui")
        self._thread.start()
        self._ready.wait(3.0)
        return self._root is not None

    def close(self) -> None:
        """在 Tk 自己的线程里销毁窗口，并等线程收尾。
        直接在别的线程 destroy 会导致进程退出时 Tcl 报
        'Tcl_AsyncDelete: async handler deleted by the wrong thread'（退出码也会被弄脏）。"""
        r = self._root
        if r is not None:
            try:
                r.after(0, self._really_close)
            except Exception:
                pass
        th = getattr(self, "_thread", None)
        if th is not None and th.is_alive():
            th.join(2.5)

    def _really_close(self) -> None:
        r = self._root
        self._root = None
        try:
            r.quit()
        except Exception:
            pass
        try:
            r.destroy()
        except Exception:
            pass

    # ---- Tk 线程 ----
    def _main(self) -> None:
        try:
            import tkinter as tk
            r = tk.Tk()
        except Exception:
            self._ready.set()
            return
        self._root = r
        try:
            r.title("vauto 运行状态")
            r.geometry("460x250+18+18")
            r.attributes("-topmost", True)
            try:
                r.attributes("-alpha", 0.94)
            except Exception:
                pass
            font_cn = ("Microsoft YaHei UI", 11)
            self._state = tk.Label(r, text="● 初始化…",
                                   font=("Microsoft YaHei UI", 17, "bold"), anchor="w")
            self._state.pack(fill="x", padx=16, pady=(14, 4))
            self._body = tk.Label(r, text="", font=font_cn, anchor="w", justify="left")
            self._body.pack(fill="x", padx=16)
            self._last = tk.Label(r, text="", font=("Microsoft YaHei UI", 10),
                                  anchor="w", justify="left", fg="#666666")
            self._last.pack(fill="x", padx=16, pady=(10, 2))
            tk.Label(r, text="F1 急停 · 关闭本窗口不影响运行",
                     font=("Microsoft YaHei UI", 9), anchor="w",
                     fg="#999999").pack(fill="x", padx=16, pady=(6, 12))
        except Exception:
            pass
        self._ready.set()
        try:
            r.after(200, self._tick)
            r.mainloop()
        except Exception:
            pass
        finally:
            # mainloop 已退出：把引用清干净，避免解释器收尾时在"错误的线程"里碰 Tcl
            self._root = None
            import gc
            gc.collect()

    @staticmethod
    def _fmt_dur(sec: float) -> str:
        sec = max(0, int(sec))
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    # ---- 刷新 ----
    def _tick(self) -> None:
        r = self._root
        try:
            st: Dict[str, Any] = dict(self._get() or {})
            label, color = _STATE_STYLE.get(str(st.get("state")), ("运行中", "#1f9d55"))
            if st.get("done"):
                label, color = "已结束", "#3366cc"
            self._state.config(text=f"● {label}", fg=color)
            rows = [
                f"阶段   {st.get('phase', '-')}",
                f"轮次   第 {st.get('round', '-')} 轮   循环 {st.get('cycle', '-')}",
                f"成绩   已解锁 {st.get('cars_done', 0)} 台 · 已跑 {st.get('rounds_done', 0)} 轮",
                f"时长   {self._fmt_dur(time.monotonic() - self._t0)}",
            ]
            self._body.config(text="\n".join(rows))
            self._last.config(text=f"最近动作: {st.get('last', '-')}")
        except Exception:
            pass
        try:
            r.after(300, self._tick)
        except Exception:
            pass

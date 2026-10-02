# -*- coding: utf-8 -*-
"""
record_gui.py —— 图形化素材录制器（第 1 步 GUI 版）

功能
----
1. 下拉选择目标窗口（自动优先选中 Forza Horizon 6）
2. 实时预览目标窗口画面，确认游戏切到了正确界面
3. 勾选场景（challenge_result / popup_no_resource ...）点「录制本场景」
4. 录制中显示进度，可随时停止；完成后状态列打勾

用法
----
    py -3.14 record_gui.py
    py -3.14 record_gui.py --smoke      # 自检：启动 2 秒后自动关闭（用于验证无报错）

录制只做截图，不做任何输入模拟。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np

from vauto import WindowCapture, enable_dpi_awareness, list_windows, record_window
from record_scenes import SCENES

PREVIEW_MAX_W = 760          # 预览图最大宽度（像素）
DEFAULT_INTERVAL = (0.4, 0.9)


class RecorderGUI:
    def __init__(self, root: tk.Tk, auto_title: str = "Forza Horizon 6") -> None:
        self.root = root
        self.root.title("vauto 素材录制器")
        self.root.geometry("900x720")
        self.root.minsize(760, 600)

        self._hwnd: int | None = None
        self._preview_on = False
        self._preview_photo = None      # 必须持有引用，否则图片被回收
        self._preview_cap: WindowCapture | None = None
        self._stop_event: threading.Event | None = None
        self._recording = False
        self._done_count = 0

        self._build()
        self._select_window_by_title(auto_title)

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")

        ttk.Label(top, text="目标窗口:").pack(side="left")
        self.win_var = tk.StringVar()
        self.win_combo = ttk.Combobox(top, textvariable=self.win_var, width=52, state="readonly")
        self.win_combo.pack(side="left", padx=6)
        self.win_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_selection())
        ttk.Button(top, text="刷新窗口", command=self._refresh_windows).pack(side="left", padx=4)
        self.preview_btn = ttk.Button(top, text="▶ 预览画面", command=self._toggle_preview)
        self.preview_btn.pack(side="left", padx=4)

        mid = ttk.Frame(self.root, padding=(8, 4))
        mid.pack(fill="both", expand=True)

        cols = ("name", "desc", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=9)
        self.tree.heading("name", text="场景")
        self.tree.heading("desc", text="需要游戏处于什么画面")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=170, anchor="w")
        self.tree.column("desc", width=470, anchor="w")
        self.tree.column("status", width=120, anchor="center")
        for i, (name, desc, _frames) in enumerate(SCENES):
            self.tree.insert("", "end", iid=f"s{i}", values=(name, desc, "未录"))
        self.tree.pack(fill="both", expand=True, side="left")

        side = ttk.Frame(mid, padding=(10, 0, 0, 0))
        side.pack(fill="y", side="left")
        ttk.Label(side, text="录制帧数:").pack(anchor="w")
        self.frames_var = tk.StringVar(value="40")
        ttk.Spinbox(side, from_=5, to=200, textvariable=self.frames_var, width=8).pack(anchor="w", pady=2)
        ttk.Label(side, text="两帧间隔(秒):").pack(anchor="w", pady=(8, 0))
        self.interval_var = tk.StringVar(value="0.4,0.9")
        ttk.Entry(side, textvariable=self.interval_var, width=10).pack(anchor="w", pady=2)
        self.record_btn = ttk.Button(side, text="● 录制本场景", command=self._start_record, width=14)
        self.record_btn.pack(anchor="w", pady=(12, 4))
        self.stop_btn = ttk.Button(side, text="■ 停止录制", command=self._stop_record, width=14, state="disabled")
        self.stop_btn.pack(anchor="w", pady=2)

        self.status_var = tk.StringVar(value="就绪：选择窗口 -> 点「预览画面」确认游戏界面 -> 选场景录制")
        ttk.Label(self.root, textvariable=self.status_var, padding=(8, 2)).pack(fill="x")

        bottom = ttk.Frame(self.root, padding=(8, 2, 8, 8))
        bottom.pack(fill="both", expand=True)
        self.preview_label = ttk.Label(bottom, text="（点「预览画面」显示目标窗口实时画面）",
                                       anchor="center", relief="groove")
        self.preview_label.pack(fill="both", expand=True)

        self._refresh_windows()

    # ------------------------------------------------------------------ #
    # 窗口选择
    # ------------------------------------------------------------------ #
    def _refresh_windows(self) -> None:
        wins = [(h, t, c) for (h, t, c) in list_windows() if t.strip()]
        self._windows = wins
        self.win_combo["values"] = [f"{t[:44]}  [{h}]" for (h, t, c) in wins]
        if wins:
            self._select_window_by_title("Forza Horizon 6")

    def _select_window_by_title(self, title: str) -> None:
        if not title:
            return
        for i, (h, t, c) in enumerate(getattr(self, "_windows", [])):
            if title.lower() in t.lower():
                self.win_combo.current(i)
                self._hwnd = h
                self.status_var.set(f"已选中窗口: {t}  (hwnd={h})")
                return

    def _apply_selection(self) -> None:
        idx = self.win_combo.current()
        if 0 <= idx < len(getattr(self, "_windows", [])):
            self._hwnd = self._windows[idx][0]

    # ------------------------------------------------------------------ #
    # 预览
    # ------------------------------------------------------------------ #
    def _toggle_preview(self) -> None:
        if self._preview_on:
            self._preview_on = False
            self.preview_btn.config(text="▶ 预览画面")
            if self._preview_cap is not None:
                self._preview_cap.close()
                self._preview_cap = None
            return
        if self._hwnd is None:
            messagebox.showwarning("提示", "请先选择目标窗口")
            return
        self._preview_cap = WindowCapture(self._hwnd, client_only=True)
        self._preview_on = True
        self.preview_btn.config(text="■ 停止预览")
        self._preview_tick()

    def _preview_tick(self) -> None:
        if not self._preview_on:
            return
        try:
            if self._preview_cap is None:
                return
            frame = self._preview_cap.grab()
            photo = self._frame_to_photo(frame)
            if photo is not None:
                self._preview_photo = photo
                self.preview_label.config(image=photo)
        except Exception as exc:
            self.preview_label.config(text=f"（预览失败: {exc}）")
        self.root.after(300, self._preview_tick)

    def _frame_to_photo(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        scale = min(1.0, PREVIEW_MAX_W / w)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return None
        return tk.PhotoImage(data=buf.tobytes())

    # ------------------------------------------------------------------ #
    # 录制
    # ------------------------------------------------------------------ #
    def _selected_scene(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表里选中一个场景")
            return None
        idx = int(sel[0].lstrip("s"))
        return SCENES[idx][0]

    def _start_record(self) -> None:
        if self._recording:
            return
        name = self._selected_scene()
        if name is None or self._hwnd is None:
            return
        try:
            frames = max(1, int(self.frames_var.get()))
        except ValueError:
            frames = 40
        try:
            lo, hi = (float(v) for v in self.interval_var.get().split(",")[:2])
        except ValueError:
            lo, hi = DEFAULT_INTERVAL

        self._preview_on = False
        self.preview_btn.config(text="▶ 预览画面", state="disabled")
        self.record_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._recording = True
        self._stop_event = threading.Event()
        self.status_var.set(f"录制中: {name}  0/{frames} 帧（点「停止录制」或关闭窗口可中止）")
        threading.Thread(target=self._record_worker,
                         args=(self._hwnd, name, frames, (lo, hi)), daemon=True).start()

    def _record_worker(self, hwnd, name, frames, interval) -> None:
        def _on_frame(i: int, _p: Path) -> None:
            self.root.after(0, lambda: self.status_var.set(
                f"录制中: {name}  {i + 1}/{frames} 帧"))

        try:
            res = record_window(hwnd, name, n_frames=frames, interval=interval,
                                stop_event=self._stop_event, on_frame=_on_frame)
        except Exception as exc:
            self.root.after(0, lambda: self._on_done(name, 0, f"失败: {exc}"))
            return
        self.root.after(0, lambda: self._on_done(name, res.count, None))

    def _on_done(self, name: str, count: int, err: str | None) -> None:
        self._recording = False
        self.record_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.preview_btn.config(state="normal")
        if err:
            self.status_var.set(err)
            return
        status = f"已录 {count} 帧 ✔"
        for item in self.tree.get_children():
            if SCENES[int(item.lstrip("s"))][0] == name:
                self.tree.set(item, "status", status)
                break
        self._done_count += 1
        self.status_var.set(f"完成: {name}  {count} 帧 -> golden_frames/{name}/  "
                            f"（{self._done_count}/{len(SCENES)} 个场景）")

    def _stop_record(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self.status_var.set("已请求停止，正在保存已录帧...")

    def on_close(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._preview_cap is not None:
            try:
                self._preview_cap.close()
            except Exception:
                pass
        self.root.destroy()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="图形化素材录制器")
    p.add_argument("--title", type=str, default="Forza Horizon 6", help="自动选中的窗口标题关键字")
    p.add_argument("--smoke", action="store_true", help="自检模式：2 秒后自动关闭")
    args = p.parse_args(argv)

    enable_dpi_awareness()
    root = tk.Tk()
    app = RecorderGUI(root, auto_title=args.title)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    if args.smoke:
        root.after(2000, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

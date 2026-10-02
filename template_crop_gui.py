# -*- coding: utf-8 -*-
"""
template_crop_gui.py —— 模板裁剪器（第 2 步 2a）

做什么
------
从 golden_frames/<场景>/ 的帧里，用鼠标框选目标元素（按钮/弹窗/节点等），
保存为 templates/<名称>.png，作为后续标定（calibrate.py）与业务识别的模板。

操作
----
1. 顶部下拉选择场景（golden_frames 下的文件夹）
2. 用 ◀ ▶ 找到合适的一帧（元素最清晰、最标准的那帧）
3. 在画布上按住左键拖拽，框住目标元素（如“重试”按钮）
4. 输入或选择模板名，点「保存模板」
5. 换帧、换场景继续，直到裁完清单里的模板

裁剪技巧（决定后面标定成败）
----------------------------
* 选独特、有纹理的区域（图标、文字局部），避免大面积纯色/渐变
* 避开数字、计时器、进度条、动画、鼠标悬停高亮
* 尺寸 20×20 ~ 200×200 像素
* 模板与 golden_frames 同分辨率（录的时候没改窗口大小就行）
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np

from vauto import load_image, save_image

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_frames"
TEMPLATES = HERE / "templates"

CANVAS_W, CANVAS_H = 980, 620

# 常用模板名（可直接选，也可自己输入新名字）
TEMPLATE_NAMES = [
    "btn_retry", "panel_result", "hud_marker",
    "popup_no_resource", "popup_confirm",
    "btn_close", "btn_back",
    "list_entry", "list_selected",
    "page_title_garage", "page_title_mastery",
    "btn_mastery", "node_grid_anchor",
    "node_inactive", "node_active",
]


class CropGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("vauto 模板裁剪器")
        self.root.geometry("1100x760")
        self.root.minsize(960, 680)

        self.scenes: list[str] = []
        self.frames: list[Path] = []
        self.frame_idx = 0
        self.frame: np.ndarray | None = None
        self.scale = 1.0
        self.sel = None                 # 显示坐标系下的选区 (x1,y1,x2,y2)
        self.sel_rect_id = None
        self._drag_start = None
        self._photo = None              # 持有引用防回收

        self._build()
        self._refresh_scenes()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")

        ttk.Label(top, text="场景:").pack(side="left")
        self.scene_var = tk.StringVar()
        self.scene_combo = ttk.Combobox(top, textvariable=self.scene_var, width=22, state="readonly")
        self.scene_combo.pack(side="left", padx=4)
        self.scene_combo.bind("<<ComboboxSelected>>", lambda e: self._load_scene())

        ttk.Button(top, text="◀ 上一帧", command=lambda: self._step_frame(-1)).pack(side="left", padx=2)
        self.frame_label = ttk.Label(top, text="- / -", width=10, anchor="center")
        self.frame_label.pack(side="left", padx=4)
        ttk.Button(top, text="下一帧 ▶", command=lambda: self._step_frame(1)).pack(side="left", padx=2)
        ttk.Button(top, text="清空选区", command=self._clear_sel).pack(side="left", padx=10)

        canvas_frame = ttk.Frame(self.root, padding=(8, 2))
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_frame, width=CANVAS_W, height=CANVAS_H,
                                bg="#1e1e1e", highlightthickness=1, highlightbackground="#555")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        bottom = ttk.Frame(self.root, padding=(8, 4))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="模板名:").pack(side="left")
        self.name_var = tk.StringVar()
        self.name_combo = ttk.Combobox(bottom, textvariable=self.name_var, width=24,
                                       values=TEMPLATE_NAMES)
        self.name_combo.pack(side="left", padx=4)
        ttk.Button(bottom, text="💾 保存模板", command=self._save).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="选场景 -> 找帧 -> 框选元素 -> 命名保存")
        ttk.Label(self.root, textvariable=self.status_var, padding=(8, 2)).pack(fill="x")

        saved = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        saved.pack(fill="x")
        ttk.Label(saved, text="已保存模板:").pack(side="left")
        self.saved_var = tk.StringVar(value="")
        self.saved_label = ttk.Label(saved, textvariable=self.saved_var, foreground="#2a6")
        self.saved_label.pack(side="left", padx=8)

    # ------------------------------------------------------------------ #
    def _refresh_scenes(self) -> None:
        self.scenes = sorted([p.name for p in GOLDEN.iterdir() if p.is_dir()]) if GOLDEN.is_dir() else []
        self.scene_combo["values"] = self.scenes
        if self.scenes:
            self.scene_combo.current(0)
            self._load_scene()

    def _load_scene(self) -> None:
        name = self.scene_var.get()
        if not name:
            return
        self.frames = sorted((GOLDEN / name).glob("*.png"))
        self.frame_idx = 0
        if self.frames:
            self._show_frame()
        else:
            self.status_var.set(f"场景 {name} 里没有 PNG 帧")

    def _step_frame(self, delta: int) -> None:
        if not self.frames:
            return
        self.frame_idx = (self.frame_idx + delta) % len(self.frames)
        self._show_frame()

    # ------------------------------------------------------------------ #
    def _show_frame(self) -> None:
        path = self.frames[self.frame_idx]
        frame = load_image(path, flags=cv2.IMREAD_COLOR)
        self.frame = frame
        self.frame_label.config(text=f"{self.frame_idx + 1}/{len(self.frames)}")
        h, w = frame.shape[:2]
        scale = min(1.0, CANVAS_W / w, CANVAS_H / h)
        self.scale = scale
        disp = frame if scale >= 1.0 else cv2.resize(
            frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", disp)
        self._photo = tk.PhotoImage(data=buf.tobytes()) if ok else None
        self.canvas.delete("all")
        if self._photo is not None:
            self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.sel = None
        self._drag_start = None
        self.sel_rect_id = None
        self.status_var.set(f"场景 {self.scene_var.get()}  帧 {self.frame_idx + 1}/{len(self.frames)}  "
                            f"原图 {w}x{h}  按住左键拖拽框选")

    # ------------------------------------------------------------------ #
    def _to_disp(self, x: int, y: int):
        return int(x * self.scale), int(y * self.scale)

    def _to_orig(self, dx: int, dy: int):
        return int(round(dx / self.scale)), int(round(dy / self.scale))

    def _on_press(self, ev) -> None:
        self._drag_start = (ev.x, ev.y)
        self.sel = (ev.x, ev.y, ev.x, ev.y)

    def _on_drag(self, ev) -> None:
        if self._drag_start is None:
            return
        x1, y1 = self._drag_start
        x2, y2 = ev.x, ev.y
        if self.sel_rect_id is not None:
            self.canvas.coords(self.sel_rect_id, x1, y1, x2, y2)
        else:
            self.sel_rect_id = self.canvas.create_rectangle(
                x1, y1, x2, y2, outline="#00e676", width=2)

    def _on_release(self, ev) -> None:
        if self._drag_start is None:
            return
        x1, y1 = self._drag_start
        self.sel = (min(x1, ev.x), min(y1, ev.y), max(x1, ev.x), max(y1, ev.y))
        w, h = self.sel[2] - self.sel[0], self.sel[3] - self.sel[1]
        self.status_var.set(f"选区: 显示 {w}x{h} px  原图约 {int(w/self.scale)}x{int(h/self.scale)} px  ->  填模板名后保存")
        self._drag_start = None

    def _clear_sel(self) -> None:
        self.sel = None
        if self.sel_rect_id is not None:
            self.canvas.delete(self.sel_rect_id)
            self.sel_rect_id = None
        self.status_var.set("选区已清空")

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        if self.frame is None or self.sel is None:
            messagebox.showwarning("提示", "请先在画布上框选一个区域")
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写模板名")
            return
        if "/" in name or "\\" in name or ":" in name:
            messagebox.showwarning("提示", "模板名不能包含 / \\ : 等字符")
            return
        x1, y1 = self._to_orig(self.sel[0], self.sel[1])
        x2, y2 = self._to_orig(self.sel[2], self.sel[3])
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        crop = self.frame[y1:y2, x1:x2]
        if crop.size == 0 or crop.shape[0] < 5 or crop.shape[1] < 5:
            messagebox.showwarning("提示", "选区太小（至少 5x5 像素）")
            return
        path = TEMPLATES / f"{name}.png"
        if path.exists():
            if not messagebox.askyesno("覆盖?", f"{path.name} 已存在，覆盖吗？"):
                return
        save_image(path, crop)
        self._refresh_saved()
        self.status_var.set(f"已保存 {path.name}（{crop.shape[1]}x{crop.shape[0]} px）-> {path}")

    def _refresh_saved(self) -> None:
        if TEMPLATES.is_dir():
            names = sorted(p.name for p in TEMPLATES.glob("*.png"))
            self.saved_var.set("、".join(names) if names else "")
        else:
            self.saved_var.set("")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="模板裁剪器（GUI）")
    p.add_argument("--smoke", action="store_true", help="自检：2 秒后自动关闭")
    args = p.parse_args(argv)

    TEMPLATES.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    CropGUI(root)
    if args.smoke:
        root.after(2000, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

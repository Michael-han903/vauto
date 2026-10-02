# -*- coding: utf-8 -*-
"""
vauto.recorder —— 帧录制器（素材工作流第 1 步）

用途
----
从目标窗口客户区按随机间隔录制 N 帧「原始画面」到 golden_frames/<scene>/，
作为模板裁剪与离线标定的素材库。录制的是干净帧（不带任何画框标注），
与运行时 WindowCapture.grab() 的输出完全同源、同分辨率。

录制完成后：
  1) 从帧里裁模板（保存到 templates/）；
  2) 用 calibrate.py 对模板做正负样本标定，得到建议阈值（第 2 步）；
  3) 后续可用离线回放对 golden_frames 跑匹配逻辑，不碰真实输入（第 7 步）。

安全
----
录制本身不做任何输入模拟，不会动鼠标键盘；仅截图并保存到本地目录。
本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from .capture import WindowCapture, WindowUnavailable
from .matching import save_image
from .timing import Humanizer, interruptible_sleep

__all__ = ["record_window", "RecordResult"]


class RecordResult:
    """一次录制的汇总信息。"""

    def __init__(self, scene: str, out_dir: Path, files: List[Path], elapsed: float):
        self.scene = scene
        self.out_dir = out_dir
        self.files = files
        self.elapsed = elapsed

    @property
    def count(self) -> int:
        return len(self.files)

    def summary(self) -> str:
        return (
            f"[record] 场景 {self.scene!r}: 共录制 {self.count} 帧 -> {self.out_dir}"
            f"（耗时 {self.elapsed:.1f}s）"
        )


def record_window(
    hwnd: int,
    scene: str,
    n_frames: int = 30,
    interval: Tuple[float, float] = (0.3, 0.8),
    out_dir: Union[str, Path] = "golden_frames",
    client_only: bool = True,
    seed: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_frame: Optional[Callable[[int, Path], None]] = None,
) -> RecordResult:
    """
    从目标窗口录制 n_frames 帧原始画面。

    参数
    ----
    hwnd        目标窗口句柄
    scene       场景名（如 'challenge_result' / 'car_mastery'），会作为子目录名
    n_frames    录制帧数
    interval    (min, max) 秒，两帧之间的随机间隔
    out_dir     输出根目录，默认 golden_frames/
    client_only 抓客户区（默认，保证与模板裁剪同源）
    seed        随机种子（可复现间隔序列）
    stop_event  急停事件；设置后立即停止录制（配合 EmergencyStop）
    on_frame    每保存一帧后的回调 (index, path)，可用于打印进度

    返回 RecordResult。抓帧失败（窗口关闭/最小化）时停止录制并返回已录部分。
    """
    cap = WindowCapture(hwnd, client_only=client_only)
    human = Humanizer(seed=seed)
    scene_clean = (scene or "scene").strip().replace("/", "_").replace("\\", "_")
    out = Path(out_dir) / scene_clean
    out.mkdir(parents=True, exist_ok=True)

    files: List[Path] = []
    start = time.monotonic()
    try:
        for i in range(max(1, int(n_frames))):
            if stop_event is not None and stop_event.is_set():
                break
            try:
                frame = cap.grab()
            except WindowUnavailable as exc:
                print(f"[record] 抓帧失败（已录制 {len(files)} 帧），停止: {exc}")
                break
            path = out / f"frame_{i:04d}_{int(time.time() * 1000)}.png"
            save_image(path, frame)
            files.append(path)
            if on_frame is not None:
                on_frame(i, path)
            if i < n_frames - 1:
                interruptible_sleep(human.sample(interval), stop_event)
    finally:
        cap.close()

    return RecordResult(scene=scene_clean, out_dir=out, files=files,
                        elapsed=time.monotonic() - start)

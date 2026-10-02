# -*- coding: utf-8 -*-
"""
flow.probe —— 判据探针（人工翻页复核）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本原型仅用于算法学习（计算机视觉 / 输入仿真研究）。若用于第三方软件，
可能违反该软件用户许可协议（EULA）或服务条款，并可能触发风控 / 反作弊，
存在账号被封禁的风险，请自行承担后果。第一次请勿使用主账号。
本模块**不发送任何键鼠输入**，只抓帧 + 打分 + 打印。
======================================================================

用途：你自己动手翻游戏页面（Esc → 车辆 → 更换车辆 → 方向键 → Enter…），
程序每 2 秒把你**当前画面**上每个判据的分数打出来：

    ● tile_change_car       0.931  阈值 0.628  ✔ 命中
      选项条                  0.512  阈值 0.750
      ...

翻完后按 Ctrl+C，会得到一张汇总表：每个判据「最高分 / 通过次数 / 结论」，
以及**每个判据第一次命中时那一帧的 ROI 截图**（存到 debug_frames/probe/），
用来人工确认"它认的到底是不是我以为的那个东西"。

这是标定之外的第二道验收：标定证明"素材上能分开"，探针证明"实机上没认错"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np

from .runner import Stack

# 低于这个分数的判据不打进实时列表（除非它通过了阈值）
QUIET_BELOW = 0.35


def _fmt(score: float, thr: float, ok: bool) -> str:
    return f"{score:5.3f}/{thr:.3f} {'✔' if ok else ' '}"


def _save_first_pass(frame: np.ndarray, det, match, out_dir: Path) -> Optional[str]:
    """保存某判据第一次命中时的 ROI 裁剪（画上命中框）—— 事后人工确认认的是不是那个东西。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        roi = det.roi
        if roi is None:
            crop = frame.copy()
            ox, oy = 0, 0
        else:
            x, y, w, h = roi
            x, y = max(0, x), max(0, y)
            crop = frame[y:y + h, x:x + w].copy()
            ox, oy = x, y
        if match is not None:
            mx, my, mw, mh = match.x - ox, match.y - oy, match.width, match.height
            cv2.rectangle(crop, (mx, my), (mx + mw, my + mh), (0, 0, 255), 3)
            cv2.putText(crop, f"{match.score:.3f}", (max(0, mx), max(18, my - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3, cv2.LINE_AA)
        path = out_dir / f"{det.name}__first_pass.png"
        cv2.imencode(".png", crop)[1].tofile(str(path))
        return str(path)
    except Exception as exc:                                  # 存图失败不该打断探针
        return f"(存图失败: {exc})"


def probe_loop(stack: Stack,
               interval: float = 2.0,
               quiet_below: float = QUIET_BELOW,
               seconds: Optional[float] = None,
               out_dir: str = "debug_frames/probe") -> Dict[str, dict]:
    """
    主循环：抓帧 → 给每个判据打一次分 → 按需打印。Ctrl+C 结束。

    返回汇总 dict：{判据名: {"threshold", "best", "passes", "roi_shot"}}
    """
    dets = stack.dets
    out = Path(out_dir)
    stat: Dict[str, dict] = {
        name: {"threshold": float(det.threshold), "best": float("-inf"),
               "passes": 0, "roi_shot": None, "name": name}
        for name, det in dets.items()
    }

    t0 = time.monotonic()
    next_print = 0.0
    frames = 0
    print(f"判据探针启动：{len(dets)} 个判据 | 每 {interval:g}s 打一次 | "
          f"自己翻页面，Ctrl+C 结束" + (f"（{seconds:g}s 后自动结束）" if seconds else ""))
    print(f"{'':2}{'判据':24s}{'分数/阈值':>14s}   说明")
    try:
        while True:
            if seconds is not None and time.monotonic() - t0 >= seconds:
                break
            frame = stack.capture.grab()
            frames += 1
            rows = []
            for name, det in dets.items():
                score, match = det.probe(frame)
                if score == score:                            # not nan
                    if score > stat[name]["best"]:
                        stat[name]["best"] = float(score)
                ok = (score == score) and score >= det.threshold
                if ok:
                    stat[name]["passes"] += 1
                    if stat[name]["roi_shot"] is None:
                        stat[name]["roi_shot"] = _save_first_pass(frame, det, match, out)
                rows.append((score if score == score else -1.0, name, det.threshold, ok))

            now = time.monotonic()
            if now >= next_print:
                next_print = now + interval
                rows.sort(key=lambda r: -r[0])
                passing = [r for r in rows if r[3]]
                below = [r for r in rows if not r[3] and r[0] >= quiet_below][:3]
                head = (f"命中 {passing[0][1]} ({passing[0][0]:.3f})" if passing
                        else "无命中")
                print(f"\n--- 第 {frames} 帧  t={now - t0:5.1f}s  {head} ---")
                for score, name, thr, ok in passing:
                    print(f"  ● {name:24s}{_fmt(score, thr, ok)}  ← 命中")
                for score, name, thr, ok in below:
                    print(f"    {name:24s}{_fmt(score, thr, ok)}")

            if interval <= 0:
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n(收到 Ctrl+C，输出汇总)")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("汇总（按「是否曾命中」和最高分排序）")
    print("=" * 72)
    order = sorted(stat.values(), key=lambda s: (-(s["passes"] > 0), -s["best"]))
    for s in order:
        best = "nan" if s["best"] == float("-inf") else f"{s['best']:.3f}"
        if s["passes"] and s["best"] >= s["threshold"] + 0.10:
            verdict = "✅ 稳（超阈值 0.10 以上）"
        elif s["passes"]:
            verdict = "⚠️ 擦边（刚过阈值，考虑降阈值或换模板）"
        else:
            verdict = "❌ 本次没出现过（可能是你还没翻到那一页）"
        shot = f"  图: {s['roi_shot']}" if s["roi_shot"] else ""
        print(f"  {s['name']:24s} 阈值 {s['threshold']:.3f}  最高 {best:>5s}  "
              f"通过 {s['passes']:4d} 帧  {verdict}{shot}")
    print(f"\n共看 {frames} 帧（{time.monotonic() - t0:.0f}s）")
    print("把上面这段贴给我，我就能复核那些「手工推定」的阈值。")
    return stat

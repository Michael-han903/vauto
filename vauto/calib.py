# -*- coding: utf-8 -*-
"""
vauto.calib —— 读取标定产物，按名字装配检测器

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。本模块不做业务判断，只负责把
`templates/thresholds.json`（calibrate.py 产出）与 `templates/manual_thresholds.json`
（无法自动标定的少数元素）合并成「名字 -> VisualDetector」的字典。

用法
----
    from vauto.calib import load_calibration, build_detectors
    cal = load_calibration()                       # dict：{scale, templates:{...}}
    dets = build_detectors(["hint_esc_retry", "hint_unlock_all"])
    hit = dets["hint_esc_retry"].observe(frame)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional

from .matching import TemplateMatcher
from .vision import VisualDetector

__all__ = ["load_calibration", "build_detectors", "calibration_table"]

HERE = Path(__file__).resolve().parent.parent
DEFAULT_DIR = HERE / "templates"
MAIN_FILE = "thresholds.json"
MANUAL_FILE = "manual_thresholds.json"


def load_calibration(templates_dir: Optional[Path] = None,
                     main_file: str = MAIN_FILE,
                     manual_file: str = MANUAL_FILE) -> dict:
    """
    合并读取标定结果。manual 里的条目会覆盖同名条目（都是手工推定的，必须复核）。

    返回 {"scale": float, "templates": {name: {...}}, "sources": [...]}
    """
    tdir = Path(templates_dir) if templates_dir else DEFAULT_DIR
    merged: Dict[str, dict] = {}
    scale = 1.0
    sources = []

    for fname in (main_file, manual_file):
        path = tdir / fname
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        scale = float(data.get("scale", scale))
        for name, item in (data.get("templates") or {}).items():
            if item.get("threshold") is None:
                continue                      # 不可用的条目不进运行期
            merged[name] = {**item, "source": fname}
        sources.append(fname)

    if not merged:
        raise FileNotFoundError(
            f"{tdir} 下没有可用的标定结果（先跑 py -3.14 calibrate.py）")
    return {"scale": scale, "templates": merged, "sources": sources, "dir": str(tdir)}


def build_detectors(names: Iterable[str],
                    cal: Optional[dict] = None,
                    matcher: Optional[TemplateMatcher] = None,
                    templates_dir: Optional[Path] = None,
                    optional: Iterable[str] = ()) -> Dict[str, VisualDetector]:
    """
    按名字装配 VisualDetector（阈值/ROI/尺度/去抖帧数全来自标定结果，运行期不要手写）。

    缺名字时：默认直接抛 KeyError（避免静默用错阈值）；
    列进 `optional` 的可以缺，缺了只打印一行警告。
    """
    cal = cal or load_calibration(templates_dir)
    opt = set(optional)
    tdir = Path(cal.get("dir") or templates_dir or DEFAULT_DIR)
    matcher = matcher or TemplateMatcher(grayscale=True, threshold=0.8)
    out: Dict[str, VisualDetector] = {}
    for name in names:
        item = cal["templates"].get(name)
        if item is None:
            if name in opt:
                print(f"[calib] 可选判据 {name!r} 没有可用标定，已跳过")
                continue
            raise KeyError(f"标定结果里没有 {name!r}；可用：{sorted(cal['templates'])}")
        out[name] = VisualDetector(
            matcher,
            str(tdir / Path(item["file"]).name),
            roi=tuple(item["roi"]) if item.get("roi") else None,
            scale=float(item.get("scale", cal["scale"])),
            threshold=float(item["threshold"]),
            release_threshold=(float(item["release_threshold"])
                               if item.get("release_threshold") is not None else None),
            confirm_frames=int(item.get("confirm_frames", 2)),
            min_std=float(item.get("min_std", 0.0) or 0.0),
            name=name,
        )
    return out


def calibration_table(cal: dict) -> str:
    """人类可读的判据清单（启动时打印一份，日志里能回答"当时用的什么阈值"）。"""
    lines = [f"标定来源 {cal.get('sources')}  尺度 {cal.get('scale')}"]
    for name, t in sorted(cal["templates"].items()):
        tag = "手工推定" if t.get("source") == MANUAL_FILE else "已标定"
        lines.append(f"  {name:<22} 阈值 {t['threshold']:<7} ROI {str(t.get('roi')):<26} "
                     f"{t.get('match_ms') or '-':>6}   {tag}")
    return "\n".join(lines)

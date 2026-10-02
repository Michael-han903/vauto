# -*- coding: utf-8 -*-
"""
calibrate.py —— 模板标定工具（第 2 步 2b）

做什么
------
对 templates/ 里的每张模板，用 golden_frames/ 的真实帧自动评估：
  1. 正样本帧（该元素【应该出现】的画面，如 btn_retry -> challenge_result 场景）
  2. 负样本帧（该元素【不应该出现】的其他画面）
  3. 分别统计匹配分数，给出【建议阈值】与【可分离性】判断

用法
----
    py -3.14 calibrate.py                          # 标定全部模板
    py -3.14 calibrate.py --template btn_retry     # 只标定一张
    py -3.14 calibrate.py --report report.md       # 同时输出 markdown 报告
    py -3.14 calibrate.py --frames-per-scene 30    # 每个场景最多取多少帧（提速）

结果
----
1. 控制台表格 + 可选 report.md
2. templates/thresholds.json —— 每张模板的建议阈值（第 3 步 manifest 直接读它）

怎么读结果
----------
* 「分离良好」：负样本最高分远低于正样本最低分 -> 这个模板靠谱，阈值按建议值
* 「重叠」：负样本里有些帧分数高于正样本 -> 模板区分度不足，
  需要重新裁更小/更有纹理/更独特的区域（或换颜色判据）
* 阈值含义：匹配分数 >= 阈值才算「认出来了」；阈值越高越严格（宁可不点，不可点错）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from vauto import TemplateMatcher, load_image

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_frames"
TEMPLATES = HERE / "templates"

# 模板 -> 该元素【应该出现】的场景（正样本）。没列出的模板用「同名场景」兜底。
SCENE_POSITIVE = {
    "btn_retry": ["challenge_result"],
    "panel_result": ["challenge_result"],
    "hud_marker": ["challenge_hud"],
    "popup_no_resource": ["popup_no_resource"],
    "popup_confirm": ["popup_no_resource"],
    "list_entry": ["garage_list"],
    "page_title_garage": ["garage_list"],
    "page_title_mastery": ["car_mastery_page"],
    "node_grid_anchor": ["car_mastery_page"],
    "node_inactive": ["node_inactive", "car_mastery_page"],
    "node_active": ["node_active", "car_mastery_page"],
}

DEFAULT_FRAMES_PER_SCENE = 40


def scene_frames(scene: str, limit: int) -> list[Path]:
    d = GOLDEN / scene
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.png"))
    return files[:limit]


def score_template(matcher: TemplateMatcher, tpl_path: Path, frames: list[Path]) -> list[float]:
    tpl = load_image(tpl_path, flags=3)  # IMREAD_COLOR
    scores: list[float] = []
    for f in frames:
        try:
            src = load_image(f, flags=3)
        except Exception:
            continue
        # 标定时必须无条件取最高分（阈值是标定的输出，不能当输入过滤掉分数）
        hit = matcher.match_best(src, tpl, scales=(1.0,), threshold=-2.0)
        if hit is not None:
            scores.append(hit.score)
    return scores


def summarize(scores: list[float]) -> dict:
    if not scores:
        return {"n": 0, "min": None, "p05": None, "median": None, "max": None}
    a = np.asarray(scores, dtype=np.float64)
    return {
        "n": int(a.size),
        "min": float(a.min()),
        "p05": float(np.percentile(a, 5)),
        "median": float(np.median(a)),
        "max": float(a.max()),
    }


def suggest_threshold(pos: dict, neg: dict,
                      pos_scores: np.ndarray, neg_scores: np.ndarray) -> tuple[float | None, str]:
    if pos["n"] == 0 or neg["n"] == 0:
        return None, "样本不足（正/负至少各 1 帧）"
    # 完美分离时用中点；否则用分位数中点，并警告重叠
    if neg["max"] < pos["min"]:
        thr = (neg["max"] + pos["min"]) / 2.0
        return round(thr, 3), "分离良好"
    neg_q = float(np.percentile(neg_scores, 95))
    pos_q = float(np.percentile(pos_scores, 5))
    thr = (neg_q + pos_q) / 2.0
    if pos_q <= neg_q:
        return round(thr, 3), "重叠（正负样本分数区间交叠，建议重裁模板或换判据）"
    return round(thr, 3), "基本可用（分位数法）"


def calibrate_one(tpl: Path, frames_per_scene: int, matcher: TemplateMatcher,
                  verbose: bool = True) -> dict | None:
    name = tpl.stem
    pos_scenes = SCENE_POSITIVE.get(name, [name] if (GOLDEN / name).is_dir() else [])
    all_scenes = sorted([p.name for p in GOLDEN.iterdir() if p.is_dir()]) if GOLDEN.is_dir() else []
    neg_scenes = [s for s in all_scenes if s not in pos_scenes]

    pos_frames: list[Path] = []
    for s in pos_scenes:
        pos_frames += scene_frames(s, frames_per_scene)
    neg_frames: list[Path] = []
    for s in neg_scenes:
        neg_frames += scene_frames(s, frames_per_scene)

    pos_scores = np.asarray(score_template(matcher, tpl, pos_frames), dtype=np.float64)
    neg_scores = np.asarray(score_template(matcher, tpl, neg_frames), dtype=np.float64)
    pos = summarize(pos_scores.tolist())
    neg = summarize(neg_scores.tolist())

    thr, verdict = suggest_threshold(pos, neg, pos_scores, neg_scores)

    if verbose:
        print(f"\n==== {name}  ({tpl.name}) ====")
        print(f"  正样本 {pos_scenes}  {pos['n']} 帧:  min={_fmt(pos['min'])} p05={_fmt(pos['p05'])} "
              f"median={_fmt(pos['median'])} max={_fmt(pos['max'])}")
        print(f"  负样本 {len(neg_scenes)} 场景 {neg['n']} 帧: min={_fmt(neg['min'])} "
              f"p05={_fmt(neg['p05'])} median={_fmt(neg['median'])} max={_fmt(neg['max'])}")
        print(f"  -> 建议阈值: {thr if thr is not None else 'N/A'}   [{verdict}]")

    return {
        "name": name,
        "file": tpl.name,
        "pos_scenes": pos_scenes,
        "pos": pos,
        "neg": neg,
        "threshold": thr,
        "verdict": verdict,
    }


def _fmt(v: float | None) -> str:
    return "  -  " if v is None else f"{v:.3f}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="模板标定工具")
    p.add_argument("--template", type=str, default="", help="只标定名称含此关键字的模板")
    p.add_argument("--frames-per-scene", type=int, default=DEFAULT_FRAMES_PER_SCENE, help="每场景最多取帧数")
    p.add_argument("--report", type=str, default="", help="输出 markdown 报告路径")
    args = p.parse_args(argv)

    if not TEMPLATES.is_dir():
        print("[!] templates/ 不存在，先裁剪模板")
        return 2
    tpls = sorted(TEMPLATES.glob("*.png"))
    if args.template:
        tpls = [t for t in tpls if args.template.lower() in t.stem.lower()]
        if not tpls:
            print(f"[!] templates/ 里没有名称含 {args.template!r} 的模板")
            return 2

    matcher = TemplateMatcher(grayscale=True, threshold=0.8)
    results = []
    for t in tpls:
        r = calibrate_one(t, args.frames_per_scene, matcher)
        if r is not None:
            results.append(r)

    # 汇总表
    print("\n" + "=" * 78)
    print(f"{'模板':<22}{'正帧':>5}{'正min':>8}{'正max':>8}{'负帧':>6}{'负max':>8}  建议阈值  判定")
    print("-" * 78)
    for r in results:
        print(f"{r['name']:<22}{r['pos']['n']:>5}{_fmt(r['pos']['min']):>8}{_fmt(r['pos']['max']):>8}"
              f"{r['neg']['n']:>6}{_fmt(r['neg']['max']):>8}  {str(r['threshold']):>8}  {r['verdict']}")

    # 写 thresholds.json
    out = {}
    for r in results:
        if r["threshold"] is not None:
            out[r["name"]] = {
                "threshold": r["threshold"],
                "verdict": r["verdict"],
                "pos_min": r["pos"]["min"],
                "pos_max": r["pos"]["max"],
                "neg_max": r["neg"]["max"],
            }
    out_path = TEMPLATES / "thresholds.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] 建议阈值已写入 {out_path}（共 {len(out)} 张）")

    if args.report:
        md = ["# 模板标定报告\n",
              f"| 模板 | 正样本 | 正min | 正max | 负样本 | 负max | 建议阈值 | 判定 |",
              f"|---|---|---|---|---|---|---|---|"]
        for r in results:
            md.append(f"| {r['name']} | {r['pos']['n']} | {_fmt(r['pos']['min'])} | {_fmt(r['pos']['max'])} | "
                      f"{r['neg']['n']} | {_fmt(r['neg']['max'])} | {r['threshold']} | {r['verdict']} |")
        Path(args.report).write_text("\n".join(md) + "\n", encoding="utf-8")
        print(f"[+] 报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

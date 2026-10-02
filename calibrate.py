# -*- coding: utf-8 -*-
"""
calibrate.py —— 模板标定工具（两段式：粗定位 ROI → ROI 内全分辨率精确标定）

做什么
------
对 templates/ 里的每张业务模板，用 golden_frames/ 的真实帧自动评估，输出：
  * 建议阈值（含滞回用的 release_threshold）
  * 是否可用（正负样本分数区间是否真的分得开）
  * 运行期搜索区域 ROI(x,y,w,h)
  * 该阈值下的检出率 / 误报率
  * ROI 内一次匹配的真实耗时 ms（运行期轮询成本的估计）
结果写 templates/thresholds.json，运行期直接读它。

两段式为什么快
--------------
第 1 段：在 0.5 尺度上对正样本做**全帧**搜索（便宜），得到命中位置 → 推出 ROI。
第 2 段：在 ROI 内做**全分辨率**精确评分（正样本 + 负样本），这也是运行期真实的搜索方式。
实测：全帧全分辨率标定 14 张模板要 6.5 分钟（672 次 * 0.5s），两段式约 30 秒，
      而且第 2 段的分数就是运行期会看到的分数（阈值可直接用）。
需要"严格全帧"结论时加 --full-frame（慢 10 倍，用于排查模板是否会在别处误命中）。

参数
----
尺度必须一致：帧与模板同时按 scale 缩放。**同一模板 1.0 尺度得 1.000、0.5 只得 0.879**，
阈值不能跨尺度复用，所以 thresholds.json 里同时记录了 scale。

用法
----
    py -3.14 calibrate.py                       # 标定全部（默认 ROI + 全分辨率）
    py -3.14 calibrate.py --template hint       # 只标定名称含 hint 的
    py -3.14 calibrate.py --report docs/标定报告.md
    py -3.14 calibrate.py --all                 # 连自检素材一起
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from vauto import TemplateMatcher, load_image, scaled_frame
from vauto.vision import to_full_point

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_frames"
TEMPLATES = HERE / "templates"

# 每张模板的【正样本】文件（相对 golden_frames 的 glob）。
# 要点：node_active/ 与 node_inactive/ 两个目录里的帧其实是"车辆精通整页"，所以
#       该页面的所有元素模板都要把它们算进正样本，否则它们会被当成负样本 → 假重叠。
MASTERY_SCENES = ["car_mastery_page/*", "node_active/*", "node_inactive/*"]
TEMPLATE_POSITIVE = {
    "btn_retry":          ["challenge_result/*"],
    "hint_esc_retry":     ["challenge_result/*"],
    "panel_result":       ["challenge_result/*"],
    "hud_marker":         ["challenge_hud/*"],
    "popup_no_resource":  ["popup_no_resource/*"],
    "popup_confirm":      ["popup_no_resource/*"],
    "list_entry":         ["garage_list/*", "current_car_22b_garage/*"],
    "page_title_garage":  ["garage_list/*", "current_car_22b_garage/*"],
    "hint_esc_back":      MASTERY_SCENES + ["garage_list/*", "current_car_22b_menu/*",
                                            "current_car_22b_garage/*"],
    "page_title_mastery": MASTERY_SCENES,
    "node_grid_anchor":   MASTERY_SCENES,
    "node_inactive":      MASTERY_SCENES,
    "node_active":        MASTERY_SCENES,
    # 「本页还有可解锁」提示：正样本必须排除"已全部解锁"的页面
    # （Apollo 截图 180316 与 node_active/ 整个场景都属于"已全部解锁"——实测它们不含 Y 提示）
    "hint_unlock_all":    ["car_mastery_page/frame_*",
                           "car_mastery_page/屏幕截图*180353*",
                           "car_mastery_page/屏幕截图*180412*",
                           "node_inactive/*"],
    # 当前车辆识别（用于"跑 A 之前必须确认当前车是 1998 斯巴鲁 Impreza 22B-STI"）
    # 注意：正样本是"该元素在画面里可见"的帧，不是"当前车是 22B"的帧 —— 这两者不等价。
    "car_current_menu":   ["current_car_22b_menu/*"],
    "car_current_garage": ["current_car_22b_garage/*", "current_car_22b_strip/*",
                           "garage_list/*"],
}

# 自检脚本用的素材，不是业务模板，默认跳过
SKIP_BY_DEFAULT = {"example_patch", "selftest_patch", "selftest_patch_half",
                   "selftest_alpha", "selftest_debug", "自检_中文路径"}

ROI_MARGIN = 12.0            # 自动 ROI 的外扩像素（全分辨率）
MIN_FRAMES = 3               # 正/负样本各至少这么多帧才给结论
MIN_POSITIVE_SCORE = 0.60    # 正样本 p05 下限：低于它说明模板在该出现的地方都匹配不上
MIN_MARGIN = 0.05            # 正样本最低分与负样本最高分之间至少要拉开这么多
MIN_DETECT_RATE = 0.90       # 阈值处正样本检出率下限
MAX_FALSE_POSITIVE = 0.10    # 阈值处负样本误报率上限


# --------------------------------------------------------------------------- #
# 帧缓存
# --------------------------------------------------------------------------- #
class FrameCache:
    """按 scale 缓存解码后的灰度帧，整个标定过程复用（每帧只解码一次）。"""

    def __init__(self, scale: float, grayscale: bool = True) -> None:
        self.scale = float(scale)
        self.grayscale = bool(grayscale)
        self._cache: dict[Path, np.ndarray] = {}
        self.loads = 0
        self.time = 0.0
        import cv2
        self._cv2 = cv2

    def get(self, path: Path) -> np.ndarray | None:
        hit = self._cache.get(path)
        if hit is not None:
            return hit
        t0 = time.perf_counter()
        try:
            img = load_image(path, flags=3)              # IMREAD_COLOR
        except Exception:
            return None
        img = scaled_frame(img, self.scale)
        if self.grayscale:
            img = self._cv2.cvtColor(img, self._cv2.COLOR_BGR2GRAY)
        self._cache[path] = img
        self.loads += 1
        self.time += time.perf_counter() - t0
        return img

    def memory_mb(self) -> float:
        return sum(a.nbytes for a in self._cache.values()) / 1e6


def all_frames() -> dict[str, list[Path]]:
    """{场景名: [帧路径...]}，按文件名排序。"""
    if not GOLDEN.is_dir():
        return {}
    return {d.name: sorted(d.glob("*.png")) for d in sorted(GOLDEN.iterdir()) if d.is_dir()}


def positive_frames(name: str, index: dict[str, list[Path]]) -> list[Path]:
    """模板的正样本帧；没配置时用同名场景兜底。"""
    pats = TEMPLATE_POSITIVE.get(name) or ([f"{name}/*"] if name in index else [])
    out: list[Path] = []
    for pat in pats:
        scene, _, sub = pat.partition("/")
        files = index.get(scene, [])
        out += files if (not sub or sub == "*") else [p for p in files if p.match(sub)]
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def sample_per_scene(files: list[Path], per_scene: int) -> list[Path]:
    """按场景分组，每个场景只取前 per_scene 帧（per_scene<=0 不限制）。"""
    if per_scene <= 0:
        return list(files)
    out: list[Path] = []
    seen: dict[str, int] = {}
    for p in files:
        n = seen.get(p.parent.name, 0)
        if n >= per_scene:
            continue
        seen[p.parent.name] = n + 1
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def score_frames(matcher: TemplateMatcher, tpl, frames: list[Path], cache: FrameCache,
                 region: list[int] | None = None
                 ) -> tuple[list[float], list[tuple[int, int]], float]:
    """逐帧取最高分（标定时必须无条件取最高分，阈值是输出不是输入）。

    返回 (分数列表, 命中中心(全分辨率), 平均每次匹配毫秒)。
    region: 全分辨率坐标的 ROI；None = 全帧。帧已按 cache.scale 缩放，
    模板通过 scales=(cache.scale,) 同步缩放。
    """
    scores: list[float] = []
    centers: list[tuple[int, int]] = []
    scaled_region = None
    if region is not None:
        k = cache.scale
        scaled_region = (int(region[0] * k), int(region[1] * k),
                         max(1, int(region[2] * k)), max(1, int(region[3] * k)))
    spent = 0.0
    n = 0
    for p in frames:
        src = cache.get(p)
        if src is None:
            continue
        t0 = time.perf_counter()
        hit = matcher.match_best(src, tpl, threshold=-2.0, region=scaled_region,
                                 scales=(cache.scale,))
        spent += time.perf_counter() - t0
        n += 1
        if hit is None:
            continue
        scores.append(hit.score)
        centers.append(to_full_point(hit.center, cache.scale))
    return scores, centers, (spent / n * 1000.0 if n else 0.0)


def summarize(scores: list[float]) -> dict:
    if not scores:
        return {"n": 0, "min": None, "p05": None, "median": None, "p95": None, "max": None}
    a = np.asarray(scores, dtype=np.float64)
    return {"n": int(a.size),
            "min": float(a.min()), "p05": float(np.percentile(a, 5)),
            "median": float(np.median(a)), "p95": float(np.percentile(a, 95)),
            "max": float(a.max())}


def rate_at(scores: list[float], threshold: float, above: bool = True) -> float | None:
    """scores 中 >= 阈值（above=True）或 < 阈值（above=False）的比例。"""
    if not scores:
        return None
    a = np.asarray(scores, dtype=np.float64)
    return float(((a >= threshold) if above else (a < threshold)).mean())


def decide(pos: dict, neg: dict, pos_scores: list[float], neg_scores: list[float]
           ) -> tuple[float | None, str, bool]:
    """
    给出 (建议阈值, 判定, 是否可用)。

    四道闸门，缺一不可（旧版只做了第一道，于是把 hud_marker 判成"基本可用"）：
      1. 正样本 p05 要够高（>= MIN_POSITIVE_SCORE）—— 模板在该出现的地方必须真的匹配上；
      2. 正负样本分数区间要分得开（完全分离 / 分位数分离），否则直接不可用；
      3. 正样本最低分与负样本最高分之间要有 MIN_MARGIN 的余量 —— 余量过小通常说明
         正负样本分组分错了（某个负样本画面里本来就该有这个元素），或该元素在别处也会出现；
      4. 在选定阈值上实测：检出率 >= 0.90 且误报率 <= 0.10。
    """
    if pos["n"] < MIN_FRAMES or neg["n"] < MIN_FRAMES:
        return None, f"样本不足（正/负各需 >= {MIN_FRAMES} 帧）", False

    if pos["p05"] < MIN_POSITIVE_SCORE:
        return None, (f"不可用（正样本 p05 仅 {pos['p05']:.3f} < {MIN_POSITIVE_SCORE}："
                      f"模板在它该出现的地方都匹配不上 —— 裁错区域、尺度不一致，或该元素会变化）"), False

    if neg["max"] < pos["min"]:
        thr, verdict = (neg["max"] + pos["min"]) / 2.0, "分离良好"
        margin = pos["min"] - neg["max"]
    elif neg["p95"] < pos["p05"]:
        thr, verdict = (neg["p95"] + pos["p05"]) / 2.0, "基本可用（分位数法，余量偏小）"
        margin = pos["p05"] - neg["p95"]
    else:
        return None, ("不可用（正负分数区间重叠：无论阈值取哪都会误判或漏判，"
                      "需重裁更独特/更小的模板、换判据、或换 ROI）"), False

    if margin < MIN_MARGIN:
        return None, (f"不可用（余量仅 {margin:+.3f} < {MIN_MARGIN}：负样本里出现了几乎相同的画面，"
                      f"多半是正负分组分错了，或该元素在别的页面也会出现）"), False

    thr = round(thr, 3)
    detect = rate_at(pos_scores, thr, True) or 0.0
    fp = rate_at(neg_scores, thr, True) or 0.0
    if detect < MIN_DETECT_RATE or fp > MAX_FALSE_POSITIVE:
        return None, (f"不可用（阈值 {thr} 处 检出率 {detect:.2f} / 误报率 {fp:.2f} 不达标）"), False
    return thr, verdict, True


def derive_roi(centers: list[tuple[int, int]], size: tuple[int, int],
               frame_size: tuple[int, int]) -> list[int] | None:
    """由正样本命中中心推出搜索区域：中心 bbox + 模板半尺寸 + margin（全分辨率坐标）。"""
    if not centers:
        return None
    tw, th = size
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]
    x1 = int(min(xs) - tw / 2 - ROI_MARGIN)
    y1 = int(min(ys) - th / 2 - ROI_MARGIN)
    x2 = int(max(xs) + tw / 2 + ROI_MARGIN)
    y2 = int(max(ys) + th / 2 + ROI_MARGIN)
    W, H = frame_size
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return [x1, y1, x2 - x1, y2 - y1]


# --------------------------------------------------------------------------- #
# 单张模板：两段式
# --------------------------------------------------------------------------- #
def calibrate_one(tpl: Path, index: dict[str, list[Path]], coarse: FrameCache, fine: FrameCache,
                  matcher: TemplateMatcher, pos_limit: int, neg_limit: int,
                  full_frame: bool = False, verbose: bool = True) -> dict:
    name = tpl.stem
    raw = load_image(tpl, flags=3)
    pos_all = positive_frames(name, index)
    pos_files = sample_per_scene(pos_all, pos_limit)
    pos_set = set(pos_all)
    neg_all = [p for files in index.values() for p in files if p not in pos_set]
    neg_files = sample_per_scene(neg_all, neg_limit)

    frame_size = (3840, 2160)
    for files in index.values():
        if files:
            probe = fine.get(files[0])
            if probe is not None:
                frame_size = (int(probe.shape[1] / fine.scale), int(probe.shape[0] / fine.scale))
            break

    # --- 第 1 段：0.5 尺度全帧粗搜正样本 -> ROI ---
    roi = None
    coarse_frames = sample_per_scene(pos_files, max(2, pos_limit // 2))
    _, centers, _ = score_frames(matcher, raw, coarse_frames, coarse)
    roi = derive_roi(centers, (raw.shape[1], raw.shape[0]), frame_size)

    # ROI 已知时用【全部】负样本帧：ROI 内匹配只要 10 ms 量级，
    # 没必要抽样 —— 抽样会漏掉偶尔拿到高分的负样本，导致阈值定得过低。
    if roi is not None and neg_limit > 0:
        neg_files = neg_all

    # --- 第 2 段：ROI 内（或全帧）全分辨率精确评分 ---
    use_region = None if (full_frame or roi is None) else roi
    pos_scores, pos_centers, ms = score_frames(matcher, raw, pos_files, fine, region=use_region)
    neg_scores, _, ms_neg = score_frames(matcher, raw, neg_files, fine, region=use_region)
    pos, neg = summarize(pos_scores), summarize(neg_scores)
    thr, verdict, usable = decide(pos, neg, pos_scores, neg_scores)

    # 第 1 段没拿到 ROI 但第 2 段可用时，用第 2 段的命中位置补一个
    if usable and roi is None:
        roi = derive_roi(pos_centers, (raw.shape[1], raw.shape[0]), frame_size)

    detect = rate_at(pos_scores, thr, True) if thr is not None else None
    fp = rate_at(neg_scores, thr, True) if thr is not None else None

    if verbose:
        print(f"\n==== {name}  ({tpl.name}) {raw.shape[1]}x{raw.shape[0]} ====")
        print(f"  正样本 {pos['n']} 帧: min={_f(pos['min'])} p05={_f(pos['p05'])} "
              f"median={_f(pos['median'])} max={_f(pos['max'])}")
        print(f"  负样本 {neg['n']} 帧: min={_f(neg['min'])} p05={_f(neg['p05'])} "
              f"median={_f(neg['median'])} max={_f(neg['max'])}")
        print(f"  -> 阈值 {thr if thr is not None else 'N/A'}   [{verdict}]")
        if usable:
            print(f"  -> ROI = {tuple(roi) if roi else '全帧'}  检出率={_f(detect)} "
                  f"误报率={_f(fp)}  一次匹配 {ms:.1f} ms")

    return {
        "name": name, "file": f"templates/{tpl.name}",
        "size": [int(raw.shape[1]), int(raw.shape[0])],
        "scale": fine.scale,
        "threshold": thr,
        "release_threshold": (None if thr is None else round(max(0.0, thr - 0.06), 3)),
        "roi": roi if usable else None,
        "search": "roi" if (usable and roi and not full_frame) else "full_frame",
        "match_ms": None if not usable else round(ms, 2),
        "confirm_frames": 2,
        "pos_scenes": sorted({p.parent.name for p in pos_all}),
        "pos": pos, "neg": neg,
        "detect_rate": detect, "false_positive_rate": fp,
        "verdict": verdict, "usable": usable,
    }


def _f(v) -> str:
    return "  -  " if v is None else f"{v:.3f}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="模板标定工具（两段式：粗定位 ROI → ROI 内精确标定）")
    p.add_argument("--template", type=str, default="", help="只标定名称含此关键字的模板")
    p.add_argument("--scale", type=float, default=1.0, help="精确标定尺度（运行期必须一致，默认 1.0）")
    p.add_argument("--coarse-scale", type=float, default=0.5, help="粗搜 ROI 用的尺度（默认 0.5）")
    p.add_argument("--pos-per-scene", type=int, default=12, help="每个正样本场景最多取多少帧")
    p.add_argument("--neg-per-scene", type=int, default=8, help="每个负样本场景最多取多少帧")
    p.add_argument("--full-frame", action="store_true", help="不用 ROI，全帧评分（慢 10 倍，排查用）")
    p.add_argument("--json", type=str, default=str(TEMPLATES / "thresholds.json"), help="阈值输出 JSON")
    p.add_argument("--report", type=str, default="", help="同时输出 markdown 报告")
    p.add_argument("--all", action="store_true", help="连自检素材(example_patch/selftest_*)一起标定")
    args = p.parse_args(argv)

    if not TEMPLATES.is_dir():
        print("[!] templates/ 不存在，先裁剪模板")
        return 2
    index = all_frames()
    if not index:
        print("[!] golden_frames/ 里没有素材")
        return 2

    tpls = sorted(TEMPLATES.glob("*.png"))
    if not args.all:
        tpls = [t for t in tpls if t.stem not in SKIP_BY_DEFAULT]
    if args.template:
        tpls = [t for t in tpls if args.template.lower() in t.stem.lower()]
    if not tpls:
        print(f"[!] 没有匹配的模板（关键字 {args.template!r}）")
        return 2

    print(f"[+] 精确尺度 {args.scale} / 粗搜尺度 {args.coarse_scale}   模板 {len(tpls)} 张   "
          f"素材 {len(index)} 个场景 {sum(len(v) for v in index.values())} 帧"
          f"{'   [全帧模式]' if args.full_frame else ''}")
    coarse = FrameCache(scale=args.coarse_scale, grayscale=True)
    fine = FrameCache(scale=args.scale, grayscale=True)
    matcher = TemplateMatcher(grayscale=True, threshold=0.8)

    t0 = time.perf_counter()
    results = [calibrate_one(t, index, coarse, fine, matcher, args.pos_per_scene,
                             args.neg_per_scene, args.full_frame) for t in tpls]
    elapsed = time.perf_counter() - t0

    print("\n" + "=" * 118)
    print(f"{'模板':<20}{'尺寸':>10}{'正帧':>5}{'正min':>8}{'负帧':>6}{'负max':>8}"
          f"{'阈值':>8}{'检出':>7}{'误报':>7}{'ms':>6}  ROI / 判定")
    print("-" * 118)
    for r in results:
        roi = f"ROI{r['roi']}" if r["roi"] else ("全帧" if r["usable"] else "ROI-")
        ms = "-" if r.get("match_ms") is None else f"{r['match_ms']:.1f}"
        print(f"{r['name']:<20}{r['size'][0]:>5}x{r['size'][1]:<4}{r['pos']['n']:>5}"
              f"{_f(r['pos']['min']):>8}{r['neg']['n']:>6}{_f(r['neg']['max']):>8}"
              f"{str(r['threshold']):>8}{_f(r['detect_rate']):>7}{_f(r['false_positive_rate']):>7}"
              f"{ms:>6}  {roi}  {r['verdict']}")

    usable = [r for r in results if r["usable"]]
    print("-" * 118)
    print(f"可用 {len(usable)}/{len(results)} 张；耗时 {elapsed:.1f}s（粗搜解码 {coarse.loads} 帧，"
          f"精确解码 {fine.loads} 帧 / {fine.time:.1f}s，缓存 {coarse.memory_mb() + fine.memory_mb():.0f} MB）")

    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "scale": args.scale, "coarse_scale": args.coarse_scale,
           "matcher": {"grayscale": True, "method": "TM_CCOEFF_NORMED"},
           "frames_dir": "golden_frames", "elapsed_seconds": round(elapsed, 2),
           "templates": {r["name"]: r for r in results}}
    out_path = Path(args.json)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 已写入 {out_path}")

    if args.report:
        md = ["# 模板标定报告\n",
              f"- 生成时间：{out['generated_at']}",
              f"- 精确尺度：{args.scale}（粗搜 {args.coarse_scale}）",
              f"- 耗时：{elapsed:.1f}s",
              f"- 可用：{len(usable)}/{len(results)}\n",
              "| 模板 | 尺寸 | 正帧 | 正min | 正max | 负帧 | 负max | 阈值 | 检出率 | 误报率 | ms | ROI | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            ms = "-" if r["match_ms"] is None else f"{r['match_ms']:.1f}"
            md.append(f"| {r['name']} | {r['size'][0]}x{r['size'][1]} | {r['pos']['n']} | "
                      f"{_f(r['pos']['min'])} | {_f(r['pos']['max'])} | {r['neg']['n']} | "
                      f"{_f(r['neg']['max'])} | {r['threshold']} | {_f(r['detect_rate'])} | "
                      f"{_f(r['false_positive_rate'])} | {ms} | {r['roi']} | {r['verdict']} |")
        Path(args.report).write_text("\n".join(md) + "\n", encoding="utf-8")
        print(f"[+] 报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

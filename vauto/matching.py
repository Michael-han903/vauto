# -*- coding: utf-8 -*-
"""
vauto.matching —— 模板匹配工具层（OpenCV），只返回坐标与置信度，不做业务决策

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

核心 API
--------
TemplateMatcher(grayscale=True, threshold=0.85)
    .match(source, template, threshold=None, region=None, max_results=0,
           nms_iou=0.35, scales=(1.0,))     -> list[Match]（按 score 降序）
    .match_best(...)                        -> Match | None
find_template(...) / find_template_best(...) 模块级便捷函数
draw_matches(frame, matches)                调试图（CV 学习用，画框 + 分数）

实现的算法细节
--------------
* 方法默认 cv2.TM_CCOEFF_NORMED（对光照线性变化鲁棒，分数天然落在 [-1,1]）。
* 纯色 / 低纹理模板会让 CCOEFF 出现 NaN/Inf（分母为 0），已用 nan_to_num 兜底，
  但仍建议模板带纹理；无纹理场景请用 (method=TM_SQDIFF_NORMED, ascending=True)。
* 多尺度搜索 scales=(0.9, 1.0, 1.1)：应对窗口缩放 / DPI 变化。
* 阈值以上常有成百上千个相邻像素点，先按分数截断候选，再做贪心 NMS(IoU) 去重。
* 模板支持 alpha 通道：自动当作 mask；此时方法自动切到 TM_CCORR_NORMED
  （CCOEFF 不支持 mask）。
* 路径用 np.fromfile + cv2.imdecode 读取，兼容中文/Unicode 路径（cv2.imread 不行）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

__all__ = ["Match", "TemplateMatcher", "find_template", "find_template_best",
           "draw_matches", "load_image", "save_image"]

ImageLike = Union[str, Path, np.ndarray]
Region = Tuple[int, int, int, int]  # (x, y, w, h)


@dataclass(frozen=True)
class Match:
    """一次命中。x/y 为模板左上角在「源图坐标系」中的位置，score 越大越像。"""

    x: int
    y: int
    width: int
    height: int
    score: float

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def offset(self, dx: int, dy: int) -> "Match":
        return Match(self.x + dx, self.y + dy, self.width, self.height, self.score)

    def __str__(self) -> str:  # 便于日志
        return f"Match(cx={self.center[0]}, cy={self.center[1]}, {self.width}x{self.height}, score={self.score:.4f})"


# --------------------------------------------------------------------------- #
# 图像读取 / 预处理
# --------------------------------------------------------------------------- #
def load_image(path: Union[str, Path], flags: int = cv2.IMREAD_UNCHANGED) -> np.ndarray:
    """读图（兼容中文路径）。返回原始通道数的图像；不存在则抛 FileNotFoundError。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"图像文件不存在: {p}")
    buf = np.fromfile(str(p), dtype=np.uint8)          # 关键：绕过 cv2.imread 的 ANSI 路径限制
    img = cv2.imdecode(buf, flags)
    if img is None:
        raise ValueError(f"图像解码失败（文件损坏或格式不支持）: {p}")
    return img


def _split_alpha(img: np.ndarray) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """拆分 BGR 与 alpha mask；4 通道时 alpha 作为 mask。"""
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), None
    if img.shape[2] == 4:
        return img[:, :, :3].copy(), img[:, :, 3].copy()
    return img, None


def save_image(path: Union[str, Path], image: np.ndarray, ext: Optional[str] = None) -> Path:
    """
    保存图像（兼容中文路径）。ext 形如 ".png"；默认取 path 的扩展名。
    与 load_image 对称：cv2.imwrite 在非 ASCII 路径上会静默失败，这里用 imencode+tofile。
    """
    p = Path(path)
    suffix = ext or p.suffix or ".png"
    ok, buf = cv2.imencode(suffix, np.asarray(image))
    if not ok:
        raise ValueError(f"图像编码失败: {p} (ext={suffix})")
    p.parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(p))
    return p


def _as_bgr(img: ImageLike) -> np.ndarray:
    """把 路径 / 灰度 / BGRA 统一成连续 BGR ndarray。"""
    if isinstance(img, (str, Path)):
        img = load_image(img, flags=cv2.IMREAD_COLOR)
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"期望 HxWx3/HxWx4/HxW 图像，收到 shape={arr.shape}")
    return np.ascontiguousarray(arr)


def _iou(a: Match, b: Match) -> float:
    ax1, ay1, ax2, ay2 = a.x, a.y, a.x + a.width, a.y + a.height
    bx1, by1, bx2, by2 = b.x, b.y, b.x + b.width, b.y + b.height
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union > 0 else 0.0


def _nms(matches: List[Match], iou_threshold: float) -> List[Match]:
    """贪心非极大值抑制：分数高的先留，压掉与之重叠过多的框。"""
    kept: List[Match] = []
    for m in sorted(matches, key=lambda x: x.score, reverse=True):
        if all(_iou(m, k) <= iou_threshold for k in kept):
            kept.append(m)
    return kept


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class TemplateMatcher:
    """
    模板匹配器。线程安全（模板缓存加了锁），可复用于多条流水线。

    参数
    ----
    grayscale : bool   是否转灰度匹配（默认 True：更快，且多数 UI 场景够用）
    method    : int    cv2 匹配方法，默认 TM_CCOEFF_NORMED
    threshold : float  默认置信阈值（0~1），可在每次 match() 时覆盖
    ascending : bool   分数是否「越小越好」（TM_SQDIFF* 系列为 True）
    cache     : bool   是否按 路径+mtime 缓存已加载模板
    """

    def __init__(
        self,
        grayscale: bool = True,
        method: int = cv2.TM_CCOEFF_NORMED,
        threshold: float = 0.85,
        ascending: bool = False,
        cache: bool = True,
    ) -> None:
        self.grayscale = bool(grayscale)
        self.method = int(method)
        self.threshold = float(threshold)
        self.ascending = bool(ascending or method in (cv2.TM_SQDIFF, cv2.TM_SQDIFF_NORMED))
        self.cache = bool(cache)
        self._cache: dict = {}
        self._lock = threading.RLock()

    # -- 模板加载 ---------------------------------------------------------- #
    def _obtain_template(self, template: ImageLike) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """返回 (BGR 模板, mask 或 None)。路径入参会走缓存。"""
        if isinstance(template, (str, Path)):
            p = Path(template).resolve()
            key = (str(p), p.stat().st_mtime_ns if p.exists() else 0)
            if self.cache:
                with self._lock:
                    hit = self._cache.get(key)
                if hit is not None:
                    return hit
            bgr, mask = _split_alpha(load_image(p))
            bgr = _as_bgr(bgr)
            if self.cache:
                with self._lock:
                    self._cache[key] = (bgr, mask)
            return bgr, mask

        bgr, mask = _split_alpha(np.asarray(template))
        return _as_bgr(bgr), mask

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- 匹配 -------------------------------------------------------------- #
    def match(
        self,
        source: ImageLike,
        template: ImageLike,
        threshold: Optional[float] = None,
        region: Optional[Region] = None,
        max_results: int = 0,
        nms_iou: float = 0.35,
        scales: Sequence[float] = (1.0,),
        max_candidates: int = 2000,
    ) -> List[Match]:
        """
        在 source 中查找 template。

        参数
        ----
        source       : BGR ndarray（通常是 WindowCapture.grab() 的帧），或图片路径
        template     : 模板图片路径 或 ndarray（带 alpha 时自动作为 mask）
        threshold    : 置信阈值，None 用实例默认值
        region       : (x, y, w, h)，只在源图某块区域内搜索，可显著提速
        max_results  : >0 时只返回分数最高的前 N 个（NMS 之后截断）
        nms_iou      : 重叠 IoU 超过该值的候选会被抑制（0 = 不去重）
        scales       : 多尺度列表，如 (0.9, 0.95, 1.0, 1.05, 1.1)
        max_candidates: 阈值内的候选点上限（防止平坦区域爆量），按分数截断

        返回
        ----
        list[Match]，按 score 降序；无命中返回 []。
        坐标位于「source 全图坐标系」（即使传了 region 也已加回偏移）。
        """
        src = _as_bgr(source)
        tpl, mask = self._obtain_template(template)
        thr = self.threshold if threshold is None else float(threshold)

        off_x, off_y = 0, 0
        if region is not None:
            rx, ry, rw, rh = (int(v) for v in region)
            rx = max(0, min(rx, src.shape[1] - 1))
            ry = max(0, min(ry, src.shape[0] - 1))
            rw = max(1, min(rw, src.shape[1] - rx))
            rh = max(1, min(rh, src.shape[0] - ry))
            src = src[ry:ry + rh, rx:rx + rw]
            off_x, off_y = rx, ry

        src_proc = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY) if self.grayscale else src
        base_mask = mask

        # 退化模板守卫：CCOEFF 族用「减均值」算相关性，纯色模板方差为 0 -> 0/0 = NaN，
        # OpenCV 会返回无意义的常量图（实测在整幅图上给出高分），必须显式拒绝。
        if base_mask is None and self.method in (cv2.TM_CCOEFF, cv2.TM_CCOEFF_NORMED):
            probe = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if self.grayscale else tpl
            if float(np.asarray(probe, dtype=np.float64).std()) < 1e-6:
                raise ValueError(
                    "模板为纯色（方差≈0），TM_CCOEFF* 无法给出有意义的相似度。"
                    "请改用有纹理的模板，或换用 TemplateMatcher(method=cv2.TM_SQDIFF_NORMED, ascending=True)"
                    "／带 alpha 通道的模板（自动当作 mask）。"
                )

        candidates: List[Match] = []
        for scale in scales:
            scale = float(scale)
            if scale <= 0:
                continue
            if abs(scale - 1.0) < 1e-6:
                tpl_proc, tpl_mask = tpl, base_mask
            else:
                new_w = max(1, int(round(tpl.shape[1] * scale)))
                new_h = max(1, int(round(tpl.shape[0] * scale)))
                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                tpl_scaled = cv2.resize(tpl, (new_w, new_h), interpolation=interp)
                tpl_proc, tpl_mask = tpl_scaled, base_mask
                if base_mask is not None:
                    tpl_mask = cv2.resize(base_mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

            if self.grayscale and tpl_proc.ndim == 3:
                tpl_proc = cv2.cvtColor(tpl_proc, cv2.COLOR_BGR2GRAY)

            th, tw = tpl_proc.shape[:2]
            # 模板必须严格小于源图，否则 matchTemplate 抛错
            if th > src_proc.shape[0] or tw > src_proc.shape[1]:
                continue

            kwargs = {}
            method = self.method
            if tpl_mask is not None:
                # CCOEFF 不支持 mask，退化为 CCORR_NORMED（分数仍为越大越好）
                if method in (cv2.TM_CCOEFF, cv2.TM_CCOEFF_NORMED):
                    method = cv2.TM_CCORR_NORMED
                kwargs["mask"] = tpl_mask.astype(np.uint8)

            try:
                result = cv2.matchTemplate(src_proc, tpl_proc, method, **kwargs)
            except cv2.error:
                continue

            result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0).astype(np.float32, copy=False)

            if self.ascending:
                hit_ys, hit_xs = np.where(result <= thr)
                scores = result[hit_ys, hit_xs]
                order = np.argsort(scores)                 # 越小越好
            else:
                hit_ys, hit_xs = np.where(result >= thr)
                scores = result[hit_ys, hit_xs]
                order = np.argsort(-scores)                # 越大越好

            if order.size > max_candidates:
                order = order[:max_candidates]

            for i in order:
                candidates.append(Match(
                    int(hit_xs[i]) + off_x,
                    int(hit_ys[i]) + off_y,
                    int(tw),
                    int(th),
                    float(scores[i]),
                ))

        if not candidates:
            return []

        matches = _nms(candidates, nms_iou) if nms_iou > 0 else candidates
        if max_results and max_results > 0:
            matches = matches[:max_results]
        return matches

    def match_best(self, source: ImageLike, template: ImageLike, **kwargs) -> Optional[Match]:
        """只取最佳匹配；无命中返回 None。kwargs 透传给 match()。"""
        kwargs.setdefault("max_results", 1)
        hits = self.match(source, template, **kwargs)
        return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# 模块级便捷函数（一次性使用，不复用缓存）
# --------------------------------------------------------------------------- #
def find_template(
    source: ImageLike,
    template_path: ImageLike,
    threshold: float = 0.85,
    grayscale: bool = True,
    **kwargs,
) -> List[Match]:
    """一次性模板匹配，返回全部命中（按 score 降序）。"""
    matcher = TemplateMatcher(grayscale=grayscale, threshold=threshold, cache=False)
    return matcher.match(source, template_path, **kwargs)


def find_template_best(
    source: ImageLike,
    template_path: ImageLike,
    threshold: float = 0.85,
    grayscale: bool = True,
    **kwargs,
) -> Optional[Match]:
    """一次性模板匹配，只返回最佳命中或无 None。"""
    matcher = TemplateMatcher(grayscale=grayscale, threshold=threshold, cache=False)
    return matcher.match_best(source, template_path, **kwargs)


def draw_matches(
    frame: np.ndarray,
    matches: Iterable[Match],
    color: Tuple[int, int, int] = (0, 200, 0),
    thickness: int = 2,
    show_score: bool = True,
    copy: bool = True,
) -> np.ndarray:
    """
    调试图：把命中框画出来并标注分数（BGR 顺序）。
    仅用于算法调试/学习，不属于业务逻辑。
    """
    out = frame.copy() if copy else frame
    for m in matches:
        cv2.rectangle(out, (m.x, m.y), (m.x + m.width, m.y + m.height), color, thickness)
        if show_score:
            cv2.putText(
                out,
                f"{m.score:.3f}",
                (m.x, max(12, m.y - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                1,
                cv2.LINE_AA,
            )
    return out

# -*- coding: utf-8 -*-
"""
vauto.vision —— 识别层组合件：ROI + 降采样 + 滞回去抖 + 画面变化检测

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。本模块不做任何业务判断（点哪个、何时点、
点几次），只提供通用能力：「在指定区域、指定缩放尺度上稳定地判定某元素是否出现」。

为什么需要它（本机实测数据，客户区 3840x2160）
----------------------------------------------
* 全帧灰度模板匹配一次 340~670 ms；0.5 缩放（1920x1080）约 100 ms；再叠加 ROI 可低到
  10 ms 量级 —— 提示条那类固定位置元素只在几百像素的小区域里搜就够了。
* 0.5 缩放后匹配中心相对全分辨率的偏差 <= 2 px（实测），足够点击。
* 阈值不能跨尺度复用：同一模板 1.0 尺度得 1.000、0.5 尺度得 0.879。
  所以 threshold 与 scale 必须成对使用（calibrate.py 按尺度输出阈值）。
* 单帧判定会被动画/抖动干扰，需要滞回：连续 confirm_frames 帧命中才算"出现"，
  连续 release_frames 帧未命中才算"消失"。
* 「画面是否变化」用差分哈希（dHash）很便宜：1080p 灰度帧约 2 ms、4K 彩帧约 15 ms
  （耗时几乎全在缩小到 64x64，比较本身只要 0.03 ms），仍比一次模板匹配便宜 1~2 个数量级，
  适合做"等画面稳定""是否卡死"这类高频判定。

坐标约定
--------
* 对外（roi / Match / 返回值）一律是「全分辨率帧坐标」。
* scale 只在内部生效，调用方不用换算。

用法
----
    det = VisualDetector(matcher, "templates/hint_unlock_all.png",
                         roi=(700, 1986, 242, 53), scale=0.5,
                         threshold=0.70, confirm_frames=2, name="本页还有可解锁")
    hit = det.observe(frame)        # 连续 2 帧命中后才返回 Match，消失要连续 1 帧
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Sequence, Tuple

import numpy as np

from .matching import Match, TemplateMatcher

try:
    import cv2
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖：opencv-python。请执行  pip install opencv-python") from exc

__all__ = [
    "scaled_frame",
    "to_full_point",
    "to_frame_point",
    "VisualDetector",
    "frame_signature",
    "signature_distance",
    "mean_abs_diff",
    "wait_stable",
]

Region = Tuple[int, int, int, int]      # (x, y, w, h)，全分辨率帧坐标


# --------------------------------------------------------------------------- #
# 缩放与坐标换算
# --------------------------------------------------------------------------- #
def scaled_frame(frame: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """按 scale 缩小一帧（scale>=1.0 时原样返回）。用 INTER_AREA，缩小时最不易产生噪声。"""
    if scale is None or abs(float(scale) - 1.0) < 1e-6:
        return frame
    k = float(scale)
    if k <= 0:
        raise ValueError(f"scale 必须为正数，收到 {scale!r}")
    h, w = frame.shape[:2]
    return cv2.resize(frame, (max(1, int(round(w * k))), max(1, int(round(h * k)))),
                      interpolation=cv2.INTER_AREA)


def _scale_region(roi: Region, scale: float) -> Region:
    x, y, w, h = (int(v) for v in roi)
    k = float(scale)
    return (int(round(x * k)), int(round(y * k)), max(1, int(round(w * k))), max(1, int(round(h * k))))


def to_full_point(point: Tuple[float, float], scale: float) -> Tuple[int, int]:
    """缩放帧坐标 -> 全分辨率帧坐标。"""
    k = float(scale)
    return int(round(point[0] / k)), int(round(point[1] / k))


def to_frame_point(point: Tuple[float, float], scale: float) -> Tuple[int, int]:
    """全分辨率帧坐标 -> 缩放帧坐标。"""
    k = float(scale)
    return int(round(point[0] * k)), int(round(point[1] * k))


def _match_to_full(m: Match, scale: float) -> Match:
    """把缩放帧坐标系里的命中还原成全分辨率坐标。"""
    k = float(scale)
    return Match(int(round(m.x / k)), int(round(m.y / k)),
                 int(round(m.width / k)), int(round(m.height / k)), m.score)


# --------------------------------------------------------------------------- #
# 带滞回的检测器
# --------------------------------------------------------------------------- #
class VisualDetector:
    """
    单元素检测器：ROI + 缩放 + 阈值滞回 + 帧数去抖。

    参数
    ----
    matcher           : TemplateMatcher（可多元素共用一个实例，模板有缓存）
    template          : 模板路径或 ndarray
    roi               : (x, y, w, h) 全分辨率帧坐标；None 表示全帧搜索（贵，不推荐）
    scale             : 工作尺度。帧与模板会同时按该尺度缩放（= 在缩放后的画面上用缩放后的模板搜索）。
                        越小越快，但分数会下降，**阈值必须按同一尺度重新标定**（calibrate.py 的 --scale）。
                        全帧找不固定位置的东西才需要它；固定位置元素优先用 roi，保真且更快。
    threshold         : 进入阈值（默认取 matcher.threshold）
    release_threshold : 退出阈值；None 时 = threshold - 0.06（滞回，避免分数在阈值附近抖动）
    confirm_frames    : 连续命中多少帧才算"出现"（默认 2，抑制单帧误判）
    release_frames    : 连续未命中多少帧才算"消失"（默认 1）
    scales            : 多尺度列表（窗口尺寸变化时用），默认 (1.0,)
    name              : 日志用名字

    线程安全：observe() 内部加锁，可被多个线程调用（不建议）。
    """

    def __init__(
        self,
        matcher: TemplateMatcher,
        template,
        roi: Optional[Region] = None,
        scale: float = 1.0,
        threshold: Optional[float] = None,
        release_threshold: Optional[float] = None,
        confirm_frames: int = 2,
        release_frames: int = 1,
        scales: Sequence[float] = (1.0,),
        nms_iou: float = 0.35,
        max_results: int = 1,
        name: str = "",
        min_std: float = 0.0,
    ) -> None:
        self.matcher = matcher
        self.template = template
        self.roi = None if roi is None else (int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3]))
        self.scale = float(scale)
        self.threshold = float(matcher.threshold if threshold is None else threshold)
        self.release_threshold = (self.threshold - 0.06
                                  if release_threshold is None else float(release_threshold))
        self.confirm_frames = max(1, int(confirm_frames))
        self.release_frames = max(1, int(release_frames))
        self.scales = tuple(scales)
        self.nms_iou = float(nms_iou)
        self.max_results = int(max_results)
        self.name = name or "detector"
        # 【2026-10-04 新增】ROI 灰度标准差下限：低于它就判"这里根本没有可辨认的内容"，
        # 直接返回"没看到"。起因是实测到 `panel_search_title` 在**纯色/空白**画面上
        # 也能打 0.909（阈值 0.696）—— TM_CCOEFF_NORMED 对低反差区域会给虚高相关分，
        # 而黑屏 / 加载中 / 抓错窗口抓到的空桌面正好都是这种画面。
        # 0 = 关（默认；不改变任何既有判据的行为），按需在标定表里给某个判据打开。
        self.min_std = float(min_std or 0.0)

        self._lock = threading.RLock()
        self._hit_streak = 0
        self._miss_streak = 0
        self._confirmed = False
        self._match: Optional[Match] = None
        self.last_score: float = float("nan")
        self.observations = 0

    # -- 状态 -------------------------------------------------------------- #
    @property
    def confirmed(self) -> bool:
        """是否处于"已确认出现"状态（滞回后的稳定结论）。"""
        with self._lock:
            return self._confirmed

    @property
    def match(self) -> Optional[Match]:
        """最近一次确认时的命中（全分辨率坐标）；未确认返回 None。"""
        with self._lock:
            return self._match

    @property
    def hit_streak(self) -> int:
        with self._lock:
            return self._hit_streak

    def reset(self) -> None:
        with self._lock:
            self._hit_streak = self._miss_streak = 0
            self._confirmed = False
            self._match = None

    # -- 检测 -------------------------------------------------------------- #
    def observe(self, frame: np.ndarray) -> Optional[Match]:
        """
        喂入一帧（全分辨率），返回：确认出现时 -> Match（全分辨率坐标），否则 None。

        * 只有"连续 confirm_frames 帧命中"之后才返回非 None；
        * 一旦确认，后续帧即使短暂丢失（<= release_frames）仍返回上一次的 Match；
        * 确认期间命中分数掉到 release_threshold 以下会立即释放（滞回抗抖动）。
        """
        with self._lock:
            self.observations += 1
            hit = self._detect_once(frame)
            self.last_score = hit.score if hit is not None else float("nan")

            if hit is not None:
                self._hit_streak += 1
                self._miss_streak = 0
                if self._hit_streak >= self.confirm_frames:
                    self._confirmed = True
                    self._match = hit
            else:
                self._hit_streak = 0
                self._miss_streak += 1
                if self._confirmed and self._miss_streak >= self.release_frames:
                    self._confirmed = False
                    self._match = None

            return self._match if self._confirmed else None

    def probe(self, frame: np.ndarray):
        """
        单帧打分，**不看阈值、不动去抖状态** —— 给 `run_vauto.py --probe`（人工翻页面时复核判据）
        和阈值复核用。返回 (最佳分数, 最佳命中)；区域不可比时分数为 nan。

        分数语义统一成"越大越像"（SQDIFF 系列会取 1-s）。
        """
        if frame is None or frame.ndim < 2:
            return float("nan"), None
        if self._roi_std(frame) < self.min_std:
            return float("nan"), None
        src = scaled_frame(frame, self.scale)
        eff_scales = tuple(float(s) * self.scale for s in self.scales)
        region = None if self.roi is None else _scale_region(self.roi, self.scale)
        hit = self.matcher.match_best(src, self.template, threshold=-1.0, region=region,
                                      scales=eff_scales, nms_iou=self.nms_iou,
                                      max_results=self.max_results)
        if hit is None:
            return float("nan"), None
        score = float(getattr(hit, "score", float("nan")))
        if self.matcher.ascending:
            score = 1.0 - score
        return score, _match_to_full(hit, self.scale)

    def _roi_std(self, frame: np.ndarray) -> float:
        """ROI 的灰度标准差（min_std<=0 时直接返回一个大值 = 不做这项检查）。

        为什么用标准差：TM_CCOEFF_NORMED 在**低反差**区域会给出虚高的相关分
        （实测 panel_search_title 在纯灰帧上 0.909），"这里有没有内容"比"像不像"
        更基础 —— 空白区域不可能真的长着我们要找的界面元素。
        """
        if self.min_std <= 0 or frame is None or frame.ndim < 2:
            return 1e9
        if self.roi is not None:
            x, y, w, h = (int(v) for v in self.roi)
            sub = frame[max(0, y):y + h, max(0, x):x + w]
        else:
            sub = frame
        if sub.size == 0:
            return 0.0
        gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY) if sub.ndim == 3 else sub
        return float(np.asarray(gray, dtype=np.float64).std())

    def _detect_once(self, frame: np.ndarray) -> Optional[Match]:
        if frame is None or frame.ndim < 2:
            return None
        if self._roi_std(frame) < self.min_std:
            return None
        src = scaled_frame(frame, self.scale)
        # 关键：模板必须与帧按同一尺度缩放，否则等于拿大模板去找小画面（实测分数会从 1.000 崩到 0.34）。
        # 这里把 scale 乘进 scales，交给 matcher 内部缩放模板 —— 语义 = 「在缩放后的画面上用缩放后的模板搜索」。
        eff_scales = tuple(float(s) * self.scale for s in self.scales)
        region = None if self.roi is None else _scale_region(self.roi, self.scale)
        # 已确认时用更宽松的退出阈值，未确认时用进入阈值（滞回）
        thr = self.release_threshold if self._confirmed else self.threshold
        hit = self.matcher.match_best(src, self.template, threshold=thr, region=region,
                                     scales=eff_scales, nms_iou=self.nms_iou,
                                     max_results=self.max_results)
        return None if hit is None else _match_to_full(hit, self.scale)

    def __repr__(self) -> str:
        return (f"VisualDetector({self.name!r}, scale={self.scale}, "
                f"thr={self.threshold:.3f}/{self.release_threshold:.3f}, "
                f"confirm={self.confirm_frames}, roi={self.roi}, confirmed={self.confirmed})")


# --------------------------------------------------------------------------- #
# 画面变化检测（比模板匹配便宜 4 个数量级）
# --------------------------------------------------------------------------- #
def frame_signature(frame: np.ndarray, size: int = 64) -> np.ndarray:
    """
    差分哈希（dHash）位图：相邻像素亮度比较，返回 (size-1, size) 的 bool 数组。
    对整体亮度变化不敏感、对结构变化敏感，适合判断"画面变了吗"。
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    small = cv2.resize(gray, (int(size), int(size)), interpolation=cv2.INTER_AREA)
    return small[:, 1:] > small[:, :-1]


def signature_distance(a: np.ndarray, b: np.ndarray) -> int:
    """两个 dHash 位图之间的汉明距离（0 = 结构完全相同；>8 一般就算变了）。"""
    return int(np.count_nonzero(np.asarray(a) != np.asarray(b)))


def mean_abs_diff(a: np.ndarray, b: np.ndarray, gray: bool = True) -> float:
    """
    两帧的灰度平均绝对差（0~255）。比 dHash 更细，1920x1080 灰度上约 3 ms。
    形状不一致时先缩放到相同尺寸再比。
    """
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY) if (gray and a.ndim == 3) else a
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY) if (gray and b.ndim == 3) else b
    return float(np.mean(cv2.absdiff(ga, gb)))


def block_max_abs_diff(a: np.ndarray, b: np.ndarray, blocks=(8, 6), gray: bool = True) -> float:
    """
    分块最大平均绝对差（0~255）—— 「画面里**局部**变了没有」的判据。

    为什么需要它：一整屏 3840×1450 的区域里只挪动一个高亮框（约 436×436 的边框），
    全局平均差只有 0.6 左右，任何合理阈值都判不出来；但按 8×6 分块后，被影响的那块
    平均差能到 20~60 → 阈值可以定得很干净。等价于"取变化最剧烈的那个区块"。

    实现：absdiff 后用 INTER_AREA 缩到 (bw, bh)，每个像素就是那一块的平均值（比逐块切片快）。
    """
    bw, bh = int(blocks[0]), int(blocks[1])
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY) if (gray and a.ndim == 3) else a
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY) if (gray and b.ndim == 3) else b
    diff = cv2.absdiff(ga, gb)
    if diff.shape[0] < bh or diff.shape[1] < bw:
        return float(np.mean(diff))
    return float(np.max(cv2.resize(diff, (bw, bh), interpolation=cv2.INTER_AREA)))


def wait_active(capture,
                stop_event=None,
                timeout: float = 120.0,
                need_streak: int = 3,
                threshold: float = 6.0,
                blocks=(8, 6),
                poll: float = 0.4,
                scale: float = 0.5):
    """
    等「画面真的动起来」—— 与 wait_stable 相反的那个闸门。

    为什么需要：加载画面（HORIZON FESTIVAL 那种过场）**是静止的**，所以 wait_stable
    会把它当成"已加载完成"提前返回（2026-10-02 实测：进赛事后 5 秒就"稳定"了，
    其实还在过场）。而挑战一旦开始，HUD（计时/名次）就在持续变化。
    所以"连续 need_streak 次分块最大差 >= threshold"才是"比赛开始"的可靠信号。

    返回 True = 检测到持续变化；False = 超时仍静止（可能卡在加载或某个等待输入的界面）。
    """
    t0 = time.monotonic()
    prev = None
    streak = 0
    while time.monotonic() - t0 < timeout:
        if stop_event is not None and stop_event.is_set():
            return False
        frame = capture.grab()
        small = scaled_frame(frame, scale) if scale != 1.0 else frame
        if prev is not None:
            if block_max_abs_diff(prev, small, blocks=blocks) >= threshold:
                streak += 1
                if streak >= need_streak:
                    return True
            else:
                streak = 0
        prev = small
        time.sleep(max(0.05, poll))
    return False


def wait_stable(
    capture,
    stop_event=None,
    timeout: Optional[float] = None,
    settle: float = 0.25,
    diff_threshold: float = 1.5,
    scale: float = 0.5,
    roi: Optional[Region] = None,
    poll: float = 0.12,
) -> bool:
    """
    等到画面"稳定"（画面连续 settle 秒没有明显变化）或超时。

    用途：截屏判定的前置守卫 —— 动画/转场/加载期间不要做判定。
    变化判据：与"上一次画面发生变化时的帧"的灰度平均绝对差 < diff_threshold。
    capture 需提供 .grab() -> BGR ndarray（如 WindowCapture）；可被急停打断。
    返回 True=已稳定，False=超时 / 被急停打断 / 抓帧失败。
    """
    from .timing import interruptible_sleep

    t_begin = time.monotonic()          # 总超时用（不随画面变化重置）
    t_change = t_begin                  # 画面最后一次变化的时间（稳定计时用）
    prev = None
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if timeout is not None and (time.monotonic() - t_begin) >= timeout:
            return False
        try:
            frame = capture.grab(region=roi) if roi is not None else capture.grab()
        except Exception:
            return False
        if scale != 1.0:
            frame = scaled_frame(frame, scale)
        if prev is not None and mean_abs_diff(prev, frame) < diff_threshold:
            if time.monotonic() - t_change >= settle:
                return True
        else:
            prev = frame
            t_change = time.monotonic()          # 画面刚变过，稳定计时重新开始
        interruptible_sleep(poll, stop_event)

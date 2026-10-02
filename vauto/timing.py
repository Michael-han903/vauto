# -*- coding: utf-8 -*-
"""
vauto.timing —— 人类化时序与轨迹（纯函数 + 随机策略），不含任何输入副作用

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

提供两层能力
------------
1) 纯函数（可单测、无副作用）：
   bezier_path()   三次贝塞尔轨迹采样，强制非直线（控制点带垂直方向随机偏移）
   ease_curve()    非线性时间轴（随机 ease-in / ease-out + 微颤 + 单调化）
   interruptible_sleep()  可被急停事件立即打断的 sleep
2) Humanizer：带随机源与配置档的策略对象，供 InputSimulator 使用：
   .delay(min, max)          随机延时并 sleep，返回实际秒数
   .jitter_point(x, y)       像素级随机偏移的落点
   .maybe_rest(n)            每 N 次循环按概率触发短暂休息
   .sample(lo, hi) / .move_duration() / .key_hold() ...

设计要点
--------
* 「拒绝匀速直线」由两件事共同保证：
  - 空间上：控制点垂直偏移 |offset| >= max(1.5px, 1.5% * 距离)，
            且控制点沿路径位置随机（0.15~0.40 / 0.60~0.85），曲线形状每次都不同；
  - 时间上：t 轴经随机幂次变换 + 逐段 ±12% 抖动，速度自然呈「慢-快-慢」而非匀速。
* 全部随机走 self.rng（random.Random），传 seed 即可完全复现，方便做实验对比。
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "TimingProfile",
    "Humanizer",
    "bezier_path",
    "ease_curve",
    "interruptible_sleep",
]

Point = Tuple[int, int]
FloatPair = Tuple[float, float]


@dataclass
class TimingProfile:
    """一行参数定义「一个人的手速习惯」。所有区间为 (最小值, 最大值) 秒 / 像素。"""

    # 通用延时
    delay: FloatPair = (0.05, 0.20)            # 动作之间的随机间隔
    # 鼠标移动
    move_duration: FloatPair = (0.18, 0.50)    # 一次移动耗时
    move_steps_per_100px: float = 30.0         # 轨迹采样密度（步/百像素）
    move_min_steps: int = 12
    move_max_steps: int = 240
    curvature: float = 0.05                    # 控制点垂直偏移比例（相对距离）
    curvature_max_px: float = 36.0             # 偏移上限，防止远距离画大弧
    tremor_px: float = 0.35                    # 轨迹微颤（像素）
    timeline_tremor: float = 0.015             # 时间轴微颤（0~1 归一化）
    overshoot_prob: float = 0.25               # 冲过目标再回拉的概率
    overshoot_px: FloatPair = (4.0, 14.0)
    # 点击
    click_offset_px: int = 3                   # 落点随机偏移半径
    click_hold: FloatPair = (0.04, 0.11)       # 左键按下时长
    double_click_gap: FloatPair = (0.05, 0.14)
    # 键盘
    key_hold: FloatPair = (0.05, 0.14)         # 单键按下时长
    key_gap: FloatPair = (0.02, 0.08)          # 组合键之间间隔
    type_interval: FloatPair = (0.03, 0.10)    # 逐字输入间隔
    # 循环休息
    rest_every_n: int = 25                     # 每 N 次循环检查一次
    rest_prob: float = 0.5                     # 触发概率
    rest_pause: FloatPair = (0.6, 2.2)         # 休息时长
    # 其他
    press_settle: FloatPair = (0.02, 0.06)     # 鼠标按下前的停顿


class Humanizer:
    """
    随机策略对象。线程安全（内部锁保护 rng）。

    用法：
        h = Humanizer(seed=42)                     # seed=None 则真随机
        h.delay(0.1, 0.3)                          # 随机 sleep
        x, y = h.jitter_point(500, 300, radius=4)  # 带像素抖动
        h.maybe_rest(i)                            # 每 N 次循环随机休息
    """

    def __init__(self, profile: Optional[TimingProfile] = None, seed: Optional[int] = None) -> None:
        self.profile = profile or TimingProfile()
        self.rng = random.Random(seed)
        self._lock = threading.RLock()
        self._rest_counter = 0

    # -- 基础随机原语 ------------------------------------------------------ #
    def uniform(self, lo: float, hi: float) -> float:
        if hi < lo:
            lo, hi = hi, lo
        with self._lock:
            return self.rng.uniform(lo, hi)

    def sample(self, pair: FloatPair) -> float:
        """从 (min, max) 区间采样。"""
        return self.uniform(pair[0], pair[1])

    def chance(self, p: float) -> bool:
        with self._lock:
            return self.rng.random() < p

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        with self._lock:
            return self.rng.gauss(mu, sigma)

    def randint(self, lo: int, hi: int) -> int:
        if hi < lo:
            lo, hi = hi, lo
        with self._lock:
            return self.rng.randint(int(lo), int(hi))

    # -- 需求 a：随机延时 -------------------------------------------------- #
    def delay(self, min_seconds: Optional[float] = None, max_seconds: Optional[float] = None,
              stop_event: Optional[threading.Event] = None) -> float:
        """
        随机 sleep：接收最小/最大秒数，返回本次实际睡眠时间。
        传 stop_event 时睡眠可被 F1 急停立刻打断。
        """
        lo = self.profile.delay[0] if min_seconds is None else float(min_seconds)
        hi = self.profile.delay[1] if max_seconds is None else float(max_seconds)
        seconds = self.sample((lo, hi))
        interruptible_sleep(seconds, stop_event)
        return seconds

    # -- 需求 b：像素随机偏移 --------------------------------------------- #
    def jitter_point(self, x: int, y: int, radius: Optional[int] = None) -> Point:
        """
        在 (x, y) 附近取一个随机落点（圆内均匀分布，非方形均匀，
        避免出现明显「方形散布」的特征）。radius=None 用 profile.click_offset_px。
        """
        r = self.profile.click_offset_px if radius is None else int(radius)
        if r <= 0:
            return int(x), int(y)
        with self._lock:
            # 圆内均匀采样
            theta = self.rng.uniform(0.0, 2.0 * math.pi)
            rad = r * math.sqrt(self.rng.random())
        return int(round(x + rad * math.cos(theta))), int(round(y + rad * math.sin(theta)))

    # -- 需求 e：循环间隙随机休息 ----------------------------------------- #
    def maybe_rest(self, iteration: int, every_n: Optional[int] = None,
                   probability: Optional[float] = None,
                   stop_event: Optional[threading.Event] = None) -> float:
        """
        每 N 次循环（默认 profile.rest_every_n）按概率触发一次短暂暂停。
        iteration 从 0 开始计；返回实际休息秒数（未触发为 0.0）。
        """
        n = self.profile.rest_every_n if every_n is None else int(every_n)
        p = self.profile.rest_prob if probability is None else float(probability)
        if n <= 0 or (iteration + 1) % n != 0:
            return 0.0
        if not self.chance(p):
            return 0.0
        seconds = self.sample(self.profile.rest_pause)
        interruptible_sleep(seconds, stop_event)
        return seconds

    # -- 各类时长采样（配合 profile 使用） -------------------------------- #
    def move_duration(self) -> float:
        return self.sample(self.profile.move_duration)

    def key_hold(self) -> float:
        return self.sample(self.profile.key_hold)

    def click_hold(self) -> float:
        return self.sample(self.profile.click_hold)


# --------------------------------------------------------------------------- #
# 纯函数：时间轴与轨迹
# --------------------------------------------------------------------------- #
def ease_curve(
    steps: int,
    rng: Optional[random.Random] = None,
    ease: FloatPair = (1.3, 2.6),
    tremor: float = 0.015,
) -> List[float]:
    """
    生成 steps 个单调不减、范围 [0, 1] 的归一化时间参数。

    做法：
      1) 随机选 ease-in 或 ease-out，用幂次 t**e / 1-(1-t)**e 拉伸时间轴；
      2) 叠加高斯微颤，模拟手部不稳定的速度波动；
      3) clip + 累积取最大（cummax）保证严格单调，端点强制为 0 与 1。
    返回长度 == steps 的 list[float]。
    """
    steps = max(2, int(steps))
    rng = rng or random.Random()
    ts = np.linspace(0.0, 1.0, steps)
    e = rng.uniform(ease[0], ease[1])
    if rng.random() < 0.5:
        ts = ts ** e                       # 起步慢、后段快
    else:
        ts = 1.0 - (1.0 - ts) ** e         # 起步快、收尾慢
    if tremor > 0:
        ts = np.array([t + rng.gauss(0.0, tremor) for t in ts], dtype=np.float64)
    ts = np.clip(ts, 0.0, 1.0)
    np.maximum.accumulate(ts, out=ts)       # 保证单调不减
    ts[0], ts[-1] = 0.0, 1.0
    return ts.tolist()


def bezier_path(
    start: Point,
    end: Point,
    steps: int,
    rng: Optional[random.Random] = None,
    curvature: float = 0.05,
    curvature_max_px: float = 36.0,
    tremor_px: float = 0.35,
) -> List[Point]:
    """
    三次贝塞尔轨迹采样（返回整数屏幕点列表）。

    「拒绝匀速直线」的空间保证：
      * 两个控制点分别位于路径 15%~40% 与 60%~85% 处（位置随机）；
      * 沿法向偏移量 = 随机符号 * uniform(0.35, 1.0) * max_off，
        其中 max_off = min(curvature * 距离, curvature_max_px)，
        且强制 >= max(1.5px, 1.5% 距离) —— 短距离也绝不成一条直线。

    端点精确落在 start / end，中间点叠加像素级高斯微颤。
    距离 < 3px 时直接返回两点（此时人手臂也不会做曲线）。
    """
    rng = rng or random.Random()
    steps = max(2, int(steps))
    p0 = np.asarray(start, dtype=np.float64)
    p1 = np.asarray(end, dtype=np.float64)
    d = p1 - p0
    dist = float(np.hypot(d[0], d[1]))

    if dist < 3.0:
        mid = int(steps // 2)
        pts = [ (int(round(p0[0])), int(round(p0[1]))) for _ in range(mid) ]
        pts += [ (int(round(p1[0])), int(round(p1[1]))) for _ in range(steps - mid) ]
        return pts

    u = d / dist
    normal = np.array([-u[1], u[0]])

    max_off = min(curvature * dist, curvature_max_px)
    min_off = max(1.5, 0.015 * dist)                 # 非直线的强制下限
    if max_off < min_off:
        max_off = min_off

    def _control(lo: float, hi: float) -> np.ndarray:
        along = p0 + d * rng.uniform(lo, hi)
        sign = 1.0 if rng.random() < 0.5 else -1.0
        off = sign * rng.uniform(0.35 * max_off, max_off)
        return along + normal * off

    c1 = _control(0.15, 0.40)
    c2 = _control(0.60, 0.85)

    ts = np.asarray(ease_curve(steps, rng, ease=(1.0, 1.0), tremor=0.0), dtype=np.float64).reshape(-1, 1)
    omt = 1.0 - ts
    curve = (omt ** 3) * p0 + 3.0 * (omt ** 2) * ts * c1 + 3.0 * omt * (ts ** 2) * c2 + (ts ** 3) * p1

    if tremor_px > 0:
        curve = curve + np.array([[rng.gauss(0.0, tremor_px), rng.gauss(0.0, tremor_px)] for _ in range(steps)])

    curve[0] = p0
    curve[-1] = p1
    return [(int(round(px)), int(round(py))) for px, py in curve]


def distribute_duration(timeline: Sequence[float], duration: float,
                        rng: Optional[random.Random] = None, jitter: float = 0.12) -> List[float]:
    """
    把总时长按非线性时间轴分配到每一步，并叠加逐段 ±jitter 抖动后归一化。
    返回长度 = len(timeline) - 1 的每步睡眠时间列表。
    """
    rng = rng or random.Random()
    steps = len(timeline) - 1
    if steps <= 0:
        return []
    span = timeline[-1] - timeline[0]
    if span <= 0:
        return [duration / steps] * steps
    raw = [(timeline[i + 1] - timeline[i]) / span * duration for i in range(steps)]
    noisy = [max(1e-4, r * (1.0 + rng.uniform(-jitter, jitter))) for r in raw]
    total = sum(noisy)
    if total <= 0:
        return [duration / steps] * steps
    return [n * duration / total for n in noisy]


# --------------------------------------------------------------------------- #
# 可中断睡眠
# --------------------------------------------------------------------------- #
def interruptible_sleep(
    seconds: float,
    stop_event: Optional[threading.Event] = None,
    chunk: float = 0.02,
) -> float:
    """
    睡眠 seconds 秒；若 stop_event 被 set（F1 急停）则立刻返回。
    返回实际睡眠时间。stop_event=None 时等价于 time.sleep。
    """
    if seconds <= 0:
        return 0.0
    if stop_event is None:
        time.sleep(seconds)
        return seconds

    start = time.monotonic()
    end = start + seconds
    while True:
        if stop_event.is_set():
            return time.monotonic() - start
        remaining = end - time.monotonic()
        if remaining <= 0:
            return seconds
        time.sleep(min(chunk, remaining))

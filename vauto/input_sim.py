# -*- coding: utf-8 -*-
"""
vauto.input_sim —— 人类输入仿真工具集（鼠标 / 键盘），基于 pynput

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。本模块只做「模拟输入」，
不读内存、不注入进程、不伪造设备 ID。

需求映射
--------
a) 随机延时            -> Humanizer.delay()
b) 带像素随机偏移的点击 -> InputSimulator.click()          （落点抖动 + 抖动时长）
c) 非线性贝塞尔移动     -> InputSimulator.move_bezier()     （拒绝匀速直线）
d) 按键按下/释放 + 抖动 -> InputSimulator.key_down/key_up/tap_key/press_hotkey()
e) 循环间隙随机休息     -> Humanizer.maybe_rest()（本模块的 click/键操作也会调用小休息）

安全设计
--------
* InputSimulator 持有 EmergencyStop 引用，每个动作前调用 _check()，
  急停触发后所有后续动作都会抛 AbortedByUser。
* 所有按下未释放的键/按钮都登记在 self._held，release_all() 可一次性释放，
  急停回调默认就绑到 release_all，避免「按住不放」的粘键事故。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

try:
    from pynput import keyboard as _kb
    from pynput import mouse as _ms
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖：pynput。请执行  pip install pynput") from exc

from .errors import AbortedByUser          # 单一定义，见 errors.py 的说明
from .timing import Humanizer, TimingProfile, bezier_path, distribute_duration, ease_curve, interruptible_sleep

__all__ = ["InputSimulator", "AbortedByUser"]

KeyLike = Union[str, "_kb.Key", "_kb.KeyCode"]
ButtonLike = Union[str, "_ms.Button"]

# pynput 特殊键名 -> Key 枚举。字符串键名不区分大小写。
_KEY_ALIASES: Dict[str, str] = {
    "alt": "alt_l", "alt_gr": "alt_gr", "ctrl": "ctrl_l", "control": "ctrl_l",
    "shift": "shift_l", "win": "cmd_l", "super": "cmd_l", "meta": "cmd_l",
    "esc": "esc", "escape": "esc", "enter": "enter", "return": "enter",
    "space": "space", "tab": "tab", "backspace": "backspace", "delete": "delete",
    "home": "home", "end": "end", "page_up": "page_up", "page_down": "page_down",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "print_screen": "print_screen", "caps_lock": "caps_lock", "insert": "insert",
}


def resolve_key(key: KeyLike) -> Union["_kb.Key", "_kb.KeyCode"]:
    """
    'a' -> KeyCode('a')；'f5'/'enter'/'ctrl' -> pynput.keyboard.Key.f5 / .enter / .ctrl_l。
    无法识别时按单字符处理，非法输入抛 ValueError。
    """
    if not isinstance(key, str):
        return key
    name = key.strip()
    if not name:
        raise ValueError("按键名不能为空")
    if len(name) == 1:
        return _kb.KeyCode.from_char(name)

    lowered = name.lower()
    lowered = _KEY_ALIASES.get(lowered, lowered)
    attr = getattr(_kb.Key, lowered, None)
    if attr is not None:
        return attr
    # 形如 "f12" 已在 Key 中；再尝试小写后去空格
    attr = getattr(_kb.Key, lowered.replace(" ", "_"), None)
    if attr is not None:
        return attr
    raise ValueError(f"无法识别的按键名: {key!r}")


def resolve_button(button: ButtonLike) -> "_ms.Button":
    if isinstance(button, str):
        attr = getattr(_ms.Button, button.lower(), None)
        if attr is None:
            raise ValueError(f"无法识别的鼠标按键: {button!r}（可用: left/right/middle）")
        return attr
    return button


class InputSimulator:
    """
    鼠标/键盘仿真器。所有对外方法都是线程安全的，但建议单线程调用。

    参数
    ----
    humanizer : Humanizer          随机策略（延时、偏移、时长）
    emergency : EmergencyStop|None 急停开关；提供后每个动作前都会检查
    move_before_click : bool       点击前是否用贝塞尔轨迹移动过去（默认 True）
    """

    def __init__(
        self,
        humanizer: Optional[Humanizer] = None,
        emergency=None,
        profile: Optional[TimingProfile] = None,
        move_before_click: bool = True,
    ) -> None:
        self.humanizer = humanizer or Humanizer(profile=profile)
        self.profile = self.humanizer.profile
        self.emergency = emergency
        self.move_before_click = bool(move_before_click)

        self.mouse = _ms.Controller()
        self.keyboard = _kb.Controller()

        self._lock = threading.RLock()
        self._held: List[Tuple[str, object]] = []   # [("key"|"button", obj)]，按按下顺序登记
        self._held_keys: List[Union["_kb.Key", "_kb.KeyCode"]] = []
        self._held_buttons: List["_ms.Button"] = []

    # ------------------------------------------------------------------ #
    # 急停检查与状态
    # ------------------------------------------------------------------ #
    @property
    def stop_event(self) -> Optional[threading.Event]:
        return self.emergency.event if self.emergency is not None else None

    def _check(self) -> None:
        """急停被触发则抛 AbortedByUser（每个动作前调用）。"""
        if self.emergency is not None and self.emergency.triggered:
            raise AbortedByUser("急停已触发（F1），后续动作全部中止")

    def _sleep(self, seconds: float) -> None:
        """可被急停打断的睡眠。"""
        interruptible_sleep(seconds, self.stop_event)

    @property
    def position(self) -> Tuple[int, int]:
        x, y = self.mouse.position
        return int(x), int(y)

    @property
    def held_keys(self) -> Tuple[Union["_kb.Key", "_kb.KeyCode"], ...]:
        with self._lock:
            return tuple(self._held_keys)

    # ------------------------------------------------------------------ #
    # 需求 c：非线性贝塞尔鼠标轨迹
    # ------------------------------------------------------------------ #
    def move_bezier(
        self,
        target: Tuple[int, int],
        duration: Optional[float] = None,
        steps: Optional[int] = None,
        overshoot: Optional[bool] = None,
    ) -> float:
        """
        沿三次贝塞尔曲线移动鼠标到屏幕坐标 target=(x, y)。

        * 控制点带随机法向偏移，且偏移量有下限 -> 绝不是直线；
        * 时间轴经随机 ease + 逐段抖动 -> 绝不是匀速；
        * overshoot=True 或以 profile.overshoot_prob 概率，会先冲过目标
          几像素再回拉，模拟真实手的过冲修正。

        返回本段移动的总耗时（秒）。
        """
        self._check()
        x1, y1 = int(target[0]), int(target[1])
        x0, y0 = self.position
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        rng = self.humanizer.rng
        prof = self.profile

        if dist < 3.0:
            self._check()
            self.mouse.position = (x1, y1)
            return 0.0

        total = 0.0
        do_overshoot = (
            self.humanizer.chance(prof.overshoot_prob) if overshoot is None else bool(overshoot)
        )

        if do_overshoot:
            ux, uy = (x1 - x0) / dist, (y1 - y0) / dist
            over_len = self.humanizer.sample(prof.overshoot_px)
            jitter_n = rng.uniform(-0.25, 0.25) * over_len
            ox = int(round(x1 + ux * over_len - uy * jitter_n))
            oy = int(round(y1 + uy * over_len + ux * jitter_n))
            total += self._glide_to((ox, oy), duration * 0.8 if duration else None, steps)
            total += self._glide_to((x1, y1), duration * 0.35 if duration else None,
                                    max(prof.move_min_steps, (steps or prof.move_min_steps) // 3))
        else:
            total += self._glide_to((x1, y1), duration, steps)
        return total

    def _glide_to(self, target: Tuple[int, int], duration: Optional[float], steps: Optional[int]) -> float:
        """沿一条随机贝塞尔曲线滑到 target（内部使用，无过冲）。"""
        prof = self.profile
        rng = self.humanizer.rng
        x0, y0 = self.position
        x1, y1 = int(target[0]), int(target[1])
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5

        if steps is None:
            steps = int(max(prof.move_min_steps, min(prof.move_max_steps,
                                                    dist * prof.move_steps_per_100px / 100.0)))
        steps = max(2, int(steps))
        # 未指定时长时，从 profile.move_duration 区间随机采样（长距离自动放慢一点）
        if duration is None:
            duration = self.humanizer.move_duration() * min(2.0, max(1.0, dist / 600.0))
        else:
            duration = float(duration)
        duration = max(0.02, duration)          # 下限保护，避免 dt 全为 0 变成瞬移

        path = bezier_path((x0, y0), (x1, y1), steps, rng=rng,
                           curvature=prof.curvature,
                           curvature_max_px=prof.curvature_max_px,
                           tremor_px=prof.tremor_px)
        timeline = ease_curve(steps, rng, tremor=prof.timeline_tremor)
        dts = distribute_duration(timeline, duration, rng)

        elapsed = 0.0
        for i in range(1, len(path)):
            self._check()
            self.mouse.position = path[i]
            dt = dts[i - 1] if i - 1 < len(dts) else 0.0
            if dt > 0:
                self._sleep(dt)
                elapsed += dt
        return elapsed

    def move_to(self, target: Tuple[int, int], humanize: bool = True) -> None:
        """移动到目标点（humanize=False 时直接瞬移，仅用于调试）。"""
        self._check()
        if humanize:
            self.move_bezier(target)
        else:
            self.mouse.position = (int(target[0]), int(target[1]))

    # ------------------------------------------------------------------ #
    # 需求 b：带像素随机偏移的点击
    # ------------------------------------------------------------------ #
    def click(
        self,
        target: Tuple[int, int],
        button: ButtonLike = "left",
        offset_px: Optional[int] = None,
        count: int = 1,
        move_first: Optional[bool] = None,
        settle: bool = True,
    ) -> Tuple[int, int]:
        """
        点击屏幕坐标 target。落点会在 ±offset_px 的圆内随机偏移，
        按下时长也在 profile.click_hold 区间随抖动。
        返回实际点击的坐标（真实落点，便于日志/调试）。
        """
        self._check()
        btn = resolve_button(button)
        move_first = self.move_before_click if move_first is None else bool(move_first)

        px, py = self.humanizer.jitter_point(target[0], target[1], offset_px)
        if move_first:
            self.move_bezier((px, py))
        else:
            self.mouse.position = (px, py)
            self._sleep(self.humanizer.sample(self.profile.press_settle))
        if settle:
            self._sleep(self.humanizer.sample(self.profile.press_settle))

        presses = max(1, int(count))
        for i in range(presses):
            self._check()
            with self._lock:
                self._held.append(("button", btn))
                self._held_buttons.append(btn)
            self.mouse.press(btn)
            self._sleep(self.humanizer.click_hold())
            self.mouse.release(btn)
            with self._lock:
                _pop_first(self._held, ("button", btn))
                _pop_first(self._held_buttons, btn)
            if i < presses - 1:
                self._sleep(self.humanizer.sample(self.profile.double_click_gap))
        return px, py

    def double_click(self, target: Tuple[int, int], button: ButtonLike = "left",
                     offset_px: Optional[int] = None) -> Tuple[int, int]:
        """双击（两次独立抖动落点，更接近真人）。"""
        return self.click(target, button=button, offset_px=offset_px, count=2)

    def right_click(self, target: Tuple[int, int], offset_px: Optional[int] = None) -> Tuple[int, int]:
        return self.click(target, button="right", offset_px=offset_px)

    def mouse_down(self, button: ButtonLike = "left") -> None:
        self._check()
        btn = resolve_button(button)
        with self._lock:
            self._held.append(("button", btn))
            self._held_buttons.append(btn)
        self.mouse.press(btn)

    def mouse_up(self, button: ButtonLike = "left") -> None:
        btn = resolve_button(button)
        self.mouse.release(btn)
        with self._lock:
            _pop_first(self._held, ("button", btn))
            _pop_first(self._held_buttons, btn)

    def drag(self, start: Tuple[int, int], end: Tuple[int, int],
             button: ButtonLike = "left", duration: Optional[float] = None) -> None:
        """从 start 拖到 end：贝塞尔走过去 -> 按下 -> 贝塞尔拖 -> 松开。"""
        self._check()
        btn = resolve_button(button)
        sx, sy = self.humanizer.jitter_point(start[0], start[1])
        self.move_bezier((sx, sy))
        self._sleep(self.humanizer.sample(self.profile.press_settle))
        self.mouse_down(btn)
        try:
            self._sleep(self.humanizer.sample(self.profile.press_settle))
            ex, ey = self.humanizer.jitter_point(end[0], end[1])
            self._glide_to((ex, ey), duration, None)
            self._sleep(self.humanizer.sample(self.profile.press_settle))
        finally:
            self.mouse_up(btn)

    def scroll(self, amount: int, target: Optional[Tuple[int, int]] = None) -> None:
        """滚轮。amount>0 上滚，<0 下滚。target 不为空时先移过去再滚。"""
        self._check()
        if target is not None:
            self.move_bezier(target)
        steps = max(1, abs(int(amount)))
        direction = 1 if amount > 0 else -1
        for _ in range(steps):
            self._check()
            self.mouse.scroll(0, direction)
            self._sleep(self.humanizer.uniform(0.01, 0.05))

    # ------------------------------------------------------------------ #
    # 需求 d：按键按下 / 释放 + 时长扰动
    # ------------------------------------------------------------------ #
    def key_down(self, key: KeyLike) -> None:
        self._check()
        k = resolve_key(key)
        with self._lock:
            self._held.append(("key", k))
            self._held_keys.append(k)
        self.keyboard.press(k)

    def key_up(self, key: KeyLike) -> None:
        k = resolve_key(key)
        self.keyboard.release(k)
        with self._lock:
            _pop_first(self._held, ("key", k))
            _pop_first(self._held_keys, k)

    def tap_key(self, key: KeyLike, hold: Optional[float] = None, times: int = 1) -> float:
        """
        按一下再松开。按下时长 = hold 或 profile.key_hold 区间内的随机值。
        返回本次实际按住的总时长。
        """
        self._check()
        k = resolve_key(key)
        total = 0.0
        for i in range(max(1, int(times))):
            self._check()
            seconds = self.humanizer.key_hold() if hold is None else float(hold)
            with self._lock:
                self._held.append(("key", k))
                self._held_keys.append(k)
            self.keyboard.press(k)
            self._sleep(seconds)
            self.keyboard.release(k)
            with self._lock:
                _pop_first(self._held, ("key", k))
                _pop_first(self._held_keys, k)
            total += seconds
            if i < times - 1:
                self._sleep(self.humanizer.sample(self.profile.key_gap))
        return total

    def press_hotkey(self, keys: Sequence[KeyLike], hold: Optional[float] = None) -> None:
        """
        组合键（如 ("ctrl", "s") 或 ("ctrl", "shift", "esc")）：
        按顺序按下，随机间隔，最后按随机时长保持，再逆序释放。
        """
        self._check()
        resolved = [resolve_key(k) for k in keys]
        if not resolved:
            return
        pressed: List[Union["_kb.Key", "_kb.KeyCode"]] = []
        try:
            for k in resolved:
                self._check()
                self.keyboard.press(k)
                pressed.append(k)
                self._sleep(self.humanizer.sample(self.profile.key_gap))
            self._sleep(self.humanizer.key_hold() if hold is None else float(hold))
        finally:
            for k in reversed(pressed):
                try:
                    self.keyboard.release(k)
                except Exception:
                    pass

    def type_text(self, text: str, interval: Optional[Tuple[float, float]] = None) -> None:
        """逐字符输入，每字间隔在 interval 或 profile.type_interval 区间内随机。"""
        self._check()
        lo, hi = interval or self.profile.type_interval
        for ch in text:
            self._check()
            self.tap_key(ch, hold=self.humanizer.uniform(0.02, 0.05))
            self._sleep(self.humanizer.uniform(lo, hi))

    # ------------------------------------------------------------------ #
    # 粘键兜底：释放所有按下的键与鼠标按钮
    # ------------------------------------------------------------------ #
    def release_all(self, quiet: bool = True) -> int:
        """
        释放所有登记为「按下未释放」的键与鼠标按钮（急停回调默认绑定它）。
        返回释放的数量；异常被吞掉以保证急停路径绝对能走完。
        """
        released = 0
        with self._lock:
            keys = list(self._held_keys)
            buttons = list(self._held_buttons)
            self._held.clear()
            self._held_keys.clear()
            self._held_buttons.clear()

        for btn in buttons:
            try:
                self.mouse.release(btn)
                released += 1
            except Exception:
                pass
        for k in keys:
            try:
                self.keyboard.release(k)
                released += 1
            except Exception:
                pass
        # 额外保险：确实释放过东西时，再把 Shift/Ctrl/Alt 等修饰键补一刀，
        # 防止「按下时漏登记」导致的粘键（没释放过就不做多余动作）
        if released:
            for name in ("shift", "ctrl", "alt", "cmd"):
                try:
                    key = getattr(_kb.Key, f"{name}_l", None)
                    if key is not None:
                        self.keyboard.release(key)
                except Exception:
                    pass
        if released and not quiet:
            print(f"[InputSimulator] 已释放 {released} 个未释放的按键/按钮")
        return released


def _pop_first(seq: list, value) -> None:
    """移除列表中第一个相等的元素（安全版 list.remove）。"""
    try:
        seq.remove(value)
    except ValueError:
        pass

# -*- coding: utf-8 -*-
"""
vauto.safety —— 全局急停（F1）与中止异常

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

行为
----
* 使用 pynput.keyboard.Listener（Windows 底层 WH_KEYBOARD_LL 钩子）全局监听，
  因此无论焦点在哪个窗口，F1 都能被捕获。
* 按下 F1 时依次执行：置位 threading.Event -> 依次调用回调（默认先释放所有按键）
  -> 打印醒目警告。此后：
    - InputSimulator 的每个动作前会检查并抛 AbortedByUser；
    - 所有 interruptible_sleep 会立刻返回；
    - FocusGuard.wait / 主循环若传入了 event，也会立即退出。
* 默认「不吞掉」F1 键（目标程序仍会收到 F1）。若需要独占该键，
  传 swallow=True —— 注意那会开启 suppress 模式，期间会拦截所有键盘事件，
  只在明确需要时使用。

已知限制
--------
* 若目标进程以管理员权限运行，或使用 Raw Input / 独占全屏，普通权限的钩子
  可能收不到按键，此时请以管理员身份运行本脚本。
* 本模块不提供任何「防检测 / 反反作弊」能力，也不应添加。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Sequence, Union

try:
    from pynput import keyboard as _kb
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖：pynput。请执行  pip install pynput") from exc

from .errors import AbortedByUser
from .input_sim import resolve_key  # 复用按键名解析（'f1' / 'esc' / 'a' ...）

__all__ = ["AbortedByUser", "EmergencyStop"]


class EmergencyStop:
    """
    全局急停开关。

    用法：
        sim = InputSimulator(...)
        stop = EmergencyStop("f1", on_trigger=sim.release_all, emergency=sim)
        with stop:               # 自动 start() / stop()
            while not stop.triggered:
                ...
        # 或在主循环里： stop.check()  /  stop.event.is_set()

    参数
    ----
    hotkey      : 触发热键，单键名（'f1'）或组合（('ctrl', 'alt', 'q')）
    on_trigger  : 触发时的回调列表/单个回调，都会在 try 里执行
    swallow     : True 时拦截该键不让前台程序收到（会开启 suppress 模式，慎用）
    verbose     : 是否打印状态信息
    """

    def __init__(
        self,
        hotkey: Union[str, Sequence[str]] = "f1",
        on_trigger: Optional[Union[Callable[[], None], Sequence[Callable[[], None]]]] = None,
        swallow: bool = False,
        verbose: bool = True,
    ) -> None:
        if isinstance(hotkey, str):
            self.hotkey: tuple = (hotkey,)
        else:
            self.hotkey = tuple(hotkey)
        if not self.hotkey:
            raise ValueError("hotkey 不能为空")
        self._keys = [resolve_key(k) for k in self.hotkey]
        self._target = self._keys[-1]                  # 最后一个键作为触发键
        self._modifiers = set(self._keys[:-1])

        self.swallow = bool(swallow)
        self.verbose = bool(verbose)

        self.event = threading.Event()                 # 供各处轮询/打断 sleep
        self._callbacks: List[Callable[[], None]] = []
        self._listener: Optional[_kb.Listener] = None
        self._pressed: set = set()
        self._lock = threading.RLock()
        self.triggered_at: Optional[float] = None

        if on_trigger is not None:
            self.register_callback(on_trigger)

    # ------------------------------------------------------------------ #
    def register_callback(self, callback: Union[Callable[[], None], Sequence[Callable[[], None]]]) -> None:
        """注册触发回调（默认建议注册 InputSimulator.release_all）。"""
        if callable(callback):
            self._callbacks.append(callback)
        else:
            self._callbacks.extend([c for c in callback if callable(c)])

    # ------------------------------------------------------------------ #
    @property
    def triggered(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        """已触发则抛 AbortedByUser；供业务循环每轮调用。"""
        if self.triggered:
            raise AbortedByUser("急停已触发（%s）" % "+".join(self.hotkey))

    def wait(self, timeout: Optional[float] = None, poll: float = 0.1) -> bool:
        """
        阻塞直到急停触发（或超时）。timeout=None 表示一直等。
        返回 True 表示被触发。可用于「按 F1 结束程序」这类主线程等待。
        """
        if timeout is None:
            self.event.wait()
            return True
        return self.event.wait(timeout)

    def reset(self) -> None:
        """清除急停状态，恢复动作能力（谨慎：一般只在测试里用）。"""
        self.event.clear()
        self.triggered_at = None

    # ------------------------------------------------------------------ #
    def _fire(self) -> None:
        with self._lock:
            if self.event.is_set():
                return
            self.event.set()
            self.triggered_at = time.monotonic()
            callbacks = list(self._callbacks)

        if self.verbose:
            print("\n" + "!" * 62)
            print(f"!! 急停已触发：[{'+'.join(self.hotkey).upper()}] —— 所有动作立即中止")
            print("!" * 62)

        for cb in callbacks:
            try:
                cb()
            except Exception as exc:                    # 绝不因回调异常打断急停链路
                if self.verbose:
                    print(f"[EmergencyStop] 回调执行失败: {cb!r} -> {exc!r}")

    def _on_press(self, key) -> Optional[bool]:
        self._pressed.add(key)
        try:
            if key == self._target and (not self._modifiers or self._modifiers.issubset(self._pressed)):
                self._fire()
        except Exception:
            pass
        if self.swallow and key == self._target:
            return False                                # 拦截：不让前台程序收到
        return None

    def _on_release(self, key) -> None:
        self._pressed.discard(key)

    # ------------------------------------------------------------------ #
    def start(self) -> "EmergencyStop":
        """启动全局监听（非阻塞，钩子在独立线程里）。"""
        if self._listener is not None:
            return self
        self._listener = _kb.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=self.swallow,
        )
        self._listener.daemon = True
        self._listener.start()
        if self.verbose:
            print(f"[EmergencyStop] 已启用全局急停键: {'+'.join(self.hotkey).upper()}"
                  f"（随时按下可立即中止并释放所有按键）")
        return self

    def stop(self) -> None:
        """停止监听（不会自动重置 triggered 状态）。"""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
            if self.verbose:
                print("[EmergencyStop] 全局监听已关闭")

    def __enter__(self) -> "EmergencyStop":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

# -*- coding: utf-8 -*-
"""
vauto.focus —— 窗口焦点检测与「非前台即暂停」守卫

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

核心 API
--------
is_foreground(hwnd, deep=True)                当前前台窗口是否就是目标窗口
wait_until_foreground(hwnd, timeout, ...)     阻塞等待回到前台（可被急停打断）
FocusGuard(hwnd, ...)                         .is_active() / .require() / .wait()

deep 判定的意义
---------------
Windows 上 GetForegroundWindow() 返回的可能是目标窗口的弹出子窗口 / 拥有窗口，
直接 == 比较会误判「不在前台」。因此 deep=True 时依次判定：
    1) 前台 hwnd 就是目标 hwnd；
    2) 前台 hwnd 的 root owner 等于目标的 root owner（处理子窗口/弹窗）；
    3) 两者属于同一进程 PID（宽松模式，strict_process=False 时才启用）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

try:
    import win32gui
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖：pywin32。请执行  pip install pywin32") from exc

from .capture import (
    GA_ROOTOWNER,
    get_foreground_hwnd,
    get_window_pid,
    get_window_title,
    is_window_alive,
    is_window_visible,
)

__all__ = [
    "NotForeground",
    "is_foreground",
    "wait_until_foreground",
    "bring_to_front",
    "FocusGuard",
]


class NotForeground(RuntimeError):
    """目标窗口当前不在前台，动作被拒绝。"""


def _root(hwnd: int) -> int:
    try:
        root = win32gui.GetAncestor(hwnd, GA_ROOTOWNER)
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def is_foreground(hwnd: int, deep: bool = True, strict_process: bool = False) -> bool:
    """
    目标窗口当前是否为前台（活动）窗口。

    deep=True          启用 root owner / 进程级宽松判定
    strict_process     True 时只接受 hwnd 直接相等或同 root owner，不做 PID 兜底
    """
    if not is_window_alive(hwnd):
        return False
    fg = get_foreground_hwnd()
    if not fg:
        return False
    if fg == hwnd:
        return True
    if not deep:
        return False
    if _root(fg) == _root(hwnd):
        return True
    if not strict_process:
        pid_fg, pid_tg = get_window_pid(fg), get_window_pid(hwnd)
        if pid_fg and pid_fg == pid_tg:
            return True
    return False


def wait_until_foreground(
    hwnd: int,
    timeout: Optional[float] = None,
    poll: float = 0.05,
    stop_event: Optional[threading.Event] = None,
    deep: bool = True,
    strict_process: bool = False,
    on_pause: Optional[Callable[[float], None]] = None,
) -> bool:
    """
    阻塞等待目标窗口回到前台（后台期间不执行任何动作）。

    timeout=None  一直等；否则最多等 timeout 秒
    stop_event    急停事件：一旦被 set，立即返回 False（避免卡死在等待里）
    on_pause      可选回调，参数为已等待秒数；便于打印「已暂停」提示

    返回 True=已在前台；False=超时 / 窗口消失 / 被急停打断。
    """
    start = time.monotonic()
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if not is_window_alive(hwnd):
            return False
        if is_foreground(hwnd, deep=deep, strict_process=strict_process):
            return True
        if timeout is not None and (time.monotonic() - start) >= timeout:
            return False
        if on_pause is not None:
            try:
                on_pause(time.monotonic() - start)
            except Exception:
                pass
        # 分片睡眠，保证急停响应足够快
        time.sleep(min(poll, 0.05))


def bring_to_front(hwnd: int) -> bool:
    """
    尽力把窗口切到前台（工具能力，是否调用由业务决定）。

    注意 Windows 的前台锁定策略：非前台进程调用 SetForegroundWindow 常被忽略，
    常见变通是 ShowWindow(SW_RESTORE) + SetWindowPos(HWND_TOP)，
    或者对前台线程 AttachThreadInput。这里只做最保守的尝试，失败返回 False。
    """
    if not is_window_alive(hwnd):
        return False
    try:
        import win32con

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        return is_foreground(hwnd)
    except Exception:
        return False


class FocusGuard:
    """
    焦点守卫：只有目标窗口在前台时才放行动作，后台自动暂停。

    用法：
        guard = FocusGuard(hwnd, stop_event=stop.event)
        guard.require()                    # 不在前台直接抛 NotForeground
        guard.wait()                       # 后台时阻塞等待（急停可打断）
        if guard.is_active(): ...          # 非阻塞查询
    """

    def __init__(
        self,
        hwnd: int,
        deep: bool = True,
        strict_process: bool = False,
        poll: float = 0.05,
        stop_event: Optional[threading.Event] = None,
        verbose: bool = False,
        on_lose_focus: Optional[Callable[[], None]] = None,
        on_gain_focus: Optional[Callable[[], None]] = None,
    ) -> None:
        self.hwnd = int(hwnd)
        self.deep = bool(deep)
        self.strict_process = bool(strict_process)
        self.poll = float(poll)
        self.stop_event = stop_event
        self.verbose = bool(verbose)
        self.paused_total = 0.0     # 累计因后台暂停的秒数（统计用）
        # 【2026-10-04 用户要求】焦点进出时要通知业务层做两件事：
        #   切出去 → 关掉 Caps Lock（否则他在别的程序里打字全是大写）；
        #   切回来 → 开回 Caps Lock + 鼠标归位 + 重新判"我在哪一屏"。
        self.on_lose_focus = on_lose_focus
        self.on_gain_focus = on_gain_focus
        self._was_active: Optional[bool] = None   # 上一次已知的前台状态（None=还没测过）
        self._firing = False                      # 防回调里再触发回调（重入）

    # ------------------------------------------------------------------ #
    def _fire_transition(self, active: bool) -> None:
        """前台状态**真的翻转**时通知一次回调（重复状态不通知）。

        注意：先更新 _was_active 再调回调 —— 回调里若又调 is_active()（比如"重新判我在哪"
        会抓帧），不会形成递归。
        """
        prev, self._was_active = self._was_active, active
        if prev is None or prev == active or self._firing:
            return
        cb = self.on_gain_focus if active else self.on_lose_focus
        if cb is None:
            return
        self._firing = True
        try:
            cb()
        except Exception:
            pass
        finally:
            self._firing = False

    def is_active(self) -> bool:
        """非阻塞查询：目标窗口是否在前台且窗口仍存活。"""
        if not is_window_alive(self.hwnd):
            active = False
        else:
            active = is_foreground(self.hwnd, deep=self.deep, strict_process=self.strict_process)
        self._fire_transition(active)
        return active

    def require(self) -> None:
        """不在前台 -> 抛 NotForeground（供「每步动作前守卫」的写法）。"""
        if not is_window_alive(self.hwnd):
            raise NotForeground(f"目标窗口已关闭: hwnd={self.hwnd}")
        if not self.is_active():
            title = get_window_title(get_foreground_hwnd())
            raise NotForeground(
                f"目标窗口不在前台（当前前台: {title!r}），动作已拒绝: hwnd={self.hwnd}"
            )

    def wait(
        self,
        timeout: Optional[float] = None,
        on_pause: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """
        后台时阻塞等待回到前台。
        返回 True=已在前台可继续；False=超时 / 窗口消失 / 被急停打断。
        """
        if self.is_active():
            return True
        start = time.monotonic()

        def _pause(elapsed: float) -> None:
            if self.verbose and int(elapsed * 10) % 10 == 0:
                print(f"[FocusGuard] 目标窗口不在前台，已暂停 {elapsed:.1f}s ...")
            if on_pause is not None:
                on_pause(elapsed)

        ok = wait_until_foreground(
            self.hwnd,
            timeout=timeout,
            poll=self.poll,
            stop_event=self.stop_event,
            deep=self.deep,
            strict_process=self.strict_process,
            on_pause=_pause,
        )
        self.paused_total += time.monotonic() - start
        if ok:
            # 【2026-10-04】一回来就立刻通知（不等下一次 is_active）—— 用户切回来时
            # 需要马上"开回大写锁定 + 鼠标归位 + 重新认界面"，晚一步就可能先按错键。
            self._fire_transition(True)
        return ok

    def blocking_loop_allowed(self) -> bool:
        """供主循环判断「是否允许执行本轮业务动作」。不在前台返回 False。"""
        return self.is_active()

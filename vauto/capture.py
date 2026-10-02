# -*- coding: utf-8 -*-
"""
vauto.capture —— 屏幕 / 窗口捕获工具层（只负责「看见」，不做任何判断）

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。本模块不含内存读取与进程注入。

核心 API
--------
enable_dpi_awareness()                 进程级 DPI 感知（必须在创建窗口/抓屏前调用）
Rect                                   窗口矩形值对象（屏幕物理像素）
WindowCapture(hwnd, client_only=True)  按窗口抓帧，.grab() -> BGR ndarray
list_windows() / find_window_by_title() 找窗口句柄
client_to_screen() / screen_to_client() 客户区坐标 <-> 屏幕坐标

技术要点
--------
* mss 的 MSS 实例是「线程局部」的：跨线程复用会抛异常，本类内部按线程重建。
* mss 只能抓「屏幕上真实可见」的像素；窗口被遮挡 = 抓到遮挡物，最小化 = 抓不到。
  被遮挡/最小化场景请改用 grab_printwindow()（有限兼容，GPU 加速窗口可能全黑）。
* 帧以 BGR 顺序返回（OpenCV 原生顺序），可直接喂给 TemplateMatcher。
"""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    import mss
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少依赖：mss。请执行  pip install mss") from exc

try:
    import win32con
    import win32gui
    import win32ui
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "缺少依赖：pywin32。请执行  pip install pywin32  "
        "（如安装后仍报错，请以管理员身份运行：python Scripts/pywin32_postinstall.py -install）"
    ) from exc

# 自备常量，避免依赖 win32con 中可能缺失的定义
GA_ROOTOWNER = 3
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

# 直接用 ctypes 调 user32 的少量函数（签名显式声明，避免 64 位句柄截断）
from ctypes import wintypes as _wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowThreadProcessId.argtypes = [_wintypes.HWND, ctypes.POINTER(_wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = _wintypes.DWORD
_user32.PrintWindow.argtypes = [_wintypes.HWND, _wintypes.HDC, _wintypes.UINT]
_user32.PrintWindow.restype = _wintypes.BOOL

__all__ = [
    "Rect",
    "WindowUnavailable",
    "enable_dpi_awareness",
    "get_window_rect",
    "client_to_screen",
    "screen_to_client",
    "list_windows",
    "find_window_by_title",
    "find_windows_by_title",
    "get_window_title",
    "get_window_pid",
    "get_window_thread_id",
    "get_window_class",
    "is_window_alive",
    "is_minimized",
    "get_foreground_hwnd",
    "capture_monitor",
    "WindowCapture",
]


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> str:
    """
    开启进程 DPI 感知，让「窗口矩形」与「真实物理像素」一一对应。

    不调用的话，在 125% / 150% 缩放下：
      - win32gui.GetWindowRect 返回逻辑像素（被缩放过的值）
      - mss 抓到的图是物理像素
    两者混用会导致点击坐标整体偏移，这是 Windows 上位姿自动化最常见的坑。

    返回实际生效的模式名，便于日志确认。
    """
    user32 = ctypes.windll.user32

    # 1) Windows 10 1703+：PER_MONITOR_AWARE_V2 = -4（最优）
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass

    # 2) Windows 8.1+：PER_MONITOR_DPI_AWARE = 2
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass

    # 3) 兜底：系统级 DPI 感知
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "none"


# --------------------------------------------------------------------------- #
# 窗口基础信息
# --------------------------------------------------------------------------- #
class WindowUnavailable(RuntimeError):
    """窗口不存在 / 已关闭 / 最小化 / 尺寸非法，无法抓帧。"""


@dataclass(frozen=True)
class Rect:
    """屏幕物理像素坐标系下的矩形（右下边界为开区间，宽度 = right - left）。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> Tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def is_valid(self, min_size: int = 2) -> bool:
        return self.width >= min_size and self.height >= min_size

    def as_mss_dict(self) -> dict:
        """转成 mss.grab() 需要的 monitor 字典。"""
        return {
            "left": int(self.left),
            "top": int(self.top),
            "width": int(self.width),
            "height": int(self.height),
        }

    def offset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def is_window_alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(win32gui.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(win32gui.IsIconic(hwnd))


def is_window_visible(hwnd: int) -> bool:
    return bool(win32gui.IsWindowVisible(hwnd))


def get_window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""


def get_window_class(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd) or ""
    except Exception:
        return ""


def get_window_pid(hwnd: int) -> int:
    """
    返回该窗口所属进程 PID（用于「同进程即视为目标」的宽松前台判定）。
    取不到时返回 0（调用方需把 0 当作「未知」，不要当成有效 pid）。
    注意：win32gui 没有导出 GetWindowThreadProcessId（pywin32 里它在 win32process），
    这里直接用 ctypes 调 user32，避免依赖 win32process 且不吞掉真实错误。
    """
    try:
        pid = _wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(_wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def get_window_thread_id(hwnd: int) -> int:
    """返回创建该窗口的线程 ID（取不到返回 0）。"""
    try:
        tid = _wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(_wintypes.HWND(int(hwnd)), ctypes.byref(tid))
        return int(tid.value)
    except Exception:
        return 0


def get_root_window(hwnd: int) -> int:
    """取顶层（root owner）窗口，处理弹出式/子窗口抢焦点的情况。"""
    try:
        root = win32gui.GetAncestor(hwnd, GA_ROOTOWNER)
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def get_foreground_hwnd() -> int:
    return int(win32gui.GetForegroundWindow())


def get_window_rect(hwnd: int, client_only: bool = False) -> Rect:
    """
    取窗口矩形。

    client_only=True   -> 客户区（不含标题栏/边框），抓帧与相对坐标匹配用它更稳
    client_only=False  -> 整个窗口（含标题栏/边框）
    """
    if not is_window_alive(hwnd):
        raise WindowUnavailable(f"窗口句柄无效或已关闭: hwnd={hwnd}")

    if client_only:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)  # 客户区左上角恒为 (0,0)
        sx, sy = win32gui.ClientToScreen(hwnd, (cl, ct))
        ex, ey = win32gui.ClientToScreen(hwnd, (cr, cb))
        return Rect(int(sx), int(sy), int(ex), int(ey))

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return Rect(int(l), int(t), int(r), int(b))


def client_to_screen(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """窗口客户区坐标 -> 屏幕物理像素坐标（点击前必转）。"""
    sx, sy = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
    return int(sx), int(sy)


def screen_to_client(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """屏幕物理像素坐标 -> 窗口客户区坐标。"""
    cx, cy = win32gui.ScreenToClient(hwnd, (int(x), int(y)))
    return int(cx), int(cy)


def list_windows(
    visible_only: bool = True,
    with_title_only: bool = True,
    class_filter: Optional[Sequence[str]] = None,
) -> List[Tuple[int, str, str]]:
    """
    枚举顶层窗口，返回 [(hwnd, title, class_name), ...]（按 Z 序，前台在前）。
    用于人工确定目标窗口句柄，例如 demo_skeleton.py --list。
    """
    result: List[Tuple[int, str, str]] = []
    allowed = set(class_filter) if class_filter else None

    def _enum(hwnd, _param):
        if visible_only and not is_window_visible(hwnd):
            return True
        title = get_window_title(hwnd)
        if with_title_only and not title:
            return True
        cls = get_window_class(hwnd)
        if allowed is not None and cls not in allowed:
            return True
        result.append((int(hwnd), title, cls))
        return True

    win32gui.EnumWindows(_enum, None)
    return result


def find_windows_by_title(
    title_sub: str,
    exact: bool = False,
    case_sensitive: bool = False,
    class_name: Optional[str] = None,
    visible_only: bool = True,
) -> List[int]:
    """按标题（子串或全等）查找所有匹配窗口，返回 hwnd 列表（Z 序）。"""
    needle = title_sub if case_sensitive else title_sub.lower()
    hits: List[int] = []
    for hwnd, title, cls in list_windows(visible_only=visible_only, with_title_only=True):
        if class_name is not None and cls != class_name:
            continue
        hay = title if case_sensitive else title.lower()
        if (hay == needle) if exact else (needle in hay):
            hits.append(hwnd)
    return hits


def find_window_by_title(
    title_sub: str,
    exact: bool = False,
    case_sensitive: bool = False,
    class_name: Optional[str] = None,
    visible_only: bool = True,
) -> Optional[int]:
    """返回第一个匹配窗口的 hwnd；找不到返回 None。"""
    hits = find_windows_by_title(title_sub, exact, case_sensitive, class_name, visible_only)
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# 抓帧
# --------------------------------------------------------------------------- #
def _bgra_to_bgr(raw) -> np.ndarray:
    """mss 的 ScreenShot -> BGR ndarray（OpenCV 顺序）。"""
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)          # BGRA 缓冲
    arr = arr.reshape(raw.height, raw.width, 4)
    return np.ascontiguousarray(arr[:, :, :3])             # 丢 alpha，得到 HxWx3 BGR


def capture_monitor(monitor_index: int = 1, region: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
    """
    抓整个显示器。monitor_index: 0 = 所有显示器拼起来，1..N = 第 N 个显示器。
    region=(x, y, w, h) 可只抓其中一块（屏幕坐标）。
    注意：MSS 实例不可跨线程复用，这里每次新建，开销很小。
    """
    with mss.mss() as sct:
        if region is not None:
            x, y, w, h = region
            box = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        else:
            box = sct.monitors[int(monitor_index)]
        return _bgra_to_bgr(sct.grab(box))


def capture_printwindow(hwnd: int, client_only: bool = False) -> np.ndarray:
    """
    用 PrintWindow 抓窗口（可抓被遮挡的窗口，代价是兼容性差）。

    局限：
      * 使用 GPU/DirectX 渲染的窗口常返回全黑或半黑，请自行验证。
      * 部分窗口会在客户区坐标与图像坐标间存在 1~2 像素偏差。
    返回 BGR ndarray。
    """
    if not is_window_alive(hwnd):
        raise WindowUnavailable(f"窗口句柄无效或已关闭: hwnd={hwnd}")

    if client_only:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        w, h = int(cr - cl), int(cb - ct)
    else:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = int(r - l), int(b - t)

    if w < 2 or h < 2:
        raise WindowUnavailable(f"窗口尺寸非法（{w}x{h}），可能已最小化: hwnd={hwnd}")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bitmap)
        flags = PW_RENDERFULLCONTENT | (PW_CLIENTONLY if client_only else 0)
        _user32.PrintWindow(_wintypes.HWND(int(hwnd)), _wintypes.HDC(save_dc.GetSafeHdc()),
                            _wintypes.UINT(flags))

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)                  # BGRA
        img = np.frombuffer(bits, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        return np.ascontiguousarray(img[:, :, :3])
    finally:
        try:
            win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        try:
            save_dc.DeleteDC()
        except Exception:
            pass
        try:
            mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


class WindowCapture:
    """
    绑定某个窗口的抓帧器（线程内自管 mss 实例）。

    用法：
        cap = WindowCapture(hwnd, client_only=True)
        frame = cap.grab()                 # BGR ndarray, 形状 (H, W, 3)
        frame = cap.grab(region=(x, y, w, h))   # 只抓客户区内一块（客户区相对坐标）
        cap.close()                        # 或 with 语句自动关闭

    抓到的帧坐标系 == 窗口客户区坐标系，因此 TemplateMatcher 的匹配结果
    可以直接用 client_to_screen(hwnd, *match.center) 转成屏幕坐标点击。
    """

    def __init__(self, hwnd: int, client_only: bool = True, printwindow_fallback: bool = False) -> None:
        self.hwnd = int(hwnd)
        self.client_only = bool(client_only)
        self.printwindow_fallback = bool(printwindow_fallback)
        self._sct = None
        self._sct_thread: Optional[int] = None
        self._lock = threading.RLock()

    # -- 生命周期 ---------------------------------------------------------- #
    def _ensure_sct(self):
        """mss 实例是线程局部的；换线程就重建，避免 'MSS instance in another thread'。"""
        tid = threading.get_ident()
        if self._sct is None or self._sct_thread != tid:
            self._close_sct()
            self._sct = mss.mss()
            self._sct_thread = tid
        return self._sct

    def _close_sct(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
        self._sct = None
        self._sct_thread = None

    def close(self) -> None:
        with self._lock:
            self._close_sct()

    def __enter__(self) -> "WindowCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 抓帧 -------------------------------------------------------------- #
    @property
    def rect(self) -> Rect:
        return get_window_rect(self.hwnd, client_only=self.client_only)

    def grab(self, region: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
        """
        抓一帧，返回 BGR ndarray。

        region: 可选，(x, y, w, h)，相对窗口客户区左上角；用于小范围高频轮询以降低算力。
        异常：WindowUnavailable（窗口没了 / 最小化 / 尺寸非法）
        """
        with self._lock:
            if not is_window_alive(self.hwnd):
                raise WindowUnavailable(f"窗口已关闭: hwnd={self.hwnd}")
            if is_minimized(self.hwnd):
                if self.printwindow_fallback:
                    return capture_printwindow(self.hwnd, client_only=self.client_only)
                raise WindowUnavailable(f"窗口已最小化，mss 无法抓帧: hwnd={self.hwnd}")

            base = get_window_rect(self.hwnd, client_only=self.client_only)
            if not base.is_valid():
                if self.printwindow_fallback:
                    return capture_printwindow(self.hwnd, client_only=self.client_only)
                raise WindowUnavailable(f"窗口尺寸非法（{base.width}x{base.height}）: hwnd={self.hwnd}")

            if region is not None:
                rx, ry, rw, rh = (int(v) for v in region)
                # 裁剪到客户区内，避免越界抓出黑边
                rx = max(0, min(rx, base.width - 1))
                ry = max(0, min(ry, base.height - 1))
                rw = max(1, min(rw, base.width - rx))
                rh = max(1, min(rh, base.height - ry))
                box = Rect(base.left + rx, base.top + ry, base.left + rx + rw, base.top + ry + rh)
            else:
                box = base

            sct = self._ensure_sct()
            return _bgra_to_bgr(sct.grab(box.as_mss_dict()))

    def grab_printwindow(self) -> np.ndarray:
        """被遮挡 / 最小化场景的备选抓帧方式（兼容性见函数说明）。"""
        with self._lock:
            return capture_printwindow(self.hwnd, client_only=self.client_only)

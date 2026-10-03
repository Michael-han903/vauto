# -*- coding: utf-8 -*-
"""运行期间阻止 Windows 息屏/睡眠（长时间实跑的必需品）。

用户实测问题（2026-10-03）："运行时间久了电脑会自己黑屏然后不运行"。

为什么会这样：
  * **系统睡眠**：整台机子挂起 → 脚本和游戏一起停摆（表现为"不运行"）；
  * **仅关显示器**：游戏可能被系统降频/暂停渲染 → 抓帧变全黑或旧帧 →
    所有判据失效 → 脚本"看得见屏幕却没反应"。

解决：跑的时候申请"显示器常亮 + 系统不休眠"（SetThreadExecutionState），退出时释放。
**只调电源管理 API，不碰游戏进程** —— 符合工具层契约（不做内存读取 / 注入 / 驱动）。

注意：SetThreadExecutionState 是**按线程**生效的（ES_CONTINUOUS 挂在线程上），
所以这里用一个常驻后台线程持有它，并周期性续订（有些机器/驱动会忽略一次性设置）。

配套（更保险，改系统设置，二选一即可）：
    powercfg /change standby-timeout-ac 0      # 永不睡眠
    powercfg /change monitor-timeout-ac 0      # 显示器永不关
"""
from __future__ import annotations

import ctypes
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


def _set(flags: int) -> bool:
    """调 SetThreadExecutionState（只影响当前线程的电源请求）。失败返回 False。"""
    try:
        k = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        k.SetThreadExecutionState.restype = ctypes.c_uint
        return bool(k.SetThreadExecutionState(ctypes.c_uint(flags)))
    except Exception:
        return False


class KeepAwake:
    """持有"防息屏/防休眠"请求的后台线程。用法：k = KeepAwake(); k.start(); …; k.stop()"""

    def __init__(self, interval: float = 30.0):
        self.interval = float(interval)
        self.active = False
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> bool:
        if self.active:
            return True
        started = threading.Event()

        def _loop() -> None:
            # 在本线程里申请（ES_CONTINUOUS 就是挂在本线程上的）
            ok = _set(_FLAGS)
            if ok:
                self.active = True
            started.set()
            while ok and not self._stop.wait(self.interval):
                _set(_FLAGS)                # 周期续订，防被忽略
            _set(ES_CONTINUOUS)             # 退出前清掉本线程的请求

        self._th = threading.Thread(target=_loop, daemon=True, name="vauto-keepawake")
        self._th.start()
        started.wait(2.0)
        return self.active

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(1.5)
        self.active = False


def keep_awake(enable: bool = True) -> KeepAwake:
    k = KeepAwake()
    if enable:
        k.start()
    return k

# -*- coding: utf-8 -*-
"""
键盘状态小工具：读 / 设置 Caps Lock（大写锁定）。

【免责声明 / EULA 与账号风险】
本模块只调用 Windows 提供的键盘状态查询与一次按键仿真
（`GetKeyState` + `keybd_event`），**不读写游戏内存、不注入进程、不加载驱动、
不修改游戏文件**。但把它用在《极限竞速：地平线》这类有反作弊与联网风控的在线游戏上，
仍可能被判定为异常输入行为，后果是账号级的；是否使用、用哪个账号，由使用者自行决定并承担。

为什么需要它（用户要求，2026-10-03）：
"设置一下程序进入地平线六的时候自动检测大写锁定状态，没开的话开一下" ——
游戏里某些按键绑定/输入行为与 Caps Lock 状态有关，开着才和用户手动操作时一致。
"""

from __future__ import annotations

import ctypes

VK_CAPITAL = 0x14          # Windows 虚拟键码：Caps Lock
KEYEVENTF_KEYUP = 0x0002


def caps_lock_on() -> bool:
    """当前 Caps Lock 是否打开（**只读**，不改状态）。"""
    try:
        return bool(ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1)
    except Exception:
        return False


def set_caps_lock(on: bool = True) -> bool:
    """把 Caps Lock 设成想要的状态；已经是目标状态就**什么都不做**。返回最终状态。

    只发一次真实的按键（按下+抬起）—— 不写注册表、不改系统设置。
    """
    try:
        if caps_lock_on() == bool(on):
            return caps_lock_on()
        user32 = ctypes.windll.user32
        user32.keybd_event(VK_CAPITAL, 0, 0, 0)                    # 按下
        user32.keybd_event(VK_CAPITAL, 0, KEYEVENTF_KEYUP, 0)      # 抬起
        return caps_lock_on()
    except Exception:
        return caps_lock_on()


def force_caps_off() -> bool:
    """收尾用：把 Caps Lock 关掉，返回最终状态（已经是关的就什么都不做）。

    【2026-10-04 用户要求】"关闭程序的时候也关掉 caps" —— 无论是 F1 急停、点「停止」、
    还是直接关控制台窗口，退出后都不应该把用户的键盘留在大写锁定状态。
    调用点：`Runner.run()` 的 finally（覆盖前两种）+ 控制台 `_on_close`（第三种会
    `os._exit`，跑不到 finally）。
    """
    try:
        set_caps_lock(False)
    except Exception:
        pass
    return caps_lock_on()


__all__ = ["VK_CAPITAL", "caps_lock_on", "set_caps_lock", "force_caps_off"]

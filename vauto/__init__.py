# -*- coding: utf-8 -*-
"""
vauto —— 通用 GUI 视觉自动化「工具层」原型（仅用于算法学习）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本原型仅用于算法学习（计算机视觉 / 输入仿真研究）。
若用于第三方软件，可能违反该软件的用户许可协议（EULA）或服务条款，
并可能触发对方的风控 / 反作弊机制，存在账号被封禁等风险，请自行承担后果。
本工具层刻意不包含：内存读取、进程注入、DLL 注入、驱动加载、网络通信、
加解密、绕过检测等任何侵入式能力。请勿在本工具层之上添加此类功能。
======================================================================

设计约定
--------
1. 本包只做「看得见 / 点得动」的工具层：
   屏幕捕获 → 模板匹配 → 仿真输入 → 窗口焦点守卫 → 全局急停。
2. 一切业务判断（点什么、何时点、点几次、点完做什么）都留给调用方，
   代码中以「# TODO: implement your own business logic here」占位。
3. 坐标系约定：
   - 所有对外暴露的坐标都是「屏幕物理像素」坐标（进程需先开启 DPI 感知）。
   - 模板匹配返回的坐标相对「抓帧图像」左上角；若用 client_only=True 抓帧，
     即窗口客户区坐标，点击前需 capture.client_to_screen(hwnd, x, y) 转屏幕坐标。
4. 所有阻塞型等待都支持传入 stop_event（EmergencyStop.event），
   保证按下 F1 后能立刻中断，而不是傻等到 timeout。

模块与依赖对应关系（子模块按需导入，缺哪个依赖只影响用到它的模块）：
    vauto.capture    -> mss, pywin32, numpy
    vauto.matching   -> opencv-python, numpy
    vauto.timing     -> numpy（随机源为标准库 random，纯函数可离线单测）
    vauto.input_sim  -> pynput, numpy
    vauto.focus      -> pywin32
    vauto.safety     -> pynput

用法见 demo_skeleton.py 与 README.md。
"""

from __future__ import annotations

import importlib
from typing import Dict, List

__version__ = "0.1.0"

# 名字 -> 定义它的子模块（惰性导入用，见下方 __getattr__）
_EXPORTS: Dict[str, str] = {
    # capture
    "enable_dpi_awareness": "capture",
    "WindowCapture": "capture",
    "Rect": "capture",
    "WindowUnavailable": "capture",
    "list_windows": "capture",
    "find_window_by_title": "capture",
    "find_windows_by_title": "capture",
    "client_to_screen": "capture",
    "screen_to_client": "capture",
    "get_window_rect": "capture",
    "get_window_title": "capture",
    "get_window_pid": "capture",
    "capture_monitor": "capture",
    # matching
    "TemplateMatcher": "matching",
    "Match": "matching",
    "find_template": "matching",
    "find_template_best": "matching",
    "draw_matches": "matching",
    "load_image": "matching",
    "save_image": "matching",
    # timing
    "Humanizer": "timing",
    "TimingProfile": "timing",
    "bezier_path": "timing",
    "ease_curve": "timing",
    "distribute_duration": "timing",
    "interruptible_sleep": "timing",
    # vision（识别层组合件：ROI / 降采样 / 滞回去抖 / 画面变化检测）
    "VisualDetector": "vision",
    "scaled_frame": "vision",
    "to_full_point": "vision",
    "to_frame_point": "vision",
    "frame_signature": "vision",
    "signature_distance": "vision",
    "mean_abs_diff": "vision",
    "wait_stable": "vision",
    # input
    "InputSimulator": "input_sim",
    "resolve_key": "input_sim",
    # focus
    "FocusGuard": "focus",
    "NotForeground": "focus",
    "is_foreground": "focus",
    "wait_until_foreground": "focus",
    "bring_to_front": "focus",
    # safety
    "EmergencyStop": "safety",
    "AbortedByUser": "errors",
    # recorder
    "record_window": "recorder",
    "RecordResult": "recorder",
}

__all__ = sorted(_EXPORTS)

# 依赖 -> pip 包名（check_dependencies 用）
_DEPENDENCIES = {
    "numpy": "numpy",
    "cv2": "opencv-python",
    "mss": "mss",
    "win32gui": "pywin32",
    "pynput": "pynput",
}


def __getattr__(name: str):
    """PEP 562 惰性导入：用到哪个名字才加载对应子模块，缺依赖时给出明确提示。"""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value          # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> List[str]:
    return sorted(__all__)


def check_dependencies() -> Dict[str, bool]:
    """
    检查运行依赖是否就绪，返回 {模块名: 是否可用}，并打印缺失项的 pip 安装命令。
    在 main() 开头调用一次即可，比 ImportError 更早给出可读提示。
    """
    import importlib.util

    status: Dict[str, bool] = {}
    missing: List[str] = []
    for module, pkg in _DEPENDENCIES.items():
        ok = importlib.util.find_spec(module) is not None
        status[module] = ok
        if not ok:
            missing.append(pkg)
    if missing:
        print("[vauto] 缺少依赖: " + ", ".join(sorted(set(missing))))
        print("[vauto] 请执行: pip install " + " ".join(sorted(set(missing))))
    return status

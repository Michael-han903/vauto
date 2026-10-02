# -*- coding: utf-8 -*-
"""
vauto.errors —— 全局共用的异常类型（放在独立模块以避免循环导入）

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

为什么单独一个模块：
    AbortedByUser 同时被 safety（急停开关）和 input_sim（动作执行）使用。
    若各模块各自定义一份，`except AbortedByUser` 只会命中其中一份，
    另一份会漏网（这是很难查的 bug）。因此这里只定义一次，两边都从这里导入。
    vauto.AbortedByUser 与 vauto.safety.AbortedByUser、vauto.input_sim.AbortedByUser
    是同一个类对象。
"""

from __future__ import annotations

__all__ = ["AbortedByUser"]


class AbortedByUser(RuntimeError):
    """用户按下急停键（默认 F1），当前动作被主动中止。"""

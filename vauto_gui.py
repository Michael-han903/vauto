# -*- coding: utf-8 -*-
"""
vauto 图形控制台 —— 把设置、启动、状态监测都放进一个窗口（打包成 exe 的主入口）。

两种用法：
  * 双击 vauto.exe            → 打开这个控制台窗口；
  * vauto.exe --phase farm …  → 当命令行用：参数原样透传给 run_vauto.main。

安全取舍（沿用项目约定）：
  * 「真的按键」不勾 = dry-run（只判不按），第一次务必先这样跑一遍；
  * 勾上「真的按键」启动前会二次确认；运行中 F1 / [停止] 立即中止并释放按键。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

# ---- 打包成 exe 后的路径处理：工作目录 = exe 所在目录（logs/ 就写在它旁边）----
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).resolve().parent
    try:
        os.chdir(_exe_dir)
    except Exception:
        pass
    _bundle = getattr(sys, "_MEIPASS", None)
    if _bundle:
        sys.path.insert(0, str(_bundle))

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

# 打包后控制台默认走 ANSI（GBK）代码页 → 中文日志会变乱码、个别字符直接抛
# UnicodeEncodeError。统一切成 UTF-8（errors=replace 兜底，绝不因为一个字符崩掉）。
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    # 命令行模式：任何参数 → 透传给 run_vauto（--list / --selftest / --phase … 都能用）
    if len(sys.argv) > 1:
        from run_vauto import main as cli_main
        return cli_main()
    # 无参数（双击）→ 图形控制台
    from flow.console import Launcher
    app = Launcher()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

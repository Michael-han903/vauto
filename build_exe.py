# -*- coding: utf-8 -*-
"""把 vauto 打包成 Windows exe（PyInstaller onedir）。

用法：
    py -3.14 build_exe.py

产物（dist/vauto/ 整个文件夹拷走就能用）：
    vauto.exe            ← 双击 = 图形控制台；带参数 = 命令行（--list / --selftest / --phase …）
    templates/           ← 判据模板（exe 内也打包了一份；这份是给你手工改阈值用的）
    logs/                ← 运行日志与证据图会写在这里
    docs/                ← 顺手带一份运行手册
    启动说明.txt

注意：dist 文件夹建议放在**可写目录**（如桌面/D盘），别放 C:\\Program Files 下。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

README = """《地平线6》自动化控制台 —— 使用说明
=====================================

双击 vauto.exe：
    打开图形控制台。左边选目标窗口（先点「刷新」），
    选阶段 / 轮数 / 车数 / 循环，勾「自动进赛事」；
    第一次先**不要**勾「真的按键」，演练一遍看看流程对不对；
    确认无误后勾上「真的按键」再启动（启动时会二次确认）。

运行中：
    F1       = 急停（任何时候，会立刻释放所有按下的键）
    「停止」 = 界面上的按钮，等同 F1
    目标窗口不在前台时，全部动作会自动暂停（切回去就继续）

命令行模式（可选）：
    vauto.exe --list                     列窗口
    vauto.exe --selftest                 离线自检（不碰屏幕）
    vauto.exe --title "Forza Horizon 6" --phase both --rounds 4 --cars 0 --cycles 2 --enter-event --live

建议：
    * 把本文件夹放在可写位置（桌面 / D盘），日志证据写在 logs/ 里；
    * 想更保险地防黑屏，可在终端里跑一次：
          powercfg /change standby-timeout-ac 0
          powercfg /change monitor-timeout-ac 0

免责声明：本工具仅用于算法学习。用于第三方软件可能违反其用户许可协议（EULA），
并可能触发风控/反作弊，存在账号风险 —— 请自行评估，建议不要使用主账号。
"""


def run(cmd) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.check_call([str(c) for c in cmd], cwd=str(ROOT))


def main() -> int:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--onedir", "--console", "--name", "vauto",
         "--add-data", "templates;templates",
         "--add-data", "logs/replay;logs/replay",
         "--add-data", "golden_frames;golden_frames",   # 离线自检的金标帧
         "--hidden-import", "win32timezone",
         # vauto/flow 里有**动态导入**（importlib 之类）→ 静态分析抓不全，必须整包收集，
         # 否则运行时报 ModuleNotFoundError: No module named 'vauto.errors'（实测踩过）
         "--collect-submodules", "vauto",
         "--collect-submodules", "flow",
         str(ROOT / "vauto_gui.py")])

    dist = ROOT / "dist" / "vauto"
    if not (dist / "vauto.exe").exists():
        print("[!] 没找到 dist/vauto/vauto.exe —— 打包失败，看上面日志")
        return 1

    # 模板放一份到 exe 旁边（方便手工改 manual_thresholds.json 做临时试验）
    shutil.copytree(ROOT / "templates", dist / "templates", dirs_exist_ok=True)
    (dist / "logs").mkdir(exist_ok=True)
    # 离线自检的回放数据：--add-data 只把它打进 _internal/，而 offline_replay 是按
    # **cwd 相对**读 logs/replay/*.jsonl 的 → 必须在 exe 旁边也放一份
    replay_src = ROOT / "logs" / "replay"
    if replay_src.is_dir():
        shutil.copytree(replay_src, dist / "logs" / "replay", dirs_exist_ok=True)
    gf = ROOT / "golden_frames"                      # 金标帧（--selftest 用；回放按 cwd 读）
    if gf.is_dir():
        shutil.copytree(gf, dist / "golden_frames", dirs_exist_ok=True)
    docs = dist / "docs"
    docs.mkdir(exist_ok=True)
    for f in ("运行手册.md", "业务实测要点.md", "标定报告.md"):
        src = ROOT / "docs" / f
        if src.exists():
            shutil.copy2(src, docs / f)
    (dist / "启动说明.txt").write_text(README, encoding="utf-8")

    print("\n✓ 打包完成：", dist / "vauto.exe")
    print("  整个", dist, "文件夹拷到可写位置，双击 vauto.exe 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

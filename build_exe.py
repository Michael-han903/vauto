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

# GitHub Actions 的 Windows 运行器默认 stdout 编码是 ANSI（charmap）→ 打印 "✓" 这类
# 非 ASCII 字符直接 UnicodeEncodeError 崩掉（2026-10-04 CI 实测踩过）。统一切 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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


def _wipe_dist() -> None:
    """先把旧的 dist/vauto 清干净。

    踩过的坑：残留目录（尤其拷过 1.1GB 金标帧的那种）会让 PyInstaller 的 --clean
    半路 WinError 5/32（"拒绝访问 / 另一个程序正在使用"）。删不掉就**改名让路**，
    绝不让一次旧残留把整次打包搞死。
    """
    import time as _t
    dist = ROOT / "dist" / "vauto"
    if not dist.exists():
        return
    shutil.rmtree(dist, ignore_errors=True)
    if dist.exists():
        try:
            dist.rename(dist.with_name(f"vauto_old_{int(_t.time())}"))
            print("[i] 旧 dist/vauto 删不掉（被占用？）→ 已改名让路")
        except Exception as exc:
            print(f"[!] 旧 dist/vauto 既删不掉也改不了名：{exc}")
            print("    请关掉正在运行的 vauto.exe / 资源管理器窗口后再试。")
            raise


def _stash_user_data() -> Path | None:
    """重打包前把**用户数据**挪到旁边暂存，打完放回。

    dist/vauto 是"解压即用"的完整目录，用户会在里面攒东西：
      * logs/                  —— 运行日志、证据图、ledger 账本（累计时长/解锁台数）
      * vauto_config.json      —— 控制台「高级设置」里存过的配置
    PyInstaller 会整目录重建 → 不保住这些的话，一次"换新 exe"就把记录清光了。
    """
    dist = ROOT / "dist" / "vauto"
    keep = ROOT / "dist" / "_vauto_keep"
    if not dist.exists():
        return None
    moved = False
    for name in ("logs", "vauto_config.json"):
        src = dist / name
        if not src.exists():
            continue
        dst = keep / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
            shutil.move(str(src), str(dst))
            moved = True
            print(f"  [保留] 先暂存用户数据：{name}")
        except Exception as exc:
            print(f"  [!] 用户数据 {name} 暂存失败（继续打包）：{exc}")
    return keep if moved else None


def _restore_user_data(keep: Path | None) -> None:
    if not keep or not keep.exists():
        return
    dist = ROOT / "dist" / "vauto"
    for name in ("logs", "vauto_config.json"):
        src = keep / name
        if not src.exists():
            continue
        dst = dist / name
        try:
            if src.is_dir():
                if dst.exists():
                    shutil.copytree(src, dst, dirs_exist_ok=True)   # 与新建的 logs 合并
                    shutil.rmtree(src, ignore_errors=True)
                else:
                    shutil.move(str(src), str(dst))
            else:
                shutil.copy2(src, dst)
                src.unlink()
            print(f"  [保留] 已放回用户数据：{name}")
        except Exception as exc:
            print(f"  [!] {name} 放回失败：{exc}")
    try:
        keep.rmdir()
    except Exception:
        pass


def main() -> int:
    # 【2026-10-04 用户关切】重打包 = 整个 dist/vauto 重建 → 先把用户数据（logs/、配置）
    # 挪走暂存、打完放回，免得"换个新 exe"把累计时长/日志/设置一起清掉。
    keep = _stash_user_data()
    _wipe_dist()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--console", "--name", "vauto",
           "--add-data", "templates;templates"]
    # 回放数据（logs/replay）在仓库里是被 gitignore 的 → 别人 clone / CI 上不一定有，
    # 没有就**别加这个参数**（PyInstaller 遇到不存在的 add-data 路径会直接报错）。
    if (ROOT / "logs" / "replay").is_dir():
        cmd += ["--add-data", "logs/replay;logs/replay"]
    cmd += ["--hidden-import", "win32timezone",
            # vauto/flow 里有**动态导入**（importlib 之类）→ 静态分析抓不全，必须整包收集，
            # 否则运行时报 ModuleNotFoundError: No module named 'vauto.errors'（实测踩过）
            "--collect-submodules", "vauto",
            "--collect-submodules", "flow",
            str(ROOT / "vauto_gui.py")]
    run(cmd)

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
    # 金标帧：仓库里的完整素材 golden_frames/（1.1GB，gitignore，只在开发机上）优先；
    # 没有就退回**已入库的最小集** golden_frames_min/（6 张 JPEG，CI/别人 clone 也有）
    # —— 这样发布包里的 exe 也能跑 `--selftest`（以前 CI 打出来的包不能，因为素材不在仓库里）。
    # `--selftest` 走的 offline_replay 只需要 challenge_hud / challenge_result 两类、各取前 3 张。
    gf = ROOT / "golden_frames"
    if not (gf / "challenge_hud").is_dir():
        gf = ROOT / "golden_frames_min"
    if gf.is_dir():
        for sub in ("challenge_hud", "challenge_result"):
            sd = gf / sub
            if not sd.is_dir():
                continue
            picks = [p for p in sorted(sd.glob("*.png"))[:3]]
            picks += [p for p in sorted(sd.glob("*.jpg"))[:3 - len(picks)]]
            dst = dist / "_internal" / "golden_frames" / sub
            dst.mkdir(parents=True, exist_ok=True)
            for f in picks:
                shutil.copy2(f, dst / f.name)
    docs = dist / "docs"
    docs.mkdir(exist_ok=True)
    for f in ("运行手册.md", "业务实测要点.md", "标定报告.md"):
        src = ROOT / "docs" / f
        if src.exists():
            shutil.copy2(src, docs / f)
    (dist / "启动说明.txt").write_text(README, encoding="utf-8")
    _restore_user_data(keep)

    print("\n✓ 打包完成：", dist / "vauto.exe")
    print("  整个", dist, "文件夹拷到可写位置，双击 vauto.exe 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

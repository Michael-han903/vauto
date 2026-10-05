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

README = """基于挑战蓝图的地平线六刷技能点软件 —— 使用说明
=====================================

（代号 vauto；可执行文件就叫 vauto.exe，双击即用。）

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


def _running_vauto() -> list:
    """正在运行的 vauto 进程列表（用于"跑着就别打包"的前置检查）。

    注意：`tasklist` 的输出**不是 UTF-8**（中文 Windows 是 GBK/936）—— 用 text=True 解码会抛
    UnicodeDecodeError（实测踩过）。这里直接拿字节、按 ASCII 过滤，只认 "vauto.exe" 这个名字。
    """
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq vauto.exe", "/FO", "CSV"],
                           capture_output=True, timeout=20)
        out = (p.stdout or b"").decode("ascii", "ignore")
    except Exception:
        return []
    return [l.strip() for l in out.splitlines() if "vauto.exe" in l.lower()]


def main() -> int:
    # 【2026-10-05 修·实测踩的坑】vauto 正在跑时 dist/vauto 被占用：
    #   * PyInstaller 改名旧目录失败 → 中途退出；
    #   * **更糟**：用户数据已经"暂存走"、却没走到最后那步"放回" → 正在跑的进程写入路径没了
    #     （实测 logs 被落在 dist/_vauto_keep 里，用户当时就在跑）。
    # 所以：先查进程 —— 跑着就**什么也不动**直接退出。要打到别处用 --out。
    _argv = sys.argv[1:]
    _out = None
    if "--out" in _argv:
        _i = _argv.index("--out")
        _out = _argv[_i + 1] if _i + 1 < len(_argv) else None
    _run = _running_vauto()
    if _run and _out is None:
        print("[X] 检测到 vauto.exe 正在运行 → 拒绝打包（会打断正在跑的流程，目录也被占用）")
        for l in _run:
            print("    ", l)
        print("    处理办法：① 先关掉 vauto 再跑本脚本；")
        print("              ② 不想关：py -3.14 build_exe.py --out dist/vauto_next（打到另一个目录）")
        return 2

    # 【2026-10-04 用户关切】重打包 = 整个 dist/vauto 重建 → 先把用户数据（logs/、配置）
    # 挪走暂存、打完放回，免得"换个新 exe"把累计时长/日志/设置一起清掉。
    # 【2026-10-05】--out 模式不碰线上目录，所以不暂存、不清空。
    keep = None if _out else _stash_user_data()
    if _out is None:
        _wipe_dist()
    # 【2026-10-05】--out 的正确姿势：PyInstaller 的 --name 同时决定 **exe 名字**和
    # **输出文件夹名**，所以直接 --distpath dist/vauto_next 会打出 vauto_next.exe ✗。
    # 先打到 <out>_stage/vauto（结构跟线上完全一致），再把整个文件夹改成 <out>。
    _out_path = Path(_out).resolve() if _out else None
    _stage = (_out_path.parent / (_out_path.name + "_stage")) if _out_path else None
    if _stage is not None and _stage.exists():
        shutil.rmtree(_stage, ignore_errors=True)
    name = "vauto"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--console", "--name", name]
    if _stage is not None:
        cmd += ["--distpath", str(_stage)]
    cmd += ["--add-data", "templates;templates"]
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
    # 【2026-10-05】打包过程崩了也要把用户数据**放回去**（实测：中断后 logs 被落在暂存区，
    # 正跑着的进程写入路径直接没了 → 比"没打成包"严重得多）
    try:
        run(cmd)
    except BaseException:
        if keep is not None:
            _restore_user_data(keep)
        raise

    dist = (_stage / "vauto") if _stage is not None else (ROOT / "dist" / "vauto")
    if not (dist / "vauto.exe").exists():
        print(f"[!] 没找到 {dist / 'vauto.exe'} —— 打包失败，看上面日志")
        return 1
    if _stage is not None:
        # 从暂存目录搬到 --out 指定位置（目录结构完全一致，拷走即用）
        if _out_path.exists():
            shutil.rmtree(_out_path, ignore_errors=True)
        shutil.move(str(dist), str(_out_path))
        shutil.rmtree(_stage, ignore_errors=True)
        dist = _out_path

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
    if keep is not None:
        _restore_user_data(keep)

    print("\n✓ 打包完成：", dist / "vauto.exe")
    print("  整个", dist, "文件夹拷到可写位置，双击 vauto.exe 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

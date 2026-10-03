# -*- coding: utf-8 -*-
"""
run_vauto.py —— 业务运行入口（A 挑战循环 / B 批量刷技能点）

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。它会**真的操作你的鼠标键盘**，请先 --dry-run。

用法
----
    py -3.14 run_vauto.py --list                     # 列出可见窗口，找标题关键字
    py -3.14 run_vauto.py --title "Forza" --dry-run  # 只判不按（默认就是 dry-run）
    py -3.14 run_vauto.py --title "Forza" --phase farm --rounds 4 --live
    py -3.14 run_vauto.py --title "Forza" --phase spend --cars 6 --live
    py -3.14 run_vauto.py --selftest                 # 离线自检：不碰屏幕与键鼠

安全
----
* 默认 `--dry-run`：只抓帧、只打印"本应按什么"，绝不按键。
* 运行中随时按 **F1** 立即中止并释放所有按下的键；目标窗口不在前台时全部动作自动暂停。
* `--live` 才会真的按键。第一次请务必先 `--dry-run`，再 `--phase farm --rounds 1 --live`。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flow.config import RunConfig
from flow.runner import Runner, build_offline_stack, build_stack


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="vauto 业务运行入口（A 挑战循环 / B 刷技能点）")
    p.add_argument("--list", action="store_true", help="列出可见窗口后退出")
    p.add_argument("--title", type=str, default="", help="目标窗口标题关键字")
    p.add_argument("--phase", choices=["farm", "spend", "both"], default="both",
                   help="farm=只打挑战；spend=只刷技能点；both=先 farm 再 spend")
    p.add_argument("--rounds", type=int, default=4, help="farm 阶段跑几轮挑战（默认 4）")
    p.add_argument("--cars", type=int, default=6,
                   help="spend 阶段最多处理几台车（默认 6；**0 = 一直解锁到「技能点不足」**）")
    p.add_argument("--cycles", type=int, default=1,
                   help="主循环次数（默认 1）：[进赛事→跑 N 轮] → [B 解锁到不足] → [换回 22B] → 再来一遍")
    p.add_argument("--live", action="store_true", help="★ 真的按键 ★（不加则只判不按）")
    p.add_argument("--hotkey", type=str, default="f1", help="急停键（默认 f1）")
    p.add_argument("--config", type=str, default="", help="从 JSON 读配置")
    p.add_argument("--save-config", type=str, default="", help="把当前配置写出来供你改")
    p.add_argument("--no-car-check", action="store_true", help="跳过「当前车是 22B」的校验")
    p.add_argument("--enter-event", action="store_true",
                   help="跑 A 之前自动进赛事（主菜单 → 创意中心 → EventLab → 搜索共享代码）")
    p.add_argument("--probe", action="store_true",
                   help="判据探针：只抓帧打分、绝不按键；你自己翻页面，翻完 Ctrl+C 看汇总")
    p.add_argument("--probe-seconds", type=float, default=0.0,
                   help="探针自动跑多少秒后结束（0=手动 Ctrl+C）")
    p.add_argument("--selftest", action="store_true", help="离线自检：不碰屏幕与键鼠")
    p.add_argument("--event-code", type=str, default="",
                   help="挑战共享代码（默认用配置里的 161047605，可用 --save-config 看/改）")
    p.add_argument("--round-minutes", type=float, default=0.0,
                   help="挑战标称时长（分钟，默认 8）—— 用来算单轮超时与界面估时")
    p.add_argument("--no-keep-awake", action="store_true",
                   help="关闭「防息屏/防休眠」（默认开：长跑时阻止显示器关闭与系统睡眠）")
    p.add_argument("--no-gui", action="store_true",
                   help="关闭实跑状态小窗（默认开；缺 tkinter 会自动跳过）")
    return p.parse_args(argv)


def cmd_list() -> int:
    from vauto import enable_dpi_awareness, list_windows
    enable_dpi_awareness()
    print(f"{'hwnd':>10}  {'class':<28} title")
    for hwnd, title, cls in list_windows():
        print(f"{hwnd:>10}  {cls[:28]:<28} {title[:70]}")
    return 0


def cmd_selftest() -> int:
    """离线自检：装配检测器 + 跑 A 循环的逻辑（用录屏帧回放），不碰真屏幕与键鼠。"""
    from offline_replay import replay_farm          # 同目录
    return replay_farm(verbose=True)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        return cmd_list()
    if args.selftest:
        return cmd_selftest()

    cfg = RunConfig()
    if args.config:
        from flow.config import load_config
        cfg = load_config(args.config)
    cfg.title_key = args.title or cfg.title_key
    cfg.phase = args.phase
    cfg.rounds = args.rounds
    cfg.cars = args.cars
    cfg.cycles = args.cycles
    cfg.hotkey = args.hotkey
    cfg.dry_run = not args.live
    cfg.require_car_22b = not args.no_car_check
    cfg.enter_event = bool(args.enter_event)
    if args.event_code:
        cfg.share_code = args.event_code.strip()
    if args.round_minutes and args.round_minutes > 0:
        cfg.round_minutes = float(args.round_minutes)
    cfg.__post_init__()                     # 让 round_minutes 立即生效（推导单轮超时）

    if args.save_config:
        from flow.config import save_config
        print(f"[+] 配置已写出: {save_config(cfg, args.save_config)}")

    if not cfg.title_key:
        print("[!] 需要 --title（先跑 --list 看窗口标题）")
        return 2
    if not cfg.dry_run:
        print("=" * 78)
        print("★ --live：程序会真的操作鼠标键盘。F1 急停。建议先跑一遍 --dry-run。")
        print("=" * 78)
        time.sleep(2)

    stack = build_stack(cfg, title_key=cfg.title_key)
    print(f"[+] 目标窗口 hwnd={stack.hwnd}")
    try:
        from vauto.calib import calibration_table
        print(calibration_table(stack.calibration))       # 日志里留一份「当时用的什么阈值」
    except Exception as exc:                              # 打印失败不该挡住运行
        print(f"[!] 判据清单打印失败: {exc}")
    if args.probe:
        # 只抓帧打分，绝不按键：用来人工翻页面复核判据
        from flow.probe import probe_loop
        probe_loop(stack, seconds=(args.probe_seconds or None))
        stack.capture.close()
        return 0
    # 防息屏/防休眠（用户实测过"跑久了黑屏然后不运行"）：默认开
    ka = None
    if not args.no_keep_awake:
        from flow.keepawake import keep_awake
        ka = keep_awake(True)
        print("  [准备] 防息屏/防休眠：" +
              ("已启用 ✓（显示器常亮、系统不休眠）" if ka.active
               else "未生效 ✗（非 Windows 或被策略禁用）—— 可手动跑 powercfg 兜底"))

    with stack.stop:
        runner = Runner(stack, cfg)
        gui = None
        if not args.no_gui:
            try:
                from flow.gui import StatusWindow
                gui = StatusWindow(
                    lambda: {**runner.status,
                             "cars_done": runner.stats.get("cars_done", 0),
                             "rounds_done": runner.stats.get("rounds", 0)},
                    t0=runner._t0)
                ok = gui.start()
                print("  [准备] 状态小窗：" + ("已打开 ✓" if ok else "起不来（缺 tkinter）→ 继续无 GUI"))
                if ok:
                    time.sleep(0.6)   # 让窗口先落地；随后流程会把游戏切回前台（本窗口不抢焦点）
            except Exception as exc:
                print(f"  [i] GUI 跳过: {exc}")
        try:
            runner.run()
        finally:
            if gui is not None:
                gui.close()
            if ka is not None:
                ka.stop()
    stack.capture.close()
    if gui is not None:
        # 【Tk 收尾·已知问题】线程里的 Tk 在解释器 finalize 阶段必报
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread"，还会把退出码弄成非 0。
        # 此时所有清理都已完成（GUI 已 join、按键已释放、采集已关闭）→ 直接干净退出。
        import os as _os
        import sys as _sys
        _sys.stdout.flush()
        _sys.stderr.flush()
        _os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

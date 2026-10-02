# -*- coding: utf-8 -*-
"""
demo_skeleton.py —— 工具层调用骨架（业务逻辑全部为 TODO 占位）

本原型仅用于算法学习。若用于第三方软件，可能违反该软件用户许可协议（EULA），
并可能触发风控 / 反作弊，存在账号风险。

跑法：
    python demo_skeleton.py --list                  # 列出当前可见窗口，找 hwnd
    python demo_skeleton.py --title "记事本"         # 对标题含「记事本」的窗口跑骨架
    python demo_skeleton.py --title "XXX" --dry-run  # 只抓帧+匹配，不模拟输入（推荐先这样验）

安全须知：
    * 运行期间随时按 F1 立即中止（会释放所有按下的键）。
    * 目标窗口不在前台时循环自动暂停，不会盲点。
    * 第一次运行请务必先 --dry-run，并确认匹配到的坐标是你要的位置。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from vauto import (
    AbortedByUser,
    EmergencyStop,
    FocusGuard,
    Humanizer,
    InputSimulator,
    NotForeground,
    TemplateMatcher,
    TimingProfile,
    WindowCapture,
    WindowUnavailable,
    client_to_screen,
    draw_matches,
    enable_dpi_awareness,
    find_window_by_title,
    list_windows,
    record_window,
    save_image,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="vauto 工具层调用骨架（仅算法学习）")
    p.add_argument("--list", action="store_true", help="列出可见窗口 (hwnd / class / title) 后退出")
    p.add_argument("--title", type=str, default="", help="目标窗口标题关键字（子串匹配）")
    p.add_argument("--template", type=str, default="templates/target.png", help="模板图片路径")
    p.add_argument("--threshold", type=float, default=0.87, help="模板匹配置信阈值 0~1")
    p.add_argument("--scales", type=str, default="1.0", help="多尺度，逗号分隔，如 0.95,1.0,1.05")
    p.add_argument("--hotkey", type=str, default="f1", help="急停热键，默认 f1")
    p.add_argument("--seed", type=int, default=None, help="随机种子（复现实验用）")
    p.add_argument("--dry-run", action="store_true", help="只抓帧+匹配并保存调试图，不做任何输入仿真")
    p.add_argument("--interval", type=float, default=0.35, help="主循环两轮之间的基准间隔（秒）")
    p.add_argument("--max-loops", type=int, default=0, help="最多跑多少轮后自动退出（0=一直跑到急停/Ctrl+C）")
    p.add_argument("--record", type=str, default="", metavar="SCENE",
                   help="录制模式：从目标窗口抓取 --frames 帧原始画面到 golden_frames/<SCENE>/（不做任何输入，按 F1 提前停止）")
    p.add_argument("--frames", type=int, default=30, help="--record 模式的录制帧数（默认 30）")
    p.add_argument("--rec-interval", type=str, default="0.3,0.8", help="--record 两帧间隔 (min,max) 秒")
    return p.parse_args(argv)


def cmd_list_windows() -> None:
    print(f"{'hwnd':>10}  {'class':<28} title")
    for hwnd, title, cls in list_windows():
        print(f"{hwnd:>10}  {cls[:28]:<28} {title[:70]}")


def build_stack(title_key: str, template: str, threshold: float, scales, seed, hotkey,
                dry_run: bool, interval: float = 0.35):
    """组装工具层（这里没有任何业务判断，只是把组件接起来）。"""
    hwnd = find_window_by_title(title_key)
    if hwnd is None:
        print(f"[!] 找不到标题包含 {title_key!r} 的窗口。先跑  python demo_skeleton.py --list")
        return None
    print(f"[+] 目标窗口 hwnd={hwnd}  title={title_key!r}")

    capture = WindowCapture(hwnd, client_only=True)     # 抓客户区：匹配坐标 == 客户区坐标
    matcher = TemplateMatcher(grayscale=True, threshold=threshold)

    profile = TimingProfile()                           # 想调手速就改这里
    humanizer = Humanizer(profile=profile, seed=seed)

    sim = InputSimulator(humanizer=humanizer, move_before_click=True)

    # 急停：F1 -> 置位 event + 释放所有按下的键/按钮
    stop = EmergencyStop(hotkey=hotkey, on_trigger=sim.release_all, verbose=True)
    sim.emergency = stop

    guard = FocusGuard(hwnd, stop_event=stop.event, verbose=True)

    return {
        "hwnd": hwnd,
        "capture": capture,
        "matcher": matcher,
        "humanizer": humanizer,
        "sim": sim,
        "stop": stop,
        "guard": guard,
        "template": template,
        "scales": scales,
        "dry_run": dry_run,
        "interval": float(interval),
    }


def run_once(ctx: dict) -> bool:
    """
    一轮业务动作。返回值仅示意：True 表示本轮做了动作。

    ⚠️ 这里的所有 if/else 都是占位，业务逻辑请你自行实现。
    """
    stop = ctx["stop"]
    guard = ctx["guard"]
    capture = ctx["capture"]
    matcher = ctx["matcher"]
    sim = ctx["sim"]
    humanizer = ctx["humanizer"]
    hwnd = ctx["hwnd"]

    # 1) 焦点守卫：不在前台就暂停（后台不执行任何动作）
    if not guard.wait(timeout=None):
        return False

    # 2) 抓帧（客户区）
    try:
        frame = capture.grab()
    except WindowUnavailable as exc:
        print(f"[!] 抓帧失败: {exc}")
        return False

    # 3) 模板匹配
    hit = matcher.match_best(frame, ctx["template"], scales=ctx["scales"])
    if hit is None:
        # TODO: implement your own business logic here
        #   （例如：没找到目标时该做什么——等一会儿再来？切页？记日志？）
        humanizer.delay(0.2, 0.5, stop_event=stop.event)
        return False

    # 4) 调试图：把匹配框画出来存盘，方便肉眼核对阈值是否合适
    if ctx["dry_run"]:
        wpx, wpy = client_to_screen(hwnd, *hit.center)
        debug = draw_matches(frame, [hit], copy=True)
        out = Path("debug_frames")
        path = save_image(out / f"dryrun_{int(time.time()*1000)}.png", debug)
        print(f"[dry-run] 最佳匹配 {hit}  若执行点击 -> 屏幕坐标 ({wpx}, {wpy})")
        print(f"[dry-run] 调试图已存: {path}（绿框=命中位置，可据此核对阈值）")
        stop.event.wait(min(1.0, ctx["interval"]))
        return False

    # 5) 客户区坐标 -> 屏幕坐标，然后点击（内部自带贝塞尔轨迹 + 像素抖动）
    sx, sy = client_to_screen(hwnd, *hit.center)
    print(f"[*] 命中 {hit}  ->  屏幕坐标 ({sx}, {sy})")
    sim.click((sx, sy), button="left")

    # TODO: implement your own business logic here
    #   （例如：点击后应该等待什么画面出现、要不要按键、要不要记录统计）

    return True


def main(argv=None) -> int:
    args = parse_args(argv)

    mode = enable_dpi_awareness()
    print(f"[+] DPI 感知模式: {mode}")

    if args.list or not args.title:
        cmd_list_windows()
        if not args.title:
            print("\n用法示例: python demo_skeleton.py --title \"窗口标题关键字\" --dry-run")
        return 0

    # ---- 录制模式：只抓帧存盘，不做任何输入模拟 ----
    if args.record:
        hwnd = find_window_by_title(args.title)
        if hwnd is None:
            print(f"[!] 找不到标题包含 {args.title!r} 的窗口。先跑  python demo_skeleton.py --list")
            return 2
        try:
            lo, hi = (float(v) for v in args.rec_interval.split(",")[:2])
        except ValueError:
            lo, hi = 0.3, 0.8
        print(f"[+] 录制模式：目标 hwnd={hwnd}  场景={args.record!r}  "
              f"帧数={args.frames}  间隔=({lo}, {hi})s（按 F1 提前停止）")
        stop = EmergencyStop(hotkey=args.hotkey, verbose=True)
        with stop:
            res = record_window(
                hwnd, args.record, n_frames=args.frames, interval=(lo, hi),
                stop_event=stop.event,
                on_frame=lambda i, p: print(f"  [frame {i + 1}] {p}"),
            )
        print(res.summary())
        return 0

    scales = tuple(float(s) for s in args.scales.split(",") if s.strip())
    ctx = build_stack(args.title, args.template, args.threshold, scales,
                      args.seed, args.hotkey, args.dry_run, args.interval)
    if ctx is None:
        return 2

    stop: EmergencyStop = ctx["stop"]
    humanizer: Humanizer = ctx["humanizer"]

    iteration = 0
    try:
        with stop:                       # 启动全局 F1 监听；退出时自动关闭
            while not stop.triggered:
                if args.max_loops and iteration >= args.max_loops:
                    print(f"[+] 达到 --max-loops={args.max_loops}，正常退出")
                    break
                try:
                    run_once(ctx)
                except AbortedByUser:
                    break                # 急停：干净退出
                except NotForeground as exc:
                    print(f"[paused] {exc}")

                # 需求 e：每 N 次循环随机短暂休息
                humanizer.maybe_rest(iteration, stop_event=stop.event)
                # 主循环基准间隔也做随机化，避免恒定周期
                humanizer.delay(args.interval * 0.6, args.interval * 1.6, stop_event=stop.event)
                iteration += 1
    except KeyboardInterrupt:
        print("\n[Ctrl+C] 手动中断")
    finally:
        ctx["sim"].release_all(quiet=False)
        ctx["capture"].close()
        print(f"[+] 退出。共执行 {iteration} 轮，暂停累计 {ctx['guard'].paused_total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
record_scenes.py —— 引导式素材录制器（第 1 步落地工具）

做什么
------
把「录哪些素材、每个画面怎么录」变成一步一步的引导流程：
  1. 游戏切到【某个画面】-> 回终端按回车 -> 自动录 N 帧到 golden_frames/<场景>/
  2. 全部场景录完自动结束；任何时候按 F1 提前中止。

素材清单（内置，对应业务流程图里的 UI 元素）
------------------------------------------
    challenge_result    挑战结算面板（含「重试」按钮）      <- A 流程核心
    challenge_hud       挑战进行中画面（HUD/计时器）
    challenge_loading   加载/转场遮罩（如有）
    popup_no_resource   「资源不足」弹窗                   <- B 流程退出信号
    garage_list         车库/车辆列表页
    car_mastery_page    车辆精通技能树页面
    node_inactive       未激活节点（灰色/未点亮）
    node_active         已激活节点（亮色/已点亮）

用法
----
    py -3.14 record_scenes.py --title "Forza Horizon 6"           # 录全部场景
    py -3.14 record_scenes.py --title "Forza Horizon 6" --only challenge_result   # 只补录一个
    py -3.14 record_scenes.py --list-scenes                        # 只看清单
    py -3.14 record_scenes.py --title X --frames 60               # 每个画面多录些帧

录制只做截图，不做任何输入模拟。按 F1 可提前停止当前场景/整个流程。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vauto import EmergencyStop, enable_dpi_awareness, find_window_by_title, list_windows, record_window

# 场景清单：(场景名, 中文说明, 建议帧数)
# 注意：实测没有转场/加载页面，challenge_loading 已移除。
SCENES = [
    ("challenge_result", "挑战结算面板（等动画完全结束、画面静止，且能看到【重试】按钮）", 40),
    ("challenge_hud", "挑战进行中画面（有 HUD/计时器，选画面稳定的时候）", 30),
    ("popup_no_resource", "「资源不足」弹窗（技能点不足时弹出的模态窗，取标题清晰的状态）", 20),
    ("garage_list", "车库/车辆列表页（能看到车辆列表即可）", 30),
    ("car_mastery_page", "某台车的【车辆精通】技能树页面（整页）", 30),
    ("node_inactive", "技能树上【未激活节点】的特写（把窗口拉近/放大到能看清节点状态）", 30),
    ("node_active", "技能树上【已激活节点】的特写（同上，找已点亮的节点）", 30),
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="引导式素材录制器（仅截图，无输入模拟）")
    p.add_argument("--title", type=str, default="", help="目标窗口标题关键字，如 \"Forza Horizon 6\"")
    p.add_argument("--frames", type=int, default=40, help="每个画面录制帧数（默认 40）")
    p.add_argument("--interval", type=str, default="0.4,0.9", help="两帧间隔 (min,max) 秒")
    p.add_argument("--only", type=str, default="", help="只录指定场景名（如 challenge_result）")
    p.add_argument("--list-scenes", action="store_true", help="打印场景清单后退出")
    return p.parse_args(argv)


def cmd_list_scenes() -> None:
    print(f"{'场景名':<22} 帧数  说明")
    print("-" * 80)
    for name, desc, frames in SCENES:
        print(f"{name:<22} {frames:<5} {desc}")


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_scenes:
        cmd_list_scenes()
        return 0

    if not args.title:
        print("[!] 需要 --title。先跑  py -3.14 record_scenes.py --list 看窗口标题")
        return 2

    mode = enable_dpi_awareness()
    print(f"[+] DPI 感知模式: {mode}")

    hwnd = find_window_by_title(args.title)
    if hwnd is None:
        print(f"[!] 找不到标题包含 {args.title!r} 的窗口。当前可见窗口：")
        for h, t, c in list_windows():
            print(f"    {h:>10}  {c[:24]:<26} {t[:60]}")
        return 2

    print(f"[+] 目标窗口 hwnd={hwnd}  title={args.title!r}")
    try:
        lo, hi = (float(v) for v in args.interval.split(",")[:2])
    except ValueError:
        lo, hi = 0.4, 0.9

    targets = [s for s in SCENES if not args.only or s[0] == args.only]
    if not targets:
        print(f"[!] 场景 {args.only!r} 不在清单里。可用场景：")
        cmd_list_scenes()
        return 2

    stop = EmergencyStop(hotkey="f1", verbose=True)
    recorded: list[str] = []
    with stop:
        for name, desc, _frames in targets:
            if stop.triggered:
                break
            frames = args.frames
            print("\n" + "=" * 72)
            print(f"【{name}】{desc}")
            print(f"  请把游戏切到这个画面，按回车开始录制 {frames} 帧（按 F1 跳过/中止）...")
            try:
                input()
            except EOFError:
                print("[!] 无法读取键盘输入，改用 3 秒倒计时后自动开始")
                import time
                for i in range(3, 0, -1):
                    print(f"  {i}...", flush=True)
                    time.sleep(1)
            if stop.triggered:
                print("[!] 已按 F1，中止整个流程")
                break
            try:
                res = record_window(
                    hwnd, name, n_frames=frames, interval=(lo, hi),
                    stop_event=stop.event,
                    on_frame=lambda i, p: print(f"  [frame {i + 1}] {Path(p).name}", end="\r", flush=True),
                )
                print(" " * 40, end="\r")
                print(res.summary())
                recorded.append(name)
            except Exception as exc:
                print(f"[!] 场景 {name} 录制失败: {exc}")

    print("\n" + "=" * 72)
    if recorded:
        print(f"[+] 完成，共录制 {len(recorded)} 个场景：{', '.join(recorded)}")
        print(f"    素材在 golden_frames/ 下，下一步用它们裁模板、标定阈值。")
    else:
        print("[+] 未录制任何场景（可能中途 F1 中止）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

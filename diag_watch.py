# -*- coding: utf-8 -*-
"""实跑时的"旁观记录器"：每 N 秒抓一帧游戏画面存进 logs/watch/。

为什么要它：用户 2026-10-03 要求"改进之后你自己调试、自己改" —— 我按键跑流程的同时，
这个脚本在旁边把画面拍成连续帧；跑完我用眼睛逐帧看，就能定位"它到底在哪一步做错了"，
不用再请用户复述现象。

**只读**：只调用 capture（BitBlt/PrintWindow），不发任何键鼠、不碰游戏状态，
可以和正式流程同时跑。用法：

    py -3.14 diag_watch.py --hwnd 2296572 --every 8 --minutes 15
"""
from __future__ import annotations

import argparse
import os
import time

import cv2

from vauto.capture import WindowCapture


def main() -> None:
    ap = argparse.ArgumentParser(description="实跑旁观记录器（只读抓帧）")
    ap.add_argument("--hwnd", type=int, required=True, help="游戏窗口句柄（先 run_vauto.py --list）")
    ap.add_argument("--every", type=float, default=8.0, help="抓帧间隔秒（默认 8）")
    ap.add_argument("--minutes", type=float, default=15.0, help="记录时长分钟（默认 15）")
    ap.add_argument("--out", default=os.path.join("logs", "watch"), help="输出目录")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    cap = WindowCapture(args.hwnd)
    t_end = time.time() + args.minutes * 60.0
    n = 0
    while time.time() < t_end:
        try:
            frame = cap.grab()
            n += 1
            path = os.path.join(args.out, f"f{n:04d}.jpg")
            cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            print(f"{time.strftime('%H:%M:%S')} {path} {frame.shape}", flush=True)
        except Exception as exc:                       # 游戏窗口短暂不可抓时不中断
            print("grab fail:", exc, flush=True)
        time.sleep(args.every)


if __name__ == "__main__":
    main()

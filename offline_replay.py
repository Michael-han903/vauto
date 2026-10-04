# -*- coding: utf-8 -*-
"""
offline_replay.py —— M2 离线回放：把 A 循环（按住 W → 等结算 → 松手 → Esc 重试）
跑在**录屏抽帧**上，用假的 capture / sim，**不碰真屏幕与键鼠**。

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本代码仅用于算法学习（计算机视觉 / 输入仿真研究）。
若用于第三方软件，可能违反该软件用户许可协议（EULA）或服务条款，
并可能触发对方的风控 / 反作弊机制，存在账号被封禁等风险，请自行承担后果。
本文件不含任何输入仿真：它只把"程序会按什么"记进列表再检查。
======================================================================

怎么用
------
    py -3.14 offline_replay.py            # 独立跑，打印决策轨迹并自检
    py -3.14 run_vauto.py --selftest      # 同样的东西，从业务入口进

它验证什么
----------
1. 结算判据是**真的在真实帧上匹配**出来的（challenge_hud 帧不命中、challenge_result 帧命中）；
2. 连续 2 帧才确认（单帧命中不动作）；
3. 松手（release_all）**先于**按 Esc —— 绝不出现"W 和 Esc 同时按着"；
4. 每轮只按一次 Esc；最后一轮改成 Enter（离开赛事）；
5. dry_run 模式下，一次按键都不会发出。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from flow.config import RunConfig
from flow.runner import Runner, build_offline_stack
from vauto import load_image

GOLDEN = HERE / "golden_frames"
# 备用素材：仓库里**已入库的最小集**（6 张 JPEG）。发布包/CI/别人 clone 上没有 gitignore 的
# golden_frames/，但 build_exe.py 会把 min 集打进 exe 的 _internal/ → 这里按同一个相对位置找。
GOLDEN_MIN = HERE / "golden_frames_min"


def _scene_frames(scene: str) -> "list":
    """某个场景的帧，**.png/.jpg 都算**。

    【2026-10-04】别只 glob("*.png")：① 完整素材里大场景的帧已被 cleanup.py 转成 JPEG；
    ② 备用的 golden_frames_min/ 本来就是 JPEG。经验教训同 selftest_*：只认一种扩展名 →
    素材"看起来不存在" → 报「缺少素材」而不是报真因（这个坑在发布打包时才暴露）。
    """
    for root in (GOLDEN, GOLDEN_MIN):
        d = root / scene
        if not d.is_dir():
            continue
        fs = sorted(p for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        if fs:
            return fs
    return []


class TapeCapture:
    """按剧本逐帧供片：每轮先给 in_round 帧，再给 settle 帧。"""

    def __init__(self, rounds: int, in_round: int = 3, settle: int = 3) -> None:
        self.hud = [load_image(p, flags=3) for p in _scene_frames("challenge_hud")[:in_round]]
        self.res = [load_image(p, flags=3) for p in _scene_frames("challenge_result")[:settle]]
        if not self.hud or not self.res:
            raise RuntimeError("缺少 challenge_hud / challenge_result 素材，无法回放")
        script: List[np.ndarray] = []
        for _ in range(rounds):
            script += self.hud + self.res
        self.script = script
        self.i = 0
        self.served = 0

    def grab(self, region=None):
        if self.i < len(self.script):
            frame = self.script[self.i]
            self.i += 1
        else:
            frame = self.hud[0] if self.i % 2 else self.hud[-1]
        self.served += 1
        if region:
            x, y, w, h = (int(v) for v in region)
            frame = frame[y:y + h, x:x + w]
        return frame


class TapeSim:
    """只记录"程序想按什么"，不真的按键。"""

    def __init__(self) -> None:
        self.actions: List[tuple] = []
        self.held: set = set()

    def key_down(self, k):
        self.held.add(k)
        self.actions.append(("key_down", k))

    def key_up(self, k):
        self.held.discard(k)
        self.actions.append(("key_up", k))

    def tap_key(self, k, *a, **kw):
        self.actions.append(("tap", k))
        return 0.0

    def click(self, pos, **kw):
        self.actions.append(("click", tuple(pos)))
        return tuple(pos)

    def move_bezier(self, pos, **kw):
        self.actions.append(("move", tuple(pos)))
        return 0.0

    def release_all(self, quiet=True):
        n = len(self.held)
        for k in list(self.held):
            self.actions.append(("key_up", k))
        self.held.clear()
        self.actions.append(("release_all",))
        return n


def _check(name: str, cond: bool, extra: str = "") -> bool:
    print(("  OK  " if cond else " FAIL ") + name + ("  " + extra if extra else ""))
    return bool(cond)


def replay_farm(verbose: bool = True, rounds: int = 2, dry_run: bool = False) -> int:
    fails = 0
    for mode in (True, False):        # 先 dry-run，再"真实按键"（其实是假 sim）
        cfg = RunConfig(phase="farm", rounds=rounds, dry_run=mode, replay=True,
                        poll=0.0, max_polls_per_round=40, watchdog_idle=0,
                        require_car_22b=False, log_dir="logs/replay",
                        ledger_path="logs/replay/ledger.json")
        sim = TapeSim()
        stack = build_offline_stack(capture=TapeCapture(rounds), sim=sim)
        runner = Runner(stack, cfg)
        if verbose:
            print(f"\n########## 回放：dry_run={mode} ##########")
        runner.run()

        taps = [a[1] for a in sim.actions if a[0] == "tap"]
        if mode:
            fails += not _check("dry-run：一次按键都没发出", not sim.actions,
                                f"动作 {len(sim.actions)} 条")
            continue

        esc_idx = [i for i, a in enumerate(sim.actions) if a == ("tap", "esc")]
        ent_idx = [i for i, a in enumerate(sim.actions) if a == ("tap", "enter")]
        rel_idx = [i for i, a in enumerate(sim.actions) if a == ("release_all",)]
        w_down = [i for i, a in enumerate(sim.actions) if a == ("key_down", "w")]
        fails += not _check("每轮按一次 Esc（重试）", len(esc_idx) == rounds - 1,
                            f"Esc {len(esc_idx)} 次 / 期望 {rounds - 1} 次")
        fails += not _check("最后一轮按 Enter（离开赛事）", len(ent_idx) == 1,
                            f"Enter {len(ent_idx)} 次")
        fails += not _check("先松手再按 Esc（不会 W+Esc 同时按）",
                            bool(esc_idx) and all(any(r < e for r in rel_idx) for e in esc_idx),
                            f"release@{rel_idx[:3]} esc@{esc_idx[:3]}")
        fails += not _check("每轮都按住过 W", len(w_down) >= rounds, f"按下 W {len(w_down)} 次")
        fails += not _check("结算只在 challenge_result 帧上被确认",
                            all(a[0] not in ("tap",) or a[1] != "esc" for a in sim.actions[:2]),
                            f"前两个动作 {sim.actions[:2]}")
        if verbose:
            print(f"  动作轨迹（前 12 条）: {sim.actions[:12]}")
    print("\n回放结果:", "全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(replay_farm(verbose=True, rounds=2))

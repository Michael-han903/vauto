# -*- coding: utf-8 -*-
"""
自检：flow/nav.py 的走格子策略
① 核对列表规则是否与用户实测描述一致
② 证明"光按 ↓ 会漏车"
③ 量出各按键策略在随机列表上的覆盖率（纯逻辑，不注入任何输入）
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\lziha\visual_auto_toolkit")

from flow.nav import GridModel, GridWalker

fails = []


def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------- #
print("== ① 规则一致性（按用户原话构造的用例） ==")
# 单辆车列：选中第 1 辆按 ↓ → 最近的非单辆车列的第 2 辆
g = GridModel((1, 3, 1, 2), prefer_right=True)
g.move("down")
ck("单辆列(第1辆) ↓ → 最近多辆列的第 2 辆", g.position == (1, 1), f"落到 {g.position} car#{g.car_id}")
# 两辆车列：选中第 2 辆按 ↓ → 最近的非两辆车列的第 3 辆
g = GridModel((2, 3, 1), col=0, row=1, prefer_right=True)
g.move("down")
ck("两辆列(第2辆) ↓ → 最近非两辆列的第 3 辆", g.position == (1, 2), f"落到 {g.position} car#{g.car_id}")
# 本列还有下一辆时，↓ 就是本列下一辆
g = GridModel((3, 3))
g.move("down")
ck("本列有下一辆时 ↓ = 本列下一辆", g.position == (0, 1), f"落到 {g.position}")
# → 是下一列
g = GridModel((3, 2))
g.move("right")
ck("→ = 下一列（行号夹到该列高度内）", g.position == (1, 0), f"落到 {g.position}")

print("\n== ② 光按 ↓ 会漏车 ==")
g = GridModel((1, 3, 1, 2))
visited = [g.car_id]
for _ in range(20):
    if not g.move("down"):
        break
    if g.car_id in visited:
        break
    visited.append(g.car_id)
ck("只按 ↓ 无法覆盖列表", len(visited) < g.total,
   f"走了 {len(visited)}/{g.total} 辆 → {visited}（漏掉的正是别的列的上半部分）")

print("\n== ③ 各按键策略覆盖率（随机列表 × 随机起点，每个策略 300 例） ==")
POLICIES = {
    "P1 ↓优先,→兜底": (("down",), ("right",), ("down", "right")),
    "P2 ↓优先,→兜底,↑→再试": (("down",), ("right",), ("down", "right"), ("up", "right")),
    "P3 →优先,↓兜底": (("right",), ("down",), ("left", "down")),
}


class FakeUI:
    """模拟游戏：press 改模型位置，car_changed 比较"上次观测到的车"。"""

    def __init__(self, grid: GridModel):
        self.grid = grid
        self.last = grid.car_id

    def press(self, key):
        self.grid.move(key)

    def car_changed(self):
        now = self.grid.car_id
        changed = now != self.last
        self.last = now
        return changed


budget = 24          # 一次 B 会话最多走多少步（真实场景：每会话只处理几台车）
print(f"  （步数预算 = {budget} 步，对应真实场景里「一次会话处理几台车」）")
for name, plan in POLICIES.items():
    cover, eff, full, worst = [], [], 0, 1.0
    for _ in range(300):
        ncol = random.randint(3, 6)
        heights = tuple(random.randint(1, 5) for _ in range(ncol))
        total = sum(heights)
        start_col = random.randrange(ncol)
        start_row = random.randrange(heights[start_col])
        grid = GridModel(heights, col=start_col, row=start_row,
                         prefer_right=random.random() < 0.5)
        ui = FakeUI(grid)
        walker = GridWalker(press=ui.press, car_changed=ui.car_changed,
                            key_plan=plan, max_fail=3, budget=budget)
        seen = {grid.car_id}
        while walker.step():
            seen.add(grid.car_id)
        r = len(seen) / total
        cover.append(r)
        eff.append(len(seen) / max(1, walker.steps))   # 每走一步"新脸"的比例
        worst = min(worst, r)
        if r >= 0.999:
            full += 1
    print(f"  {name:<22} 列表覆盖 {sum(cover)/len(cover)*100:5.1f}%   "
          f"步数效率(新脸/步) {sum(eff)/len(eff)*100:5.1f}%   100%覆盖 {full}/300")

print("\n== ④ 有步数预算时一定停得下来 ==")
stuck = 0
for _ in range(200):
    heights = tuple(random.randint(1, 4) for _ in range(random.randint(2, 6)))
    grid = GridModel(heights)
    ui = FakeUI(grid)
    w = GridWalker(press=ui.press, car_changed=ui.car_changed, max_fail=3, budget=15)
    n = 0
    while w.step():
        n += 1
        if n > 15:
            stuck += 1
            break
ck("任何配置下都在预算内停下", stuck == 0, f"超预算的情况 {stuck}/200")

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:5]}")
sys.exit(1 if fails else 0)

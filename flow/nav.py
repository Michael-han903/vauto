# -*- coding: utf-8 -*-
"""
flow.nav —— 「更换车辆」列表的走格子策略（业务层，刻意不放进 vauto 工具层）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本代码仅用于算法学习（计算机视觉 / 输入仿真研究）。
若用于第三方软件，可能违反该软件的用户许可协议（EULA）或服务条款，
并可能触发对方的风控 / 反作弊机制，存在账号被封禁等风险，请自行承担后果。
本层只做「看图 → 按键」的业务编排，不含内存读取、进程注入、DLL 注入、驱动加载、
网络通信、加解密、绕过检测等任何侵入式能力。请勿在其上添加此类功能。
======================================================================

用户实测的列表导航规则（2026-10-02，地平线 6 车库「更换车辆」/「我的车辆」）
----------------------------------------------------------------------
1. **列优先填充**：车辆按列从上往下排满一列，再排下一列；列高不固定。
2. `↓` = 本列下一辆。
3. `→` = 下一列。
4. 列走到底还按 `↓`，游戏会**跳到附近最近的非该高度列**的「第 (行号+1) 辆」：
   - 单辆车的列（选中第 1 辆）按 `↓` → 跳到最近的非单辆车列的**第 2 辆**；
   - 两辆车的列（选中第 2 辆）按 `↓` → 跳到最近的非两辆车列的**第 3 辆**。

由此得到的关键结论
------------------
**光按 `↓` 会漏车**（上例里第 2 列的第 1 辆就被跳过了）。
而且程序看得到的只有「车名区域像素」，看不到光标在第几列第几行，
所以**无法事先算出该按 `↓` 还是 `→`**。

因此本模块采取的策略是「不假设列高，每步都验证」：

    step():
      ① 按 ↓  → 车名区域变了？ 成功，返回 True
      ② 没变  → 按 →  → 变了？ 成功，返回 True
      ③ 都没变 → 再试一次 ①②（列跳转有时要二次触发）
      ④ 连续 max_fail 步都换不动 → 返回 False（认为到了列表边界）

配合「已解锁的车直接跳过」（见 flow/states 里的判据 `hint_unlock_all`），
即使偶尔回到做过的车也是无副作用的重访 —— 真正的 B 轮退出条件是**技能点不足弹窗**，
不是"把列表走完"，所以不需要一次遍历 100% 覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

__all__ = ["GridModel", "GridWalker"]

# 一个「走一步」的按键尝试序列（按顺序试，谁先让车变了就用谁）
DEFAULT_KEY_PLAN: Tuple[Tuple[str, ...], ...] = (("down",), ("right",), ("down", "right"))


# --------------------------------------------------------------------------- #
# 列表布局模型（纯逻辑，可离线单测；用于验证策略覆盖率）
# --------------------------------------------------------------------------- #
@dataclass
class GridModel:
    """
    列优先、列高可变的列表模型，实现上面第 2~4 条规则。纯逻辑，不碰输入。

    heights      各列的车辆数，例如 (1, 3, 1, 2)
    prefer_right 列到底跳转时，左右距离相同的情况下选哪边（游戏里未实测，默认右）
    clamp_right  按 → 到下一列时，若该列没有当前行号的车，是否夹到该列最后一辆
    """

    heights: Sequence[int]
    prefer_right: bool = True
    clamp_right: bool = True
    col: int = 0
    row: int = 0

    def __post_init__(self) -> None:
        self.heights = tuple(int(h) for h in self.heights)
        if not self.heights or any(h <= 0 for h in self.heights):
            raise ValueError(f"每列至少要有一辆车，收到 heights={self.heights}")
        self.col = max(0, min(self.col, len(self.heights) - 1))
        self.row = max(0, min(self.row, self.heights[self.col] - 1))

    # -- 位置查询 ---------------------------------------------------------- #
    @property
    def sizes(self) -> Tuple[int, ...]:
        return self.heights

    @property
    def total(self) -> int:
        return sum(self.heights)

    def index(self, col: Optional[int] = None, row: Optional[int] = None) -> int:
        """列优先序号（从 0 开始）：用于判断"是否已经走过这台车/走了几台"。"""
        c = self.col if col is None else col
        r = self.row if row is None else row
        return sum(self.heights[:c]) + r

    @property
    def position(self) -> Tuple[int, int]:
        return self.col, self.row

    @property
    def car_id(self) -> int:
        """当前位置的"车"标识（=列优先序号），模拟里用它判断"换车了没"。"""
        return self.index()

    # -- 按键 -------------------------------------------------------------- #
    def move(self, key: str) -> bool:
        """
        按一次键；返回是否真的换了车（False = 这一步游戏里不会有变化）。
        实现用户描述的四条规则。
        """
        key = key.lower()
        if key == "down":
            return self._down()
        if key == "up":
            return self._up()
        if key == "right":
            return self._side(+1)
        if key == "left":
            return self._side(-1)
        raise ValueError(f"不支持的键: {key!r}")

    def _down(self) -> bool:
        h = self.heights[self.col]
        if self.row + 1 < h:                      # 本列还有下一辆
            self.row += 1
            return True
        target_row = self.row + 1                 # 列到底：跳到最近的非该高度列
        cands = [c for c, hh in enumerate(self.heights) if hh > target_row and c != self.col]
        if not cands:
            return False
        cands.sort(key=lambda c: (abs(c - self.col), c if self.prefer_right else -c))
        self.col, self.row = cands[0], target_row
        return True

    def _up(self) -> bool:
        if self.row - 1 >= 0:
            self.row -= 1
            return True
        target_row = self.row - 1
        if target_row < 0:
            return False
        cands = [c for c, hh in enumerate(self.heights) if c != self.col]
        if not cands:
            return False
        cands.sort(key=lambda c: (abs(c - self.col), c if self.prefer_right else -c))
        self.col, self.row = cands[0], target_row
        return True

    def _side(self, step: int) -> bool:
        target = self.col + step
        if not (0 <= target < len(self.heights)):
            return False
        if step > 0:                              # → ：下一列
            nxt_row = self.row if self.heights[target] > self.row else self.heights[target] - 1
        else:                                     # ← ：上一列
            nxt_row = self.row if self.heights[target] > self.row else self.heights[target] - 1
        if not self.clamp_right and nxt_row != self.row:
            return False
        if (target, nxt_row) == (self.col, self.row):
            return False
        self.col, self.row = target, nxt_row
        return True


# --------------------------------------------------------------------------- #
# 运行期走格子器
# --------------------------------------------------------------------------- #
@dataclass
class GridWalker:
    """
    一步一步往前走，每一步都用「车名区域是否变化」验证。

    press        : Callable[[str], None]     按键（'down' / 'right' / 'up' / 'left'）
    car_changed  : Callable[[], bool]        与"上一步之前保存的基准帧"比较，车是否变了
    key_plan     : 每步依次尝试的按键序列
    max_fail     : 连续多少步都换不动就认为到边界
    verbose      : 打印每一步的调试信息
    """

    press: Callable[[str], None]
    car_changed: Callable[[], bool]
    key_plan: Tuple[Tuple[str, ...], ...] = DEFAULT_KEY_PLAN
    max_fail: int = 3
    budget: int = 0                 # >0 时最多走这么多步（防止在列表里无限打转）
    verbose: bool = False

    steps: int = 0
    fails: int = 0
    at_boundary: bool = False
    _trace: List[Tuple[int, str, bool]] = field(default_factory=list)

    # -- 主入口 ------------------------------------------------------------ #
    def step(self) -> bool:
        """
        前进一步。返回 True = 确实换了车；False = 停下（到边界，或步数预算用完）。

        终止条件有两个，缺一不可：
          * 连续 max_fail 步都换不动 → 认为到列表边界；
          * 步数达到 budget（若 >0）→ 认为本次会话该收尾了。
        之所以需要 budget：列表是列优先 + 列到底会跳列，程序看不到光标行列，
        光靠"换不动"是停不下来的（模拟里 200 个随机列表有 142 个会一直走下去）。
        真实业务里的主退出条件是「技能点不足弹窗」，budget 只是兜底。
        """
        if self.at_boundary:
            return False
        if self.budget and self.steps >= self.budget:
            self.at_boundary = True
            if self.verbose:
                print(f"[nav] 达到步数预算 {self.budget}，停止走格子")
            return False
        for plan in self.key_plan:
            for key in plan:
                self.press(key)
                if self.car_changed():
                    self.steps += 1
                    self.fails = 0
                    self._trace.append((self.steps, "+".join(plan), True))
                    if self.verbose:
                        print(f"[nav] 第 {self.steps} 步：{' + '.join(plan)} → 已换车")
                    return True
            if self.verbose:
                print(f"[nav] 试过 {' + '.join(plan)}，车没变")
        self.fails += 1
        self._trace.append((self.steps, "fail", False))
        if self.verbose:
            print(f"[nav] 第 {self.steps} 步失败（连续 {self.fails}/{self.max_fail}）")
        if self.fails >= self.max_fail:
            self.at_boundary = True
        return False

    def rewind_hint(self) -> None:
        """走到边界后想"回到列表头"时用：目前只重置内部状态，实际回头的按键由业务层决定。"""
        self.at_boundary = False
        self.fails = 0

    def reset(self) -> None:
        self.steps = 0
        self.fails = 0
        self.at_boundary = False
        self._trace.clear()

    @property
    def trace(self) -> Tuple[Tuple[int, str, bool], ...]:
        return tuple(self._trace)

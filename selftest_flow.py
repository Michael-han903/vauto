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

print("\n== ⑤ 换车验证信号：名条判不出来，列表区域判得出来（真实录屏帧） ==")
# 【这是第一次真跑失败的根因】原来用左上角「当前车辆」名条做"车换了吗"的判据，
# 但实测：光标在「我的车辆」列表里移动时，名条**完全不变**（它显示当前驾驶的车）。
# 用 golden_frames/garage_list 里 44 张连续帧（用户当时正在按方向键）验证：
#   名条均差全为 0.00；列表区域分块最大差 18~152（阈值 6.0 有 3 倍余量）。
_gf = Path(__file__).resolve().parent / "golden_frames" / "garage_list"
_frames = sorted(_gf.glob("*.png")) if _gf.is_dir() else []
if len(_frames) < 5:
    ck("车库列表帧可用（golden_frames/garage_list）", False, f"只有 {len(_frames)} 张")
else:
    import cv2
    import numpy as np
    from vauto import block_max_abs_diff, mean_abs_diff

    def _load(p):
        return cv2.imdecode(np.fromfile(str(p), dtype="uint8"), cv2.IMREAD_COLOR)

    def _crop(im, r):
        x, y, w, h = r
        return im[y:y + h, x:x + w]

    STRIP, WATCH, THR = (68, 48, 1356, 136), (0, 400, 3840, 1520), 6.0
    ims = [_load(p) for p in _frames]
    strip = [mean_abs_diff(_crop(a, STRIP), _crop(b, STRIP)) for a, b in zip(ims, ims[1:])]
    watch = [block_max_abs_diff(_crop(a, WATCH), _crop(b, WATCH)) for a, b in zip(ims, ims[1:])]
    ck("名条在光标移动时几乎不变（旧判据必然失败）", max(strip) < 1.0,
       f"最大均差 {max(strip):.2f}（{len(strip)} 对相邻帧）")
    ck("列表区域分块最大差稳定超过阈值（新判据可用）",
       min(watch) > THR * 2, f"最小 {min(watch):.2f} / 阈值 {THR}（最大 {max(watch):.2f}）")

print("\n⑥ 静态检查：runner.py 里按名字取判据的地方，名字都必须在装配名单里")
# 血泪（2026-10-02）：car_tile_22b 只加进了标定表、忘了加进 runner.py 顶部的 DETECTORS →
# 换回 22B 的扫描循环第一行 self.s.dets["car_tile_22b"] 直接 KeyError →
# 程序带着未处理的 traceback 退出，日志里只剩 release_all 收尾（"不知道怎么卡住了"）。
import re
from flow.runner import DETECTORS, OPTIONAL_DETECTORS

_src = Path(r"C:\Users\lziha\visual_auto_toolkit\flow\runner.py").read_text(encoding="utf-8")
_used = set()
for _pat in (r'self\.s\.dets\["([a-z0-9_]+)"\]',
             r'self\.s\.dets\.get\("([a-z0-9_]+)"',
             r'(?:observe|_probe_loc|_wait_for|_wait_gone)\("([a-z0-9_]+)"',
             r'click_match\("([a-z0-9_]+)"'):
    _used |= set(re.findall(_pat, _src))
_used -= {"car_current_menu"}          # 可选判据，缺了也能跑
_known = set(DETECTORS) | set(OPTIONAL_DETECTORS)
_missing = sorted(_used - _known)
ck("runner.py 用到的判据都装配了", not _missing,
   f"用到 {len(_used)} 个 | 装配 {len(_known)} 个" + (f" | 缺 {_missing}" if _missing else ""))

print("\n⑦ 换车指纹：从「选中格车名」认出是哪台车（治『来来回回换同一两辆』）")
# 2026-10-02 用户实测："你的换车逻辑有问题，来来回回就两辆车在那里换，而且太慢了"。
# 原因：change_car() 只"走一格就上车"，选中的车弄过没有完全看不出来。
try:
    import glob as _glob
    import tempfile
    import cv2
    import numpy as _np
    from flow.config import RunConfig
    from flow.runner import Runner, build_offline_stack

    def _load_img(p):
        return cv2.imdecode(_np.fromfile(str(p), dtype="uint8"), cv2.IMREAD_COLOR)

    _cfg = RunConfig()
    _cfg.log_dir = tempfile.mkdtemp(prefix="vauto_selftest_")
    _r = Runner(build_offline_stack(), _cfg)
    _p = sorted(_glob.glob(r"C:\Users\lziha\visual_auto_toolkit\golden_frames\current_car_22b_garage\*.png"))[0]
    _im = _load_img(_p)
    _fp = _r._selected_tile_title(_im)
    ck("能在「我的车辆」里认出选中格的车名", _fp is not None,
       f"指纹尺寸 {None if _fp is None else _fp.shape}")

    if _fp is not None:
        def _crop(dx=0, dy=0):
            return cv2.cvtColor(_im[416 + dy:508 + dy, 818 + dx:1418 + dx], cv2.COLOR_BGR2GRAY)

        _same = max(_r._fp_diff(_fp, _crop(dx=dx, dy=dy))
                    for dx in (-4, -2, 0, 2, 4) for dy in (-2, 0, 2))
        _others = {lab: _r._fp_diff(_fp, _crop(dx=dx, dy=dy)) for lab, dx, dy in
                   (("124 SPIDER", 695, 0), ("695 BIPOSTO", 0, 522),
                    ("FIAT 131", 695, 522), ("#6165 TRUCK", 1390, 0))}
        print(f"       同车（±4 像素错位）最大差 {_same:.2f} | 不同车最小差 {min(_others.values()):.2f}"
              f" | 阈值 {_cfg.fp_same_tol}")
        ck("同一台车：错位 ±4 像素内仍判为同一台", _same < _cfg.fp_same_tol, f"{_same:.2f}")
        ck("不同车：差异远大于阈值", min(_others.values()) > _cfg.fp_same_tol * 2,
           f"最小 {min(_others.values()):.2f}（{min(_others, key=_others.get)}）")
    # 录屏帧里也要认得出来（框必须完整才认，滚动中间态宁可不认）
    _fs = sorted(_glob.glob(r"C:\Users\lziha\visual_auto_toolkit\golden_frames\garage_list\*.png"))
    _ok = sum(1 for _f in _fs[:12]
              if _r._selected_tile_title(_load_img(_f)) is not None)
    ck("录屏车库帧里也认得出选中格", _ok >= 6, f"{_ok}/12 帧")
except Exception as _e:
    ck("换车指纹自检可运行", False, repr(_e))

print("\n⑧ 车格/♥ 检测：用用户实机截图验证（B 段选车靠它）")
try:
    import os as _os
    import cv2 as _cv2
    _IMG = r"C:\Users\lziha\AppData\Local\Hermes Agent CN Desktop\data\hermes-home\images\upload_20261003_002623_16.png"
    _im2 = _cv2.imread(_IMG) if _os.path.isfile(_IMG) else None
    if _im2 is None:
        print("       跳过（找不到用户截图）")
    else:
        _cfg2 = RunConfig()
        _cfg2.log_dir = tempfile.mkdtemp(prefix="vauto_selftest2_")
        _r2 = Runner(build_offline_stack(), _cfg2)
        _tiles = _r2._grid_tiles(_im2)
        ck("能从实机截图里检出车格", len(_tiles) >= 8, f"{len(_tiles)} 个车格")
        if _tiles:
            _ws = [t[4] for t in _tiles]
            _hs = [t[5] for t in _tiles]
            print(f"       车格尺寸 {min(_ws)}~{max(_ws)} x {min(_hs)}~{max(_hs)}"
                  f"（实测基准 636~648 x 468~488）")
            _scores = [t[7] for t in _tiles if t[7] == t[7]]
            _nheart = sum(1 for t in _tiles if t[6])
            print(f"       ♥ 检出 {_nheart}/{len(_tiles)} 格"
                  f"（这张图上用户说都收藏过了）；♥ 分数 {min(_scores):.3f}~{max(_scores):.3f}")
            ck("该图（用户已全部收藏）每格都判为有 ♥", _nheart == len(_tiles),
               f"{_nheart}/{len(_tiles)}")
            # 反向：车格中部（没有♥的地方）不该被判成♥
            _t0 = _tiles[0]
            _mid = _im2[_t0[3] + 150:_t0[3] + 230, _t0[2] + 200:_t0[2] + 310]
            _det = _r2.s.dets["fav_heart"]
            _h = _r2.s.matcher.match_best(_mid, _det.template, threshold=-1.0)
            _midscore = float(getattr(_h, "score", float("nan"))) if _h else float("nan")
            ck("没有♥的区域不会被判成有♥", not (_midscore == _midscore and _midscore >= _det.threshold),
               f"车格中部得分 {_midscore:.3f} < 阈值 {_det.threshold}")
            _cur = _r2._cursor_cell(_im2, _tiles)
            ck("能认出光标在哪个车格", _cur == (0, 0), f"光标格 {_cur}（图上黄框在第 1 格）")
except Exception as _e:
    ck("车格/♥ 自检可运行", False, repr(_e))

print("\n⑨ 当前车辆（列表最左侧那格）必须被跳过")
# 2026-10-03 用户指出："新的 b 阶段逻辑怎么会去研究当前车辆呢？最左侧的车是当前车辆"
try:
    _IMG3 = r"C:\Users\lziha\AppData\Local\Hermes Agent CN Desktop\data\hermes-home\images\upload_20261003_003624_18.png"
    _im3 = _cv2.imread(_IMG3) if _os.path.isfile(_IMG3) else None
    if _im3 is None:
        print("       跳过（找不到用户截图）")
    else:
        _cfg3 = RunConfig()
        _cfg3.log_dir = tempfile.mkdtemp(prefix="vauto_selftest3_")
        _r3 = Runner(build_offline_stack(), _cfg3)
        _t3 = _r3._grid_tiles(_im3)
        _c3 = _r3._cursor_cell(_im3, _t3)
        ck("新截图：光标落在最左那格（= 当前车辆）", _c3 == (0, 0), f"光标格 {_c3}")
        _t00 = [t for t in _t3 if (t[0], t[1]) == (0, 0)]
        if _t00:
            ck("当前车辆那格在标准 ♥ 位置读不出♥（所以要靠 seed 跳过）",
               not _t00[0][6], f"score {_t00[0][7]:.3f} < 阈值 {_r3.s.dets['fav_heart'].threshold}")
            _fp0 = _r3._title_crop(_im3, _t00[0][2], _t00[0][3], _t00[0][4], _t00[0][5])
            _r3._seen_cars.append(_fp0)          # 模拟 change_car 的 seed
            _cands = [t for t in _t3 if not t[6]
                      and not _r3._fp_seen(_r3._title_crop(_im3, t[2], t[3], t[4], t[5]))]
            ck("seed 之后当前车辆不再进候选", all((t[0], t[1]) != (0, 0) for t in _cands),
               f"候选 {len(_cands)} 个：{[(t[0], t[1]) for t in _cands]}")
        else:
            ck("新截图能检出最左那格", False, "没检出 (0,0)")
except Exception as _e:
    ck("当前车辆跳过自检可运行", False, repr(_e))

print("\n⑩ 鼠标用完归位到左上角（用户要求：悬停会改控件外观、干扰画面判据）")
try:
    _cfg5 = RunConfig()
    _cfg5.log_dir = tempfile.mkdtemp(prefix="vauto_selftest5_")
    _cfg5.dry_run = False            # 要真走按键/鼠标路径（离线的假 sim，不碰真机）
    _st5 = build_offline_stack()
    _r5 = Runner(_st5, _cfg5)
    _r5.click_client((1234, 567), "自检：点一下看看会不会归位")
    _acts = list(getattr(_st5.sim, "held", []))
    print(f"       离线假 sim 记到的动作: {_acts}")
    ck("点击之后鼠标被移回左上角", any(a[0] == "move" and tuple(a[1]) == tuple(_cfg5.park_at)
                                    for a in _acts), f"park_at={_cfg5.park_at}")
    ck("点击本身也发生了", any(a[0] == "click" for a in _acts))
    _cfg5.dry_run = True             # dry-run 下不该动鼠标
    _st5b = build_offline_stack()
    _r5b = Runner(_st5b, _cfg5)
    _n_before = len(list(getattr(_st5b.sim, "held", [])))
    _r5b.click_client((10, 10), "dry-run")
    _n_after = len(list(getattr(_st5b.sim, "held", [])))
    ck("dry-run 下不发任何鼠标动作", _n_after == _n_before, f"动作数 {_n_before} → {_n_after}")
except Exception as _e:
    ck("鼠标归位自检可运行", False, repr(_e))

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:5]}")
sys.exit(1 if fails else 0)

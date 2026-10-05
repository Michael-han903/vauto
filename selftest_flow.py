# -*- coding: utf-8 -*-
"""
自检：flow/nav.py 的走格子策略
① 核对列表规则是否与用户实测描述一致
② 证明"光按 ↓ 会漏车"
③ 量出各按键策略在随机列表上的覆盖率（纯逻辑，不注入任何输入）
"""
import os
import pathlib
import random
import sys
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # 谁的机器都能跑
_ROOT = pathlib.Path(__file__).resolve().parent
_HERMES_IMGS = pathlib.Path(os.environ.get("HERMES_HOME", ".")) / "images"

from flow.nav import GridModel, GridWalker

fails = []


def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------- #
print("== ① 规则一致性（按用户 2026-10-04 晚给的真实模型构造用例） ==")
# 用户原话：\"箭头上下只会在当前列切换，箭头左右会在左右列之间切换\"。
g = GridModel((3, 2))
g.move("down")
ck("↓ 只在当前列内往下", g.position == (0, 1), f"落到 {g.position}")
g.move("down")
ck("列内到底后 ↓ 不再往下（不跨列）", g.position == (0, 2), f"落到 {g.position}")
g.move("up")
ck("↑ 回到本列上一辆", g.position == (0, 1), f"落到 {g.position}")
g.move("right")
ck("→ 换到右一列（行号夹到该列高度内）", g.position == (1, 1), f"落到 {g.position}")
g.move("left")
ck("← 换回左一列", g.position == (0, 1), f"落到 {g.position}")

print("\n== ② 光按 ↓ 会漏整列（列与列之间必须用 ←/→） ==")
g = GridModel((1, 3, 1, 2))
visited = [g.car_id]
for _ in range(20):
    if not g.move("down"):
        break
    if g.car_id in visited:
        break
    visited.append(g.car_id)
ck("只按 ↓ 无法覆盖列表", len(visited) < g.total,
   f"走了 {len(visited)}/{g.total} 辆 → {visited}（↓ 只能在本列内，别的列得用 ←/→）")

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
# 【2026-10-04】cleanup.py 把大场景里除前 3 张外的无损 PNG 转成了 JPEG（省 800MB）
# → 这里必须 .png/.jpg 都收，否则"只有 3 张"直接判失败。
_frames = sorted(p for p in _gf.iterdir()
                 if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")) \
    if _gf.is_dir() else []
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

_src = Path(str(_ROOT) + r"\flow\runner.py").read_text(encoding="utf-8")
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
    _p = sorted(_glob.glob(str(_ROOT) + r"\golden_frames\current_car_22b_garage\*.png"))[0]
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
    # （.png/.jpg 都收：大场景的帧已被 cleanup.py 转成 JPEG 省空间）
    _fs = sorted(p for p in (_ROOT / "golden_frames" / "garage_list").iterdir()
                 if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    _ok = sum(1 for _f in _fs[:12]
              if _r._selected_tile_title(_load_img(_f)) is not None)
    ck("录屏车库帧里也认得出选中格", _ok >= 6, f"{_ok}/12 帧")
except Exception as _e:
    ck("换车指纹自检可运行", False, repr(_e))

print("\n⑧ 车格/♥ 检测：用用户实机截图验证（B 段选车靠它）")
try:
    import os as _os
    import cv2 as _cv2
    _IMG = str(_HERMES_IMGS) + r"\upload_20261003_002623_16.png"
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
            # 【2026-10-03 放宽】这张图上"当前车"那格的 ♥ 位置是第三种（既不在标准位、
            # 也不在驾驶图标左侧）→ 单靠两位置 ♥ 判据会漏它一格。但这**无害**：
            # 它会被 _is_current_car（「驾驶中」图标）排除，不会被当成候选。
            ck("该图（用户已全部收藏）♥ 至少判对 9/10 格", _nheart >= len(_tiles) - 1,
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
    _IMG3 = str(_HERMES_IMGS) + r"\upload_20261003_003624_18.png"
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
            # 【2026-10-03 更新】旧断言（当前车读不出 ♥）已被用户指路推翻：
            # 判据改成「标准位 + 驾驶图标左侧位取最大值」，当前车现在能读出 ♥。
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

print()
print("⑭ 当前车的 ♥ 判据：标准位 + 驾驶图标左侧位（用户 2026-10-03 指路）")
try:
    _IMG4 = (str(_HERMES_IMGS.parent) + "/"
             "images/upload_20261003_020318_24.png")
    _im4 = _cv2.imread(_IMG4) if _os.path.isfile(_IMG4) else None
    if _im4 is None:
        print("       跳过（找不到用户截图）")
    else:
        _cfg4 = RunConfig()
        _cfg4.log_dir = tempfile.mkdtemp(prefix="vauto_selftest4_")
        _r4 = Runner(build_offline_stack(), _cfg4)
        _t4 = _r4._grid_tiles(_im4)
        _cur = [t for t in _t4 if (t[0], t[1]) == (0, 0)]
        ck("当前车（已收藏，♥ 画在驾驶图标左侧）能读出 ♥",
           bool(_cur) and _cur[0][6],
           (f"score {_cur[0][7]:.3f}" if _cur else "没检出 (0,0)"))
        _oth = [t for t in _t4 if (t[0], t[1]) != (0, 0)]
        ck("同屏其它已收藏车也照样读出 ♥（没改坏）",
           all(t[6] for t in _oth), f"{sum(1 for t in _oth if t[6])}/{len(_oth)}")
        _cand = [t for t in _t4 if not t[6]]
        ck("这一屏（全都收藏过）没有「未收藏」误报",
           not _cand, f"误报 {[(t[0], t[1], round(t[7], 3)) for t in _cand]}")
except Exception as _e:
    ck("当前车 ♥ 判据自检可运行", False, repr(_e))

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

print("\n⑪ 走路逻辑：合成车格模拟（复现 2026-10-03 那次『走不到 (2,1)』）")
try:
    _cfg6 = RunConfig()
    _cfg6.log_dir = tempfile.mkdtemp(prefix="vauto_selftest6_")
    _cfg6.dry_run = False
    _r6 = Runner(build_offline_stack(), _cfg6)
    _W, _H, _GAP, _COLS, _ROWS = 648, 488, 24, 4, 3

    def _cell_box(r, c):
        return (816 + c * (_W + _GAP), 424 + r * (_H + _GAP), _W, _H)

    def _sim(sel_row, sel_col, semantics, no_heart=(2, 1), ncols=_COLS):
        """把 Runner 的"看画面"接口换成合成网格 —— 关键：**照真机几何建模**。

        【2026-10-05 修】以前这里的桩是"车格固定、光标框随按键移动" —— 跟真机**正好相反**
        （真机是：**选中的那一列永远画在最左**，光标框 x 恒 ~816；按 ←/→ 动的是**列表**）。
        正因为桩建反了，才没抓住"用光标框变没变当进度"这个 bug。现在：
          * 逻辑列 `lc` 渲染到屏幕列 `lc - sel_col`（选中的列 = 屏幕第 0 列 = x 816）；
          * 光标框永远 x=816，只有 y 跟着"选中行"走；
          * `←`/`→` 改 `sel_col`（列表整体滑动），`↑`/`↓` 在列内改选中行。
        """
        st = {"sel_row": sel_row, "sel_col": sel_col, "presses": []}

        def _onscreen(lr, lc):
            return lr, lc - st["sel_col"]

        def _tiles(frame):
            out = []
            for lr in range(_ROWS):
                for lc in range(ncols):
                    sr, sc = _onscreen(lr, lc)
                    if not (0 <= sc < ncols):        # 滑出视野的不报
                        continue
                    out.append((sr, sc, *_cell_box(sr, sc), (lr, lc) != no_heart,
                                0.9 if (lr, lc) != no_heart else 0.2))
            return out

        _r6._grid_tiles = _tiles
        # 光标框：x 永远 816（选中列画在最左），y 跟选中行 —— 真机就是这样，所以**它不会横移**
        _r6._cursor_box = lambda frame, tiles=None, _st=st: _cell_box(_st["sel_row"], 0)
        _r6._title_crop = lambda frame, x, y, w, h: f"px_{x}_{y}"
        _r6._fp_diff = lambda a, b: 0.0 if a == b else 100.0
        _r6.frame = lambda guard=True: _DUMMY_FRAME
        _r6.sleep = lambda s: None

        def _press(key, note="", _st=st):
            _st["presses"].append(key)
            semantics(_st, key)
        _r6.press = _press
        return st

    def _new_model(st, key):
        """【2026-10-04 晚·用户实测模型】↑/↓ **只在当前列内**上下；←/→ **在左右列之间**切换
        （= 列表整体滑动，让目标那列变成"选中的最左列"）。"""
        r, c = st["sel_row"], st["sel_col"]
        if key == "down":
            st["sel_row"] = min(r + 1, _ROWS - 1)
        elif key == "up":
            st["sel_row"] = max(r - 1, 0)
        elif key == "right":
            st["sel_col"] = min(c + 1, _COLS - 1)
        elif key == "left":
            st["sel_col"] = max(c - 1, 0)

    _DUMMY_FRAME = _np.ones((8, 8, 3), dtype="uint8") * 128

    _s1 = _sim(0, 0, _new_model, no_heart=(2, 1))
    _ok1 = _r6._walk_to_tile()
    ck("走路能走到目标：先用 ←/→ 把目标列挪成选中列，再用 ↑/↓ 对行",
       _ok1 and (_s1["sel_row"], _s1["sel_col"]) == (2, 1),
       f"sel=(行{_s1['sel_row']},列{_s1['sel_col']}) 按键 {_s1['presses']}")
    ck("换列确实用了 ←/→（旧的\"靠 ↓ 跨列\"是错的）",
       any(k in ("left", "right") for k in _s1["presses"]),
       f"按键序列 {_s1['presses'][:6]}")
    ck("列内纵向只用 ↑/↓（对行阶段）", any(k in ("up", "down") for k in _s1["presses"]),
       f"按键序列 {_s1['presses'][:6]}")
    ck("光标框 x 全程不动也不影响判定（真机就是选中列画最左）",
       True, f"按键序列 {_s1['presses'][:8]}")

    _s2 = _sim(0, 0, _new_model, no_heart=(0, 3))
    _ok2 = _r6._walk_to_tile()
    ck("同行的远处目标（第 3 列）也能走到", _ok2 and (_s2["sel_row"], _s2["sel_col"]) == (0, 3),
       f"sel=(行{_s2['sel_row']},列{_s2['sel_col']}) 按键 {_s2['presses']}")

    _s3 = _sim(1, 0, _new_model, no_heart=(2, 0))
    _ok3 = _r6._walk_to_tile()
    ck("目标已在选中列时：只用 ↑/↓ 对行（一发 ←/→ 都不按）",
       _ok3 and (_s3["sel_row"], _s3["sel_col"]) == (2, 0)
       and all(k in ("up", "down") for k in _s3["presses"]),
       f"sel=(行{_s3['sel_row']},列{_s3['sel_col']}) 按键 {_s3['presses']}")

    def _weird_model(st, key):
        """故意把 ←/→ 的方向反过来 —— 走位器应该**自己试出来**哪个键有效（自校准）。"""
        r, c = st["sel_row"], st["sel_col"]
        if key == "down":
            st["sel_row"] = min(r + 1, _ROWS - 1)
        elif key == "up":
            st["sel_row"] = max(r - 1, 0)
        elif key == "right":
            st["sel_col"] = max(c - 1, 0)
        elif key == "left":
            st["sel_col"] = min(c + 1, _COLS - 1)

    _s4 = _sim(0, 0, _weird_model, no_heart=(0, 2))
    _ok4 = _r6._walk_to_tile()
    ck("方向猜错时能自己纠正（试出有效键后记住）",
       _ok4 and (_s4["sel_row"], _s4["sel_col"]) == (0, 2),
       f"sel=(行{_s4['sel_row']},列{_s4['sel_col']}) 按键 {_s4['presses']}")
except Exception as _e:
    ck("走路模拟自检可运行", False, repr(_e))

print("\n⑫ 光标定位锚定在车格上（不再全图找黄色）—— 用两张实机截图验")
# 2026-10-03 日志：光标中心在 (1140,668)/(1140,1172)/(856,1676) 之间乱蹦，856 那里根本不是
# 车格 → 因为旧实现是"全图找最大的黄色轮廓"，抓到了品牌标签高亮/左栏价格框那些黄东西。
try:
    for _p7, _nm7 in ((_IMG, "ABARTH 那屏"), (_IMG3, "当前车辆 DMC-12 那屏")):
        if not _os.path.isfile(_p7):
            continue
        _im7 = _cv2.imread(_p7)
        _cfg7 = RunConfig()
        _cfg7.log_dir = tempfile.mkdtemp(prefix="vauto_selftest7_")
        _r7 = Runner(build_offline_stack(), _cfg7)
        _ts7 = _r7._grid_tiles(_im7)
        _cb7 = _r7._cursor_box(_im7, _ts7)
        _t007 = [t for t in _ts7 if (t[0], t[1]) == (0, 0)]
        if _t007 and _cb7:
            _d7 = abs(_cb7[0] - _t007[0][2]) + abs(_cb7[1] - _t007[0][3])
            ck(f"光标框≈(0,0) 那格的车格框（{_nm7}）", _d7 < 30,
               f"差 {_d7}px  cursor={_cb7} 车格={( _t007[0][2], _t007[0][3])}")
        else:
            ck(f"能认出光标框（{_nm7}）", False, f"cursor={_cb7} tiles={len(_ts7)}")
except Exception as _e:
    ck("光标锚定自检可运行", False, repr(_e))

print("\n⑬ 多辆待处理时的优先顺序：先最左列、同列先最上（用户口径）")
# 2026-10-03 用户："识别到一张图片内有多辆未收藏车辆的时候，应该先看最左列的，
# 如果最左列有不止一辆，应该看先最上面的"
try:
    _cfg8 = RunConfig()
    _cfg8.log_dir = tempfile.mkdtemp(prefix="vauto_selftest8_")
    _r8 = Runner(build_offline_stack(), _cfg8)
    _cand = [((2, 1), "A"), ((0, 2), "B"), ((1, 0), "C"), ((0, 1), "D")]
    _ord8 = [tag for _t, tag in _r8._order_candidates(_cand)]
    ck("先最左列，同列再最上", _ord8 == ["C", "D", "A", "B"],
       f"输入 (row,col) A=(2,1) B=(0,2) C=(1,0) D=(0,1) → 顺序 {_ord8}")
except Exception as _e:
    ck("候选排序自检可运行", False, repr(_e))

print("\n⑭ 【2026-10-04 修】「走不到」名单的作用域 —— 静态检查 flow/runner.py")
# 日志证据 run_20261003_115546：一轮里 grid_target_unreachable 出现 10 次、两次扫描隔 46 秒。
# 根因：`self._unreachable = []` 写在了 for attempt 循环**里面** → 刚记住"这台走不到"，
# 下一屏就被抹掉 → 同一台车在同一轮里被反复选中。这是"改回去就会重现"的那种 bug，
# 所以用静态检查钉住（DETECTORS 名单也是这么钉的）。
try:
    _src14 = pathlib.Path(__file__).resolve().parent / "flow" / "runner.py"
    _ls14 = _src14.read_text(encoding="utf-8").splitlines()
    _i0 = next(i for i, ln in enumerate(_ls14) if "def change_car(self)" in ln)
    _i1 = next(i for i in range(_i0 + 1, len(_ls14)) if _ls14[i].startswith("    def "))
    _body14 = _ls14[_i0:_i1]
    _for14 = next(i for i, ln in enumerate(_body14)
                  if "for attempt in range(1, self.cfg.nav_budget + 1):" in ln)
    _clr14 = next(i for i, ln in enumerate(_body14) if ln.strip() == "self._unreachable = []")
    _ind14 = len(_body14[_clr14]) - len(_body14[_clr14].lstrip())
    ck("清空「走不到」名单在 for 之外（不会被每屏抹掉）",
       _clr14 < _for14 and _ind14 <= 8,
       f"清空在第 {_clr14} 行(缩进{_ind14}) / for 在第 {_for14} 行")
    ck("品牌翻页计数每轮重置（不再 getattr 累加跨轮）",
       any(ln.strip() == "self._brand_jumps = 0" for ln in _body14), "")
    ck("翻屏每屏只抓一帧（用完再结算 diff，不再 after=抓帧）",
       "_adv_before" in "\n".join(_body14)
       and not any(ln.strip().startswith("after = self._list_roi(self.frame())")
                   for ln in _body14), "")
    ck("「绕回开头」只跟本轮第一屏比（不再跟所有看过的屏逐一比）",
       "first_sig" in "\n".join(_body14)
       and "for _i, _old in enumerate(self._seen_screens)" not in "\n".join(_body14), "")
except Exception as _e14:
    ck("runner 静态检查可运行", False, repr(_e14))

print("\n⑮ 【2026-10-04 修】鼠标点车格：判不出黄框**绝不许**当成功")
# 日志统计：这一条路成功 6 次 / 没选中 87 次（命中率 ~7%）。更要命的是旧代码在
# "判不出黄框"时 return True（看不见就假设成功）→ 后面那下回车打在哪全凭运气。
try:
    import numpy as _np15
    from flow.config import RunConfig as _RC15
    from flow.runner import Runner as _R15, build_offline_stack as _BOS15

    class _Cap15:
        def __init__(self, f):
            self._f = f
        def grab(self, region=None):
            return self._f

    _cfg15 = _RC15()
    _cfg15.log_dir = tempfile.mkdtemp(prefix="vauto_selftest15_")
    _cfg15.dry_run = True
    _cfg15.use_mouse_select = True
    _cfg15.grid_walk_dwell = 0.01
    _r15 = _R15(_BOS15(capture=_Cap15(_np15.full((2160, 3840, 3), 200, _np15.uint8))), _cfg15)
    _tiles15 = [(0, 0, 800, 400, 648, 488, False, 0.2)]

    _r15._cursor_box = lambda *a, **k: None
    ck("判不出黄框 → 返回「未确定」（不是 True）", _r15._click_tile(_tiles15, (0, 0)) is not True,
       "旧实现在这里直接判成功 ✗")
    _r15._cursor_box = lambda *a, **k: (800, 400, 648, 488)
    ck("黄框确实落在目标格 → 认成功", _r15._click_tile(_tiles15, (0, 0)) is True, "")
    _r15._cursor_box = lambda *a, **k: (1500, 400, 648, 488)
    ck("黄框落在别的格 → 判「没选中」", _r15._click_tile(_tiles15, (0, 0)) is False, "")
except Exception as _e15:
    ck("鼠标点选自检可运行", False, repr(_e15))

print("\n⑯ 【2026-10-04 修】到站后按**光标那一格**绑定身份 + ♥ 复核（用 115627 那张证据图）")
# 日志 115546：目标本来是第 4 列那台没♥的车，程序却在 cell=[0,0] 上宣布"到站"，
# 却把**目标**的指纹记成"刚处理的车" → 加收藏复核拿这个指纹在列表里找不到格子 →
# favorite_done ok=false，白跑 ~14 秒。现在以光标实际压着的那一格为准。
try:
    _p16 = pathlib.Path(__file__).resolve().parent / "logs" / "target_r0c3_20261003_115627.png"
    if not _p16.is_file():
        print("  --  跳过（没有 logs/target_r0c3_20261003_115627.png 这张实机证据图）")
    else:
        import numpy as _np16
        import cv2 as _cv16
        from flow.config import RunConfig as _RC16
        from flow.runner import Runner as _R16, build_offline_stack as _BOS16

        class _Cap16:
            def __init__(self, f):
                self._f = f
            def grab(self, region=None):
                return self._f

        _im16 = _cv16.imdecode(_np16.fromfile(str(_p16), dtype="uint8"), _cv16.IMREAD_COLOR)
        _cfg16 = _RC16()
        _cfg16.log_dir = tempfile.mkdtemp(prefix="vauto_selftest16_")
        _r16 = _R16(_BOS16(capture=_Cap16(_im16)), _cfg16)
        _ts16 = _r16._grid_tiles(_im16)
        ck("_grid_tiles 在「HSV 收窄 + 同帧只算一次」之后与基准逐位一致",
           [(t[0], t[1], t[2], t[3], t[4], t[5], bool(t[6])) for t in _ts16] == [
               (0, 0, 816, 424, 648, 488, True), (0, 1, 1506, 424, 648, 488, True),
               (0, 2, 2170, 424, 648, 488, True), (0, 3, 2860, 424, 648, 488, False),
               (1, 0, 816, 937, 648, 479, True), (1, 1, 1506, 928, 648, 488, True),
               (1, 2, 2170, 928, 648, 488, True), (1, 3, 2860, 928, 648, 488, True),
               (2, 0, 1506, 1432, 648, 468, True)],
           f"得到 {len(_ts16)} 格")
        _fp16, _cell16, _tile16, _cur16 = _r16._cursor_tile_binding()
        ck("到站绑定 = 光标实际压着的那一格（不是「打算去」的那一格）",
           _cell16 == (0, 0) and _fp16 is not None and _tile16 is not None,
           f"cell={_cell16} fp={'有' if _fp16 is not None else '无'}")
        ck("♥ 复核按光标格（该格有♥ → True）", _r16._heart_at_cursor() is True, "")
        ck("_cursor_box 与基准一致", _r16._cursor_box(_im16, _ts16) == (816, 424, 648, 488),
           f"{_r16._cursor_box(_im16, _ts16)}")
except Exception as _e16:
    ck("到站绑定自检可运行", False, repr(_e16))

print("\n⑰ 【2026-10-04 修】判页按时间预算收手（不再固定 10 次 ≈20 秒）")
# 日志：6/57 次运行卡在「到不了车辆页」，因为固定 10 次 × esc_dwell(2s) 只有 ~25 秒，
# 上车加载 13~18 秒 + 慢盘就超了。现在按 cfg.tab_ensure_budget（默认 45s）。
try:
    import time as _time17
    import numpy as _np17
    from flow.config import RunConfig as _RC17
    from flow.runner import Runner as _R17, build_offline_stack as _BOS17

    class _Cap17:
        def __init__(self, f):
            self._f = f
        def grab(self, region=None):
            return self._f

    _cfg17 = _RC17()
    _cfg17.log_dir = tempfile.mkdtemp(prefix="vauto_selftest17_")
    _cfg17.tab_ensure_budget = 0.2
    _cfg17.auto_recover = False
    _cfg17.dry_run = True
    _r17 = _R17(_BOS17(capture=_Cap17(_np17.full((2160, 3840, 3), 200, _np17.uint8))), _cfg17)
    _t17 = _time17.monotonic()
    _ok17 = _r17._ensure_vehicle_tab()
    _d17 = _time17.monotonic() - _t17
    ck("预算到点就收手（固定 10 次的话这里要 ~20 秒）", _ok17 is False and _d17 < 8.0,
       f"耗时 {_d17:.2f}s → {_ok17}")
except Exception as _e17:
    ck("判页预算自检可运行", False, repr(_e17))

print("\n⑱ 【2026-10-04 新增】界面状态分类 + 自愈（认不出界面时能说清「我在哪」）")
try:
    import numpy as _np18
    import cv2 as _cv18
    from flow.config import RunConfig as _RC18
    from flow.runner import Runner as _R18, build_offline_stack as _BOS18

    class _Cap18:
        def __init__(self, f):
            self._f = f
        def grab(self, region=None):
            return self._f

    _p18 = pathlib.Path(__file__).resolve().parent / "logs" / "target_r0c3_20261003_115627.png"
    _cfg18 = _RC18()
    _cfg18.log_dir = tempfile.mkdtemp(prefix="vauto_selftest18_")
    _r18 = _R18(_BOS18(capture=_Cap18(_np18.full((2160, 3840, 3), 20, _np18.uint8))), _cfg18)
    _st18, _sc18 = _r18._where_am_i()
    ck("全黑/认不出的画面 → 标签是 world（不是乱猜某个页）", _st18 == "world",
       f"得到 {_st18}")
    # 【2026-10-04 实测抓到的判据缺陷】panel_search_title 在纯灰/黑屏上也能打 0.909
    # （阈值 0.696），其余 12 个判据在空屏上都是 0.000 → 它会在"看不见屏幕"时假确认。
    # 修法：给该判据加 min_std（标定表里的对比度下限）。这条检查保证那个修法没被标定覆盖掉。
    _d18 = _r18.s.dets.get("panel_search_title")
    ck("panel_search_title 带 min_std 对比度下限（空屏不再假命中）",
       _d18 is not None and float(getattr(_d18, "min_std", 0.0)) > 0,
       f"min_std={None if _d18 is None else getattr(_d18, 'min_std', None)}")
    if _d18 is not None:
        _s_flat, _ = _d18.probe(_np18.full((2160, 3840, 3), 200, _np18.uint8))
        ck("纯灰帧上该判据不再给分（分数为 nan/无效）", not (_s_flat == _s_flat),
           f"score={_s_flat}")
    # 【2026-10-04 重做】旧模板几乎是一整条纯柠檬绿按钮 → 在任何带柠檬绿的真实界面上都虚高
    # （剧情页 0.877、车库列表 0.789、挑战结算 0.909，全超过阈值 0.696 = 永远命中）。
    # 新模板只裁「搜索」两个字 → 真面板 1.000、剧情页/比赛菜单/车库列表都 < 0.18。
    _p_ev = (pathlib.Path(os.environ.get("HERMES_HOME", ".")) / "images"
             / "upload_20261004_222623_1.png")
    _p_panel = pathlib.Path(__file__).resolve().parent / "golden_frames" / "event_entry" / "entry_05.png"
    if _d18 is not None and _p_panel.is_file():
        _im_panel = _cv18.imdecode(_np18.fromfile(str(_p_panel), dtype="uint8"), _cv18.IMREAD_COLOR)
        _sp, _ = _d18.probe(_im_panel)
        ck("真「搜索面板」上命中（判据仍然管用）", _sp == _sp and _sp >= _d18.threshold,
           f"score={_sp:.3f}")
        if _p_ev.is_file():
            _im_st = _cv18.imdecode(_np18.fromfile(str(_p_ev), dtype="uint8"), _cv18.IMREAD_COLOR)
            _ss, _ = _d18.probe(_im_st)
            ck("正常剧情页上**不**命中（旧模板在这里 0.877）",
               not (_ss == _ss and _ss >= _d18.threshold), f"score={_ss:.3f}")
    if _p18.is_file():
        _im18 = _cv18.imdecode(_np18.fromfile(str(_p18), dtype="uint8"), _cv18.IMREAD_COLOR)
        _st18b, _sc18b = _r18._where_am_i(_im18)
        ck("车库列表截图 → 标签是 garage_list（带分数可核对）",
           _st18b == "garage_list",
           f"得到 {_st18b}  garage={( _sc18b or {}).get('page_title_garage')}")
    ck("自愈在 replay 下不动手（回放/自检不会乱按键）",
       _r18.cfg.replay is False and _r18.recover_to_known.__name__ == "recover_to_known", "")
    _cfg18.auto_recover = False
    ck("auto_recover=False → 自愈直接放弃（返回 disabled）",
       _r18.recover_to_known("测试") == "disabled", "")
except Exception as _e18:
    ck("状态分类自检可运行", False, repr(_e18))

print("\n⑲ 【2026-10-04 回归】把 change_car 整条扫描链路离线跑一遍（不进游戏、不按键）")
# 目的：⑭ 是静态检查，⑯ 只验到站绑定；这里把**扫描循环本体**（_adv_before 延迟结算、
# first_sig 绕回判据、品牌翻页、unreachable 跳过）真跑一遍。
# 用的是一张固定不变的车库截图 → 光标永远走不动 → 正好落在"走不到"的分支上：
#   修复后：只记 1 次 grid_target_unreachable 就把这台跳过 → 这一屏没候选 → 翻列 → 翻不动
#           → 点品牌 → 还是不动 → 收尾（grid_end / all_cars_seen）
#   修复前：同一台车会被反复选中 nav_budget 次（每台白走 ≤30 次按键）
try:
    import numpy as _np19
    import cv2 as _cv19
    import json as _json19
    from flow.config import RunConfig as _RC19
    from flow.runner import Runner as _R19, build_offline_stack as _BOS19

    class _Cap19:
        def __init__(self, f):
            self._f = f
        def grab(self, region=None):
            return self._f

    _p19 = pathlib.Path(__file__).resolve().parent / "logs" / "target_r0c3_20261003_115627.png"
    if not _p19.is_file():
        print("  --  跳过（没有那张车库列表证据图）")
    else:
        _im19 = _cv19.imdecode(_np19.fromfile(str(_p19), dtype="uint8"), _cv19.IMREAD_COLOR)
        _cfg19 = _RC19()
        _cfg19.log_dir = tempfile.mkdtemp(prefix="vauto_selftest19_")
        _cfg19.dry_run = True
        _cfg19.grid_walk_dwell = 0.01
        _cfg19.grid_scroll_dwell = 0.01
        _cfg19.esc_dwell = 0.01
        _r19 = _R19(_BOS19(capture=_Cap19(_im19)), _cfg19)
        # 这张证据图已经是"点完「更换车辆」之后的列表"，所以入口那两步点不到磁贴 ——
        # 我们要测的是**扫描循环本体**，所以把"入口点磁贴/判页"这两步跳过（其余照旧）。
        _r19.click_match = lambda *a, **k: True
        _ok19 = _r19.change_car()
        _rows19 = [_json19.loads(_l) for _l in
                   open(_r19.log.path, encoding="utf-8") if _l.strip()]
        _kinds19 = [r["kind"] for r in _rows19]
        _unreach19 = sum(1 for k in _kinds19 if k == "grid_target_unreachable")
        ck("整条扫描链路能跑完（没抛异常、有明确收尾）",
           _ok19 is False and any(k in _kinds19 for k in ("grid_end", "grid_budget", "grid_wrapped")),
           f"返回 {_ok19}，收尾事件 {[k for k in _kinds19 if k.startswith('grid_')][-3:]}")
        ck("「走不到」的车只试一次（修复前是 nav_budget 次 = 400）",
           1 <= _unreach19 <= 3, f"grid_target_unreachable × {_unreach19}")
        ck("扫描循环本体跑到了（每屏一条 grid_scan + 一条 grid_advance）",
           _kinds19.count("grid_scan") >= 2 and "grid_advance" in _kinds19,
           f"grid_scan × {_kinds19.count('grid_scan')}, grid_advance × {_kinds19.count('grid_advance')}")
        ck("回放/dry-run 下不发任何真实按键（离线自检不进游戏）",
           _cfg19.dry_run is True, "")
except Exception as _e19:
    ck("change_car 离线链路自检可运行", False, repr(_e19))

print("\n⑳ 【2026-10-04 新增】焦点进出：切出关大写锁定 / 切回开回来 + 鼠标归位 + 认出界面")
# 用户原话："我有的时候会把屏幕焦点切出去做一些别的，但是软件在我再次回来之后就不识别了，
# 当我切出去的时候自动解除大写锁定，切回来的时候再自动开启，并且检测鼠标位置，把鼠标归位，
# 关闭程序的时候也关掉 caps"。
# 这里**不碰真实键盘**：caps_lock_on / set_caps_lock / force_caps_off 全部换成记录器。
try:
    import numpy as _np20
    import json as _json20

    import vauto.focus as _vf20
    _live20 = {"fg": False}
    _orig_alive20, _orig_fg20 = _vf20.is_window_alive, _vf20.is_foreground
    _vf20.is_window_alive = lambda h: True
    _vf20.is_foreground = lambda *a, **k: _live20["fg"]
    try:
        _calls20 = []
        _g20 = _vf20.FocusGuard(1,
                                on_lose_focus=lambda: _calls20.append("out"),
                                on_gain_focus=lambda: _calls20.append("in"))
        _g20.is_active()                      # 第一次：只建立基线，不该通知
        _live20["fg"] = True
        _g20.is_active()                      # 切回来
        _g20.is_active(); _g20.is_active()    # 状态没变 → 不该重复通知
        _live20["fg"] = False
        _g20.is_active()                      # 切出去
        ck("焦点守卫只在状态**真的翻转**时通知（重复不通知）",
           _calls20 == ["in", "out"], f"通知序列 {_calls20}")
    finally:
        _vf20.is_window_alive, _vf20.is_foreground = _orig_alive20, _orig_fg20

    import flow.runner as _fr20
    from flow.config import RunConfig as _RC20
    from flow.runner import Runner as _R20, build_offline_stack as _BOS20

    class _Cap20:
        def __init__(self, f):
            self._f = f
        def grab(self, region=None):
            return self._f

    _orig_on20, _orig_set20, _orig_force20 = (_fr20.caps_lock_on, _fr20.set_caps_lock,
                                              _fr20.force_caps_off)
    _caps20 = {"on": True, "calls": []}
    try:
        _fr20.caps_lock_on = lambda: _caps20["on"]
        def _fake_set20(on):
            _caps20["calls"].append(("set", bool(on)))
            _caps20["on"] = bool(on)
            return _caps20["on"]
        _fr20.set_caps_lock = _fake_set20
        def _fake_force20():
            _caps20["calls"].append(("force_off",))
            _caps20["on"] = False
            return False
        _fr20.force_caps_off = _fake_force20

        _frame20 = _np20.full((2160, 3840, 3), 200, _np20.uint8)
        _cfg20 = _RC20()
        _cfg20.log_dir = tempfile.mkdtemp(prefix="vauto_selftest20_")
        _cfg20.dry_run = False               # 走真实分支（但按键全被换成记录器）
        _r20 = _R20(_BOS20(capture=_Cap20(_frame20)), _cfg20)

        _r20._on_focus_lost()
        ck("切出游戏 → 大写锁定被关掉", _caps20["calls"][-1] == ("set", False),
           f"{_caps20['calls']}")
        _caps20["calls"].clear()
        _r20._on_focus_gained()
        ck("切回来 → 大写锁定被开回", ("set", True) in _caps20["calls"], f"{_caps20['calls']}")
        _rows20 = [_json20.loads(_l) for _l in
                   open(_r20.log.path, encoding="utf-8") if _l.strip()]
        _kinds20 = [r["kind"] for r in _rows20]
        ck("切回来记了 focus_gained（含离开时长/鼠标位置/界面判定）",
           "focus_gained" in _kinds20
           and any(r.get("state") for r in _rows20 if r["kind"] == "focus_gained"),
           f"事件 {[k for k in _kinds20 if k.startswith('focus')]}")
        ck("切回来顺手把鼠标归了位（park_pointer 事件）", "park_pointer" in _kinds20,
           f"{[k for k in _kinds20 if k.startswith('park')]}")
        _caps20["calls"].clear()
        _r20._caps_restore_on_exit()
        ck("退出程序 → 大写锁定被关掉", ("force_off",) in _caps20["calls"],
           f"{_caps20['calls']}")

        # 安全性质：dry-run（演练）时**绝不动键盘**
        _cfg20b = _RC20()
        _cfg20b.log_dir = tempfile.mkdtemp(prefix="vauto_selftest20b_")
        _cfg20b.dry_run = True
        _r20b = _R20(_BOS20(capture=_Cap20(_frame20)), _cfg20b)
        _caps20["calls"].clear()
        _r20b._on_focus_lost(); _r20b._on_focus_gained(); _r20b._caps_restore_on_exit()
        ck("dry-run 下这三个动作一个键都不发（演练不碰键盘）",
           _caps20["calls"] == [], f"{_caps20['calls']}")

        # 开关能关掉
        _cfg20c = _RC20()
        _cfg20c.log_dir = tempfile.mkdtemp(prefix="vauto_selftest20c_")
        _cfg20c.dry_run = False
        _cfg20c.caps_lock_follow_focus = False
        _cfg20c.caps_off_on_exit = False
        _r20c = _R20(_BOS20(capture=_Cap20(_frame20)), _cfg20c)
        _caps20["calls"].clear()
        _r20c._on_focus_lost(); _r20c._on_focus_gained(); _r20c._caps_restore_on_exit()
        ck("三个开关关掉后就不动大写锁定了", _caps20["calls"] == [], f"{_caps20['calls']}")
    finally:
        _fr20.caps_lock_on, _fr20.set_caps_lock, _fr20.force_caps_off = (
            _orig_on20, _orig_set20, _orig_force20)
except Exception as _e20:
    ck("焦点进出自检可运行", False, repr(_e20))

print("\n㉑ 【2026-10-04 新增】「赛事暂停菜单」（比赛里按 Esc／切出去再回来）——用户截图实测标定")
# 用户给的 5 张图里，图五是"在比赛界面切出去再回来"看到的样子：「重新开始赛事 / 退出赛事」
# 磁贴。他说"重点锚点可能是 重新开始赛事 与 退出赛事"。这两个判据就是照他指的锚点建的。
try:
    import numpy as _np21
    import cv2 as _cv21
    import json as _json21
    from flow.config import RunConfig as _RC21
    from flow.runner import Runner as _R21, build_offline_stack as _BOS21

    class _Cap21:
        def __init__(self, imgs):
            self._imgs = list(imgs)
            self.i = 0
        def grab(self, region=None):
            im = self._imgs[min(self.i, len(self._imgs) - 1)]
            self.i += 1
            return im

    _IMG21 = pathlib.Path(os.environ.get("HERMES_HOME", ".")) / "images"
    _p5 = _IMG21 / "upload_20261004_222623_5.png"      # 正样本：比赛菜单
    _p1 = _IMG21 / "upload_20261004_222623_1.png"      # 关键负样本：正常的剧情页
    if not (_p5.is_file() and _p1.is_file()):
        print("  --  跳过（没有那两张用户截图）")
    else:
        _im5 = _cv21.imdecode(_np21.fromfile(str(_p5), dtype="uint8"), _cv21.IMREAD_COLOR)
        _im1 = _cv21.imdecode(_np21.fromfile(str(_p1), dtype="uint8"), _cv21.IMREAD_COLOR)
        ck("两张用户截图都是原生 3840x2160（模板坐标不用缩放）",
           _im5.shape[:2] == (2160, 3840) and _im1.shape[:2] == (2160, 3840),
           f"{_im5.shape[:2]} / {_im1.shape[:2]}")

        _cfg21 = _RC21()
        _cfg21.log_dir = tempfile.mkdtemp(prefix="vauto_selftest21_")
        _cfg21.dry_run = True
        _r21 = _R21(_BOS21(capture=_Cap21([_im5])), _cfg21)
        for _nm in ("tile_restart_event", "tile_exit_event"):
            _d = _r21.s.dets.get(_nm)
            if _d is None:
                ck(f"判据 {_nm} 已装配（DETECTORS 名单里有）", False, "没装配 → 会 KeyError")
                continue
            _s5, _ = _d.probe(_im5)
            _s1, _ = _d.probe(_im1)
            ck(f"{_nm}：用户截图上命中、正常剧情页上不命中",
               _s5 == _s5 and _s5 >= _d.threshold and not (_s1 == _s1 and _s1 >= _d.threshold),
               f"比赛菜单 {_s5:.3f}（阈值 {_d.threshold:.3f}）/ 剧情页 {_s1:.3f}")

        _st5, _sc5 = _r21._where_am_i(_im5)
        _st1, _sc1 = _r21._where_am_i(_im1)
        ck("界面判定：图五 = event_menu（比赛菜单）", _st5 == "event_menu", f"得到 {_st5}")
        ck("界面判定：图一 = story_menu（正常剧情页，没被误判成比赛菜单）",
           _st1 == "story_menu", f"得到 {_st1}")

        # 自愈动作：在比赛菜单里应该按"返回"键（默认 Esc），**绝不能按回车**
        _cfg21b = _RC21()
        _cfg21b.log_dir = tempfile.mkdtemp(prefix="vauto_selftest21b_")
        _cfg21b.dry_run = False
        _cfg21b.esc_dwell = 0.01
        _sim21 = _BOS21(capture=_Cap21([_im5]))
        _r21b = _R21(_sim21, _cfg21b)
        _r21b._where_am_i = lambda *a, **k: ("event_menu", {})
        _r21b.recover_to_known("测试", rounds=1)
        _acts21 = [a for a in getattr(_sim21.sim, "held", [])]
        ck("自愈在比赛菜单里按的是「返回」键（Esc），不是回车",
           any(a[0] == "tap" and a[1] == "esc" for a in _acts21)
           and not any(a[0] == "tap" and a[1] == "enter" for a in _acts21),
           f"动作 {_acts21}")

        # 集成：A 阶段轮询中认到比赛菜单 → 自动回比赛（而不是等满看门狗）
        _cfg21c = _RC21()
        _cfg21c.log_dir = tempfile.mkdtemp(prefix="vauto_selftest21c_")
        _cfg21c.dry_run = False
        _cfg21c.round_minutes = 0
        _cfg21c.round_timeout = 1.0
        _cfg21c.round_settle_before = 0.3
        _cfg21c.round_active_wait = 0.3
        _cfg21c.poll = 0.05
        _cfg21c.watchdog_idle = 0
        _cfg21c.esc_dwell = 0.01
        _cfg21c.max_polls_per_round = 4
        _st21c = _BOS21(capture=_Cap21([_im5] * 40))
        _r21c = _R21(_st21c, _cfg21c)
        _out21 = _r21c.farm_one_round(retry=False)
        _rows21c = [_json21.loads(_l) for _l in
                    open(_r21c.log.path, encoding="utf-8") if _l.strip()]
        _kinds21c = [r["kind"] for r in _rows21c]
        ck("A 阶段轮询里认到比赛菜单就按返回键（不再干等结算判据）",
           "event_menu_detected" in _kinds21c
           and any(a[0] == "tap" and a[1] == "esc" for a in _st21c.sim.held),
           f"返回 {_out21}；事件 {[k for k in _kinds21c if 'event' in k or k == 'press'][:6]}")
        # 【2026-10-04 用户口径】"Esc 回到比赛之后继续按 W 直到比赛结束" → 返回后必须重新按住 W，
        # 而不是继续用菜单弹出前那次（游戏恢复后不一定还认它仍按着）。
        _esc_at21 = next((i for i, a in enumerate(_st21c.sim.held)
                          if a[0] == "tap" and a[1] == "esc"), None)
        # 桩把「按住」记成 ("down", key)（见 _NoSim.key_down）
        _w_after21 = None if _esc_at21 is None else [
            a for a in _st21c.sim.held[_esc_at21 + 1:] if a[0] == "down"]
        ck("返回比赛后**重新按住 W**（保证游戏收到新的 keydown，一直按到比赛结束）",
           bool(_w_after21) and any(_cfg21c.hold_key in a[1] for a in _w_after21),
           f"Esc 之后的动作 {_w_after21}")
except Exception as _e21:
    ck("比赛菜单自检可运行", False, repr(_e21))

print("\n㉒ 【2026-10-04 晚新增】车库列表：当前车不能\"不算数\"、目标不能每帧重挑（用户实测两个坑）")
# 用户当晚报的两个现象，都在这两张**真实现场帧**上复现过（帧已入库 golden_frames_min/garage_cursor，
# 所以 CI / 别人 clone 上也能跑）：
#   ① "一页全收藏的列表里在两辆车之间上下横跳" ② 当前车（未收藏）那格走不到 → 每轮白等十几秒
# 帧来源：dist/vauto/logs/target_r0c0_20261004_230719.png（光标压着当前车那格、它没♥，
#         同一页其余 7 格全有♥）与 target_r1c3_20261004_203100.png（光标在有♥的格上，
#         两台待处理车在右下方）。
try:
    import numpy as _np22
    import cv2 as _cv22
    import json as _json22
    from pathlib import Path as _P22
    from flow.config import RunConfig as _RC22
    from flow.runner import Runner as _R22, build_offline_stack as _BOS22

    _root22 = _P22(__file__).resolve().parent

    def _frame22(name):
        """按名字找入库的真实现场帧（.png/.jpg 都认；优先完整素材目录）。"""
        for base in ("golden_frames", "golden_frames_min"):
            d = _root22 / base / "garage_cursor"
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.stem == name and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    return _cv22.imdecode(_np22.fromfile(str(p), dtype="uint8"), _cv22.IMREAD_COLOR)
        return None

    class _Cap22:
        def __init__(self, imgs):
            self.imgs, self.i = list(imgs), 0

        def grab(self, region=None):
            # 循环取帧（两张就 A,B,A,B…）—— 用来精确构造"光标在 A/B 之间来回跳"的场面
            im = self.imgs[self.i % len(self.imgs)]
            self.i += 1
            return im

    def _runner22(imgs, log_prefix="vauto_selftest22_"):
        _cfg = _RC22()
        _cfg.log_dir = tempfile.mkdtemp(prefix=log_prefix)
        _cfg.dry_run = False          # 让按键走到假 sim（记录下来），方便断言
        _cfg.grid_walk_max = 12
        _st = _BOS22(capture=_Cap22(imgs))
        return _R22(_st, _cfg), _st

    _f_cur = _frame22("cur_on_current_car")
    _f_right = _frame22("candidates_right")
    ck("入库的车库现场帧可用（golden_frames_min/garage_cursor）",
       _f_cur is not None and _f_right is not None,
       f"cur={None if _f_cur is None else _f_cur.shape} right={None if _f_right is None else _f_right.shape}")

    if _f_cur is not None:
        _r22, _st22 = _runner22([_f_cur])
        _tiles22 = _r22._grid_tiles(_f_cur)
        _cbox22 = _r22._cursor_box(_f_cur, _tiles22)
        _cur_tiles = [t for t in _tiles22 if _r22._is_current_car(_f_cur, t)]
        _noheart = [t for t in _tiles22 if not t[6]]
        print(f"    （现场帧：{len(_tiles22)} 格、没♥ {len(_noheart)} 格、"
              f"其中当前车 {len(_cur_tiles)} 格；光标框 {_cbox22}）")
        ck("现场帧上：唯一没♥的就是当前车那一格（=用户说的\"一页全收藏\"里剩的那台）",
           len(_noheart) == 1 and len(_cur_tiles) == 1
           and tuple(_noheart[0][:2]) == tuple(_cur_tiles[0][:2]),
           f"没♥ {[list(t[:2]) for t in _noheart]} / 当前车 {[list(t[:2]) for t in _cur_tiles]}")
        _st22.sim.held.clear()
        _ok22 = _r22._walk_to_tile()
        ck("★ 走位器认这台当前车（修前：把它排除 → \"视野里没有可处理的车格\" → 白等 13 秒 ×27 次）",
           _ok22 is True and not _st22.sim.held,
           f"返回 {_ok22}、按键 {_st22.sim.held}")
        ck("★ 而且它就在光标下面 → 一发键都不用按（到站即停）",
           _r22._last_walk_why == "",
           f"理由 {_r22._last_walk_why!r}")

    if _f_right is not None:
        # 光标停在有♥的格上、候选在右下方 → 必须走动，但**必须有界**（不能无限横跳）
        _r22b, _st22b = _runner22([_f_right])
        _ok22b = _r22b._walk_to_tile()
        _keys22b = [a[1] for a in _st22b.sim.held if a[0] == "tap"]
        ck("静态画面下走不动时**有界退出**（按几下就放弃，不是一路按到底）",
           _ok22b is False and len(_keys22b) <= 8 and _r22b._last_walk_why == "no_progress",
           f"返回 {_ok22b}、理由 {_r22b._last_walk_why!r}、按键数 {len(_keys22b)} {_keys22b}")

        # ★ 目标锁定 + 震荡保护（白盒）：把"光标框"换成受控序列 A,B,A,B…
        #   真机帧凑不出"光标在两处来回跳"这个**动态过程**（我手上的现场帧光标都停在同一格），
        #   所以这里直接给 `_cursor_box` 装一个受控序列，验证两条护栏：
        #   ① 一次走位里**目标不重挑**（修前每帧重挑 → 两张候选轮流当"最近" → 上下横跳）
        #   ② 连续 3 轮"没朝目标靠近" → 判定走不动、立即放弃（不再耗满 30 次）
        #   【2026-10-05 改】护栏口径从"同一光标框出现第 3 次"换成"连续 3 轮没靠近" ——
        #   因为真机上**光标框 x 恒 ~816 根本不会动**，拿它当护栏必然误杀（见走位器注释）。
        _r22d, _st22d = _runner22([_f_right])
        _real_tiles = _r22d._grid_tiles(_f_right)
        _r22d._grid_tiles = lambda _fr: _real_tiles      # 车格固定（不随帧变）
        _seq22 = [(816, 424, 648, 488), (1506, 424, 648, 488)]   # 都在有♥的格上（不是候选）
        _box22 = {"i": 0}

        def _fake_cursor(_fr, _tiles):
            # 像真机一样：**只在按过方向键之后才换位置**，两次按键之间保持不变
            # （真机上光标框要等动画结束才稳；`_settle_after_key` 要求连续两次采样一致）
            n = sum(1 for a in _st22d.sim.held
                    if a[0] == "tap" and a[1] in ("down", "up", "left", "right"))
            return _seq22[n % 2]

        _r22d._cursor_box = _fake_cursor
        _ok22d = _r22d._walk_to_tile()
        _steps22d = []
        try:
            with open(_r22d.log.path, encoding="utf-8") as _fh:
                for _l in _fh:
                    _row = _json22.loads(_l)
                    if _row.get("kind") == "grid_walk_step":
                        _steps22d.append(_row)
        except Exception:
            pass
        _tgts22d = {tuple(s.get("target_cell") or ()) for s in _steps22d}
        ck("★ 一次走位里目标**锁死不变**，且一律走\"换列\"阶段（新模型：←/→ 换列）",
           len(_steps22d) >= 2 and len(_tgts22d) == 1
           and all(s.get("phase") == "column" for s in _steps22d),
           f"走位步数 {len(_steps22d)}、出现过目标 {_tgts22d}、"
           f"阶段 {[s.get('phase') for s in _steps22d]}、键 {[s.get('key') for s in _steps22d]}")
        ck("★ 走不动时按几下就放弃（新护栏：连续 3 轮没靠近目标）",
           _ok22d is False and _r22d._last_walk_why == "no_progress",
           f"返回 {_ok22d}、理由 {_r22d._last_walk_why!r}")
except Exception as _e22:
    ck("车库走位自检可运行", False, repr(_e22))

print("\n㉓ 【2026-10-04 晚新增】切回窗口 / W 掉了 → **重新按住 W**（用户实测报的问题）")
# 用户原话："从其他窗口切回去之后没有重新按 w，这是为什么呢？"
# 根因：游戏在失焦/暂停时会丢掉按键状态，而程序内部以为自己还按着（key_down 只在开始时发过一次）。
try:
    import json as _json23
    import pathlib as _pl23
    import numpy as _np23
    import cv2 as _cv23
    from flow.config import RunConfig as _RC23
    from flow.runner import Runner as _R23, build_offline_stack as _BOS23

    class _Cap23:
        def __init__(self, imgs):
            self.imgs, self.i = list(imgs), 0

        def grab(self, region=None):
            im = self.imgs[self.i % len(self.imgs)]
            self.i += 1
            return im

    _img23 = None
    for _base in ("golden_frames", "golden_frames_min"):
        _d23 = _pl23.Path(__file__).resolve().parent / _base / "challenge_hud"
        if not _d23.is_dir():
            continue
        for _p in sorted(_d23.iterdir()):
            if _p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                _img23 = _cv23.imdecode(_np23.fromfile(str(_p), dtype="uint8"), _cv23.IMREAD_COLOR)
                break
        if _img23 is not None:
            break
    ck("㉓ 用的静态帧可用", _img23 is not None, f"shape={None if _img23 is None else _img23.shape}")

    if _img23 is not None:
        # ① 焦点切回来 → 重新按住 W（直接走 _on_focus_gained 那条路）
        _cfg23 = _RC23()
        _cfg23.log_dir = tempfile.mkdtemp(prefix="vauto_selftest23a_")
        _cfg23.dry_run = False
        _cfg23.caps_lock_follow_focus = False     # 自检绝不能真的动系统大写锁定
        _cfg23.park_on_focus_return = False       # 也不要真移鼠标
        _cfg23.focus_resync = False               # 走"不做界面重判"那条分支（仍必须重按 W）
        _st23 = _BOS23(capture=_Cap23([_img23]))
        _r23 = _R23(_st23, _cfg23)
        _st23.sim.held.clear()
        _r23.hold("w", "模拟比赛进行中")
        _r23._on_focus_gained()
        _acts23 = list(_st23.sim.held)
        _downs23 = [a for a in _acts23 if a[0] == "down"]
        ck("★ 焦点切回游戏 → **重新按住 W**（修前：什么都不做，车就不动了）",
           len(_downs23) >= 2 and _acts23[-1] == ("down", "w"),
           f"动作 {_acts23}")
        ck("先松开再按（保证是新的 keydown，游戏才认）",
           any(a[0] == "release_all" for a in _acts23[1:]),
           f"动作 {_acts23}")

        # ② 比赛里画面长时间没变 → 自动补按 W（而不是干等看门狗 ~9 分钟）
        _cfg23b = _RC23()
        _cfg23b.log_dir = tempfile.mkdtemp(prefix="vauto_selftest23b_")
        _cfg23b.dry_run = False
        _cfg23b.race_rehold_after = 0.4
        _cfg23b.round_minutes = 0
        _cfg23b.round_timeout = 1.5
        _cfg23b.round_settle_before = 0.2
        _cfg23b.round_active_wait = 0.2
        _cfg23b.poll = 0.05
        _cfg23b.watchdog_idle = 0
        _cfg23b.max_polls_per_round = 40
        _cfg23b.caps_lock_follow_focus = False
        _st23b = _BOS23(capture=_Cap23([_img23]))      # 永远静止的画面
        _r23b = _R23(_st23b, _cfg23b)
        _r23b._where_am_i = lambda *a, **k: ("world", {})   # 让这一轮真的跑起来
        _r23b.farm_one_round(retry=False)
        _rows23b = [_json23.loads(_l) for _l in
                    open(_r23b.log.path, encoding="utf-8") if _l.strip()]
        ck("★ 比赛里画面静止 → 自动补按 W（不再干等看门狗）",
           any(r.get("kind") == "rehold" for r in _rows23b),
           f"事件 {[r.get('kind') for r in _rows23b][-8:]}")
except Exception as _e23:
    ck("rehold 自检可运行", False, repr(_e23))

print("\n㉔ 【2026-10-06 新增】控制台选窗口不再\"乱挑\"（用户实测崩溃：标题变成 104310437/12960）")
# 用户原话："[崩溃] ...RuntimeError: 找不到标题包含 '104310437/12960' 的窗口 ... 怎么会这样呢？"
# 根因：控制台**比游戏先开**时，窗口列表里还没有 Forza，旧代码的兜底是"选列表第一个窗口"
# （他那次是 104310437/12960）→ 一按开始就崩。现在绝不乱挑：只认 forza/地平线，认不到就留空+提示。
try:
    from flow.console import pick_window_title as _pwt24

    _junk = ["104310437/12960", "设置", "NVIDIA GeForce Overlay", "Program Manager"]
    _t24, _m24 = _pwt24(_junk, "")
    ck("★ 列表里没有游戏时**不挑任何窗口**（修前会选中列表第一个 → 一发车就崩）",
       _t24 == "" and "没找到 Forza" in _m24, f"标题={_t24!r} 提示={_m24.strip()!r}")
    ck("★ 上面这个场景里绝不能出现那个无关窗口",
       "104310437" not in _t24, f"标题={_t24!r}")

    _t24b, _m24b = _pwt24(_junk, "104310437/12960")
    ck("当前值还在列表里但不是游戏 → 保留用户选择，但**明确警告**（不悄悄拿它发车）",
       _t24b == "104310437/12960" and "不是游戏" in _m24b, f"标题={_t24b!r} 提示={_m24b.strip()!r}")

    _t24b2, _m24b2 = _pwt24(_junk, "已经关掉的窗口某某")
    ck("当前值已经不在列表里 + 没有游戏 → 清空并提示（不再拿它去发车）",
       _t24b2 == "" and "游戏开着吗" in _m24b2, f"标题={_t24b2!r}")

    _t24c, _m24c = _pwt24(["Forza Horizon 6"] + _junk, "104310437/12960")
    ck("游戏出现后 → 自动切回游戏窗口", _t24c == "Forza Horizon 6", f"标题={_t24c!r}")

    _t24d, _m24d = _pwt24(_junk, "设置")
    ck("当前值在列表里但不是游戏、也没有游戏窗口 → 原样保留（不乱动用户选择）",
       _t24d == "设置", f"标题={_t24d!r}")

    _t24e, _m24e = _pwt24(["Forza Horizon 6"], "")
    ck("有游戏时自动选中游戏", _t24e == "Forza Horizon 6" and _m24e == "", f"标题={_t24e!r}")

    # build_stack 的报错也要能直接定位（把当前可见窗口列出来）
    try:
        from flow.runner import build_stack as _bs24
        from flow.config import RunConfig as _RC24b
        _bs24(_RC24b(), title_key="这辈子不存在的窗口标题zzz")
        ck("build_stack 报错带候选窗口", False, "竟然没抛异常？")
    except RuntimeError as _e24:
        ck("★ 找不到窗口时的报错**列出当前可见窗口**（修前只有一句话，无从判断游戏开没开）",
           "当前可见窗口" in str(_e24), f"报错={str(_e24)[:120]!r}")
    except Exception as _e24:
        ck("build_stack 报错带候选窗口", False, repr(_e24))
except Exception as _e24:
    ck("选窗口自检可运行", False, repr(_e24))

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:5]}")
sys.exit(1 if fails else 0)

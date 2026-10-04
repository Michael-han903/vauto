# -*- coding: utf-8 -*-
"""自检：vision.py 识别层（ROI / 降采样 / 滞回去抖 / 变化检测）
用 golden_frames 真实帧 + templates/thresholds.json 的标定结果验证；
全程不注入任何键鼠输入（StubCapture 只回放已有帧）。
"""
import pathlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # 谁的机器都能跑

import numpy as np

from vauto import TemplateMatcher, load_image, scaled_frame, to_full_point, to_frame_point
from vauto.vision import VisualDetector, frame_signature, mean_abs_diff, signature_distance, wait_stable

HERE = Path(__file__).resolve().parent
G = HERE / "golden_frames"
T = HERE / "templates"

fails = []


def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------- #
print("== 缩放与坐标换算 ==")
for k in (1.0, 0.5, 0.35):
    for p in [(0, 0), (1234, 987), (3839, 2159)]:
        got = to_full_point(to_frame_point(p, k), k)
        ck(f"坐标往返 scale={k} {p}", max(abs(got[0] - p[0]), abs(got[1] - p[1])) <= 2,
           f"-> {got}")
def golden(scene: str, pat: str = "*") -> list:
    """golden_frames/<scene>/ 里匹配 pat 的帧，**.png 和 .jpg 都算**。

    【2026-10-04】cleanup.py 为省磁盘（1.1GB→327MB）把大场景里除前 3 张外的无损 PNG
    转成了质量 95 的 JPEG（实测判据分差 ≤0.03、零阈值翻转）。自检若只挑 "*.png"，
    就会以为那批帧"不存在"→ 空列表 / IndexError。这里统一按扩展名过滤。
    """
    d = G / scene
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
                  and p.match(pat))


img = load_image(golden("challenge_result")[0], flags=3)
ck("scaled_frame 尺寸正确", scaled_frame(img, 0.5).shape[:2] == (1080, 1920),
   f"{img.shape} -> {scaled_frame(img, 0.5).shape}")

# --------------------------------------------------------------------------- #
print("\n== 读取标定结果 ==")
th_path = T / "thresholds.json"
if not th_path.is_file():
    print("[!] 缺少 templates/thresholds.json，请先跑  py -3.14 calibrate.py")
    sys.exit(2)
cal = json.loads(th_path.read_text(encoding="utf-8"))
matcher = TemplateMatcher(grayscale=True, threshold=0.8)


def det(name, **kw):
    """按标定结果构造 VisualDetector。"""
    c = cal["templates"][name]
    if not c["usable"]:
        raise AssertionError(f"模板 {name} 标定为不可用，自检需要可用模板")
    args = dict(roi=tuple(c["roi"]) if c["roi"] else None, scale=cal["scale"],
                threshold=c["threshold"], name=name)
    args.update(kw)
    return VisualDetector(matcher, str(T / f"{name}.png"), **args)


def frame(scene, i=0):
    return load_image(golden(scene)[i], flags=3)


WITH_Y = golden("car_mastery_page", "frame_*")[0]             # 精通页：还有可解锁
NO_Y = golden("car_mastery_page", "屏幕截图*180316*")[0]      # 精通页：已全部解锁

# --------------------------------------------------------------------------- #
print("\n== hint_unlock_all：滞回去抖（连续 2 帧才算出现，消失要连续 2 帧） ==")
d = det("hint_unlock_all", confirm_frames=2, release_frames=2)
f_have, f_none = load_image(WITH_Y, flags=3), load_image(NO_Y, flags=3)
r1 = d.observe(f_have)
ck("第 1 帧命中不确认（confirm=2）", r1 is None and d.hit_streak == 1,
   f"score={d.last_score:.3f}")
r2 = d.observe(f_have)
ck("第 2 帧确认出现", r2 is not None and d.confirmed, f"hit={r2}")
ck("确认分数高", d.last_score > 0.9, f"score={d.last_score:.3f}")
ck("命中坐标落在 ROI 内", r2 is not None and
   cal["templates"]["hint_unlock_all"]["roi"][0] <= r2.center[0] <=
   cal["templates"]["hint_unlock_all"]["roi"][0] + cal["templates"]["hint_unlock_all"]["roi"][2],
   f"center={r2.center if r2 else None}")
r3 = d.observe(f_none)
ck("未命中第 1 帧仍保持确认（release=2）", r3 is not None, f"confirmed={d.confirmed}")
r4 = d.observe(f_none)
ck("未命中第 2 帧后释放", r4 is None and not d.confirmed)

print("\n== 已全部解锁的页面（Apollo 截图）不应命中 ==")
d2 = det("hint_unlock_all", confirm_frames=1)
scores = [d2.observe(f_none) for _ in range(5)]
ck("连续 5 帧都不命中", all(s is None for s in scores), f"最高分 ~{d2.last_score:.3f}")

# --------------------------------------------------------------------------- #
print("\n== ROI 与全帧搜索结果一致、错 ROI 不命中 ==")
roi = cal["templates"]["hint_unlock_all"]["roi"]
d_roi = det("hint_unlock_all", confirm_frames=1)
d_full = det("hint_unlock_all", roi=None, confirm_frames=1)
d_wrong = det("hint_unlock_all", roi=(0, 0, 400, 200), confirm_frames=1)
a, b, c = d_roi.observe(f_have), d_full.observe(f_have), d_wrong.observe(f_have)
ck("ROI 命中与全帧命中位置一致", a is not None and b is not None and
   max(abs(a.center[0] - b.center[0]), abs(a.center[1] - b.center[1])) <= 3,
   f"roi={a.center if a else None} full={b.center if b else None}")
ck("分数一致（差 <= 0.02）", a is not None and b is not None and abs(a.score - b.score) <= 0.02,
   f"{a.score:.4f} vs {b.score:.4f}")
ck("ROI 指向错误区域时不命中", c is None, f"score={d_wrong.last_score:.3f}")

# --------------------------------------------------------------------------- #
print("\n== 降采样一致性（帧与模板必须同步缩放） ==")
d_10 = det("hint_unlock_all", confirm_frames=1, scale=1.0, threshold=0.73)
d_05 = det("hint_unlock_all", confirm_frames=1, scale=0.5, threshold=0.50)
h10, h05 = d_10.observe(f_have), d_05.observe(f_have)
ck("0.5 尺度仍能命中", h05 is not None, f"score={d_05.last_score:.3f}")
ck("0.5 与 1.0 命中位置偏差 <= 4px", h10 is not None and h05 is not None and
   max(abs(h10.center[0] - h05.center[0]), abs(h10.center[1] - h05.center[1])) <= 4,
   f"1.0={h10.center if h10 else None} 0.5={h05.center if h05 else None}")

# --------------------------------------------------------------------------- #
print("\n== 画面变化检测 ==")
f_a, f_b = frame("challenge_hud"), frame("garage_list")
ck("同一帧 dHash 距离 0", signature_distance(frame_signature(f_a), frame_signature(f_a)) == 0)
ck("不同场景 dHash 距离 > 8", signature_distance(frame_signature(f_a), frame_signature(f_b)) > 8,
   f"dist={signature_distance(frame_signature(f_a), frame_signature(f_b))}")
ck("同一帧灰度差 ~0", mean_abs_diff(f_a, f_a) < 1e-9)
ck("不同场景灰度差大", mean_abs_diff(f_a, f_b) > 10, f"diff={mean_abs_diff(f_a, f_b):.1f}")
_full_batches = []
for _ in range(5):
    _t = time.perf_counter()
    for _ in range(20):
        frame_signature(f_b)
    _full_batches.append((time.perf_counter() - _t) / 20 * 1000)
ms_full = min(_full_batches)          # 同上：取最小值，避免被系统负载放大
gray_small = scaled_frame(f_b, 0.5)
gray_small = gray_small.mean(axis=2).astype(np.uint8)
# 【2026-10-04】微基准用"多次取最小"：单次计时会被系统调度/别的进程干扰
# （实测同一台机器在 2.6~3.6ms 之间跳，偶尔越过 3ms 线 → 假失败）。
# 取最小值才是这段代码的真实代价，与本测试想守的"数量级"一致。
_batches = []
for _ in range(7):
    _t = time.perf_counter()
    for _ in range(50):
        frame_signature(gray_small)
    _batches.append((time.perf_counter() - _t) / 50 * 1000)
ms_small = min(_batches)
ck("dHash 直接吃 4K 彩帧 < 30ms", ms_full < 30.0,
   f"{ms_full:.2f} ms（5 批取最小；实测最好 ~17.5ms，留余量防负载抖动）")
ck("dHash 吃 1080p 灰度帧 < 5ms", ms_small < 5.0,
   f"{ms_small:.3f} ms（7 批取最小；实测最好 ~2.6ms，留余量防负载抖动；"
   f"比较本身 0.03ms，一次模板匹配约 100ms → 仍是两个数量级的差距）")


# --------------------------------------------------------------------------- #
class StubCapture:
    """只回放已有帧，不含任何输入仿真（自检专用）。"""

    def __init__(self, frames, cycle=False):
        self.frames = frames
        self.cycle = cycle
        self.i = 0

    def grab(self, region=None):
        if self.cycle:
            f = self.frames[self.i % len(self.frames)]
            self.i += 1
        else:
            f = self.frames[0]
        return f[region[1]:region[1] + region[3], region[0]:region[0] + region[2]] if region else f


print("\n== wait_stable：静止画面判稳定，变化画面判超时 ==")
t0 = time.perf_counter()
ok_static = wait_stable(StubCapture([f_a]), timeout=2.0, settle=0.1, scale=0.5, poll=0.02)
ck("画面不变 -> 稳定返回 True", ok_static and time.perf_counter() - t0 < 1.0,
   f"elapsed={time.perf_counter() - t0:.2f}s")
ok_moving = wait_stable(StubCapture([f_a, f_b, frame("popup_no_resource")], cycle=True),
                        timeout=0.4, settle=0.2, scale=0.5, poll=0.02)
ck("画面一直变 -> 超时返回 False", not ok_moving)

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:6]}")
sys.exit(1 if fails else 0)

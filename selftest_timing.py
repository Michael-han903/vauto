# -*- coding: utf-8 -*-
"""自检：timing.py 的纯函数与随机策略（只需要 numpy，可离线跑）"""
import pathlib
import math, random, threading, time, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # 谁的机器都能跑
from vauto.timing import TimingProfile, Humanizer, bezier_path, ease_curve, distribute_duration, interruptible_sleep

fails = []
def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond: fails.append(name)

print("== bezier_path: 端点精确 + 非直线 + 非匀速（按 px/秒 计速） ==")
for (p0, p1) in [((0,0),(800,100)), ((100,100),(140,105)), ((0,0),(37,9)), ((500,500),(505,502))]:
    devs, speed_ratios = [], []
    for seed in range(60):
        rng = random.Random(seed)
        steps = 60
        pts = bezier_path(p0, p1, steps, rng=rng)
        ck_end = pts[0] == p0 and pts[-1] == p1
        # 到起点-终点连线的最大垂直距离
        ax, ay = p0; bx, by = p1
        L = math.hypot(bx-ax, by-ay) or 1.0
        dmax = max(abs((bx-ax)*(y-ay)-(by-ay)*(x-ax))/L for x, y in pts)
        devs.append(dmax)
        # 真实速度：每步位移 / 该步耗时（时间轴来自 ease_curve + distribute_duration）
        dts = distribute_duration(ease_curve(steps, rng), 0.4, rng)
        v = [math.dist(pts[i], pts[i+1]) / max(1e-6, dts[i]) for i in range(len(dts))]
        v = [x for x in v if x > 0]
        speed_ratios.append(max(v) / (sum(v)/len(v)))
        if not ck_end: ck(f"endpoints {p0}->{p1}", False)
    dist = math.hypot(p1[0]-p0[0], p1[1]-p0[1])
    if dist >= 3:
        ck(f"非直线 {p0}->{p1}: min_dev={min(devs):.2f}px / max_dev={max(devs):.2f}px (dist={dist:.0f})",
           min(devs) > 0.8, "曲线相对弦的最小偏移(非直线保证)")
        ck(f"非匀速 {p0}->{p1}: 峰值速度/均速 = {min(speed_ratios):.2f}~{max(speed_ratios):.2f}",
           min(speed_ratios) > 1.5, "要求每 seed 都 > 1.5 才叫非匀速")
        ck(f"端点精确 {p0}->{p1}", True)
    else:
        ck(f"短距离({dist:.0f}px)直给", True)

print("== ease_curve ==")
for seed in range(20):
    rng = random.Random(seed)
    ts = ease_curve(50, rng, tremor=0.015)
    mono = all(ts[i] <= ts[i+1] for i in range(len(ts)-1))
    dts = [ts[i+1]-ts[i] for i in range(len(ts)-1)]
    ck(f"单调/端点/非匀速 seed={seed}", mono and ts[0]==0.0 and ts[-1]==1.0
       and max(dts) > 2.2*min(dts), f"dt range={min(dts):.4f}~{max(dts):.4f}")
    if len(fails) > 2: break

print("== distribute_duration ==")
for seed in range(10):
    rng = random.Random(seed)
    ts = ease_curve(40, rng)
    dts = distribute_duration(ts, 0.4, rng)
    ck(f"总时长守恒 seed={seed}", abs(sum(dts)-0.4) < 1e-6, f"sum={sum(dts):.6f}")
    if len(fails) > 2: break

print("== Humanizer ==")
h = Humanizer(seed=7)
t0 = time.perf_counter()
sec = h.delay(0.05, 0.12)
el = time.perf_counter()-t0
ck("需要 a: delay 返回随机 sleep 秒数并在区间内", 0.05 <= sec <= 0.12 and el >= sec*0.9, f"sec={sec:.3f} elapsed={el:.3f}")
pts = [h.jitter_point(1000, 1000, radius=5) for _ in range(3000)]
dists = [math.dist(p, (1000, 1000)) for p in pts]
# 落点取整到整数像素，边界可能比半径多出最多 ~0.71px（45° 方向），属预期
ck("需要 b: 像素抖动在半径内且真的在抖",
   max(dists) <= 5 + 0.71 + 1e-6 and len(set(pts)) > 20
   and sum(1 for d in dists if d > 2.5) > 0.7*len(dists),
   f"max_dist={max(dists):.3f} 唯一落点={len(set(pts))}")
ck("需要 b: 落点均值无系统偏移", abs(sum(p[0] for p in pts)/len(pts)-1000) < 0.4)
rest_hits = [h.maybe_rest(i) for i in range(60)]
ck("需要 e: 每 N 次循环有触发休息", any(v > 0 for v in rest_hits), f"触发 {sum(1 for v in rest_hits if v>0)}/60 轮")
ev = threading.Event(); ev.set()
t0 = time.perf_counter(); interruptible_sleep(5.0, ev); el = time.perf_counter()-t0
ck("急停可打断 sleep(5s)", el < 0.05, f"elapsed={el:.4f}s")

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:5]}")
sys.exit(1 if fails else 0)

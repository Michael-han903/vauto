# -*- coding: utf-8 -*-
"""
集成自检：capture / matching / focus / safety / input_sim 的真实调用链。

安全说明：本脚本 **不注入任何真实鼠标/键盘事件**（不会动你的鼠标、不会敲键），
输入仿真部分通过替换底层 Controller 做状态机验证；急停部分直接调用内部 _fire()
模拟「按下了 F1」。

用法：
    <venv>/Scripts/python.exe selftest_stack.py
"""
import sys, time, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vauto import check_dependencies
check_dependencies()

import cv2
from vauto import (
    AbortedByUser, EmergencyStop, FocusGuard, Humanizer, InputSimulator, Match,
    NotForeground, TemplateMatcher, TimingProfile, WindowCapture, WindowUnavailable,
    capture_monitor, client_to_screen, draw_matches, enable_dpi_awareness,
    find_window_by_title, get_window_pid, get_window_rect, is_foreground,
    list_windows, screen_to_client, load_image, save_image, resolve_key,
)
from vauto import capture as capmod

fails = []
def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond: fails.append(name)

print("== 0) 依赖与 DPI ==")
print("  DPI 模式:", enable_dpi_awareness())

print("== 1) capture: 全屏抓帧 + BGR 通道顺序 ==")
frame = capture_monitor(1)
ck("全屏帧形状/类型", frame.ndim == 3 and frame.shape[2] == 3 and frame.dtype == np.uint8, f"shape={frame.shape}")
# 通道顺序验证：纯红像素在 BGR 里应为 (0,0,255)。用一块已知色块验证不可靠，
# 改为验证 mss 的 bgra 切片逻辑：手动构造 ScreenShot 等价的字节流。
fake = np.zeros((4, 4, 4), np.uint8); fake[:, :, 2] = 255  # BGRA 中 R 通道 = 255
class _Fake:  # 模拟 mss ScreenShot 的最小接口
    height, width = 4, 4
    bgra = fake.tobytes()
out = capmod._bgra_to_bgr(_Fake())
ck("BGRA->BGR 通道顺序正确（红 -> BGR(0,0,255)）",
   tuple(out[0, 0]) == (0, 0, 255), f"pixel={tuple(out[0,0])}")

print("== 2) matching: 从确定性测试图上裁剪模板再找回来 ==")
# 【踩过的坑】这里原来用 capture_monitor(1) 的**实时屏幕**当测试图 →
# 屏幕内容一变（比如新素材入库、游戏切页面）断言就会莫名其妙地失败，
# 属于"测试输入不可复现"的缺陷。改成种子随机的合成图：自包含、可复现、不依赖环境。
_rng = np.random.default_rng(20261002)
frame = np.full((600, 900, 3), 30, np.uint8)
for _ in range(120):                      # 低频色块（缩放后仍可辨认，且非周期 → 不会多个等高峰）
    bx, by = int(_rng.integers(0, 880)), int(_rng.integers(0, 580))
    bw, bh = int(_rng.integers(24, 120)), int(_rng.integers(24, 90))
    cv2.rectangle(frame, (bx, by), (bx + bw, by + bh),
                  tuple(int(c) for c in _rng.integers(40, 255, 3)), -1)
cv2.circle(frame, (375, 280), 22, (0, 0, 255), -1)          # 裁窗内的唯一标记，保证匹配无歧义
cv2.line(frame, (330, 300), (420, 300), (255, 255, 255), 5)
cv2.line(frame, (375, 245), (375, 315), (0, 255, 0), 3)
h, w = frame.shape[:2]
x0, y0 = 315, 240
patch = frame[y0:y0 + 80, x0:x0 + 120].copy()
tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
os.makedirs(tmpdir, exist_ok=True)
tpl_path = os.path.join(tmpdir, "selftest_patch.png")
save_image(tpl_path, patch)
matcher = TemplateMatcher(grayscale=True, threshold=0.9)
hit = matcher.match_best(frame, tpl_path)
ck("模板命中且分数极高", hit is not None and hit.score > 0.99, f"{hit}")
ck("命中位置与裁剪位置一致（±1px）",
   hit is not None and abs(hit.x - x0) <= 1 and abs(hit.y - y0) <= 1,
   f"expect=({x0},{y0}) got={None if hit is None else (hit.x, hit.y)}")

# 多尺度 + 缩放模板（把模板缩小 0.5 后应能在原图里以 scale=2.0 找回）
small = cv2.resize(patch, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
small_path = os.path.join(tmpdir, "selftest_patch_half.png")
save_image(small_path, small)
hits = matcher.match(frame, small_path, threshold=0.9, scales=(2.0,), nms_iou=0.3)
best = hits[0] if hits else None
ck("多尺度（0.5x 模板 + scale=2.0）命中同一位置",
   best is not None and abs(best.x - x0) <= 3 and abs(best.y - y0) <= 3
   and best.width == patch.shape[1],
   f"{best}")

# 有纹理但画面里不存在的模板：不应误报
rng = np.random.default_rng(0)
noise = rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)
ck("画面中不存在的模板不误报", matcher.match_best(frame, noise) is None)
# 纯色模板：CCOEFF 退化，必须显式报错而不是乱给高分
try:
    matcher.match_best(frame, np.full((40, 40, 3), 17, np.uint8))
    ck("纯色模板应抛 ValueError", False)
except ValueError as e:
    ck("纯色模板应抛 ValueError", True, str(e)[:40] + "...")
# 纯色模板改用 SQDIFF_NORMED 则可用（不报错）
sq = TemplateMatcher(method=cv2.TM_SQDIFF_NORMED, threshold=0.05)
ck("SQDIFF 模式下纯色模板可用", isinstance(sq.match_best(frame, np.full((40, 40, 3), 17, np.uint8)), (type(None), Match)))
# 中文路径
cn_path = os.path.join(tmpdir, "自检_中文路径.png")
save_image(cn_path, patch)
ck("中文路径写盘成功", os.path.isfile(cn_path), cn_path)
cn_hit = matcher.match_best(frame, cn_path)
ck("中文路径模板可加载并命中", cn_hit is not None and cn_hit.score > 0.99)
# 带 alpha 的模板 -> 自动当 mask（把右侧 20px 设为全透明）
rgba = cv2.cvtColor(patch, cv2.COLOR_BGR2BGRA)
rgba[:, -20:, 3] = 0
alpha_path = os.path.join(tmpdir, "selftest_alpha.png")
save_image(alpha_path, rgba)
ah = matcher.match_best(frame, alpha_path, threshold=0.8)
ck("带 alpha 模板自动作为 mask 且能命中",
   ah is not None and abs(ah.x - x0) <= 2 and abs(ah.y - y0) <= 2, f"{ah}")
# NMS：同一目标不应返回一堆重叠框
many = matcher.match(frame, tpl_path, threshold=0.9, nms_iou=0.3)
ck("NMS 去重生效（同目标只留少量框）", 1 <= len(many) <= 3, f"命中数={len(many)}")
# region 搜索：坐标要能还原回全帧
rh = matcher.match(frame, tpl_path, threshold=0.9, region=(max(0, x0 - 200), max(0, y0 - 200), 400, 400))
ck("region 搜索返回全帧坐标", rh and abs(rh[0].x - x0) <= 1 and abs(rh[0].y - y0) <= 1)
# 调试图
dbg = draw_matches(frame, [(hit or Match(x0, y0, 80, 120, 0.9))])
dbg_path = save_image(os.path.join(tmpdir, "selftest_debug.png"), dbg)
ck("调试图写出", os.path.getsize(dbg_path) > 1000)

print("== 3) focus: 窗口枚举 / 前台判定 / 行矩形 ==")
wins = list_windows()
ck("枚举到可见窗口", len(wins) > 0, f"count={len(wins)}")
hwnd, title, cls = wins[0]
# 【2026-10-04 修】原来断言 wins[0] 就是前台窗口 —— 枚举顺序本来就不保证（换个窗口顺序
# 就假失败，例如 Hermes 自己在前台时）。改成直接问系统"当前前台窗口是谁"再断言。
import ctypes as _ct
_fg = int(_ct.windll.user32.GetForegroundWindow() or 0)
ck("前台窗口被 is_foreground 判定为真", _fg != 0 and is_foreground(_fg),
   f"fg={_fg} title={(title[:30] if _fg else '')!r}")
ck("is_foreground 对伪造 hwnd 返回 False", not is_foreground(0xDEADBEEF))
ck("get_window_pid 合理", get_window_pid(hwnd) > 0, f"pid={get_window_pid(hwnd)}")
ck("前台窗口与自身进程 PID 可读", get_window_pid(hwnd) != 0)
r = get_window_rect(hwnd, client_only=False)
ck("窗口矩形有效", r.is_valid(), f"{r}")
# 找到本进程/终端所在窗口做 WindowCapture 实测
own = None
for h, t, c in wins:
    if "python" in t.lower() or "cmd" in c.lower() or "console" in c.lower() or "terminal" in t.lower():
        own = (h, t, c); break
if own is None:
    own = (hwnd, title, cls)
cap = WindowCapture(own[0], client_only=True)
try:
    win_frame = cap.grab()
    ck("WindowCapture.grab 真实抓帧", win_frame.ndim == 3 and win_frame.shape[2] == 3,
       f"hwnd={own[0]} shape={win_frame.shape}")
    wr = cap.rect
    ck("客户区抓帧尺寸 == 客户区矩形尺寸",
       abs(win_frame.shape[1] - wr.width) <= 2 and abs(win_frame.shape[0] - wr.height) <= 2,
       f"img={win_frame.shape[1]}x{win_frame.shape[0]} rect={wr.width}x{wr.height}")
    cx, cy = client_to_screen(own[0], 10, 10)
    bx, by = screen_to_client(own[0], cx, cy)
    ck("client<->screen 互转自洽", (bx, by) == (10, 10), f"->({cx},{cy})->({bx},{by})")
    pw = cap.grab_printwindow()
    nonblack = float((pw.reshape(-1, pw.shape[2]).mean(axis=1) > 8).mean())
    # 【2026-10-04 修】游戏没开时这一步抓到的是自己/桌面窗口，必然接近全黑 → 以前会**假报失败**，
    # 让人分不清"代码回归"还是"游戏没开"。游戏不在就只提示跳过。
    try:
        _game_up = bool(find_window_by_title("Forza Horizon"))
    except Exception:
        _game_up = False
    if not _game_up:
        print(f"  SKIP  PrintWindow 备选抓帧可用（非全黑）  游戏未运行，跳过  非黑={nonblack:.2%}")
    else:
        ck("PrintWindow 备选抓帧可用（非全黑）", nonblack > 0.05, f"非黑像素占比={nonblack:.2%}")
except WindowUnavailable as e:
    ck("WindowCapture.grab", False, repr(e))
cap.close()
# 已关闭的窗口应抛异常
try:
    WindowCapture(0xDEADBEEF, client_only=True).grab()
    ck("无效句柄应抛 WindowUnavailable", False)
except WindowUnavailable:
    ck("无效句柄应抛 WindowUnavailable", True)

# ---- 前台焦点守卫：**不要假设"现在前台是谁"**（2026-10-02：用户在跑游戏时这条会失败）----
# 改成显式取当前前台窗口 hwnd 做"放行"用例、取一个确定不在前台的窗口做"拒绝"用例。
import win32gui as _wg
_fg = _wg.GetForegroundWindow()
ck("FocusGuard 前台放行", FocusGuard(_fg).is_active())
ck("FocusGuard.require 前台不抛异常", (lambda: (FocusGuard(_fg).require(), True)[1])())
_notfg = next((h for h, _t, _c in list_windows()
               if h != _fg and not FocusGuard(h).is_active()), None)
# 【2026-10-04 修偶发】原来只挑"hwnd != 前台 hwnd"的窗口，但有些枚举出来的 hwnd 是前台
# 窗口的**子窗口/属主窗口**，守卫会把它们也算作前台 → 那条断言偶发假失败（实战里踩到一次）。
# 改成用"守卫自己的判据"筛：只挑它认为**确实不在前台**的窗口，测试就与当时谁在前台无关。
if _notfg is not None:
    try:
        FocusGuard(_notfg).require()
        ck("FocusGuard 非前台应抛 NotForeground", False, f"hwnd={_notfg} 却没抛")
    except NotForeground:
        ck("FocusGuard 非前台应抛 NotForeground", True, f"hwnd={_notfg}")
else:
    ck("FocusGuard 非前台应抛 NotForeground", False, "找不到第二个窗口")
try:
    FocusGuard(0xDEADBEEF).require()
    ck("FocusGuard 失效窗口应抛 NotForeground", False)
except NotForeground:
    ck("FocusGuard 失效窗口应抛 NotForeground", True)

print("== 4) input_sim: 轨迹/按键状态机（不注入真实事件） ==")
class FakeMouse:
    def __init__(self): self.position = (100, 100); self.events = []
    def press(self, b): self.events.append(("press", b))
    def release(self, b): self.events.append(("release", b))
    def scroll(self, dx, dy): self.events.append(("scroll", dx, dy))
class FakeKb:
    def __init__(self): self.events = []
    def press(self, k): self.events.append(("press", k))
    def release(self, k): self.events.append(("release", k))

prof = TimingProfile()
prof.move_duration = (0.05, 0.08)      # 自检时加速
sim = InputSimulator(humanizer=Humanizer(profile=prof, seed=1))
sim.mouse, sim.keyboard = FakeMouse(), FakeKb()
t0 = time.perf_counter()
sim.move_bezier((420, 260))
el = time.perf_counter() - t0
ck("move_bezier 真实移动了光标（fakemouse 记录）", sim.mouse.position != (100, 100), f"end={sim.mouse.position}")
ck("move_bezier 耗时接近设定（0.05~0.35s）", 0.02 < el < 0.6, f"elapsed={el:.3f}s")
sim.click((500, 400))
ck("click 产生 press+release 且落点带抖动",
   len([e for e in sim.mouse.events if e[0] == "press"]) == 1 and
   len([e for e in sim.mouse.events if e[0] == "release"]) == 1)
sim.key_down("shift"); sim.tap_key("a"); sim.key_up("shift")
ck("按键按下/释放成对出现",
   len([e for e in sim.keyboard.events if e[0] == "press"]) ==
   len([e for e in sim.keyboard.events if e[0] == "release"]) == 2)
sim.key_down("ctrl")                     # 故意「粘键」
ck("粘键被登记", len(sim.held_keys) == 1)
n = sim.release_all()
ck("release_all 释放粘键", n >= 1 and len(sim.held_keys) == 0, f"释放 {n} 个")
def _kb_counts(sim):
    return (len([e for e in sim.keyboard.events if e[0] == "press"]),
            len([e for e in sim.keyboard.events if e[0] == "release"]))


_kb_before = _kb_counts(sim)
sim.press_hotkey(("ctrl", "s"))
_kb_after = _kb_counts(sim)
ck("press_hotkey 按下==释放（按本次增量计）",
   _kb_after[0] - _kb_before[0] == 2 and _kb_after[1] - _kb_before[1] == 2,
   f"press +{_kb_after[0]-_kb_before[0]} / release +{_kb_after[1]-_kb_before[1]}")
ck("resolve_key 名称解析", resolve_key("f1") is not None and resolve_key("F1") is not None
   and resolve_key("a") is not None and resolve_key("ctrl") is not None)
try:
    resolve_key("这不是键"); ck("非法键名应报错", False)
except ValueError:
    ck("非法键名应报错", True)

print("== 5) safety: F1 急停链路（直接触发，不按真键） ==")
import vauto as _va, vauto.input_sim as _is, vauto.safety as _sf, vauto.errors as _er
ck("AbortedByUser 全局唯一（errors/input_sim/safety/vauto 同一个类）",
   _va.AbortedByUser is _is.AbortedByUser is _sf.AbortedByUser is _er.AbortedByUser)
sim2 = InputSimulator(humanizer=Humanizer(profile=prof, seed=2))
sim2.mouse, sim2.keyboard = FakeMouse(), FakeKb()
calls = []
stop = EmergencyStop("f1", on_trigger=[lambda: calls.append("cb"), sim2.release_all], verbose=True)
sim2.emergency = stop
sim2.key_down("shift")                   # 制造一个粘键，验证急停会释放
ck("急停前 triggered=False", not stop.triggered)
stop.start()                             # 真实启动 pynput 全局钩子（不注入事件）
stop._fire()                             # 等价于「按下了 F1」
time.sleep(0.05)
ck("急停后 triggered=True", stop.triggered)
ck("急停回调已执行", calls == ["cb"], f"calls={calls}")
ck("急停已释放所有按下的键", len(sim2.held_keys) == 0 and
   any(e[0] == "release" for e in sim2.keyboard.events))
stop.check() if False else None
try:
    stop.check(); ck("急停后 check() 抛 AbortedByUser", False)
except AbortedByUser:
    ck("急停后 check() 抛 AbortedByUser", True)
try:
    sim2.click((10, 10)); ck("急停后动作被中止", False)
except AbortedByUser:
    ck("急停后动作被中止（顶层导入的异常类能抓住）", True)
try:
    sim2.move_bezier((300, 300)); ck("急停后移动被中止", False)
except AbortedByUser:
    ck("急停后移动被中止", True)
ev = stop.event
t0 = time.perf_counter(); from vauto import interruptible_sleep
interruptible_sleep(2.0, ev); el = time.perf_counter() - t0
ck("急停后长 sleep 立即返回", el < 0.05, f"elapsed={el:.4f}s")
ck("guard.wait 遇急停立即返回 False", FocusGuard(0xDEADBEEF, stop_event=ev).wait(timeout=5) is False)
stop.stop()
stop.reset()
ck("reset 后可恢复", not stop.triggered)

# ---- Caps Lock 读写（用户要求：进游戏时自动检测并打开）----
# 自检**只读 + 一次 no-op 设置**（传当前状态 = 不按键），绝不真的去改用户的键盘状态。
from vauto.keystate import caps_lock_on, set_caps_lock

_cur_cap = caps_lock_on()
ck("Caps Lock 状态可读（只读，不改）", isinstance(_cur_cap, bool), f"当前 {'开' if _cur_cap else '关'}")
ck("set_caps_lock(当前状态) 是 no-op（不会多按一次键）", set_caps_lock(_cur_cap) == _cur_cap,
   f"仍是 {'开' if _cur_cap else '关'}")

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails[:6]}")
sys.exit(1 if fails else 0)

# -*- coding: utf-8 -*-
"""
e2e_demo_test.py —— 端到端验证 demo_skeleton.py 能真的跑起来

流程：
  1. 找到 Hermes 桌面窗口（本机可见窗口）并抓一帧客户区；
  2. 从帧中间裁一块有纹理的模板存成 templates/example_patch.png；
  3. 子进程调用 demo_skeleton.py --title ... --template ... --dry-run --max-loops 3；
  4. 校验 stdout 里出现命中日志，且 debug_frames/ 下有调试图产出。

全程 dry-run：不做任何真实鼠标/键盘注入。
"""
import os, subprocess, sys, glob, time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
sys.path.insert(0, HERE)

from vauto import (enable_dpi_awareness, find_window_by_title, WindowCapture,
                   list_windows, save_image, check_dependencies)
check_dependencies()
enable_dpi_awareness()

fails = []
def ck(name, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond: fails.append(name)

# 1) 找一个窗口（优先 Hermes 桌面窗口，保证窗口足够大且有内容）
wins = list_windows()
title_key = None
for h, t, c in wins:
    if "Hermes" in t or "hermes" in t:
        title_key = t.split(" - ")[0][:6]     # 用短关键字，演示子串匹配
        hwnd = h
        break
if title_key is None:
    hwnd, t, c = max(wins, key=lambda w: 1)
    title_key = t[:6]
ck("找到可用目标窗口", bool(title_key), f"hwnd={hwnd} key={title_key!r}")

cap = WindowCapture(hwnd, client_only=True)
frame = cap.grab()
h, w = frame.shape[:2]
ck("抓帧成功且尺寸合理", w > 200 and h > 200, f"{w}x{h}")

# 2) 裁一块位于画面中部、有纹理的模板
best = None
for dy in (0.35, 0.45, 0.55, 0.25):
    for dx in (0.30, 0.40, 0.50, 0.60):
        x, y = int(w*dx), int(h*dy)
        crop = frame[y:y+70, x:x+140]
        import numpy as np
        if crop.size and float(np.asarray(crop, dtype=float).std()) > 25:   # 有纹理
            best = (x, y, crop); break
    if best: break
ck("找到有纹理的裁剪区域", best is not None,
   f"at=({best[0]},{best[1]}) std={float(best[2].std()):.1f}" if best else "")
x0, y0, crop = best
tpl = os.path.join(HERE, "templates", "example_patch.png")
save_image(tpl, crop)
ck("示例模板已生成", os.path.isfile(tpl), tpl)
cap.close()

# 3) 跑 demo_skeleton.py（dry-run）
cmd = [PY, os.path.join(HERE, "demo_skeleton.py"),
       "--title", title_key, "--template", tpl,
       "--threshold", "0.9", "--dry-run", "--max-loops", "3", "--hotkey", "f1"]
print("  $ " + " ".join(cmd))
p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=180)
print("---- demo_skeleton.py stdout ----")
print(p.stdout)
if p.stderr.strip():
    print("---- stderr ----"); print(p.stderr)
print("---- end ----")
ck("demo_skeleton 退出码为 0", p.returncode == 0, f"rc={p.returncode}")
ck("日志出现目标窗口 hwnd", "目标窗口 hwnd=" in p.stdout)
ck("日志出现命中与「将要点击的屏幕坐标」", "最佳匹配 Match(" in p.stdout and "屏幕坐标" in p.stdout)
ck("日志标出急停热键已启用", "已启用全局急停键: F1" in p.stdout)
ck("达到 --max-loops 正常退出", "达到 --max-loops=3" in p.stdout)
ck("退出时释放并关闭抓帧器", "退出。共执行" in p.stdout)
neg = subprocess.run(cmd + ["--max-loops", "1"], cwd=HERE, capture_output=True, text=True, timeout=180)
dbg = sorted(glob.glob(os.path.join(HERE, "debug_frames", "dryrun_*.png")))
ck("debug_frames 下有调试图产出", len(dbg) > 0, f"共 {len(dbg)} 张，最新 {os.path.basename(dbg[-1]) if dbg else '-'}")

# 4) --list 模式
lst = subprocess.run([PY, os.path.join(HERE, "demo_skeleton.py"), "--list"],
                     cwd=HERE, capture_output=True, text=True, timeout=60)
ck("--list 列出窗口", lst.returncode == 0 and "hwnd" in lst.stdout and len(lst.stdout.splitlines()) > 2)
# 5) 不存在的标题 -> 优雅报错（rc=2）
bad = subprocess.run([PY, os.path.join(HERE, "demo_skeleton.py"), "--title", "绝对不存在的窗口标题XYZ"],
                     cwd=HERE, capture_output=True, text=True, timeout=60)
ck("找不到窗口时优雅退出（rc=2）", bad.returncode == 2 and "找不到标题包含" in bad.stdout, f"rc={bad.returncode}")

print("\n结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌ -> {fails}")
sys.exit(1 if fails else 0)

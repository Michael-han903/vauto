"""把「当前版本真正需要的东西」打包成一个干净文件夹放到桌面。

用户要求（2026-10-06）："把这版本所需要的一切打包到一个文件夹，存放在 C:\\Desktop，
不需要记录报错的日志以及多余的截图视频什么的，只保留最核心的全功能可运行部分"。

规则：
  收：vauto.exe / _internal/（Python 运行时 + 依赖）/ templates/（判据模板，运行时必需）/
      docs/（说明书，几十 KB）/ 启动说明.txt / logs/ledger.json（他的累计数据，**不是**报错日志）
  不收：logs/*.png（433 张证据截图 ≈ 1.2GB）、logs/*.jsonl（每次运行的日志）、任何视频
"""
import shutil
import sys
from pathlib import Path

SRC = Path(r"C:\Users\lziha\visual_auto_toolkit\dist\vauto")
DST = Path(r"C:\Desktop") / "基于挑战蓝图的地平线六刷技能点软件 v0.2.0"

if not (SRC / "vauto.exe").exists():
    print("[X] 没找到 dist/vauto/vauto.exe —— 先打包")
    sys.exit(1)

if DST.exists():
    print(f"[i] 目标已存在，先删掉：{DST}")
    shutil.rmtree(DST)
DST.mkdir(parents=True)

# 1) exe + 运行时目录 + 模板 + 文档 + 说明
for name in ("vauto.exe", "_internal", "templates", "docs", "启动说明.txt"):
    s = SRC / name
    if not s.exists():
        print(f"[!] 源里没有 {name}，跳过")
        continue
    t = DST / name
    if s.is_dir():
        shutil.copytree(s, t)
    else:
        shutil.copy2(s, t)
    print(f"  + {name}")

# 2) 只保留他的账本（累计数据），不要日志与截图
logs = DST / "logs"
logs.mkdir(exist_ok=True)
led = SRC / "logs" / "ledger.json"
if led.exists():
    shutil.copy2(led, logs / "ledger.json")
    print("  + logs/ledger.json（你的累计数据；不想要可以直接删掉这个文件）")
else:
    print("  （没有 ledger.json，logs/ 留空即可）")

# 3) 自检：包内不该有任何截图/日志/视频
bad = [p for p in DST.rglob("*")
       if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".jsonl", ".mp4", ".avi", ".mkv")
       and "_internal" not in p.parts]          # _internal 里是程序自带的图标/金标帧，不算"多余截图"
print(f"\n包外多余文件检查：{len(bad)} 个" + (f" ✗ {bad[:5]}" if bad else " ✓"))

total = sum(p.stat().st_size for p in DST.rglob("*") if p.is_file())
print(f"总大小：{total / 1024 / 1024:.0f} MB")
print(f"目标：{DST}")

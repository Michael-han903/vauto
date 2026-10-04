# -*- coding: utf-8 -*-
"""cleanup.py —— vauto 磁盘占用清理（默认**只报告**，加 --apply 才真删）

背景（2026-10-04 用户问「这个项目是不是有点占用太多了」）：
量出来约 3.9 GB，全是**可再生的调试素材**，源码/模板/文档/日志正文只占几 MB：

    logs/            1411 MB  实跑证据图（每次跑几十张 4K PNG）
    dist/            1.2 GB   打包产物 + 一个 343MB 的发布 zip（dist 的压缩副本）
    golden_frames/   1.1 GB   标定用金标帧（每张约 10MB 的 4K 无损 PNG）
    build/ + 零碎     30 MB   PyInstaller 中间产物 / 旧压缩包

这个脚本把三件事分开，**只删"重新跑一遍就能再有"的东西**：

  1) 构建垃圾      build/ __pycache__/ *.spec vauto.bundle *.zip(旧) debug_frames/ _car/
  2) 证据图瘦身    每个日志目录只留最新 N 张 PNG（默认 40）；.jsonl / ledger / 配置全留
  3) 金标帧瘦身    每个场景只留前 K 张（默认 3）—— 回放自检只需要 challenge_hud/result 各 3 张

**绝不碰**：源码(flow/ vauto/ *.py)、templates/、docs/、.git/、.gh_token、
logs/*.jsonl（日志正文）、ledger.json（累计账本）、vauto_config.json（你的设置）。

用法：
    py -3.14 cleanup.py                      # 只报告（推荐先看这个）
    py -3.14 cleanup.py --apply              # 执行上面 1)2)3)
    py -3.14 cleanup.py --apply --keep-images 100 --keep-golden 5
    py -3.14 cleanup.py --apply --purge-dist-zip    # 连 dist 里的旧发布 zip 一起删
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:                      # Windows 控制台默认 GBK，打印 ✓ 之类会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------- #
# 白名单：任何情况下都不删（就算传了 --apply）
# --------------------------------------------------------------------------- #
PROTECTED_DIRS = {".git", ".github", "flow", "vauto", "templates", "docs",
                  "logs\\replay", "logs/replay"}
PROTECTED_NAMES = {".gh_token", "_repo_full_name.txt", "vauto_config.json",
                   "ledger.json", "_pkgs.txt"}


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _size_of(paths) -> int:
    total = 0
    for p in paths:
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
    return total


def _is_protected(p: Path) -> bool:
    try:
        rel = p.relative_to(ROOT).as_posix()
    except ValueError:
        return True
    if p.name in PROTECTED_NAMES:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in PROTECTED_DIRS)


# --------------------------------------------------------------------------- #
# 1) 构建垃圾：重新跑一遍 build_exe.py / 导入一次就有的东西
# --------------------------------------------------------------------------- #
def build_junk() -> list[Path]:
    out: list[Path] = []
    for name in ("build", "__pycache__", "debug_frames", "_car"):
        p = ROOT / name
        if p.exists():
            out.append(p)
    for pat in ("**/__pycache__",):
        out += [p for p in ROOT.glob(pat) if p.is_dir() and p not in out]
    for pat in ("*.spec", "vauto.bundle", "vauto-toolkit.zip", "calib_out.txt",
                "${fileNameWithoutExt}.html", "_sanitize_paths.py"):
        p = ROOT / pat
        if p.exists():
            out.append(p)
    return [p for p in out if not _is_protected(p)]


# --------------------------------------------------------------------------- #
# 2) 证据图瘦身：每个日志目录只留最新 N 张
# --------------------------------------------------------------------------- #
def evidence_prune(keep: int) -> tuple[list[Path], int]:
    """返回（要删的图, 保留的图数量）。保留规则：按修改时间倒序留前 keep 张。"""
    victims: list[Path] = []
    kept = 0
    for d in (ROOT / "logs", ROOT / "dist" / "vauto" / "logs"):
        if not d.is_dir():
            continue
        pngs = sorted((p for p in d.glob("*.png") if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        kept += min(keep, len(pngs))
        victims += pngs[keep:]
    return victims, kept


# --------------------------------------------------------------------------- #
# 3) 金标帧瘦身：**不删帧**，把大场景的无损 PNG 转成高质量 JPEG（省 80% 空间）
#
#    为什么不直接删：golden_frames/ 是 calibrate.py 的输入，删了就"以后没法重新标定"
#    （他要换分辨率/换场景时得重新进游戏录一遍，那是他的时间）。转 JPEG 画面还在，
#    标定/自检都照样能跑；每个场景的**前 K 张保留原 PNG**（offline_replay 按
#    sorted(glob("*.png"))[:3] 取片，py 里按文件名写死的那些帧也必须还是 PNG）。
# --------------------------------------------------------------------------- #
# 源码/文档里按名字写死引用过的场景 → 整目录保持 PNG 不动
PROTECT_SCENES = {
    "event_entry",              # entry_05/06/10/11 被 calibrate.py 按文件名引用
    "current_car_22b_garage", "current_car_22b_menu", "current_car_22b_strip",
    "menu_vehicle_tab", "menu_select_action", "popup_not_enough",
}
# 会被转 JPEG 的大场景（glob-only；标定把它们当正/负样本，转完仍是有效样本）
CONVERT_SCENES = {"challenge_hud", "challenge_result", "garage_list",
                  "popup_no_resource", "car_mastery_page", "node_active",
                  "node_inactive"}


def golden_shrink(keep_png: int = 3, quality: int = 95,
                  purge: bool = False) -> tuple[int, int, int]:
    """返回（处理张数, 回收字节, 删除张数）。purge=True 时直接删而不是转 JPEG。"""
    converted, freed, deleted = 0, 0, 0
    gf = ROOT / "golden_frames"
    if not gf.is_dir():
        return converted, freed, deleted
    try:
        import cv2
        import numpy as np
    except Exception:
        print("  [!] 没有 cv2，跳过金标帧瘦身")
        return converted, freed, deleted
    for d in sorted(p for p in gf.iterdir() if p.is_dir()):
        if d.name in PROTECT_SCENES and not purge:
            continue
        pngs = sorted(p for p in d.glob("*.png") if p.is_file())
        for p in pngs[keep_png:]:
            before = p.stat().st_size
            try:
                if purge:
                    p.unlink()
                    deleted += 1
                else:
                    img = cv2.imdecode(np.fromfile(str(p), dtype="uint8"), cv2.IMREAD_COLOR)
                    if img is None:
                        continue
                    jpg = p.with_suffix(".jpg")
                    ok, buf = cv2.imencode(".jpg", img,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
                    if not ok:
                        continue
                    buf.tofile(str(jpg))
                    if not jpg.exists() or jpg.stat().st_size == 0:
                        continue
                    p.unlink()                      # 确认写成功才删原图
                    converted += 1
                freed += before - (0 if purge else jpg.stat().st_size)
            except Exception as exc:
                print(f"    [!] {p.name}: {exc}")
    return converted, freed, deleted


# --------------------------------------------------------------------------- #
# 4) dist 里旧的发布 zip（默认只报告）
# --------------------------------------------------------------------------- #
def dist_zips() -> list[Path]:
    d = ROOT / "dist"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.zip") if p.is_file())


def _report(title: str, victims: list[Path], total: int, extra: str = "") -> None:
    print(f"\n【{title}】要删 {len(victims)} 项，可回收 {_mb(total)}" + (f"  {extra}" if extra else ""))
    for p in victims[:8]:
        print(f"    - {p.relative_to(ROOT).as_posix()}")
    if len(victims) > 8:
        print(f"    … 还有 {len(victims) - 8} 项")


def _delete(victims: list[Path], label: str) -> int:
    freed, failed = 0, 0
    for p in victims:
        if _is_protected(p):                     # 双保险
            continue
        n = _size_of([p])
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            else:
                p.unlink()
            freed += n
        except Exception as exc:
            failed += 1
            print(f"    [!] 删不掉 {p.name}：{exc}")
    print(f"  [{label}] 已回收 {_mb(freed)}" + (f"，{failed} 项失败" if failed else ""))
    return freed


def main() -> int:
    ap = argparse.ArgumentParser(description="vauto 磁盘占用清理（默认只报告）")
    ap.add_argument("--apply", action="store_true", help="真的删（不加就是只报告）")
    ap.add_argument("--keep-images", type=int, default=40,
                    help="每个日志目录保留最新几张证据图（默认 40；0 = 全清）")
    ap.add_argument("--keep-golden", type=int, default=3,
                    help="每个场景保留前几张原 PNG（默认 3；其余转 JPEG）")
    ap.add_argument("--golden-quality", type=int, default=95,
                    help="金标帧转 JPEG 的质量（默认 95；97 更清晰但更大）")
    ap.add_argument("--purge-golden", action="store_true",
                    help="金标帧**直接删**而不是转 JPEG（省更多，但以后重新标定要重新录素材）")
    ap.add_argument("--purge-dist-zip", action="store_true",
                    help="连 dist/*.zip（旧发布包，dist 的压缩副本）一起删")
    args = ap.parse_args()

    print("vauto 清理工具 —— 根目录:", ROOT)
    print("（不加 --apply 就是只报告，一个文件都不会动）")

    junk = build_junk()
    ev, ev_kept = evidence_prune(args.keep_images)
    zips = dist_zips()

    _report("构建垃圾（build/、__pycache__、旧压缩包、探针输出）", junk, _size_of(junk))
    _report("证据图瘦身（每个日志目录留最新 %d 张，日志正文 .jsonl 全留）" % args.keep_images,
            ev, _size_of(ev), f"（保留 {ev_kept} 张）")

    # 金标帧：算一遍"如果现在处理，能省多少"（不动手）
    gf = ROOT / "golden_frames"
    g_plan, g_plan_bytes, g_total = 0, 0, 0
    if gf.is_dir():
        for d in sorted(p for p in gf.iterdir() if p.is_dir()):
            if d.name in PROTECT_SCENES and not args.purge_golden:
                continue
            pngs = sorted(p for p in d.glob("*.png") if p.is_file())
            g_total += len(pngs)
            for p in pngs[args.keep_golden:]:
                g_plan += 1
                g_plan_bytes += p.stat().st_size
    if args.purge_golden:
        print(f"\n【金标帧：直接删】要删 {g_plan} 张，可回收 {_mb(g_plan_bytes)}"
              f"  ← 注意：以后重新标定要重新录素材")
    else:
        print(f"\n【金标帧：转 JPEG（画面全保留，能重新标定）】"
              f"要处理 {g_plan} 张，可回收约 {_mb(int(g_plan_bytes * 0.88))}"
              f"  （每个场景前 {args.keep_golden} 张与按名字引用的场景保持原 PNG；"
              f"共 {g_total} 张参与）")

    if zips:
        _report("dist 里的旧发布 zip（需要时可用 build_exe.py + 压缩重做）"
                + ("" if args.purge_dist_zip else "  ← 需要 --purge-dist-zip 才会删"),
                zips, _size_of(zips))

    gold_saving = g_plan_bytes if args.purge_golden else int(g_plan_bytes * 0.88)
    reclaimable = _size_of(junk) + _size_of(ev) + gold_saving
    print(f"\n合计可回收：约 {_mb(reclaimable)}"
          + (f"（再加 zip 共 {_mb(reclaimable + _size_of(zips))}）" if zips else ""))

    if not args.apply:
        print("\n这是**报告模式**。确认没问题就加 --apply 执行：")
        print("    py -3.14 cleanup.py --apply")
        return 0

    print("\n开始清理…")
    freed = 0
    freed += _delete(junk, "构建垃圾")
    freed += _delete(ev, "证据图")
    conv, g_freed, dele = golden_shrink(args.keep_golden, args.golden_quality,
                                        purge=args.purge_golden)
    if args.purge_golden:
        print(f"  [金标帧] 已删 {dele} 张，回收 {_mb(g_freed)}")
    else:
        print(f"  [金标帧] 已转 JPEG {conv} 张，回收 {_mb(g_freed)}（画面保留，可重新标定）")
    freed += g_freed
    if args.purge_dist_zip:
        freed += _delete(zips, "dist zip")
    print(f"\n✓ 清理完成，共回收 {_mb(freed)}")
    print("  说明：日志正文（logs/*.jsonl）、累计账本（ledger.json）、你的设置")
    print("        （vauto_config.json）、模板、文档、源码一律没动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

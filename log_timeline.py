# -*- coding: utf-8 -*-
"""
log_timeline —— 把一次实跑的 JSONL 日志 + 证据图，变成一张**能直接看的复盘时间线**（HTML）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本工具只是把日志渲染成网页，不含任何对游戏的自动化能力。
主程序（vauto / flow）仅用于算法学习（计算机视觉 / 输入仿真研究）；
用它操作游戏可能违反游戏用户许可协议（EULA），并可能触发风控 / 反作弊，
存在账号被封禁等风险，请自行承担后果。
======================================================================

用法
----
    py -3.14 log_timeline.py                     # 取 logs/ 里最新的 run_*.jsonl
    py -3.14 log_timeline.py logs/run_xxx.jsonl  # 指定一次
    py -3.14 log_timeline.py -o out.html         # 指定输出（默认 logs/timeline_<名字>.html）

它做什么
--------
* 按时间排出每个事件，标出"什么时候停的、为什么停的"（abort / crash / *_fail / timeout …）；
* 把 `shot=` 指向的证据图**直接嵌在时间线上**（点开就是当时那一屏），
  "程序说它看到 X、实际屏幕是 Y" 这类问题一眼就能对；
* 顶部给一张**判据分数表**（每个检测器出现过的最高分 / 阈值），复盘判据是否有余量；
* 右侧一列是**异常清单**（只列问题事件 + 它们的证据图），来不及看全量时就看它。

为什么不直接看 JSONL：一屏 2400 行 `press` 会把关键那 5 行淹掉；
本工具默认"关键事件"模式，勾掉勾选框才是全量。
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import time
from collections import Counter, OrderedDict

# 一定是"问题"的事件片段（用于高亮 + 异常清单 + 结局判定）
BAD_SUB = ("fail", "stuck", "timeout", "no_points", "crash", "abort", "miss",
           "lost", "unreachable", "budget", "flip", "unverified", "gap", "hit",
           "wrapped", "end", "stall", "oscillat", "error", "repeat", "crit")
# 值得单独看一眼的"里程碑"
GOOD_SUB = ("unlocked", "favorite_done", "settle", "rounds", "event_entered",
            "cycle_farm_begin", "vehicle_tab_ok", "car_ok", "grid_target",
            "grid_wrapped", "recover_done", "caps_lock", "car_fp_rebind")
# 纯噪音：全量模式下也折叠（数量大、信息重复）
NOISE = ("press", "click", "park_pointer", "hold", "release_all", "grid_scan",
         "ensure_tab_probe", "tab_probe_shot", "grid_advance", "grid_walk_step",
         "recover_probe", "nav_press")


def _classify(kind: str) -> str:
    k = kind.lower()
    if any(s in k for s in BAD_SUB):
        return "bad"
    if any(s in k for s in GOOD_SUB):
        return "good"
    return "plain"


def _load(path: str):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _fmt_detail(row: dict, maxlen: int = 220) -> str:
    parts = []
    for k, v in row.items():
        if k in ("t", "kind"):
            continue
        if isinstance(v, str) and len(v) > 90:
            v = v[:90] + "…"
        parts.append(f"{k}={v}")
    s = "  ".join(parts)
    return s if len(s) <= maxlen else s[:maxlen] + "…"


_DECISIVE = (
    ("crash", "崩溃（未预期异常）"),
    ("abort", "人工急停（F1）"),
    ("watchdog_abort", "挑战卡死看门狗中止"),
    ("set_22b_fail", "换回 22B 失败"),
    ("enter_event_failed", "自动进赛事失败"),
    ("enter_no_menu", "自动进赛事失败（不在菜单里）"),
    ("vehicle_tab_fail", "卡在「到不了车辆页」"),
    ("after_load_stuck", "上车后回不到车辆页"),
    ("grid_end", "列表翻到头（没有可处理的车）"),
    ("grid_budget", "翻列表超预算（保险丝）"),
    ("grid_wrapped", "列表翻完一圈（没有没♥的车）"),
    ("phase_spend_done", "B 正常收尾（列表里没有待处理的车）"),
    ("b_reset_after_no_points", "撞到「技能点不足」→ 去刷点"),
    ("unlock_stalled_no_points", "按了 Y 提示还在 → 按点数不足处理"),
    ("cars_cap_hit", "打到「处理车数」保险丝（异常信号）"),
)


def _outcome(rows) -> str:
    """结局 = **最后**那几条有意义的事件说了什么（按重要性倒着找第一个决定性的）。

    为什么要倒着找：一次运行可能前半段撞过「点数不足」、后半段被 F1 停掉 ——
    按"出现过什么"来判会写出前半段的事，用户想知道的是"它最后为什么停了"。
    """
    tail = [r.get("kind", "") for r in rows if r.get("kind") not in NOISE][-12:]
    for kind in reversed(tail):
        for sub, label in _DECISIVE:
            if kind == sub:
                return label
    return "（没有明确的收尾事件）"


def build(path: str, out_path: str | None = None) -> str:
    rows = _load(path)
    if not rows:
        raise SystemExit(f"日志是空的或读不出来：{path}")
    t0 = rows[0].get("t", 0.0)
    dur = rows[-1].get("t", t0) - t0
    name = os.path.basename(path)
    logdir = os.path.dirname(os.path.abspath(path))
    if out_path is None:
        out_path = os.path.join(logdir, "timeline_" + os.path.splitext(name)[0] + ".html")

    kinds = Counter(r.get("kind", "?") for r in rows)

    # 判据分数：每个判据出现过的最高分（从 probe/scores 字段里刮）
    det_scores: dict[str, float] = {}
    for r in rows:
        sc = r.get("scores")
        if isinstance(sc, dict):
            for k, v in sc.items():
                if isinstance(v, (int, float)) and (k not in det_scores or v > det_scores[k]):
                    det_scores[k] = float(v)

    bad_rows = [r for r in rows if _classify(r.get("kind", "")) == "bad"]

    # ---- 时间线 HTML ----
    items = []
    for r in rows:
        kind = r.get("kind", "?")
        cls = _classify(kind)
        dt = r.get("t", t0) - t0
        detail = _fmt_detail(r)
        shot = r.get("shot")
        img = ""
        if isinstance(shot, str) and shot:
            base = os.path.basename(shot.replace("\\", "/"))
            rel = os.path.relpath(os.path.join(logdir, base),
                                  os.path.dirname(os.path.abspath(out_path)))
            img = (f'<a class="shot" href="{html.escape(rel)}" target="_blank">'
                   f'<img loading="lazy" src="{html.escape(rel)}" alt="{html.escape(base)}">'
                   f'<span>{html.escape(base)}</span></a>')
        noise = "1" if kind in NOISE else "0"
        items.append(
            f'<li class="{cls}" data-noise="{noise}">'
            f'<span class="t">+{dt:7.1f}s</span>'
            f'<span class="k">{html.escape(kind)}</span>'
            f'<span class="d">{html.escape(detail)}</span>{img}</li>')

    bad_items = []
    for r in bad_rows:
        shot = r.get("shot")
        img = ""
        if isinstance(shot, str) and shot:
            base = os.path.basename(shot.replace("\\", "/"))
            rel = os.path.relpath(os.path.join(logdir, base),
                                  os.path.dirname(os.path.abspath(out_path)))
            img = (f'<a class="shot" href="{html.escape(rel)}" target="_blank">'
                   f'<img loading="lazy" src="{html.escape(rel)}" alt="{html.escape(base)}">'
                   f'<span>{html.escape(base)}</span></a>')
        bad_items.append(
            f'<li><span class="t">+{r.get("t", t0) - t0:7.1f}s</span>'
            f'<span class="k">{html.escape(r.get("kind", "?"))}</span>'
            f'<span class="d">{html.escape(_fmt_detail(r, 160))}</span>{img}</li>')

    score_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v:.3f}</td></tr>"
        for k, v in sorted(det_scores.items(), key=lambda kv: -kv[1]))

    kind_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
        for k, v in kinds.most_common())

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>复盘 · {html.escape(name)}</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ margin:0; background:#14161a; color:#e6e8ec;
        font:13px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
 header {{ padding:16px 20px; border-bottom:1px solid #262a31; position:sticky; top:0;
          background:#14161af2; backdrop-filter:blur(6px); z-index:2; }}
 h1 {{ margin:0 0 6px; font-size:16px; }}
 .meta {{ color:#9aa3af; font-size:12px; }}
 .meta b {{ color:#e6e8ec; font-weight:600; }}
 .wrap {{ display:grid; grid-template-columns:1fr 420px; gap:0; align-items:start; }}
 @media (max-width:1100px) {{ .wrap {{ grid-template-columns:1fr; }} }}
 .col {{ padding:14px 18px; }}
 h2 {{ font-size:13px; margin:14px 0 8px; color:#c8cdd6; text-transform:none; }}
 ul {{ list-style:none; margin:0; padding:0; }}
 li {{ display:grid; grid-template-columns:86px 168px 1fr; gap:8px;
      padding:3px 6px; border-bottom:1px solid #1c1f25; align-items:start; }}
 li.bad {{ background:#3a1c1f; }}
 li.bad .k {{ color:#ff8b8b; }}
 li.good {{ background:#152a1f; }}
 li.good .k {{ color:#7fe0a8; }}
 .t {{ color:#7c8794; font-variant-numeric:tabular-nums; }}
 .k {{ color:#9fb3d1; }}
 .d {{ color:#b9c0cb; word-break:break-all; }}
 img {{ max-width:100%; border-radius:4px; border:1px solid #2c313a; margin-top:4px; display:block; }}
 a.shot {{ grid-column:1/-1; display:block; text-decoration:none; color:#7c8794; font-size:11px; }}
 table {{ border-collapse:collapse; width:100%; font-size:12px; }}
 td {{ padding:2px 6px; border-bottom:1px solid #1c1f25; }}
 td:last-child {{ text-align:right; color:#cfd6e0; }}
 .bar {{ display:flex; gap:10px; align-items:center; margin-top:8px; font-size:12px; color:#9aa3af; }}
 .panel {{ background:#191c21; border:1px solid #262a31; border-radius:8px; padding:10px 12px; }}
</style></head><body>
<header>
  <h1>复盘时间线 · {html.escape(name)}</h1>
  <div class="meta">
    开始 <b>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rows[0].get('t', t0)))}</b>
    · 时长 <b>{dur/60:.1f} 分钟</b> · 事件 <b>{len(rows)}</b> 条
    · 结局 <b>{html.escape(_outcome(rows))}</b>
  </div>
  <div class="bar">
    <label><input type="checkbox" id="ck" checked> 只看关键事件（隐藏重复的按键/轮询）</label>
    <span>· 异常 {len(bad_rows)} 条 · 证据图看右栏</span>
  </div>
</header>
<div class="wrap">
  <div class="col">
    <h2>时间线</h2>
    <ul id="tl">{''.join(items)}</ul>
  </div>
  <div class="col">
    <h2>异常清单（先看这个）</h2>
    <ul class="panel">{''.join(bad_items) or '<li>没有异常事件 🎉</li>'}</ul>
    <h2>判据出现过的最高分</h2>
    <table class="panel">{score_rows or '<tr><td>（日志里没有 scores 字段）</td></tr>'}</table>
    <h2>事件种类统计</h2>
    <table class="panel">{kind_rows}</table>
  </div>
</div>
<script>
 var ck = document.getElementById('ck');
 function apply() {{
   var hide = ck.checked;
   document.querySelectorAll('#tl li').forEach(function (li) {{
     if (hide && li.dataset.noise === '1') li.style.display = 'none';
     else li.style.display = '';
   }});
 }}
 ck.addEventListener('change', apply); apply();
</script>
</body></html>
"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="把一次实跑的 JSONL 日志渲染成复盘时间线 HTML")
    ap.add_argument("log", nargs="?", help="run_*.jsonl 路径（默认取 logs/ 里最新的）")
    ap.add_argument("-o", "--out", help="输出 HTML 路径")
    args = ap.parse_args()

    path = args.log
    if not path:
        here = os.path.dirname(os.path.abspath(__file__))
        cands = glob.glob(os.path.join(here, "logs", "run_*.jsonl"))
        if not cands:
            raise SystemExit("logs/ 里没有 run_*.jsonl，请指定日志路径")
        path = max(cands, key=os.path.getmtime)
        print(f"[timeline] 自动选最新一次: {os.path.basename(path)}")
    out = build(path, args.out)
    print(f"[timeline] 已生成: {out}")
    print("          直接双击打开，或拖进浏览器。")


if __name__ == "__main__":
    main()

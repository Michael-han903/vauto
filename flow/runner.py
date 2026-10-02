# -*- coding: utf-8 -*-
"""
flow.runner —— A/B 状态机（业务层，唯一会真正按键的地方）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本代码仅用于算法学习（计算机视觉 / 输入仿真研究）。
若用于第三方软件，可能违反该软件用户许可协议（EULA）或服务条款，
并可能触发对方的风控 / 反作弊机制，存在账号被封禁等风险，请自行承担后果。
本层只做「看图 → 按键」的业务编排，不含内存读取、进程注入、DLL 注入、驱动加载、
网络通信、加解密、绕过检测等任何侵入式能力。请勿在其上添加此类功能。
======================================================================

两个阶段
--------
A（farm）  : 按住 W 打挑战 → 结算判据命中 → 松手 → Esc 重试，跑 rounds 轮
B（spend） : 点「车辆」→「更换车辆」→ 走一格 → Enter Enter 上车 →
             点「车辆熟练度」→ 有 [Y] 解锁全部 就 Y+Enter → 直到弹出「不够支付全部」→ 结束

关键安全设计
------------
* `dry_run=True` 时**绝不**调用任何按键/点击；只抓帧 + 判定 + 打印"我打算按什么"。
* 所有动作前过 `EmergencyStop.check()`（F1 立即抛 AbortedByUser）与 `FocusGuard`（非前台不动作）。
* 每个"我以为成功了"的判断后面都有一次**图像复核**（见各 `_verify_*`）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import cv2
import numpy as np

from vauto import (AbortedByUser, EmergencyStop, FocusGuard, Humanizer, InputSimulator,
                   TemplateMatcher, TimingProfile, WindowCapture, block_max_abs_diff,
                   client_to_screen, enable_dpi_awareness, find_window_by_title,
                   load_calibration, build_detectors, mean_abs_diff, scaled_frame,
                   wait_stable, wait_active)
from vauto.calib import calibration_table

from .config import RunConfig
from .nav import GridWalker

__all__ = ["Stack", "Runner", "build_stack", "build_offline_stack"]

# 需要的检测器（全部来自 templates/*.json 标定结果，运行期不手写阈值）
DETECTORS = (
    "hint_esc_retry", "panel_result",                       # A：结算
    "hint_unlock_all", "popup_no_resource", "popup_confirm",  # B：解锁 / 点数不足
    "popup_rate_event",                                     # 离开赛事后的「为挑战评分?」弹窗
    "page_title_mastery", "page_title_garage",              # 页面
    "tile_change_car", "tile_mastery",                      # 可点击磁贴
    "menu_select_title", "option_enter_car",                # 「选择操作」菜单 + 「上车」行
    "car_current_garage",                                   # 当前车辆是 22B（车库版式）
    "tile_collection",                                      # 在不在主菜单剧情页（与当前车无关）
    "panel_search_title",                                   # 进赛事：搜索面板已打开
    "tab_vehicle", "tab_creativity",                        # 用标签匹配来点标签（别写死坐标）
)
# 可选判据：没有也能跑（car_current_menu 在「刚上车的淡入帧」上分数不稳，标定可能把它判掉）
OPTIONAL_DETECTORS = ("car_current_menu",)

CAR_NAME_ROI = (68, 48, 1356, 136)      # 左上"当前车辆"名条的 ROI（换车验证用，与模板无关）


def _is_dry(runner) -> bool:
    return bool(getattr(runner, "cfg", None) and runner.cfg.dry_run)


# --------------------------------------------------------------------------- #
# 工具层装配
# --------------------------------------------------------------------------- #
@dataclass
class Stack:
    hwnd: int
    capture: object
    matcher: TemplateMatcher
    sim: object
    stop: object
    guard: object
    humanizer: Humanizer
    dets: Dict[str, object]
    calibration: dict = field(default_factory=dict)


def build_stack(cfg: RunConfig, title_key: Optional[str] = None) -> Stack:
    """在真机上装配（需要真实窗口 / 键鼠）。"""
    enable_dpi_awareness()
    key = title_key or cfg.title_key
    hwnd = find_window_by_title(key)
    if hwnd is None:
        raise RuntimeError(f"找不到标题包含 {key!r} 的窗口，先跑 py -3.14 run_vauto.py --list")
    capture = WindowCapture(hwnd, client_only=True)
    matcher = TemplateMatcher(grayscale=True, threshold=0.8)
    humanizer = Humanizer(profile=TimingProfile(), seed=None)
    sim = InputSimulator(humanizer=humanizer, move_before_click=True)
    stop = EmergencyStop(hotkey=cfg.hotkey, on_trigger=sim.release_all, verbose=True)
    sim.emergency = stop
    guard = FocusGuard(hwnd, stop_event=stop.event, verbose=True)
    cal = load_calibration()
    dets = build_detectors(tuple(DETECTORS) + tuple(OPTIONAL_DETECTORS),
                           cal=cal, matcher=matcher, optional=OPTIONAL_DETECTORS)
    return Stack(hwnd=hwnd, capture=capture, matcher=matcher, sim=sim, stop=stop,
                 guard=guard, humanizer=humanizer, dets=dets, calibration=cal)


def build_offline_stack(capture=None, sim=None, hwnd: int = 0) -> Stack:
    """
    离线/回放装配：注入假的 capture 与 sim，不碰真实屏幕与键鼠。
    `run_vauto.py --selftest` 与 replay_offline.py 用它。
    """
    matcher = TemplateMatcher(grayscale=True, threshold=0.8)
    cal = load_calibration()
    dets = build_detectors(tuple(DETECTORS) + tuple(OPTIONAL_DETECTORS),
                           cal=cal, matcher=matcher, optional=OPTIONAL_DETECTORS)

    class _NoStop:
        event = None
        triggered = False
        def check(self):
            return None
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def start(self):
            return self
        def stop(self):
            return None

    class _AlwaysFg:
        paused_total = 0.0
        def is_active(self):
            return True
        def wait(self, timeout=None, on_pause=None):
            return True
        def require(self):
            return None

    stop = _NoStop()
    return Stack(hwnd=hwnd, capture=capture, matcher=matcher,
                 sim=sim if sim is not None else _NoSim(),
                 stop=stop, guard=_AlwaysFg(),
                 humanizer=Humanizer(seed=1), dets=dets, calibration=cal)


class _NoSim:
    """离线默认桩：什么都不做（dry_run 之外的路径不该碰它）。"""
    held: list = []
    def key_down(self, k):
        self.held.append(("down", k))
    def key_up(self, k):
        self.held.append(("up", k))
    def tap_key(self, k, *a, **kw):
        self.held.append(("tap", k))
        return 0.0
    def click(self, pos, **kw):
        self.held.append(("click", tuple(pos)))
        return tuple(pos)
    def move_bezier(self, pos, **kw):
        self.held.append(("move", tuple(pos)))
        return 0.0
    def release_all(self, quiet=True):
        self.held.append(("release_all",))
        return 0


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
class _Logger:
    def __init__(self, log_dir: str, tag: str = "") -> None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"run_{time.strftime('%Y%m%d_%H%M%S')}{('_' + tag) if tag else ''}.jsonl"
        self.rows: list = []

    def event(self, kind: str, **kw) -> None:
        row = {"t": round(time.time(), 3), "kind": kind}
        row.update(kw)
        self.rows.append(row)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def summary(self) -> str:
        kinds: Dict[str, int] = {}
        for r in self.rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        return "  ".join(f"{k}={v}" for k, v in sorted(kinds.items()))


# --------------------------------------------------------------------------- #
# 主状态机
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, stack: Stack, cfg: RunConfig) -> None:
        self.s = stack
        self.cfg = cfg
        self.log = _Logger(cfg.log_dir, "dry" if cfg.dry_run else "live")
        self.stats: Dict[str, int] = {"rounds": 0, "cars_done": 0, "cars_skipped": 0,
                                      "unlock_presses": 0, "no_points": 0, "errors": 0}
        self.ledger = self._load_ledger()
        self._nav_baseline: Optional[np.ndarray] = None
        self._t0 = time.monotonic()

    # ---------------- 台账 ---------------- #
    def _load_ledger(self) -> dict:
        p = Path(self.cfg.ledger_path)
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"cars_done_total": 0, "rounds_total": 0, "sessions": 0}

    def _save_ledger(self) -> None:
        self.ledger["cars_done_total"] += self.stats["cars_done"]
        self.ledger["rounds_total"] += self.stats["rounds"]
        self.ledger["sessions"] += 1
        self.ledger["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        p = Path(self.cfg.ledger_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 基础动作 ---------------- #
    def frame(self, guard: bool = True) -> np.ndarray:
        """抓一帧。急停会抛 AbortedByUser；非前台会阻塞等待（可被急停打断）。"""
        self.s.stop.check()
        if guard and self.s.guard is not None:
            if not self.s.guard.wait():
                raise AbortedByUser("等待前台时被中断")
        return self.s.capture.grab()

    def observe(self, name: str, frame: np.ndarray):
        return self.s.dets[name].observe(frame)

    _SLEEP: Callable[[float], None] = staticmethod(
        lambda sec: time.sleep(sec) if sec > 0 else None)

    def sleep(self, sec: float) -> None:
        """可被急停打断的等待（离线回放里是空操作）。"""
        from vauto import interruptible_sleep
        event = getattr(self.s.stop, "event", None)
        if event is None and sec > 0:
            self._SLEEP(sec)
        else:
            interruptible_sleep(sec, event)

    # 动作：dry_run 下只记账，不真的按
    def press(self, key: str, note: str = "") -> None:
        self.s.stop.check()
        if not self.cfg.dry_run and not self.s.guard.is_active():
            # 目标窗口不在前台 → 阻塞等待（急停可打断），绝不盲按
            self.s.guard.wait()
            self.s.stop.check()
        self.log.event("press", key=key, note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应按下 {key!r}  ({note})")
            return
        self.s.sim.tap_key(key)
        self.sleep(0.15)

    def hold(self, key: str, note: str = "") -> None:
        self.s.stop.check()
        self.log.event("hold", key=key, note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应按住 {key!r} 不放  ({note})")
            return
        self.s.sim.key_down(key)

    def release_all(self, note: str = "") -> None:
        self.log.event("release_all", note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应松开所有键  ({note})")
            return
        self.s.sim.release_all(quiet=True)

    def click_client(self, xy, note: str = "") -> None:
        """点客户区坐标（会自动换算成屏幕坐标）。"""
        self.s.stop.check()
        sx, sy = client_to_screen(self.s.hwnd, int(xy[0]), int(xy[1])) if self.s.hwnd else xy
        self.log.event("click", at=[sx, sy], note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应点击屏幕 ({sx}, {sy})  ({note})")
            return
        self.s.sim.click((sx, sy))

    def click_match(self, name: str, frame: Optional[np.ndarray] = None,
                    note: str = "") -> bool:
        """匹配某个元素并点它的中心（最稳的点击方式：坐标不写死）。"""
        frame = self.frame() if frame is None else frame
        hit = self.observe(name, frame)
        if hit is None:
            hit = self._wait_for(name, 1.5)
        if hit is None:
            self.log.event("click_miss", det=name)
            print(f"  [!] 没找到 {name}，跳过点击")
            return False
        self.click_client(hit.center, note or f"点 {name}")
        return True

    # ---------------- 等待类 ---------------- #
    def _wait_for(self, name: str, timeout: float):
        """轮询直到该检测器**确认**命中（连续 confirm_frames 帧）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            frame = self.frame()
            hit = self.observe(name, frame)
            if hit is not None:
                return hit
            if time.monotonic() >= deadline:
                return None
            self.sleep(self.cfg.poll)

    def _wait_gone(self, name: str, timeout: float) -> bool:
        """轮询直到该检测器不再处于"已确认"状态。"""
        deadline = time.monotonic() + max(0.0, timeout)
        det = self.s.dets[name]
        while True:
            self.observe(name, self.frame())
            if not det.confirmed:
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(self.cfg.poll)

    def _name_roi(self, frame: np.ndarray) -> np.ndarray:
        x, y, w, h = CAR_NAME_ROI
        return frame[y:y + h, x:x + w]

    def _list_roi(self, frame: np.ndarray) -> np.ndarray:
        """列表/车格区域（不含左上名条与底部提示条）—— 换车验证看这里。"""
        x, y, w, h = self.cfg.nav_watch_roi
        return frame[y:y + h, x:x + w]

    def _save_evidence(self, frame: np.ndarray, tag: str) -> str:
        """存下"出问题那一刻"的画面 —— 无人值守跑挂后能回答"当时停在哪儿"（存到 logs/）。"""
        try:
            d = Path(self.cfg.log_dir)
            d.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = d / f"{tag}_{ts}.png"
            cv2.imencode(".png", frame)[1].tofile(str(path))
            return str(path)
        except Exception as exc:
            return f"(存图失败: {exc})"

    # ---------------- A 阶段 ---------------- #
    def phase_farm(self, rounds: int) -> dict:
        print(f"\n===== A 阶段：{rounds} 轮挑战（按住 W → 等结算 → Esc 重试） =====")
        settled = 0
        while settled < rounds:
            if self.cfg.max_runtime_min and \
                    (time.monotonic() - self._t0) / 60 > self.cfg.max_runtime_min:
                print("  [!] 达到总时长上限，停止 A 阶段")
                break
            last = (settled == rounds - 1)
            outcome = self.farm_one_round(retry=not last)
            if outcome == "aborted":
                break
            if outcome == "settled":
                settled += 1
                self.stats["rounds"] += 1
                print(f"  [A] 第 {settled}/{rounds} 轮完成")
            elif outcome == "timeout":
                print("  [!] 本轮超时未见结算判据，继续等待下一轮")
                settled += 1
            else:
                self.stats["errors"] += 1
                print(f"  [!] 本轮异常：{outcome}")
                break
        return {"rounds": settled, "phase": "farm"}

    def farm_one_round(self, retry: bool = True) -> str:
        """一轮：等加载稳定 → 按住 W → 等结算 → 松手 → （Esc 重试 | Enter 继续）。"""
        # 1) 等加载（画面连续稳定；不做固定 sleep）
        if not self.cfg.replay:
            wait_stable(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                        timeout=self.cfg.round_settle_before, settle=0.8, scale=0.5,
                        poll=min(0.3, self.cfg.poll))
        # 1.5) 等"画面真的动起来" = 比赛开始。
        # 加载过场（HORIZON FESTIVAL 那种）是**静止的**，wait_stable 会把它误判成"加载完成"
        # （2026-10-02 证据图 logs/event_entered_*.png 就是这么拍到的）。而挑战开始后 HUD
        # 一直在动 → 用 wait_active 作为真正的起跑闸门。
        if not self.cfg.replay:
            active = wait_active(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                                 timeout=self.cfg.round_active_wait,
                                 threshold=self.cfg.nav_change_threshold,
                                 blocks=self.cfg.nav_change_blocks)
            self.log.event("round_active", ok=active)
            if not active:
                print("  [!] 等了很久画面还是静止的 —— 可能卡在加载或某个等待输入的界面")
        # 2) 按住 W
        # 先松一次再按：保证这条 keydown 发生在加载**之后**（加载过程可能吞掉按键事件）
        self.release_all("比赛开始，准备按住 W")
        self.hold(self.cfg.hold_key, "挑战进行中")
        # 3) 轮询结算判据（两个判据每帧都要喂，否则其中一个的连续帧计数会断）
        polls, settle_hit = 0, None
        last_frame, last_change, idle_warns = None, time.monotonic(), 0
        while polls < self.cfg.max_polls_per_round:
            polls += 1
            try:
                frame = self.frame()
            except AbortedByUser:
                self.release_all("急停")
                return "aborted"
            ha = self.observe("hint_esc_retry", frame)
            hb = self.observe("panel_result", frame)
            if ha or hb:
                settle_hit = ha or hb
                break
            # 卡死看门狗：画面 180s 没变告警一次；累计 3 次（约 9 分钟）→ 存证据 + 中止本轮
            # 注意：这与「加载期没命中任何判据」是两码事 —— 加载时画面在变，不会触发这里。
            if self.cfg.watchdog_idle > 0 and not self.cfg.replay:
                if last_frame is None or mean_abs_diff(last_frame, frame, gray=True) > 2.0:
                    last_frame, last_change, idle_warns = frame, time.monotonic(), 0
                elif time.monotonic() - last_change > self.cfg.watchdog_idle:
                    idle_warns += 1
                    shot = self._save_evidence(frame, f"watchdog{idle_warns}")
                    self.log.event("watchdog_idle", count=idle_warns, shot=shot)
                    print(f"  [!] 画面已 {self.cfg.watchdog_idle:.0f}s 无变化"
                          f"（第 {idle_warns}/3 次）证据: {shot}")
                    if idle_warns >= 3:
                        self.log.event("watchdog_abort", shot=shot)
                        self.release_all("卡死中止")      # 必须先松手再返回：否则 W 会一直按着
                        return "watchdog"
                    last_change = time.monotonic()
            self.sleep(self.cfg.poll)
        # 4) 松手（一定要先松，避免 W 和 Esc 同时按着）
        self.release_all("结算出现" if settle_hit else "轮询结束")
        if settle_hit is None:
            return "timeout"
        self.log.event("settle", score=round(settle_hit.score, 3), polls=polls)
        print(f"  [A] 结算判据命中（{settle_hit.score:.3f}，第 {polls} 次轮询）")
        # 5) 重试 / 离开
        key = self.cfg.retry_key if retry else self.cfg.leave_event_key
        note = "重试（下一轮）" if retry else "继续（离开赛事）"
        self.press(key, note)
        if retry:
            ok = self._wait_gone("hint_esc_retry", self.cfg.ack_timeout)
            self.log.event("retry_ack", ok=ok)
            print(f"  [A] 已离开结算界面: {ok}")
        else:
            # 离开赛事后会弹「为挑战评分?」——**按回车**（用户明确要求：不管高亮在哪一行都按回车）。
            # 给它 25 秒的耐心，会一路观察加载/过场。
            self.sleep(1.5)
            self.clear_blocking_dialog("离开赛事后")
        return "settled"

    # ---------------- B 阶段 ---------------- #
    def phase_spend(self, max_cars: int) -> dict:
        print(f"\n===== B 阶段：最多 {max_cars} 台车（换车 → 精通页 → Y+Enter） =====")
        self.clear_blocking_dialog("B 阶段开始前", patience=12.0)   # 清掉上一阶段可能留下的弹窗
        self._ensure_vehicle_tab()
        for i in range(max_cars):
            self.s.stop.check()
            if not self._ensure_vehicle_tab():
                print("  [!] 到不了「车辆」页，结束 B 阶段")
                break
            if not self.change_car():
                # 记为错误（原来这里不计错误，跑挂了汇总里仍是 errors=0，会让人以为一切正常）
                self.stats["errors"] += 1
                self.log.event("change_car_failed", car_index=i + 1)
                print("  [!] 换车失败，结束 B 阶段（详情看日志/证据图）")
                break
            print(f"  [B] 第 {i + 1} 台车：已上车")
            state = self.unlock_current_car()
            if state == "unlocked":
                self.stats["cars_done"] += 1
                self.stats["unlock_presses"] += 1
                print("  [B] 本页已解锁 ✅")
            elif state == "already":
                self.stats["cars_skipped"] += 1
                print("  [B] 这台车早就解锁过，跳过")
            elif state == "no_points":
                self.stats["no_points"] += 1
                print("  [B] 技能点不足 → 该回 A 刷点了")
                self._leave_mastery()
                break
            else:
                self.stats["errors"] += 1
                print(f"  [!] 解锁异常：{state}")
            self._leave_mastery()
        return {"cars_done": self.stats["cars_done"], "phase": "spend"}

    def _in_menu_now(self, frame: Optional[np.ndarray] = None) -> Tuple[bool, dict]:
        """当前是否在「菜单系统」里（主菜单任意标签 / 我的车辆列表 / 精通页 / 弹窗）。

        实测：上车后会回到**主世界（自由驾驶）**，此时左上角没有菜单那块车名面板
        → 必须先按 Esc 才出现主菜单。所以"在不在菜单里"是决定按什么键的关键。

        判定只用**与当前车辆无关**的判据：
        - `tile_collection`（剧情页「收集簿」磁贴白字 —— 每次按 Esc 菜单都开在剧情页）
        - `tile_mastery`（车辆页磁贴）/ `page_title_garage`（我的车辆）/ `page_title_mastery`（精通页）
        **不要用 `car_current_menu` / `car_current_garage`**：它们是从 22B 的截图裁的，
        连车图+车名一起记住了，换成别的车只有 ~0.5，会把"人就在主菜单上"误判成"不在菜单里"
        （2026-10-02 --cars 3 跑挂的根因）。
        """
        frame = self.frame() if frame is None else frame
        probe_names = ("tile_collection", "car_current_menu",
                       "tile_mastery", "tile_change_car",
                       "page_title_garage", "page_title_mastery", "hint_esc_retry",
                       "panel_result", "popup_confirm", "popup_no_resource",
                       "hint_esc_back", "hint_unlock_all", "car_current_garage",
                       "menu_select_title")
        sc = {}
        for nm in probe_names:
            d = self.s.dets.get(nm)
            if d is None:
                continue
            score, _ = d.probe(frame)
            sc[nm] = round(score, 3)
        in_menu = False
        for nm in ("tile_collection", "tile_mastery",
                   "page_title_garage", "page_title_mastery"):
            d = self.s.dets.get(nm)
            if d is not None and sc.get(nm, 0.0) >= d.threshold:
                in_menu = True
        return in_menu, sc

    def _ensure_vehicle_tab(self, after_load: bool = False) -> bool:
        """确保停在主菜单「车辆」标签页（用 tile_mastery 的出现来验证）。

        顺序很重要（2026-10-02 用户实测确认 + 第二次真跑）：
        **点「上车」之后游戏一定回到主世界（自由驾驶）**，必须先按 `Esc` 才会出现主菜单，
        然后才能点「车辆」标签。所以 after_load 时 Esc 优先，而不是先点标签。
        """
        det_tab = self.s.dets["tile_mastery"]
        if after_load and not self.cfg.replay:
            self.press("esc", "上车后回到主世界 → Esc 打开主菜单")
            self.sleep(self.cfg.esc_dwell)
        seen_states = set()
        for attempt in range(5):
            frame = self.frame()
            tab_score, _ = det_tab.probe(frame)
            if tab_score >= det_tab.threshold:
                self.observe("tile_mastery", frame)              # 让去抖状态跟上
                self.log.event("vehicle_tab_ok", attempt=attempt, after_load=after_load)
                return True
            in_menu, sc = self._in_menu_now(frame)
            self.log.event("ensure_tab_probe", attempt=attempt, after_load=after_load,
                           in_menu=in_menu, scores=sc)
            # 证据：把"没见过的画面"存下来（最多 3 张）—— 失败时能直接看到当时是什么界面
            sig = tuple(sorted((k, v) for k, v in sc.items() if v >= 0.3))
            if sig not in seen_states and len(seen_states) < 3:
                seen_states.add(sig)
                shot = self._save_evidence(frame, f"tab_probe_a{attempt}")
                self.log.event("tab_probe_shot", attempt=attempt, shot=shot)
            if in_menu:
                # 在菜单里但不在车辆页（别的标签页 / 我的车辆列表 / 精通页）
                if sc.get("page_title_garage", 0.0) >= self.s.dets["page_title_garage"].threshold:
                    self.press("esc", "从「我的车辆」列表退回")
                    self.sleep(self.cfg.esc_dwell)
                else:
                    print(f"  [B] 在菜单里但不在车辆页 → 点「车辆」标签  {sc}")
                    if not self.click_match("tab_vehicle", frame=frame, note="车辆 tab"):
                        # 兜底：标签匹配不上（例如它正被选中 → 黑底白字）时按坐标点
                        self.click_client(self.cfg.tab_vehicle_click, "车辆 tab（坐标兜底）")
                    if self._wait_for("tile_mastery", self.cfg.page_timeout) is not None:
                        self.log.event("vehicle_tab_ok", attempt=attempt,
                                       after_load=after_load, needed_action="click")
                        return True
            else:
                # 主世界（自由驾驶）/ 加载中 → Esc 才会出现主菜单（这是 after_load 的正常路径）
                print(f"  [B] 不在菜单里（主世界/加载中）→ 按 Esc 打开主菜单  {sc}")
                self.press("esc", "自由驾驶 → 主菜单")
                self.sleep(self.cfg.esc_dwell)
        self.log.event("vehicle_tab_fail", after_load=after_load)
        return False

    def change_car(self) -> bool:
        """换到列表里的下一辆车：点「更换车辆」→ 走一格 → Enter → Enter。"""
        if not self.click_match("tile_change_car", note="点「更换车辆」"):
            return False
        # 等列表出现（判据见下）
        # 等列表出现。**必须用页面标题判据**：左上角「当前车辆」名条在「车辆」标签页上也有，
        # 用它等于没判 —— 第一次真跑（2026-10-02 20:43）就是这么漏过去的。
        if self._wait_for("page_title_garage", self.cfg.page_timeout) is None:
            shot = self._save_evidence(self.frame(), "nav_list_not_opened")
            print(f"  [!] 「更换车辆」列表没出现（证据: {shot}）")
            self.log.event("nav_list_missing", shot=shot)
            return False
        # 走一格：每步都用「列表区域是否发生**局部**变化」验证。
        # 不能用左上角名条：实测（2026-10-02 探针 620 帧）光标在列表里移动时，名条一直
        # 保持 1.000 完全不变 —— 它显示的是"当前驾驶的车"，不跟随光标。
        if not self.cfg.replay:
            wait_stable(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                        timeout=self.cfg.car_change_timeout, settle=0.6, scale=0.5,
                        poll=min(0.3, self.cfg.poll))
        self._nav_base_frame = self.frame()
        self._nav_last_frame = self._nav_base_frame
        self._nav_baseline = self._list_roi(self._nav_base_frame)

        def _press_raw(key: str) -> None:
            self.s.stop.check()
            self.log.event("nav_press", key=key, dry=self.cfg.dry_run)
            if not self.cfg.dry_run:
                self.s.sim.tap_key(key)
                self.sleep(0.35)      # 给光标移动/高亮重绘留时间，避免抓到按键前的帧

        def _changed() -> bool:
            before = self._nav_baseline
            f = self.frame()
            self._nav_last_frame = f
            now = self._list_roi(f)
            self._nav_baseline = now
            if before is None:
                return False
            score = block_max_abs_diff(before, now, blocks=self.cfg.nav_change_blocks)
            changed = score >= self.cfg.nav_change_threshold
            self.log.event("nav_change", score=round(score, 2), changed=changed,
                           threshold=self.cfg.nav_change_threshold)
            return changed

        walker = GridWalker(press=_press_raw, car_changed=_changed,
                            max_fail=self.cfg.nav_max_fail, budget=self.cfg.nav_budget)
        if not walker.step():
            # 失败必须留证据：光标移不动 = 要么没进列表、要么判据不对 —— 有图才能定论
            before_shot = self._save_evidence(self._nav_base_frame, "nav_stuck_before")
            after_shot = self._save_evidence(self._nav_last_frame, "nav_stuck_after")
            print(f"  [!] 走不动了（光标没动）证据: {before_shot} | {after_shot}")
            self.log.event("nav_stuck", before=before_shot, after=after_shot,
                           steps=getattr(walker, "steps", None))
            return False
        self.log.event("nav_step", steps=walker.steps)
        # 选择操作 → 上车
        self.press(self.cfg.confirm_key, "打开「选择操作」")
        if not self.cfg.replay:
            menu = self._wait_for("menu_select_title", 4.0)
            row = self._wait_for("option_enter_car", 1.0)
            if menu is None or row is None:
                # 这两个阈值是手工推定的，首次在线就靠这条日志定论
                self.log.event("car_menu_threshold_check", menu_title=bool(menu),
                               enter_row=bool(row),
                               note="「选择操作」菜单判据不完整，仍继续（Enter 幂等）")
        self.press(self.cfg.confirm_key, "选「上车」")
        # 等加载完成（车会重新载入，画面会明显变化再稳定）
        if not self.cfg.replay:
            time.sleep(max(1.0, self.cfg.poll))
            wait_stable(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                        timeout=self.cfg.car_change_timeout, settle=1.0, scale=0.5, poll=0.3)
            # 【2026-10-02 用户实测确认】点「上车」后游戏**一定回到主世界（自由驾驶）**，
            # 要按 Esc 才出现主菜单 → 所以这里是 after_load=True（Esc 优先），
            # 之后还要点「车辆」标签才回到车辆页。判页分数写进日志（ensure_tab_probe）。
            if not self._ensure_vehicle_tab(after_load=True):
                shot = self._save_evidence(self.frame(), "after_load_no_vehicle_tab")
                print(f"  [!] 上车后回不到「车辆」页（证据: {shot}）")
                self.log.event("after_load_stuck", shot=shot)
                return False
        return True

    def unlock_current_car(self) -> str:
        """进精通页并解锁；返回 unlocked / already / no_points / no_page。"""
        # 进精通页的前提是「车辆」页可见（tile_mastery 只在这一页）→ 先主动确保
        if not self.cfg.replay and not self._ensure_vehicle_tab():
            shot = self._save_evidence(self.frame(), "no_vehicle_tab_before_mastery")
            print(f"  [!] 到不了「车辆」页（证据: {shot}）")
            self.log.event("no_vehicle_tab", shot=shot)
            return "no_page"
        if not self.click_match("tile_mastery", note="点「车辆熟练度」"):
            return "no_page"
        if self._wait_for("page_title_mastery", self.cfg.page_timeout) is None:
            return "no_page"
        det = self.s.dets["hint_unlock_all"]
        if self._wait_for("hint_unlock_all", 2.0) is None:
            self.log.event("mastery_already")
            return "already"                    # 没有 [Y] 解锁全部 → 本页早就做完了
        self.press(self.cfg.unlock_key, "解锁全部")
        self.press(self.cfg.confirm_key, "确认「解锁额外加成」")
        # 等结果：hint_unlock_all 消失 = 成功；popup_no_resource 出现 = 点数不足
        deadline = time.monotonic() + self.cfg.page_timeout
        while True:
            frame = self.frame()
            if self.observe("popup_no_resource", frame) is not None:
                self.press(self.cfg.confirm_key, "关掉「不够支付全部」")
                self.log.event("no_points")
                return "no_points"
            self.observe("hint_unlock_all", frame)
            if not det.confirmed:
                return "unlocked"
            if time.monotonic() >= deadline:
                return "timeout_wait_unlock"
            self.sleep(self.cfg.poll)

    def _leave_mastery(self) -> bool:
        self.press("esc", "离开精通页")
        if self.cfg.replay:
            return True
        return self._wait_gone("page_title_mastery", self.cfg.page_timeout)

    # ---------------- 车辆校验 ---------------- #
    def check_car_22b(self) -> bool:
        """
        A 之前校验当前车辆是 1998 斯巴鲁 Impreza 22B-STI（主菜单或车库两种版式各一个判据）。
        命中任意一个即认为通过。

        【重要】这两个判据都是**认车的**：模板是从 22B 的截图裁的，连车图+车名一起记住了。
        所以它们**不能**用来判断"我在不在菜单里"（换了车分数就会掉到 0.5 左右）。
        判断在不在菜单要用 tile_collection / tile_mastery / page_title_* 这些页面判据。
        """
        for name in ("car_current_menu", "car_current_garage"):
            hit = self._wait_for(name, 1.5)
            if hit is not None:
                print(f"  [校验] 当前车辆是 22B（{name}，{hit.score:.3f}）")
                self.log.event("car_ok", det=name, score=round(hit.score, 3))
                return True
        # 没命中：区分"人不在这两页"还是"在菜单里但开的不是 22B"（后者才是真问题）
        in_menu, sc = self._in_menu_now()
        if in_menu:
            print("  [!] 你在菜单里，但**当前车辆不是 22B** —— 面板判据分数："
                  f"menu={sc.get('car_current_menu')} (阈值 "
                  f"{self.s.dets['car_current_menu'].threshold if 'car_current_menu' in self.s.dets else '-'})"
                  f" / garage={sc.get('car_current_garage')}")
            print("      → 跑 A 之前必须手动把车换回 1998 斯巴鲁 Impreza 22B-STI；"
                  "只想刷技能点可以直接 --phase spend")
        else:
            print("  [!] 看不到菜单（当前不在主菜单/车库页）→ 无法校验当前车。"
                  "如果你已经在赛事里，加 --no-car-check 跳过这条校验")
        self.log.event("car_check_failed", in_menu=in_menu, scores=sc)
        return False

    def clear_blocking_dialog(self, note: str = "", patience: float = 25.0) -> bool:
        """把挡路的「等待输入」界面清掉（默认按回车，键由 cfg.dialog_cancel_key 决定）。

        【用户要求】不管高亮在哪一行都按回车（2026-10-02）。
        记录的观察：同一个「为挑战评分?」弹窗，两次截图的高亮行不同（一次在「取消」、
        一次在「**点赞**」）。按回车 = 确认**当前高亮的那一项**，所以高亮在「点赞」时
        回车会点赞。用户明确接受这个行为，因此默认用回车；要改成 Esc（取消，与高亮无关）
        只需把 cfg.dialog_cancel_key 改成 "esc"。

        做法：在 `patience` 秒内反复观察 ——
        * 回到菜单了 → 完成；
        * 评分弹窗判据命中 → 按键；
        * 画面静止（1.2 秒内分块最大差 < 阈值）= 有东西在等输入 → 按键；
        * 画面在动（加载/过场/自由驾驶）→ 继续观察，不动手。
        每次动手都存证据图 logs/clear_dialog_*.png。
        """
        if self.cfg.replay:
            return False
        deadline = time.monotonic() + patience
        acted = 0
        while time.monotonic() < deadline:
            in_menu, sc = self._in_menu_now()
            if in_menu:
                self.log.event("dialog_cleared", note=note, in_menu=True, acted=acted)
                return True
            f1 = self.frame()
            self.sleep(1.2)
            f2 = self.frame()
            frozen = (block_max_abs_diff(f1, f2, blocks=self.cfg.nav_change_blocks)
                      < self.cfg.nav_change_threshold)
            hit = None
            if "popup_rate_event" in self.s.dets:
                hit = self.observe("popup_rate_event", f2)
            if hit is None and not frozen:
                self.sleep(0.5)
                continue                     # 画面在动（加载/过场/自由驾驶）→ 不动手
            acted += 1
            shot = self._save_evidence(f2, f"clear_dialog{acted}")
            print(f"  [兜底] {note or '挡路界面'}："
                  f"{'评分弹窗判据命中' if hit else '画面静止'}"
                  f" → 按 {self.cfg.dialog_cancel_key} 关掉  证据: {shot}")
            self.log.event("clear_dialog", note=note, rate_judge=bool(hit),
                           index=acted, shot=shot, key=self.cfg.dialog_cancel_key)
            self.press(self.cfg.dialog_cancel_key, "关掉等待输入的界面（取消）")
            self.sleep(1.5)
            if acted >= 3:
                break
        self.log.event("dialog_cleared", note=note, in_menu=False, acted=acted)
        return False

    # ---------------- 进赛事（自动开局）---------------- #
    def _enter_fail(self, step: str) -> bool:
        """进赛事某一步没生效 → 存证据图 + 停下（**绝不继续盲按**）。

        2026-10-02 的教训：点错标签那一次，程序在"这一步没生效"之后还继续按了 18 下，
        把游戏带到搜索面板那种半途状态。一步失败就应该停。
        """
        shot = self._save_evidence(self.frame(), f"enter_fail_{step}")
        print(f"  [!] 进赛事卡在「{step}」这一步 → 停下（证据: {shot}）")
        self.log.event("enter_failed", step=step, shot=shot)
        return False

    def _click_creativity_tab(self) -> None:
        """点主菜单的「创意中心」标签：优先用标签模板匹配到的中心，匹配不上再退回坐标。"""
        if not self.click_match("tab_creativity", note="创意中心 tab"):
            self.log.event("tab_label_miss", tab="creativity")
            print("  [!] 认不出「创意中心」标签（也许已经在创意中心页）→ 按坐标兜底点一次")
            self.click_client(self.cfg.tab_creativity_click, "创意中心 tab（坐标兜底）")

    def _do_step(self, action, note: str, timeout: float = 8.0) -> bool:
        """执行一步，并确认「画面按预期变了」。返回 False = 这一步没生效。

        为什么不每步都用模板：进赛事是一串固定按键，每一步的预期效果就是一次翻页/弹层；
        而"画面有没有变"用分块最大差就能可靠判出来（跟换车验证同款指标，实测灵敏度足够）。
        另有一个坑：黑条+白字那种模板在 0.5 粗搜尺度下会退化成"黑块+白影"，跟别的黑条撞车
        （实测负样本 0.99 被判不可用）—— 所以这里能不靠模板就不靠。
        """
        if self.cfg.replay:
            action()
            return True
        before = self.frame()
        action()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sleep(0.25)
            cur = self.frame()
            score = block_max_abs_diff(before, cur, blocks=self.cfg.nav_change_blocks)
            if score >= self.cfg.nav_change_threshold:
                self.log.event("step_ok", note=note, change=round(score, 1))
                return True
        self.log.event("step_no_change", note=note)
        print(f"  [!] 「{note}」这一步画面没有变化（可能没生效）")
        return False

    def _type_text(self, text: str, note: str = "") -> None:
        """逐字符输入（pynput 走 KeyCode.from_char）。用于共享代码这种纯数字输入框。"""
        self.s.stop.check()
        self.log.event("type_text", text=text, note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应输入 {text!r}  ({note})")
            return
        for ch in text:
            self.s.sim.tap_key(ch)
            self.sleep(0.06)

    def enter_event(self) -> bool:
        """从主菜单自动进赛事（用户口述序列，见 docs/业务实测要点.md 第 6 节）。

        闸门设计：
        * 翻页类步骤用 `_do_step`（"画面变了"）—— 不依赖每个页面都裁出可用模板；
        * 搜索面板那一步用真判据 `panel_search_title` 复核（标定过：正 1.000 / 负 0.392）；
        * 对不上就存证据图 + 停下，**绝不盲按**（盲按会把游戏带到未知状态）。
        """
        cfg = self.cfg
        print("\n===== 自动进赛事：创意中心 → EventLab → 参加挑战 → 搜索共享代码 =====")
        # 0) 要先在菜单系统里
        in_menu, sc = self._in_menu_now()
        if not in_menu:
            print("  [进赛事] 当前不在菜单里 → 先按 Esc 试着回主菜单")
            self.press("esc", "回主菜单")
            self.sleep(cfg.esc_dwell)
            in_menu, sc = self._in_menu_now()
            if not in_menu:
                shot = self._save_evidence(self.frame(), "enter_no_menu")
                print(f"  [!] 还是不在菜单里，放弃自动进赛事（证据: {shot}）")
                self.log.event("enter_no_menu", scores=sc, shot=shot)
                return False
        # 1) 点「创意中心」标签（优先用标签模板匹配到的中心）
        if not self._do_step(self._click_creativity_tab, "点「创意中心」标签"):
            return self._enter_fail("click_creativity")
        # 2) Enter 进 EventLab → ↓ 到「参加挑战」→ Enter
        if not self._do_step(lambda: self.press(cfg.confirm_key, "进入 EventLab"),
                             "进入 EventLab"):
            return self._enter_fail("enter_eventlab")
        self.press("down", "移到「参加挑战」")          # 光标一格，不设闸门
        self.sleep(0.3)
        if not self._do_step(lambda: self.press(cfg.confirm_key, "进入「参加挑战」"),
                             "进入「参加挑战」"):
            return self._enter_fail("enter_join_challenge")
        # 3) Backspace 打开搜索面板（这一步有真判据复核）
        if not self._do_step(lambda: self.press("backspace", "打开搜索面板"), "打开搜索面板"):
            return self._enter_fail("backspace_search")
        if not self.cfg.replay and self._wait_for("panel_search_title", 6.0) is None:
            return self._enter_fail("search_panel_judge")
        # 4) ↑ 到「共享代码」→ Enter → 输代码（已填就别重输）→ Enter → ↓ → Enter
        self.press("up", "移到「共享代码」行")
        self.press(cfg.confirm_key, "进入代码输入")
        print("  [进赛事] 输入共享代码（先退格清空，防拼成两遍）")
        for _ in range(int(cfg.code_clear_backspaces)):
            self.press("backspace", "清空输入框")
        self._type_text(cfg.share_code, "输入共享代码")
        self.press(cfg.confirm_key, "确认代码文本")
        self.press("down", "移到「确认」")
        self._do_step(lambda: self.press(cfg.confirm_key, "确认（执行搜索）"), "执行搜索")
        # 5) 等结果 → Enter 进挑战 → 等加载
        self.sleep(1.5 if self.cfg.replay else 2.5)
        self.press(cfg.confirm_key, "进入挑战（结果列表里第一张就是目标赛事）")
        print("  [进赛事] 已按 Enter 进赛事，等加载（实测约 1 分钟）…")
        if not self.cfg.replay:
            wait_stable(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                        timeout=cfg.entry_load_timeout, settle=1.5, scale=0.5, poll=0.3)
        print("  [进赛事] 加载结束 ✅")
        self.log.event("event_entered", shot=self._save_evidence(self.frame(), "event_entered"))
        self.stats["events_entered"] = self.stats.get("events_entered", 0) + 1
        return True

    # ---------------- 总入口 ---------------- #
    def run(self) -> dict:
        cfg = self.cfg
        print("=" * 78)
        print(f"vauto 业务运行 | 阶段={cfg.phase} | {'dry-run（不按键）' if cfg.dry_run else '★ 真实按键 ★'}")
        print(f"日志: {self.log.path}")
        print(calibration_table(self.s.calibration))
        print("=" * 78)
        if not cfg.dry_run:
            print("提示：随时按 F1 立即中止；目标窗口不在前台时全部动作会自动暂停。")
        try:
            # 【顺序很重要】车检必须在"菜单可见"的时候做 —— 进了赛事/挑战之后，左上角那块
            # 车名面板就不存在了，check_car_22b 必然失败并把整个运行停掉。
            # 2026-10-02 实测：自动进赛事成功之后卡在这里（日志 car_check_failed，
            # in_menu=false），一次 W 都没按 —— 就是"比赛开始了但程序一动不动"。
            if cfg.a_enabled and cfg.require_car_22b:
                if not self.check_car_22b():
                    print("[!] 当前车辆校验未通过：请手动把车换成 22B 再跑（或 --no-car-check）")
                    if not cfg.dry_run:
                        return {"stopped": "car_check_failed", **self.stats}
            if cfg.a_enabled:
                if cfg.enter_event:
                    # 自动开局：主菜单 → 赛事（用户口述序列）。失败就停下，不盲跑。
                    if not self.enter_event():
                        print("[!] 自动进赛事失败 → 停下（请手动进赛事，或看 logs/ 里的证据图）")
                        if not cfg.dry_run:
                            return {"stopped": "enter_event_failed", **self.stats}
                self.phase_farm(cfg.rounds)
            if cfg.b_enabled and cfg.phase != "farm":
                self.phase_spend(cfg.cars)
        except AbortedByUser as exc:
            print(f"\n[急停] {exc}")
        except KeyboardInterrupt:
            print("\n[Ctrl+C] 手动中断")
        finally:
            self.release_all("收尾")
            self._save_ledger()
        print("-" * 78)
        print(f"本次结果: {self.stats}")
        print(f"台账: {self.ledger}  → {self.cfg.ledger_path}")
        print(f"日志事件统计: {self.log.summary()}")
        return dict(self.stats)

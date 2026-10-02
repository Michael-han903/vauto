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
from vauto.keystate import caps_lock_on, set_caps_lock

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
    "car_tile_22b",                                         # 「我的车辆」里 22B 那一格（换回 22B 用）
    "fav_heart",                                            # 车格右下角的 ♥ = 已加入收藏（B 选车用）
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
    def move_to(self, pos, **kw):
        self.held.append(("move", tuple(pos)))
        return None
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
        # B 阶段"已经弄过的车"指纹集合（选中格车名缩略图）—— 防止在同一两辆车之间打转。
        # 详见 change_car()：2026-10-02 用户实测"来来回回就两辆车在那里换，而且太慢了"。
        self._seen_cars: list = []
        self._all_cars_seen = False        # 列表里已没有"没弄过的车"（B 可以收尾了）
        self._last_car_fp = None           # 刚处理的那台车的车名指纹（加收藏前核对用）
        self._unreachable: list = []       # 本次运行里"走不到"的车（按指纹记，不再重复选它）
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
        """点客户区坐标（会自动换算成屏幕坐标）。点完**把鼠标移回左上角**（见 _park_pointer）。"""
        self.s.stop.check()
        sx, sy = client_to_screen(self.s.hwnd, int(xy[0]), int(xy[1])) if self.s.hwnd else xy
        self.log.event("click", at=[sx, sy], note=note, dry=self.cfg.dry_run)
        if self.cfg.dry_run:
            print(f"  [dry-run] 本应点击屏幕 ({sx}, {sy})  ({note})")
            return
        self.s.sim.click((sx, sy))
        self._park_pointer()

    def _park_pointer(self, note: str = "") -> None:
        """把鼠标移回**最左上角**并停在那儿。

        【用户要求 2026-10-03】"每次用鼠标操作完之后都要把鼠标移动到最左上角，然后下次
        需要鼠标的时候再移动到所需位置"。原因：鼠标停在控件上会改变它的外观（悬停高亮、
        提示条），会干扰后面的画面判据（♥/车格/判页都靠画面）。
        需要鼠标时再移过去 —— sim.click 本身会先把指针移到目标点再按下。
        """
        if not self.cfg.park_pointer or self.cfg.dry_run or self.cfg.replay:
            return
        try:
            px, py = (client_to_screen(self.s.hwnd, *self.cfg.park_at)
                      if self.s.hwnd else self.cfg.park_at)
            self.s.sim.move_to((int(px), int(py)))
            self.log.event("park_pointer", at=[int(px), int(py)], note=note)
        except Exception as exc:                    # 归位失败不影响主流程
            self.log.event("park_pointer_fail", err=str(exc)[:120])

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
        # max_cars = 0 → 「一直解锁到技能点不足」（用户要求的口径）；用 cfg.cars_cap 当安全上限，
        # 防止"所有车都解锁过了"时无限换车。
        cap = max_cars if max_cars > 0 else self.cfg.cars_cap
        title = f"最多 {cap} 台车" if max_cars > 0 else f"一直解锁到「技能点不足」（安全上限 {cap} 台）"
        print(f"\n===== B 阶段：{title}（换车 → 精通页 → Y+Enter） =====")
        self.clear_blocking_dialog("B 阶段开始前", patience=12.0)   # 清掉上一阶段可能留下的弹窗
        self._ensure_vehicle_tab()
        for i in range(cap):
            self.s.stop.check()
            if not self._ensure_vehicle_tab():
                print("  [!] 到不了「车辆」页，结束 B 阶段")
                break
            if not self.change_car():
                if self._all_cars_seen:
                    print("  [B] 列表里已经没有没弄过的车了 → 正常收尾（不算错误）")
                    self.log.event("phase_spend_done", reason="all_cars_seen", cars=i)
                else:
                    # 记为错误（原来这里不计错误，跑挂了汇总里仍是 errors=0，会让人以为一切正常）
                    self.stats["errors"] += 1
                    self.log.event("change_car_failed", car_index=i + 1)
                    print("  [!] 换车失败，结束 B 阶段（详情看日志/证据图）")
                break
            print(f"  [B] 第 {i + 1} 台车：已上车")
            state = self.unlock_current_car()
            need_fav = state in ("unlocked", "already")
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
            if need_fav:
                # 【2026-10-03 用户口径】点满（或本来就满）→ 回车 → ↓(添加至收藏) → 回车。
                # 他手工漏标的车，这一步会补上 —— 下次就少一台要重看的车。
                self.favorite_this_car()
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
        # 【2026-10-03】attempts 5 → 10：上车加载要 13~18 秒，而这段等待以前是靠
        # wait_stable 干等 60 秒撑着的。现在取消干等、由这里轮询（每次 ≈ esc_dwell），
        # 所以次数要够覆盖 20 秒以上，否则加载没完就报"到不了车辆页"。
        for attempt in range(10):
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

    def _selected_tile_title(self, frame: np.ndarray):
        """在「我的车辆」列表里找到**选中格**（黄色高亮框），把车名文字裁成指纹。

        作用：不 OCR 也能判断"现在选中的是哪台车" —— 换车打转/重复就是靠它发现的。
        2026-10-02 用户实测："来来回回就两辆车在那里换，而且太慢了"：原实现只"走一格就
        上车"，选中重复车完全看不出来，每轮还白付 68 秒上车加载。
        黄色框是唯一且明显的（实测最大轮廓面积 351544，第二名只有 71995）。
        """
        if frame is None or getattr(frame, "ndim", 0) < 3:
            return None
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (25, 150, 150), (45, 255, 255))
            gy = int(self.cfg.grid_origin[1])
            mask[:max(0, gy - 10), :] = 0                    # 只看车格区域
            mask[:, int(frame.shape[1] * 0.94):] = 0         # 右边留白别算
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                return None
            x, y, bw, bh = cv2.boundingRect(max(cnts, key=cv2.contourArea))
            # 只接受**完整**车格（实测 680x520）：滚动中间态里框会被裁掉一截，
            # 那时裁出来的"车名"位置是歪的 —— 宁可不认（上层会等下一帧再取），也别认错车。
            if not (600 <= bw <= 780 and 440 <= bh <= 600):
                return None
            tx, ty = x + 18, y + 8
            tw, th = min(600, bw - 30), 92
            if tw <= 0 or ty + th > frame.shape[0] or tx + tw > frame.shape[1]:
                return None
            tile = frame[ty:ty + th, tx:tx + tw]
            # 车名区域的全分辨率灰度图（不是缩略图）：后面用"带对齐的差异"来比，
            # 缩略图会把 1 像素错位放大成 10 的差异，两类就糊在一起了。
            return cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None

    def _fp_diff(self, a, b) -> float:
        """两张车名裁图的差异（先在 ±fp_align_px 内找最佳对齐，再比）。

        为什么必须对齐（2026-10-02 量出来的）：车名是文字，裁窗偏 1 像素就让"同一台车"的
        像素差从 0 跳到 10，偏 6 像素到 53 —— 而"不同车"才 75。不对齐的话两类会糊在一起，
        于是"这台车弄过没有"就会频繁判错。对齐之后：同车 ≈ 0~5，不同车 ≈ 40 以上。
        """
        if a is None or b is None:
            return float("inf")
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
        pad = int(self.cfg.fp_align_px)
        if h - 2 * pad < 8 or w - 2 * pad < 8:
            pad = 0
        core = b[pad:h - pad, pad:w - pad] if pad else b
        if core.size == 0:
            return float("inf")
        try:
            res = cv2.matchTemplate(a, core, cv2.TM_SQDIFF_NORMED)
            return float(res.min()) * 255.0
        except Exception:
            return float("inf")

    MAX_SEEN = 2

    def _remember_done(self, fp) -> None:
        """记下"处理过的那台车"，但**只保留最近 2 条**（种子 + 最近的）。

        为什么不累积（2026-10-03 实测）：车库里有多台**同款同名**的车
        （如 3 辆 CLASS 10 RACE CAR），它们的车名指纹**一模一样** →
        累积起来会把它们互相判成"已处理"而跳过 —— 日志里 seen=1 却跳掉了 5 台没 ♥ 的车。
        只留最近 2 条：既能避免"刚处理完又立刻重来"，又不会误杀掉同名车。
        """
        if fp is None:
            return
        self._seen_cars = (list(self._seen_cars[:1]) + [fp])[-self.MAX_SEEN:]

    def _fp_matches(self, fp, group) -> bool:
        """指纹 fp 是否与集合 group 里某一台是同一台车。"""
        if fp is None:
            return False
        return any(self._fp_diff(fp, s) < self.cfg.fp_same_tol for s in group)

    def _fp_seen(self, fp) -> bool:
        """这台车是不是已经弄过（在已处理集合里）。"""
        return self._fp_matches(fp, self._seen_cars)

    def _tile_fp(self, tries: int = 4):
        """取选中格的指纹；如果框还在动/不完整（滚动中间态）就短等一下重取。"""
        for _ in range(max(1, tries)):
            fp = self._selected_tile_title(self.frame())
            if fp is not None:
                return fp
            self.sleep(0.3)
        return None

    # ---------------- 收藏(♥)驱动的选车（2026-10-03 用户定的新口径）---------------- #
    def _title_crop(self, frame, x, y, w, h):
        """按车格 bbox 裁车名文字（灰度），当车格指纹用。"""
        tx, ty = int(x) + 18, int(y) + 8
        tw, th = min(600, int(w) - 30), 92
        if tw <= 0 or ty + th > frame.shape[0] or tx + tw > frame.shape[1]:
            return None
        return cv2.cvtColor(frame[ty:ty + th, tx:tx + tw], cv2.COLOR_BGR2GRAY)

    def _grid_tiles(self, frame) -> list:
        """检出「我的车辆」里**可见的每个车格**：[(row, col, x, y, w, h, has_heart, heart_score)]。

        为什么不按"车格间距"推位置：2026-10-03 实测这张界面里间距**不固定**
        （x=816/1506/2170/2834 → 690/664/664）→ 推位置会错。直接检白底车格矩形。
        * 车格 = 青色背景上的白色圆角矩形（V>215 & S<45 的实心大块）
        * ♥ = 车格内 (+560,+340) 起 110x80 区域内，与 fav_heart 模板的匹配分（实测有♥=1.000）
        """
        if frame is None or getattr(frame, "ndim", 0) < 3:
            return []
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            S, V = hsv[:, :, 1], hsv[:, :, 2]
            x0, y0, x1, y1 = self.cfg.grid_area
            white = ((V > 215) & (S < 45)).astype(np.uint8) * 255
            white[:max(0, int(self.cfg.grid_origin[1]) - 10), :] = 0
            white[int(y1):, :] = 0
            white[:, :int(x0)] = 0
            white[:, int(x1):] = 0
            white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
            cnts, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            boxes = []
            for c in cnts:
                bx, by, bw, bh = cv2.boundingRect(c)
                if not (self.cfg.tile_w_min <= bw <= self.cfg.tile_w_max
                        and self.cfg.tile_h_min <= bh <= self.cfg.tile_h_max):
                    continue
                if cv2.contourArea(c) < 0.75 * bw * bh:
                    continue
                boxes.append((bx, by, bw, bh))
            boxes.sort(key=lambda t: (t[1], t[0]))
            rows = []
            for t in boxes:
                if rows and abs(t[1] - rows[-1][0][1]) < 60:
                    rows[-1].append(t)
                else:
                    rows.append([t])
            det = self.s.dets.get("fav_heart")
            thr = float(getattr(det, "threshold", 0.85) or 0.85)
            ox, oy = self.cfg.heart_off
            rw, rh = self.cfg.heart_roi
            out = []
            for r, row in enumerate(rows):
                for c, (bx, by, bw, bh) in enumerate(sorted(row)):
                    roi = frame[by + oy:by + oy + rh, bx + ox:bx + ox + rw]
                    sc = float("nan")
                    if det is not None and roi.size:
                        try:
                            hit = self.s.matcher.match_best(roi, det.template, threshold=-1.0)
                            if hit is not None:
                                sc = float(getattr(hit, "score", float("nan")))
                                if getattr(self.s.matcher, "ascending", False):
                                    sc = 1.0 - sc
                        except Exception:
                            sc = float("nan")
                    out.append((r, c, bx, by, bw, bh, bool(sc == sc and sc >= thr), sc))
            return out
        except Exception:
            return []

    def _cursor_box(self, frame, tiles=None):
        """光标所在车格的外框 → (x, y, w, h)；认不出返回 None。

        【2026-10-03 重写】原来是"全图找最大的黄色轮廓" ✗ —— 实测会抓到别的黄色元素
        （品牌标签的高亮、左栏价格框…），于是"光标在哪"乱跳：
        日志 光标中心在 (1140,668) / (1140,1172) / **(856,1676)** 之间蹦，而 856 那里根本
        不是车格 → 走路一会儿 right 一会儿 down，永远走不到目标。
        现在**锚定在已检出的车格上**：对每个车格采样它白底外圈那 5 像素，看是不是"光标黄"
        （HSV 25~45 / S>150 / V>150），一圈最像的那个就是光标所在格。
        车格本身是可靠检出的（白底矩形），所以这个锚点稳。
        """
        if frame is None or getattr(frame, "ndim", 0) < 3:
            return None
        try:
            if tiles is None:
                tiles = self._grid_tiles(frame)
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
            yellow = ((H >= 25) & (H <= 45) & (S > 150) & (V > 150)).astype(np.uint8)
            fh, fw = frame.shape[0], frame.shape[1]
            best = None
            # 【参数是量出来的，2026-10-03】黄框在白底车格框**外 12~20 像素**处：
            #   pad= 5 → 全部 0.00（紧贴那圈没有黄）；pad=12 → 光标格 0.34 / 其他 0.00；
            #   pad=20 → 光标格 0.40 / 其他 0.00  ← 分离干净，所以取 pad=20、阈值 0.20
            pad, thr = 20, 0.20
            for (r, c, bx, by, bw, bh, has, sc) in tiles:
                hit, tot = 0, 0
                for (sy, sx, ey, ex) in ((by - pad, bx, by, bx + bw),
                                         (by + bh, bx, by + bh + pad, bx + bw),
                                         (by, bx - pad, by + bh, bx),
                                         (by, bx + bw, by + bh, bx + bw + pad)):
                    sy, sx = max(0, sy), max(0, sx)
                    ey, ex = min(fh, ey), min(fw, ex)
                    if ey <= sy or ex <= sx:
                        continue
                    sub = yellow[sy:ey, sx:ex]
                    hit += int(sub.sum())
                    tot += int(sub.size)
                frac = hit / float(tot) if tot else 0.0
                if best is None or frac > best[0]:
                    best = (frac, bx, by, bw, bh)
            if best is None or best[0] < thr:      # 没有任何一格被黄框包着
                return None
            return (int(best[1]), int(best[2]), int(best[3]), int(best[4]))
        except Exception:
            return None

    def _cursor_cell(self, frame, tiles):
        """光标（黄色高亮框）现在在哪个车格 → (row, col)；认不出返回 None。"""
        box = self._cursor_box(frame)
        if box is None:
            return None
        try:
            x, y, w, h = box
            cx, cy = x + w / 2.0, y + h / 2.0
            best = None
            for (r, c, bx, by, bw, bh, has, sc) in tiles:
                d = abs(cx - (bx + bw / 2.0)) + abs(cy - (by + bh / 2.0))
                if best is None or d < best[0]:
                    best = (d, r, c)
            return (best[1], best[2]) if best and best[0] < 200 else None
        except Exception:
            return None

    def _walk_to_tile(self, target_fp, tries_each: int = 2) -> bool:
        """走到"车名指纹 = target_fp"的那个车格 —— **按像素方向走，不看行列号**。

        为什么改（2026-10-03 用户跑挂："走不到目标车格 (2,1)（现在 (1,0)）"）：
        * 列表里部分露出的车格时有时无 → 行列号会整体错位，目标和光标对不上；
        * 走动时列表会滚动 → 记下来的目标行列号立刻失效。
        改成：每轮都重新用**指纹**找目标、用**黄框**找光标，按两个框中心的像素偏移选方向
        （目标在下方就 ↓、在右侧就 →），按完验证"黄框真的变了"，两个方向都不动才算失败。
        """
        stuck = 0
        for _ in range(self.cfg.grid_walk_max):
            self.s.stop.check()
            frame = self.frame()
            tiles = self._grid_tiles(frame)
            tbox = None
            for (r, c, bx, by, bw, bh, has, sc) in tiles:
                fp = self._title_crop(frame, bx, by, bw, bh)
                if fp is not None and self._fp_diff(fp, target_fp) < self.cfg.fp_same_tol:
                    tbox = (bx, by, bw, bh)
                    break
            cbox = self._cursor_box(frame, tiles)
            if cbox is None:
                self.log.event("grid_walk_fail", why="cursor_unknown")
                return False
            if tbox is None:                      # 目标滚出视野了 → 沿纵向找回来
                stuck += 1
                if stuck > 8:
                    self.log.event("grid_walk_fail", why="target_lost", stuck=stuck)
                    return False
                # 不记得往前还是往后走丢的 → 两下 ↓、两下 ↑ 地扫（right/left 在车库列表里不动光标）
                self.press("down" if (stuck % 4) < 2 else "up", "目标不在视野 → 纵向找回来")
                self.sleep(self.cfg.grid_walk_dwell)
                continue
            cx, cy = cbox[0] + cbox[2] / 2.0, cbox[1] + cbox[3] / 2.0
            tx, ty = tbox[0] + tbox[2] / 2.0, tbox[1] + tbox[3] / 2.0

            def _ov(a0, a1, b0, b1):
                """两段区间重叠占较短一段的比例（1=完全重叠，0=不沾边）。"""
                inter = min(a1, b1) - max(a0, b0)
                short = min(a1 - a0, b1 - b0)
                return inter / float(short) if short > 0 else 0.0

            same_col = _ov(cbox[0], cbox[0] + cbox[2], tbox[0], tbox[0] + tbox[2]) > 0.5
            same_row = _ov(cbox[1], cbox[1] + cbox[3], tbox[1], tbox[1] + tbox[3]) > 0.5
            if same_col and same_row:
                return True                        # 光标已经在目标格上
            # 【2026-10-03 实测修正】车库列表是**纵向**的：`↓` 列内往下、到底跳下一列，
            # `↑` 是它的反向；而 **`→` / `←` 在车库列表里根本不动光标**
            # （日志：连按 4 次 right，光标框一直是 (816,424,648,488) 一动不动 → target_lost）。
            # 所以走路只用 ↓/↑：
            #   * 目标在别的列 → 先沿着 ↓/↑ "换列"（列优先，用户口径）；
            #   * 列对上了 → 再按行 ↓/↑。
            keys = []
            if not same_col:
                keys.append("down" if tx > cx else "up")
            elif not same_row:
                keys.append("down" if ty > cy else "up")
            keys += [k for k in ("down", "up") if k not in keys]
            self.log.event("grid_walk_step", same_col=bool(same_col), same_row=bool(same_row),
                           keys=keys[:2], cursor=list(cbox), target=list(tbox))
            progressed = False
            for key in keys[:max(1, tries_each)]:
                self.press(key, f"走向目标车格（光标中心 {int(cx)},{int(cy)}"
                                f"{'' if same_col else '，先换列'}）")
                self.sleep(self.cfg.grid_walk_dwell)
                f2 = self.frame()
                t2 = self._grid_tiles(f2)
                c2 = self._cursor_box(f2, t2)
                if c2 is not None and tuple(c2) != tuple(cbox):
                    progressed = True
                    break
            if not progressed:
                shot = self._save_evidence(self.frame(), "grid_walk_stuck")
                print(f"  [!] 走不到目标车格（光标框 {cbox}）证据 {shot}")
                self.log.event("grid_walk_fail", why="no_progress", cursor=list(cbox), shot=shot)
                return False
        self.log.event("grid_walk_fail", why="budget")
        return False

    def _enter_car_now(self) -> bool:
        """对**列表里当前选中格**执行：Enter（选择操作）→ Enter（上车）→ 等加载 → 回车辆页。"""
        self.press(self.cfg.confirm_key, "打开「选择操作」")
        if not self.cfg.replay:
            # 这两个是**手工推定**的阈值，实测一直没命中（日志 menu_title=False
            # enter_row=False）→ 每次白等 5.9 秒（2026-10-03 日志）。缩短成"诊断用的一瞥"，
            # 反正下面那个 Enter 是幂等的（菜单开着就会选「上车」）。
            menu = self._wait_for("menu_select_title", 1.2)
            row = self._wait_for("option_enter_car", 0.8)
            if menu is None or row is None:
                self.log.event("car_menu_threshold_check", menu_title=bool(menu),
                               enter_row=bool(row),
                               note="「选择操作」菜单判据不完整，仍继续")
        self.press(self.cfg.confirm_key, "选「上车」")
        if not self.cfg.replay:
            # 【2026-10-03 用户实测】"会卡在这个界面的查看车辆里面的选项上" ——
            # 因为**当前车辆**那一格的「选择操作」菜单里**没有「上车」这一项**
            # （用户截图：只剩 添加至收藏 / 查看车辆 / 查看历史记录），于是这一下回车
            # 点成了别的项，菜单还开着/进了别的界面，人就卡在那儿。
            # 复核判据：真的"上车"会立刻进入加载（车格全部消失）。
            # **不能**用「我的车辆」标题判：菜单弹出时背景是模糊的，标题匹配不上
            # （第一次修就是这么漏判的）。
            _t1 = self._grid_tiles(self.frame())      # 刚点完的回车（上车会立刻进加载）
            self.sleep(2.0)
            _t2 = self._grid_tiles(self.frame())
            if _t1 and _t2:                       # 两次都还能看到车格 → 没进加载
                self.log.event("enter_car_not_offered", tiles=len(_t2),
                               note="车格还在 → 没上车（菜单首项不是「上车」，很可能是当前车辆）")
                print(f"  [!] 这台车没有「上车」选项（车格还在 {len(_t2)} 个）→ 按 Esc 退出菜单")
                self.press("esc", "退出「选择操作」菜单")
                self.sleep(0.8)
                return False
            # 【2026-10-03 实测 · 修"上车后等 61 秒"】原来这里用 wait_stable 等"画面静止"，
            # 但上车加载完之后落在**主世界（自由驾驶）** —— 那画面一直在动（车在开、云在飘），
            # 永远不会"静止" → 每次都等满 car_change_timeout=60 秒超时才继续
            # （日志：选「上车」→ +61.3s → 才按 Esc）。
            # 改：只给一点固定缓冲，剩下的交给 _ensure_vehicle_tab 自己轮询（它会按 Esc 并
            # 反复判页）—— 加载一结束它自然把我们带到「车辆」页，不用干等。
            self.sleep(self.cfg.after_enter_car_wait)
            # 【2026-10-02 用户实测确认】点「上车」后游戏**一定回到主世界（自由驾驶）**，
            # 要按 Esc 才出现主菜单 → 所以这里是 after_load=True（Esc 优先），
            # 之后还要点「车辆」标签才回到车辆页。判页分数写进日志（ensure_tab_probe）。
            if not self._ensure_vehicle_tab(after_load=True):
                shot = self._save_evidence(self.frame(), "after_load_no_vehicle_tab")
                print(f"  [!] 上车后回不到「车辆」页（证据: {shot}）")
                self.log.event("after_load_stuck", shot=shot)
                return False
        return True

    # ---------------- B：♥ 驱动的换车（2026-10-03 用户定的口径）---------------- #
    @staticmethod
    def _order_candidates(todo):
        """候选车格的优先顺序（用户口径 2026-10-03）。

        "识别到一张图片内有多辆未收藏车辆的时候，应该先看最左列的，如果最左列有不止一辆，
        应该看先最上面的" → 按 **(列, 行)** 升序；不要按"离光标最近"（那样会来回跳、
        而且不符合你看屏幕的顺序）。元素是 ((row, col, ...), fp)。
        """
        return sorted(todo, key=lambda p: (p[0][1], p[0][0]))

    def change_car(self) -> bool:
        """换到列表里**一台还没收藏（♥）的车**：点「更换车辆」→ 找没 ♥ 的车格 → 走过去 → 上车。

        用户口径（2026-10-03 原话）："我会把每一辆点满了技术点的车辆加入收藏，你可以在车辆
        列表里面看没有加入收藏的车辆然后去点技术点……每一辆车右下角的爱心就是已经加入了
        收藏的意思"。所以：
        * **没 ♥ = 还没点满，需要处理** —— 现成的、跨会话持久的目标清单（比之前的指纹集合可靠）；
        * 处理完一台就按 回车 → ↓(添加至收藏) → 回车 把它标记掉（见 favorite_this_car）。
        这一屏全都有 ♥ 就按 → 往右滚一列继续找；滚不动了 = 没有待处理的车了。
        """
        if not self.click_match("tile_change_car", note="点「更换车辆」"):
            return False
        # 等列表出现。**必须用页面标题判据**：左上角「当前车辆」名条在「车辆」标签页上也有，
        # 用它等于没判 —— 第一次真跑（2026-10-02 20:43）就是这么漏过去的。
        if self._wait_for("page_title_garage", self.cfg.page_timeout) is None:
            shot = self._save_evidence(self.frame(), "nav_list_not_opened")
            print(f"  [!] 「更换车辆」列表没出现（证据: {shot}）")
            self.log.event("nav_list_missing", shot=shot)
            return False
        if not self.cfg.replay:
            wait_stable(self.s.capture, stop_event=getattr(self.s.stop, "event", None),
                        timeout=self.cfg.car_change_timeout, settle=0.6, scale=0.5,
                        poll=min(0.3, self.cfg.poll))
        # 【2026-10-03 用户纠正】"**只有最开始第一次**进入车辆列表的时候，第一辆才是当前车辆；
        # 以后的最左侧第一辆就不是了"。所以这里只在**第一次**进列表时，把"光标所在那格"
        # 记成已处理（那一刻它就是当前车辆）；之后一律靠 指纹 + ♥ 判断，绝不假设位置。
        # （另外当前车辆那格的「选择操作」菜单里没有「上车」项，见 _enter_car_now 的复核。）
        if not self._seen_cars:
            _f0 = self.frame()
            _t0 = self._grid_tiles(_f0)
            _c0 = self._cursor_cell(_f0, _t0)
            if _c0 is not None:
                for (_r, _c, _bx, _by, _bw, _bh, _has, _sc) in _t0:
                    if (_r, _c) == tuple(_c0):
                        _fp0 = self._title_crop(_f0, _bx, _by, _bw, _bh)
                        if _fp0 is not None:
                            self._remember_done(_fp0)
                            self.log.event("car_fp_seed", cell=list(_c0),
                                           note="首次进列表：光标那格=当前车辆，跳过")
                            print(f"  [找] 首次进列表，光标在 ({_r},{_c}) = 当前车辆 → 记为已处理")
                        break
        for attempt in range(1, self.cfg.nav_budget + 1):
            self.s.stop.check()
            frame = self.frame()
            tiles = self._grid_tiles(frame)
            if not tiles:
                shot = self._save_evidence(frame, "grid_no_tiles")
                print(f"  [!] 认不出车格（证据 {shot}）→ 结束 B")
                self.log.event("grid_no_tiles", shot=shot)
                return False
            cur = self._cursor_cell(frame, tiles)
            # 【两道校验并用，缺一不可】2026-10-03 用户补充：“有的车我点满了也没有加入收藏，
            # 所以你不能删掉旧的校验机制” —— 所以 ♥ 只是“他标记过的车”，不是全集：
            # 候选 = 「没 ♥」**且**「本次会话还没处理过」（指纹集合，旧机制）。
            todo = []
            skipped = 0
            for t in tiles:
                if t[6]:                        # 有 ♥ → 用户标过"已点满"
                    continue
                fp = self._title_crop(frame, t[2], t[3], t[4], t[5])
                if self._fp_seen(fp):
                    skipped += 1                # 没♥但指纹说这次已经弄过 → 跳过（省掉 70 秒）
                    continue
                if self._fp_matches(fp, self._unreachable):
                    skipped += 1                # 之前走过但走不到的车，别再选它
                    continue
                todo.append((t, fp))
            todo = self._order_candidates(todo)
            hearts = [t[7] for t in tiles if t[7] == t[7]]
            no_heart = sum(1 for t in tiles if not t[6])
            self.log.event("grid_scan", attempt=attempt, tiles=len(tiles), todo=len(todo),
                           no_heart=no_heart, fp_skipped=skipped, seen=len(self._seen_cars),
                           cursor=None if cur is None else list(cur),
                           heart_min=round(min(hearts), 3) if hearts else None,
                           heart_max=round(max(hearts), 3) if hearts else None)
            if todo:
                (r, c, bx, by, bw, bh, has, sc), fp = todo[0]
                if fp is None:
                    fp = self._title_crop(frame, bx, by, bw, bh)
                print(f"  [找] 第 {attempt} 屏：{len(tiles)} 格 / 待处理 {len(todo)}"
                      f"（没♥ {no_heart} − 本次已弄过 {skipped}）→ 去第{r}行第{c}列（♥ 分 {sc:.3f}）")
                shot = self._save_evidence(frame, f"target_r{r}c{c}")
                self.log.event("grid_target", row=r, col=c, heart_score=round(sc, 3),
                               cand=len(todo), fp_ok=fp is not None, shot=shot)
                if not self._walk_to_tile(fp):
                    # 走不到这台 → 记下它的指纹（本次运行不再选它），换下一台继续，
                    # 不能因为一台够不着就把整个 B 阶段停掉（2026-10-03 就是这么失败的）
                    if fp is not None:
                        self._unreachable.append(fp)
                    print("  [找] 这台走不到 → 记下并换下一台")
                    self.log.event("grid_target_unreachable", row=r, col=c)
                    continue
                self._last_car_fp = fp
                print("  [换] 走到目标车 → 上车")
                if not self._enter_car_now():
                    # 这台没有「上车」选项（很可能是当前车辆，或菜单首项漂移）→
                    # 记下来换下一台，**不要整个 B 阶段停掉**
                    if fp is not None:
                        self._unreachable.append(fp)
                    print("  [找] 这台进不去 → 记下并换下一台")
                    self.log.event("grid_target_unreachable", row=r, col=c, why="enter_failed")
                    continue
                if self._last_car_fp is not None:   # 记进“刚处理过”（旧校验机制，但只留最近 2 条）
                    self._remember_done(self._last_car_fp)
                    self.log.event("car_fp_record", total=len(self._seen_cars))
                return True
            # 这一屏都收藏过了 → 往右滚一列，把后面的车拉进来
            print(f"  [找] 第 {attempt} 屏：{len(tiles)} 格全都有 ♥ → 往右滚一列")
            before = self._list_roi(frame)
            self.press("right", "右移一列（找没收藏的车）")
            self.sleep(self.cfg.grid_walk_dwell)
            after = self._list_roi(self.frame())
            diff = float(block_max_abs_diff(before, after, blocks=self.cfg.nav_change_blocks))
            self.log.event("grid_advance", attempt=attempt, diff=round(diff, 1))
            if diff < self.cfg.nav_change_threshold:
                shot = self._save_evidence(self.frame(), "grid_end")
                print(f"  [B] 列表滚不动了（分块差 {diff:.1f}）→ 没有待处理的车了（证据 {shot}）")
                self.log.event("grid_end", diff=round(diff, 1), shot=shot)
                self._all_cars_seen = True
                return False
        shot = self._save_evidence(self.frame(), "grid_budget")
        print(f"  [!] 翻了 {self.cfg.nav_budget} 屏都没找到没收藏的车（证据 {shot}）")
        self.log.event("grid_budget", shot=shot)
        self._all_cars_seen = True
        return False

    def favorite_this_car(self) -> bool:
        """把**刚点满的那台车**加入收藏：回车 → ↓(添加至收藏) → 回车（用户定的标记法）。

        为什么做：用户用收藏当"这台已点满"的标记 → 下次（甚至下次开游戏）只看"没 ♥ 的车"，
        程序不需要任何"上次弄到哪"的记忆。
        加之前用**车名指纹**核对光标确实在刚处理的那台车上 —— 绝不点错车。
        """
        if self.cfg.replay:
            return True
        if not self._ensure_vehicle_tab():
            return False
        if not self.click_match("tile_change_car", note="点「更换车辆」（准备加收藏）"):
            return False
        if self._wait_for("page_title_garage", self.cfg.page_timeout) is None:
            print("  [!] 加收藏前：列表没打开")
            return False
        if self._last_car_fp is None:
            print("  [!] 没有「刚处理的那台车」的指纹 → 跳过收藏（不猜、不点错车）")
            self.log.event("favorite_no_target", why="no_fp")
            return False
        # 走过去 = 按指纹找（位置无关）→ 到不了就不收藏
        if not self._walk_to_tile(self._last_car_fp):
            shot = self._save_evidence(self.frame(), "favorite_walk_fail")
            print(f"  [!] 走不到刚处理的那台车 → 不加收藏 证据 {shot}")
            self.log.event("favorite_no_target", why="walk_fail", shot=shot)
            return False
        # 【2026-10-03 用户实测："会卡在这个界面的查看车辆里面的选项上"】
        # 「选择操作」菜单的**项数会变**：一般 5 项（上车 / 添加至收藏 / 查看车辆 / …），
        # 而当前车辆那格只有 3 项（添加至收藏 / 查看车辆 / 查看历史记录）——
        # 那时"↓ 一次"就跑到了「查看车辆」，回车就进了看车界面，卡住。
        # 所以：先按 ↓×1 试，不做数就 Esc 退出来，再用 ↓×0 试一次（总能有对的）。
        # 每种组合之后都用"那一格有没有出现 ♥"复核。
        for attempt, downs in enumerate((1, 0)):
            self.press(self.cfg.confirm_key,
                       f"打开「选择操作」（加收藏·第 {attempt + 1} 次：↓×{downs}）")
            self.sleep(0.9)
            for _ in range(downs):
                self.press(self.cfg.fav_key, "↓ 选「添加至收藏」")
                self.sleep(0.4)
            self.press(self.cfg.confirm_key, f"确认「添加至收藏」（第 {attempt + 1} 次）")
            self.sleep(1.1)
            if self._heart_shown(self._last_car_fp):
                self.log.event("favorite_done", ok=True, downs=downs)
                print("  [B] 加入收藏 成功 ✅（已看到 ♥）")
                return True
            if attempt == 0:
                print("  [!] 没看到 ♥ → 多半是 3 项菜单（首项就是「添加至收藏」）→ "
                      "Esc 退出来，改成不按 ↓ 再试一次")
                self.press("esc", "退出（可能进了「查看车辆」或菜单还开着）")
                self.sleep(1.0)
                self.press("esc", "再退一层")
                self.sleep(1.0)
                if not self._ensure_vehicle_tab():
                    self.log.event("favorite_done", ok=False, why="tab_lost")
                    return False
                if not self.click_match("tile_change_car", note="重新进列表（重试加收藏）"):
                    self.log.event("favorite_done", ok=False, why="list_lost")
                    return False
                if self._wait_for("page_title_garage", self.cfg.page_timeout) is None:
                    self.log.event("favorite_done", ok=False, why="list_lost")
                    return False
                if not self._walk_to_tile(self._last_car_fp):
                    self.log.event("favorite_done", ok=False, why="walk_back_failed")
                    return False
        self.log.event("favorite_done", ok=False, why="both_offsets_failed")
        print("  [B] 加入收藏 没成功（两种按键组合都不行）→ 下次还会被当成待处理")
        return False

    def _heart_shown(self, fp) -> bool:
        """复核：指纹对应的那一格现在是不是有 ♥（按指纹找，不受滚动影响）。"""
        if fp is None:
            return False
        for _ in range(3):
            frame = self.frame()
            for (r, c, bx, by, bw, bh, has, sc) in self._grid_tiles(frame):
                f = self._title_crop(frame, bx, by, bw, bh)
                if f is not None and self._fp_diff(f, fp) < self.cfg.fp_same_tol:
                    return bool(has)
            self.sleep(0.4)
        return False

    def change_car_legacy(self) -> bool:
        """【已停用，保留作回退】指纹驱动的换车：走一步 → 取指纹 → 弄过就继续走。

        2026-10-03 用户改用「收藏(♥)」当已完成标记后，本方法被新的 change_car 取代。
        新的实机验证通过后可以删掉这个。
        """
        if not self.click_match("tile_change_car", note="点「更换车辆」"):
            return False
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

        # 第一次进 B：把"当前这辆车"也记成弄过的 —— B 的目的就是换走，别原地重来一遍
        if not self._seen_cars:
            fp0 = self._selected_tile_title(self._nav_base_frame)
            if fp0 is not None:
                self._seen_cars.append(fp0)
                self.log.event("car_fp_seed", note="当前车也算弄过（B 就是要把车换走）")

        repeats = 0
        for attempt in range(1, self.cfg.nav_budget + 1):
            fp = self._tile_fp()
            if fp is not None and self._fp_seen(fp):
                repeats += 1
                self.log.event("car_fp_repeat", attempt=attempt, repeats=repeats,
                               seen=len(self._seen_cars))
                print(f"  [换] 第 {attempt} 步：这台车弄过了（指纹重复 {repeats}）→ 继续往前")
                if repeats >= self.cfg.nav_repeat_limit:
                    shot = self._save_evidence(self.frame(), "car_fp_stuck")
                    print(f"  [!] 连续 {repeats} 次都是弄过的车 → 列表里没新车了（证据 {shot}）")
                    self.log.event("car_fp_stuck", repeats=repeats, shot=shot)
                    self._all_cars_seen = True
                    return False
            else:
                # fp is None（认不出选中格：离线回放 / 界面异常）→ 退化成原来的"走一格就上车"
                self.log.event("car_fp_new", attempt=attempt, fp_ok=fp is not None)
                print(f"  [换] 第 {attempt} 步："
                      + ("选中的是没弄过的车 → 上车" if fp is not None
                         else "认不出选中格 → 按原逻辑上车"))
                if self._enter_car_now():
                    if fp is not None:
                        self._seen_cars.append(fp)
                        self.log.event("car_fp_record", total=len(self._seen_cars))
                    self.log.event("nav_step", steps=walker.steps)
                    return True
                return False
            # 往前走一格（走不动就留证据、报错）
            if not walker.step():
                before_shot = self._save_evidence(self._nav_base_frame, "nav_stuck_before")
                after_shot = self._save_evidence(self._nav_last_frame, "nav_stuck_after")
                print(f"  [!] 走不动了（光标没动）证据: {before_shot} | {after_shot}")
                self.log.event("nav_stuck", before=before_shot, after=after_shot,
                               steps=getattr(walker, "steps", None))
                return False
        # 走到预算尽头（没找到没弄过的车）
        shot = self._save_evidence(self.frame(), "car_fp_budget")
        print(f"  [!] 走了 {self.cfg.nav_budget} 步没找到没弄过的车（证据 {shot}）")
        self.log.event("car_fp_budget", steps=self.cfg.nav_budget, shot=shot)
        self._all_cars_seen = True
        return False

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
        hit = self._wait_for("hint_unlock_all", 3.0)
        if hit is None:
            sc, _m = self._probe_loc("hint_unlock_all", self.frame())
            # 判据 = 底部「[Y] 解锁全部加成」提示在不在：本页没有可解锁项时游戏不显示它。
            # 这条是**间接**证据，所以把分数一起记进日志 —— 事后能核查"是不是误判成 already"。
            self.log.event("mastery_already",
                           hint_score=(round(sc, 3) if sc == sc else None),
                           threshold=det.threshold)
            print(f"  [B] 没有「[Y] 解锁全部加成」提示"
                  f"（{sc:.3f}/{det.threshold}）→ 本页已解锁过，跳过")
            return "already"
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
    # ---------------- 回 22B（B 跑完回 A 的前提）---------------- #
    def _probe_loc(self, name: str, frame: np.ndarray):
        """单帧定位（不动去抖状态）→ 返回 (分数, Match)；Match 已在全帧坐标上。"""
        det = self.s.dets.get(name)
        if det is None:
            return float("nan"), None
        return det.probe(frame)

    def _grid_cell_of(self, hit) -> Tuple[int, int]:
        """车格位置 → (第几列, 第几行)。间距/原点都是实机量出来的，不是猜的。"""
        gx, gy = self.cfg.grid_origin
        px, py = self.cfg.grid_pitch
        return (max(0, int(round((hit.x - gx) / px))),
                max(0, int(round((hit.y - gy) / py))))

    def _enter_car_and_verify(self, where: str = "") -> bool:
        """对**当前选中格**按 Enter（选择操作）→ Enter（上车）→ 等加载 → Esc → 复核是不是 22B。

        【2026-10-02 实测的不变量】上车后游戏一定回到**主世界（自由驾驶）**，那里没有车名
        面板 → 必须先按一次 Esc 才会出现主菜单。所以复核必须在 Esc **之后**做：上一版等的是
        车库页的面板，等于永远等不到（那个 bug 还没在实机暴露，因为那次用户提前按了 F1）。
        顺带把落点留成"主菜单"——循环里下一轮 A 要正是从这里进赛事。
        """
        self.press("enter", "打开「选择操作」")
        self.sleep(0.9)
        self.press("enter", "选「上车」")
        # 【别当卡死】上车要加载 13~18s（实测），这期间没有任何判据命中是正常的。
        self.sleep(self.cfg.car_load_wait)
        self.press("esc", "上车后回主菜单（自由驾驶 → 主菜单）")
        self.sleep(1.5)
        ok = False
        for _ in range(6):          # 面板可能还要一点时间才画出来
            if self.check_car_22b(log_fail=False):
                ok = True
                break
            self.sleep(1.0)
        self.log.event("set_22b_enter", where=where, ok=bool(ok))
        print(f"  [换] {where} → " + ("成功，当前车就是 22B ✅"
                                    if ok else "Esc 回主菜单后复核：当前车**不是 22B**"))
        return bool(ok)

    def set_car_22b(self) -> bool:
        """把当前车换回 1998 斯巴鲁 Impreza 22B-STI —— 跑 A 的前提。

        实机量到的界面事实（2026-10-02，用户截图 + 视频帧）：
        * 「我的车辆」列表第一个标签「当前车辆」里装的是**全部车**（不按品牌过滤），当前车排最前；
        * 22B 那格的车名文字 = templates/car_tile_22b.png；车格间距 ≈ (709, 522)、首格原点 ≈ (800, 408)；
        * 选中某格：**先试鼠标点它**（最简单），点不动就用方向键按格数走过去（用户手动就是这么走的）；
        * 上车 = Enter（选择操作）→ Enter（上车）→ 加载 13~18s；
        * 复核：check_car_22b（左上角车名面板，认车的那个判据）。
        """
        cfg = self.cfg
        print("\n===== 换回 22B：更换车辆 → 找 IMPRESA 22B-STI → 上车 =====")
        # 【血泪教训 2026-10-02】判据加进标定表还不够，必须同时加进 runner.py 顶部的
        # DETECTORS 名单 —— 那次漏了 car_tile_22b，扫描循环第一行就 KeyError，
        # 程序带着未处理的 traceback 直接退出（日志里只剩 release_all 收尾）。
        # 现在缺判据只会安静地放弃换车，不会把整轮跑挂掉。
        det22 = self.s.dets.get("car_tile_22b")
        if det22 is None:
            print("  [!] 判据 car_tile_22b 没装配（DETECTORS 名单漏了？）→ 放弃换车，其余照常")
            self.log.event("set_22b_fail", where="detector_missing")
            self.stats["errors"] = self.stats.get("errors", 0) + 1
            return False
        if self.check_car_22b(log_fail=False):
            print("  [=] 当前已经是 22B，不用换")
            self.log.event("set_22b_skip", reason="already_22b")
            return True
        if not self._ensure_vehicle_tab():
            return False
        if not self.click_match("tile_change_car", note="点「更换车辆」"):
            return False
        if self._wait_for("page_title_garage", cfg.page_timeout) is None:
            print("  [!] 换车列表没打开")
            self.log.event("set_22b_fail", where="list_not_open")
            return False
        # 【一列一列扫，直到**真的走不动**才认输】
        # 2026-10-02 实测教训：用户车库几千台车、三四百列，而我只给了 12 列预算 →
        # 日志 set_22b_scan ×12 score=null → set_22b_fail where=exhausted，可那时
        # 列表每一步都还在动（diff≈149）。所以：预算给到 450 列，真正的结束条件是
        # "连续 2 次画面没变"（= 列表到头了）。
        # 速度：每轮只抓 1 帧（同帧既用来找 22B，也用来判断列表动没动），约 0.6~0.7s/列，
        # 扫满 450 列约 5 分钟 —— 这个代价只在"B 换过车之后"付一次。
        self.sleep(1.0)
        prev = self._list_roi(self.frame())
        went, stall, best = 0, 0, 0.0
        tries_on_found = 0      # 已经定位到 22B 那格、但没换成功了几次（防死循环）
        stop = "budget"         # 为什么停下来（写进日志，省得下次还要猜）
        while went < cfg.car_find_tries and stall < 2:
            frame = self.frame()
            score, hit = self._probe_loc("car_tile_22b", frame)
            if score != score:
                score = 0.0
            best = max(best, score)
            if hit is None or score < det22.threshold:
                # 视野里没有 22B → 往右走一格，同时看画面到底动没动
                if went % cfg.car_scan_report == 0:
                    print(f"  [找] 已扫 {went} 列 | 本次最高 {best:.3f}"
                          f"（阈值 {det22.threshold}）")
                    self.log.event("set_22b_scan", went=went, best=round(best, 3))
                self.press("right", "列表里往右一格")
                self.sleep(0.35)
                after = self._list_roi(self.frame())
                diff = float(block_max_abs_diff(prev, after, blocks=cfg.nav_change_blocks))
                prev = after
                went += 1
                if diff < cfg.nav_change_threshold:
                    stall += 1
                    self.log.event("set_22b_end_check", went=went, diff=round(diff, 1), stall=stall)
                else:
                    stall = 0
                continue
            # ---- 找到 22B 那格了 ----
            # 【顺序有讲究，2026-10-02 23:50 实测】鼠标点车格**选不中**：点完按 Enter/Enter
            # 面板等满 60s 都没回来（日志 set_22b_enter panel=false）；而方向键按格距走过去
            # 一次就换成了。所以：**先方向键**，鼠标点只当兜底（不浪费那 60 秒）。
            pt = (int(hit.center[0]), int(hit.center[1]))
            col, row = self._grid_cell_of(hit)
            self.log.event("set_22b_found", went=went, at=list(pt), score=round(score, 3),
                           col=col, row=row)
            print(f"  [找] 第 {went} 列找到 22B：{pt}（{score:.3f}）"
                  f"→ 方向键 右{col} 下{row} 走过去")
            self.log.event("set_22b_arrows", col=col, row=row)
            for _ in range(col):
                self.press("right", "往右一列")
                self.sleep(0.3)
            for _ in range(row):
                self.press("down", "往下一行")
                self.sleep(0.3)
            self.sleep(0.8)
            if self._enter_car_and_verify(f"方向键{col},{row}"):
                return True
            # 方向键也不见效 → 试鼠标点它（别的界面/别的游戏鼠标点也许能选中）
            print("  [找] 方向键没成功 → 退一步试鼠标点这格")
            self.click_client(pt, "点 22B 车格")
            self.sleep(0.9)
            if self._enter_car_and_verify(f"鼠标点{pt}"):
                return True
            # 两条路都没换成（比如鼠标点选不中、格距算错）→ 记一次失败
            tries_on_found += 1
            self.log.event("set_22b_try_failed", at=list(pt), n=tries_on_found)
            if tries_on_found >= 3:
                print("  [!] 3 次定位到 22B 那格都没换成功 → 停下（证据图能看出卡在哪一步）")
                stop = "tries_on_found"
                break
            # 还愿意再试：把这一格让过去，继续往右扫（换一格再试）
            went += 1
            self.press("right", "往右一格（换一格再试）")
            self.sleep(0.35)
            prev = self._list_roi(self.frame())
        if stall >= 2:
            stop = "list_end"
        print(f"  [找] 扫了 {went} 列，停因={stop}（连续 {stall} 次画面没变）最高分 {best:.3f}")
        shot = self._save_evidence(self.frame(), "set_22b_failed")
        print(f"  [!] 没换成 22B → 证据 {shot} → 停下（不盲换）")
        self.log.event("set_22b_fail", where=stop, went=went, best=round(best, 3), shot=shot)
        self.stats["errors"] = self.stats.get("errors", 0) + 1
        return False

    def check_car_22b(self, log_fail: bool = True) -> bool:
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
        if not log_fail:
            return False          # 预检用（set_car_22b 开头）——"当前不是 22B"是已知前提，别记成失败
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

    def clear_blocking_dialog(self, note: str = "", patience: float = 45.0) -> bool:
        """等「挡路弹窗」出现并关掉它（**只看真判据**；按键由 cfg.dialog_cancel_key 决定，默认回车）。

        2026-10-02 三次实测教训（都写在这里免得再踩）：
        1) 只检查一次不行 —— 「退出赛事 → 弹窗」是淡入过程，那一刻画面在动（日志 dismiss_skip
           reason=画面在动）。
        2) 「画面静止 = 有东西在等输入」这个判据对它**是错的**：这个弹窗盖在**活的自由驾驶
           画面**上（背景一直在动）→ 永远判成"在动" → 25 秒里一次都没动手（日志
           dialog_cleared acted=0）。所以必须用真判据 popup_rate_event。
        3) 判据模板要裁**与高亮无关**的部分（标题条 + 说明文字，不含三行选项）——
           同一个弹窗两次截图的高亮行不一样（一次「点赞」、一次「取消」）。
        """
        if self.cfg.replay:
            return False
        det = self.s.dets.get("popup_rate_event")
        acted = 0
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            in_menu, _sc = self._in_menu_now()
            if in_menu:
                self.log.event("dialog_cleared", note=note, in_menu=True, acted=acted)
                return True
            frame = self.frame()
            hit = det.observe(frame) if det is not None else None
            if hit is None:
                self.sleep(0.6)
                continue
            acted += 1
            shot = self._save_evidence(frame, f"clear_dialog{acted}")
            print(f"  [兜底] {note or '挡路弹窗'}：评分弹窗判据命中（{hit.score:.3f}）"
                  f"→ 按 {self.cfg.dialog_cancel_key} 关掉  证据: {shot}")
            self.log.event("clear_dialog", note=note, score=round(hit.score, 3),
                           index=acted, shot=shot, key=self.cfg.dialog_cancel_key)
            self.press(self.cfg.dialog_cancel_key, "关掉「为挑战评分?」等弹窗")
            if self._wait_gone("popup_rate_event", 6.0):
                self.log.event("dialog_cleared", note=note, in_menu=False, acted=acted)
                print("  [兜底] 弹窗已消失 ✅")
                return True
            if acted >= 3:
                break
        self.log.event("dialog_cleared", note=note, in_menu=False, acted=acted, gave_up=True)
        print(f"  [!] {note or '挡路弹窗'}：等了 {patience:.0f}s 没等到评分弹窗（也可能它已不在）")
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
        if not cfg.dry_run:
            self._park_pointer("开跑前先把鼠标归位到左上角")   # 免得一开始就悬停在某个控件上
        # 【用户要求 2026-10-03】"设置一下程序进入地平线六的时候自动检测大写锁定状态，
        # 没开的话开一下" —— 游戏的按键绑定/输入行为与 Caps Lock 有关，开着才和手动操作一致。
        # 已经是开的就什么都不做（只读一次状态，不发多余按键）。
        if cfg.ensure_caps_lock and not cfg.dry_run:
            _cap_before = caps_lock_on()
            _cap_after = set_caps_lock(True)
            print("  [准备] 大写锁定：" + ("本来就是开的 ✓" if _cap_before
                                        else "原来是关的 → 已打开 ✓")
                  + f"（现在 {'开' if _cap_after else '关 ✗ 请手动开一下'}）")
            self.log.event("caps_lock", before=bool(_cap_before), after=bool(_cap_after))
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

            # ---- 主循环：[进赛事 → 跑 N 轮] → [B 一直解锁到「技能点不足」] → [换回 22B] ----
            # 用户口径（2026-10-02）："直到弹出提示说技能点不足再回去跑挑战"。
            # --cycles 默认 1（一轮就退），>1 时自动"回 A 刷点 → 再花"，不用手动重跑命令。
            cyc_total = max(1, int(cfg.cycles))
            for cyc in range(1, cyc_total + 1):
                if cyc > 1:
                    print(f"\n########## 循环 {cyc}/{cyc_total}"
                          f"（上一轮 B 已刷到「技能点不足」→ 回来再跑挑战）##########")
                if cfg.a_enabled:
                    if cfg.enter_event:
                        # 自动开局：主菜单 → 赛事（用户口述序列）。失败就停下，不盲跑。
                        if not self.enter_event():
                            print("[!] 自动进赛事失败 → 停下（请手动进赛事，或看 logs/ 里的证据图）")
                            if not cfg.dry_run:
                                return {"stopped": "enter_event_failed", **self.stats}
                    self.phase_farm(cfg.rounds)
                spent_before = self.stats.get("unlock_presses", 0)
                if cfg.b_enabled and cfg.phase != "farm":
                    self.phase_spend(cfg.cars)
                # 【闭环的关键一步】B 之后把车换回 22B —— 否则下一轮 A 用的不是 22B
                # （A 的节奏/判据都是按它标定的）。--phase spend 也会走到这里，方便单独试。
                if cfg.b_enabled and cfg.back_to_22b and not cfg.dry_run:
                    if not self.set_car_22b():
                        print("[!] 换回 22B 失败 → 停下（下次跑 A 之前请手动换车；证据见 logs/）")
                        return {"stopped": "back_to_22b_failed", **self.stats}
                # 一整轮 B 一次都没解锁 → 大概率所有车的技能都点完了，再循环只是白刷挑战
                if (cfg.b_enabled and cfg.phase != "farm" and cyc < cyc_total
                        and self.stats.get("unlock_presses", 0) == spent_before):
                    print(f"[=] 本轮 B 没解锁任何一页（所有车可能都已点完）→ 停在循环 {cyc}/{cyc_total}")
                    break
        except AbortedByUser as exc:
            print(f"\n[急停] {exc}")
            # 写进日志：否则事后只剩一条 release_all 收尾，分不清"人停的"还是"自己停的"
            # （2026-10-02 那次换车跑完就是这种情况，只能靠猜）
            self.log.event("abort", reason=str(exc)[:200])
        except KeyboardInterrupt:
            print("\n[Ctrl+C] 手动中断")
            self.log.event("interrupt")
        except Exception as exc:
            # 【兜底】2026-10-02 血泪：car_tile_22b 漏进装配名单 → 扫描时 KeyError →
            # 程序带着浅显的 traceback 直接退出，日志里只剩一条 release_all 收尾，
            # 用户和我都只看到「不知道怎么卡住了」。所以这里兜住一切未预期异常：
            # 打 traceback + 存现场证据图 + 写一条 crash 事件（日志自己能回答"为什么停"）。
            import traceback
            print("\n[崩溃] 未预期异常 → 已停下并释放按键，原文如下：")
            traceback.print_exc()
            self.stats["stopped"] = f"crash:{type(exc).__name__}"
            self.stats["errors"] = self.stats.get("errors", 0) + 1
            shot = None
            try:
                shot = self._save_evidence(self.frame(guard=False), "crash")
                print(f"[崩溃] 现场证据图: {shot}")
            except Exception:
                pass
            self.log.event("crash", type=type(exc).__name__, msg=str(exc)[:300], shot=shot)
        finally:
            self.release_all("收尾")
            self._save_ledger()
        print("-" * 78)
        print(f"本次结果: {self.stats}")
        print(f"台账: {self.ledger}  → {self.cfg.ledger_path}")
        print(f"日志事件统计: {self.log.summary()}")
        return dict(self.stats)

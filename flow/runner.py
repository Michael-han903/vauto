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
from typing import Callable, Dict, Optional

import numpy as np

from vauto import (AbortedByUser, EmergencyStop, FocusGuard, Humanizer, InputSimulator,
                   TemplateMatcher, TimingProfile, WindowCapture, client_to_screen,
                   enable_dpi_awareness, find_window_by_title, load_calibration,
                   build_detectors, mean_abs_diff, scaled_frame, wait_stable)
from vauto.calib import calibration_table

from .config import RunConfig
from .nav import GridWalker

__all__ = ["Stack", "Runner", "build_stack", "build_offline_stack"]

# 需要的检测器（全部来自 templates/*.json 标定结果，运行期不手写阈值）
DETECTORS = (
    "hint_esc_retry", "panel_result",                       # A：结算
    "hint_unlock_all", "popup_no_resource", "popup_confirm",  # B：解锁 / 点数不足
    "page_title_mastery", "page_title_garage",              # 页面
    "tile_change_car", "tile_mastery",                      # 可点击磁贴
    "menu_select_title", "option_enter_car",                # 「选择操作」菜单 + 「上车」行
    "car_current_garage",                                   # 当前车辆（车库页）
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
        # 2) 按住 W
        self.hold(self.cfg.hold_key, "挑战进行中")
        # 3) 轮询结算判据（两个判据每帧都要喂，否则其中一个的连续帧计数会断）
        polls, settle_hit = 0, None
        last_frame, last_change = None, time.monotonic()
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
            # 卡死看门狗（只告警，不动作）
            if self.cfg.watchdog_idle > 0 and not self.cfg.replay:
                if last_frame is None or mean_abs_diff(last_frame, frame, gray=True) > 2.0:
                    last_frame, last_change = frame, time.monotonic()
                elif time.monotonic() - last_change > self.cfg.watchdog_idle:
                    print(f"  [!] 画面已 {self.cfg.watchdog_idle:.0f}s 无变化，可能卡住")
                    self.log.event("watchdog_idle")
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
        return "settled"

    # ---------------- B 阶段 ---------------- #
    def phase_spend(self, max_cars: int) -> dict:
        print(f"\n===== B 阶段：最多 {max_cars} 台车（换车 → 精通页 → Y+Enter） =====")
        self._ensure_vehicle_tab()
        for i in range(max_cars):
            self.s.stop.check()
            if not self._ensure_vehicle_tab():
                print("  [!] 到不了「车辆」页，结束 B 阶段")
                break
            if not self.change_car():
                print("  [!] 换车失败，结束 B 阶段")
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

    def _ensure_vehicle_tab(self) -> bool:
        """确保停在主菜单「车辆」标签页（用 tile_mastery 的出现来验证）。"""
        for attempt in range(3):
            frame = self.frame()
            if self.observe("tile_mastery", frame) is not None:
                return True
            if attempt == 0:
                print("  [B] 鼠标点「车辆」标签")
                self.click_client(self.cfg.tab_vehicle_click, "车辆 tab")
            else:
                self.press("esc", "退回主菜单")
            if self._wait_for("tile_mastery", self.cfg.page_timeout) is not None:
                return True
        return False

    def change_car(self) -> bool:
        """换到列表里的下一辆车：点「更换车辆」→ 走一格 → Enter → Enter。"""
        if not self.click_match("tile_change_car", note="点「更换车辆」"):
            return False
        # 等列表出现（用左上角"当前车辆"名条是否存在来判：车库列表页一定有它）
        if self._wait_for("car_current_garage", self.cfg.page_timeout) is None:
            print("  [!] 「更换车辆」列表没出现")
            return False
        # 走一格：每步都用"名条像素是否变化"验证
        self._nav_baseline = self._name_roi(self.frame())

        def _press_raw(key: str) -> None:
            self.s.stop.check()
            self.log.event("nav_press", key=key, dry=self.cfg.dry_run)
            if not self.cfg.dry_run:
                self.s.sim.tap_key(key)
                self.sleep(0.25)

        def _changed() -> bool:
            before = self._nav_baseline
            now = self._name_roi(self.frame())
            self._nav_baseline = now
            if before is None:
                return False
            return mean_abs_diff(before, now, gray=True) > 3.0

        walker = GridWalker(press=_press_raw, car_changed=_changed,
                            max_fail=self.cfg.nav_max_fail, budget=self.cfg.nav_budget)
        if not walker.step():
            print("  [!] 走不动了（可能已是列表最后一辆）")
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
            self._wait_for("tile_mastery", self.cfg.car_change_timeout)
        return True

    def unlock_current_car(self) -> str:
        """进精通页并解锁；返回 unlocked / already / no_points / no_page。"""
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
        """
        for name in ("car_current_menu", "car_current_garage"):
            hit = self._wait_for(name, 1.5)
            if hit is not None:
                print(f"  [校验] 当前车辆是 22B（{name}，{hit.score:.3f}）")
                self.log.event("car_ok", det=name, score=round(hit.score, 3))
                return True
        print("  [!] 没检测到当前车辆是 1998 斯巴鲁 Impreza 22B-STI")
        self.log.event("car_check_failed")
        return False

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
            if cfg.a_enabled:
                if cfg.require_car_22b and not self.check_car_22b():
                    print("[!] 当前车辆校验未通过：请手动把车换成 22B 再跑（或 --no-car-check）")
                    if not cfg.dry_run:
                        return {"stopped": "car_check_failed", **self.stats}
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

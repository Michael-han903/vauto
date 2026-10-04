# -*- coding: utf-8 -*-
"""运行期间阻止 Windows 息屏/睡眠 + 可选「电源计划托管」（长时间实跑的必需品）。

用户实测问题（2026-10-03）："运行时间久了电脑会自己黑屏然后不运行"。
用户追问（2026-10-04）："有没有什么办法能让这个程序在台式机黑屏之后依然运行？"

为什么会停：
  * **系统睡眠/休眠**：整台机子挂起 → 脚本和游戏一起停摆（"不运行"）；
  * **仅关显示器（DPMS）**：游戏可能被降频/暂停渲染 → 抓帧全黑 → 判据全废；
  * 某些驱动/策略会**忽略**一次性的电源请求 → 需要多道保险。

四道保险（全是电源管理 API，**不碰游戏进程**，符合工具层契约）：
  1) `SetThreadExecutionState(ES_CONTINUOUS|ES_DISPLAY_REQUIRED|ES_SYSTEM_REQUIRED)`
     —— 按线程生效：用常驻后台线程持有 + 周期续订（有些驱动会漏掉一次性设置）；
  2) `PowerSetRequest(PowerRequestDisplayRequired/SystemRequired)`
     —— Vista+ 的正式 API，与 1 相互独立，双保险；
  3) 可选**电源计划托管**（plan_guard）：运行期间把当前计划的
     关显示器 / 睡眠 / 休眠 / 硬盘超时改成"从不"，退出时**自动还原**；
     若程序崩溃没来得及还原：备份留在 logs/power_plan_backup.json，
     **下次启动自动还原**后重新托管（不残留系统改动）；
  4) 配合 runner 的「黑屏检测 + 轻推鼠标唤醒 + 恢复即续跑」（见 runner.frame）。

注意：SetThreadExecutionState 是**按线程**生效的（ES_CONTINUOUS 挂在线程上），
所以这里用一个常驻后台线程持有它。

配套（更保险，改系统设置，装完不用管的老办法）：
    powercfg /change standby-timeout-ac 0      # 永不睡眠
    powercfg /change monitor-timeout-ac 0      # 显示器永不关
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
import threading
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

# ---- PowerSetRequest 常量 ----
_PRC_VERSION = 0
_PRC_SIMPLE_STRING = 0x00000001
_REQ_DISPLAY = 0          # POWER_REQUEST_TYPE: DisplayRequired
_REQ_SYSTEM = 1           # SystemRequired


class _PowerRequestContext(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("Flags", ctypes.c_ulong),
                ("SimpleReasonString", ctypes.c_wchar_p)]


def _set(flags: int) -> bool:
    """调 SetThreadExecutionState（只影响当前线程的电源请求）。失败返回 False。"""
    try:
        k = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        k.SetThreadExecutionState.restype = ctypes.c_uint
        return bool(k.SetThreadExecutionState(ctypes.c_uint(flags)))
    except Exception:
        return False


# --------------------------------------------------------------------------
# 电源计划托管
# --------------------------------------------------------------------------
# 设置项 GUID（固定值，不随语言变化）
_SETTINGS = {
    #  键        子组 GUID                               设置 GUID
    "monitor":   ("7516b95f-f776-4464-8c53-06167f40cc99", "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"),
    "standby":   ("238c9fa8-0aad-41ed-83f4-97be242c8f20", "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"),
    "hibernate": ("238c9fa8-0aad-41ed-83f4-97be242c8f20", "9d7815a6-7ee4-497e-8888-515a05f02364"),
    "disk":      ("0012ee47-9041-4b5d-9b77-535fba8b1442", "6738e2c4-e8a5-4a42-b16a-e040e769756e"),
}
# 对应的 powercfg /change 前缀
_CHANGE = {"monitor": "monitor-timeout", "standby": "standby-timeout",
           "hibernate": "hibernate-timeout", "disk": "disk-timeout"}


def _run_powercfg(args: list[str]) -> str:
    """跑 powercfg 并**按系统 OEM 代码页解码**（中文 Windows 输出是 GBK ✗ 按 utf-8 读会花掉）。"""
    try:
        p = subprocess.run(["powercfg", *args], capture_output=True, timeout=20)
    except Exception:
        return ""
    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _query_index(kind: str, sub: str, guid: str):
    """查当前电源计划某设置项 AC/DC 的秒数（查不到返回 None）。中英文输出都认。"""
    out = _run_powercfg(["/query", "SCHEME_CURRENT", sub, guid])
    zh = "交流" if kind == "ac" else "直流"
    en = "AC" if kind == "ac" else "DC"
    for ln in out.splitlines():
        if "0x" in ln and (zh in ln or f"Current {en}" in ln or f" {en} " in ln):
            m = re.search(r"0x[0-9a-fA-F]+", ln)
            if m:
                return int(m.group(0), 16)
    return None


class PowerPlanGuard:
    """运行期间把电源计划改成"永不熄屏/不睡眠"，退出还原；崩溃有备份兜底。"""

    def __init__(self, backup_path: Path | str):
        self.backup_path = Path(backup_path)
        self.saved: dict | None = None
        self.guarded = False

    # ---- 对外 ----
    def start(self) -> str:
        """返回 off / guarded / unsupported。"""
        self._restore_leftover()                      # ① 先清掉上次崩溃的可能残留
        cur: dict = {}
        for key, (sub, guid) in _SETTINGS.items():
            cur[key] = {"ac": _query_index("ac", sub, guid),
                        "dc": _query_index("dc", sub, guid)}
        if all(v["ac"] is None and v["dc"] is None for v in cur.values()):
            return "unsupported"                      # 查不到 → 别乱改
        changed = False
        for key, pfx in _CHANGE.items():
            for ch in ("ac", "dc"):
                if cur[key][ch] is None:
                    continue
                _run_powercfg(["/change", f"{pfx}-{ch}", "0"])
                changed = True
        if not changed:
            return "unsupported"
        self.saved = cur
        self.guarded = True
        try:                                          # ② 备份（崩溃后下次启动可还原）
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            self.backup_path.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except Exception:
            pass
        return "guarded"

    def stop(self) -> None:
        if not self.guarded or not self.saved:
            return
        self._apply(self.saved)
        self.guarded = False
        try:
            if self.backup_path.is_file():
                self.backup_path.unlink()
        except Exception:
            pass

    # ---- 内部 ----
    @staticmethod
    def _apply(data: dict) -> None:
        for key, pfx in _CHANGE.items():
            for ch in ("ac", "dc"):
                sec = (data.get(key) or {}).get(ch)
                if sec is None:
                    continue
                mins = 0 if sec <= 0 else max(1, int(round(sec / 60.0)))
                _run_powercfg(["/change", f"{pfx}-{ch}", str(mins)])

    def _restore_leftover(self) -> None:
        try:
            if self.backup_path.is_file():
                self._apply(json.loads(self.backup_path.read_text(encoding="utf-8")))
                self.backup_path.unlink()
                print("  [i] 发现上次运行遗留的电源计划备份 → 已先还原 ✓")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 主类
# --------------------------------------------------------------------------
class KeepAwake:
    """持有"防息屏/防休眠"请求的后台组件。用法：k = KeepAwake(...); k.start(); …; k.stop()"""

    def __init__(self, interval: float = 30.0, strict: bool = True,
                 plan_guard: bool = False, backup_path: Path | str | None = None):
        self.interval = float(interval)
        self.strict = bool(strict)                    # 是否启用 PowerSetRequest 双保险
        self.active = False                           # 第 1 道（SetThreadExecutionState）是否生效
        self.power_ok = False                         # 第 2 道（PowerSetRequest）是否生效
        self.plan_status = "off"                      # 第 3 道：off / guarded / unsupported
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self._h = None                                # PowerCreateRequest 句柄
        self._plan = PowerPlanGuard(backup_path) if (plan_guard and backup_path) else None

    # ---- PowerSetRequest ----
    def _power_on(self) -> bool:
        if not self.strict:
            return False
        try:
            k = ctypes.windll.kernel32                 # type: ignore[attr-defined]
            k.PowerCreateRequest.argtypes = [ctypes.POINTER(_PowerRequestContext)]
            k.PowerCreateRequest.restype = ctypes.c_void_p
            k.PowerSetRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
            k.PowerSetRequest.restype = ctypes.c_int
            ctx = _PowerRequestContext(_PRC_VERSION, _PRC_SIMPLE_STRING, "vauto")
            h = k.PowerCreateRequest(ctypes.byref(ctx))
            if not h:
                return False
            ok1 = bool(k.PowerSetRequest(ctypes.c_void_p(h), _REQ_DISPLAY))
            ok2 = bool(k.PowerSetRequest(ctypes.c_void_p(h), _REQ_SYSTEM))
            self._h = h
            return ok1 or ok2
        except Exception:
            return False

    def _power_off(self) -> None:
        h, self._h = self._h, None
        if not h:
            return
        try:
            k = ctypes.windll.kernel32                 # type: ignore[attr-defined]
            k.PowerClearRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
            k.PowerClearRequest(ctypes.c_void_p(h), _REQ_DISPLAY)
            k.PowerClearRequest(ctypes.c_void_p(h), _REQ_SYSTEM)
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass

    # ---- 对外 ----
    def start(self) -> bool:
        if self.active:
            return True
        started = threading.Event()

        def _loop() -> None:
            # 在第 1 道请求必须由存活线程持有（ES_CONTINUOUS 挂在本线程上）
            ok = _set(_FLAGS)
            if ok:
                self.active = True
            started.set()
            while ok and not self._stop.wait(self.interval):
                _set(_FLAGS)                          # 周期续订，防被忽略
            _set(ES_CONTINUOUS)                       # 退出前清掉本线程的请求

        self._th = threading.Thread(target=_loop, daemon=True, name="vauto-keepawake")
        self._th.start()
        started.wait(2.0)
        self.power_ok = self._power_on()              # 第 2 道
        if self._plan is not None:                    # 第 3 道
            self.plan_status = self._plan.start()
        return self.active

    def stop(self) -> None:
        if self._plan is not None:
            self._plan.stop()                         # 先还原电源计划
        self._power_off()
        self._stop.set()
        if self._th is not None:
            self._th.join(1.5)
        self.active = False


def keep_awake(enable: bool = True, strict: bool = True, plan_guard: bool = False,
               backup_path: Path | str | None = None) -> KeepAwake:
    k = KeepAwake(strict=strict, plan_guard=plan_guard, backup_path=backup_path)
    if enable:
        k.start()
    return k

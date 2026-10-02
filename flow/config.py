# -*- coding: utf-8 -*-
"""
flow.config —— 运行期配置（业务层）

======================================================================
免责声明 / DISCLAIMER
----------------------------------------------------------------------
本代码仅用于算法学习（计算机视觉 / 输入仿真研究）。
若用于第三方软件，可能违反该软件用户许可协议（EULA）或服务条款，
并可能触发对方的风控 / 反作弊机制，存在账号被封禁等风险，请自行承担后果。
本层只做「看图 → 按键」的业务编排，不含内存读取、进程注入、DLL 注入、驱动加载、
网络通信、加解密、绕过检测等任何侵入式能力。请勿在其上添加此类功能。
======================================================================

所有"游戏侧事实"都集中在这里，改参数不用碰逻辑。
标注【实测】的值来自流程录屏 / 用户口述；标注【待核】的必须在首次在线 dry-run 里确认。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Tuple

__all__ = ["RunConfig", "load_config", "save_config"]


@dataclass
class RunConfig:
    # ---- 目标窗口 ----
    title_key: str = ""                 # 窗口标题关键字（空 = 启动时列出窗口让你选）
    hotkey: str = "f1"                  # 急停键

    # ---- A：挑战循环 ----
    hold_key: str = "w"                 # 【实测】挑战进行中只需一直按住 W
    retry_key: str = "esc"              # 【实测】结算画面 [Esc] 重试
    leave_event_key: str = "enter"      # 【实测】结算画面 [Enter] 继续（离开赛事）
    round_timeout: float = 840.0        # 【实测】一轮约 542 s，留到 14 分钟
    round_settle_before: float = 90.0   # 【实测】Esc 重试后要重新加载（用户口述约 1 分钟）→ 超时给足
    poll: float = 0.5                   # 轮询间隔
    max_polls_per_round: int = 0        # 0 = 由 round_timeout/poll 推算；回放时设小值
    ack_timeout: float = 8.0            # 按 Esc 后确认"已离开结算"的超时
    watchdog_idle: float = 180.0        # 画面连续多久没变就认为卡住（秒，0=关闭）

    # ---- B：刷技能点 ----
    tab_vehicle_click: Tuple[int, int] = (1390, 460)   # 【实测】主菜单「车辆」标签（鼠标可点）
    unlock_key: str = "y"               # 【实测】精通页 [Y] 解锁全部
    confirm_key: str = "enter"          # 【实测】确认框 / 关弹窗 / 上车
    max_cars_per_session: int = 8       # 一次 B 会话最多处理几台车（真正的退出是点数不足）
    nav_budget: int = 24                # 走格子步数预算（见 flow/nav.py）
    nav_max_fail: int = 3
    esc_dwell: float = 2.0             # 按 Esc 之后等画面切过去的时间（实测 ~1.5 s 才有变化）
    page_timeout: float = 20.0          # 等某个页面出现的超时（实测页内切换约 2 s，留足）
    # 【实测 2026-10-02 探针】点「上车」后会进入 13~18 秒「所有判据都不命中」的加载窗口
    # （无判据命中是**正常现象**，不是异常）。所以换车/加载的等待要 ≥ 60 s，兜底逻辑也不能
    # 把"没命中任何判据"当成"未知画面"来干预 —— 必须用"画面是否还在变化"来区分加载/卡死。
    car_change_timeout: float = 60.0    # 换车后等加载完成（含过场；实测 18.5 s 最长一次）

    # ---- 列表导航（「更换车辆」用）----
    # 【实测 2026-10-02】光标在列表里移动时，左上角「当前车辆」名条**完全不变**（它显示当前
    # 驾驶的车，不跟随光标）→ 换车验证必须看「列表区域有没有发生局部变化」。
    # 全局平均差对"挪一个高亮框"不敏感（0.6 量级），所以用分块最大差（被影响的那一块 20~60）。
    nav_watch_roi: Tuple[int, int, int, int] = (0, 400, 3840, 1520)   # 列表区域（含标签栏+车格）
    nav_change_blocks: Tuple[int, int] = (8, 6)                       # 分块数
    nav_change_threshold: float = 6.0                                 # 分块最大差 ≥ 此值 = 变了

    # ---- 走法开关 ----
    phase: str = "both"                 # farm / spend / both
    rounds: int = 4                     # farm 阶段跑几轮挑战
    cars: int = 6                       # spend 阶段最多几台车
    ledger_path: str = "logs/ledger.json"
    log_dir: str = "logs"

    # ---- 安全 ----
    dry_run: bool = True                # 默认只判不按：不 dry_run 就绝不动键鼠
    require_car_22b: bool = True        # A 之前校验当前车是 1998 斯巴鲁 Impreza 22B-STI
    max_runtime_min: float = 0.0        # 0 = 不限制总时长

    # ---- 回放（离线） ----
    replay: bool = False

    def __post_init__(self) -> None:
        self.tab_vehicle_click = tuple(int(v) for v in self.tab_vehicle_click)
        self.nav_watch_roi = tuple(int(v) for v in self.nav_watch_roi)
        self.nav_change_blocks = tuple(int(v) for v in self.nav_change_blocks)
        if self.phase not in ("farm", "spend", "both"):
            raise ValueError(f"phase 只能是 farm/spend/both，收到 {self.phase!r}")
        if self.poll <= 0:
            self.poll = 0.01
        if not self.max_polls_per_round:
            self.max_polls_per_round = int(self.round_timeout / self.poll) + 10

    # 常用派生值
    @property
    def a_enabled(self) -> bool:
        return self.phase in ("farm", "both")

    @property
    def b_enabled(self) -> bool:
        return self.phase in ("spend", "both")


def load_config(path: Optional[str | Path] = None) -> RunConfig:
    """从 JSON 读配置（缺的字段用默认值）。没有文件就返回默认配置。"""
    if not path:
        return RunConfig()
    p = Path(path)
    if not p.is_file():
        print(f"[config] 没有 {p}，使用默认配置")
        return RunConfig()
    data = json.loads(p.read_text(encoding="utf-8"))
    known = {f for f in RunConfig.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        print(f"[config] 忽略未知字段: {sorted(unknown)}")
    return RunConfig(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: RunConfig, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    return p

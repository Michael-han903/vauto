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
    round_settle_before: float = 90.0
    round_active_wait: float = 150.0    # 等"画面动起来"=比赛开始（加载画面是静止的，不能只等静止）   # 【实测】Esc 重试后要重新加载（用户口述约 1 分钟）→ 超时给足
    poll: float = 0.5                   # 轮询间隔
    max_polls_per_round: int = 0        # 0 = 由 round_timeout/poll 推算；回放时设小值
    ack_timeout: float = 8.0            # 按 Esc 后确认"已离开结算"的超时
    watchdog_idle: float = 180.0        # 画面连续多久没变就认为卡住（秒，0=关闭）

    # ---- 进赛事（2026-10-02 用户口述序列 + 7 张截图；见 docs/业务实测要点.md 第 6 节）----
    # 主菜单(剧情页) → 点「创意中心」标签 → Enter(EventLab) → ↓ → Enter(参加挑战)
    #   → Backspace(搜索面板) → ↑ → Enter(共享代码) → 输入代码 → Enter → ↓ → Enter(确认)
    #   → 等结果卡片 → Enter(进入挑战)
    # ---- 回 22B（B 之后回 A 的前提）----
    car_find_tries: int = 1200          # 找 22B 最多往右扫多少列（用户车库几千台车/三四百列以上，
                                        # 必须扫到"列表真的走不动"才认输；实际耗时由真实列数决定，
                                        # 不是由这个预算决定 —— 预算只是保险丝）
    car_scan_report: int = 25           # 每扫多少列在终端报一次进度
    list_search_roi: Tuple[int, int, int, int] = (760, 380, 3080, 1560)   # 车格列表区域
    brand_next_click: Tuple[int, int] = (3621, 354)   # 品牌栏「▶ 下一个品牌」（找 22B 的兜底手段）
    grid_origin: Tuple[int, int] = (800, 408)          # 「我的车辆」车格首格左上角（实机量）
    grid_pitch: Tuple[int, int] = (709, 522)           # 车格间距（实机量）—— 用于按格数走过去
    back_to_22b: bool = True            # B 阶段结束后自动把车换回 22B
    cycles: int = 1                     # 主循环次数：A 跑 N 轮 → B 解锁到「技能点不足」→ 换回 22B → 再来
    cars_cap: int = 40                  # --cars 0（解锁到不足）时的安全上限，防止无限换车
    car_load_wait: float = 20.0         # 上车加载等待（实测 13~18s，留余量）

    enter_event: bool = False                              # 跑 A 之前自动进赛事（--enter-event）
    tab_creativity_click: Tuple[int, int] = (2390, 476)   # 【实测】创意中心标签中心（坐标兜底；正常走 tab_creativity 模板匹配）
    brand_next_click: Tuple[int, int] = (3621, 354)   # 【实测 2026-10-03】品牌栏 ▶ 箭头（本品牌滚到头 → 点它翻下一个品牌）
    share_code: str = "161047605"                          # 【实测】挑战共享代码
    dialog_cancel_key: str = "enter"                      # 关掉挡路弹窗按哪个键。
                                                          # 【用户要求】不管高亮在哪一行都按回车；
                                                          # 注意「为挑战评分?」的高亮行会变（实测一次在
                                                          # 「取消」、一次在「点赞」），回车=确认高亮项。
                                                          # 想改成"取消"语义（与高亮无关）就填 "esc"。
    code_clear_backspaces: int = 12                        # 输入前先退格清空（防拼成两遍代码）
    search_timeout: float = 30.0                           # 等搜索结果卡片的超时
    entry_load_timeout: float = 150.0                      # 进赛事后的加载超时（实测约 1 分钟）

    # ---- B：刷技能点 ----
    tab_vehicle_click: Tuple[int, int] = (1495, 476)   # 【实测】车辆标签中心（坐标兜底；正常走 tab_vehicle 模板匹配）
    unlock_key: str = "y"               # 【实测】精通页 [Y] 解锁全部
    confirm_key: str = "enter"          # 【实测】确认框 / 关弹窗 / 上车
    max_cars_per_session: int = 8       # 一次 B 会话最多处理几台车（真正的退出是点数不足）
    nav_budget: int = 24                # 走格子步数预算（见 flow/nav.py）
    nav_max_fail: int = 3
    # 【2026-10-02 用户实测】"来来回回就两辆车在那里换" → 每走一步都用"选中格车名指纹"
    # 判断这台车弄过没有：弄过就继续走，不走重复的车（省下每次 68 秒的上车加载）。
    nav_repeat_limit: int = 6           # 连续这么多次都撞上"弄过的车" → 判定列表里没新车了
    # ---- ♥（收藏）驱动选车（2026-10-03 用户口径：点满的车他会加收藏，没♥=待处理）----
    fav_key: str = "down"               # 「选择操作」里「添加至收藏」是第 2 项 → 回车后按一下 ↓
    # ---- 鼠标归位（用户要求 2026-10-03）----
    # "每次用鼠标操作完之后都要把鼠标移动到最左上角，然后下次需要鼠标的时候再移动到所需位置"
    # 原因：鼠标停在控件上会改它的外观（悬停高亮/提示条），干扰靠画面做的判据。
    park_pointer: bool = True
    park_at: Tuple[int, int] = (3, 3)   # 客户区左上角（那一带是背景，没有可悬停的控件）
    # 【2026-10-03 实测】上车加载完之后落在主世界（自由驾驶）——画面一直在动，永远"不静止"，
    # 所以不能用 wait_stable 等它（原来等满 car_change_timeout=60 秒；日志 +61.3s）。
    # 现在只给这么点固定缓冲，剩下交给 _ensure_vehicle_tab 轮询（它按 Esc + 反复判页）。
    after_enter_car_wait: float = 6.0
    ensure_caps_lock: bool = True       # 开跑前检查大写锁定，关着就打开（用户要求）
    grid_area: Tuple[int, int, int, int] = (760, 400, 3800, 1900)   # 车格区域（左,上,右,下）
    tile_w_min: int = 560               # 车格宽度范围（实测 636~648，留余量）
    tile_w_max: int = 800
    tile_h_min: int = 400               # 车格高度范围（实测 468~488）
    tile_h_max: int = 600
    heart_off: Tuple[int, int] = (560, 340)   # 车格内 ♥ 搜索区偏移（实测 ♥ 在 +(606,378)）
    heart_roi: Tuple[int, int] = (110, 80)    # ♥ 搜索区大小
    # 【2026-10-03 用户指路 + 实机图实测】当前驾驶的车辆被收藏时，♥ 画在
    # **驾驶图标（方向盘）的左边** —— 标准位被方向盘占了（读数 0.271~0.78 噪声）。
    # 这里再查一个「左侧位」（x, y, w, h，相对车格角点）：实测 当前车 0.995 /
    # 普通车 0.778~0.804 → 两处取最大值，>= 阈值就算已收藏。
    heart_roi2: Tuple[int, int, int, int] = (540, 370, 70, 60)
    # 【2026-10-03】「驾驶中」小图标的搜索区（x, y, w, h，相对车格角点）——
    # 用它判「这格是不是当前驾驶的车」（取代已作废的「第一列=当前车」位置规则）。
    drive_badge_roi: Tuple[int, int, int, int] = (556, 340, 100, 100)
    grid_walk_max: int = 30             # 走到目标车格最多按几次方向键
    grid_walk_dwell: float = 0.45       # 每次方向键后的等待（够黄框重画）
    grid_walk_keys: Tuple[str, ...] = ("down", "right")   # 试键顺序（闭环验证，不依赖语义假设）
    fp_same_tol: float = 20.0           # 车名指纹差异 < 此值 = 同一台车（对齐后同车≈0~5、不同车≈40+）
    fp_align_px: int = 4                # 比对指纹前在 ±这么多像素内找最佳对齐（文字对错位极敏感）
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
        self.grid_origin = tuple(int(v) for v in self.grid_origin)
        self.grid_pitch = tuple(int(v) for v in self.grid_pitch)
        self.grid_area = tuple(int(v) for v in self.grid_area)
        self.heart_off = tuple(int(v) for v in self.heart_off)
        self.heart_roi = tuple(int(v) for v in self.heart_roi)
        self.heart_roi2 = tuple(int(v) for v in self.heart_roi2)
        self.grid_walk_keys = tuple(str(v) for v in self.grid_walk_keys)
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

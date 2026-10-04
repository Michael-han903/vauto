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
    round_timeout: float = 840.0        # 单轮超时（秒）。若 round_minutes>0 会由它自动推导
    round_minutes: float = 8.0          # 【可调】挑战标称时长（分钟）——用来算单轮超时 + 界面估时
                                        # （实测一轮全程约 542s：8 分钟挑战 + 加载/结算 ≈ 62s）
    round_settle_before: float = 90.0
    round_active_wait: float = 150.0    # 等"画面动起来"=比赛开始（加载画面是静止的，不能只等静止）   # 【实测】Esc 重试后要重新加载（用户口述约 1 分钟）→ 超时给足
    poll: float = 0.5                   # 轮询间隔
    max_polls_per_round: int = 0        # 0 = 由 round_timeout/poll 推算；回放时设小值
    ack_timeout: float = 8.0            # 按 Esc 后确认"已离开结算"的超时
    hint_wait: float = 6.0              # 进精通页后等「[Y] 解锁全部加成」出现的窗口（秒）：
                                        # 太短会把"提示还没渲染出来"误判成"本页已解锁过" ✗
                                        # （2026-10-03 用户实测"没全解锁的车被漏掉"的根因之一）
    watchdog_idle: float = 180.0        # 画面连续多久没变就认为卡住（秒，0=关闭）
    race_rehold_after: float = 12.0     # 【2026-10-04】比赛里画面这么久没变 → 重新按一次 W
    #   （失焦/暂停会把按键状态丢掉，而程序以为自己还按着；0=关。每轮最多补按 3 次）

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
    # 【2026-10-03 用户实测】刷完挑战**不要**再换回 22B：
    # 用户原话："四轮完成之后不应该去车库里面加点吗？怎么跳转到斯巴鲁上车流程了？"
    # 根因：此时画面还在赛事结算/加载里（不在车库），"在 22B 就跳过"的检查看不到当前车
    # → set_car_22b 退化成**全库扫描**（≈2 分钟白跑）。而挑战过程不会换车（车还是 22B），
    # 且下一轮循环进赛事前会再确保一次 → 这一步是纯浪费 → 默认关。
    back_to_22b: bool = False           # 【默认关】刷完挑战是否再"换回 22B"（见上）
    # 【2026-10-03】开跑前的"画面验货"：必须与标定分辨率一致（w, h）——
    # 抓到别的尺寸（比如游戏重启时的小窗口）说明抓错窗口，判据全部失效 → 拒绝动作。
    frame_expect: Tuple[int, int] = (3840, 2160)
    cycles: int = 1                     # 主循环次数；**0 或负数 = 一直循环**（用户 2026-10-03：
                                        # "全自动应该……循环往复才对"）——靠 F1/「停止」结束
                                        # 【注意】库默认必须是 1 ✗：回放/自检/库里别的调用都
                                        # 用默认值构造，默认 0 会让它们**无限循环**挂死 ✗
                                        # （2026-10-03 踩过：selftest 的 A 阶段跑不完）。
                                        # GUI/CLI 的默认值另行显式给 0（用户要的"一直循环"）。
    farm_first: bool = False            # 全自动开局先去刷挑战（刷完 N 轮再回车库花）。
                                        # False（默认）= 先花后刷，花不动才去刷 —— 你之前的口径
    # 【2026-10-03 用户口径】"不应该车库全翻一遍吗？" —— --cars 0 时的**正常**结束条件：
    #   ① 撞到「技能点不足」→ 去刷（主循环切 22B）；
    #   ② 整个车辆列表翻完、绕回开头且没有「没♥且没弄过」的车 → 正常收尾。
    # 原来的 40 太小（"点还够、没♥的车还很多"时会**提前收工** ✗）；1000 对上千辆的车库
    # 也还是紧（2026-10-04 用户问"为什么还是 1000"）。
    # 定论：它只是**防死循环保险丝**，正常结束永远不看它（①点数不足 ②扫完绕回开头 ✓）。
    # 给到"实际不可能跑到"的量级；真打到它 = 异常 → 醒目告警 + 记 cars_cap_hit。
    cars_cap: int = 100000
    car_load_wait: float = 20.0         # 上车加载等待（实测 13~18s，留余量）

    unfav_cap: int = 100000             # 「只取消收藏」阶段的台数上限（车库几千辆 → 给足；
                                        # F1 随时能停。与 B 阶段的 cars_cap 各管各的）
    use_manufacturer_panel: bool = True  # 【2026-10-03 用户要求】找 22B 时优先用
                                          # 「制造商」面板过滤（智能找车厂、不记坐标）；
                                          # 认不出面板就自动退回逐列扫描
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
    nav_budget: int = 400                # 走格子步数预算（见 flow/nav.py）
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
    # ---- 焦点进出（2026-10-04 用户要求）----
    # "我有的时候会把屏幕焦点切出去做一些别的……当我切出去的时候自动解除大写锁定，
    #  切回来的时候再自动开启，并且检测鼠标位置，把鼠标归位，关闭程序的时候也关掉 caps"
    caps_lock_follow_focus: bool = True   # 切出游戏窗口 → 自动关 CapsLock；切回来 → 自动开回
    event_menu_resume_key: str = "esc"    # 「重新开始赛事 / 退出赛事」这个**比赛菜单**里，按哪个键
                                          # 回到比赛。实测底部提示条写的是 "Esc 返回"；
                                          # 千万别按回车 —— 那会选中高亮的那一项（可能是退出赛事）
    event_menu_auto_resume: bool = True   # 比赛进行中一旦认到这个菜单 → 自动按上面那个键回比赛
                                          # （用户在比赛里切出去再回来就会看到它，挑战计时也会停）
    caps_off_on_exit: bool = True         # 退出程序（F1/停止/关窗口）→ 把 CapsLock 关掉
    park_on_focus_return: bool = True     # 切回来先检测/归位鼠标（悬停会改控件外观、干扰判据）
    focus_resync: bool = True             # 切回来重新判一次"我在哪一屏"，只清掉确实挡路的
                                          # 弹窗/菜单（绝不乱按回车）
    # ---- 挂机防睡（2026-10-04 用户口径：屏幕允许变黑，程序必须继续跑）----
    keep_awake: bool = True             # 运行期间防「系统睡眠」（双 API 保险）。
                                        # 注意：默认**不拦截熄屏** —— 15 分钟没操作照常黑屏
    keep_display_on: bool = False       # True=运行期间还要求屏幕常亮（阻止变黑）。
                                        # 默认 False：晚上挂机就让它黑，机器照跑
    power_plan_guard: bool = True       # 再上一道：运行期间把「睡眠/休眠/硬盘超时」改成
                                        # "从不"（**不碰熄屏时间**）；退出自动还原，
                                        # 崩溃留下的改动下次启动自动还原
    black_wake_nudge: bool = False      # 黑屏时是否每 ~20s 轻推 2px 鼠标唤醒屏幕。
                                        # 默认 False（用户要黑屏挂机）；想主动唤醒再开
    black_wait_max: float = 0.0         # 抓帧真全黑时最多等多少秒，0=无限等（挂机推荐）
                                        # 等待期间不释放已按住的键；F1 随时可停
    grid_area: Tuple[int, int, int, int] = (760, 400, 3800, 1900)   # 车格区域（左,上,右,下）
    tile_w_min: int = 560               # 车格宽度范围（实测 636~648，留余量）
    tile_w_max: int = 800
    tile_h_min: int = 400               # 车格高度范围（实测 468~488）
    tile_h_max: int = 600
    heart_off: Tuple[int, int] = (583, 355)   # 车格内 ♥ 搜索区**左上角**（2026-10-03 收紧：
                                              # 原来 (560,340) 框太大，车身/轮胎的深色形状
                                              # 会被 0.94 的虚高分骗进 ROI → 误判"已收藏"）
    heart_roi: Tuple[int, int] = (76, 76)     # ♥ 搜索区大小（模板 60x60 带白底上下文，±8px）
    # 【2026-10-03 用户指路 + 实机图实测】当前驾驶的车辆被收藏时，♥ 画在
    # **驾驶图标（方向盘）的左边** —— 标准位被方向盘占了（读数 0.271~0.78 噪声）。
    # 这里再查一个「左侧位」（x, y, w, h，相对车格角点）：实测 当前车 0.995 /
    # 普通车 0.778~0.804 → 两处取最大值，>= 阈值就算已收藏。
    heart_roi2: Tuple[int, int, int, int] = (504, 340, 80, 80)
    # 【2026-10-03】「驾驶中」小图标的搜索区（x, y, w, h，相对车格角点）——
    # 用它判「这格是不是当前驾驶的车」（取代已作废的「第一列=当前车」位置规则）。
    drive_badge_roi: Tuple[int, int, int, int] = (556, 340, 100, 100)
    grid_walk_max: int = 30             # 走到目标车格最多按几次方向键
    grid_walk_dwell: float = 0.45       # 每次方向键后的等待（够黄框重画）
    # 【2026-10-04】"翻列表"（按 → 换一列）之后的等待，与走路分开：
    # 走路那 0.45s 要等黄框重画；翻列只是换视图，实测往往很快 —— 但它现在是整条
    # 扫描链路里**最大的一块时间**（每屏 ≈0.6s 等待 + 0.15s 抓帧 + 0.03s 判据）。
    # 默认保持 0.45（= 现有行为，不冒险）；想提速就调小，用 @grid_advance 的 diff 验证：
    # 只要每屏 diff 仍 ≥ nav_change_threshold（默认 6.0），说明这一屏确实已经滚到位了。
    grid_scroll_dwell: float = 0.45
    grid_walk_keys: Tuple[str, ...] = ("down", "right")   # 试键顺序（闭环验证，不依赖语义假设）
    # ---- 选车/判页（2026-10-04 提质：日志统计驱动的修正）----
    use_mouse_select: bool = False      # 用鼠标点车格来选车。**实测命中率 ~7%**
                                        # （日志：成功 6 次 / 没选中 87 次），而每次失败要白付
                                        # ~2.5 秒再退回方向键走路 → 默认关，直接走方向键。
                                        # 想试鼠标（也许换了界面/分辨率就能点中）再打开。
    walk_confirm_heart: bool = True     # 到站后**再抓一帧**复核"这格确实没♥"。
                                        # 实测同一格的 ♥ 读数会在帧之间从 1.000 掉到 0.554
                                        # → 单帧判定会把有♥的车当成待处理（上错车）。
    tab_ensure_budget: float = 45.0     # 「确保停在主菜单车辆页」的总时间预算（秒）。
                                        # 原来是固定试 10 次（≈25s），上车加载 13~18s + 慢盘
                                        # 就不够 → 日志里 6/57 次运行卡在这。
    brand_jump_max: int = 6             # 本品牌滚到头后最多点几次「▶ 下一个品牌」（每轮重置）。
    auto_recover: bool = True           # 卡在"认不出的界面"时自动回到已知状态（Esc / 清弹窗）
    recover_rounds: int = 4             # 自愈最多按几轮 Esc
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
        if self.phase not in ("farm", "spend", "both", "unfav"):
            raise ValueError(f"phase 只能是 farm/spend/both，收到 {self.phase!r}")
        if self.poll <= 0:
            self.poll = 0.01
        if self.round_minutes and self.round_minutes > 0:
            # 单轮超时 = 挑战时长 + 5 分钟缓冲（加载/结算/评分弹窗都算在里面）
            self.round_timeout = float(self.round_minutes) * 60.0 + 300.0
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

# vauto —— 通用 GUI 视觉自动化「工具层」原型

> **免责声明**：本原型仅用于算法学习（计算机视觉 / 输入仿真研究）。
> 若用于第三方软件，可能违反该软件的用户许可协议（EULA）或服务条款，
> 并可能触发对方风控 / 反作弊机制，**存在账号被封禁等风险**，请自行承担后果。
> 本工具层刻意不包含内存读取、进程注入、DLL 注入、驱动加载、网络通信等侵入式能力，也不应添加。

## 1. 目录结构

```
visual_auto_toolkit/
├─ vauto/
│  ├─ __init__.py     统一导出（PEP 562 惰性加载）
│  ├─ capture.py      mss 窗口抓帧 + win32gui 窗口矩形/客户区坐标 + PrintWindow 备选
│  ├─ matching.py     OpenCV 模板匹配（多尺度 + NMS + alpha mask + 中文路径）
│  ├─ vision.py       识别层组合件：ROI + 降采样 + 滞回去抖(VisualDetector) + 画面变化检测/dHash + wait_stable
│  ├─ calib.py        读标定产物（thresholds.json + manual_thresholds.json）并装配检测器
│  ├─ timing.py       随机延时 / 贝塞尔轨迹 / 非线性时间轴 / 可中断 sleep（纯函数可单测）
│  ├─ input_sim.py    pynput 鼠标键盘仿真（抖动点击、贝塞尔移动、粘键兜底）
│  ├─ focus.py        win32gui 前台焦点检测与「非前台即暂停」守卫
│  ├─ safety.py       F1 全局急停 + AbortedByUser
│  └─ recorder.py     帧录制器（素材工作流：采集 golden_frames 原始帧）
├─ flow/              业务层（刻意与工具层隔离，唯一会真按键的地方）
│  ├─ config.py       运行期配置（所有"游戏侧事实"集中在此）
│  ├─ nav.py          「更换车辆」列表的走格子策略（列优先 + 列到底跳列）
│  └─ runner.py       A/B 状态机：A=按住W打挑战+Esc重试；B=换车+精通页Y解锁
├─ templates/         模板 + thresholds.json（标定产物）/ manual_thresholds.json（手工推定）
├─ golden_frames/     素材：各场景原始帧（不入 git，体积大）
├─ docs/              业务流程设计说明书 / 业务实测要点 / 流程录屏时间线 / 标定报告 / 运行手册
├─ run_vauto.py       ★ 业务运行入口（--dry-run / --live / --phase farm|spend）
├─ demo_skeleton.py   调用骨架（业务逻辑全是 TODO 占位）
├─ record_scenes.py   引导式素材录制（只截图，不模拟输入）
├─ record_gui.py      素材录制图形界面
├─ template_crop_gui.py 模板裁剪器（从 golden_frames 框选元素）
├─ calibrate.py       模板标定：两段式（粗搜 ROI → ROI 内精确），产出 thresholds.json
├─ offline_replay.py  离线回放：A 循环跑在录屏帧上（不碰屏幕与键鼠）
├─ selftest_*.py      自检（timing / stack / vision / flow）
├─ requirements.txt
└─ README.md
```

## 2. 安装

```bash
pip install -r requirements.txt
```

依赖清单：`mss` `numpy` `opencv-python` `pywin32` `pynput`
（可选：`pyinstaller` 打包）

实测环境：Windows 11 + Python 3.14.2 + numpy 2.5.3 + opencv-python 5.0.0.93 + mss 10.2.0 + pywin32 312 + pynput 1.8.2（全部功能自检通过）。

pywin32 安装后若报 DLL 加载失败，以管理员身份执行一次：
`python Scripts/pywin32_postinstall.py -install`

依赖是按需导入的（`vauto/__init__.py` 用 PEP 562 惰性加载）：
只 `import vauto.timing` 不需要装 pywin32 / 摄像头相关依赖；缺哪个依赖只影响用到它的模块，
并且会打印 `pip install xxx` 的提示（也可主动调用 `vauto.check_dependencies()`）。

## 3. 最小用法

```python
from vauto import (enable_dpi_awareness, WindowCapture, TemplateMatcher,
                   Humanizer, InputSimulator, EmergencyStop, FocusGuard,
                   client_to_screen, find_window_by_title)

enable_dpi_awareness()                                    # 必须在一切之前

hwnd    = find_window_by_title("目标窗口标题关键字")
capture = WindowCapture(hwnd, client_only=True)
matcher = TemplateMatcher(grayscale=True, threshold=0.87)
human   = Humanizer(seed=None)                           # 传 seed 可复现
sim     = InputSimulator(humanizer=human)
stop    = EmergencyStop("f1", on_trigger=sim.release_all) # F1 急停 + 释放粘键
sim.emergency = stop                                     # 让每个动作都检查急停
guard   = FocusGuard(hwnd, stop_event=stop.event)        # 非前台即暂停

with stop:
    i = 0
    while not stop.triggered:
        if not guard.wait():        # 后台时在这里阻塞，不会盲点
            break
        frame = capture.grab()                              # BGR ndarray
        hit = matcher.match_best(frame, "templates/btn.png") # 客户区坐标系
        if hit is None:
            human.delay(0.2, 0.5, stop_event=stop.event)
            continue
        # TODO: implement your own business logic here
        sim.click(client_to_screen(hwnd, *hit.center))       # 自动贝塞尔 + 抖动
        human.maybe_rest(i, stop_event=stop.event)           # 每 N 次循环随机休息
        i += 1
```

命令行骨架：

```bash
python demo_skeleton.py --list                  # 列出窗口找 hwnd
python demo_skeleton.py --title "记事本" --dry-run   # 只抓帧+匹配，存调试图（先跑这个）
python demo_skeleton.py --title "记事本" --template templates/btn.png --threshold 0.88
```
运行中**随时按 F1 立即中止**。

## 4. 模板图片怎么用（重点）

**制作**：从 `WindowCapture.grab()` 抓到的帧里裁剪，保证模板与实时帧同源。

```python
frame = WindowCapture(hwnd, client_only=True).grab()
cv2.imwrite("templates/btn.png", frame[220:250, 400:470])   # 注意是 [y1:y2, x1:x2]
```
也可以先用 `--dry-run` 跑一遍，看输出的调试图（`debug_frames/*.png`）再决定裁哪块。

要点：

| 事项 | 建议 |
|---|---|
| 尺寸 | 20×20 ~ 200×200 像素；太小易误匹配，太大则对遮挡/动画敏感 |
| 内容 | 选**独特、有纹理**的区域（图标、文字局部）；避免大面积纯色、渐变、重复图案 |
| 稳定性 | 不要包含会变的部分：数字、时间、进度条、动画、鼠标悬停高亮 |
| 格式 | 存 **PNG**（无损）。JPEG 压缩噪声会明显拉低匹配分数 |
| 缩放 | 模板必须与抓帧的 DPI/分辨率一致，否则用多尺度 `scales=(0.95, 1.0, 1.05)` |
| 透明 | 模板带 alpha 通道时会自动当 mask 用（此时算法自动切到 `TM_CCORR_NORMED`） |
| 中文路径 | 读用 `load_image()`、写用 `save_image()`（`np.fromfile`/`imencode+tofile`），支持中文/Unicode 路径；直接用 `cv2.imread/imwrite` 在中文路径上会**静默失败** |
| 阈值 | 一般 0.85 ~ 0.92。先 `--dry-run` 打印实际 score，再按实测值上下浮动 |
| 纯色模板 | `TM_CCOEFF_NORMED` 在纯色模板上分母为 0，OpenCV 会返回无意义的高分图，本库会直接抛 `ValueError` 提示；确需匹配纯色请用 `TemplateMatcher(method=cv2.TM_SQDIFF_NORMED, ascending=True)` 或给模板加 alpha mask |
| 多目标 | `matcher.match(...)` 返回全部命中（含 NMS 去重），`match_best` 只取最高分 |

**坐标换算**：匹配结果坐标属于「抓帧图像坐标系」。
若 `client_only=True`（推荐），即窗口客户区坐标，点击前必须：
`client_to_screen(hwnd, *hit.center)` → 得到屏幕物理像素坐标 → 交给 `InputSimulator`。
若用 `region=(x,y,w,h)` 限定搜索区域，返回坐标**已自动加回**该区域偏移，仍是整帧坐标。

**素材工作流第一步：录制原始帧**（`--record`，不做任何输入模拟）

```bash
python demo_skeleton.py --list
python demo_skeleton.py --title "窗口标题关键字" --record 场景名 --frames 30 --rec-interval 0.3,0.8
```

在目标窗口的**每个关键画面**（结算面板、进行中、弹窗、列表页、子页面、节点两态）各录
30~60 帧，输出到 `golden_frames/<场景名>/`。这些帧是后续「裁模板 → 标定阈值 → 离线回放」
的唯一素材来源，必须与运行时抓帧同分辨率、同画质（同一次会话、不缩放窗口）。录制中按 F1 可提前停止。

**图形化版本**（不用记命令）：

```bash
py -3.14 record_gui.py        # 下拉选窗口 -> 预览画面确认 -> 选中场景 -> 录制
```

预览、场景清单、进度、停止按钮都在窗口里；只截图，不做任何输入模拟。

## 5. 人类化行为都在哪

| 需求 | 实现位置 |
|---|---|
| 随机延时（收 min/max 秒） | `Humanizer.delay(min, max, stop_event=)` |
| 像素随机偏移点击 | `InputSimulator.click()` → `Humanizer.jitter_point()`（圆内均匀采样） |
| 非线性贝塞尔轨迹（拒绝匀速直线） | `timing.bezier_path()` + `bezier_path` 控制点法向随机偏移（有下限 1.5px / 1.5% 距离）；时间轴 `timing.ease_curve()` 随机 ease + 逐段 ±12% 抖动；`InputSimulator.move_bezier()` 还带概率性过冲回拉 |
| 按键按下/释放 + 时长扰动 | `InputSimulator.tap_key()/key_down()/key_up()/press_hotkey()`，时长取 `profile.key_hold` 区间随机值 |
| 循环间隙随机休息 | `Humanizer.maybe_rest(iteration)`：每 `rest_every_n=25` 次按 `rest_prob=0.5` 触发 0.6~2.2s 暂停 |
| F1 急停 + 释放所有按键 | `EmergencyStop`（pynput 全局低级钩子）→ 置位 `event` → 回调 `InputSimulator.release_all()`；所有动作前 `_check()` 抛 `AbortedByUser`，所有 sleep 走 `interruptible_sleep` 立即返回 |
| 焦点检测（非前台即暂停） | `focus.is_foreground(hwnd, deep=True)` + `FocusGuard.wait()`；后台期间阻塞，不用 CPU 空转 |

想换「手速风格」只改 `TimingProfile`（一个 dataclass，全区间参数），或传 `Humanizer(seed=42)` 复现实验。

## 6. 已知限制（务必知道）

1. **最小化抓不到帧**：mss 抓的是屏幕上真实像素。窗口被遮挡会抓到遮挡物，最小化直接抛 `WindowUnavailable`。
   备选 `capture.grab_printwindow()`（可抓被遮挡窗口），但 GPU/DirectX 渲染的窗口常返回全黑，需实测。
2. **管理员权限**：若目标进程以管理员运行或使用独占全屏 / Raw Input，普通权限的键盘钩子和 `SendInput` 可能失效，需以管理员运行脚本。
3. **DPI**：必须在任何抓帧/点击前调用一次 `enable_dpi_awareness()`，否则 125%/150% 缩放下坐标会整体偏移。
4. **前台锁定**：`bring_to_front()` 是尽力而为，Windows 会拒绝非前台进程的 `SetForegroundWindow`。
5. **无窗口渲染**：DirectX/Vulkan 独占全屏内容通常抓不到（需要 Desktop Duplication API，超出本原型范围）。
6. 本工具层不提供、也不应添加任何绕过风控/检测的手段。

## 7. 自检脚本（可复现的验证）

| 脚本 | 覆盖内容 | 是否注入真实输入 |
|---|---|---|
| `selftest_timing.py` | 贝塞尔非直线（最小法向偏移）、非匀速（峰值速度/均速 > 1.5）、端点精确、时间轴单调、时长守恒、像素抖动半径、每 N 轮休息、急停打断 sleep | 否（只需 numpy） |
| `selftest_stack.py` | 真实全屏/窗口抓帧、通道顺序、模板匹配定位精度（±1px）、多尺度、alpha mask、中文路径、NMS、region 坐标还原、窗口枚举/前台判定/焦点守卫、按键状态机与粘键兜底、F1 急停全链路 | 否（用桩 Controller 替换底层，**不会动你的鼠标键盘**） |
| `selftest_vision.py` | 坐标往返、`VisualDetector` 滞回（连续 2 帧确认/释放）、ROI 与全帧结果一致（≤3px）、错 ROI 不命中、0.5 尺度与 1.0 尺度位置一致（≤4px）、dHash/灰度差变化检测、`wait_stable` 稳定与超时 | 否（用 StubCapture 回放 golden_frames） |
| `e2e_demo_test.py` | 端到端跑 `demo_skeleton.py`：抓帧 → 生成模板 → dry-run 匹配 → 调试图 → 优雅退出 | 否（全程 `--dry-run`） |

```bash
python selftest_timing.py
python selftest_stack.py
python selftest_vision.py
python selftest_flow.py
python offline_replay.py
python e2e_demo_test.py
```

## 7.6 怎么真正跑起来

见 `docs/运行手册.md`。最短路径：

```bash
py -3.14 run_vauto.py --list                                              # 找窗口
py -3.14 run_vauto.py --title "Forza Horizon 6" --phase farm --rounds 1   # 只看不按（默认 dry-run）
py -3.14 run_vauto.py --title "Forza Horizon 6" --phase farm --rounds 1 --live   # 真按键（F1 急停）
```

阶段说明：`--phase farm` = 按住 W 打挑战 + Esc 重试；`--phase spend` = 车库逐辆换车 + 精通页
`Y`/`Enter` 解锁，直到弹出「不够支付全部」；`--phase both` = 先 farm 再 spend。
**注意 spend 会把当前车辆换成别的车，再跑 farm 之前要人工把车换回 1998 斯巴鲁 Impreza 22B-STI。**
```

## 7.5 标定与识别层（运行期怎么用）

模板标定产出 `templates/thresholds.json`，里面每张模板都有：阈值、滞回用的退出阈值、
搜索区域 ROI、尺度、去抖帧数、检出率/误报率、ROI 内一次匹配耗时。运行期不要手写阈值。

```bash
py -3.14 calibrate.py --report docs/标定报告.md     # 两段式：0.5 粗搜 ROI → ROI 内全分辨率精确标定
```

要点（均为本机实测，客户区 3840x2160）：

| 事项 | 数据 |
|---|---|
| 全帧匹配 vs ROI 匹配 | 340~670 ms vs **7~17 ms**（约 50 倍） |
| 标定耗时 | 旧脚本 13 张模板 7 分钟跑不完 → 现在 14 张 **35 秒** |
| 阈值与尺度 | **绑定**。同一模板 1.0 尺度 1.000、0.5 尺度 0.879，换尺度必须重新标定 |
| 降采样 | 帧与模板必须**同时**缩放；只缩帧不缩模板会让分数从 1.000 崩到 0.34 |
| 固定位置元素 | 用 ROI 就够，不必降采样（保真且更快） |
| 画面变化检测 | dHash：4K 彩帧约 15 ms / 1080p 灰度约 2 ms（耗时几乎全在缩小到 64x64） |

```python
from vauto import TemplateMatcher, VisualDetector, load_image, wait_stable
import json
cal = json.load(open("templates/thresholds.json", encoding="utf-8"))
c = cal["templates"]["hint_unlock_all"]          # 示例：精通页是否还有可解锁
det = VisualDetector(TemplateMatcher(), "templates/hint_unlock_all.png",
                     roi=tuple(c["roi"]), scale=cal["scale"],
                     threshold=c["threshold"], confirm_frames=2, name="有可解锁")
hit = det.observe(frame)        # 连续 2 帧命中才返回 Match（全分辨率坐标），否则 None
```

## 8. 安全提示

- 第一次运行一律先 `--dry-run`，确认匹配坐标无误再开启输入仿真。
- 建议先把鼠标移到屏幕角落、或准备好在任务管理器结束进程，作为 F1 之外的第二道保险。
- `--max-loops N` 可让骨架跑 N 轮后自动退出，适合试跑。
- 只在你自己拥有/已获授权、且许可协议允许自动化的软件上使用。

# 全屏程序让位（隐藏 dock + 短暂注销 AppBar）

## 需求

> 有程序（**非系统程序**）全屏显示时，隐藏 dock 栏并短暂注销 AppBar；
> 全屏程序解除时，恢复 AppBar 并重新显示 dock 栏。

dock 通过 AppBar 在屏幕底部保留了一块工作区（见 `core/sys32.py`），最大化窗口会
自动避开 dock。但全屏窗口应当独占整块屏幕 —— 只要保留区还在，系统就不会把整块屏
幕交给它。所以全屏时必须**注销 AppBar**，退出后再**装回去**。

## 状态机

```
                    ┌──────────────────────────────────────────────┐
                    │ 正常态：AppBar 已注册，dock 可见并置顶          │
                    └──────────────────────────────────────────────┘
                          │                            ▲
   连续 enter_confirm 轮  │                            │ 连续 exit_confirm 轮
   命中「非系统程序全屏」   │                            │ 未命中
                          ▼                            │
                    ┌──────────────────────────────────────────────┐
                    │ 让位态：AppBar 已注销（工作区还给系统）          │
                    │        dock 窗口隐藏、提示条收起                │
                    └──────────────────────────────────────────────┘
```

* `enter_confirm` 默认 **1**：全屏一出现就让位，dock 不在画面上多停一拍。
* `exit_confirm` 默认 **2**：连续两轮（≈800ms）没检测到全屏才恢复，扛住 Alt+Tab、
  全屏程序弹系统对话框这类瞬时抖动 —— 否则 AppBar 反复注册/注销，整个桌面的工作区
  来回拉扯。
* 只有**状态真正翻转**才发信号，稳态下不动 AppBar。

## 判定规则：什么算「非系统程序全屏显示」

实现见 `core/fullscreen_watch.py`。一个窗口要**同时**满足五条才算数：

| # | 条件 | 为什么 |
| - | ---- | ------ |
| 1 | 是**前台窗口**，或前台窗口的 root owner | 「用户正在看的东西」才该让位；多显示器下用户在副屏工作时，主屏挂着的全屏游戏不该把 dock 一起藏掉。root owner 覆盖全屏程序弹出的对话框/菜单。 |
| 2 | 可见、未最小化、不是工具窗口（`WS_EX_TOOLWINDOW`） | 输入法提示、悬浮工具栏铺满屏幕也不代表"有程序全屏"。 |
| 3 | 窗口矩形完整覆盖**它所在显示器**的 `rcMonitor`（容差默认 2px） | 真正的全屏是铺满显示器；**普通最大化窗口过不了这条**——AppBar 已抬高工作区底边，最大化窗口底边停在工具区底边附近，够不到显示器底边。 |
| 4 | 窗口类不是系统外壳（`Progman`/`WorkerW`/`Shell_TrayWnd`/任务视图…） | 桌面、任务栏铺满屏幕也不是"某个程序全屏"。 |
| 5 | 所属进程不是 Windows 自身组件（见下） | 这才是「非系统程序」的落点。 |

### 「系统程序」的界定

* 内置名单 `SYSTEM_PROCESS_NAMES`：explorer / dwm / 登录锁屏 / 外壳（开始菜单、
  搜索、输入法）/ UAC `consent.exe` / 设置 / 任务管理器等，共 24 项。
* **刻意不复用** `dock.except_processes`：那份列表语义是「不要在 dock 上显示」，
  里面既有 `applicationframehost.exe`（UWP 全屏窗口在系统里属于它），也有
  `python.exe`（本项目自己就是 python 跑的）。拿它判断全屏会误伤 UWP 全屏和
  pygame 一类用 python 跑的全屏程序，所以判定只认上面这份内置名单，不再提供
  用户可编辑的排除列表。

### 为什么是轮询而不是 `SetWinEventHook`

事件钩子需要一条带消息循环的专用线程，回调跑在系统上下文里，出错排查成本高。项目
里 `core/process_scan.ProcessScanWorker` 已确立「后台线程定时轮询 + 信号投递回 GUI
线程」的模式，这里沿用：每轮只是一两次 `GetForegroundWindow` / `GetWindowRect` 和
一次**带 TTL 缓存**的进程查询（复用 `ProcessManager.proc_info_for_pid`），成本远低
于既有的进程扫描，且能被 `core/thread_mgr` 统一启停。

## 接线（`dock.py`）

| 环节 | 位置 | 说明 |
| ---- | ---- | ---- |
| 启动监听 | `_start_fullscreen_watch()` | 在 `thread_manager` 就绪后登记线程；`fullscreen.enabled=false` 时不启动。 |
| 进入让位 | `enter_fullscreen_suppression()` | **先注销 AppBar，再隐藏窗口**：AppBar 宿主是另一个隐藏窗口，两步互不依赖；先注销尽早把工作区还给全屏窗口，不留一帧被保留区挤压的机会。 |
| 退出让位 | `exit_fullscreen_suppression()` | 刷新屏幕指标 → 按最新位置重注册 AppBar → 重排并显示 dock → 补一次置顶保险。 |
| 幂等 | 两个方法都以 `_fs_suppressed` 早退 | 重复信号不会把 AppBar 注册/注销玩乱。 |
| 屏幕变化 | `_on_screen_changed()` | 让位期间**跳过** AppBar 重注册（否则把保留区塞回全屏窗口，让位当场失效），只刷新指标，等全屏结束再按新分辨率注册。 |
| 安全恢复 | `_ensure_dock_visible()` | 让位期间不强行显示 dock。 |
| 退出程序 | `exit_app()` / `atexit` | 先停监听线程再注销 AppBar，避免退出过程中又被信号动一次。 |
| 设置界面 | `_apply_fullscreen_settings()` | 开关即时启停线程。 |

位置计算统一走 `_dock_target_y()`：读**启动时保存的**原始工作区底部
（`_original_work_area_bottom`）减窗口高度。用保存值，反复注销/注册 AppBar 不会累
积漂移；`update_window_position()` 用同一算式，两者不打架。

## 配置

`settings.json`（缺失时由 `config_manager.load_config` 自动补齐）：

```json
"fullscreen": {
  "enabled": true,
  "poll_interval_ms": 400,
  "enter_confirm": 1,
  "exit_confirm": 2,
  "tolerance": 2
}
```

设置界面 → **Dock** 页 →「全屏程序」只有开关；`poll_interval_ms` /
`enter_confirm` / `exit_confirm` / `tolerance` 是调参项，只在配置文件里手改。

## 已知边界与取舍

* **窗口化全屏（borderless windowed）**：矩形铺满显示器且是前台一样触发 —— 正是想要的。
* **副屏全屏**：前台窗口在副屏铺满 → 让位。工作区是全局概念，dock 在主屏也会消失，
  这是当前取舍。
* **打包运行时**按 `sys.executable` 排除自己；源码运行（`python dock.py`）时**不**
  按进程名排除，否则 python 写的全屏程序会被一起放过。dock 自己的窗口另有句柄兜底
  （`set_ignored_hwnds`）。
* **用户拿回 dock 的办法**：按 Win 键/Alt+Tab 让全屏程序失去前台。前台一旦不再是全屏
  窗口（开始菜单属于系统组件、不在候选里），`exit_confirm` 轮后 dock 就回来。
* **`exit_confirm=2` 的代价**：全屏程序关闭后 dock 晚约 0.8 秒出现。

## 测试与验证

```bash
python tests/test_fullscreen_watch.py      # 判定规则 / 去抖状态机 / 监听线程 / 设置往返
python tests/manual_fullscreen_probe.py [秒数]   # 只看不动：打印前台窗口与判定理由
python tests/manual_appbar_cycle.py              # 真机 AppBar 注册→注销→重新注册
python tests/manual_dock_fullscreen_cycle.py     # 启动真实 dock 实例，走一遍让位/恢复
```

`manual_fullscreen_probe.py` 不改任何系统状态，可边用电脑边跑；想看到「会让位」的判
定，观察期间把浏览器按 F11 全屏即可。另两个脚本会让屏幕底部短暂出现 dock、工作区短
暂变化（约 3 秒被一块深灰色窗口铺满），收尾都会还原工作区；其中
`manual_dock_fullscreen_cycle.py` 覆盖两层：先直接调让位/恢复入口验证 AppBar 与窗
口状态，再**造一个铺满显示器的前台窗口**跑真正的自动链路（监听线程 → 信号 → dock
让位 → 关闭后自动恢复），全程不手动调用让位方法。

最近一次真机结果（1920×1080，任务栏可见时工作区底部 1020）：

```
让位前工作区底部=959（dock 的 AppBar 保留区生效）
让位后 AppBar 已注销 → 工作区底部回到 1020，dock 隐藏
恢复后 AppBar 重新注册 → 工作区底部精确回到 959，dock 重新显示
自动链路：模拟全屏窗口 rect=(0,0,1920,1080) → 自动让位 → 关闭后自动恢复
```

日志关键字：`[全屏]`、`[AppBar]`。

## 附：本次实现顺带修掉的两个坑

都是**既有代码里潜伏的问题**，被「全屏结束后必须重新注册 AppBar」这条新路径踩了出来：

1. **AppBar 宿主窗口类的 WNDPROC 悬空指针**（`core/sys32.py`）。老实现每次创建宿主
   窗口都重新构造 `_WNDPROC` 回调并覆盖模块全局，而窗口类只注册一次、存的是**第一个**
   回调的代码指针。全局一换，旧回调被回收，窗口类指向已释放内存 —— 之后再创建该类窗
   口以 `0xC000041D`（用户回调中发生致命异常）崩掉进程，即「重新注册 AppBar 必崩」。
   现在回调只在注册窗口类那一次创建。
2. **ctypes 签名冲突导致工作区读数为空**（`core/sys32.py` + `core/fullscreen_watch.py`）。
   `ctypes.windll.user32` 在进程内是同一对象，`argtypes` 挂在函数对象上。新模块曾用
   自己的 `MONITORINFO` 再声明一次 `GetMonitorInfoW`，覆盖 sys32 的声明，使 sys32 传
   的 `byref` 类型对不上而抛 `ArgumentError` —— 异常被 `except` 吞掉，
   `refresh_metrics()` 从此永远返回旧值（刚起来时全 0），屏幕变化后工作区再也不更新。
   现在这类多模块共用的函数只在 `core/sys32.py` 声明一次，并提供
   `monitor_rect_for_window()` 供其它模块调用。

另外把 `ABM_NEW` 的返回值判断改准确了：**同一个宿主窗口重复 `ABM_NEW` 返回 0，那是
"本来就注册着"而不是失败**。老实现（以及本次修改的第一版）会因此把状态标成「未注
册」，让位逻辑跳过注销，保留区再也还不回去。

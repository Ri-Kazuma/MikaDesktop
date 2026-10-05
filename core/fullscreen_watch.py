"""全屏程序监听：让 dock 与 AppBar 在全屏程序出现时让位。

要解决的问题
------------
dock 通过 AppBar 在屏幕某条边上保留了一块工作区（见 :mod:`core.sys32`）。这块
保留区对普通窗口是好事（最大化窗口会避开 dock），但对全屏程序是干扰：全屏窗口
应当独占整块屏幕。于是需要「检测到非系统程序全屏显示 → 注销 AppBar 并隐藏
dock；全屏解除 → 重新注册 AppBar 并显示 dock」。

为什么用轮询而不是 WinEvent 钩子
--------------------------------
``SetWinEventHook`` 需要一条带消息循环的专用线程来接收事件，回调还运行在系统
上下文里，出错时排查成本很高。本项目已经在 :class:`core.process_scan.ProcessScanWorker`
确立了「后台线程定时轮询 + 信号投递回 GUI 线程」的模式，这里沿用同一套：每次
轮询只做一两次 ``GetForegroundWindow`` / ``GetWindowRect`` 和一次带 TTL 缓存的
进程查询，成本远低于既有的进程扫描，而且这套模型在 :mod:`core.thread_mgr` 里
已经能被统一启停。

判定规则（"什么算全屏显示的非系统程序"）
----------------------------------------
一个窗口只有在同时满足下列条件时才算数：

1. 它是**前台窗口**，或者是前台窗口的 root owner（用来覆盖全屏游戏/播放器里
   弹出的对话框、菜单——它们的 root owner 才是那个铺满屏幕的窗口）。用「前台」
   而不是「Z 序最上」是因为多显示器：用户在副屏工作的同时主屏挂着全屏游戏时，
   前台窗口是副屏那个应用，dock 不该跟着消失。
2. 可见、未最小化、不是工具窗口（``WS_EX_TOOLWINDOW``，这类是输入法提示、
   悬浮工具栏之类的辅助窗口）。
3. 它的窗口矩形完整覆盖**它所在显示器**的 ``rcMonitor``（容差默认 2px）。
   普通「最大化」窗口过不了这一条：dock 的 AppBar 已经抬高了工作区底边，
   最大化窗口的底边停在工具区底边附近，够不到显示器底边。
4. 窗口类不是系统外壳窗口（桌面 Progman/WorkerW、任务栏、任务视图等）。
5. 所属进程不是 Windows 自身组件（见 :data:`SYSTEM_PROCESS_NAMES`）。

这里**刻意不复用** ``dock.except_processes``：那份列表的语义是「不要在 dock 上
显示这个程序」，里面既有 ``applicationframehost.exe``（UWP 应用的全屏窗口在
系统里属于它），也有 ``python.exe``（本项目自己就是 python 跑起来的）。拿它来
判断全屏会误伤 UWP 全屏和 pygame 一类的 python 全屏程序，所以判定只认
:data:`SYSTEM_PROCESS_NAMES` 这一份内置名单，不提供用户可编辑的排除列表。
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass

import win32gui
import win32process

from PySide6.QtCore import QThread, Signal

from . import log_maker
from . import sys32

log = log_maker.logger()

__all__ = [
    "SYSTEM_PROCESS_NAMES",
    "SYSTEM_WINDOW_CLASSES",
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_TOLERANCE",
    "DEFAULT_ENTER_CONFIRM",
    "DEFAULT_EXIT_CONFIRM",
    "WindowSnapshot",
    "covers_monitor",
    "is_system_process",
    "is_system_window_class",
    "normalize_process_name",
    "select_fullscreen_window",
    "snapshot_window",
    "collect_candidates",
    "FullscreenWatcherWorker",
]


# ========== Win32 ==========
# 坐标一致性：窗口矩形与显示器矩形都取自同一个进程的 Win32 调用，而进程的 DPI
# 感知状态由 core.sys32 在导入时统一设定，所以两边永远是同一套坐标系（物理像素），
# 不需要也不应该在覆盖判定里做任何缩放换算。
_user32 = ctypes.windll.user32

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_GA_ROOTOWNER = 3

# 64 位句柄不能按默认的 c_int 返回，否则句柄被截断（core/sys32.py 里同样踩过）。
#
# 注意：这里只为**本模块自己用**的函数声明签名。GetMonitorInfoW / MonitorFromWindow
# 这类多个模块都要用的函数统一由 core/sys32.py 声明一次（ctypes 的 argtypes 挂在
# 函数对象上，重复声明会互相覆盖，最后导致某一方传参类型不匹配而静默失败）。
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL


# ========== 判定规则 ==========
#: Windows 自身组件：即使全屏也不该让 dock 让位（这正是「非系统程序」的反面）。
#: 名单只收「有可能铺满整块屏幕」的组件，避免把无关进程也写进来造成误判。
SYSTEM_PROCESS_NAMES = frozenset([
    "explorer.exe",                  # 桌面 / 文件资源管理器窗口
    "dwm.exe",                       # 桌面窗口管理器
    "winlogon.exe", "logonui.exe", "lockapp.exe",     # 登录、锁屏
    "csrss.exe", "wininit.exe", "services.exe", "lsass.exe", "smss.exe",
    "sihost.exe", "fontdrvhost.exe", "dllhost.exe", "runtimebroker.exe",
    "shellexperiencehost.exe",       # 开始菜单 / 音量 / 操作中心等外壳界面
    "startmenuexperiencehost.exe",
    "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "consent.exe",                   # UAC 提权提示
    "systemsettings.exe",            # 设置
    "taskmgr.exe",                   # 任务管理器
    "widgets.exe", "widgetservice.exe",
])

#: 系统外壳窗口类：这些窗口即使铺满屏幕也是外壳的一部分，不是「某个程序全屏」。
SYSTEM_WINDOW_CLASSES = frozenset([
    "Progman",                       # 桌面本体
    "WorkerW",                       # 桌面壁纸层
    "Shell_TrayWnd",                 # 主任务栏
    "Shell_SecondaryTrayWnd",        # 副屏任务栏
    "MultitaskingViewFrame",         # 任务视图
    "TaskListThumbnailWnd",          # 悬停预览
    "XamlExplorerHostIslandWindow",  # Win11 任务视图 / 桌面岛
    "Shell_InputSwitchTopLevelWindow",
    "ApplicationManager_DesktopShellWindow",
    "LockScreenOcclusionFrame", "LockScreenBackstopFrame",
    "ForegroundStaging",             # 前台切换用的中间窗口
    "SysShadow",                     # 窗口投影
])

#: 轮询间隔（毫秒）。400ms 下 dock 的让位几乎无感，开销也只有一次前台窗口查询。
DEFAULT_POLL_INTERVAL_MS = 400
#: 覆盖判定容差（像素）：留一点余量吸收窗口边框/无边框全屏的 1~2px 偏差。
DEFAULT_TOLERANCE = 2
#: 进入全屏需要连续命中的轮数（1 = 立刻让位，避免 dock 在全屏画面上多停一拍）
DEFAULT_ENTER_CONFIRM = 1
#: 退出全屏需要连续未命中的轮数。取 2 是为了扛住 Alt+Tab 切窗口、全屏程序弹
#: 系统对话框这类瞬时抖动，避免 AppBar 反复注册/注销把工作区来回拉扯。
DEFAULT_EXIT_CONFIRM = 2


def normalize_process_name(name) -> str:
    """进程名规范化为小写且带扩展名（``Chrome`` -> ``chrome.exe``）。"""
    if not name:
        return ""
    text = str(name).strip().lower()
    if not text:
        return ""
    if "." not in text:
        text += ".exe"
    return text


def is_system_window_class(class_name) -> bool:
    """窗口类是否属于系统外壳。"""
    return (class_name or "") in SYSTEM_WINDOW_CLASSES


def is_system_process(process_name, exe_path=None) -> bool:
    """进程是否属于 Windows 自身组件。

    ``process_name`` 拿不到时（某些受保护进程）退化为用 ``exe_path`` 的文件名
    判断，仍然拿不到就按「不是系统进程」处理——宁可判成普通程序，也不要因为
    读不到信息就永远不隐藏 dock。
    """
    name = normalize_process_name(process_name)
    if not name and exe_path:
        name = normalize_process_name(os.path.basename(str(exe_path)))
    if not name:
        return False
    return name in SYSTEM_PROCESS_NAMES


def covers_monitor(rect, monitor_rect, tolerance: int = DEFAULT_TOLERANCE) -> bool:
    """``rect`` 是否完整覆盖显示器 ``monitor_rect``（允许 ``tolerance`` 像素误差）。

    两个矩形都是屏幕坐标 ``(left, top, right, bottom)``。``monitor_rect`` 为
    ``None``（读不到显示器信息）时一律判否：这种情况下的默认值是一个空矩形，
    拿来比较会得到「什么都覆盖」的错误结论。
    """
    if not rect or not monitor_rect:
        return False
    try:
        left, top, right, bottom = rect
        mon_left, mon_top, mon_right, mon_bottom = monitor_rect
    except (TypeError, ValueError):
        return False

    tol = max(int(tolerance), 0)
    # 先比尺寸，够不到显示器尺寸的窗口直接否掉（比逐边比较更快也更直观）
    if right - left < (mon_right - mon_left) - 2 * tol:
        return False
    if bottom - top < (mon_bottom - mon_top) - 2 * tol:
        return False

    return (left <= mon_left + tol
            and top <= mon_top + tol
            and right >= mon_right - tol
            and bottom >= mon_bottom - tol)


@dataclass(frozen=True)
class WindowSnapshot:
    """某一时刻某个窗口的判定所需信息（纯数据，便于单测直接构造）。"""

    hwnd: int
    class_name: str = ""
    title: str = ""
    process_name: str = ""
    exe_path: str | None = None
    rect: tuple | None = None
    monitor_rect: tuple | None = None
    #: 是否是当前前台窗口
    foreground: bool = False
    #: 是否是前台窗口的 root owner
    root_owner: bool = False
    visible: bool = True
    iconic: bool = False
    tool_window: bool = False

    def describe(self) -> str:
        """给人看的描述，用于日志与 dock 的提示。"""
        name = normalize_process_name(self.process_name) or os.path.basename(self.exe_path or "")
        if not name:
            name = "未知进程"
        title = (self.title or "").strip()
        if title:
            return f"{name}（{title}）"
        return name


def select_fullscreen_window(snapshots, *, tolerance: int = DEFAULT_TOLERANCE,
                             ignored_hwnds=(),
                             own_process_name: str = ""):
    """从快照里挑出「正在全屏显示的非系统窗口」；没有则返回 ``None``。

    ``snapshots`` 按候选优先级排列（前台窗口在前），返回第一个通过的快照。
    判定过程中用到的每个条件都在模块文档里有说明；这里的顺序是「先便宜后昂贵」。
    """
    ignored = {int(h) for h in (ignored_hwnds or ()) if h}
    own = normalize_process_name(own_process_name)

    for snapshot in snapshots or ():
        if not isinstance(snapshot, WindowSnapshot):
            continue
        # 只有前台窗口（或其 root owner）才代表"用户正在看的东西"
        if not (snapshot.foreground or snapshot.root_owner):
            continue
        if snapshot.hwnd in ignored:
            continue
        if not snapshot.visible or snapshot.iconic or snapshot.tool_window:
            continue
        if is_system_window_class(snapshot.class_name):
            continue
        if is_system_process(snapshot.process_name, snapshot.exe_path):
            continue
        name = normalize_process_name(snapshot.process_name) or normalize_process_name(
            os.path.basename(str(snapshot.exe_path or ""))
        )
        if own and name == own:
            continue
        if not covers_monitor(snapshot.rect, snapshot.monitor_rect, tolerance):
            continue
        return snapshot
    return None


# ========== 真实窗口信息 ==========
def root_owner_hwnd(hwnd):
    """前台窗口的 root owner（没有则返回 0）。"""
    try:
        if not hwnd:
            return 0
        return _user32.GetAncestor(hwnd, _GA_ROOTOWNER) or 0
    except Exception:
        return 0


def snapshot_window(hwnd, process_manager=None, foreground: bool = False,
                    root_owner: bool = False):
    """读取一个窗口的判定信息；窗口不存在或读取失败返回 ``None``。

    ``process_manager`` 提供 ``proc_info_for_pid``（带 TTL 缓存）用于解析窗口归属
    的进程；不传的话进程字段留空（只做几何判定，供测试/探针使用）。
    """
    try:
        if not hwnd or not _user32.IsWindow(hwnd):
            return None

        process_name = ""
        exe_path = None
        if process_manager is not None:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            if pid:
                try:
                    exe_path, process_name = process_manager.proc_info_for_pid(pid)
                except Exception as e:
                    log.debug(f"解析窗口 {hwnd} 归属进程失败: {e}")

        rect = tuple(win32gui.GetWindowRect(hwnd))
        return WindowSnapshot(
            hwnd=int(hwnd),
            class_name=win32gui.GetClassName(hwnd),
            title=win32gui.GetWindowText(hwnd),
            process_name=process_name or "",
            exe_path=exe_path,
            rect=rect,
            monitor_rect=sys32.monitor_rect_for_window(hwnd),
            foreground=bool(foreground),
            root_owner=bool(root_owner),
            visible=bool(_user32.IsWindowVisible(hwnd)),
            iconic=bool(_user32.IsIconic(hwnd)),
            tool_window=bool(win32gui.GetWindowLong(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW),
        )
    except Exception as e:
        log.debug(f"读取窗口 {hwnd} 信息失败: {e}")
        return None


def collect_candidates(process_manager, ignored_hwnds=()):
    """收集候选快照：当前前台窗口，以及它的 root owner。

    只收集这两个，是因为判定规则要求候选必须是前台窗口或其 root owner ——
    枚举全系统窗口既没必要，也拿不到"用户正在看哪个"这个信息。
    """
    ignored = {int(h) for h in (ignored_hwnds or ()) if h}
    foreground_hwnd = int(_user32.GetForegroundWindow() or 0)
    owner_hwnd = root_owner_hwnd(foreground_hwnd)

    snapshots = []
    for hwnd, is_owner in ((foreground_hwnd, False), (owner_hwnd, True)):
        if not hwnd or hwnd in ignored:
            continue
        if any(snap.hwnd == hwnd for snap in snapshots):
            continue
        snapshot = snapshot_window(
            hwnd, process_manager,
            foreground=(hwnd == foreground_hwnd),
            root_owner=is_owner,
        )
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


# ========== 监听线程 ==========
class FullscreenWatcherWorker(QThread):
    """周期性检测是否有非系统程序正在全屏显示。

    与 :class:`core.process_scan.ProcessScanWorker` 同一套线程模型：``run()`` 跑
    自己的循环，结果通过 Qt 信号排队投递回 GUI 线程（AppBar 的宿主窗口是在 GUI
    线程创建的，注销/注册必须回到同一个线程执行）。
    """

    #: (is_fullscreen, 描述) —— 只在状态真正翻转时发出
    state_changed = Signal(bool, str)
    #: 检测过程中的异常描述（只上报，不中断循环）
    scan_failed = Signal(str)

    def __init__(self, process_manager, interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
                 enter_confirm: int = DEFAULT_ENTER_CONFIRM,
                 exit_confirm: int = DEFAULT_EXIT_CONFIRM,
                 tolerance: int = DEFAULT_TOLERANCE,
                 own_process_name: str = "",
                 candidate_provider=None,
                 parent=None):
        super().__init__(parent)
        self._process_manager = process_manager
        self._interval = max(int(interval_ms), 50) / 1000.0
        self._enter_confirm = max(int(enter_confirm), 1)
        self._exit_confirm = max(int(exit_confirm), 1)
        self._tolerance = max(int(tolerance), 0)
        self._own_process_name = own_process_name or ""
        # 候选来源可注入：单测无需真实桌面即可驱动整个状态机
        self._candidate_provider = candidate_provider or collect_candidates

        self._lock = threading.RLock()
        self._ignored_hwnds = set()
        self._state = False
        self._label = ""
        self._pending = False
        self._pending_count = 0

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    # -- 运行期参数（GUI 线程调用） --------------------------------------- #
    def set_ignored_hwnds(self, hwnds) -> None:
        """设置要忽略的窗口句柄（dock 自己的窗口、XHT 窗口等）。"""
        with self._lock:
            self._ignored_hwnds = {int(h) for h in (hwnds or ()) if h}

    def is_fullscreen(self) -> bool:
        """当前（去抖后）的全屏状态。"""
        with self._lock:
            return self._state

    def current_label(self) -> str:
        """当前全屏窗口的描述（非全屏时为空串）。"""
        with self._lock:
            return self._label

    # -- 生命周期 -------------------------------------------------------- #
    def stop(self, wait_ms: int = 3000) -> None:
        """请求停止并（默认）等待线程退出；可重复调用。"""
        self._stop_event.set()
        self._wake_event.set()
        if self.isRunning():
            self.wait(wait_ms)

    def quit(self) -> None:  # noqa: D102 - 覆盖 QThread.quit，配合 ThreadManager.stop()
        # 本线程跑的是自己的 while 循环而不是事件循环，QThread.quit() 对它无效。
        self._stop_event.set()
        self._wake_event.set()

    # -- 检测 ------------------------------------------------------------ #
    def poll_once(self) -> bool:
        """执行一次检测并把结果交给去抖状态机；返回是否发生了对外的状态翻转。

        单独抽出来是为了可测：注入 ``candidate_provider`` 后，不必启动线程、不必
        真的把某个程序全屏，就能验证整套「检测 → 去抖 → 上报」逻辑。
        """
        with self._lock:
            ignored = set(self._ignored_hwnds)
            tolerance = self._tolerance
            own = self._own_process_name

        snapshots = self._candidate_provider(self._process_manager, ignored)
        snapshot = select_fullscreen_window(
            snapshots,
            tolerance=tolerance,
            ignored_hwnds=ignored,
            own_process_name=own,
        )
        return self._decide(snapshot is not None,
                            snapshot.describe() if snapshot is not None else "")

    def _decide(self, now_fullscreen: bool, description: str = "") -> bool:
        """把一次检测结果喂给去抖状态机；返回是否发生了对外的状态翻转。"""
        changed = False
        with self._lock:
            if now_fullscreen != self._pending:
                # 状态候选变了，重新计数
                self._pending = now_fullscreen
                self._pending_count = 1
            else:
                self._pending_count += 1

            need = self._enter_confirm if self._pending else self._exit_confirm
            if self._pending != self._state and self._pending_count >= need:
                self._state = self._pending
                self._label = description if self._pending else ""
                changed = True
                state, label = self._state, self._label
            else:
                state = label = None

        if changed:
            log.info("[全屏] {}：{}".format("检测到全屏程序" if state else "全屏程序已退出",
                                             label or ""))
            self.state_changed.emit(state, label)
        return changed

    # -- 线程主体 -------------------------------------------------------- #
    def run(self) -> None:  # noqa: D102 - QThread 入口
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - 后台线程异常必须上报，否则静默失效
                if not self._stop_event.is_set():
                    self.scan_failed.emit(f"{type(exc).__name__}: {exc}")

            self._wake_event.wait(self._interval)
            self._wake_event.clear()

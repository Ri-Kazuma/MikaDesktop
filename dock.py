import gc
import os
import sys
import uuid
import hashlib
import atexit
from typing import Dict, List, Any
import subprocess

import win32api
import win32con
import win32event
import win32gui
import winerror
from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt, QSize, QTimer, QEvent)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QPushButton, QFileDialog, QVBoxLayout, QHBoxLayout,
                               QDialog, QInputDialog)
from core.custom_ui import IconHoverFilter, ContextPopup, ShutdownDialog
from core.dock_constants import DockConstants
from core import dock_extension
from core.dock_extension import DockExtensionWindow
from core.dock_tooltip import DockTooltip
from core.fullscreen_watch import (
    DEFAULT_ENTER_CONFIRM,
    DEFAULT_EXIT_CONFIRM,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_TOLERANCE,
    FullscreenWatcherWorker,
)
from core.process_manager import ProcessManager
from core.process_scan import ProcessScanWorker
from core import pinned_apps
import core.sys32 as sys32
import core.log_maker as log_maker
import core.config_manager as Config
import core.settings as settings
from features.XHT.Lib import XHTWindow

from core.thread_mgr import manager

VERSION = "0.0.0"

log = log_maker.logger()
#log.enable_debug()
log.disable_debug()


def _config_int(value, default, minimum):
    """配置里的整数取值：手写 JSON 里可能是字符串/空值，坏了就退回默认值。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(result, minimum)



# 常量与样式表统一定义在 core/dock_constants.py（纯数据，独立成模块便于复用）


class DockApp(QMainWindow):
    def __init__(self):
        super().__init__()
        if getattr(sys, 'frozen', False):
            self.script_dir = os.path.dirname(sys.executable)
        else:
            self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.settings_file = os.path.join(self.script_dir, "settings.json")
        self.all_settings = None
        # 命令提示符禁用模式（nocmd_mode）与内置进程管理器状态
        self.is_cmd_disabled = False
        self._process_mgr_worker = None
        self._process_mgr_thread_id = None
        
        # 应用数据存储
        self.running_apps: Dict[str, str] = {}
        self.app_buttons: Dict[str, QPushButton] = {}
        self.pinned_app_buttons: Dict[str, QPushButton] = {}
        self.running_app_buttons: Dict[str, QPushButton] = {}
        
        # 应用列表
        self.pinned_apps: List[Dict[str, Any]] = []
        self.apps: List[Dict[str, Any]] = []
        self.running_apps_list: List[Dict[str, Any]] = []
        
        # UI组件
        self.icon_hover_filter = IconHoverFilter(self)
        self.process_manager = ProcessManager()
        self.geometry_anim = None
        # 最近一次动画的目标矩形，用于避免重复重启动画（见 update_window_position）
        self._geom_anim_target = None

        self.xhtelements = []
        self.xht_window = None  # 保存 XHT 窗口引用，防止被 GC 回收

        # dock 右侧的扩展窗口（高度与 dock 相同、宽度 150~600；里面放
        # 网络/音量/电源与通知中心入口，布局与让位都跟着 dock 走，
        # 见 core/dock_extension.py 与 core/extension_panel.py）
        self._extension = None
        self._extension_panel = None
        self._status_worker = None
        self._status_thread_id = None

        self.hwnd = None

        # 全屏让位状态：True = 有非系统程序正在全屏显示，AppBar 已注销、dock 已隐藏
        # （监听逻辑见 core/fullscreen_watch.py，线程在 thread_manager 就绪后启动）
        self._fs_suppressed = False
        self._fs_worker = None
        self._fs_thread_id = None
        self._fs_last_description = ""

        # 图标版本管理
        self._list_versions: Dict[str, str] = {}
        
        self.init_ui()
        # 右侧扩展窗口必须在第一次 update_window_position 之前建好，
        # 这样首次布局就会把它和 dock 一起摆好（见 _init_extension）
        self._init_extension()
        self.load_settings()
        self.load_pinned_apps()
        self.update_app_buttons()
        self.setup_process_monitoring()
        # Position the window at center horizontally and 20 pixels from bottom
        self.update_window_position()
        # 将程序栏注册为底部 AppBar，系统自动调整工作区使最大化窗口避开 dock
        # 注册前保存原始工作区底部（Qt 逻辑坐标），供 update_window_position 定位
        self._original_work_area_bottom = QApplication.primaryScreen().availableGeometry().bottom()
        # Qt 坐标是逻辑像素，AppBar 需要物理像素（系统 DPI aware 坐标系）
        dpr = QApplication.primaryScreen().devicePixelRatio()
        dock_top_phys = round(self.geometry().y() * dpr)
        sys32.set_appbar_bottom(dock_top_phys)
        # 注册 atexit 兜底，确保任何退出方式都能恢复工作区
        atexit.register(self.on_unusual_exit)
        # 使用统一的线程管理器启动所有后台服务
        self.thread_manager = manager.ThreadManager()
        # 进程扫描线程必须等 thread_manager 就绪后再注册启动
        self._start_process_monitoring()
        # 全屏程序监听同样登记到 thread_manager，退出时统一收尾
        self._start_fullscreen_watch()
        # 扩展窗口的状态轮询（网络 / 音量 / 电源）
        self._start_status_watch()
        # 分辨率/显示器变化后刷新屏幕指标缓存并重新定位
        self._connect_screen_signals()

        # 启动 XHT 浮动时间窗口
        self.start_xht()

        self.destroyed.connect(self.exit_app)

    def on_unusual_exit(self):
        """处理非正常退出"""
        sys32.remove_appbar()
        sys32.show_window(sys32.HWND_TRAY)



    def eventFilter(self, obj, event):
        """过滤键盘事件，屏蔽关闭窗口相关的快捷键"""
        if event.type() == QEvent.KeyPress:
            key = event.key()
            modifiers = event.modifiers()
            
            # 屏蔽常见关闭窗口快捷键组合
            blocked_shortcuts = [
                (Qt.ControlModifier, Qt.Key_Q),  # Ctrl+Q
                (Qt.ControlModifier, Qt.Key_W),  # Ctrl+W
                (Qt.AltModifier, Qt.Key_F4),     # Alt+F4
                (Qt.ControlModifier | Qt.ShiftModifier, Qt.Key_W),  # Ctrl+Shift+W
                (Qt.NoModifier, Qt.Key_Escape)   # Escape
            ]
            
            for mod, k in blocked_shortcuts:
                if modifiers == mod and key == k:
                    log.info(f"阻止快捷键: {modifiers.name() if modifiers else 'No Modifiers'}+{event.text() if event.text() else key}")
                    return True
        
        return super().eventFilter(obj, event)
    
    def create_app_button(self, app_data: Dict[str, Any], button_dict: Dict[str, QPushButton], 
                         layout: QHBoxLayout, is_running_app: bool = False) -> QPushButton:
        """创建统一的应用按钮"""
        app_name = app_data['name']
        uid = self._assign_uid(app_data)
        
        # 确保图标存在
        icon_path = app_data.get('icon') or ''
        if not icon_path or not os.path.exists(icon_path):
            # 如果图标路径不存在或文件不存在，重新提取图标
            app_data['icon'] = self.process_manager.extract_icon(app_data.get('path', '')) or ''
            icon_path = app_data['icon']
        
        # 创建按钮
        button = QPushButton()
        button.setFixedSize(DockConstants.BUTTON_SIZE, DockConstants.BUTTON_SIZE)
        button.setMouseTracking(True)
        button._bound_uid = uid
        
        # 设置图标
        if icon_path and os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            if not pixmap.isNull():
                button.setIcon(QIcon(pixmap))
                button.setIconSize(QSize(DockConstants.ICON_SIZE, DockConstants.ICON_SIZE))
        
        # 检查运行状态并设置样式
        if is_running_app:
            is_running = self.process_manager.is_process_running(app_data['path'])
        else:
            is_running = app_name in self.running_apps
        
        self.set_button_style(button, is_running)
        
        # 绑定点击事件
        button.clicked.connect(lambda checked, app=app_data: self.handle_app_click(app))
        
        # 绑定右键菜单
        button.setContextMenuPolicy(Qt.CustomContextMenu)
        button.customContextMenuRequested.connect(
            lambda pos, app=app_data, btn=button: self.show_app_context_menu(pos, app, btn)
        )
        
        # 设置工具提示
        button.setToolTip(app_name)
        
        # 安装悬浮事件过滤器
        button.setAttribute(Qt.WA_Hover, True)
        button.setMouseTracking(True)
        button.installEventFilter(self.icon_hover_filter)
        
        # 保存按钮引用
        button_dict[app_name] = button
        
        # 添加到布局
        layout.addWidget(button)

        # 新按钮默认是隐藏的，要等 Qt 的 ChildPolished / LayoutRequest 事件处理完才会
        # 显示；在那之前布局把它当成空项（QWidgetItem::isEmpty()），这一组的最小尺寸
        # 会被算成 0 —— 紧接着 update_window_position 算出来的目标宽度就是错的（窗口
        # 缩不回去、多出来的宽度被 app_container 吞掉，那组按钮间距忽大忽小）。
        # 显式显示，让布局立刻把它算进去；父窗口/父容器本身隐藏时不会有副作用。
        button.show()
        
        return button

    def load_pinned_apps(self):
        """获取 Windows 任务栏上固定的应用程序（实现见 core/pinned_apps.py）。"""
        try:
            # 注意局部名不要叫 pinned_apps，否则会遮蔽上面 import 的模块
            self.pinned_apps = pinned_apps.discover_pinned_apps(self.process_manager, log)
        except Exception as e:
            self.handle_error(f"获取固定应用时出错: {e}")
            self.pinned_apps = []
    
    def handle_error(self, message: str, show_dialog: bool = False):
        """统一错误处理"""
        log.error(message)
        if show_dialog:
            sys32.messagebox("错误", message, sys32.MB_ICONSTOP | sys32.MB_OKCANCEL)

    def setup_process_monitoring(self):
        """创建后台进程扫描线程。

        扫描本身（EnumWindows + 全量 psutil 遍历 + 图标提取）在
        :class:`~core.process_scan.ProcessScanWorker` 里跑，这里只负责建对象和连
        信号；线程在 thread_manager 就绪后再启动（见 ``__init__``）。
        """
        self._scan_worker = ProcessScanWorker(
            self.process_manager, DockConstants.PROCESS_CHECK_INTERVAL
        )
        self._scan_worker.scan_finished.connect(self._on_process_scan_finished)
        self._scan_worker.scan_failed.connect(self._on_process_scan_failed)
        self._scan_thread_id = None

    def _start_process_monitoring(self):
        """把扫描线程注册到统一线程管理器并启动（需要 thread_manager 已就绪）。"""
        worker = getattr(self, '_scan_worker', None)
        if worker is None:
            return
        try:
            self._scan_thread_id = self.thread_manager.create(
                name="dock_process_scan",
                start_when_create=True,
                worker=worker,
            )
        except Exception as e:
            # 线程管理器有数量上限（默认 16），注册失败时退化为直接启动，
            # 至少保证「运行中应用」的显示还能工作。
            log.warning(f"注册进程扫描线程失败，改为直接启动: {e}")
            worker.start()

    def _connect_screen_signals(self):
        """监听显示器/分辨率变化，刷新 sys32 缓存的屏幕指标。"""
        app = QApplication.instance()
        if app is None:
            return
        for name in ("screenAdded", "screenRemoved", "primaryScreenChanged"):
            signal = getattr(app, name, None)
            if signal is None:
                continue
            try:
                signal.connect(self._on_screen_changed)
            except Exception as e:
                log.debug(f"连接 {name} 信号失败: {e}")

    def _on_screen_changed(self, *_args):
        """分辨率或显示器变化：刷新指标缓存、重新注册 AppBar 并重排窗口。"""
        try:
            metrics = sys32.refresh_metrics()
            log.info(f"屏幕变化，已刷新屏幕指标: {metrics}")
            if getattr(self, '_fs_suppressed', False):
                # 全屏让位期间 AppBar 已注销：这里再注册一次会把保留区塞回全屏
                # 窗口，让位当场失效。改成只刷新指标，等全屏结束由
                # exit_fullscreen_suppression() 按新分辨率重新注册。
                log.info("当前处于全屏让位状态，跳过 AppBar 重新注册")
                return
            screen = QApplication.primaryScreen()
            if screen is not None:
                dpr = screen.devicePixelRatio()
                sys32.set_appbar_bottom(round(self._dock_target_y() * dpr))
            self.update_window_position()
        except Exception as e:
            log.error(f"处理屏幕变化时出错: {e}")

    def check_running_processes(self):
        """请求一次进程状态检查（非阻塞）。

        真正的扫描在后台线程里做，结果回来后由
        :meth:`_on_process_scan_finished` 在 GUI 线程更新界面。保留这个方法名是
        为了兼容既有的 ``QTimer.singleShot(..., self.check_running_processes)``
        调用点（启动/结束进程后要求尽快刷新）。
        """
        worker = getattr(self, '_scan_worker', None)
        if worker is not None:
            worker.request_scan()

    def _on_process_scan_failed(self, message):
        """后台扫描出错（只记录，不打断界面）。"""
        log.error(f"后台进程扫描失败: {message}")

    def _on_process_scan_finished(self, all_running):
        """后台扫描完成：在 GUI 线程里比对并更新按钮状态。"""
        try:
            norm = self.process_manager.norm_path
            all_apps = self.pinned_apps + self.apps

            # running_apps_list 只保留未知应用（非 pinned + 非手动添加）
            normalized_known_paths = {norm(app['path']) for app in all_apps}
            self.running_apps_list = [
                info for path, info in all_running.items()
                if norm(path) not in normalized_known_paths
            ]

            # 批量检查已知应用状态
            normalized_running = {norm(p) for p in all_running}
            current_running = {}
            for app in all_apps:
                if norm(app['path']) in normalized_running:
                    current_running[app['name']] = app['path']

            # 更新按钮状态
            changed_apps = set(self.running_apps.keys()) ^ set(current_running.keys())
            for app_name in changed_apps:
                button = self.get_app_button(app_name)
                if button:
                    is_running = app_name in current_running
                    self.set_button_style(button, is_running)
                    log.info(f"应用 {app_name} 状态更新: {'运行中' if is_running else '已关闭'}")

            self.running_apps = current_running
            self.update_app_buttons()

        except Exception as e:
            log.error(f"更新运行进程状态时出错: {e}")

    def _ensure_dock_visible(self):
        """确保 dock 栏处于可见置顶状态（安全恢复方法）。

        系统可能把窗口重新显示出来，此时补一次 ShowWindow + raise_ 即可。
        """
        if self.hwnd is None:
            return
        if getattr(self, '_fs_suppressed', False):
            # 全屏让位期间 dock 本来就该藏着，别在这里把它强行显示回来
            return
        if not self.isVisible():
            sys32.show_window(self.hwnd)
            log.info("dock栏已恢复显示（安全恢复）")
        # 扩展窗口跟着 dock 一起恢复（让位时是一起藏起来的）
        self._set_extension_visible(True)

    # ------------------------------------------------------------------ #
    # 右侧扩展窗口：创建、显隐、宽度（几何布局见 update_window_position）
    # ------------------------------------------------------------------ #
    def _init_extension(self):
        """创建 dock 右侧的扩展窗口，并在里面放上状态面板。

        面板（网络 / 音量 / 电源 / 通知中心）是可选的：构造失败时扩展窗口
        退化成一块空白卡片，不影响 dock 本体。
        """
        try:
            extension = DockExtensionWindow()
            # 窗口第一次真正显示时把句柄告诉全屏监听线程：扩展窗口自己也永远
            # 不该被当成"全屏程序"，否则会触发无意义的让位
            extension.shown_signal.connect(self._sync_fullscreen_ignored_windows)
            try:
                self._build_extension_panel(extension)
            except Exception as e:
                log.error(f"创建扩展窗口面板失败（面板区域保持空白）: {e}")
            self._extension = extension
            log.info(f"扩展窗口已创建：宽 {extension.extension_width()}，"
                     f"高 {extension.extension_height()}")
        except Exception as e:
            self._extension = None
            log.error(f"创建扩展窗口失败: {e}")

    def _build_extension_panel(self, extension):
        """把状态面板放进扩展窗口，并接好宽度联动。"""
        from core.extension_panel import ExtensionPanel
        from core.system_status import SystemStatusWorker

        self._status_worker = SystemStatusWorker()
        self._extension_panel = ExtensionPanel(
            res_dir=os.path.join(self.script_dir, "res"),
            status_worker=self._status_worker,
            log=log,
        )
        extension.set_content(self._extension_panel)
        # 内容宽度变化（例如台式机上不显示电源按钮）→ 重新排布 dock + 扩展窗口
        extension.width_hint_changed.connect(self.set_extension_width)

    def _start_status_watch(self):
        """把系统状态轮询线程登记到统一线程管理器并启动（需要 thread_manager 就绪）。"""
        worker = getattr(self, '_status_worker', None)
        if worker is None:
            return
        try:
            self._status_thread_id = self.thread_manager.create(
                name="dock_system_status",
                start_when_create=True,
                worker=worker,
            )
        except Exception as e:
            log.warning(f"注册系统状态线程失败，改为直接启动: {e}")
            try:
                worker.start()
            except Exception as exc:
                log.error(f"启动系统状态线程失败: {exc}")

    def _set_extension_visible(self, visible: bool):
        """按 dock 的显隐状态显示 / 隐藏扩展窗口（全屏让位时一并让位）。"""
        extension = getattr(self, '_extension', None)
        if extension is None:
            return
        try:
            if visible:
                if not extension.isVisible():
                    extension.show()
                # 显示后再按 dock 的真实几何对一次位置/高度：首次显示时 dock 的
                # 高度会被 Qt 布局撑到最小高度，不能沿用布局前的估算值
                self._follow_extension()
            else:
                extension.hide()
        except Exception as e:
            log.error(f"{'显示' if visible else '隐藏'}扩展窗口失败: {e}")

    def _follow_extension(self):
        """把扩展窗口贴到 dock 实际矩形的右侧（间隙见 EXTENSION_GAP）。

        dock 移动 / 改变大小都会走到这里（``moveEvent`` / ``resizeEvent``），
        所以扩展窗口的左边界始终 = dock 右边界 + 间隙，绝不与 dock 重叠；
        顶边和高度也始终等于 dock 的真实值。
        """
        extension = getattr(self, '_extension', None)
        if extension is None:
            return
        try:
            extension.follow_dock(self.geometry())
        except Exception as e:
            log.debug(f"跟随 dock 摆放扩展窗口失败: {e}")

    def moveEvent(self, event):
        super().moveEvent(event)
        self._follow_extension()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._follow_extension()

    def set_extension_width(self, width):
        """调整扩展窗口宽度（自动夹到 150~600）并重排 dock + 扩展窗口。

        返回夹取后的宽度；没有扩展窗口时返回 ``None``。给后续往扩展窗口里放
        内容的代码用，当前启动阶段不用调用（启动宽度就是最小值）。
        """
        extension = getattr(self, '_extension', None)
        if extension is None:
            return None
        new_width = extension.set_extension_width(width)
        self._geom_anim_target = None
        self.update_window_position()
        # 改宽度会改变 dock 的目标位置；若几何恰好没变，上面不会触发移动事件，
        # 这里再对齐一次，保证改完宽度两者仍然是贴着的
        self._follow_extension()
        log.info(f"扩展窗口宽度已调整为 {new_width}")
        return new_width


    # ------------------------------------------------------------------ #
    # 全屏程序让位：注销 AppBar + 隐藏 dock（判定规则见 core/fullscreen_watch.py）
    # ------------------------------------------------------------------ #
    def _start_fullscreen_watch(self):
        """启动全屏程序监听线程。"""
        fs_config = (self.all_settings or {}).get('fullscreen', {}) or {}
        if not fs_config.get('enabled', True):
            log.info("全屏程序监听未启用（settings.json: fullscreen.enabled=false）")
            return
        try:
            self._fs_worker = FullscreenWatcherWorker(
                self.process_manager,
                interval_ms=_config_int(fs_config.get('poll_interval_ms'),
                                        DEFAULT_POLL_INTERVAL_MS, 50),
                enter_confirm=_config_int(fs_config.get('enter_confirm'),
                                          DEFAULT_ENTER_CONFIRM, 1),
                exit_confirm=_config_int(fs_config.get('exit_confirm'),
                                         DEFAULT_EXIT_CONFIRM, 1),
                tolerance=_config_int(fs_config.get('tolerance'), DEFAULT_TOLERANCE, 0),
                extra_processes=fs_config.get('except_processes') or (),
                # 打包后 sys.executable 就是本程序；源码运行时它是 python.exe，
                # 这时不能按进程名排除自己，否则 pygame 之类用 python 跑的全屏
                # 程序会被一起放过（dock 自己的窗口另有 hwnd 兜底）。
                own_process_name=(os.path.basename(sys.executable)
                                  if getattr(sys, 'frozen', False) else ""),
            )
            self._fs_worker.state_changed.connect(self._on_fullscreen_state_changed)
            self._fs_worker.scan_failed.connect(self._on_fullscreen_scan_failed)
            self._sync_fullscreen_ignored_windows()
            self._fs_thread_id = self.thread_manager.create(
                name="dock_fullscreen_watch",
                start_when_create=True,
                worker=self._fs_worker,
            )
            log.info("全屏程序监听已启动")
        except Exception as e:
            # 线程管理器有数量上限（默认 16），注册失败不该影响 dock 本体
            log.warning(f"启动全屏程序监听失败: {e}")
            self._fs_worker = None
            self._fs_thread_id = None

    def _stop_fullscreen_watch(self):
        """停止全屏程序监听，并把 dock 从挂起态恢复回来。

        设置界面里关掉开关时走到这里；先停线程再恢复，避免恢复过程中又被新的
        检测结果按回挂起态。
        """
        worker = getattr(self, '_fs_worker', None)
        thread_id = getattr(self, '_fs_thread_id', None)
        self._fs_worker = None
        self._fs_thread_id = None
        try:
            if thread_id and hasattr(self, 'thread_manager'):
                self.thread_manager.destroy(thread_id)
            elif worker is not None:
                worker.stop()
        except Exception as e:
            log.warning(f"停止全屏程序监听失败: {e}")
        finally:
            self.exit_fullscreen_suppression()
            log.info("全屏程序监听已停止")

    def _sync_fullscreen_ignored_windows(self):
        """把 dock 与扩展窗口自己的句柄告诉监听线程（永远不会被当成全屏程序）。"""
        worker = getattr(self, '_fs_worker', None)
        if worker is None:
            return
        hwnds = [self.hwnd] if self.hwnd else []
        extension_hwnd = getattr(getattr(self, '_extension', None), 'hwnd', None)
        if extension_hwnd:
            hwnds.append(extension_hwnd)
        try:
            worker.set_ignored_hwnds(hwnds)
        except Exception as e:
            log.debug(f"同步忽略窗口失败: {e}")

    def _on_fullscreen_scan_failed(self, message):
        """监听线程的检测异常（只记录，不打断界面）。"""
        log.error(f"全屏程序检测失败: {message}")

    def _on_fullscreen_state_changed(self, is_fullscreen, description):
        """监听线程报来的状态翻转（信号已排队到 GUI 线程）。"""
        if is_fullscreen:
            self.enter_fullscreen_suppression(description)
        else:
            self.exit_fullscreen_suppression()

    def enter_fullscreen_suppression(self, description=""):
        """有非系统程序全屏显示：注销 AppBar 并隐藏 dock。

        顺序是「先注销 AppBar，再隐藏窗口」。AppBar 的宿主窗口是另一个隐藏窗口
        （见 core/sys32.py），所以两步互不依赖；先注销是为了让工作区尽早还给
        全屏窗口，不给它留一帧被保留区挤压的机会。

        两步各自兜异常：任何一步失败都不能让状态卡住——真出错时至少用户还能正常
        用他的全屏程序。
        """
        if getattr(self, '_fs_suppressed', False):
            return
        self._fs_suppressed = True
        self._fs_last_description = description or ""
        log.info(f"[全屏] 检测到全屏程序 {description or '(未知)'}：注销 AppBar 并隐藏 dock")

        # 提示条是独立的置顶窗口，不主动收起会残留在全屏画面上
        try:
            self.hide_icon_tooltip()
        except Exception as e:
            log.debug(f"隐藏图标提示时出错: {e}")

        try:
            if sys32.is_appbar_registered():
                sys32.remove_appbar()
        except Exception as e:
            log.error(f"注销 AppBar 失败: {e}")

        try:
            self.hide()
        except Exception as e:
            log.error(f"隐藏 dock 窗口失败: {e}")

        # 扩展窗口与 dock 是同一个视觉整体，必须一起让位（它没有自己的 AppBar
        # 保留区，让位后同样不能留在全屏画面上）
        self._set_extension_visible(False)

    def exit_fullscreen_suppression(self):
        """全屏程序已退出：重新注册 AppBar 并显示 dock。"""
        if not getattr(self, '_fs_suppressed', False):
            return
        self._fs_suppressed = False
        log.info(f"[全屏] 全屏程序已退出（{self._fs_last_description or '未知'}）："
                 "重新注册 AppBar 并显示 dock")

        # 挂起期间可能换过显示器/分辨率，重新注册前先按最新指标算一次
        try:
            sys32.refresh_metrics()
            screen = QApplication.primaryScreen()
            dpr = screen.devicePixelRatio() if screen is not None else 1.0
            sys32.set_appbar_bottom(round(self._dock_target_y() * dpr))
        except Exception as e:
            log.error(f"重新注册 AppBar 失败: {e}")

        try:
            # 挂起期间按钮可能增减过，清掉动画目标让 update_window_position 重新算
            self._geom_anim_target = None
            self.update_window_position()
            self.show()
            self._ensure_dock_visible()
        except Exception as e:
            log.error(f"恢复 dock 显示失败: {e}")
        finally:
            # 扩展窗口跟着 dock 一起恢复；放在 finally 里，上面任何一步失败也
            # 不至于"dock 回来了、扩展窗口还藏着"
            self._set_extension_visible(True)
            self._fs_last_description = ""

    def _dock_target_y(self):
        """dock 顶端的逻辑 Y 坐标（= 注册 AppBar 前的工作区底部 - 窗口高度）。

        和 :meth:`update_window_position` 用同一个算式，保证"重新注册 AppBar 的
        位置"与"窗口实际位置"始终一致；这里读的是启动时保存的原始工作区底部，
        所以反复注销/注册不会累积漂移。
        """
        work_bottom = getattr(self, '_original_work_area_bottom', 0)
        if not work_bottom:
            screen = QApplication.primaryScreen()
            work_bottom = screen.availableGeometry().bottom() if screen is not None else 0
        return work_bottom - DockConstants.WINDOW_HEIGHT


    def handle_app_click(self, app_data):
        """处理应用按钮点击事件 - 添加状态立即更新"""
        app_name = app_data['name']
        app_path = app_data['path']
        
        # 使用进程管理器检查应用是否正在运行
        if app_name in self.running_apps or self.process_manager.is_process_running(app_path):
            # 如果正在运行，激活窗口
            self.activate_window(app_path)
        else:
            # 如果未运行，启动应用
            try:
                # 启动前立即更新状态（避免启动延迟导致的显示问题）
                self.running_apps[app_name] = app_path
                button = self.get_app_button(app_name)
                if button:
                    self.set_button_style(button, True)
                
                # 启动应用
                self.launch_app(app_path)
                
                # 延迟检查一次确保状态正确
                QTimer.singleShot(1000, self.check_running_processes)
                
            except Exception as e:
                # 如果启动失败，回滚状态
                if app_name in self.running_apps:
                    del self.running_apps[app_name]
                button = self.get_app_button(app_name)
                if button:
                    self.set_button_style(button, False)
                sys32.messagebox("错误", f"无法启动应用: {str(e)}", sys32.MB_ICONSTOP | sys32.MB_OK)

    def get_app_button(self, app_name):
        """获取指定应用名称的按钮引用，适用于所有类型的应用"""
        if app_name in self.app_buttons:
            return self.app_buttons[app_name]
        elif app_name in self.pinned_app_buttons:
            return self.pinned_app_buttons[app_name]
        elif app_name in self.running_app_buttons:
            return self.running_app_buttons[app_name]
        return None

    def _extract_app_name(self, file_path: str) -> str:
        """从文件路径提取应用名（快捷方式和可执行文件统一处理）"""
        return os.path.splitext(os.path.basename(file_path))[0]

    def _generate_unique_app_name(self, base_name: str) -> str:
        """生成不与已有应用重名的唯一应用名，重名时添加 (1), (2)... 后缀"""
        name = base_name
        counter = 1
        while any(app['name'] == name for app in self.apps):
            name = f"{base_name} ({counter})"
            counter += 1
        return name

    def add_running_app_to_dock(self, app_data):
        """将运行中的应用添加到程序栏"""
        # 检查是否已存在相同路径的应用
        for app in self.apps:
            if app['path'] == app_data['path']:
                sys32.messagebox("提示", "该应用已存在", sys32.MB_ICONINFORMATION)
                return
        
        # 检查是否与固定应用重复
        for app in self.pinned_apps:
            if app['path'] == app_data['path']:
                sys32.messagebox("提示", "该应用已在固定列表中", sys32.MB_ICONINFORMATION)
                return
        
        # 从运行中应用列表中移除（避免重复）
        self.running_apps_list = [app for app in self.running_apps_list if app['path'] != app_data['path']]
        
        base_name = self._extract_app_name(app_data['path'])
        app_name = self._generate_unique_app_name(base_name)
        
        new_app = {
            'name': app_name,
            'path': app_data['path'],
            'icon': app_data['icon']
        }
        self.apps.append(new_app)
        
        self.save_settings()
        self.update_app_buttons()

    def activate_window(self, app_path):
        """激活已运行的应用窗口（取第一个可见窗口）"""
        visible_windows = self.process_manager.get_app_visible_windows(app_path)
        if visible_windows:
            hwnd, _ = visible_windows[0]
            self._bring_window_to_top(hwnd)
        else:
            log.warning(f"未找到应用 {app_path} 的可见窗口")

    def activate_specific_window(self, hwnd):
        """激活指定的窗口句柄"""
        self._bring_window_to_top(hwnd)

    def _bring_window_to_top(self, hwnd):
        """将指定窗口置顶并恢复（如果最小化）"""
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            log.info(f"窗口 {win32gui.GetWindowText(hwnd)} 已成功激活")
        except Exception as e:
            log.error(f"激活窗口时出错: {e}")

    def init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle("MikaDock")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.ToolTip)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.installEventFilter(self)
        self.setStyleSheet(DockConstants.MAIN_WINDOW_STYLE)

        # 创建中央窗口部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 创建主布局
        self.main_layout = QVBoxLayout(central_widget)
        self.main_layout.setContentsMargins(15, 15, 15, 15)
        self.main_layout.setSpacing(5)

        # 创建内容布局
        self.content_layout = QHBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(DockConstants.BUTTON_SPACING)

        # 添加菜单按钮
        self.menu_button = self.create_special_button(
            os.path.join(self.script_dir, "res", "icon_start.png"),
            self.show_menu,
            right_click_handler=self.show_menu
        )
        self.content_layout.addWidget(self.menu_button)

        # 创建固定应用按钮容器
        self.pinned_app_container = QWidget()
        self.pinned_app_layout = QHBoxLayout(self.pinned_app_container)
        self.pinned_app_layout.setContentsMargins(0, 0, 0, 0)
        self.pinned_app_layout.setSpacing(10)  # 设置最小间距为10
        # 设置容器非透明样式
        self.pinned_app_container.setStyleSheet(DockConstants.CONTAINER_STYLE)
        
        # 创建分隔符
        self.separator = QWidget()
        self.separator.setFixedWidth(2)  # 设置分隔符宽度为2像素
        self.separator.setStyleSheet(DockConstants.SEPARATOR_STYLE)
        
        # 创建用户添加应用按钮容器
        self.app_container = QWidget()
        self.app_layout = QHBoxLayout(self.app_container)
        self.app_layout.setContentsMargins(0, 0, 0, 0)
        self.app_layout.setSpacing(10)  # 设置最小间距为10
        # 设置容器非透明样式
        self.app_container.setStyleSheet(DockConstants.CONTAINER_STYLE)

        # 创建运行中应用的分隔符
        self.running_separator = QWidget()
        self.running_separator.setFixedWidth(2)  # 设置分隔符宽度为2像素
        self.running_separator.setStyleSheet(DockConstants.SEPARATOR_STYLE)

        # 创建运行中应用按钮容器
        self.running_app_container = QWidget()
        self.running_app_layout = QHBoxLayout(self.running_app_container)
        self.running_app_layout.setContentsMargins(0, 0, 0, 0)
        self.running_app_layout.setSpacing(10)  # 设置最小间距为10
        # 设置容器非透明样式
        self.running_app_container.setStyleSheet(DockConstants.CONTAINER_STYLE)

        # 将容器添加到内容布局
        self.content_layout.addWidget(self.pinned_app_container)
        self.content_layout.addWidget(self.separator)
        self.content_layout.addWidget(self.app_container, 1)
        self.content_layout.addWidget(self.running_separator)
        self.content_layout.addWidget(self.running_app_container)
        # 添加设置按钮
        settings_layout = QHBoxLayout()
        settings_layout.addStretch()
        self.settings_button = self.create_special_button(
            os.path.join(self.script_dir, "res", "icon_settings.png"),
            self.open_settings
        )
        settings_layout.addWidget(self.settings_button)
        self.content_layout.addLayout(settings_layout)

        self.main_layout.addLayout(self.content_layout)
        self.init_tooltip()

    def start_xht(self):
        """创建并显示小黑条窗口。

        把 dock 的线程管理器传进去，通知监听线程就会登记在同一处，退出时一起收尾。
        """
        self.xht_window = XHTWindow.Window(
            config=self.all_settings.get("xht"),
            elements=self.xhtelements,
            logger=log,
            thread_manager=self.thread_manager,
        )
        self.xht_window.show()

    def update_window_position(self):
        """更新窗口位置 - 根据应用数量自动调整宽度（使用动画平滑过渡）

        dock 与右侧扩展窗口在同一个算式里布局：两者作为一个整体居中（几何计算见
        ``core/dock_extension.layout_rects``，纯函数便于测试）；扩展窗口的高度与
        位置再由 ``_follow_extension`` 按 dock 的真实矩形贴合。
        """
        # 使用可用几何（工作区）而不是整个屏幕几何
        available_geometry = QApplication.primaryScreen().availableGeometry()
        
        # 计算所需宽度：菜单按钮 + 固定应用按钮 + 分隔符 + 用户应用按钮 + 运行应用分隔符 + 运行应用按钮 + 设置按钮 + 间距
        pinned_button_count = len(self.pinned_apps)
        user_button_count = len(self.apps)
        running_button_count = len(self.running_apps_list)
        button_width = DockConstants.BUTTON_SIZE
        button_spacing = DockConstants.BUTTON_SPACING  # 按钮间间距
        separator_width = DockConstants.SEPARATOR_WIDTH  # 分隔符宽度
        margin = DockConstants.WINDOW_MARGIN  # 边距
        
        # 基础宽度：菜单按钮 + 设置按钮 + 边距
        base_width = DockConstants.BUTTON_SIZE + DockConstants.BUTTON_SIZE + (margin * 2)  # 菜单按钮 + 设置按钮 + 左右边距
        # 固定应用按钮总宽度：按钮数量 * 按钮宽度 + 间距
        pinned_apps_width = pinned_button_count * button_width
        if pinned_button_count > 0:
            pinned_apps_width += (pinned_button_count - 1) * button_spacing  # 按钮间间距
        # 用户应用按钮总宽度：按钮数量 * 按钮宽度 + 间距
        user_apps_width = user_button_count * button_width
        if user_button_count > 0:
            user_apps_width += (user_button_count - 1) * button_spacing  # 按钮间间距
        # 运行中应用按钮总宽度：按钮数量 * 按钮宽度 + 间距
        running_apps_width = running_button_count * button_width
        if running_button_count > 0:
            running_apps_width += (running_button_count - 1) * button_spacing  # 按钮间间距
        
        # 根据分隔符可见性计算实际宽度
        separator1_width = DockConstants.SEPARATOR_WIDTH if (hasattr(self, 'separator') and self.separator.isVisible()) else 0
        separator2_width = DockConstants.SEPARATOR_WIDTH if (hasattr(self, 'running_separator') and self.running_separator.isVisible()) else 0
        
        # dock 按内容算出的期望宽度（上限在 layout_rects 里连同扩展窗口一起收）
        total_width = base_width + pinned_apps_width + separator1_width + user_apps_width + separator2_width + running_apps_width
        # Qt 布局会强制一个最小尺寸（内容 + 边距 + 间距），比上面按按钮累加的值大；
        # 目标矩形用真实尺寸，否则窗口显示后又被 Qt 撑宽，整体会偏离中心（扩展窗口
        # 也会跟着偏）。这一步必须同步激活布局再读，否则刚增删完按钮时读到的还是
        # 上一次的最小尺寸（见 _refresh_layout_minimum / _relax_minimum_size）。
        layout_min = self._refresh_layout_minimum()
        if layout_min is not None and layout_min.isValid():
            total_width = max(total_width, int(layout_min.width()))

        # 将窗口放置在可用几何的底部：使用保存的原始工作区底部
        # （AppBar 注册后 available_geometry 会变化）
        work_bottom = getattr(self, '_original_work_area_bottom', 0) or available_geometry.bottom()

        extension = getattr(self, '_extension', None)
        extension_width = extension.extension_width() if extension is not None else 0

        # 主窗口 + 扩展窗口的目标矩形（一并居中、等高）
        target_rect, extension_rect = dock_extension.layout_rects(
            available_geometry, total_width, extension_width,
            DockConstants.WINDOW_HEIGHT, work_bottom,
        )
        # 高度也要对齐 Qt 真正强制的最小高度（布局最小高度是 90，名义常量是 48）。
        # 否则 current_rect == target_rect 永远不成立，每轮轮询都会重启动画。
        # 注意只加高矩形、不动 y：AppBar 保留区的上边界始终是
        # work_bottom - WINDOW_HEIGHT（_dock_target_y 的算式）。
        if layout_min is not None and layout_min.isValid():
            target_rect.setHeight(max(target_rect.height(), int(layout_min.height())))

        # 如果窗口尚未显示，直接设置几何（避免首次不可见时的动画问题）
        if not self.isVisible():
            self.setGeometry(target_rect)
            # 扩展窗口按 dock 的**实际**矩形贴合（不是按估算的目标矩形），
            # 两者之间固定留 EXTENSION_GAP，不会重叠
            self._follow_extension()
            return
        
        # 如果当前几何与目标相同，不重复动画
        current_rect = self.geometry()
        if current_rect == target_rect:
            self._follow_extension()
            return

        animation = self.geometry_anim
        animating = (isinstance(animation, QPropertyAnimation)
                     and animation.state() == QAbstractAnimation.Running)
        target_unchanged = getattr(self, '_geom_anim_target', None) == target_rect

        # 正在往同一个目标做动画：不重启动画。进程轮询每 500ms 会走到这里，
        # 反复 stop()/start() 会让窗口一直停在动画中途，看上去就是持续抖动。
        if target_unchanged and animating:
            return

        # 目标没变但已经不在动画中（上一轮动画被 Qt 用旧的最小尺寸夹住、没到位）：
        # 直接落位一次把它纠回来。否则窗口会一直偏宽，多出来的宽度被
        # app_container（唯一带 stretch 的项）吞掉，那一组按钮间距就会忽大忽小。
        if target_unchanged:
            log.debug(f"几何未到目标（{current_rect} != {target_rect}），直接落位")
            self.setGeometry(target_rect)
            self._follow_extension()
            return

        self._geom_anim_target = target_rect

        # 复用同一个动画对象，避免每次重建（下同：只在缺失时创建）
        if not isinstance(self.geometry_anim, QPropertyAnimation):
            self.geometry_anim = QPropertyAnimation(self, b"geometry", self)
            self.geometry_anim.setDuration(DockConstants.GEOMETRY_ANIM_DURATION)
            self.geometry_anim.setEasingCurve(QEasingCurve.OutCubic)
            # 动画结束没到位就补一次（Qt 可能用旧的最小尺寸把动画帧夹回去）
            self.geometry_anim.finished.connect(self._on_geometry_anim_finished)

        try:
            self.geometry_anim.stop()
        except Exception as e:
            log.debug(f"停止几何动画时出错: {e}")
        self.geometry_anim.setStartValue(current_rect)
        self.geometry_anim.setEndValue(target_rect)
        self.geometry_anim.setEasingCurve(QEasingCurve.OutCubic)
        
        self.geometry_anim.start()

        # 扩展窗口不需要自己的动画：动画期间 dock 每移动/改变一帧都会触发
        # moveEvent/resizeEvent，_follow_extension() 就贴在它右侧一起滑过去
        self._follow_extension()

    # ------------------------------------------------------------------ #
    # 布局最小尺寸：Qt 是异步更新的，这里同步取一次并用它算目标矩形
    # ------------------------------------------------------------------ #
    def _refresh_layout_minimum(self):
        """同步激活布局并返回它当前的最小尺寸（读不到返回 ``None``）。

        按钮增删、分组显隐会把布局标脏，但重算走的是稍后才处理的 LayoutRequest。
        刚改完内容就调用 ``minimumSizeHint()`` 会拿到**上一次**的最小尺寸，于是目标
        宽度还是旧值、窗口根本不收缩；等布局真正更新后也没人再重排，窗口就一直偏宽
        （多出来的宽度被 app_container 吞掉 → 那一组按钮间距忽大忽小）。这里先把
        布局同步激活，再读到真实值。
        """
        try:
            layout = self.layout()
            if layout is not None:
                layout.activate()
            central = self.centralWidget()
            if central is not None and central.layout() is not None:
                central.layout().activate()
            size = self.minimumSizeHint()
        except Exception as e:
            log.debug(f"同步布局最小尺寸失败: {e}")
            return None
        self._relax_minimum_size(size)
        return size

    def _relax_minimum_size(self, layout_min):
        """布局最小尺寸变小时，同步放低窗口自身的 ``minimumSize``。

        布局把最小尺寸写到窗口上也是异步的：内容变少后窗口的 ``minimumSize`` 还停在
        旧的大值上，紧接着的 ``setGeometry``/几何动画会被它夹回去（窗口缩不动）。
        只放低、不抬高 —— 放大方向不需要这个（目标比旧最小尺寸大，本来就不受夹）。
        """
        try:
            if not layout_min.isValid():
                return
            if self.minimumWidth() > layout_min.width():
                self.setMinimumWidth(layout_min.width())
            if self.minimumHeight() > layout_min.height():
                self.setMinimumHeight(layout_min.height())
        except Exception as e:
            log.debug(f"同步窗口最小尺寸失败: {e}")

    def _on_geometry_anim_finished(self):
        """几何动画结束：没到目标就补一次落位，并让扩展窗口跟上来。"""
        target = getattr(self, '_geom_anim_target', None)
        if target is not None and self.geometry() != target:
            log.debug(f"几何动画结束未到位 {self.geometry()} -> {target}，直接落位")
            self.setGeometry(target)
        self._follow_extension()


    
    def create_special_button(self, icon_path: str, click_handler, right_click_handler=None) -> QPushButton:
        """创建特殊按钮（菜单、设置等）"""
        button = QPushButton()
        button.setFixedSize(DockConstants.BUTTON_SIZE, DockConstants.BUTTON_SIZE)
        
        if os.path.exists(icon_path):
            button.setIcon(QIcon(icon_path))
            button.setIconSize(QSize(DockConstants.ICON_SIZE, DockConstants.ICON_SIZE))
        
        button.setStyleSheet(DockConstants.BUTTON_STYLE_INACTIVE)
        button.clicked.connect(click_handler)
        
        if right_click_handler:
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(right_click_handler)
        
        return button



    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(DockConstants.COLOR_BACKGROUND))
        painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
        painter.drawRoundedRect(self.rect(), DockConstants.WINDOW_BORDER_RADIUS, DockConstants.WINDOW_BORDER_RADIUS)
    


    def add_application(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择应用程序", 
            "", 
            "可执行文件 (*.exe *.bat *.com);;所有文件 (*)"
        )
        
        if file_path:
            icon_path = self.process_manager.extract_icon(file_path)
            
            for app in self.apps:
                if app['path'] == file_path:
                    sys32.messagebox("提示", "该应用已存在", sys32.MB_ICONINFORMATION | sys32.MB_OK)
                    return
            
            for app in self.pinned_apps:
                if app['path'] == file_path:
                    sys32.messagebox("提示", "该应用已在固定列表中", sys32.MB_ICONINFORMATION | sys32.MB_OK)
                    return
            
            base_name = self._extract_app_name(file_path)
            app_name = self._generate_unique_app_name(base_name)
            
            self.apps.append({
                'name': app_name,
                'path': file_path,
                'icon': icon_path
            })
            
            self.save_settings()
            self.update_app_buttons()


    def init_tooltip(self):
        """创建图标悬浮提示条（实现见 core/dock_tooltip.py）。"""
        self.tooltip = DockTooltip(None)

    def show_icon_tooltip(self, button, text):
        tooltip = getattr(self, 'tooltip', None)
        if tooltip is not None:
            tooltip.show_for(button, text)

    def hide_icon_tooltip(self):
        tooltip = getattr(self, 'tooltip', None)
        if tooltip is not None:
            tooltip.hide_tip()

    def update_icon_tooltip_position(self, button):
        tooltip = getattr(self, 'tooltip', None)
        if tooltip is not None:
            tooltip.follow(button)

    def clear_layout(self, layout: QHBoxLayout) -> None:
        """清空布局中的所有部件"""
        for i in reversed(range(layout.count())):
            widget = layout.itemAt(i).widget()
            if widget is not None:
                layout.removeWidget(widget)
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _assign_uid(self, app_data: Dict[str, Any]) -> str:
        """为应用数据分配唯一标识符，确保图标-按钮一对一绑定"""
        if '_uid' not in app_data:
            app_data['_uid'] = str(uuid.uuid4())
        return app_data['_uid']

    def _compute_list_hash(self, app_list: List[Dict[str, Any]]) -> str:
        """计算应用列表的内容哈希（基于稳定字段 path+name），用于版本比对"""
        content = '|'.join(
            f"{app.get('path', '')}::{app.get('name', '')}"
            for app in app_list
        )
        return hashlib.md5(content.encode()).hexdigest()

    def _rebuild_section(self, app_list: List[Dict[str, Any]],
                         button_dict: Dict[str, QPushButton],
                         layout: QHBoxLayout,
                         is_running_section: bool = False) -> None:
        """重建单个分区的所有按钮"""
        self.clear_layout(layout)
        button_dict.clear()
        for app in app_list:
            self.create_app_button(app, button_dict, layout, is_running_app=is_running_section)

    def _validate_button_positions(self) -> None:
        """校验按钮位置完整性：检查绑定有效、位置对应、容器不溢出"""
        sections = [
            (self.pinned_apps, self.pinned_app_buttons, self.pinned_app_layout, 'pinned'),
            (self.apps, self.app_buttons, self.app_layout, 'apps'),
            (self.running_apps_list, self.running_app_buttons, self.running_app_layout, 'running'),
        ]
        for app_list, button_dict, layout, name in sections:
            if not app_list and not button_dict:
                continue
            widget_count = layout.count()
            expected_count = len(app_list)
            if widget_count != expected_count:
                log.warning(f"[{name}] 按钮数({widget_count})与应用数({expected_count})不匹配")
                continue
            bound_uids = set()
            for i in range(widget_count):
                widget = layout.itemAt(i).widget()
                if widget is None:
                    log.warning(f"[{name}] 位置 {i} 为空")
                    continue
                uid = getattr(widget, '_bound_uid', None)
                if uid is None:
                    log.warning(f"[{name}] 位置 {i} 按钮缺少绑定UID")
                    continue
                if uid in bound_uids:
                    log.warning(f"[{name}] 按钮UID重复: {uid}")
                bound_uids.add(uid)

    def update_app_buttons(self) -> None:
        """增量更新所有应用按钮 - 仅当列表哈希变化时重建对应分区"""
        sections = [
            ('pinned', self.pinned_apps, self.pinned_app_buttons, self.pinned_app_layout, False),
            ('apps', self.apps, self.app_buttons, self.app_layout, False),
            ('running', self.running_apps_list, self.running_app_buttons, self.running_app_layout, True),
        ]
        any_rebuilt = False
        for section_name, app_list, button_dict, layout, is_running_section in sections:
            new_hash = self._compute_list_hash(app_list)
            if self._list_versions.get(section_name) != new_hash:
                log.debug(f"[{section_name}] 版本变化，重建按钮")
                self._rebuild_section(app_list, button_dict, layout, is_running_section)
                self._list_versions[section_name] = new_hash
                any_rebuilt = True

        if any_rebuilt:
            self._update_container_visibility()
            self._validate_button_positions()
            self.update_window_position()

    def _update_container_visibility(self) -> None:
        """更新容器和分隔符的可见性"""
        pinned_apps_visible = len(self.pinned_apps) > 0
        user_apps_visible = len(self.apps) > 0
        running_apps_visible = len(self.running_apps_list) > 0
        
        self.pinned_app_container.setVisible(pinned_apps_visible)
        self.app_container.setVisible(user_apps_visible)
        self.running_app_container.setVisible(running_apps_visible)
        
        self.separator.setVisible(pinned_apps_visible and user_apps_visible)
        self.running_separator.setVisible(user_apps_visible and running_apps_visible)

    def set_button_style(self, button, is_running):
        """设置按钮样式，根据运行状态"""
        if is_running:
            button.setStyleSheet(DockConstants.BUTTON_STYLE_RUNNING)
        else:
            button.setStyleSheet(DockConstants.BUTTON_STYLE_INACTIVE)


    def launch_app(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            sys32.messagebox("错误", f"无法启动应用: {str(e)}", sys32.MB_ICONSTOP | sys32.MB_OK)

    def terminate_app_process(self, app_data):
        """终止应用进程"""
        # 使用进程管理器终止应用
        self.process_manager.terminate_app_process(app_data['path'])
        
        # 延迟检查进程状态
        QTimer.singleShot(1000, self.check_running_processes)

    def show_app_context_menu(self, pos, app_data, sender=None):
        # sender 必须显式传入（来自 lambda），用于精确计算图标全局矩形
        try:
            self.hide_icon_tooltip()
        except Exception as e:
            log.debug(f"隐藏图标提示时出错: {e}")

        # 构建动作列表
        actions = []
        is_running_app = any(app['path'] == app_data['path'] for app in self.running_apps_list)
        try:
            is_running = self.process_manager.is_process_running(app_data.get('path', ''))
        except Exception:
            is_running = False
        visible_windows = self.process_manager.get_app_visible_windows(app_data.get('path', ''))
        if is_running:
            if visible_windows:
                for hwnd, title in visible_windows:
                    label = f"{title[:40]}{'...' if len(title) > 40 else ''}"
                    callback = lambda h=hwnd: self.activate_specific_window(h)
                    actions.append((label, callback, True))
            else:
                actions.append(("没有可用窗口", None, False))
        if is_running_app:
            actions.append(("添加到程序栏", lambda: self.add_running_app_to_dock(app_data), True))
            actions.append((app_data['name'], lambda: self.handle_app_click(app_data), True))
        elif not app_data.get('is_pinned', False):
            actions.append(("删除应用", lambda: (self.remove_app(app_data)), True))
            actions.append(("修改应用名", lambda: (self.rename_app(app_data)), True))
            actions.append(("更改图标", lambda: (self.change_app_icon(app_data)), True))
        else:
            actions.append((app_data['name'], lambda: self.launch_app(app_data['path']), True))
        if is_running:
            if visible_windows:
                actions.append(("关闭窗口", lambda: self.close_app_window(app_data), True))
            actions.append(("关闭应用", lambda: self.terminate_app_process(app_data), True))

        # 创建并显示自定义弹窗，使用传入的 sender 作为锚点（始终居中在图标上方）
        popup = ContextPopup(actions, parent=None)
        popup.show_at_position(pos, sender)

    def close_app_window(self, app_data):
        """关闭应用窗口。

        实现收敛到 :meth:`ProcessManager.close_app_window`（按完整路径匹配，
        不会因为同名 exe 误关别的进程），这里只负责关完再刷新一次状态。
        """
        try:
            self.process_manager.close_app_window(app_data['path'])
            # 延迟检查进程状态
            QTimer.singleShot(1000, self.check_running_processes)
        except Exception as e:
            log.error(f"关闭窗口时出错: {e}")

    def remove_app(self, app_data):
        reply = sys32.messagebox(
            "确认",
            f"确定要删除应用 '{app_data['name']}' 吗？",
            sys32.MB_YESNO | sys32.MB_ICONQUESTION
        )

        if reply == sys32.IDYES:
            # 按路径（稳定标识）删除，避免对象/内容比较失败导致 ValueError
            app_path = app_data.get('path')
            self.apps = [a for a in self.apps if a.get('path') != app_path]
            # 如果应用正在运行，从运行列表中移除
            if app_data['name'] in self.running_apps:
                del self.running_apps[app_data['name']]
            self.save_settings()
            self.update_app_buttons()

    def rename_app(self, app_data):
        """修改应用名称"""
        current_name = app_data['name']
        new_name, ok = QInputDialog.getText(
            self, 
            "修改应用名", 
            "输入应用名称:", 
            text=current_name
        )
        
        if ok and new_name.strip():
            new_name = self._generate_unique_app_name(new_name.strip())
            
            # 更新应用名称
            app_data['name'] = new_name
            
            # 更新按钮引用
            if current_name in self.app_buttons:
                button = self.app_buttons[current_name]
                del self.app_buttons[current_name]
                self.app_buttons[new_name] = button
            
            self.save_settings()
            self.update_app_buttons()

    def change_app_icon(self, app_data):
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择图标文件", 
            "", 
            "图标文件 (*.ico *.png *.jpg *.bmp);;所有文件 (*)"
        )
        
        if file_path:
            if os.path.exists(file_path):
                app_data['icon'] = file_path
                self.save_settings()
                self.update_app_buttons()
            else:
                sys32.messagebox("错误", "选择的图标文件不存在", sys32.MB_ICONWARNING | sys32.MB_OK)

    def open_settings(self):
        """打开设置对话框"""
        self.dialog = settings.SettingsUI(
            version=VERSION,
            config_path=self.settings_file,
            on_save_callback=self.on_settings_saved,
        )
        self.dialog.show()

    def on_settings_saved(self, config_data):
        """设置保存后的回调"""
        dock_config = config_data.get('dock', {})
        except_list = dock_config.get('except_processes', [])
        # 空列表是有效意图（用户主动清空排除列表），所以这里不做真值判断
        if hasattr(self, 'process_manager') and self.process_manager:
            try:
                self.process_manager.set_except_processes(except_list)
            except Exception:
                pass

        debug_enabled = config_data.get('debug', False)
        if debug_enabled:
            log.enable_debug()
        else:
            log.disable_debug()

        if config_data.get('nocmd_mode', False):
            log.warning("cmd被禁用")
            self.is_cmd_disabled = True
        else:
            self.is_cmd_disabled = False

        # XHT 的通知提示等设置即时生效，不必重启
        xht_config = config_data.get('xht')
        if xht_config and getattr(self, 'xht_window', None) is not None:
            try:
                self.xht_window.config = xht_config
                self.xht_window.RefreshConfig()
                log.info("XHT 配置已刷新")
            except Exception as e:
                log.warning(f"刷新 XHT 配置失败: {e}")

        # 全屏让位设置同样即时生效（开关会启停监听线程，排除列表直接更新）
        self._apply_fullscreen_settings(config_data.get('fullscreen') or {})

        log.info("设置已更新")

    def _apply_fullscreen_settings(self, fs_config):
        """把设置界面里的全屏让位配置应用到运行中的监听线程。"""
        enabled = bool(fs_config.get('enabled', True))
        if enabled and getattr(self, '_fs_worker', None) is None:
            self._start_fullscreen_watch()
        elif not enabled and getattr(self, '_fs_worker', None) is not None:
            self._stop_fullscreen_watch()
        elif getattr(self, '_fs_worker', None) is not None:
            try:
                self._fs_worker.set_extra_processes(fs_config.get('except_processes') or ())
                log.info("全屏让位排除列表已刷新")
            except Exception as e:
                log.warning(f"刷新全屏让位排除列表失败: {e}")

    def load_settings(self):
        try:
            # 确保配置文件存在（不存在时写入默认配置）
            Config.check(self.settings_file)
            self.all_settings = Config.load_config(self.settings_file)
            if self.all_settings.get('nocmd_mode', False):
                log.warning("cmd被禁用")
                self.is_cmd_disabled = True
            else:
                self.is_cmd_disabled = False
        
            # 从 dock栏配置部分获取数据
            dock_config = self.all_settings.get('dock', {})
            self.apps = dock_config.get('apps', [])
            
            # 加载 ProcessManager 的排除进程设置
            # （load_config 已保证该键存在，空列表 = 用户清空了排除列表）
            except_list = dock_config.get('except_processes', [])
            if hasattr(self, 'process_manager') and self.process_manager:
                try:
                    self.process_manager.set_except_processes(except_list)
                except Exception:
                    pass
            

            
            # 确保加载设置后更新应用按钮
            self.update_app_buttons()
        except Exception as e:
            log.error(f"加载配置文件 {self.settings_file} 时出错")
            self.apps = []  # 出错时使用默认设置
            self.update_app_buttons()

    def save_settings(self):
        try:
            self.all_settings = Config.load_config(self.settings_file)
            config = self.all_settings
            config['dock']['apps'] = self.apps
            config['dock']['except_processes'] = getattr(self.process_manager, 'except_processes', [])
            Config.save_config(self.settings_file, config)
        except Exception as e:
            self.handle_error(f"保存配置文件 {self.settings_file} 时出错")

    def show_menu(self, pos):
        """显示菜单按钮的菜单"""
        # 构建动作列表
        actions = [
            ("命令提示符", self.open_terminal, True),
            ("命令提示符（管理员）", self.open_terminal_admin, True),
            ("任务管理器", self.open_task_manager, True),
            ("电源操作", self.show_shutdown_menu, True),
            ("添加应用到程序栏", self.add_application, True),
            ("退出", self.exit_app, True),
        ]
        
        # 创建并显示自定义弹窗，使用菜单按钮作为锚点
        popup = ContextPopup(actions, parent=None)
        popup.show_at_position(pos, self.sender())

    def open_terminal(self):
        """打开命令提示符"""
        try:
            os.startfile("cmd.exe")
        except Exception as e:
            self.handle_error(f"打开命令提示符失败: {e}")

    def open_terminal_admin(self):
        """打开管理员命令提示符"""
        try:
            subprocess.run(["powershell", "-Command", "Start-Process", "cmd.exe", "-Verb", "RunAs"])
        except Exception as e:
            self.handle_error(f"打开管理员命令提示符失败: {e}")

    def open_task_manager(self):
        """打开任务管理器"""
        try:
            import features.process_mgr as process_mgr
            if self.is_cmd_disabled:
                # 复用已存在的采集线程与窗口，避免重复创建（ThreadManager 上限 16）
                if self._process_mgr_worker is None:
                    self._process_mgr_worker = process_mgr.ProcessCollectorWorker()
                    self._process_mgr_thread_id = self.thread_manager.create(
                        name=self._process_mgr_worker.get_name(),
                        start_when_create=True,
                        worker=self._process_mgr_worker,
                    )
                process_mgr.run(collector=self._process_mgr_worker)
            else:
                subprocess.run(["taskmgr.exe"])
        except Exception as e:
            self.handle_error(f"打开任务管理器失败: {e}")

    def show_shutdown_menu(self):
        """显示关机或注销对话框"""
        try:
            dialog = ShutdownDialog(self)
            if dialog.exec() == QDialog.Accepted:
                action = dialog.selected_action

                action_names = {
                    "logout": "注销",
                    "shutdown": "关机",
                    "restart": "重启",
                    "hibernate": "休眠"
                }

                reply = sys32.messagebox(
                    "确认操作",
                    f"确定要执行{action_names[action]}操作吗？\n\n请确保已保存所有工作！",
                    sys32.MB_YESNO | sys32.MB_ICONQUESTION,
                )

                if reply == sys32.IDYES:
                    if action == "logout":
                        subprocess.run(["shutdown.exe", "/l"])
                    elif action == "shutdown":
                        subprocess.run(["shutdown.exe", "/s", "/t", "0"])
                    elif action == "restart":
                        subprocess.run(["shutdown.exe", "/r", "/t", "0"])
                    elif action == "hibernate":
                        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "Hibernate"])
        except Exception as e:
            self.handle_error(f"显示关机对话框失败: {e}")

    def exit_app(self):
        """清理资源并退出应用"""
        self.save_settings()
        gc.collect()
        try:
            # 注销 AppBar，恢复原始工作区
            sys32.remove_appbar()

            # 先显式停掉进程扫描线程：它跑的是自己的 while 循环，显式 stop() 比
            # 依赖 stop_all() 更确定（注册失败退化直启时 stop_all 并不知道它）。
            scan_worker = getattr(self, '_scan_worker', None)
            if scan_worker is not None:
                try:
                    scan_worker.stop()
                except Exception as e:
                    log.error(f"停止进程扫描线程时出错: {e}")

            # 全屏监听同理；并且要先让它停止发信号，避免退出过程中又去动 AppBar
            fs_worker = getattr(self, '_fs_worker', None)
            if fs_worker is not None:
                try:
                    fs_worker.stop()
                except Exception as e:
                    log.error(f"停止全屏监听线程时出错: {e}")
            self._fs_worker = None

            # 扩展窗口的状态轮询同理（未登记进管理器时是直接启动的，需要显式停）
            status_worker = getattr(self, '_status_worker', None)
            if status_worker is not None:
                try:
                    status_worker.stop()
                except Exception as e:
                    log.error(f"停止系统状态线程时出错: {e}")
                self._status_worker = None

            # 使用统一的线程管理器停止所有后台服务
            if hasattr(self, 'thread_manager') and self.thread_manager:
                try:
                    self.thread_manager.stop_all()
                    log.info("所有后台服务已停止")
                except Exception as e:
                    log.error(f"停止后台服务时出错: {e}")

            # 停止时间更新定时器
            if hasattr(self, 'time_timer') and self.time_timer:
                self.time_timer.stop()

            # 重启explorer.exe
            sys32.show_window(sys32.HWND_TRAY)
            
            log.info("应用程序已清理资源并退出")
            sys.exit(0)
            
        except Exception as e:
            log.error(f"退出应用时出错: {e}")
            # 重启explorer.exe
            sys32.show_window(sys32.HWND_TRAY)
            os._exit(0)

    def closeEvent(self, event):
        event.ignore()  # 忽略关闭事件，因为应用程序不应该真正退出

    def showEvent(self, event):
        super().showEvent(event)
        if self.hwnd is None:
            self.hwnd = int(self.winId())
            # dock 自己的窗口永远不该被当成"全屏程序"（扩展窗口同理，见
            # _sync_fullscreen_ignored_windows）
            self._sync_fullscreen_ignored_windows()
        # 窗口被系统恢复显示时，确保 Topmost 层级正确；扩展窗口一起恢复
        self._ensure_dock_visible()


# ========== 单实例保护 ==========
# 用命名互斥体确保同一时间只有一个 dock 进程。多个实例会各自注册 AppBar、各自
# 隐藏/显示系统任务栏：隐藏与恢复交错执行，任务栏的最终状态不可预测（也正是
# 「点了退出任务栏却没回来」的诱因）。这里直接拒绝第二个实例。
#
# 用会话内（Local）命名空间即可：dock 是每个登录会话各跑一个的桌面程序，不需要
# 跨会话全局唯一；Global\ 命名空间反而可能因缺少 SeCreateGlobalPrivilege 而创建失败。
_SINGLE_INSTANCE_MUTEX_NAME = "MikaDesktop_SingleInstance_Mutex"
# 必须持有句柄直到进程结束，否则被 GC 回收 → 互斥体提前释放，保护失效
_single_instance_mutex = None


def _acquire_single_instance() -> bool:
    """尝试成为唯一实例；若已有实例在运行则返回 False。

    创建失败（例如环境异常）时一律返回 True 放行：宁可多开一个，也不要因为
    单实例机制本身出问题而让程序无法启动。
    """
    global _single_instance_mutex
    try:
        handle = win32event.CreateMutex(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    except Exception as e:
        log.warning(f"创建单实例互斥体失败（{e}），跳过单实例检查")
        return True

    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        # 已有实例：只需关掉这个多余的句柄，绝不能去动系统任务栏 ——
        # 任务栏此刻是被那个正在运行的实例隐藏的，显示出来会把它顶掉。
        try:
            win32api.CloseHandle(handle)
        except Exception:
            pass
        return False

    _single_instance_mutex = handle
    return True


def _start_taskbar_watchdog():
    """启动独立的任务栏看门狗进程，兜底「主进程被强杀」时任务栏无法恢复。

    看门狗是独立进程：本进程被任务管理器「结束任务」强杀（``TerminateProcess``）后
    它仍存活，会在主进程一消失时把系统任务栏显示回来。
    """
    try:
        if getattr(sys, "frozen", False):
            watchdog = os.path.join(os.path.dirname(sys.executable), "MikaWatchdog.exe")
            if not os.path.exists(watchdog):
                log.warning(f"未找到任务栏看门狗程序，跳过：{watchdog}")
                return
            args = [watchdog, str(os.getpid())]
        else:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "taskbar_watchdog.py")
            args = [sys.executable, script, str(os.getpid())]

        # DETACHED_PROCESS：不要控制台窗口；CREATE_NEW_PROCESS_GROUP：让看门狗
        # 脱离本进程的控制台/信号组，避免主进程退出时被一起带走。
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        log.info("任务栏看门狗已启动")
    except Exception as e:
        log.warning(f"启动任务栏看门狗失败：{e}")


def main():
    # 单实例检查必须放在隐藏任务栏之前：第二个实例若先隐藏了任务栏再退出，
    # 会在退出时把它显示出来，反而破坏了正在运行的那个实例的界面。
    if not _acquire_single_instance():
        log.info("检测到已有 MikaDesktop 实例在运行，本次启动退出")
        return

    sys32.hide_window(sys32.HWND_TRAY)
    _start_taskbar_watchdog()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 防止关闭主窗口时退出应用
    app.setApplicationName("MikaDock")

    dock = DockApp()
    dock.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
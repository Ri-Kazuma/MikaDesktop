"""扩展窗口里的状态面板：网络 / 音量 / 电源 + 通知中心入口。

布局（与 dock 的按钮同尺寸，高度天然等于 dock）：

    [网络] [音量] [电源?] │ [通知中心]

* **网络 / 音量 / 电源**：点击打开系统原生「快速设置」（Win11）或等效面板
  （Win10，见 :func:`core.system_status.open_quick_settings`）；tooltip 显示状态，
  电池按钮在**没有电池的机器上不显示**（宽度随之变化）。
* **通知中心**：点击打开系统通知中心。

状态由 :class:`core.system_status.SystemStatusWorker` 在后台线程轮询，结果通过
``status_changed`` 投递回 GUI 线程，本面板只负责"照着快照改图标和 tooltip"。
所有取值/文案映射都是纯函数（:func:`network_tooltip` 之类），便于单测。
"""

from __future__ import annotations

import os
from typing import Any, Dict

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from .. import system_status
from ..custom_ui import IconHoverFilter
from .dock_constants import DockConstants
from .dock_tooltip import DockTooltip

__all__ = [
    "ExtensionPanel",
    "network_icon_name",
    "network_tooltip",
    "volume_icon_name",
    "volume_tooltip",
    "battery_icon_name",
    "battery_tooltip",
]

_MEDIUM_TEXT = {"wifi": "无线", "ethernet": "有线"}


# --------------------------------------------------------------------------- #
# 状态 -> 图标 / 文案（纯函数，可直接单测）
# --------------------------------------------------------------------------- #
def network_icon_name(info: Dict[str, Any]) -> str:
    """网络图标文件名（未知状态用中性的 Wi-Fi 图标，别一上来就显示"断开"）。"""
    if info and info.get("connected") is False:
        return "icon_network_off.png"
    if info and info.get("medium") == "ethernet":
        return "icon_network_eth.png"
    return "icon_network.png"


def network_tooltip(info: Dict[str, Any]) -> str:
    """网络 tooltip：连接对象 + 介质 + 是否联网。"""
    if not info:
        return "网络：未知"
    if info.get("error") and info.get("connected") is None:
        return f"网络：读取失败（{info['error']}）"
    if info.get("connected") is False:
        return "网络：未连接"
    name = info.get("name") or ""
    medium = _MEDIUM_TEXT.get(info.get("medium", ""), "")
    parts = [p for p in (name, medium) if p]
    detail = "，".join(parts)
    if info.get("internet") is False:
        suffix = "已连接，但无 Internet 访问"
    else:
        suffix = "已连接互联网"
    return "网络：%s（%s）" % (detail, suffix) if detail else f"网络：{suffix}"


def volume_icon_name(info: Dict[str, Any]) -> str:
    """音量图标文件名（未知状态用中性图标）。"""
    if not info:
        return "icon_volume_high.png"
    if info.get("muted"):
        return "icon_volume_mute.png"
    percent = info.get("percent")
    if percent is None:
        return "icon_volume_high.png"
    if percent <= 0:
        return "icon_volume_mute.png"
    return "icon_volume_low.png" if percent < 50 else "icon_volume_high.png"


def volume_tooltip(info: Dict[str, Any]) -> str:
    if not info:
        return "音量：未知"
    if info.get("percent") is None:
        return f"音量：读取失败（{info.get('error') or '不可用'}）"
    if info.get("muted"):
        return f"音量：{info['percent']}%（已静音）"
    return f"音量：{info['percent']}%"


def battery_icon_name(power: Dict[str, Any]) -> str:
    if not power:
        return "icon_battery_empty.png"
    if power.get("charging"):
        return "icon_battery_charging.png"
    percent = power.get("percent")
    if percent is None:
        return "icon_battery_empty.png"
    if percent >= 80:
        return "icon_battery_full.png"
    if percent >= 40:
        return "icon_battery_mid.png"
    if percent >= 15:
        return "icon_battery_low.png"
    return "icon_battery_empty.png"


def battery_tooltip(power: Dict[str, Any]) -> str:
    """电源 tooltip（只在有电池的机器上调用）。"""
    if not power:
        return "电源：未知"
    percent = power.get("percent")
    percent_text = "未知" if percent is None else f"{percent}%"
    if power.get("charging"):
        state = "充电中"
    elif power.get("ac"):
        state = "已接通电源"
    elif power.get("ac") is False:
        state = "使用电池"
    else:
        state = "电源状态未知"
    return f"电源：{percent_text}（{state}）"


# --------------------------------------------------------------------------- #
# 面板
# --------------------------------------------------------------------------- #
class ExtensionPanel(QWidget):
    """扩展窗口的内容部件。"""

    #: 内容宽度变化（电源按钮显隐）时发出，dock 据此重排
    preferred_width_changed = Signal(int)

    def __init__(self, res_dir: str, status_worker=None, parent=None,
                 status_api=system_status, log=None):
        super().__init__(parent)
        self.res_dir = res_dir
        #: 后台轮询线程（由 DockApp 交给统一线程管理器启动；可为 None，用于测试）
        self.status_worker = status_worker
        #: 注入点：单测可以塞一个假的状态模块进来，完全不碰真实系统
        self.api = status_api
        self.log = log
        self._status: Dict[str, Any] = {}
        self._hovered = None
        self._battery_visible = None

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.tooltip = DockTooltip(None)
        self.hover_filter = IconHoverFilter(self)

        self._build_ui()
        if self.status_worker is not None:
            self.status_worker.status_changed.connect(self.apply_status)
        # 先同步问一次电池在不在：决定电源按钮一开始显不显示（纯 ctypes，很快）
        try:
            self.apply_status({"power": self.api.power_status()})
        except Exception:
            pass

    # -- 构建界面 ---------------------------------------------------------- #
    def _icon(self, name: str) -> QIcon:
        path = os.path.join(self.res_dir, name)
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _add_button(self, name: str) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(DockConstants.BUTTON_SIZE, DockConstants.BUTTON_SIZE)
        button.setIconSize(QSize(DockConstants.ICON_SIZE, DockConstants.ICON_SIZE))
        button.setStyleSheet(DockConstants.EXTENSION_BUTTON_STYLE)
        button.setMouseTracking(True)
        button.setAttribute(Qt.WA_Hover, True)
        button.installEventFilter(self.hover_filter)
        button.setProperty("panel_name", name)
        self._layout.addWidget(button)
        return button

    def _build_ui(self) -> None:
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(DockConstants.BUTTON_SPACING)

        # 网络 / 音量 / 电源
        self.network_button = self._add_button("network")
        self.network_button.clicked.connect(lambda: self._open_panel("network"))

        self.volume_button = self._add_button("volume")
        self.volume_button.clicked.connect(lambda: self._open_panel("volume"))

        self.battery_button = self._add_button("battery")
        self.battery_button.clicked.connect(lambda: self._open_panel("power"))

        # 分隔符 + 通知中心入口
        self.separator = QWidget()
        self.separator.setFixedWidth(DockConstants.SEPARATOR_WIDTH)
        self.separator.setStyleSheet(DockConstants.SEPARATOR_STYLE)
        self._layout.addWidget(self.separator)

        self.notify_button = self._add_button("notify")
        self.notify_button.setIcon(self._icon("icon_notify.png"))
        self.notify_button.clicked.connect(self._on_notify_clicked)

        self._refresh_tooltips()

    # -- tooltip（复用 dock 的悬浮过滤器约定） ----------------------------- #
    def show_icon_tooltip(self, button, text: str) -> None:
        self._hovered = button
        self.tooltip.show_for(button, text)

    def hide_icon_tooltip(self) -> None:
        self._hovered = None
        self.tooltip.hide_tip()

    def update_icon_tooltip_position(self, button) -> None:
        self.tooltip.follow(button)

    # -- 状态刷新 ---------------------------------------------------------- #
    def apply_status(self, snapshot: Dict[str, Any]) -> None:
        """按后台快照更新图标 / tooltip / 电源按钮显隐。"""
        if not snapshot:
            return
        self._status.update(snapshot)
        power = self._status.get("power") or {}
        network = self._status.get("network") or {}
        volume = self._status.get("volume") or {}

        self.network_button.setIcon(self._icon(network_icon_name(network)))
        self.volume_button.setIcon(self._icon(volume_icon_name(volume)))

        battery_present = bool(power.get("present"))
        if battery_present:
            self.battery_button.setIcon(self._icon(battery_icon_name(power)))
        self._set_battery_visible(battery_present)

        # 状态由图标本身表达（新图标是 make_app_icon 模板合成的彩色字形：断网红、
        # 电量红/橙/浅绿/绿），按钮只保留 dock 统一的描边样式，不再额外加"激活描边"，
        # 否则会和图标的颜色语义打架（例如红色断网图标套一圈高亮边框）。

        self._refresh_tooltips()

    def _refresh_tooltips(self) -> None:
        power = self._status.get("power") or {}
        self.network_button.setToolTip(network_tooltip(self._status.get("network") or {}))
        self.volume_button.setToolTip(volume_tooltip(self._status.get("volume") or {}))
        self.battery_button.setToolTip(battery_tooltip(power))
        self.notify_button.setToolTip("通知中心")
        # 鼠标正悬停在某个按钮上时，立刻把新文案刷出来（否则要移开再进才更新）
        if self._hovered is not None:
            text = self._hovered.toolTip()
            if text != self.tooltip.text():
                self.tooltip.show_for(self._hovered, text)

    def _set_battery_visible(self, visible: bool) -> None:
        # 用自己记的状态判断，别用 isVisible()：父窗口还没显示时它永远是 False
        if self._battery_visible == visible:
            return
        self._battery_visible = visible
        self.battery_button.setVisible(visible)
        # 隐藏的部件不参与布局 → 面板自然宽度变化，通知 dock 重排
        self._layout.activate()
        self.preferred_width_changed.emit(self.preferred_width())

    def preferred_width(self) -> int:
        """面板自然宽度（由布局算出）。"""
        return int(self.sizeHint().width())

    # -- 交互 -------------------------------------------------------------- #
    def _open_panel(self, kind: str) -> None:
        try:
            self.api.open_quick_settings(kind)
        except Exception as e:
            if self.log:
                self.log.warning(f"打开快速设置失败: {e}")

    def _on_notify_clicked(self) -> None:
        try:
            self.api.open_notification_center()
        except Exception as e:
            if self.log:
                self.log.warning(f"打开通知中心失败: {e}")

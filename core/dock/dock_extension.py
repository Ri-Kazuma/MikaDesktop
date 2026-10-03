"""dock 右侧的扩展窗口。

主 dock 栏右侧挂一个和它等高、同样置顶、同样外观的扩展窗口：启动时以最小宽度
（150）注册，宽度可放大到 600，内容由后续功能填充（当前为空）。

设计要点：

* **一并布局**：:func:`layout_rects` 是纯函数，一次算出 dock 与扩展窗口的目标
  矩形 —— 两者作为一个整体居中，``y`` 与高度完全相同；dock 的内容宽度上限也会
  为扩展窗口留位置（不再是只看自己占屏宽 90%）。窗口真正落位时再由
  :meth:`DockExtensionWindow.follow_dock` 按 **dock 的实际矩形** 贴到它右侧
  （Qt 布局可能把 dock 撑得比估算值更宽，用实测值才不会重叠），两者的间隙
  固定为 :data:`EXTENSION_GAP`。
* **一并让位**：扩展窗口没有自己的 AppBar 保留区（它落在 dock 已经注册的底部
  AppBar 条带内部），显隐完全跟随 dock：全屏程序让位时一起隐藏、一起把保留区
  还给全屏窗口，恢复时一起显示；它的窗口句柄也会进全屏判定的忽略列表，避免被
  当成"全屏程序"而触发让位循环。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .dock_constants import DockConstants

EXTENSION_MIN_WIDTH = DockConstants.EXTENSION_MIN_WIDTH
EXTENSION_MAX_WIDTH = DockConstants.EXTENSION_MAX_WIDTH
EXTENSION_GAP = DockConstants.EXTENSION_GAP

#: 扩展窗口内容的四周边距（与 dock 的 ``main_layout`` 一致）
CONTENT_MARGIN = 15

__all__ = [
    "EXTENSION_MIN_WIDTH",
    "EXTENSION_MAX_WIDTH",
    "EXTENSION_GAP",
    "CONTENT_MARGIN",
    "clamp_extension_width",
    "layout_rects",
    "DockExtensionWindow",
]


def clamp_extension_width(width) -> int:
    """把请求宽度夹到 ``[150, 600]``；非法值退回最小宽度。"""
    try:
        value = int(width)
    except (TypeError, ValueError):
        return EXTENSION_MIN_WIDTH
    return max(EXTENSION_MIN_WIDTH, min(EXTENSION_MAX_WIDTH, value))


def layout_rects(available: QRect, dock_width: int, extension_width: int = 0,
                 window_height: int = None, work_bottom: int = None,
                 gap: int = EXTENSION_GAP):
    """算出 dock 与扩展窗口的目标矩形（两者一并居中）。

    Args:
        available: 可用几何（逻辑像素；AppBar 注册**之前**的工作区）。
        dock_width: dock 的期望宽度（内容估算值，调用方已与 Qt 最小宽度取较大者；
            这里还可能被可用宽度上限压缩）。
        extension_width: 扩展窗口宽度；``0`` 或负数表示没有扩展窗口。
        window_height: 两个窗口共用的高度，默认 :data:`DockConstants.WINDOW_HEIGHT`。
        work_bottom: 定位用的工作区底部 Y（AppBar 注册前保存的原值），
            窗口底边贴着它。
        gap: dock 与扩展窗口之间的间隙。

    Returns:
        ``(dock_rect, extension_rect)``；没有扩展窗口时 ``extension_rect`` 为
        ``None``。

    宽度上限：整个系统（dock + 间隙 + 扩展窗口）不超过可用宽度的 90%，dock 至少
    保留两个按钮的宽度（菜单 + 设置），避免屏幕很窄时 dock 被压没。居中后若起点
    落在可用几何左侧，则贴左摆放。
    """
    height = int(DockConstants.WINDOW_HEIGHT if window_height is None else window_height)

    try:
        extension_w = int(extension_width or 0)
    except (TypeError, ValueError):
        extension_w = 0
    has_extension = extension_w > 0
    if has_extension:
        extension_w = clamp_extension_width(extension_w)
    group_gap = int(gap) if has_extension else 0

    max_group = int(available.width() * 0.9)
    max_dock = max(max_group - group_gap - extension_w, DockConstants.BUTTON_SIZE * 2)
    try:
        requested_dock_w = int(dock_width)
    except (TypeError, ValueError):
        requested_dock_w = max_dock
    dock_w = max(min(requested_dock_w, max_dock), 0)

    group_w = dock_w + group_gap + extension_w
    x = available.x() + (available.width() - group_w) // 2
    if x < available.x():
        x = available.x()

    bottom = available.bottom() if work_bottom is None else int(work_bottom)
    y = bottom - height

    dock_rect = QRect(x, y, dock_w, height)
    if not has_extension:
        return dock_rect, None
    return dock_rect, QRect(x + dock_w + group_gap, y, extension_w, height)


class DockExtensionWindow(QWidget):
    """dock 右侧的扩展窗口：高度与 dock 相同，宽度限制在 ``[150, 600]``。

    当前不显示任何内容，只注册窗口本身（启动宽度 = 最小宽度）。位置/高度由
    :meth:`follow_dock` 按 dock 的实际几何决定（dock 的 move/resize 事件里调用），
    后续往里放内容时调用 :meth:`set_extension_width` 改宽度即可 —— dock 会跟着
    重新排列两者。
    """

    #: 窗口第一次真正显示出来（句柄可用）时发出，dock 用它刷新全屏判定的忽略列表
    shown_signal = Signal()
    #: 内容推荐宽度变化（例如电源按钮显隐）时发出，dock 据此重排两者
    width_hint_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("MikaDockExtension")
        # 与 XHT 同样的窗口属性：无边框、置顶、不进任务栏、不抢焦点
        # （Qt.Tool 在 Windows 上带 WS_EX_TOOLWINDOW，不会出现在任务栏/Alt+Tab）
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 恢复显示时不该把焦点从用户当前窗口抢走（全屏程序退出瞬间尤其明显）
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 只约束宽度下限/上限；高度由 follow_dock() 跟随 dock 的真实高度
        # （dock 的窗口高度是布局算出来的，和常量不一定相等，所以不能写死）
        self.setMinimumWidth(EXTENSION_MIN_WIDTH)
        self.setMaximumWidth(EXTENSION_MAX_WIDTH)
        self.setMinimumHeight(DockConstants.WINDOW_HEIGHT)

        # 期望宽度（启动 = 最小宽度）；布局一律以它为准
        self._width = EXTENSION_MIN_WIDTH
        self.resize(self._width, DockConstants.WINDOW_HEIGHT)

        self._content = None
        self._content_layout = None
        self.hwnd = None

    # ------------------------------------------------------------------ #
    # 内容
    # ------------------------------------------------------------------ #
    def set_content(self, widget) -> None:
        """放入内容部件（如状态面板），并让窗口宽度贴合内容。"""
        if self._content_layout is None:
            from PySide6.QtWidgets import QVBoxLayout

            self._content_layout = QVBoxLayout(self)
            self._content_layout.setContentsMargins(
                CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN)
            self._content_layout.setSpacing(0)
        self._content = widget
        # 居中对齐：内容高度固定（60），窗口高度跟着 dock（90），居中最好看
        self._content_layout.addWidget(widget, 0, Qt.AlignCenter)
        # 内容自己知道宽度变了（比如电源按钮显隐）时同步窗口宽度
        hint_signal = getattr(widget, "preferred_width_changed", None)
        if hint_signal is not None and hasattr(hint_signal, "connect"):
            hint_signal.connect(self._on_content_width_hint)
        self.fit_to_content()

    def content_width(self) -> int:
        """按内容自然尺寸算出的窗口宽度（含内容边距）；没有内容时返回当前宽度。"""
        if self._content is None or self._content_layout is None:
            return self._width
        self._content_layout.activate()
        return int(self._content.sizeHint().width()) + CONTENT_MARGIN * 2

    def fit_to_content(self) -> int:
        """把窗口宽度调整成内容需要的宽度（夹到 ``[150, 600]``）。"""
        width = clamp_extension_width(self.content_width())
        changed = width != self._width
        self._width = width
        self.resize(width, self.height())
        if changed:
            self.width_hint_changed.emit(width)
        return width

    def _on_content_width_hint(self, _width: int) -> None:
        self.fit_to_content()

    # ------------------------------------------------------------------ #
    # 尺寸与跟随
    # ------------------------------------------------------------------ #
    @staticmethod
    def extension_height() -> int:
        """dock 的名义高度（常量）；实际高度以 dock 的真实几何为准。"""
        return int(DockConstants.WINDOW_HEIGHT)

    def extension_width(self) -> int:
        """当前期望宽度（始终在 ``[150, 600]`` 内）。"""
        return self._width

    def set_extension_width(self, width) -> int:
        """设置期望宽度，返回夹取后的实际宽度。"""
        self._width = clamp_extension_width(width)
        self.resize(self._width, self.height())
        return self._width

    def follow_dock(self, dock_rect: QRect, gap: int = EXTENSION_GAP) -> QRect:
        """贴到 dock **实际**矩形的右侧：左边 = dock 右边界 + 间隙，顶边、高度与 dock 相同。

        位置一律由 dock 的真实几何算出来（而不是按内容估算的目标矩形）：Qt 布局会
        把 dock 撑到它的最小尺寸，估算值可能偏小，直接拿估算值算 x 会让两个窗口
        重叠。dock 移动 / 改变大小时（含宽度动画的每一帧）调用本方法，扩展窗口
        就会严丝合缝地跟着走，间隙始终是 :data:`EXTENSION_GAP`。

        Returns:
            本窗口的新矩形，便于调用方断言 / 记录。
        """
        rect = QRect(int(dock_rect.right()) + 1 + int(gap), int(dock_rect.top()),
                     self._width, int(dock_rect.height()))
        self.setGeometry(rect)
        return rect

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def showEvent(self, event):
        super().showEvent(event)
        if self.hwnd is None and self.isWindow():
            self.hwnd = int(self.winId())
            self.shown_signal.emit()

    def paintEvent(self, event):
        """与 dock 相同的外观：圆角浅色卡片 + 淡边框。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(DockConstants.COLOR_BACKGROUND))
        painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
        painter.drawRoundedRect(self.rect(), DockConstants.WINDOW_BORDER_RADIUS,
                               DockConstants.WINDOW_BORDER_RADIUS)

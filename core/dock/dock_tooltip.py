"""程序栏图标的悬浮提示条。

从 ``dock.py`` 抽出来：这是一个自带窗口样式与定位逻辑的独立部件，跟主窗口的
其它职责没有关系；单独成模块后主窗口只需要转发调用。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel

from .dock_constants import DockConstants

__all__ = ["DockTooltip"]

#: 提示条与图标之间的垂直间距（像素）
_OFFSET = 8

_OBJECT_NAME = "DockIconTooltip"


class DockTooltip(QLabel):
    """居中对齐在图标上方的小提示条；超出工作区时自动贴边。"""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setObjectName(_OBJECT_NAME)
        self.setWindowFlags(
            Qt.ToolTip | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
        )
        # 提示条本身不能吃鼠标事件，否则会挡住它下面的图标
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(f"""
            QLabel#{_OBJECT_NAME} {{
                color: white;
                font-family: 'Microsoft YaHei UI';
                font-weight: Medium;
                font-size: 14px;
                background-color: {DockConstants.COLOR_TOOLTIP};
                border-radius: 5px;
                padding: 8px 16px;
            }}
        """)
        self.hide()

    def show_for(self, button, text: str) -> None:
        """在 ``button`` 上方显示提示条；``text`` 为空时不做任何事。"""
        if not text:
            return
        self.setText(text)
        self.adjustSize()
        self.follow(button)
        self.show()

    def hide_tip(self) -> None:
        """隐藏提示条（未显示时什么都不做）。"""
        if self.isVisible():
            self.hide()

    def follow(self, button) -> None:
        """重新计算位置：居中放在图标上方，并限制在主屏幕工作区内。"""
        if button is None:
            return

        global_center = button.mapToGlobal(QPoint(button.width() // 2, 0))
        tw = self.width()
        th = self.height()
        x = global_center.x() - tw // 2
        y = global_center.y() - th - _OFFSET

        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(x, y)
            return

        screen_rect = screen.availableGeometry()
        if x < screen_rect.left():
            x = screen_rect.left() + 4
        if x + tw > screen_rect.right():
            x = screen_rect.right() - tw - 4
        if y < screen_rect.top():
            y = global_center.y() + 16  # 上方放不下就放到图标下方
        self.move(x, y)

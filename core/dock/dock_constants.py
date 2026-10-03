"""程序栏的尺寸、配色与样式表常量。

从 ``dock.py`` 抽出来：这些是纯数据，放在主窗口文件里只会让那个文件更长，
而且任何需要样式的地方都得反过来导入主入口。
"""

from __future__ import annotations

__all__ = ["DockConstants"]


class DockConstants:
    """Dock应用常量定义"""
    BUTTON_SIZE = 60
    ICON_SIZE = 48
    BORDER_RADIUS = 16
    WINDOW_BORDER_RADIUS = 18
    BUTTON_SPACING = 10
    WINDOW_MARGIN = 0
    SEPARATOR_WIDTH = 2
    PROCESS_CHECK_INTERVAL = 500   # 进程检查间隔（毫秒）
    GEOMETRY_ANIM_DURATION = 220   # 窗口宽度变化动画时长（毫秒）

    # dock 与右侧扩展窗口共用的窗口高度（见 core/dock_extension.py）
    WINDOW_HEIGHT = ICON_SIZE + WINDOW_MARGIN * 2

    # 右侧扩展窗口：宽度范围与启动宽度、以及和 dock 之间的间隙
    EXTENSION_MIN_WIDTH = 150      # 启动时以这个宽度注册
    EXTENSION_MAX_WIDTH = 600
    EXTENSION_GAP = 12             # dock 与扩展窗口之间的间隙（像素），嫌挤/嫌远改这里

    # 颜色常量（基于UI配色方案）
    COLOR_BACKGROUND = "#F8F9FA"      # Surface - 卡片、输入框背景
    COLOR_HOVER = "#80E0D7"           # Primary Light - 悬停状态
    COLOR_BORDER_ACTIVE = "#39C5BB"   # Primary - 主色
    COLOR_BORDER_INACTIVE = "#ADB5BD" # Text Disabled - 禁用/非活跃边框
    COLOR_BG_ACTIVE = "#39C5BB"       # Primary - 运行中背景
    COLOR_BG_HOVER_ACTIVE = "#80E0D7" # Primary Light - 运行中悬停
    COLOR_BG_HOVER_INACTIVE = "#80E0D7" # Primary Light - 非活跃悬停
    COLOR_SEPARATOR = "#ADB5BD"       # Text Disabled - 分隔符
    COLOR_WINDOW_BORDER = "#212529"   # Text Primary - 窗口边框
    COLOR_TOOLTIP = "#39C5BB"         # Primary - 工具提示

    # 样式表模板
    BUTTON_STYLE_RUNNING = f"""
        QPushButton {{
            border: 2px solid {COLOR_BORDER_ACTIVE};
            border-radius: {BORDER_RADIUS}px;
            background-color: {COLOR_BG_ACTIVE};
        }}
        QPushButton:hover {{
            border: 2px solid {COLOR_BORDER_ACTIVE};
            background-color: {COLOR_BG_HOVER_ACTIVE};
        }}
    """

    BUTTON_STYLE_INACTIVE = f"""
        QPushButton {{
            border: 2px solid {COLOR_BORDER_INACTIVE};
            border-radius: {BORDER_RADIUS}px;
            background-color: {COLOR_BACKGROUND};
        }}
        QPushButton:hover {{
            border: 2px solid {COLOR_BORDER_ACTIVE};
            background-color: {COLOR_BG_HOVER_INACTIVE};
        }}
    """

    CONTAINER_STYLE = f"""
        QWidget {{
            background-color: {COLOR_BACKGROUND};
            border-radius: {BORDER_RADIUS}px;
        }}
    """

    SEPARATOR_STYLE = f"""
        QWidget {{
            background-color: {COLOR_SEPARATOR};
            border-radius: 1px;
        }}
    """

    MAIN_WINDOW_STYLE = f"""
        QMainWindow {{
            background: {COLOR_BACKGROUND};
            border: 1px solid {COLOR_WINDOW_BORDER};
            border-radius: {WINDOW_BORDER_RADIUS}px;
        }}
        QPushButton {{
            border: none;
            border-radius: {BORDER_RADIUS}px;
            background-color: {COLOR_BACKGROUND};
        }}
        QPushButton:hover {{
            background-color: {COLOR_HOVER};
        }}
    """

    # 时间标签样式
    TIME_LABEL_STYLE = f"""
        QLabel {{
            color: #212529;
            font-family: 'Microsoft YaHei UI';
            font-weight: 600;
            font-size: 16px;
            background-color: transparent;
            padding: 0 8px;
        }}
    """

    # 扩展窗口里的状态按钮：与 dock 按钮同一套描边。
    # 状态由图标本身表达（状态图标是 make_app_icon 模板合成的彩色字形），所以没有
    # "激活态"变体 —— 再叠一层主色描边会和图标颜色语义打架。
    EXTENSION_BUTTON_STYLE = f"""
        QPushButton {{
            border: 2px solid {COLOR_BORDER_INACTIVE};
            border-radius: {BORDER_RADIUS}px;
            background-color: {COLOR_BACKGROUND};
        }}
        QPushButton:hover {{
            border: 2px solid {COLOR_BORDER_ACTIVE};
            background-color: {COLOR_HOVER};
        }}
        QPushButton:pressed {{
            border: 2px solid {COLOR_BORDER_ACTIVE};
            background-color: {COLOR_BG_ACTIVE};
        }}
    """

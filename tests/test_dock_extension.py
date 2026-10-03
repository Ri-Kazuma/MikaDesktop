"""扩展窗口回归：尺寸夹取、与 dock 一并布局、让位联动。

dock 右侧的扩展窗口高度必须和 dock 相同、宽度限制在 150~600（启动即最小宽度），
并且和 dock 作为一个整体居中。布局算式在 ``core/dock_extension.layout_rects``
里是纯函数，这里直接对它做断言，不需要真的启动 dock（也就不动系统的 AppBar）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from core.dock import dock_extension
from core.dock.dock_constants import DockConstants
from core.dock.dock_extension import (
    EXTENSION_GAP,
    EXTENSION_MAX_WIDTH,
    EXTENSION_MIN_WIDTH,
    DockExtensionWindow,
    clamp_extension_width,
    layout_rects,
)

# ---------------------------------------------------------------- 常量与夹取
check("扩展窗口最小宽度为 150", EXTENSION_MIN_WIDTH == 150)
check("扩展窗口最大宽度为 600", EXTENSION_MAX_WIDTH == 600)
check("常量与 DockConstants 一致",
      DockConstants.EXTENSION_MIN_WIDTH == 150 and DockConstants.EXTENSION_MAX_WIDTH == 600)
check("dock 与扩展窗口共用高度常量",
      DockConstants.WINDOW_HEIGHT == DockConstants.ICON_SIZE + DockConstants.WINDOW_MARGIN * 2)

check("低于下限夹到 150", clamp_extension_width(10) == 150)
check("高于上限夹到 600", clamp_extension_width(9999) == 600)
check("范围内原样返回", clamp_extension_width(320) == 320)
check("非法值退回最小宽度", clamp_extension_width("abc") == 150)
check("None 退回最小宽度", clamp_extension_width(None) == 150)

# ---------------------------------------------------------------- 窗口本体
window = DockExtensionWindow()
check("启动时以最小宽度注册", window.extension_width() == 150 and window.width() == 150,
      "width=%d" % window.width())
check("高度与 dock 名义高度相等", window.height() == DockConstants.WINDOW_HEIGHT,
      "height=%d expect=%d" % (window.height(), DockConstants.WINDOW_HEIGHT))
check("最小高度为名义高度（不会缩没）",
      window.minimumHeight() == DockConstants.WINDOW_HEIGHT)
check("宽度上下限已设置",
      window.minimumWidth() == EXTENSION_MIN_WIDTH and window.maximumWidth() == EXTENSION_MAX_WIDTH)
check("初始不可见（由 dock 统一显示）", not window.isVisible())

check("设置宽度在范围内生效", window.set_extension_width(320) == 320 and window.width() == 320)
check("设置宽度超过上限被夹到 600",
      window.set_extension_width(5000) == 600 and window.width() == 600)
check("设置宽度低于下限被夹到 150",
      window.set_extension_width(1) == 150 and window.width() == 150)
check("扩展窗口不会抢焦点",
      bool(window.windowFlags() & dock_extension.Qt.WindowDoesNotAcceptFocus))

# 跟随 dock 的**真实**几何：dock 被 Qt 布局撑到 90 高、814 宽时也要贴住不留缝
dock_like = QRect(300, 700, 814, 90)
followed = window.follow_dock(dock_like)
check("follow_dock 贴在 dock 右侧并留出间隙",
      followed.left() == dock_like.right() + 1 + EXTENSION_GAP,
      "dock=%s ext=%s gap=%d" % (dock_like, followed, EXTENSION_GAP))
check("follow_dock 绝不与 dock 重叠", followed.left() > dock_like.right())
check("follow_dock 顶边与高度都跟 dock 一致",
      followed.top() == dock_like.top() and followed.height() == dock_like.height(),
      "dock=%s ext=%s" % (dock_like, followed))
check("follow_dock 不改变设定宽度", followed.width() == window.extension_width() == 150)
check("follow_dock 返回值即窗口新几何", window.geometry() == followed)

# dock 变矮（例如布局变化）时高度跟着变，仍然没有重叠
shorter = window.follow_dock(QRect(100, 780, 500, 48))
check("dock 变矮时扩展窗口跟着变矮",
      shorter.height() == 48 and shorter.top() == 780
      and shorter.left() == QRect(100, 780, 500, 48).right() + 1 + EXTENSION_GAP,
      shorter)

# ---------------------------------------------------------------- 布局算式
HEIGHT = DockConstants.WINDOW_HEIGHT
AVAIL = QRect(0, 0, 1920, 1040)
WORK_BOTTOM = 1040

dock_rect, ext_rect = layout_rects(AVAIL, 600, 150, HEIGHT, WORK_BOTTOM)
check("没有扩展窗口时返回 None", layout_rects(AVAIL, 600, 0, HEIGHT, WORK_BOTTOM)[1] is None)
check("没有扩展窗口时 dock 宽度不变",
      layout_rects(AVAIL, 600, 0, HEIGHT, WORK_BOTTOM)[0].width() == 600)
check("扩展窗口在 dock 右侧", ext_rect.left() > dock_rect.left(), ext_rect)
check("间隙等于 EXTENSION_GAP", ext_rect.left() == dock_rect.right() + 1 + EXTENSION_GAP,
      "dock=%s ext=%s" % (dock_rect, ext_rect))
check("两者高度相同", ext_rect.height() == dock_rect.height() == HEIGHT)
check("两者顶边对齐", ext_rect.top() == dock_rect.top())
check("两者底边都贴着工作区底部",
      dock_rect.bottom() + 1 == WORK_BOTTOM and ext_rect.bottom() + 1 == WORK_BOTTOM)
check("整体居中（左右留白相等）",
      abs((dock_rect.left() - AVAIL.left()) - (AVAIL.right() - ext_rect.right())) <= 1,
      "left=%d right=%d" % (dock_rect.left() - AVAIL.left(), AVAIL.right() - ext_rect.right()))

# dock 宽度上限要为扩展窗口留位置：整体不超过可用宽度的 90%
wide_dock, wide_ext = layout_rects(AVAIL, 100000, 600, HEIGHT, WORK_BOTTOM)
group_width = wide_dock.width() + EXTENSION_GAP + wide_ext.width()
check("dock 内容过宽时为扩展窗口留位置",
      group_width == int(AVAIL.width() * 0.9), "group=%d" % group_width)
check("传给 layout_rects 的宽度超过 600 时也会被夹取",
      layout_rects(AVAIL, 400, 1000, HEIGHT, WORK_BOTTOM)[1].width() == 600)

# 屏幕很窄：整体仍从可用几何左边界开始，dock 至少保留两个按钮宽
narrow = QRect(0, 0, 200, 600)
narrow_dock, narrow_ext = layout_rects(narrow, 200, 150, HEIGHT, 600)
check("窄屏下 dock 至少保留两个按钮宽",
      narrow_dock.width() == DockConstants.BUTTON_SIZE * 2, narrow_dock.width())
check("窄屏下整体贴左不越界", narrow_dock.left() == narrow.left())

# 副屏（可用几何 x 为负）：居中仍基于可用几何原点
left_screen = QRect(-1920, 0, 1920, 1040)
left_dock, left_ext = layout_rects(left_screen, 600, 150, HEIGHT, WORK_BOTTOM)
check("副屏上按可用几何原点居中",
      left_dock.left() - left_screen.left() == AVAIL.right() - ext_rect.right(),
      "dock_left=%d ext_right=%d" % (left_dock.left(), left_ext.right()))
check("副屏上不越出左边界", left_dock.left() >= left_screen.left())

# 自定义间隙生效
gap_dock, gap_ext = layout_rects(AVAIL, 600, 150, HEIGHT, WORK_BOTTOM, gap=24)
check("自定义间隙生效", gap_ext.left() == gap_dock.right() + 1 + 24)

# ---------------------------------------------------------------- 与 dock 方法接线
# 直接借用 DockApp 的真实方法（update_window_position / set_extension_width）
# 在一个只准备所需属性的桩窗口上跑一遍：验证真正的接线，不碰 AppBar / 任务栏 /
# 后台线程，也不需要显示窗口。
import app as dock_module
from PySide6.QtWidgets import QMainWindow, QWidget


class _DockStub(QMainWindow):
    # 只借不依赖 super() 的方法：moveEvent/resizeEvent 里用了零参 super()，绑到
    # 非 DockApp 实例上会 TypeError，所以桩里改为在调用后显式 _follow_extension()
    update_window_position = dock_module.DockApp.update_window_position
    set_extension_width = dock_module.DockApp.set_extension_width
    _follow_extension = dock_module.DockApp._follow_extension
    # update_window_position 现在还会同步布局最小尺寸、并在动画结束时补位
    _refresh_layout_minimum = dock_module.DockApp._refresh_layout_minimum
    _relax_minimum_size = dock_module.DockApp._relax_minimum_size
    _on_geometry_anim_finished = dock_module.DockApp._on_geometry_anim_finished

    def __init__(self):
        super().__init__()
        self.pinned_apps = []
        self.apps = [{"name": "a", "path": "a"}]
        self.running_apps_list = []
        self.separator = QWidget(self)
        self.running_separator = QWidget(self)
        self._original_work_area_bottom = QApplication.primaryScreen().availableGeometry().bottom()
        self.geometry_anim = None
        self._geom_anim_target = None
        self._extension = DockExtensionWindow()


stub = _DockStub()
stub.update_window_position()
stub_dock = stub.geometry()
stub_ext = stub._extension.geometry()
stub_available = QApplication.primaryScreen().availableGeometry()

check("DockApp 布局后扩展窗口在 dock 右侧",
      stub_ext.left() == stub_dock.right() + 1 + EXTENSION_GAP,
      "dock=%s ext=%s" % (stub_dock, stub_ext))
check("DockApp 布局后两者等高、顶边对齐",
      stub_ext.height() == stub_dock.height() == HEIGHT and stub_ext.top() == stub_dock.top())
check("DockApp 布局后整体居中",
      abs((stub_dock.left() - stub_available.left())
          - (stub_available.right() - stub_ext.right())) <= 1,
      "dock=%s ext=%s avail=%s" % (stub_dock, stub_ext, stub_available))
check("DockApp 布局后底边贴着原工作区底部",
      stub_dock.bottom() + 1 == stub._original_work_area_bottom,
      "bottom=%d work_bottom=%d" % (stub_dock.bottom(), stub._original_work_area_bottom))

stub.set_extension_width(9999)
check("DockApp.set_extension_width 夹到 600",
      stub._extension.extension_width() == 600, stub._extension.extension_width())
check("加宽扩展窗口后 dock 左移（整体仍居中）",
      stub.geometry().left() < stub_dock.left(),
      "before=%d after=%d" % (stub_dock.left(), stub.geometry().left()))
check("加宽后两者仍然相接",
      stub._extension.geometry().left()
      == stub.geometry().right() + 1 + EXTENSION_GAP)

stub.set_extension_width(10)
check("DockApp.set_extension_width 夹到 150",
      stub._extension.extension_width() == 150 and stub._extension.width() == 150)

# 没有扩展窗口时整条路径也不能出错（退化行为）
stub._extension = None
stub.update_window_position()
check("没有扩展窗口时 dock 仍能正常摆位",
      stub.geometry().right() < QApplication.primaryScreen().availableGeometry().right() + 1)

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

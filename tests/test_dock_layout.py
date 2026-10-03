"""dock 布局回归：内容增删后窗口必须收缩到新的最小宽度，分组间距恒定。

背景（真机截图实测）：应用列表变化后窗口偶尔**缩不回去**，一直停在偏宽的尺寸上；
多出来的宽度全部被 ``app_container``（content_layout 里唯一带 stretch 的项）吞掉，
在它内部按"每个间隙均分"展开，于是那一组按钮间距忽大忽小（实测 10 → 24px）。

原因有三层，都在 Qt 的异步/夹取行为上：
1. 按钮增删、分组显隐把布局标脏，但重算走稍后处理的 LayoutRequest；
   同一次调用里读 ``minimumSizeHint()`` 还是**旧值** → 目标宽度 = 旧宽度 → 不重排；
2. 窗口自身的 ``minimumSize`` 也异步更新，收缩目标会被它夹回去；
3. "目标没变就不重启动画"的保护把偏宽的几何锁死。

本测试用 offscreen 平台 + 假屏幕跑真实的 DockApp 布局方法（不注册 AppBar、不启动
XHT / 后台线程，不碰用户桌面），断言每个状态下：
窗口宽度 == 布局最小宽度，且三个分组的按钮间距都等于 BUTTON_SPACING。
"""

import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QMainWindow

app = QApplication.instance() or QApplication(sys.argv)

import app as dock_module
from core.dock.dock_constants import DockConstants


class FakeScreen:
    """把可用几何固定成用户那台机器的值（1536x864 逻辑）。"""

    def availableGeometry(self):
        return QRect(0, 0, 1536, 864)

    def devicePixelRatio(self):
        return 1.25


QApplication.primaryScreen = staticmethod(lambda: FakeScreen())

ICON = os.path.join("res", "icon_settings.png")


class Harness(dock_module.DockApp):
    """只借用 DockApp 的界面/布局方法，不执行会碰系统的 __init__。"""

    def __init__(self):
        QMainWindow.__init__(self)
        self.script_dir = ROOT
        self.settings_file = os.path.join(ROOT, "settings.json")
        self.all_settings = {}
        self.icon_hover_filter = dock_module.IconHoverFilter(self)
        self.process_manager = dock_module.ProcessManager()
        self.geometry_anim = None
        self._geom_anim_target = None
        self.xhtelements = []
        self.xht_window = None
        self._extension = None
        self.hwnd = None
        self._fs_suppressed = False
        self._list_versions = {}
        self.app_buttons = {}
        self.pinned_app_buttons = {}
        self.running_app_buttons = {}
        self.running_apps = {}
        self.pinned_apps = []
        self.apps = []
        self.running_apps_list = []
        self._original_work_area_bottom = 864
        self.init_ui()


def fake_app(name):
    return {"name": name, "path": "x", "icon": ICON}


def group_gaps(layout):
    boxes = []
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if widget is not None and not widget.isHidden():
            boxes.append((widget.x(), widget.width()))
    return [boxes[i + 1][0] - (boxes[i][0] + boxes[i][1]) for i in range(len(boxes) - 1)]


def settle(timeout=2.0):
    """跑到几何稳定（动画最多 220ms）：连续 5 次采样不变即认为到位。"""
    end = time.time() + timeout
    last = None
    stable = 0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)
        now = dock.geometry().getRect()
        if now == last:
            stable += 1
            if stable >= 5:
                break
        else:
            stable = 0
            last = now
    return dock.geometry().getRect()


def apply_content(pinned, apps_count, running_count):
    dock.pinned_apps = [dict(fake_app("p%d" % i), is_pinned=True) for i in range(pinned)]
    dock.apps = [fake_app("u%d" % i) for i in range(apps_count)]
    dock.running_apps_list = [fake_app("r%d" % i) for i in range(running_count)]
    dock._list_versions.clear()
    dock.update_app_buttons()


def layout_min_width():
    dock.layout().activate()
    return dock.minimumSizeHint().width()


def expect_settled(tag, pinned, apps_count, running_count):
    """改内容 → 等稳定 → 断言宽度收敛到最小宽度、三个分组间距都正常。"""
    apply_content(pinned, apps_count, running_count)
    rect = settle()
    min_width = layout_min_width()
    ok_width = rect[2] == min_width
    check("%s：窗口宽度收敛到布局最小宽度" % tag, ok_width,
          "width=%d min=%d geom=%s" % (rect[2], min_width, rect))

    for name, layout in (("固定组", dock.pinned_app_layout),
                         ("应用组", dock.app_layout),
                         ("运行组", dock.running_app_layout)):
        gaps = group_gaps(layout)
        check("%s：%s 按钮间距 = %d" % (tag, name, DockConstants.BUTTON_SPACING),
              all(g == DockConstants.BUTTON_SPACING for g in gaps),
              "gaps=%s" % (gaps,))

    # 扩展窗口仍然贴在 dock 右侧、留出固定间隙
    extension = dock._extension
    if extension is not None:
        ext_left = extension.geometry().left()
        check("%s：扩展窗口仍紧贴 dock 右侧" % tag,
              ext_left == rect[0] + rect[2] + DockConstants.EXTENSION_GAP,
              "dock=%s ext.left=%d" % (rect, ext_left))
    return rect


dock = Harness()
dock.show()
settle()

# 起始：3 固定 + 4 用户 + 3 运行（≈ 截图里的内容）
expect_settled("初始 3+4+3", 3, 4, 3)

# 运行组消失（截图状态的前因之一）
expect_settled("去掉运行组", 3, 4, 0)

# 去掉一个用户应用：正好 60 + 10 = 70px 富余 —— 截图里那组 24px 间距的来源
expect_settled("少一个用户应用", 3, 3, 0)

# 再加回来
expect_settled("加回用户应用", 3, 4, 0)
expect_settled("运行组回来", 3, 4, 3)

# 连续快速变化（动画没结束就再次改内容）之后也必须收敛
apply_content(3, 4, 3)
settle()
apply_content(3, 0, 0)      # 立刻缩
apply_content(3, 5, 4)      # 动画中途又变长
rect = settle()
check("快速连续变化后宽度收敛",
      rect[2] == layout_min_width(), "geom=%s min=%d" % (rect, layout_min_width()))
check("快速连续变化后应用组间距正常",
      all(g == DockConstants.BUTTON_SPACING for g in group_gaps(dock.app_layout)),
      group_gaps(dock.app_layout))

# 反复收缩/放大 5 轮，任何一轮都不许停偏
bad = []
for i in range(5):
    apply_content(3, 4, 3)
    settle()
    wide = dock.geometry().getRect()
    if wide[2] != layout_min_width():
        bad.append(("宽", wide, layout_min_width()))
    apply_content(3, 1, 0)
    settle()
    narrow = dock.geometry().getRect()
    if narrow[2] != layout_min_width():
        bad.append(("窄", narrow, layout_min_width()))
check("反复收缩/放大 5 轮都不卡在偏宽状态", not bad, bad)

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

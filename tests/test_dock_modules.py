"""P2 拆分验证：从 dock.py 抽出的常量 / 提示条 / 任务栏固定项解析仍可独立工作。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication(sys.argv)

# ---------- core.dock_constants ----------
from core.dock.dock_constants import DockConstants
from core.dock.dock_tooltip import DockTooltip
from core import pinned_apps
from core.process_manager import ProcessManager
from core.log_maker import logger as _logger_factory

log = _logger_factory()

check("DockConstants.BUTTON_SIZE 已迁移", DockConstants.BUTTON_SIZE == 60)
check("样式表非空且带主色",
      DockConstants.COLOR_BORDER_ACTIVE in DockConstants.BUTTON_STYLE_RUNNING
      and len(DockConstants.MAIN_WINDOW_STYLE) > 50)
check("进程检查间隔保留", DockConstants.PROCESS_CHECK_INTERVAL == 500)
check("几何动画时长保留", DockConstants.GEOMETRY_ANIM_DURATION == 220)

# ---------- core.dock_tooltip ----------
tip = DockTooltip(None)
check("提示条初始不可见", not tip.isVisible())

button = QPushButton("x")
button.resize(60, 60)
button.move(400, 400)
button.show()

tip.show_for(button, "测试提示")
check("show_for 设置文本", tip.text() == "测试提示")
check("show_for 后可见", tip.isVisible())

screen_rect = QApplication.primaryScreen().availableGeometry()
inside = (screen_rect.left() <= tip.x()
          and tip.x() + tip.width() <= screen_rect.right() + 1
          and screen_rect.top() <= tip.y())
check("位置被限制在屏幕工作区内", inside,
      "tip=(%d,%d,%d,%d) screen=%s" % (tip.x(), tip.y(), tip.width(), tip.height(), screen_rect))

tip.hide_tip()
check("hide_tip 后不可见", not tip.isVisible())

tip.show_for(button, "")
check("空文本不显示提示条", not tip.isVisible())

# ---------- core.pinned_apps ----------
check("pinned_taskbar_dir 返回路径字符串或 None",
      pinned_apps.pinned_taskbar_dir() is None
      or isinstance(pinned_apps.pinned_taskbar_dir(), str))

missing_lnk = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "no_such_shortcut_xyz.lnk")
check("解析不存在的快捷方式返回 None",
      pinned_apps.app_info_from_shortcut(missing_lnk, ProcessManager(), log) is None)

discovered = pinned_apps.discover_pinned_apps(ProcessManager(), log)
check("discover_pinned_apps 返回列表", isinstance(discovered, list),
      "共 %d 项" % len(discovered))
if discovered:
    entry = discovered[0]
    check("固定项包含 name/path/icon/is_pinned 字段",
          {"name", "path", "icon", "is_pinned"} <= set(entry), entry.get("name"))

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

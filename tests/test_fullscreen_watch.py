"""全屏程序让位验证：判定规则、去抖状态机、监听线程与 dock 接线。

覆盖的功能是「有程序（非系统程序）全屏显示时隐藏 dock 并短暂注销 AppBar，
全屏解除时恢复 AppBar 并显示 dock」。判定与状态机都在
``core/fullscreen_watch.py`` 里，这里是纯逻辑 + 真实线程两层验证；
真机（真实桌面）的观察用 ``tests/manual_fullscreen_probe.py``。
"""

import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

import core.sys32 as sys32
from core.fullscreen_watch import (
    DEFAULT_EXIT_CONFIRM,
    WindowSnapshot,
    FullscreenWatcherWorker,
    covers_monitor,
    is_system_process,
    is_system_window_class,
    normalize_process_name,
    select_fullscreen_window,
)

# ---------------------------------------------------------------- 夹具
MON = (0, 0, 1536, 864)        # 主显示器
MON2 = (1536, 0, 3072, 864)    # 副屏


def snap(**kwargs):
    """构造一个「普通程序全屏」快照，再用关键字覆盖需要的字段。"""
    data = {
        "hwnd": 100,
        "class_name": "Chrome_WidgetWin_1",
        "title": "某游戏",
        "process_name": "game.exe",
        "exe_path": r"D:\Games\game.exe",
        "rect": MON,
        "monitor_rect": MON,
        "foreground": True,
        "root_owner": False,
        "visible": True,
        "iconic": False,
        "tool_window": False,
    }
    data.update(kwargs)
    return WindowSnapshot(**data)


# ---------------------------------------------------------------- 1) 工具函数
check("进程名规范化补 .exe 并转小写", normalize_process_name("Chrome") == "chrome.exe")
check("进程名规范化保留已有扩展名", normalize_process_name("MSEDGE.EXE") == "msedge.exe")
check("空进程名规范化返回空串", normalize_process_name(None) == "")

check("explorer.exe 属于系统组件", is_system_process("explorer.exe"))
check("SYSTEM 进程名大小写不敏感", is_system_process("EXPLORER"))
check("游戏不属于系统组件", not is_system_process("game.exe"))
check("进程名缺失时用 exe 路径兜底", is_system_process("", r"C:\Windows\explorer.exe"))
check("进程名与路径都缺失时不当作系统组件", not is_system_process("", None))

check("Progman 是系统外壳窗口类", is_system_window_class("Progman"))
check("Shell_TrayWnd 是系统外壳窗口类", is_system_window_class("Shell_TrayWnd"))
check("普通窗口类不是系统外壳", not is_system_window_class("Chrome_WidgetWin_1"))

# ---------------------------------------------------------------- 2) 覆盖判定
check("完全吻合算覆盖", covers_monitor(MON, MON))
check("在容差内算覆盖", covers_monitor((-2, -1, 1538, 866), MON))
check("超过容差不覆盖", covers_monitor((-9, -9, 1545, 873), MON))
# 底部被工作区挡住（普通最大化窗口：底边停在 dock 上沿，够不到显示器底边）
check("底边差太多不覆盖（普通最大化窗口）", not covers_monitor((0, 0, 1536, 800), MON))
check("左侧只盖一半不覆盖", not covers_monitor((0, 0, 900, 864), MON))
check("读不到显示器信息一律不覆盖", not covers_monitor(MON, None))
check("矩形为 None 不覆盖", not covers_monitor(None, MON))
check("非四元组不覆盖", not covers_monitor((0, 0, 1536), MON))

# ---------------------------------------------------------------- 3) 判定规则
check("普通程序全屏 → 命中", select_fullscreen_window([snap()]) is not None)

check("桌面 Progman → 不命中",
      select_fullscreen_window([snap(class_name="Progman", process_name="explorer.exe")]) is None)
check("系统进程全屏 → 不命中",
      select_fullscreen_window([snap(process_name="explorer.exe")]) is None)
check("开始菜单全屏 → 不命中",
      select_fullscreen_window([snap(process_name="StartMenuExperienceHost.exe")]) is None)
check("任务栏窗口 → 不命中",
      select_fullscreen_window([snap(class_name="Shell_TrayWnd")]) is None)
check("dock 自己的窗口 → 不命中",
      select_fullscreen_window([snap(hwnd=4242)], ignored_hwnds=[4242]) is None)
check("工具窗口 → 不命中", select_fullscreen_window([snap(tool_window=True)]) is None)
check("最小化窗口 → 不命中", select_fullscreen_window([snap(iconic=True)]) is None)
check("隐藏窗口 → 不命中", select_fullscreen_window([snap(visible=False)]) is None)
check("非前台窗口 → 不命中", select_fullscreen_window([snap(foreground=False)]) is None)
check("前台窗口的 root owner 全屏 → 命中",
      select_fullscreen_window([snap(root_owner=True, foreground=False)]) is not None)
check("普通最大化窗口 → 不命中",
      select_fullscreen_window([snap(rect=(0, 0, 1536, 800))]) is None)
check("副屏全屏按副屏显示器判定 → 命中",
      select_fullscreen_window([snap(rect=MON2, monitor_rect=MON2)]) is not None)
check("在副屏全屏、但前台在另一个屏幕 → 不命中",
      select_fullscreen_window([snap(rect=MON2, monitor_rect=MON2, foreground=False)]) is None)
check("用户额外排除的程序 → 不命中",
      select_fullscreen_window([snap(process_name="OBS64.exe")],
                               extra_processes=["obs64"]) is None)
check("本程序自己（打包后）→ 不命中",
      select_fullscreen_window([snap(process_name="MikaDock.exe")],
                               own_process_name="MikaDock.exe") is None)
check("候选里有多个时取第一个命中的",
      select_fullscreen_window([snap(hwnd=1, rect=(0, 0, 10, 10)), snap(hwnd=2)]).hwnd == 2)
check("快照列表为空 → 不命中", select_fullscreen_window([]) is None)

# ---------------------------------------------------------------- 4) 去抖状态机
class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, is_fullscreen, description):
        self.events.append((bool(is_fullscreen), description))


def make_worker(snapshots_factory, **kwargs):
    """构造一个不启动线程的监听器，候选快照由 snapshots_factory 提供。"""
    worker = FullscreenWatcherWorker(
        None,
        candidate_provider=lambda _pm, _ignored: snapshots_factory(),
        **kwargs,
    )
    recorder = Recorder()
    worker.state_changed.connect(recorder)
    return worker, recorder


worker, rec = make_worker(lambda: [snap()])
check("初始状态为非全屏", worker.is_fullscreen() is False)
worker.poll_once()
check("一轮命中即上报进入全屏（enter_confirm=1）",
      rec.events == [(True, "game.exe（某游戏）")], rec.events)
check("上报后 is_fullscreen 为真", worker.is_fullscreen() is True)
worker.poll_once()
check("持续全屏不会重复上报", len(rec.events) == 1, rec.events)

# 退出需要 exit_confirm 轮连续未命中（默认 2）
state = {"full": True}
worker, rec = make_worker(lambda: [snap()] if state["full"] else [])
worker.poll_once()
check("退出测试：先进入全屏", rec.events == [(True, "game.exe（某游戏）")], rec.events)
rec.events.clear()
state["full"] = False
worker.poll_once()
check("退出全屏第一轮未命中不发信号", rec.events == [], rec.events)
check("第一轮未命中后仍在全屏状态", worker.is_fullscreen() is True)
worker.poll_once()
check("第二轮未命中才上报退出全屏",
      rec.events == [(False, "")], rec.events)
check("退出后 is_fullscreen 为假", worker.is_fullscreen() is False)
check("退出后描述被清空", worker.current_label() == "")

# 抖动：全屏 → 未命中一轮 → 又全屏，不该翻转
state = {"full": True}
worker2, rec2 = make_worker(lambda: [snap()] if state["full"] else [], exit_confirm=2)
worker2.poll_once()                      # 进入全屏
check("抖动测试：先进入全屏", rec2.events == [(True, "game.exe（某游戏）")], rec2.events)
state["full"] = False
worker2.poll_once()                      # 未命中第 1 轮（还没到 exit_confirm=2）
state["full"] = True
worker2.poll_once()                      # 又命中
check("抖动测试：中途恢复全屏不会误报退出", len(rec2.events) == 1, rec2.events)
check("抖动测试：状态仍是全屏", worker2.is_fullscreen() is True)

# enter_confirm=2 时进入也需要连续两轮
worker3 = FullscreenWatcherWorker(None, candidate_provider=lambda _p, _i: [snap()],
                                  enter_confirm=2)
rec3 = Recorder()
worker3.state_changed.connect(rec3)
worker3.poll_once()
check("enter_confirm=2 时第一轮不上报", rec3.events == [], rec3.events)
worker3.poll_once()
check("enter_confirm=2 时第二轮上报", rec3.events == [(True, "game.exe（某游戏）")], rec3.events)
check("默认退出确认轮数为 2", DEFAULT_EXIT_CONFIRM == 2)

# 运行期参数
always = [snap(hwnd=777)]
worker4 = FullscreenWatcherWorker(None, candidate_provider=lambda _p, _i: always)
rec4 = Recorder()
worker4.state_changed.connect(rec4)
worker4.set_ignored_hwnds([777])
worker4.poll_once()
check("ignore 列表里的窗口不会被判定为全屏", rec4.events == [], rec4.events)
worker4.set_ignored_hwnds([])
worker4.set_extra_processes(["game"])
worker4.poll_once()
check("额外排除列表在运行期生效", rec4.events == [], rec4.events)

# ---------------------------------------------------------------- 5) 监听线程
state = {"full": True}
thread_worker = FullscreenWatcherWorker(
    None,
    interval_ms=60,
    candidate_provider=lambda _pm, _ignored: [snap()] if state["full"] else [],
)
thread_rec = Recorder()
thread_worker.state_changed.connect(thread_rec)
thread_worker.start()

deadline = time.time() + 5
while time.time() < deadline and not thread_rec.events:
    app.processEvents()
    time.sleep(0.02)
check("监听线程能检测到全屏", thread_rec.events and thread_rec.events[0][0], thread_rec.events)

state["full"] = False
deadline = time.time() + 5
while time.time() < deadline and len(thread_rec.events) < 2:
    app.processEvents()
    time.sleep(0.02)
check("监听线程能检测到全屏解除",
      len(thread_rec.events) >= 2 and thread_rec.events[1][0] is False, thread_rec.events)

thread_worker.stop()
check("stop() 之后监听线程已退出", not thread_worker.isRunning())
thread_worker.stop()
check("重复 stop() 不抛异常", True)

# ---------------------------------------------------------------- 6) sys32 AppBar 接口
check("未注册时 is_appbar_registered() 为假", sys32.is_appbar_registered() is False)
sys32.remove_appbar()
check("未注册时 remove_appbar() 是安全的空操作", sys32.is_appbar_registered() is False)
check("sys32 暴露 AppBar 重注册所需的两个函数",
      callable(sys32.set_appbar_bottom) and callable(sys32.remove_appbar))

# ---------------------------------------------------------------- 7) 配置与 dock 接线
import core.config_manager as Config
import app as dock_module

fs_default = Config.DEFAULT_CONFIG.get("fullscreen", {})
check("默认配置包含 fullscreen 段", isinstance(fs_default, dict) and bool(fs_default))
check("默认开启全屏让位", fs_default.get("enabled") is True)
check("默认排除列表为空（不影响判定）", fs_default.get("except_processes") == [])
for key in ("enabled", "poll_interval_ms", "enter_confirm", "exit_confirm",
            "tolerance", "except_processes"):
    check(f"默认配置包含 {key}", key in fs_default)

check("DockApp 提供进入/退出让位方法",
      callable(getattr(dock_module.DockApp, "enter_fullscreen_suppression", None))
      and callable(getattr(dock_module.DockApp, "exit_fullscreen_suppression", None)))
check("DockApp 提供让位线程的启停方法",
      callable(getattr(dock_module.DockApp, "_start_fullscreen_watch", None))
      and callable(getattr(dock_module.DockApp, "_stop_fullscreen_watch", None)))
check("DockApp 提供右侧扩展窗口的创建与显隐方法",
      callable(getattr(dock_module.DockApp, "_init_extension", None))
      and callable(getattr(dock_module.DockApp, "_set_extension_visible", None))
      and callable(getattr(dock_module.DockApp, "set_extension_width", None)))


class FakeDock:
    """借用 DockApp 的让位方法：只准备这些方法用到的属性。"""

    _fs_suppressed = False
    _fs_last_description = ""
    _original_work_area_bottom = 864

    def __init__(self, appbar_registered):
        self.appbar_registered = appbar_registered
        self.calls = []
        self.hidden = False
        self.shown = False
        self.extension_visible = None

    # -- 被测方法真正会碰到的宿主能力 --
    def hide_icon_tooltip(self):
        self.calls.append("hide_tooltip")

    def hide(self):
        self.hidden = True
        self.calls.append("hide")

    def show(self):
        self.shown = True
        self.calls.append("show")

    def _ensure_dock_visible(self):
        self.calls.append("ensure_visible")

    def update_window_position(self):
        self.calls.append("reposition")

    def _set_extension_visible(self, visible):
        self.extension_visible = visible
        self.calls.append("extension_show" if visible else "extension_hide")

    _dock_target_y = dock_module.DockApp._dock_target_y
    enter_fullscreen_suppression = dock_module.DockApp.enter_fullscreen_suppression
    exit_fullscreen_suppression = dock_module.DockApp.exit_fullscreen_suppression
    _sync_fullscreen_ignored_windows = dock_module.DockApp._sync_fullscreen_ignored_windows


class FakeWorker:
    """替身：只记录被登记的忽略句柄。"""

    def __init__(self):
        self.ignored = None

    def set_ignored_hwnds(self, hwnds):
        self.ignored = list(hwnds)


class FakeSys32:
    """替身：记录 AppBar 的注销/注册调用顺序，不真的动系统工作区。"""

    def __init__(self):
        self.registered = True
        self.calls = []

    def is_appbar_registered(self):
        return self.registered

    def remove_appbar(self):
        self.registered = False
        self.calls.append("remove_appbar")

    def set_appbar_bottom(self, top):
        self.registered = True
        self.calls.append(("set_appbar_bottom", int(top)))

    def refresh_metrics(self):
        self.calls.append("refresh_metrics")


real_sys32 = dock_module.sys32
fake_sys32 = FakeSys32()
dock_module.sys32 = fake_sys32
try:
    fake_dock = FakeDock(appbar_registered=True)
    fake_dock.enter_fullscreen_suppression("game.exe（某游戏）")
    check("进入让位会注销 AppBar", fake_sys32.calls[0] == "remove_appbar", fake_sys32.calls)
    check("进入让位会隐藏 dock", fake_dock.hidden is True)
    check("进入让位会收起提示条", "hide_tooltip" in fake_dock.calls, fake_dock.calls)
    check("进入让位会隐藏右侧扩展窗口", fake_dock.extension_visible is False,
          fake_dock.calls)
    check("进入让位后状态标记为挂起", fake_dock._fs_suppressed is True)

    calls_before = len(fake_sys32.calls)
    fake_dock.enter_fullscreen_suppression("再来一次")
    check("重复进入让位是幂等的", len(fake_sys32.calls) == calls_before, fake_sys32.calls)

    fake_dock.exit_fullscreen_suppression()
    check("退出让位会重新注册 AppBar",
          any(isinstance(c, tuple) and c[0] == "set_appbar_bottom"
              for c in fake_sys32.calls), fake_sys32.calls)
    check("重新注册前会刷新屏幕指标", "refresh_metrics" in fake_sys32.calls, fake_sys32.calls)
    check("退出让位会重新显示 dock", fake_dock.shown is True)
    check("退出让位会重排窗口", "reposition" in fake_dock.calls, fake_dock.calls)
    check("退出让位会恢复右侧扩展窗口", fake_dock.extension_visible is True,
          fake_dock.calls)
    check("退出让位后挂起标记清除", fake_dock._fs_suppressed is False)

    # AppBar 本来就没注册时（例如上一轮已经注销过）不该重复注销
    fake_sys32.calls.clear()
    fake_sys32.registered = False
    dock2 = FakeDock(appbar_registered=False)
    dock2.enter_fullscreen_suppression("game.exe")
    check("AppBar 未注册时跳过重复注销", fake_sys32.calls == [], fake_sys32.calls)
    check("AppBar 未注册时依然隐藏 dock", dock2.hidden is True)

    # _dock_target_y 只依赖启动时保存的原始工作区底部
    check("_dock_target_y 与窗口位置算式一致",
          dock2._dock_target_y() == 864 - (dock_module.DockConstants.ICON_SIZE
                                           + dock_module.DockConstants.WINDOW_MARGIN * 2))

    # 扩展窗口的句柄也要进忽略列表，否则它自己会被判成"全屏程序"触发让位
    sync_dock = FakeDock(appbar_registered=True)
    sync_dock.hwnd = 0x111
    sync_dock._extension = types.SimpleNamespace(hwnd=0x222)
    sync_dock._fs_worker = FakeWorker()
    sync_dock._sync_fullscreen_ignored_windows()
    check("忽略列表包含 dock 与扩展窗口的句柄",
          sync_dock._fs_worker.ignored == [0x111, 0x222], sync_dock._fs_worker.ignored)

    sync_dock._extension.hwnd = None
    sync_dock._sync_fullscreen_ignored_windows()
    check("扩展窗口还没有句柄时只登记 dock",
          sync_dock._fs_worker.ignored == [0x111], sync_dock._fs_worker.ignored)

    sync_dock._extension = None
    sync_dock._sync_fullscreen_ignored_windows()
    check("没有扩展窗口时也不报错",
          sync_dock._fs_worker.ignored == [0x111], sync_dock._fs_worker.ignored)
finally:
    dock_module.sys32 = real_sys32

# ---------------------------------------------------------------- 8) 配置兼容与设置界面
import shutil
import tempfile

import core.settings as settings_module

tmp_dir = tempfile.mkdtemp(prefix="fs-cfg-")
cfg_path = os.path.join(tmp_dir, "settings.json")
try:
    # 老配置文件（还没有 fullscreen 段）必须能自动补上默认值
    Config.save_config(cfg_path, {"dock": {"apps": []}})
    backfilled = Config.load_config(cfg_path)
    check("老配置自动补上 fullscreen 段",
          isinstance(backfilled.get("fullscreen"), dict))
    check("补上的默认值是开启让位",
          backfilled["fullscreen"].get("enabled") is True)
    check("补上的默认值保留调参项",
          backfilled["fullscreen"].get("exit_confirm") == 2)

    # 设置界面往返：读得到、改得回
    ui = settings_module.SettingsUI(version="test", config_path=cfg_path)
    check("设置界面读到的默认状态是开启", ui.fullscreen_enabled.isChecked() is True)
    check("设置界面默认排除列表为空", ui.fullscreen_except.toPlainText().strip() == "")

    ui.fullscreen_enabled.setChecked(False)
    check("关掉开关会把排除列表置灰", ui.fullscreen_except.isEnabled() is False)
    ui.fullscreen_enabled.setChecked(True)
    check("重新打开开关会恢复排除列表", ui.fullscreen_except.isEnabled() is True)

    ui.fullscreen_except.setPlainText("obs64.exe\n\n  msedge  \n")
    ui.collect_settings()
    collected = ui.config_data.get("fullscreen", {})
    check("设置界面写回 enabled", collected.get("enabled") is True)
    check("设置界面写回排除列表（去空行与空白）",
          collected.get("except_processes") == ["obs64.exe", "msedge"],
          collected.get("except_processes"))
    check("设置界面不会丢掉未接管的调参项",
          collected.get("poll_interval_ms") == 400
          and collected.get("tolerance") == 2, collected)

    # 再读一次：排除列表里的值要能正确回填（带 .exe 与不带扩展名都原样保留）
    reloaded = Config.load_config(cfg_path)
    reloaded["fullscreen"]["except_processes"] = ["obs64.exe", "msedge"]
    Config.save_config(cfg_path, reloaded)
    ui2 = settings_module.SettingsUI(version="test", config_path=cfg_path)
    check("排除列表能回填到设置界面",
          ui2.fullscreen_except.toPlainText().split("\n") == ["obs64.exe", "msedge"],
          ui2.fullscreen_except.toPlainText())
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

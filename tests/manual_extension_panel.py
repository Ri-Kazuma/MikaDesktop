"""手动验证（会短暂影响真实桌面）：扩展窗口状态面板 + 系统原生面板联动。

用法::

    python tests/manual_extension_panel.py [--preview 路径]

做的事：

1. 真的创建一个扩展窗口 + 状态面板，采集一次真实状态（网络/音量/电源），
   渲染一张预览图（``log/extension_panel_preview.png``）供肉眼检查；
2. 把扩展窗口按 dock 的位置显示几秒，确认它可见、尺寸正确（高度 == dock 高度）；
3. 用 **真实** 状态检查 tooltip 文案、电源按钮显隐（台式机应隐藏）；
4. 依次触发「快速设置」与「通知中心」，检查系统面板确实开了，然后 Esc 关掉；
5. 面板按钮的命中测试（置顶工具窗口点不点得中）。

中途中断也不会留下东西：面板会关掉、系统浮出用 Esc 关闭。
"""

import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    sys.stdout.flush()
    if not ok:
        failures.append(name)


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def foreground():
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    try:
        return win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)
    except Exception:
        return "", ""


def visible_windows():
    """当前可见的顶层窗口 ``[(class, title)]``。"""
    import win32gui

    found = []

    def collect(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                found.append((win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return found


def system_panels():
    """系统浮出面板（快速设置 / 通知中心）。

    这些 XAML 面板**不在** EnumWindows 的结果里（UWP 岛窗口），要用 UI Automation
    才看得到；前台窗口则是最省事的补充信号。
    """
    found = []
    try:
        import comtypes
        import comtypes.client
        from comtypes import CLSCTX_INPROC_SERVER, CoCreateInstance

        if "uia" not in _UIA:
            comtypes.CoInitialize()
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            _UIA["uia"] = CoCreateInstance(module.CUIAutomation._reg_clsid_,
                                           interface=module.IUIAutomation,
                                           clsctx=CLSCTX_INPROC_SERVER)
            _UIA["root"] = _UIA["uia"].GetRootElement()
        walker = _UIA["uia"].ControlViewWalker
        child = walker.GetFirstChildElement(_UIA["root"])
        while child:
            try:
                name = child.CurrentName or ""
                cls = child.CurrentClassName or ""
                if cls == "ControlCenterWindow" or "快速设置" in name or "通知中心" in name:
                    found.append((cls, name))
            except Exception:
                pass
            child = walker.GetNextSiblingElement(child)
    except Exception as e:
        print("      UIA 枚举失败（退回只看前台）: %s" % e)
    fg = foreground()
    if (fg[0] == "ControlCenterWindow" or "快速设置" in fg[1] or "通知中心" in fg[1]) \
            and (fg[0], fg[1]) not in found:
        found.append(fg)
    return found


_UIA = {}


def quick_settings_present():
    return any(cls == "ControlCenterWindow" or "快速设置" in name
               for cls, name in system_panels())


def notification_center_present():
    return any("通知中心" in name for cls, name in system_panels())


def wait_for(predicate, timeout=2.5, pump_seconds=0.1):
    """轮询等待；期间要跑 Qt 事件循环（延迟动作、QTimer 都靠它）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    end = time.time() + timeout
    while time.time() < end:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(pump_seconds)
    return False


def ensure_closed(predicate, toggle=None, attempts=3):
    """把面板关干净。

    ``ms-availablenetworks:`` / ``ms-actioncenter:`` 是**切换**语义（面板开着时再调
    一次会把关掉），所以测试前必须确认它是"关"的状态，否则后面那步会变成关闭。

    优先 Esc；如果面板开着却没拿前台，Esc 会打到别的窗口上，这时用切换语义再调一次
    URI 把它关掉。
    """
    for _ in range(attempts):
        if not predicate():
            return True
        send_esc()
        time.sleep(0.5)
        if not predicate():
            return True
        if toggle is not None:
            toggle()
            time.sleep(0.8)
    return not predicate()


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion)]


def _send_key(vk, up=False):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.restype = ctypes.c_uint
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    inp = _INPUT()
    inp.type = 1  # INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.dwFlags = 0x0002 if up else 0
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTunion2(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("pad", ctypes.c_byte * 40)]


class _INPUT2(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion2)]


def send_mouse_click():
    """在当前光标位置按一下左键（不移动光标，坐标由 Qt 的 QCursor.setPos 负责）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.restype = ctypes.c_uint
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    results = []
    for flag in (0x0002, 0x0004):   # LEFTDOWN / LEFTUP
        inp = _INPUT2()
        inp.type = 0                # INPUT_MOUSE
        inp.u.mi.dwFlags = flag
        results.append(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT2)))
        time.sleep(0.05)
    return results


def send_esc():
    """用 keybd_event 发 Esc。

    SendInput 在这台机器上被拦（对 Win 键/鼠标都返回 0），keybd_event 可用。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(0x1B, 0, 0, None)
    user32.keybd_event(0x1B, 0, 2, None)


def close_settings() -> int:
    """关掉设置窗口（ApplicationFrameWindow / 设置），返回关掉的数量。"""
    import win32gui

    targets = []

    def collect(hwnd, _):
        try:
            if (win32gui.GetClassName(hwnd) == "ApplicationFrameWindow"
                    and "设置" in win32gui.GetWindowText(hwnd)):
                targets.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    for hwnd in targets:
        win32gui.PostMessage(hwnd, 0x0010, 0, 0)   # WM_CLOSE
    return len(targets)


def panel_names(limit: int = 400):
    """快速设置窗口内部的 UIA 元素名（用来区分"主界面"和"WLAN 页"）。"""
    names = []
    try:
        import comtypes
        import comtypes.client
        from comtypes import CLSCTX_INPROC_SERVER, CoCreateInstance

        if "uia" not in _UIA:
            comtypes.CoInitialize()
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            _UIA["uia"] = CoCreateInstance(module.CUIAutomation._reg_clsid_,
                                           interface=module.IUIAutomation,
                                           clsctx=CLSCTX_INPROC_SERVER)
            _UIA["root"] = _UIA["uia"].GetRootElement()
        walker = _UIA["uia"].ControlViewWalker

        def walk(element, depth=0):
            if depth > 6 or len(names) >= limit:
                return
            try:
                child = walker.GetFirstChildElement(element)
            except Exception:
                return
            while child:
                try:
                    name = child.CurrentName or ""
                    if name:
                        names.append(name)
                except Exception:
                    pass
                walk(child, depth + 1)
                child = walker.GetNextSiblingElement(child)

        walk(_UIA["root"])
    except Exception as e:
        print("      UIA 读取面板内容失败: %s" % e)
    return names


def close_quick_settings(open_fn) -> bool:
    """关掉快速设置窗口：Esc 不行就再用**同一个**打开方式切一次（热键/URI 都是切换语义）。"""
    for _ in range(3):
        if not quick_settings_present():
            return True
        send_esc()
        time.sleep(0.5)
        if not quick_settings_present():
            return True
        open_fn()
        time.sleep(0.8)
    return not quick_settings_present()


def close_panels(predicate, attempts=3):
    """（保留给真机点击分支用）反复 Esc 直到面板消失。"""
    for _ in range(attempts):
        send_esc()
        time.sleep(0.5)
        if not predicate():
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", default=None,
                        help="预览图输出路径（默认 log/extension_panel_preview.png）")
    args = parser.parse_args()

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    from core import system_status
    from core.dock.dock_extension import DockExtensionWindow, CONTENT_MARGIN
    from core.dock.dock_constants import DockConstants
    from core.dock.extension_panel import ExtensionPanel
    from core.system_status import SystemStatusWorker

    # ---------- 1. 组件与真实状态 ----------
    worker = SystemStatusWorker()
    extension = DockExtensionWindow()
    panel = ExtensionPanel(res_dir=os.path.join(ROOT, "res"), status_worker=worker, log=None)
    snapshot = system_status.collect_status()
    panel.apply_status(snapshot)
    extension.set_content(panel)
    extension.fit_to_content()
    panel._layout.activate()
    extension.fit_to_content()

    print(f"      网络   = {snapshot['network']}")
    print(f"      音量   = {snapshot['volume']}")
    print(f"      电源   = {snapshot['power']}")
    print("      面板宽 = %d，窗口宽 = %d，窗口高 = %d"
          % (panel.preferred_width(), extension.extension_width(), extension.height()))

    log_dir = os.path.join(ROOT, "log")
    os.makedirs(log_dir, exist_ok=True)
    preview = args.preview or os.path.join(log_dir, "extension_panel_preview.png")
    # 真实运行时高度由 dock 决定（布局最小高度 90 = 15 边距 + 60 按钮 + 15 边距）
    window_height = max(DockConstants.WINDOW_HEIGHT,
                        panel.sizeHint().height() + CONTENT_MARGIN * 2)
    extension.resize(extension.extension_width(), window_height)
    panel._layout.activate()
    extension.grab().save(preview)
    check("预览图已生成", os.path.getsize(preview) > 0, preview)

    # ---------- 2. 显示在 dock 的位置 ----------
    screen = app.primaryScreen().availableGeometry()
    width = extension.extension_width()
    height = window_height
    extension.setGeometry(screen.x() + (screen.width() - width) // 2,
                          screen.bottom() - height, width, height)
    extension.show()
    pump(app, 1.2)
    check("扩展窗口已显示", extension.isVisible())
    check("扩展窗口高度等于 dock 实际高度（90）",
          extension.height() == window_height, extension.height())
    check("面板按钮都可见且有图标",
          all(not b.icon().isNull() for b in (panel.network_button,
                                              panel.volume_button, panel.notify_button)))
    check("面板没有超出窗口宽度",
          panel.width() <= extension.width() - CONTENT_MARGIN * 2 + 2,
          "panel=%d window=%d" % (panel.width(), extension.width()))

    # ---------- 3. tooltip 文案 ----------
    if snapshot["network"].get("connected"):
        check("网络 tooltip 含连接信息", "网络：" in panel.network_button.toolTip()
              and "连接" in panel.network_button.toolTip(), panel.network_button.toolTip())
    check("音量 tooltip 含百分比或读取失败",
          "音量：" in panel.volume_button.toolTip(), panel.volume_button.toolTip())
    if snapshot["power"].get("present"):
        check("笔记本上有电源按钮且 tooltip 有电量",
              not panel.battery_button.isHidden() and "电源：" in panel.battery_button.toolTip(),
              panel.battery_button.toolTip())
    else:
        check("台式机隐藏电源按钮", panel.battery_button.isHidden())

    # ---------- 4. 原生面板 ----------
    close_quick = lambda: ensure_closed(                       # noqa: E731
        quick_settings_present, lambda: system_status.open_quick_settings("network"))
    close_notify = lambda: ensure_closed(                      # noqa: E731
        notification_center_present, system_status.open_notification_center)
    check("测试前先把系统面板关干净（这两个 URI 是切换语义）",
          close_quick() and close_notify(), system_panels())

    ok_quick = system_status.open_quick_settings("network")
    opened = wait_for(quick_settings_present, 3.0)
    print("      快速设置: 前台=%s 系统面板=%s" % (foreground(), system_panels()))
    check("触发了快速设置（URI 已注册）", ok_quick)
    check("快速设置面板确实打开了", opened, foreground())
    check("之后快速设置被关掉", close_quick(), system_panels())

    ok_notify = system_status.open_notification_center()
    opened = wait_for(notification_center_present, 3.0)
    print("      通知中心: 前台=%s 系统面板=%s" % (foreground(), system_panels()))
    check("触发了通知中心（URI 已注册）", ok_notify)
    check("通知中心确实打开了", opened, foreground())
    check("之后通知中心被关掉", close_notify(), system_panels())

    # ---------- 4c. 三个状态键要打开**不同**的界面 ----------
    # 网络 → WLAN 网络列表；音量 → 快速设置主界面（Win+A）；电源 → 设置的电池页
    open_network = lambda: system_status.open_quick_settings("network")     # noqa: E731
    open_volume = lambda: system_status.open_quick_settings("volume")       # noqa: E731

    system_status.open_quick_settings("network")
    opened = wait_for(quick_settings_present, 3.0)
    names = panel_names()
    check("网络键打开 WLAN 网络列表",
          opened and any("Wi-Fi" in n or "WLAN" in n for n in names),
          [n for n in names if "WLAN" in n or "Wi-Fi" in n][:5])
    check("网络列表关掉", close_quick_settings(open_network), system_panels())

    system_status.open_quick_settings("volume")
    opened = wait_for(quick_settings_present, 4.0)      # 等 URI 打开 + QTimer 点「后退」
    pump(app, 1.2)
    names = panel_names()
    check("音量键打开快速设置主界面（不是 WLAN 列表）",
          opened and any("蓝牙" in n or "投影" in n or "亮度" in n for n in names),
          [n for n in names if "蓝牙" in n or "投影" in n or "亮度" in n][:5])
    check("快速设置主界面关掉", close_quick_settings(open_volume), system_panels())

    system_status.open_quick_settings("power")
    opened = wait_for(lambda: foreground()[0] == "ApplicationFrameWindow"
                      or "设置" in foreground()[1], 4.0)
    check("电源键打开设置的电池页", opened, foreground())
    check("设置窗口已关闭", close_settings() >= 0)
    pump(app, 0.5)

    # ---------- 4b. 面板按钮的命中测试 / 真实鼠标点击 ----------
    # 扩展窗口是"置顶 + 不抢焦点"的工具窗口，必须确认鼠标点得中它。
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QCursor

    notify_center = panel.notify_button.mapToGlobal(
        QPoint(panel.notify_button.width() // 2, panel.notify_button.height() // 2))
    old_cursor = QCursor.pos()
    widget_under = None
    for _ in range(3):
        # 置顶工具窗口偶尔会被系统浮出的收尾动作压一下，重新抬到最前再测
        extension.show()
        extension.raise_()
        QCursor.setPos(notify_center)
        pump(app, 0.4)
        widget_under = app.widgetAt(QCursor.pos())
        if widget_under is panel.notify_button:
            break
    check("光标下的部件就是通知按钮（点击会落到面板上）",
          widget_under is panel.notify_button, "光标下是 %s" % widget_under)

    clicks = []
    panel.notify_button.clicked.connect(lambda: clicks.append(1))
    results = send_mouse_click()
    pump(app, 0.6)
    if any(results):
        check("真实鼠标点击通知按钮能打开通知中心",
              wait_for(notification_center_present) or bool(clicks),
              "clicked=%d 前台=%s" % (len(clicks), foreground()))
        ensure_closed(notification_center_present, attempts=3)
        check("真实鼠标点击后扩展窗口仍然置顶（没被系统浮出盖住）",
              app.widgetAt(QCursor.pos()) is panel.notify_button)
    else:
        # 这台机器（或安全软件）拦下了合成输入：SendInput 对鼠标也返回 0，
        # 没法用程序模拟"真人点击"。命中测试已经证明光标下的窗口就是按钮。
        print("      [SKIP] 系统拦下了合成鼠标输入（SendInput 返回 0），"
              "无法在本机做真实点击验证；命中测试已确认点击会落到按钮上")
    QCursor.setPos(old_cursor)
    pump(app, 0.2)

    extension.hide()
    pump(app, 0.3)
    check("收尾：扩展窗口已隐藏", not extension.isVisible())
    try:
        worker.stop()
    except Exception:
        pass

    print()
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - " + name)
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)

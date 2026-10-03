"""手动验证（会在几秒内启动一个真实的 dock 实例）：全屏让位端到端走一遍。

用法::

    python tests/manual_dock_fullscreen_cycle.py

做的事：

1. 真的创建 :class:`dock.DockApp`（会注册 AppBar、启动进程扫描线程、显示 XHT 小黑条）；
   顺带检查右侧扩展窗口的尺寸/位置（启动宽度 150、与 dock 等高、间隙
   ``EXTENSION_GAP``、整体居中、句柄进了全屏忽略列表）；
2. 直接调用让位入口 :meth:`DockApp.enter_fullscreen_suppression`（等价于"检测到
   非系统程序全屏"），检查 dock 与扩展窗口是否隐藏、AppBar 是否注销、工作区是否
   还回去；
3. 调用恢复入口 :meth:`DockApp.exit_fullscreen_suppression`，检查 dock 与扩展窗口
   是否重新显示、AppBar 是否装回、保留高度是否与让位前一致；
4. 再走一遍**真正的自动检测**：自己造一个铺满整块显示器的前台窗口，完全不碰让位
   方法，验证监听线程 → 信号 → dock 这条链路能让位（扩展窗口一起隐藏），并在窗口
   关闭后自动恢复；
5. 无论成败都注销 AppBar、停线程，然后直接退出进程。

脚本**不会**隐藏系统任务栏（只有 ``dock.py`` 的 ``main()`` 会那么做），所以中途
中断也不会把任务栏弄丢。运行期间屏幕底部会短暂出现 dock 与小黑条、工作区会短暂
变小，第 4 步还会有几秒钟被一块深灰色窗口铺满，都属正常现象。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    sys.stdout.flush()
    if not ok:
        failures.append(name)


def pump(app, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("MikaDock-fullscreen-selftest")

    import core.sys32 as sys32
    import app as dock_module

    dock = None
    exit_code = 1
    try:
        dock = dock_module.DockApp()
        dock.show()
        pump(app, 1.0)   # 等窗口真正显示（hwnd 在 showEvent 里才拿到）、线程跑起来

        check("dock 窗口已显示", dock.isVisible())
        check("启动后 AppBar 已注册", sys32.is_appbar_registered() is True)
        check("全屏监听线程已启动",
              dock._fs_worker is not None and dock._fs_worker.isRunning())

        # ---------- 右侧扩展窗口：启动尺寸与位置 ----------
        extension = getattr(dock, '_extension', None)
        check("扩展窗口已创建", extension is not None)
        if extension is not None:
            gap = dock_module.DockConstants.EXTENSION_GAP
            ext_rect = extension.geometry()
            dock_rect = dock.geometry()
            avail = app.primaryScreen().availableGeometry()
            print("      dock=%s 扩展=%s 可用=%s" % (dock_rect, ext_rect, avail))
            check("扩展窗口已随 dock 显示", extension.isVisible())
            check("扩展窗口宽度在允许范围内（150~600）",
                  150 <= extension.width() <= 600, extension.width())
            check("扩展窗口与 dock 等高、顶边对齐",
                  ext_rect.height() == dock_rect.height()
                  and ext_rect.top() == dock_rect.top(),
                  "dock=%s ext=%s" % (dock_rect, ext_rect))
            check("扩展窗口不与 dock 重叠", ext_rect.left() > dock_rect.right(),
                  "dock.right=%d ext.left=%d" % (dock_rect.right(), ext_rect.left()))
            check("扩展窗口与 dock 的间隙等于 EXTENSION_GAP",
                  ext_rect.left() - dock_rect.right() - 1 == gap,
                  "实际间距=%d 期望=%d" % (ext_rect.left() - dock_rect.right() - 1, gap))
            check("dock + 扩展窗口整体居中",
                  abs((dock_rect.left() - avail.left())
                      - (avail.right() - ext_rect.right())) <= 2,
                  "左留白=%d 右留白=%d"
                  % (dock_rect.left() - avail.left(), avail.right() - ext_rect.right()))
            check("扩展窗口句柄已进全屏忽略列表",
                  extension.hwnd in getattr(dock._fs_worker, '_ignored_hwnds', set()),
                  "hwnd=%s ignored=%s" % (extension.hwnd,
                                          getattr(dock._fs_worker, '_ignored_hwnds', None)))

        # ---------- 扩展窗口里的状态面板 ----------
        panel = getattr(dock, '_extension_panel', None)
        check("扩展窗口里有状态面板", panel is not None)
        if panel is not None:
            from core import system_status as _status_module

            content_margin = dock_module.dock_extension.CONTENT_MARGIN
            check("扩展窗口宽度贴合面板内容",
                  extension.width()
                  == min(600, max(150, panel.preferred_width() + 2 * content_margin)),
                  "window=%d panel=%d" % (extension.width(), panel.preferred_width()))
            check("面板宽度放得进扩展窗口",
                  panel.preferred_width() <= extension.width() - 2 * content_margin + 2,
                  "panel=%d window=%d" % (panel.preferred_width(), extension.width()))
            check("面板按钮与 dock 同尺寸",
                  all(b.width() == dock_module.DockConstants.BUTTON_SIZE
                      and b.height() == dock_module.DockConstants.BUTTON_SIZE
                      for b in (panel.network_button, panel.volume_button,
                                panel.battery_button, panel.notify_button)))
            check("网络/音量 tooltip 已填好内容",
                  "：" in panel.network_button.toolTip() and "：" in panel.volume_button.toolTip(),
                  "%s | %s" % (panel.network_button.toolTip(), panel.volume_button.toolTip()))
            check("通知中心入口存在", panel.notify_button.toolTip() == "通知中心")
            battery_present = bool(_status_module.power_status().get("present"))
            check("电源按钮显隐与是否有电池一致",
                  (not panel.battery_button.isHidden()) == battery_present,
                  "present=%s hidden=%s" % (battery_present, panel.battery_button.isHidden()))
        check("状态轮询线程已启动",
              getattr(dock, '_status_worker', None) is not None
              and dock._status_worker.isRunning())
        reserved_work = sys32.refresh_metrics()["work"]
        dock_top = reserved_work[3]
        print(f"      让位前工作区底部={dock_top}（物理像素）")

        # ---------- 按钮间距：窗口必须收敛到布局最小宽度 ----------
        # Qt 的布局最小尺寸是异步冒泡的（新按钮先处于隐藏状态、被当成空项），
        # 一旦窗口停在偏宽状态，多出来的宽度会被 app_container（content_layout 里
        # 唯一带 stretch 的项）吞掉，那一组按钮间距就会忽大忽小。
        def gaps_of(layout):
            boxes = []
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if widget is not None:
                    boxes.append((widget.x(), widget.width()))
            return [boxes[i + 1][0] - (boxes[i][0] + boxes[i][1])
                    for i in range(len(boxes) - 1)]

        dock_min_width = dock.minimumSizeHint().width()
        check("dock 宽度等于布局最小宽度（没有偏宽的富余空间）",
              dock.width() == dock_min_width,
              "width=%d min=%d" % (dock.width(), dock_min_width))
        for group_name, layout in (("固定组", dock.pinned_app_layout),
                                   ("应用组", dock.app_layout),
                                   ("运行组", dock.running_app_layout)):
            gaps = gaps_of(layout)
            check("%s按钮间距 = %d" % (group_name, dock_module.DockConstants.BUTTON_SPACING),
                  all(g == dock_module.DockConstants.BUTTON_SPACING for g in gaps),
                  "gaps=%s" % (gaps,))

        # ---------- 让位 ----------
        dock.enter_fullscreen_suppression("测试程序（手动验证）")
        pump(app, 0.6)
        check("让位后 AppBar 已注销", sys32.is_appbar_registered() is False)
        restored_work = sys32.refresh_metrics()["work"]
        check("让位后工作区底部回落（保留区还给系统）",
              restored_work[3] > dock_top, f"{dock_top} -> {restored_work[3]}")
        check("让位后 dock 窗口已隐藏", not dock.isVisible())
        check("让位后扩展窗口一并隐藏",
              extension is None or not extension.isVisible())
        check("让位后挂起标记为真", dock._fs_suppressed is True)

        # ---------- 恢复 ----------
        dock.exit_fullscreen_suppression()
        pump(app, 0.6)
        check("恢复后 AppBar 重新注册", sys32.is_appbar_registered() is True)
        again_work = sys32.refresh_metrics()["work"]
        check("恢复后保留高度与让位前一致",
              again_work[3] == dock_top, f"{again_work[3]} vs {dock_top}")
        check("恢复后 dock 窗口重新显示", dock.isVisible())
        check("恢复后扩展窗口一并显示",
              extension is None or extension.isVisible())
        check("恢复后挂起标记清除", dock._fs_suppressed is False)

        # ---------- 让位期间隐藏/恢复是幂等的 ----------
        dock.enter_fullscreen_suppression("测试程序")
        dock.enter_fullscreen_suppression("测试程序")
        dock.exit_fullscreen_suppression()
        dock.exit_fullscreen_suppression()
        pump(app, 0.3)
        check("重复进入/退出让位后 AppBar 状态正确", sys32.is_appbar_registered() is True)
        check("重复进入/退出让位后工作区正确",
              sys32.refresh_metrics()["work"][3] == dock_top)

        # ---------- 真·自动检测：造一个铺满屏幕的前台窗口，全程不碰让位方法 ----------
        import win32gui
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        fake = QWidget()
        fake.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        fake.setStyleSheet("background: #202020;")
        # 用整块屏幕（不是工作区）的几何，模拟一个真正的全屏窗口
        fake.setGeometry(app.primaryScreen().geometry())
        fake.show()
        fake.raise_()
        fake.activateWindow()
        fake_hwnd = int(fake.winId())
        for _ in range(10):   # 抢前台：Windows 偶尔会拒绝一次
            try:
                win32gui.SetForegroundWindow(fake_hwnd)
            except Exception:
                pass
            app.processEvents()
            if int(win32gui.GetForegroundWindow() or 0) == fake_hwnd:
                break
            time.sleep(0.1)
        print("      模拟全屏窗口 hwnd=0x%X rect=%s monitor=%s"
              % (fake_hwnd, win32gui.GetWindowRect(fake_hwnd),
                 sys32.monitor_rect_for_window(fake_hwnd)))
        got_foreground = int(win32gui.GetForegroundWindow() or 0) == fake_hwnd

        if not got_foreground:
            # 抢不到前台时，全屏判定链路（前台窗口 → 监听线程 → 让位）根本没法触发，
            # 这不是功能回归：跳过这段，其余检查照跑。
            print("      [SKIP] 模拟全屏窗口抢不到前台（当前前台=0x%X，Windows 前台锁"
                  "在桌面正被使用时就会拒绝），跳过自动让位检测；"
                  "显式调用让位入口的那段已经验证过了"
                  % int(win32gui.GetForegroundWindow() or 0))
            fake.hide()
            fake.close()
            pump(app, 0.4)
        else:
            check("模拟全屏窗口已成为前台窗口", True)
            pump(app, 3.0)        # 等监听线程轮询（400ms）+ 去抖
            check("自动检测到全屏窗口并让位（未手动调用让位方法）",
                  dock._fs_suppressed is True, "suppressed=%s" % dock._fs_suppressed)
            check("自动让位后 dock 已隐藏", not dock.isVisible())
            check("自动让位后扩展窗口已隐藏",
                  extension is None or not extension.isVisible())
            check("自动让位后 AppBar 已注销", sys32.is_appbar_registered() is False)
            check("自动让位后工作区已还给系统",
                  sys32.refresh_metrics()["work"][3] > dock_top)

            fake.hide()
            fake.close()
            pump(app, 3.0)        # 等退出确认轮数（2 轮 ≈ 800ms）
            check("全屏窗口关闭后自动恢复",
                  dock._fs_suppressed is False and dock.isVisible(),
                  "suppressed=%s visible=%s" % (dock._fs_suppressed, dock.isVisible()))
            check("自动恢复后扩展窗口一并显示",
                  extension is None or extension.isVisible())
            check("自动恢复后 AppBar 已重新注册", sys32.is_appbar_registered() is True)
            check("自动恢复后保留高度与原先一致",
                  sys32.refresh_metrics()["work"][3] == dock_top,
                  "%s vs %s" % (sys32.refresh_metrics()["work"][3], dock_top))

        exit_code = 1 if failures else 0
    finally:
        # 收尾：先停线程再注销 AppBar，最后直接退出进程（不触发 DockApp.exit_app
        # 里隐藏任务栏之外的动作，避免把验证脚本本身搞复杂）
        try:
            if dock is not None:
                dock._stop_fullscreen_watch()
                scan_worker = getattr(dock, '_scan_worker', None)
                if scan_worker is not None:
                    scan_worker.stop()
                dock.thread_manager.stop_all()
        except Exception as exc:  # noqa: BLE001
            print(f"停止后台线程时出错: {exc}")
        try:
            sys32.remove_appbar()
        except Exception as exc:  # noqa: BLE001
            print(f"注销 AppBar 时出错: {exc}")
        if dock is not None:
            check("收尾后 AppBar 已注销", sys32.is_appbar_registered() is False)
        print()
        print("FAILED: %d" % len(failures))
        for name in failures:
            print("  - " + name)
        sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)

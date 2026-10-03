"""扩展窗口状态面板回归：图标/文案映射、电池显隐与宽度、点击行为。

面板的状态来源是可注入的（``status_api``），所以这里用假 API 跑完整交互，**不碰
真实系统**：不打开系统面板、不读 COM（``QT_QPA_PLATFORM=offscreen``）。
纯后端 :mod:`core.system_status` 的只读接口另外做几项烟测。
"""

import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from core import system_status
from core.dock.dock_constants import DockConstants
from core.dock.extension_panel import (
    ExtensionPanel,
    battery_icon_name,
    battery_tooltip,
    network_icon_name,
    network_tooltip,
    volume_icon_name,
    volume_tooltip,
)

RES = os.path.join(ROOT, "res")

# ---------------------------------------------------------------- 图标映射
check("断网 → 断开图标", network_icon_name({"connected": False}) == "icon_network_off.png")
check("有线 → 有线图标", network_icon_name({"connected": True, "medium": "ethernet"})
      == "icon_network_eth.png")
check("无线 → Wi-Fi 图标", network_icon_name({"connected": True, "medium": "wifi"})
      == "icon_network.png")
check("未知网络 → 中性 Wi-Fi 图标（不是断开）",
      network_icon_name({}) == "icon_network.png")

check("静音 → 静音图标", volume_icon_name({"percent": 40, "muted": True})
      == "icon_volume_mute.png")
check("音量为 0 → 静音图标", volume_icon_name({"percent": 0, "muted": False})
      == "icon_volume_mute.png")
check("音量 30 → 低音量图标", volume_icon_name({"percent": 30, "muted": False})
      == "icon_volume_low.png")
check("音量 70 → 高音量图标", volume_icon_name({"percent": 70, "muted": False})
      == "icon_volume_high.png")
check("音量未知 → 中性图标", volume_icon_name({}) == "icon_volume_high.png")

check("充电中 → 充电图标", battery_icon_name({"percent": 50, "charging": True})
      == "icon_battery_charging.png")
check("电量 90 → 满格图标", battery_icon_name({"percent": 90}) == "icon_battery_full.png")
check("电量 55 → 中格图标", battery_icon_name({"percent": 55}) == "icon_battery_mid.png")
check("电量 20 → 低格图标", battery_icon_name({"percent": 20}) == "icon_battery_low.png")
check("电量 5 → 空图标", battery_icon_name({"percent": 5}) == "icon_battery_empty.png")

check("网络文案含名称与互联网状态",
      "WLAN 4" in network_tooltip({"connected": True, "internet": True,
                                   "name": "WLAN 4", "medium": "wifi"})
      and "互联网" in network_tooltip({"connected": True, "internet": True,
                                       "name": "WLAN 4", "medium": "wifi"}))
check("无互联网的文案会点明",
      "无 Internet" in network_tooltip({"connected": True, "internet": False, "name": "X"}))
check("未连接文案", network_tooltip({"connected": False}) == "网络：未连接")
check("静音文案带百分比与静音标记",
      volume_tooltip({"percent": 30, "muted": True}) == "音量：30%（已静音）")
check("音量未知文案", "读取失败" in volume_tooltip({"percent": None, "error": "boom"}))
check("充电中文案", battery_tooltip({"percent": 66, "charging": True}) == "电源：66%（充电中）")
check("电池供电文案", battery_tooltip({"percent": 20, "ac": False}) == "电源：20%（使用电池）")

# 面板引用的图标文件都要真实存在（图标是手工用 make_app_icon 模板合成的，
# 改名/漏文件时这里要立刻报出来，而不是界面上出现一个空白按钮）
ICON_FILES = {
    "icon_notify.png",
    network_icon_name({"connected": False}),
    network_icon_name({"connected": True, "medium": "ethernet"}),
    network_icon_name({"connected": True, "medium": "wifi"}),
    network_icon_name({}),
    volume_icon_name({"muted": True}),
    volume_icon_name({"percent": 30}),
    volume_icon_name({"percent": 90}),
    volume_icon_name({}),
    battery_icon_name({"charging": True}),
    battery_icon_name({"percent": 90}),
    battery_icon_name({"percent": 55}),
    battery_icon_name({"percent": 20}),
    battery_icon_name({"percent": 5}),
}
missing_icons = sorted(n for n in ICON_FILES if not os.path.exists(os.path.join(RES, n)))
check("面板用到的图标文件都存在", not missing_icons, missing_icons)


# ---------------------------------------------------------------- 假状态 API
class FakeApi:
    def __init__(self, power):
        self.power = power
        self.calls = []

    def power_status(self):
        return self.power

    def open_quick_settings(self, kind="network"):
        self.calls.append(("quick", kind))
        return True

    def open_notification_center(self):
        self.calls.append(("notify",))
        return True


POWER = {"present": True, "percent": 72, "ac": False, "charging": False, "error": ""}
SNAPSHOT = {
    "power": POWER,
    "network": {"connected": True, "internet": True, "name": "WLAN 4",
                "medium": "wifi", "error": ""},
    "volume": {"percent": 62, "muted": False, "error": ""},
}

api = FakeApi(POWER)
panel = ExtensionPanel(RES, status_worker=None, status_api=api, log=None)
panel.apply_status(SNAPSHOT)

check("面板包含 4 个按钮 + 分隔符",
      all(hasattr(panel, n) for n in
          ("network_button", "volume_button", "battery_button", "notify_button"))
      and panel.separator is not None)
check("面板里已经没有输入法按钮", not hasattr(panel, "ime_button"))
check("按钮尺寸与 dock 一致",
      panel.network_button.width() == DockConstants.BUTTON_SIZE
      and panel.network_button.height() == DockConstants.BUTTON_SIZE)
check("有电池时显示电源按钮", not panel.battery_button.isHidden())
check("电源 tooltip 显示电量与状态",
      panel.battery_button.toolTip() == "电源：72%（使用电池）", panel.battery_button.toolTip())
check("网络 tooltip 显示网络名与介质",
      "WLAN 4" in panel.network_button.toolTip() and "无线" in panel.network_button.toolTip(),
      panel.network_button.toolTip())
check("音量 tooltip 显示百分比",
      panel.volume_button.toolTip() == "音量：62%", panel.volume_button.toolTip())
check("通知入口 tooltip", panel.notify_button.toolTip() == "通知中心")
check("状态按钮图标非空",
      not panel.network_button.icon().isNull() and not panel.volume_button.icon().isNull()
      and not panel.battery_button.icon().isNull())

width_with_battery = panel.preferred_width()

# 点击行为：三个状态按钮打开原生快速设置（带 kind），通知入口打开通知中心
panel.network_button.click()
panel.volume_button.click()
panel.battery_button.click()
panel.notify_button.click()
check("点击网络/音量/电源分别请求快速设置",
      [c for c in api.calls if c[0] == "quick"]
      == [("quick", "network"), ("quick", "volume"), ("quick", "power")], api.calls)
check("点击通知入口请求通知中心",
      [c for c in api.calls if c[0] == "notify"] == [("notify",)], api.calls)

# 台式机（没有电池）→ 电源按钮隐藏，面板随之变窄
api_desktop = FakeApi({"present": False, "percent": None, "ac": None, "charging": False})
desktop = ExtensionPanel(RES, status_worker=None, status_api=api_desktop, log=None)
desktop.apply_status({"power": {"present": False, "percent": None, "ac": None, "charging": False}})
check("没有电池时隐藏电源按钮", desktop.battery_button.isHidden())
check("没有电池时面板更窄", desktop.preferred_width() < width_with_battery,
      "%d < %d" % (desktop.preferred_width(), width_with_battery))
check("没有电池时 tooltip 也不提电池",
      "电池" not in desktop.network_button.toolTip()
      and "电池" not in desktop.volume_button.toolTip())

# 拔掉/插回电池（HID 变化）→ 通知 dock 重排
hints = []
desktop.preferred_width_changed.connect(hints.append)
desktop.apply_status({"power": {"present": True, "percent": 50, "ac": False,
                                "charging": False}})
check("电源按钮显隐变化会发出宽度提示", hints and not desktop.battery_button.isHidden(), hints)

# ---------------------------------------------------------------- 交互结果分派
# 三个状态按钮要打开**不同**的系统界面（曾经全都被映射到网络那条 URI）：
#   网络 → WLAN 网络列表（ms-availablenetworks:）
#   音量 → 快速设置主界面（没有 URI：先开 WLAN 页，再 UIA 点「后退」）
#   电源 → 设置的「电池」页（ms-settings:batterysaver）
# 这里把底层"真的打开"的动作替换成记录器，验证分派表本身。
calls = []
real_shell_open = system_status.shell_open
real_quick_main = system_status.open_quick_settings_main
system_status.shell_open = lambda uri: (calls.append(("shell", uri)), True)[1]
system_status.open_quick_settings_main = lambda *a, **k: (calls.append(("main",)), True)[1]
try:
    system_status.open_quick_settings("network")
    system_status.open_quick_settings("volume")
    system_status.open_quick_settings("power")
    system_status.open_notification_center()
finally:
    system_status.shell_open = real_shell_open
    system_status.open_quick_settings_main = real_quick_main

check("网络按钮打开 WLAN 网络列表",
      ("shell", "ms-availablenetworks:") in calls, calls)
check("音量按钮打开快速设置主界面（不是 WLAN 列表）",
      ("main",) in calls, calls)
check("电源按钮打开设置的电池页",
      ("shell", "ms-settings:batterysaver") in calls, calls)
check("通知入口走 ms-actioncenter:",
      ("shell", "ms-actioncenter:") in calls, calls)
check("三个状态按钮的交互结果互不相同",
      len({("shell", "ms-availablenetworks:"), ("main",),
           ("shell", "ms-settings:batterysaver")} & set(calls)) == 3, calls)

# open_quick_settings_main 的行为：先开 WLAN 页，再安排"点后退"（不真的点）
calls.clear()
real_schedule = system_status._schedule_back_click
system_status.shell_open = lambda uri: (calls.append(("shell", uri)), True)[1]
system_status._schedule_back_click = lambda *a, **k: calls.append(("schedule-back",))
try:
    result = system_status.open_quick_settings_main()
    first_calls = list(calls)
    calls.clear()
    system_status.shell_open = lambda uri: False
    result_fail = system_status.open_quick_settings_main()
    fail_calls = list(calls)
finally:
    system_status.shell_open = real_shell_open
    system_status._schedule_back_click = real_schedule
check("主界面：先开 WLAN 页、再安排点【后退】",
      result and first_calls == [("shell", "ms-availablenetworks:"), ("schedule-back",)],
      first_calls)
check("URI 打不开时不再安排点【后退】",
      result_fail is False and fail_calls == [], fail_calls)

# ---------------------------------------------------------------- 后端只读烟测
power = system_status.power_status()
check("power_status 结构完整",
      {"present", "percent", "ac", "charging", "error"} <= set(power) and not power["error"], power)
check("windows_build 是正整数", system_status.windows_build() > 0)
check("is_windows_11 与版本号一致",
      system_status.is_windows_11() == (system_status.windows_build() >= 22000))
check("collect_status 不再采集输入法",
      set(system_status.collect_status()) == {"power", "network", "volume", "windows_build"})
check("后端不再提供输入法接口",
      not any(hasattr(system_status, name) for name in
              ("list_imes", "current_ime", "switch_ime", "layout_display_name")))
check("ms-availablenetworks: 已注册（快速设置）",
      system_status.uri_registered("ms-availablenetworks:"))
check("ms-actioncenter: 已注册（通知中心）",
      system_status.uri_registered("ms-actioncenter:"))
check("不存在的 URI 不会被当成已注册",
      not system_status.uri_registered("ms-definitely-not-registered:"))

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

import sys
import os
import winreg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout,
    QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QSpacerItem, QSpinBox, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget)
import datetime
import requests
import core.config_manager as Config
from core import log_maker

import BlurWindow.blurWindow as blurWindow

log = log_maker.logger()

ABOUT_TEXT = f"&&PROJNAME&&\n版本 &&VERSION&&\n\n&&PROJNAME&& 是开源软件，依据 &&LICENSE&& 开源协议管理源代码及其二进制文件分发\n\n&&CONTRIBUTION&&\n\n&&UPDINFO&&"
PROJ_NAME = "MikaDesktop"

LICENSE = "BSD 3-Clause License"
CONTRIBUTION = f"""有许多人为 {PROJ_NAME} 做出了贡献，访问 {PROJ_NAME} 的 GitHub 仓库以获取更多信息。

同时衷心感谢以下项目，没有他们，米卡桌面或许只是一群虫子（？）：
Yohaku 余白
https://github.com/DavidF-Dev/Yohaku
我们借鉴了这个项目的AppBar窗口注册相关写法"""

NO_UPD_TEXT = f"已为最新版本。"
NEW_UPD_TEXT = f"存在新版本"
ERROR_UPD_TEXT = f"检查更新失败："

BASE_CHECK_UPD_URL = "https://api.github.com/repos/KazumaRimatsu/MikaDesktop"
LATEST_CHECK_UPD_URL = f"{BASE_CHECK_UPD_URL}/activity?per_page=1"
RELEASE_CHECK_UPD_URL = f"{BASE_CHECK_UPD_URL}/releases"

AUTOSTART_KEY = "MikaDesktop"


class SettingsUI(QDialog):
    def __init__(self, version: str = "unknown", is_nuitka: bool = False,
                 config_path: str = None, on_save_callback=None):
        super().__init__()
        self.version = version
        self.is_nuitka = is_nuitka
        self.config_path = config_path
        self.on_save_callback = on_save_callback
        self.config_data = {}
        self.load_config_data()
        self.init_ui()

    def load_config_data(self):
        if self.config_path and os.path.exists(self.config_path):
            self.config_data = Config.load_config(self.config_path)
        else:
            self.config_data = Config.DEFAULT_CONFIG.copy()

    def init_ui(self):
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(640, 480)
        self.setWindowTitle(f"{PROJ_NAME} - 设置")
        self.verticalLayout_3 = QVBoxLayout(self)
        self.tabWidget = QTabWidget(self)

        self.general = QWidget()
        self.verticalLayout_5 = QVBoxLayout(self.general)

        self.enable_autostart = QCheckBox(self.general)
        self.verticalLayout_5.addWidget(self.enable_autostart)

        self.enable_debug = QCheckBox(self.general)
        self.verticalLayout_5.addWidget(self.enable_debug)

        self.nocmd_mode = QCheckBox(self.general)
        self.verticalLayout_5.addWidget(self.nocmd_mode)

        self.verticalSpacer = QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.verticalLayout_5.addItem(self.verticalSpacer)

        self.tabWidget.addTab(self.general, "")

        self.dock = QWidget()
        # Dock 页原本没给 tab 自己设布局（QGroupBox 直接挂在 self.dock 上靠默认
        # 几何显示）。这里补一个纵向布局，好把「全屏程序」分组放进同一页。
        self.verticalLayout_7 = QVBoxLayout(self.dock)

        self.except_apps = QGroupBox(self.dock)
        self.verticalLayout = QVBoxLayout(self.except_apps)

        self.except_apps_tips_label = QLabel(self.except_apps)
        self.verticalLayout.addWidget(self.except_apps_tips_label)

        self.plainTextEdit = QPlainTextEdit(self.except_apps)
        self.plainTextEdit.setStyleSheet(u"background: #F8F9FA")
        self.plainTextEdit.setDocumentTitle(u"")
        self.plainTextEdit.setPlaceholderText(u"例如：notepad.exe\\nmsedge.exe")
        self.verticalLayout.addWidget(self.plainTextEdit)

        self.verticalLayout_7.addWidget(self.except_apps)

        # ---- 全屏程序让位 ----
        self.fullscreen_group = QGroupBox(self.dock)
        self.fullscreen_layout = QVBoxLayout(self.fullscreen_group)

        self.fullscreen_enabled = QCheckBox(self.fullscreen_group)
        self.fullscreen_layout.addWidget(self.fullscreen_enabled)

        self.fullscreen_tips_label = QLabel(self.fullscreen_group)
        self.fullscreen_tips_label.setWordWrap(True)
        self.fullscreen_layout.addWidget(self.fullscreen_tips_label)

        self.verticalLayout_7.addWidget(self.fullscreen_group)

        #self.verticalLayout_2.addWidget(self.except_apps)

        self.tabWidget.addTab(self.dock, "")

        # ---- XHT 页：启动位置 + 通知提示 ----
        self.xht = QWidget()
        self.verticalLayout_6 = QVBoxLayout(self.xht)

        self.windowpos_label = QLabel(self.xht)
        self.verticalLayout_6.addWidget(self.windowpos_label)

        self.windowpos = QComboBox(self.xht)
        self.verticalLayout_6.addWidget(self.windowpos)

        self.notify_enabled = QCheckBox(self.xht)
        self.verticalLayout_6.addWidget(self.notify_enabled)

        self.notify_mode_label = QLabel(self.xht)
        self.verticalLayout_6.addWidget(self.notify_mode_label)

        self.notify_mode = QComboBox(self.xht)
        self.verticalLayout_6.addWidget(self.notify_mode)

        self.notify_duration_label = QLabel(self.xht)
        self.verticalLayout_6.addWidget(self.notify_duration_label)

        self.notify_duration = QSpinBox(self.xht)
        self.notify_duration.setRange(0, 30)
        self.notify_duration.setSuffix(u" 秒")
        self.verticalLayout_6.addWidget(self.notify_duration)

        # Toast 内容元素：按钮 / 图片 / 点击通知本体激活应用
        self.notify_actions = QCheckBox(self.xht)
        self.verticalLayout_6.addWidget(self.notify_actions)

        self.notify_images = QCheckBox(self.xht)
        self.verticalLayout_6.addWidget(self.notify_images)

        self.notify_click_activates = QCheckBox(self.xht)
        self.verticalLayout_6.addWidget(self.notify_click_activates)

        self.notify_tips_label = QLabel(self.xht)
        self.notify_tips_label.setWordWrap(True)
        self.verticalLayout_6.addWidget(self.notify_tips_label)

        self.verticalSpacer_6 = QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.verticalLayout_6.addItem(self.verticalSpacer_6)

        self.tabWidget.addTab(self.xht, "")

        self.about = QWidget()
        self.verticalLayout_4 = QVBoxLayout(self.about)
        self.about_text = QTextEdit(self.about)
        self.about_text.setDocumentTitle(u"")
        self.about_text.setAcceptRichText(True)
        self.about_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard|Qt.TextInteractionFlag.TextSelectableByMouse)

        self.verticalLayout_4.addWidget(self.about_text)

        self.horizontalLayout_3 = QHBoxLayout()
        self.check_upd_button = QPushButton(self.about)
        self.check_upd_button.setStyleSheet(u"QPushButton {\n"
"                background-color: #F8F9FA;\n"
"                color: #212529;\n"
"                border: 1px solid #ADB5BD;\n"
"                border-radius: 5px;\n"
"                padding: 12px;\n"
"                font-size: 15px;\n"
"				font-family: 'Microsoft YaHei UI';\n"
"				font-weight: Bold;\n"
"                min-height: 20px;\n"
"            }\n"
"            QPushButton:hover {\n"
"                background-color: #80E0D7;\n"
"                border: 1px solid #39C5BB;\n"
"            }\n"
"            QPushButton:pressed {\n"
"                background-color: #2A9A91;\n"
"                border: 1px solid #2A9A91;\n"
"                color: white;\n"
"            }")

        self.horizontalLayout_3.addWidget(self.check_upd_button)

        self.horizontalSpacer_2 = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.horizontalLayout_3.addItem(self.horizontalSpacer_2)

        self.verticalLayout_4.addLayout(self.horizontalLayout_3)

        self.tabWidget.addTab(self.about, "")

        self.verticalLayout_3.addWidget(self.tabWidget)

        self.horizontalLayout = QHBoxLayout()

        self.status_label = QLabel(self)
        self.horizontalLayout.addWidget(self.status_label)

        self.horizontalSpacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.horizontalLayout.addItem(self.horizontalSpacer)
        self.save_button = QPushButton(self)
        self.save_button.setStyleSheet(u"QPushButton {\n"
"                background-color: #39C5BB;\n"
"                color: white;\n"
"                border: none;\n"
"                border-radius: 5px;\n"
"                padding: 12px;\n"
"                font-size: 15px;\n"
"				font-family: 'Microsoft YaHei UI';\n"
"				font-weight: Bold;\n"
"                min-height: 20px;\n"
"            }\n"
"            QPushButton:hover {\n"
"                background-color: #80E0D7;\n"
"                color: #212529;\n"
"            }\n"
"            QPushButton:pressed {\n"
"                background-color: #2A9A91;\n"
"                color: white;\n"
"            }")

        self.horizontalLayout.addWidget(self.save_button)

        self.verticalLayout_3.addLayout(self.horizontalLayout)

        self.enable_autostart.setText(u"开机时自动运行")
        self.enable_debug.setText(u"启用debug日志")
        self.nocmd_mode.setText(u"脱离命令提示符")
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.general), u"通用")
        self.except_apps.setTitle(u"排除的应用")
        self.except_apps_tips_label.setText(u"键入进程名（每行一个，无需.exe后缀）")
        self.fullscreen_group.setTitle(u"全屏程序")
        self.fullscreen_enabled.setText(u"程序全屏时隐藏 Dock 并注销 AppBar")
        self.fullscreen_tips_label.setText(
            u"非系统程序全屏显示（铺满整个显示器，不是最大化）时，解除屏幕底部的"
            u"工作区保留并隐藏 Dock，让全屏画面完整占满屏幕；全屏结束或切到别的"
            u"窗口后自动恢复。\n"
            u"系统组件（桌面、任务栏、开始菜单、锁屏、UAC 提示等）不会触发让位。")
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.dock), u"Dock")
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.xht), u"XHT")
        self.windowpos_label.setText(u"启动时位置")
        for position_value, position_label in self._windowpos_choices():
            self.windowpos.addItem(position_label, position_value)
        self.notify_enabled.setText(u"启用通知提示")
        self.notify_mode_label.setText(u"通知显示方式")
        self.notify_duration_label.setText(u"通知显示时间")
        self.notify_actions.setText(u"显示通知按钮（点击后把动作发回应用）")
        self.notify_images.setText(u"显示通知图片（应用图标 / 横幅图）")
        self.notify_click_activates.setText(u"点击通知内容时同时激活应用")
        self.notify_tips_label.setText(
            u"启动时位置：小黑条每次启动出现的位置；拖动小黑条只改变本次运行的位置，"
            u"保存设置或重启后仍以这里的设置为准。\n"
            u"不展开小黑条：完全静默，只在内部累计未读数，不改变小黑条外观。\n"
            u"只显示 🔔 图标：小黑条上出现 🔔，多个未读时显示数量，例如 🔔3。\n"
            u"展开显示通知内容：按 Windows Toast 的内容元素排版 —— 标题、正文、"
            u"归属文本（如「来自 某应用」）、提醒/闹钟/来电场景、进度条、"
            u"应用图标与横幅图，以及可点击的通知按钮。\n"
            u"通知按钮：点击后把按钮的 arguments（以及你填写的输入内容）通过应用注册的"
            u"COM 激活器发回去，和点系统通知一样；应用没注册激活器时会退回「打开该应用」。\n"
            u"显示时间 0 秒 = 不自动收起，一直显示到点击；点击 🔔 会打开系统通知中心并"
            u"清零未读（展开内容里那个 🔔N 也一样），点击展开的内容则是「标记已读」，"
            u"勾选上面的「点击通知内容时同时激活应用」后还会把该通知发回应用。")
        self._notify_modes, notify_labels = self._notify_mode_choices()
        for notify_mode in self._notify_modes:
            self.notify_mode.addItem(notify_labels.get(notify_mode, notify_mode))
        self.notify_enabled.toggled.connect(self._sync_notify_widgets)
        self.about_text.setText(ABOUT_TEXT)
        self.check_upd_button.setText(u"检查更新")
        self.check_upd_button.clicked.connect(self.check_update)
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.about), u"关于")
        self.status_label.setText(u"就绪")
        self.save_button.setText(u"  保存  ")
        self.tabWidget.setCurrentIndex(1)

        self.save_button.clicked.connect(self.save_settings)
        self.load_settings_to_ui()

        # 加载nocmd_mode设置
        nocmd_mode_enabled = self.config_data.get('nocmd_mode', False)
        self.nocmd_mode.setChecked(nocmd_mode_enabled)
        self.upd_about_text()

        QTimer.singleShot(100, self.apply_blur_effect)

        self.upd_status("就绪")

    @staticmethod
    def _windowpos_choices():
        """小黑条启动位置的选项：值沿用 XHT 的 L / M / R。"""
        return (
            ("L", u"左侧贴边"),
            ("M", u"居中（从上方滑入）"),
            ("R", u"右侧贴边"),
        )

    def _notify_mode_choices(self):
        """通知显示方式的选项：优先用 XHT 里的定义，取不到就用内置副本兜底。"""
        try:
            from features.XHT.Lib.Notify import MODE_LABELS, MODES
            return tuple(MODES), dict(MODE_LABELS)
        except Exception:  # noqa: BLE001 - 设置界面不该因为 XHT 导入失败而打不开
            return ("silent", "badge", "expand"), {
                "silent": u"不展开小黑条（静默）",
                "badge": u"只显示 🔔 图标和未读数量",
                "expand": u"展开显示通知内容",
            }

    def _sync_notify_widgets(self, enabled: bool):
        """关掉「启用通知提示」时，把下面的通知相关控件一起置灰。"""
        self.notify_mode.setEnabled(bool(enabled))
        self.notify_duration.setEnabled(bool(enabled))
        self.notify_actions.setEnabled(bool(enabled))
        self.notify_images.setEnabled(bool(enabled))
        self.notify_click_activates.setEnabled(bool(enabled))

    def load_settings_to_ui(self):
        debug_enabled = self.config_data.get('debug', False)
        self.enable_debug.setChecked(debug_enabled)

        dock_config = self.config_data.get('dock', {})

        except_list = dock_config.get('except_processes', [])
        self.plainTextEdit.setPlainText('\n'.join(except_list))

        # XHT：启动位置 + 通知提示
        xht_config = self.config_data.get('xht', {}) or {}
        position = str(xht_config.get('windowpos', 'R')).upper()
        position_index = self.windowpos.findData(position)
        if position_index < 0:
            position_index = self.windowpos.findData('R')
        self.windowpos.setCurrentIndex(max(position_index, 0))
        self.notify_enabled.setChecked(bool(xht_config.get('notify_enabled', True)))
        modes = getattr(self, '_notify_modes', ())
        mode = str(xht_config.get('notify_mode', 'badge'))
        if mode in modes:
            self.notify_mode.setCurrentIndex(modes.index(mode))
        elif len(modes) > 1:
            self.notify_mode.setCurrentIndex(1)
        try:
            duration = int(xht_config.get('notify_duration', 6))
        except (TypeError, ValueError):
            duration = 6
        self.notify_duration.setValue(max(0, min(duration, 30)))
        # Toast 内容元素：按钮 / 图片 / 点击内容激活应用
        self.notify_actions.setChecked(bool(xht_config.get('notify_actions', True)))
        self.notify_images.setChecked(bool(xht_config.get('notify_images', True)))
        self.notify_click_activates.setChecked(
            bool(xht_config.get('notify_click_activates', False)))
        self._sync_notify_widgets(self.notify_enabled.isChecked())

        # 全屏程序让位
        fs_config = self.config_data.get('fullscreen', {}) or {}
        self.fullscreen_enabled.setChecked(bool(fs_config.get('enabled', True)))

        self.check_autostart_status()

    def check_autostart_status(self):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ
            )
            try:
                value, _ = winreg.QueryValueEx(key, AUTOSTART_KEY)
                self.enable_autostart.setChecked(True)
            except FileNotFoundError:
                self.enable_autostart.setChecked(False)
            winreg.CloseKey(key)
        except Exception as e:
            log.error(f"读取自启动注册表失败: {e}")
            self.enable_autostart.setChecked(False)

    def set_autostart(self, enable: bool):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
            )
            if enable:
                exe_path = sys.executable
                if not self.is_nuitka and not getattr(sys, 'frozen', False):
                    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dock.py")
                    if os.path.exists(script_path):
                        value = f'"{exe_path}" "{script_path}"'
                    else:
                        value = f'"{exe_path}"'
                else:
                    value = f'"{exe_path}"'
                winreg.SetValueEx(key, AUTOSTART_KEY, 0, winreg.REG_SZ, value)
                log.info(f"已添加自启动: {value}")
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_KEY)
                    log.info("已移除自启动")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            log.error(f"设置自启动失败: {e}")

    def collect_settings(self):
        dock_config = self.config_data.get('dock', {})
        dock_config['except_processes'] = [
            line.strip() for line in self.plainTextEdit.toPlainText().split('\n') if line.strip()
        ]

        self.config_data['nocmd_mode'] = self.nocmd_mode.isChecked()
        self.config_data['debug'] = self.enable_debug.isChecked()
        self.config_data['dock'] = dock_config

        # 全屏程序让位：界面只管理开关，poll_interval_ms 之类的调参项保持在
        # 配置文件里手改的值
        fs_config = self.config_data.get('fullscreen', {}) or {}
        fs_config['enabled'] = self.fullscreen_enabled.isChecked()
        # 已删掉的「不让位的程序」排除列表：老配置里残留的键顺手清掉，别再写回去
        fs_config.pop('except_processes', None)
        self.config_data['fullscreen'] = fs_config

        # XHT：启动位置 + 通知提示
        xht_config = self.config_data.get('xht', {}) or {}
        xht_config['windowpos'] = self.windowpos.currentData() or 'R'
        xht_config['notify_enabled'] = self.notify_enabled.isChecked()
        modes = getattr(self, '_notify_modes', ())
        index = self.notify_mode.currentIndex()
        xht_config['notify_mode'] = modes[index] if 0 <= index < len(modes) else 'badge'
        xht_config['notify_duration'] = int(self.notify_duration.value())
        xht_config['notify_actions'] = bool(self.notify_actions.isChecked())
        xht_config['notify_images'] = bool(self.notify_images.isChecked())
        xht_config['notify_click_activates'] = bool(self.notify_click_activates.isChecked())
        self.config_data['xht'] = xht_config

    def save_settings(self):
        try:
            self.collect_settings()

            if self.config_path:
                # dock.apps 由主界面（DockApp）维护，设置界面不管理它。这里重新
                # 读一次磁盘上的值覆盖回对话框打开时的旧快照，否则「保存设置」会
                # 把主界面在这期间新增/删除的应用列表整个回滚掉。
                latest = Config.load_config(self.config_path)
                merged = dict(self.config_data)
                dock_config = dict(merged.get("dock", {}) or {})
                dock_config["apps"] = (latest.get("dock", {}) or {}).get("apps", [])
                merged["dock"] = dock_config
                self.config_data = merged
                Config.save_config(self.config_path, merged)

            self.set_autostart(self.enable_autostart.isChecked())

            if self.enable_debug.isChecked():
                log.enable_debug()
            else:
                log.disable_debug()

            self.upd_status(u"设置已保存", "success")

            if self.on_save_callback:
                self.on_save_callback(self.config_data)

            log.info("设置已保存")
        except Exception as e:
            self.upd_status(f"保存设置失败: {e}", "error")
            log.error(f"保存设置失败: {e}")

    def upd_status(self, text: str, status_type: str = None):
        if status_type == "success":
            self.status_label.setStyleSheet(u"color: #4DB6AC;")  # Success
            self.status_label.setText(text)
        elif status_type == "error":
            self.status_label.setStyleSheet(u"color: #EF5350;")  # Error
            self.status_label.setText(text)
        elif status_type == "warning":
            self.status_label.setStyleSheet(u"color: #FFB74D;")  # Warning
            self.status_label.setText(text)
        else:
            self.status_label.setStyleSheet(u"color: #39C5BB;")  # Primary
            self.status_label.setText(text)

    @staticmethod
    def _parse_github_time(value):
        """解析 GitHub 返回的 ISO-8601 UTC 时间戳（如 ``2026-10-02T18:30:43Z``）。

        解析失败返回 None，由调用方决定降级行为，而不是抛异常打断界面。
        """
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.datetime.strptime(text, fmt).replace(
                    tzinfo=datetime.timezone.utc
                )
            except ValueError:
                continue
        try:  # 兜底：交给 fromisoformat（Python 3.11+ 能处理更多写法）
            return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _local_reference_time():
        """本地版本的「时间基准」，用于判断远端是否有更新。

        源码运行：取 ``dock.py`` 的修改时间；编译后：取可执行文件时间。
        取不到就返回 None（此时不做新旧判断，直接报「已为最新版本」）。
        """
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        else:
            candidates.append(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dock.py"
            ))
        for path in candidates:
            try:
                return datetime.datetime.fromtimestamp(
                    os.path.getmtime(path), datetime.timezone.utc
                )
            except OSError:
                continue
        return None

    def check_update(self):
        """检查是否有新版本。

        旧实现的比较逻辑是反的（拿远端时间戳和「当前时间」比，且混用了
        naive/aware datetime），所以永远报「已为最新」；同时用
        ``list(response.text)`` 把 JSON 文本拆成了字符列表，空判断也失效。
        这里改成：远端发布时间 vs 本地文件修改时间（都是 UTC aware）。
        """
        reference = self._local_reference_time()
        headers = {"Accept": "application/vnd.github+json"}

        try:
            if self.is_nuitka:
                self.upd_status("编译环境，检查Release")
                response = requests.get(RELEASE_CHECK_UPD_URL, headers=headers, timeout=10)
                if response.status_code != 200:
                    self.upd_status("检查更新失败", "error")
                    self.upd_about_text(upd_text=f"{ERROR_UPD_TEXT}{response.status_code}")
                    return
                releases = response.json()
                if not isinstance(releases, list) or not releases:
                    self.upd_status("没有Releases", "warning")
                    self.upd_about_text(upd_text=NO_UPD_TEXT)
                    return
                remote_time = self._parse_github_time(releases[0].get("published_at"))
            else:
                self.upd_status("非编译环境，检查最近推送")
                response = requests.get(LATEST_CHECK_UPD_URL, headers=headers, timeout=10)
                if response.status_code != 200:
                    self.upd_status("检查更新失败", "error")
                    self.upd_about_text(upd_text=f"{ERROR_UPD_TEXT}{response.status_code}")
                    return
                events = response.json()
                if not isinstance(events, list) or not events:
                    self.upd_status("没有latest Builds", "warning")
                    self.upd_about_text(upd_text=NO_UPD_TEXT)
                    return
                newest = events[0]
                remote_time = self._parse_github_time(newest.get("timestamp"))
                if remote_time is None:  # /commits 风格的返回
                    commit = newest.get("commit") or {}
                    committer = commit.get("committer") or {}
                    remote_time = self._parse_github_time(committer.get("date"))
        except requests.exceptions.RequestException as exc:
            self.upd_status("检查更新失败", "error")
            self.upd_about_text(upd_text=f"{ERROR_UPD_TEXT}{exc}")
            return
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            self.upd_status("检查更新失败", "error")
            self.upd_about_text(upd_text=f"{ERROR_UPD_TEXT}{exc}")
            return

        has_update = bool(reference and remote_time and remote_time > reference)
        if has_update:
            self.upd_status("存在新版本", "success")
            self.upd_about_text(upd_text=NEW_UPD_TEXT)
        else:
            self.upd_status("暂无新版本", "success")
            self.upd_about_text(upd_text=NO_UPD_TEXT)

    def upd_about_text(self, upd_text: str = "", contribution: bool = True):
        self.about_text.setText(ABOUT_TEXT.replace("&&VERSION&&", self.version)
            .replace("&&PROJNAME&&", PROJ_NAME)
            .replace("&&LICENSE&&", LICENSE)
            .replace("&&CONTRIBUTION&&", CONTRIBUTION if contribution else "")
            .replace("&&UPDINFO&&", upd_text))
        
    def apply_blur_effect(self):
        """应用窗口模糊效果"""
        try:
            # 使用GlobalBlur函数为窗口添加模糊效果
            blurWindow.blur(self.winId(), hexColor=False, Dark=True)
        except Exception as e:
            log.warning(f"设置窗口模糊失败: {e}")



if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SettingsUI(is_nuitka=False, config_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json"))
    window.show()
    sys.exit(app.exec())

"""XHT 的通知提示：把 :mod:`features.catch_notify` 抓到的通知显示在小黑条上。

显示方式（``settings.json`` 里的 ``xht.notify_mode``）
---------------------------------------------------
=========  ==================================================================
``silent`` 不展开小黑条：**完全静默** —— 不弹出、不显示图标，只在内部累计未读数
``badge``  只添加一个 🔔 图标：有多个未读时显示数量，例如 ``🔔3``
``expand`` 展开显示通知内容：小黑条展开，按 **Toast 内容元素** 排版显示
=========  ==================================================================

按内容元素排版（``expand``）
---------------------------
Windows Toast 的 XML 是一棵内容树，不是一个标题 + 一个正文。本模块不自己解析
XML，而是直接消费 :class:`~features.catch_notify.toast.ToastContent`：

======================  ====================================================
内容元素                显示方式
======================  ====================================================
标题（role=title）      大号加粗
正文（role=body）       中号常规，多行分别成行
归属（placement=…）     小号弱化（``来自 某某``）
``<image placement=…>`` 应用图标（标题前内联 20px）/ 横幅图（最大 320×90）
``<progress>``          小号弱化（``进度 下载 · 进行中 · 40%``）
``<header>``            小号弱化（``提醒 · 今天 20:00``）
``<toast scenario>``    小号弱化（提醒 / 闹钟 / 来电）
``<action>``            可点击的链接（``▶ 回复``）；上下文菜单项只进 tooltip
``<input>``             点击按钮时弹输入框收集内容，随激活一起发给应用
``<audio silent>``      小号弱化（``🔇``）
======================  ====================================================

只有**本地可读**的图片会被显示（``file:///…`` / 本地路径 /
``ms-appdata:///local/…``）；网络图片与包内资源会跳过 —— QLabel 不会去下载图片，
装作能显示反而会出错。图片尺寸用 :class:`QImageReader` 先量再等比缩放。

按钮点击 = 真正把动作送回应用
-----------------------------
按钮不是超链接：点击后由 :mod:`features.catch_notify.activation` 把
``arguments``（以及用户填写的输入内容）通过应用注册的
``INotificationActivationCallback`` COM 激活器发回去；拿不到激活器时按内容元素
降级（``protocol`` 动作用协议打开、通知本体用 ``shell:AppsFolder`` 拉起应用）。
COM 调用可能在等应用的后台进程，所以一律放到工作线程里做，结果通过 Qt 信号回到
GUI 线程。

显示时间（``xht.notify_duration``，0-30 秒）
-----------------------------------------
* ``0`` = 不自动收起，一直显示到用户点击；
* ``N`` = N 秒后收起内容；若还有未读则保留 ``🔔N``，并且如果小黑条是被这条通知
  自动弹出来的，还会自动收回贴边。

未读与点击
----------
* 点击 **🔔 图标**：打开**系统通知中心**，然后清零未读 —— 多条通知堆在小黑条里逐条
  点开容易乱，交给系统通知中心看更清楚。判定依据是「当前显示的是不是 🔔」，而不是
  ``xht.notify_mode``：badge 档位整体就是 🔔；expand 档位在内容自动收起后也会退化成
  ``🔔N``（见 :meth:`NotificationPresenter._on_expire`），展开内容里那行小号 ``🔔N``
  同样做成了可点链接 —— 三种形态点下去行为一致。
* 点击**展开的内容**（expand 模式）：按需把它当作"打开这条通知"发回应用，再清零
  （``mark_read()``）。

线程模型
--------
:class:`NotificationWatcher` 是个 ``QThread``，在后台跑
``features.catch_notify`` 的阻塞式 ``watch_notifications()``，通过 Qt 信号把通知
投递回 GUI 线程；激活按钮时另开一个短命的工作线程。界面更新一律发生在 GUI 线程里。
"""

from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QInputDialog, QLabel, QLineEdit

# 日志参数格式化规则只有一份实现，见 core/text_utils.py
# （原来这里和 core/log_maker.py 各写了一份，改一边另一边不会跟着变）
from core.text_utils import format_message as _format_message

logger = logging.getLogger(__name__)

__all__ = [
    "MODE_SILENT",
    "MODE_BADGE",
    "MODE_EXPAND",
    "MODES",
    "MODE_LABELS",
    "DEFAULT_MODE",
    "DEFAULT_DURATION",
    "MAX_DURATION",
    "CENTER_SCHEME",
    "format_unread",
    "NotificationBadge",
    "NotificationWatcher",
    "NotificationPresenter",
    "ActionDispatcher",
]

MODE_SILENT = "silent"
MODE_BADGE = "badge"
MODE_EXPAND = "expand"
MODES = (MODE_SILENT, MODE_BADGE, MODE_EXPAND)

#: 设置界面下拉框用的中文名（顺序与 MODES 一致）
MODE_LABELS = {
    MODE_SILENT: "不展开小黑条（静默）",
    MODE_BADGE: "只显示 🔔 图标和未读数量",
    MODE_EXPAND: "展开显示通知内容",
}

DEFAULT_MODE = MODE_BADGE
DEFAULT_DURATION = 6
MAX_DURATION = 30

#: 展开时正文最多显示多少个字，避免把小黑条撑爆
MAX_BODY_CHARS = 120

#: 展开内容时三级文字的样式：靠字号 + 字重区分，其他信息再弱化颜色
FONT_TITLE = "font-size:16px;font-weight:700;"
FONT_BODY = "font-size:14px;font-weight:400;"
FONT_META = "font-size:13px;font-weight:400;color:#B8B8B8;"
#: 按钮链接：用品牌色，和上面的文字区分开（配色见 杂物/1.md）
FONT_ACTION = "font-size:13px;font-weight:700;color:#80E0D7;text-decoration:none;"
#: 展开内容里那个 🔔N 未读计数：字号跟 meta 一致，颜色跟按钮一致（表示可点）
FONT_META_LINK = "font-size:13px;font-weight:700;color:#80E0D7;text-decoration:none;"

#: 图片尺寸上限（像素）：应用图标内联，横幅图等比缩放到这个框里
APP_LOGO_MAX = 20
HERO_MAX_WIDTH = 320
HERO_MAX_HEIGHT = 90

#: 激活结果在小黑条上停留多久（毫秒）
FEEDBACK_MS = 3000


def _default_open_center() -> bool:
    """默认的"打开系统通知中心"实现：交给 :mod:`core.system_status`。

    XHT 是本项目里的一个 feature，依赖 shared 的 core 层没问题；拿不到时返回
    ``False``，只影响"点 🔔 打开通知中心"这一步，不影响其它逻辑。
    """
    try:
        from core.system_status import open_notification_center

        return bool(open_notification_center())
    except Exception:
        logger.debug("打开系统通知中心失败（core.system_status 不可用？）")
        return False

#: 链接 href 前缀：按钮在 :attr:`ToastContent.button_actions` 里的下标
ACTION_SCHEME = "xht-action:"

#: 链接 href：展开内容里那个 🔔N 未读计数。点它 = 打开系统通知中心（含义与 badge
#: 档位下点整个 🔔 一致，见模块文档「未读与点击」）。
CENTER_SCHEME = "xht-center:"


def _clip(text, limit: int = MAX_BODY_CHARS) -> str:
    """过长就截断（只用于显示，原始文本在 Notification.xml 里完好无损）。"""
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _escape_html(text) -> str:
    """通知文本要放进富文本里，必须转义，否则应用发来的 & < > 会把排版搞乱。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def format_unread(count: int) -> str:
    """未读数 → 图标文本：``1`` → ``🔔``，``3`` → ``🔔3``。"""
    return "🔔" if count <= 1 else "🔔%d" % count


def _image_size(path: str):
    """量一下图片尺寸；读不出来（缺失 / 不是图片 / 格式不支持）返回 ``None``。"""
    try:
        if not path or not os.path.isfile(path):
            return None
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        if not reader.canRead():
            return None
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            return size.width(), size.height()
        image = reader.read()
        if image.isNull():
            return None
        return image.width(), image.height()
    except Exception as exc:  # noqa: BLE001 - 图片问题绝不能影响通知显示
        logger.debug("读取图片尺寸失败 %s：%s", path, exc)
        return None


def image_html(path: str, *, max_width: int, max_height: int,
               alt: str = "") -> str:
    """本地图片 → 富文本 ``<img>``（等比缩放；不可读就返回空串）。"""
    if not path:
        return ""
    measured = _image_size(path)
    if measured is None:
        return ""
    width, height = measured
    scale = min(1.0, max_width / float(width), max_height / float(height))
    width = max(1, int(round(width * scale)))
    height = max(1, int(round(height * scale)))
    try:
        url = QUrl.fromLocalFile(path).toString()
    except Exception:  # noqa: BLE001
        return ""
    alt_text = ' alt="%s"' % _escape_html(alt) if alt else ""
    return '<img src="%s" width="%d" height="%d"%s/>' % (
        _escape_html(url), width, height, alt_text)


class _LoggerAdapter:
    """让 printf 风格的日志调用也能用在「只接受单个 msg」的 logger 上。

    先用 :func:`core.text_utils.format_message` 把参数格式化好再交给底层 logger，
    所以无论传进来的是标准库 :mod:`logging` 还是项目的 ``core.log_maker`` 都能用；
    日志本身出错也不会影响通知功能。
    """

    __slots__ = ("_logger",)

    def __init__(self, logger_obj):
        self._logger = logger_obj

    def _emit(self, level, message, args) -> None:
        emit = getattr(self._logger, level, None)
        if emit is None:
            return
        try:
            emit(_format_message(message, args) if args else message)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响通知功能
            pass

    def debug(self, message, *args):
        self._emit("debug", message, args)

    def info(self, message, *args):
        self._emit("info", message, args)

    def warning(self, message, *args):
        self._emit("warning", message, args)

    def error(self, message, *args):
        self._emit("error", message, args)

    def critical(self, message, *args):
        self._emit("critical", message, args)


class NotificationBadge(QLabel):
    """小黑条上的通知部件：要么是 ``🔔N``，要么是通知内容；点击表示「已读」。"""

    clicked = Signal()
    #: 用户点了第 N 个可点击按钮（下标对应 :attr:`ToastContent.button_actions`）
    actionTriggered = Signal(int)
    #: 用户点了展开内容里的 🔔N 链接（= 打开系统通知中心）
    centerRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("notifyBadge")
        # 居中显示（时间标签本来就是居中的，这里保持一致）
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMaximumWidth(420)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("color: white; font-size: 13px; font-weight: bold;")
        self.setToolTip("点击标记为已读")
        # 富文本里的按钮要能点：只放行链接交互，避免误触发文本选择
        self.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setOpenExternalLinks(False)
        self.linkActivated.connect(self._on_link_activated)
        self._actions = ()
        self._link_fired = False
        #: 当前显示的是不是「只有 🔔N」（而不是通知内容）。点击语义按它分流：
        #: 显示 🔔 时点击 = 打开系统通知中心。只在 :meth:`show_badge` /
        #: :meth:`show_content` / :meth:`clear` 里维护，不会和实际显示漂移。
        self._badge_only = False
        self.setVisible(False)

    # -- 显示 -------------------------------------------------------------- #
    def show_badge(self, unread: int) -> None:
        """只显示 ``🔔`` / ``🔔N``。"""
        self._actions = ()
        self._badge_only = True
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(format_unread(unread))
        self.setToolTip("点击打开通知中心")
        self.setVisible(True)

    def is_badge_only(self) -> bool:
        """当前显示的是不是「只有 🔔N」这种形态（而不是展开的通知内容）。"""
        return bool(self._badge_only)

    def show_content(self, content, unread: int = 1, app_name: str = "",
                     package_family: str = "", feedback: str = "",
                     show_actions: bool = True, show_images: bool = True) -> None:
        """按 Toast 内容元素展开显示。

        ``content`` 是 :class:`~features.catch_notify.toast.ToastContent`（没有内容
        元素时传 ``None`` 也能工作，只是显示「（无可显示内容）」）。
        """
        content = content or _EMPTY_CONTENT
        self._badge_only = False
        lines: list = []

        if show_images:
            hero = content.hero_image
            if hero is not None:
                hero_html = image_html(hero.path(package_family) or "",
                                       max_width=HERO_MAX_WIDTH,
                                       max_height=HERO_MAX_HEIGHT,
                                       alt=hero.alt)
                if hero_html:
                    lines.append('<div>%s</div>' % hero_html)

        title_html = _escape_html(_clip(content.title, 60)) if content.title else ""
        if show_images and title_html:
            logo = content.app_logo
            if logo is not None:
                logo_html = image_html(logo.path(package_family) or "",
                                       max_width=APP_LOGO_MAX,
                                       max_height=APP_LOGO_MAX,
                                       alt=content.title)
                if logo_html:
                    title_html = "%s&nbsp;%s" % (logo_html, title_html)
        if title_html:
            lines.append('<span style="%s">%s</span>' % (FONT_TITLE, title_html))

        for line in content.body_lines:
            if line:
                lines.append('<span style="%s">%s</span>'
                             % (FONT_BODY, _escape_html(_clip(line))))

        meta = self._meta_html(content, unread, app_name, feedback)
        if meta:
            lines.append('<span style="%s">%s</span>' % (FONT_META, meta))

        self._actions = tuple(content.button_actions) if show_actions else ()
        if self._actions:
            links = []
            for index, action in enumerate(self._actions):
                label = _clip(action.label, 20)
                links.append('<a href="%s%d" style="%s">%s</a>'
                             % (ACTION_SCHEME, index, FONT_ACTION, _escape_html(label)))
            lines.append('<span style="%s">%s</span>'
                         % (FONT_META, "&nbsp;&nbsp;".join(links)))

        if not lines:
            lines.append('<span style="%s">（无可显示内容）</span>' % FONT_BODY)

        self.setTextFormat(Qt.TextFormat.RichText)
        # 居中：外层块级 div 的 text-align 对富文本最可靠 —— QLabel 的 alignment 只作用
        # 于行内内容，包一层块级元素才能保证每一行（标题/正文/其他信息）都居中。
        self.setText('<div style="text-align:center;">%s</div>' % "<br>".join(lines))
        self.setToolTip(self._tooltip(content, unread, app_name, feedback))
        self.setVisible(True)

    @staticmethod
    def _meta_parts(content, app_name: str, feedback: str) -> list:
        """小号弱化那一行的内容元素（纯文本，未读计数不在这里，见 :meth:`_meta_html`）。"""
        parts: list = []
        if feedback:
            parts.append(str(feedback))
        header = content.header
        header_label = header.label if header is not None else ""
        # 场景标签和 header 标题经常是同一个词（都叫「提醒」），只留信息更多的那个
        if content.scenario_label and not (
                header_label and header_label.startswith(content.scenario_label)):
            parts.append(content.scenario_label)
        if header_label:
            parts.append(header_label)
        if content.progress is not None and content.progress.label:
            parts.append("进度 %s" % content.progress.label)
        if content.attribution:
            parts.append(content.attribution)
        if content.is_silent:
            parts.append("🔇")
        if not content.has_text and not content.images and app_name:
            # 没有任何文本/图片可显示时，至少让用户知道是谁发的
            parts.append(app_name)
        # 兜底去重：任何重复的片段都没必要占两遍地方
        unique: list = []
        for part in parts:
            if part not in unique:
                unique.append(part)
        return unique

    def _meta_html(self, content, unread: int, app_name: str, feedback: str) -> str:
        """小号那一行的 HTML：其余片段转义后拼接，未读计数 ``🔔N`` 做成可点链接。

        未读计数必须做成**链接**而不是普通文字：整块 QLabel 的点击代表「打开这条
        通知 / 标记已读」，点 🔔 的语义是「打开系统通知中心」，两者只能用链接区分
        （``QLabel`` 上点链接会走 ``linkActivated``，不会再发 ``clicked``）。
        """
        parts = [_escape_html(part)
                 for part in self._meta_parts(content, app_name, feedback)]
        if unread > 1:
            parts.append('<a href="%s" style="%s">%s</a>'
                         % (CENTER_SCHEME, FONT_META_LINK,
                            _escape_html(format_unread(unread))))
        return " · ".join(parts)

    @staticmethod
    def _tooltip(content, unread: int, app_name: str, feedback: str) -> str:
        """完整内容放进 tooltip（正文被截断时这里还能看到全的）。"""
        parts: list = []
        if app_name:
            parts.append(app_name)
        if feedback:
            parts.append(str(feedback))
        if content.template:
            parts.append("模板：%s" % content.template)
        if content.scenario_label:
            parts.append("场景：%s" % content.scenario_label)
        for text in content.texts:
            if text.content:
                parts.append(_role_label(text.role) + text.content)
        for image in content.images:
            location = image.path() or image.src
            parts.append("%s%s" % (_image_label(image), location))
        header = content.header
        if header is not None and header.label:
            parts.append("头部：%s" % header.label)
        if content.progress is not None and content.progress.label:
            parts.append("进度：%s" % content.progress.label)
        for action in content.actions:
            marker = "菜单" if action.is_context_menu else "按钮"
            parts.append("%s：%s → %s（%s）"
                         % (marker, action.label, action.arguments or "-",
                            action.activation_type))
        for item in content.inputs:
            parts.append("输入框：%s（%s）%s"
                         % (item.id or "-", item.type,
                            ("默认 %s" % item.default_input) if item.default_input else ""))
        if content.audio is not None:
            sound = "静音" if content.audio.silent else (content.audio.src or "默认提示音")
            parts.append("声音：%s" % sound)
        if content.launch:
            parts.append("launch：%s" % content.launch)
        for message in content.parse_errors:
            parts.append("解析提示：%s" % message)
        if unread > 1:
            parts.append("未读 %d 条" % unread)
        hint = "点击标记为已读"
        if content.has_actions:
            hint += "；点击按钮可把操作发回应用"
        if unread > 1:
            hint += "；点 🔔N 打开通知中心"
        parts.append(hint)
        return "\n".join(parts)

    def clear(self) -> None:
        self._actions = ()
        self._link_fired = False
        self._badge_only = False
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText("")
        self.setToolTip("点击标记为已读")
        self.setVisible(False)

    # -- 交互 -------------------------------------------------------------- #
    def _on_link_activated(self, href: str) -> None:
        """富文本里的链接被点击（``xht-action:<下标>`` 或 ``xht-center:``）。"""
        self._link_fired = True
        text = str(href or "")
        if text.startswith(CENTER_SCHEME):
            # 点 🔔N 链接 = 打开系统通知中心；不当作「点内容」
            self.centerRequested.emit()
            return
        if not text.startswith(ACTION_SCHEME):
            return
        try:
            index = int(text[len(ACTION_SCHEME):])
        except ValueError:
            return
        if 0 <= index < len(self._actions):
            self.actionTriggered.emit(index)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 先交给 QLabel：点在按钮链接上时它会**同步**发出 linkActivated。
            # （PySide6 没有绑定 QLabel::anchorAt，只能靠这个标志区分「点按钮」和
            #  「点内容」——后者才代表「标记已读 / 打开通知」。）
            self._link_fired = False
            super().mouseReleaseEvent(event)
            if not self._link_fired:
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _role_label(role: str) -> str:
    return {"title": "标题：", "body": "正文：", "attribution": "归属："}.get(role, "文本：")


def _image_label(image) -> str:
    if image.is_hero:
        return "横幅图："
    if image.is_app_logo:
        return "应用图标："
    return "图片："


class _EmptyContent:
    """``content=None`` 时的占位对象（让显示代码不必到处判空）。"""

    template = ""
    scenario = ""
    scenario_label = ""
    attribution = ""
    title = ""
    body_lines: tuple = ()
    texts: tuple = ()
    images: tuple = ()
    actions: tuple = ()
    inputs: tuple = ()
    header = None
    progress = None
    audio = None
    launch = ""
    parse_errors: tuple = ()

    @property
    def hero_image(self):
        return None

    @property
    def app_logo(self):
        return None

    @property
    def button_actions(self) -> tuple:
        return ()

    @property
    def has_text(self) -> bool:
        return False

    @property
    def has_actions(self) -> bool:
        return False

    @property
    def is_silent(self) -> bool:
        return False


_EMPTY_CONTENT = _EmptyContent()


class NotificationWatcher(QThread):
    """后台读取通知的线程，把每条通知通过信号发回 GUI 线程。"""

    notification_received = Signal(object)   # features.catch_notify.Notification
    failed = Signal(str)

    def __init__(self, parent=None, interval: float = 0.8):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._interval = interval

    def stop(self, wait_ms: int = 3000) -> None:
        """请求停止并等待线程退出（可重复调用）。"""
        self._stop_event.set()
        if self.isRunning():
            self.wait(wait_ms)

    def quit(self) -> None:  # noqa: D102 - 覆盖 QThread.quit，配合 ThreadManager.stop()
        # 本线程跑的是「阻塞式生成器」循环而不是事件循环，QThread.quit() 对它无效。
        # ThreadManager.stop() 会先 quit() 再 wait()，不覆盖的话 wait() 必然超时，
        # 进而走到 terminate() 强杀线程（可能把 sqlite 连接留在半开状态）。
        self._stop_event.set()

    def run(self) -> None:  # noqa: D102 - QThread 入口
        # 允许被 ThreadManager 停掉之后再次 run()：不清掉旧的停止标志，重启后会
        # 立刻退出，表现为「启用通知」失效。
        self._stop_event.clear()
        stop_event = self._stop_event
        try:
            from ...catch_notify import (
                CatchNotifyError,
                master_switch,
                watch_notifications,
            )
        except ImportError as exc:
            self.failed.emit("通知库 features.catch_notify 不可用：%s" % exc)
            return

        # 总开关关掉时通知根本不会进通知库，提前说清楚原因，避免用户以为是本程序坏了。
        switch = master_switch()
        if switch.get("enabled") is False:
            self.failed.emit(
                "系统通知总开关「获取来自应用和其他发送者的通知」已关闭，"
                "通知不会进入通知库，因此收不到任何提示。"
            )
        elif switch.get("enabled") is None:
            logger.debug("无法读取通知总开关：%s", switch.get("error"))

        try:
            for item in watch_notifications(
                source="auto",
                kinds=("toast",),
                interval=self._interval,
                skip_existing=True,      # 只报启动之后的新通知，避免历史上的未读一次性涌进来
                stop_event=stop_event,
            ):
                if stop_event.is_set():
                    break
                self.notification_received.emit(item)
        except CatchNotifyError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 后台线程里的任何异常都要报出来
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))
        finally:
            logger.debug("通知监听线程已退出")


class ActionDispatcher:
    """把「点击按钮」变成一次真正的激活（可在测试里替换掉）。

    真正干活的实现在 :mod:`features.catch_notify.activation`，这里只做两件事：
    懒导入（不点击就不会加载 COM 那套代码）和统一吞掉异常。
    """

    def __init__(self, *, dismiss_after: bool = True, lookup=None):
        self.dismiss_after = bool(dismiss_after)
        self.lookup = lookup

    def __call__(self, item, action=None, inputs=None, target: str = "action"):
        from ...catch_notify.activation import activate
        return activate(item, action=action, inputs=inputs, target=target,
                        lookup=self.lookup, dismiss_after=self.dismiss_after)


class NotificationPresenter(QObject):
    """把「通知 → 小黑条外观」的策略集中在这里，方便单独测试。"""

    #: 激活完成（工作线程 → GUI 线程），携带 ActivationResult
    activationFinished = Signal(object)

    def __init__(self, window, badge: NotificationBadge, config: dict = None, log=None,
                 thread_manager=None, dispatcher=None, prompt=None, open_center=None):
        super().__init__(window)
        self.window = window
        self.badge = badge
        self.log = _LoggerAdapter(log or logger)
        self.unread = 0
        self.last_item = None
        self.last_content = _EMPTY_CONTENT
        self.last_title = ""
        self.last_body = ""
        self.last_extra = ()
        self.last_app = ""
        self.last_actions: tuple = ()
        self._auto_shown = False       # 小黑条是不是被这条通知自动弹出来的
        self._failed = False           # 监听线程已经报错退出，别自动重启
        self._feedback = ""            # 激活结果等临时提示
        self._activating = 0
        self._activation_item = None   # 正在激活的那条通知（用来判断期间有没有新通知）

        # 注入点：dispatcher 负责「把动作发回应用」，prompt 负责「收集输入框内容」，
        # open_center 负责「打开系统通知中心」（点 🔔 时用）
        self.dispatcher = dispatcher or ActionDispatcher()
        self.prompt = prompt
        self.open_center = open_center or _default_open_center

        # 统一线程管理器（可选）。传入时监听线程会注册进去，退出时随
        # ThreadManager.stop_all() 一起收尾，而不是各停各的。
        self.thread_manager = thread_manager
        self._watcher_thread_id = None

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._on_expire)

        self.feedback_timer = QTimer(self)
        self.feedback_timer.setSingleShot(True)
        self.feedback_timer.timeout.connect(self._clear_feedback)

        self.watcher = NotificationWatcher(self, interval=0.8)
        self.watcher.notification_received.connect(self.on_notification)
        self.watcher.failed.connect(self.on_failed)
        self.badge.clicked.connect(self.on_clicked)
        self.badge.actionTriggered.connect(self.on_action)
        self.badge.centerRequested.connect(self.on_center_requested)
        self.activationFinished.connect(self.on_activation_finished)

        self.apply_config(config or {}, start=False)

    # -- 配置 -------------------------------------------------------------- #
    def apply_config(self, config: dict, *, start: bool = True) -> None:
        """应用 ``xht`` 配置段（模式 / 时长 / 开关）并同步界面。"""
        self.enabled = bool(config.get("notify_enabled", True))
        mode = str(config.get("notify_mode", DEFAULT_MODE)).lower()
        self.mode = mode if mode in MODES else DEFAULT_MODE
        try:
            duration = int(config.get("notify_duration", DEFAULT_DURATION))
        except (TypeError, ValueError):
            duration = DEFAULT_DURATION
        self.duration = max(0, min(duration, MAX_DURATION))
        # 内容元素相关的显示开关
        self.show_actions = bool(config.get("notify_actions", True))
        self.show_images = bool(config.get("notify_images", True))
        self.click_activates = bool(config.get("notify_click_activates", False))

        if not self.enabled:
            self.stop()
            self.mark_read()
            return

        self._failed = False
        if start:
            self.start()
        self._render()

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> None:
        """启动监听线程。

        有 ThreadManager 时走它（统一登记、统一退出）；没有就退化为直接 start()，
        保证本模块单独使用时也能工作。
        """
        manager = self.thread_manager
        if manager is None:
            if not self.watcher.isRunning():
                self.watcher.start()
            return

        if self._watcher_thread_id is None:
            try:
                self._watcher_thread_id = manager.create(
                    name="xht_notification_watch",
                    start_when_create=True,
                    worker=self.watcher,
                )
                return
            except Exception as exc:  # noqa: BLE001 - 线程上限等情况下退化直启
                self.log.warning("注册通知监听线程失败，改为直接启动：%s", exc)
                self._watcher_thread_id = None
                if not self.watcher.isRunning():
                    self.watcher.start()
                return

        # 已经在管理器里登记过：重启走 run()，让它把状态从 STOPPED 翻回 RUNNING
        try:
            manager.run(self._watcher_thread_id)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("重启通知监听线程失败：%s", exc)

    def stop(self) -> None:
        self.timer.stop()
        self.feedback_timer.stop()
        manager = self.thread_manager
        if manager is not None and self._watcher_thread_id is not None:
            try:
                # 不在管理器里登记的状态就要走 stop()，否则管理器会一直以为它还活着
                manager.stop(self._watcher_thread_id, wait=True)
                return
            except Exception as exc:  # noqa: BLE001
                self.log.debug("通过线程管理器停止通知监听失败：%s", exc)
        self.watcher.stop()

    # -- 通知处理（GUI 线程） ---------------------------------------------- #
    def on_notification(self, item) -> None:
        """收到一条新通知。"""
        if not self.enabled:
            return

        self.unread += 1
        self.last_item = item
        content = getattr(item, "content", _EMPTY_CONTENT)
        self.last_content = content
        self.last_actions = tuple(content.button_actions)
        self.last_title = getattr(item, "title", "") or ""
        self.last_body = getattr(item, "body", "") or ""
        self.last_extra = tuple(getattr(item, "other_texts", ()) or ())
        self.last_app = getattr(item, "display_app", "") or ""
        self._feedback = ""
        self.log.info("收到通知：%s | %s | 模板=%s 按钮=%d 输入框=%d",
                      self.last_app or "?", self.last_title,
                      content.template or "-", len(content.actions), len(content.inputs))

        if self.mode == MODE_SILENT:
            # 完全静默：不改动小黑条外观，只在内部累计未读数。
            return

        if self.mode == MODE_EXPAND:
            self._show_content()
        else:
            self._show_badge()

        self._pop_out()
        self._restart_timer()
        self._resize()

    def on_failed(self, message: str) -> None:
        """监听线程报错：只记录，不弹窗打断用户。"""
        self._failed = True
        self.log.warning("通知监听不可用：%s", message)

    def on_clicked(self) -> None:
        """点击 🔔 图标 / 展开的内容。

        * 当前显示的是 **🔔**（badge 档位，或 expand 档位内容收起后退化成的 ``🔔N``）：
          打开**系统通知中心**，然后清零未读 —— 多条通知堆在小黑条里逐条点开容易乱，
          交给系统通知中心看更清楚。注意判定看的是「现在显示的是不是 🔔」，不是
          ``notify_mode``：同一个小图标在两种档位下都得是同一个行为。
        * 当前显示的是**展开的内容**：按需把它当作「打开这条通知」发回应用，再清零未读。
          （内容里那个 ``🔔N`` 是链接，走 :meth:`on_center_requested`，不会落到这里。）
        """
        if self._badge_showing():
            self._open_notification_center(source="点击 🔔")
            self.mark_read()
            return

        item = self.last_item
        if self.click_activates and item is not None:
            self._start_activation(item, action=None, inputs=None, target="body")
        self.mark_read()

    def on_center_requested(self) -> None:
        """点了展开内容里的 ``🔔N`` 链接：和点 🔔 图标同一件事。"""
        self._open_notification_center(source="点击 🔔N 链接")
        self.mark_read()

    def _badge_showing(self) -> bool:
        """小图标当前是不是「只有 🔔N」的形态。

        部件没提供这个能力时（测试替身等）退回按 ``notify_mode`` 判断。
        """
        checker = getattr(self.badge, "is_badge_only", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception as exc:  # noqa: BLE001 - 判定失败不该影响点击
                self.log.debug("查询小图标形态失败：%s", exc)
        return self.mode == MODE_BADGE

    def _open_notification_center(self, source: str = "") -> bool:
        """打开系统通知中心（best-effort），只记录结果不抛异常。"""
        ok = False
        try:
            ok = bool(self.open_center())
        except Exception as exc:  # noqa: BLE001 - 打不开通知中心也不能影响清零
            self.log.warning("打开通知中心失败：%s", exc)
        self.log.info("%s：%s", source or "打开通知中心",
                      "已打开系统通知中心" if ok else "系统通知中心打开失败")
        return ok

    def on_action(self, index: int) -> None:
        """点击了第 ``index`` 个按钮：收集输入 → 发回应用。"""
        if not self.enabled or not self.show_actions:
            return
        content = self.last_content
        try:
            action = self.last_actions[int(index)]
        except (IndexError, TypeError, ValueError):
            return
        item = self.last_item
        if item is None:
            return

        needed = content.action_inputs(action)
        inputs: dict = {}
        if needed:
            collected = self._collect_inputs(needed, action)
            if collected is None:
                self.log.debug("用户取消了对「%s」的输入", action.label)
                return
            inputs = collected
        self._start_activation(item, action=action, inputs=inputs, target="action")

    def on_activation_finished(self, result) -> None:
        """工作线程把激活结果送回来了（GUI 线程）。"""
        self._activating = max(0, self._activating - 1)
        if result is None:
            return
        ok = bool(getattr(result, "ok", False))
        message = str(getattr(result, "message", "") or "")
        if ok:
            self.log.info("通知动作已完成：%s（%s）", message,
                          getattr(result, "method", ""))
        else:
            self.log.warning("通知动作失败：%s（%s）", message, getattr(result, "method", ""))
        # 先清零未读（它会清掉内容），再把结果显示出来 —— 顺序反了会被 clear() 抹掉。
        # 但如果激活期间又来了新通知，就不要动它的未读状态，只把结果显示出来。
        if self.last_item is self._activation_item:
            self.mark_read()
        self._show_feedback(("✓ " if ok else "⚠ ") + _clip(message, 40))

    # -- 未读 -------------------------------------------------------------- #
    def mark_read(self) -> None:
        """点击图标 / 内容时清零未读数。"""
        self.unread = 0
        self.last_title = ""
        self.last_body = ""
        self.last_extra = ()
        self.last_app = ""
        self.last_actions = ()
        self.last_item = None
        self.last_content = _EMPTY_CONTENT
        self.timer.stop()
        self._auto_shown = False
        self.badge.clear()
        self._set_time_visible(True)
        self._resize()

    # -- 激活 -------------------------------------------------------------- #
    def _collect_inputs(self, needed, action):
        """收集按钮需要的输入内容；用户取消返回 ``None``。"""
        if self.prompt is not None:
            try:
                return self.prompt(needed, action)
            except Exception as exc:  # noqa: BLE001 - 自定义 prompt 出错不该拖垮界面
                self.log.warning("收集输入内容失败：%s", exc)
                return None
        return _prompt_inputs(needed, action, self.window)

    def _start_activation(self, item, *, action, inputs, target: str) -> None:
        """在工作线程里执行激活（COM 调用可能等应用的后台进程）。"""
        self._activating += 1
        self._activation_item = item
        self.log.info("发送通知动作：%s → %s",
                      action.label if action is not None else "打开通知",
                      (action.arguments if action is not None else "launch"))

        def worker():
            try:
                result = self.dispatcher(item, action=action, inputs=inputs, target=target)
            except Exception as exc:  # noqa: BLE001 - 工作线程里任何异常都要报回去
                result = _FailedResult("%s: %s" % (type(exc).__name__, exc))
            try:
                self.activationFinished.emit(result)
            except RuntimeError:  # pragma: no cover - 退出过程中对象已销毁
                pass

        threading.Thread(target=worker, name="xht_notify_action", daemon=True).start()

    def _show_feedback(self, text: str) -> None:
        self._feedback = str(text or "")
        self.feedback_timer.stop()
        if self._feedback:
            self.feedback_timer.start(FEEDBACK_MS)
        self._render()

    def _clear_feedback(self) -> None:
        self._feedback = ""
        self._render()

    # -- 内部 -------------------------------------------------------------- #
    def _render(self) -> None:
        """按当前模式和未读数重画图标/内容。"""
        if not self.enabled or self.mode == MODE_SILENT:
            self.badge.clear()
            self._set_time_visible(True)
            self._resize()
            return

        if self.unread <= 0 and not self._feedback:
            self.badge.clear()
            self._set_time_visible(True)
            self._resize()
            return

        if self.mode == MODE_EXPAND:
            self._show_content()
        else:
            self._show_badge()
        self._resize()

    def _show_content(self) -> None:
        """展开通知内容；按需求同时把时间藏起来，让位给内容。"""
        content = self.last_content
        if self.last_item is None and not self._feedback:
            content = _EMPTY_CONTENT
        package_family = ""
        extra = getattr(self.last_item, "extra", None)
        if isinstance(extra, dict):
            package_family = str(extra.get("package_family_name") or "")
        self.badge.show_content(content, self.unread, self.last_app,
                                package_family=package_family, feedback=self._feedback,
                                show_actions=self.show_actions,
                                show_images=self.show_images)
        self._set_time_visible(False)

    def _show_badge(self) -> None:
        """只显示 🔔N；这时时间照常显示。"""
        if self.unread > 0:
            self.badge.show_badge(self.unread)
            self._set_time_visible(True)
            return
        # 没有未读时才可能只剩反馈文字：用展开样式把它显示出来
        self._show_content()

    def _set_time_visible(self, visible: bool) -> None:
        """窗口没有这个能力（例如测试替身）就安静跳过。"""
        setter = getattr(self.window, "set_time_visible", None)
        if not callable(setter):
            return
        try:
            setter(visible)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("切换时间显示失败：%s", exc)

    def _pop_out(self) -> None:
        """把收起来的小黑条滑出来（silent 档不会走到这里）。

        判断「需不需要弹出」用的是窗口的 :attr:`is_popping_hidden`：它不仅看
        ``is_hidden``，还算上「隐藏动画正在播放」的情形 —— 否则那段 250ms 里来的
        通知会被隐藏动画一起带走，用户根本看不到（表现为「有时候通知不显示」）。
        """
        window = self.window
        if not callable(getattr(window, "ShowWindow", None)):
            return

        # 窗口给的是 is_popping_hidden 属性（也可能是测试替身的方法），两种都认
        state = getattr(window, "is_popping_hidden", None)
        if state is None:
            needs_pop = bool(getattr(window, "is_hidden", False))
        else:
            try:
                needs_pop = bool(state() if callable(state) else state)
            except Exception as exc:  # noqa: BLE001 - 替身窗口可能没这个能力
                self.log.debug("查询小黑条状态失败：%s", exc)
                needs_pop = bool(getattr(window, "is_hidden", False))

        if not needs_pop:
            return
        self._auto_shown = True
        try:
            window.ShowWindow()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("弹出小黑条失败：%s", exc)

    def _restart_timer(self) -> None:
        self.timer.stop()
        if self.duration > 0:
            self.timer.start(self.duration * 1000)

    def _on_expire(self) -> None:
        """显示时间到：收起内容，必要时把小黑条收回贴边。"""
        if self.unread > 0 and self.mode == MODE_EXPAND:
            # 内容收起，但未读还在，退化成 🔔N（时间也随之恢复显示）
            self._show_badge()
        elif self.mode == MODE_BADGE and self.unread <= 0:
            self.badge.clear()
            self._set_time_visible(True)

        if self._auto_shown:
            self._auto_shown = False
            try:
                self.window.HideWindow()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("收回小黑条失败：%s", exc)
        self._resize()

    def _resize(self) -> None:
        """内容变了要请窗口重新按内容算尺寸。"""
        try:
            self.window.AutoSetSize()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("重算窗口尺寸失败：%s", exc)


class _FailedResult:
    """``ActivationResult`` 的最小替身：工作线程里出错时的兜底。"""

    __slots__ = ("ok", "method", "message", "detail", "error", "hresult", "dismissed")

    def __init__(self, error: str):
        self.ok = False
        self.method = "error"
        self.message = error
        self.detail = ""
        self.error = error
        self.hresult = 0
        self.dismissed = False


def _prompt_inputs(needed, action, parent=None):
    """弹出输入框收集按钮需要的输入内容（返回 ``{输入框 id: 内容}``）。

    用户取消返回 ``None``。文本输入用 ``QInputDialog.getText``，下拉输入用
    ``QInputDialog.getItem``；多个输入框会依次询问。
    """
    title = "回复 %s" % action.label if action is not None else "填写通知内容"
    values: dict = {}
    for item in needed:
        label = item.title or item.placeholder or item.id or "请输入"
        if item.is_selection and item.choices:
            labels = [choice.content or choice.id or "-" for choice in item.choices]
            current = 0
            for index, choice in enumerate(item.choices):
                if choice.id and choice.id == item.default_input:
                    current = index
                    break
            text, accepted = QInputDialog.getItem(parent, title, label, labels, current, False)
            if not accepted:
                return None
            chosen = item.choices[labels.index(text)] if text in labels else None
            values[item.id] = chosen.id if chosen is not None else text
        else:
            text, accepted = QInputDialog.getText(parent, title, label,
                                                  QLineEdit.EchoMode.Normal,
                                                  item.default_input or "")
            if not accepted:
                return None
            values[item.id] = text
    return values

r"""过滤 Qt 平台插件在 Windows 上刷出的「无害但吵」的显示器告警。

睡眠/唤醒、插拔显示器、坞站或 KVM 重连之后，Windows 会重新枚举显示设备。Qt 的
Windows 平台插件（qwindows.dll 里 ``qwindowsscreen.cpp`` 的
``setMonitorDataFromSetupApi()``）会拿系统给出的显示器设备路径去调用
``SetupDiOpenDeviceInterfaceW()``，目的是读 EDID、厂商/型号/序列号和 ICC 色彩
配置。这一步在「设备接口还没注册好」的那一瞬间必然失败，Qt 自己在源码里就注明
了 *"This can fail for virtual screens with no physical target - not an error"*，
随后退回用 ``\\.\DISPLAY1`` 当屏幕名继续跑：

    qt.qpa.screen: "Unable to open monitor interface to \\\\.\\DISPLAY1:"
                   "Unknown error 0xe0000225."

（``0xe0000225`` 是 SetupAPI 私有的 ``SPAPI_E_NO_SUCH_DEVICE_INTERFACE``，不在
标准 Win32 错误表里，所以 ``FormatMessage`` 查不到文本，只能打印成
"Unknown error 0x..."。）

这些消息只说明「友好名称 / EDID 没读到」：屏幕照常枚举，只是该 ``QScreen`` 的
``name()`` 退回成 ``\\.\DISPLAY1``、厂商型号为空、色彩空间退回 sRGB。本项目从不
依赖 ``QScreen.name()`` / 厂商 / 序列号（屏幕指标走 :mod:`core.sys32` 的 Win32
调用），因此这里把它们从控制台里丢掉即可。**其余 Qt 消息一律原样转发到 stderr，
不做任何吞并**，免得掩盖真正的报错。
"""

import sys

from PySide6.QtCore import qInstallMessageHandler

__all__ = ["install_qt_log_filter", "is_filtered_message"]

#: 只针对屏幕枚举这一个日志类别下手。
_FILTERED_CATEGORY = "qt.qpa.screen"

#: 需要丢掉的子串。都出自 ``setMonitorDataFromSetupApi()`` 的同一条失败路径，
#: 语义相同（读不到显示器设备接口，Qt 继续跑）。要保留其中某条就从这里删掉。
_FILTERED_SUBSTRINGS = (
    "Unable to open monitor interface",   # 用户实际看到的那条（0xe0000225 系列）
    "Unable to get monitor metadata",
    "Unable to get device information",
    "Unable to get EDID",
)

#: 上一个处理器。PySide6 只保存回调的裸指针，必须在 Python 侧留个引用，否则会被
#: GC 掉、之后所有 Qt 消息都进黑洞。
_previous_handler = None
_installed = False


def is_filtered_message(category, message):
    """判断一条 Qt 消息是否属于要丢弃的无害显示器告警。"""
    if (category or "") != _FILTERED_CATEGORY:
        return False
    text = message or ""
    return any(needle in text for needle in _FILTERED_SUBSTRINGS)


def _handler(mode, context, message):
    """替代 Qt 默认消息处理器：丢弃白名单消息，其余按原样写到 stderr。"""
    category = getattr(context, "category", "") or ""
    if is_filtered_message(category, message):
        return

    # 别人先装过处理器就别抢：转交给它，保持原有行为。
    if _previous_handler is not None:
        try:
            _previous_handler(mode, context, message)
            return
        except Exception:
            pass

    # 复刻 Qt 默认处理器的行为（stderr + 类别前缀）。消息本身可能自带换行，
    # 别再补一个。
    try:
        text = "%s: %s" % (category, message) if category else (message or "")
        if not text.endswith("\n"):
            text += "\n"
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:
        # 控制台不可用（无 stderr 的 GUI 进程）不该反过来影响程序。
        pass


def install_qt_log_filter():
    """安装过滤器，返回本次调用是否真的装上了（重复调用返回 False）。

    必须在 ``QApplication`` 创建之前调用：平台插件初始化时就会枚举屏幕并打出
    这些告警。消息处理器与具体 Qt 版本无关，所以比 ``QT_LOGGING_RULES`` 更可靠
    ——那套规则只能按类别/级别整片静音，且一旦这条消息是 warning 级别就得连别的
    告警一起关掉。
    """
    global _previous_handler, _installed
    if _installed:
        return False
    _previous_handler = qInstallMessageHandler(_handler)
    _installed = True
    return True

"""验证 Qt 显示器告警过滤器：只丢无害的屏幕枚举告警，其余消息照常输出。"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QLoggingCategory, qCWarning, qInstallMessageHandler  # noqa: E402

import core.qt_log_filter as qlf  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


class FakeContext:
    """模拟 QMessageLogContext（处理器只用 category 字段）。"""

    def __init__(self, category):
        self.category = category
        self.file = None
        self.line = 0
        self.function = None


# 用户实际看到的那条：注意 Qt 的默认处理器会把字符串按 qDebug 规则加引号并转义反斜杠
REPORTED = ('Unable to open monitor interface to \\\\.\\DISPLAY1:" '
            '"Unknown error 0xe0000225.')
# 睡眠后常见的另一种结尾（GetLastError 没被设上时 Qt 打印的就是它）
REPORTED_SUCCESS = 'Unable to open monitor interface to \\\\.\\DISPLAY1:" "The operation completed successfully.'

# ---------- 1) 白名单判定 ----------
check("丢弃用户报的那条",
      qlf.is_filtered_message("qt.qpa.screen", REPORTED) is True)
check("丢弃 \"The operation completed successfully.\" 那种结尾",
      qlf.is_filtered_message("qt.qpa.screen", REPORTED_SUCCESS) is True)
check("丢弃同一失败路径的兄弟消息（metadata / EDID / device information）",
      all(qlf.is_filtered_message("qt.qpa.screen", text) for text in (
          "Unable to get monitor metadata for \\\\.\\DISPLAY1:",
          "Unable to get device information for 1:",
          "Unable to get EDID from the Registry for \\\\.\\DISPLAY1:",
      )))
check("别的类别里同样文字不丢（避免误伤）",
      qlf.is_filtered_message("qt.qpa.window", REPORTED) is False)
check("qt.qpa.screen 里的其他告警不丢",
      qlf.is_filtered_message(
          "qt.qpa.screen",
          'QWindowsWindow::setGeometry: Unable to set geometry 1955x1028+0+23') is False)
check("空类别 / 空消息不丢",
      qlf.is_filtered_message(None, REPORTED) is False
      and qlf.is_filtered_message("qt.qpa.screen", None) is False)


# ---------- 2) 处理器行为：吞掉白名单，其余转发 stderr ----------
def run_handler(category, message):
    """直接调处理器，返回它写到 stderr 的内容。"""
    captured = io.StringIO()
    original = sys.stderr
    sys.stderr = captured
    try:
        qlf._handler(1, FakeContext(category), message)  # 1 == QtWarningMsg
    finally:
        sys.stderr = original
    return captured.getvalue()


check("处理器吞掉白名单消息", run_handler("qt.qpa.screen", REPORTED) == "")
check("处理器转发其他类别消息，并带类别前缀",
      run_handler("qt.qpa.window", "boom") == "qt.qpa.window: boom\n")
check("处理器转发 qt.qpa.screen 的非白名单消息",
      run_handler("qt.qpa.screen", "some other screen warning")
      == "qt.qpa.screen: some other screen warning\n")
check("无类别消息不加前缀",
      run_handler("", "bare message") == "bare message\n")
check("消息自带换行时不重复补换行",
      run_handler("", "already\n") == "already\n")


# ---------- 3) 安装 / 幂等 / 真实 Qt 消息走一遍 ----------
saved = qInstallMessageHandler(None)          # 先复位并记住别人的处理器
try:
    check("首次安装成功", qlf.install_qt_log_filter() is True)
    check("重复安装被忽略", qlf.install_qt_log_filter() is False)

    captured = io.StringIO()
    original = sys.stderr
    sys.stderr = captured
    try:
        qCWarning(QLoggingCategory("qt.qpa.screen"), REPORTED)
        qCWarning(QLoggingCategory("qt.qpa.window"), "真实的其它告警")
    finally:
        sys.stderr = original
    output = captured.getvalue()
    check("真实 qt.qpa.screen 白名单消息没进 stderr",
          "Unable to open monitor interface" not in output, output.strip())
    check("真实其它类别消息照样出现在 stderr",
          "真实的其它告警" in output, output.strip())
finally:
    qInstallMessageHandler(saved)

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)

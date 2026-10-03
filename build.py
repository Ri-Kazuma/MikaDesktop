"""
HOW TO USE
1.pip install -r requirements.txt
2.python build.py build
"""

import sys
import os
from pathlib import Path

from cx_Freeze import setup, Executable

# 项目根目录
ROOT_DIR = Path(__file__).parent

# 基础依赖包
PACKAGES = [
    "core",
    "core.thread_mgr",
    "core.make_app_icon",
    "features",
    # 通知抓取库（纯标准库实现；随 features 一起打包，这里显式列出以防被漏掉）
    "features.catch_notify",
]

# 第三方包（cx_freeze 会自动检测大部分，这里显式列出可能遗漏的）
INCLUDES = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PIL",
    "loguru",
    "psutil",
    "win32com",
    "win32com.shell",
    "win32con",
    "win32gui",
    "win32process",
    "win32timezone",
    "win32api",
    "win32print",
    "winreg",
    "requests",
    "charset_normalizer",
    "BlurWindow",
    "BlurWindow.blurWindow",
    "ctypes",
    "hashlib",
    "json",
    "datetime",
    "threading",
    "uuid",
    "subprocess",
    "gc",
    "io",
    "warnings",
    "dataclasses",
    "enum",
    "typing",
]

# 需要拷贝的资源文件
INCLUDE_FILES = []

# 可选：要把 WinRT/COM 数据源也打进包里时，取消下面注释（没有这个 exe 也能正常用
# 默认的数据库源，所以这里默认不打包）。exe 由 features/catch_notify 的
# native/build.ps1 编译产出。
# catch_notify_bridge = ROOT_DIR / "features" / "catch_notify" / "native" / "NotificationBridge.exe"
# if catch_notify_bridge.exists():
#     INCLUDE_FILES.append((
#         str(catch_notify_bridge),
#         os.path.join("features", "catch_notify", "native", "NotificationBridge.exe"),
#     ))

# res/ 目录下的资源文件
res_dir = ROOT_DIR / "res"
if res_dir.exists():
    for f in res_dir.iterdir():
        if f.is_file():
            INCLUDE_FILES.append((str(f), os.path.join("res", f.name)))

# app_model.png
app_model = ROOT_DIR / "core" / "make_app_icon" / "app_model.png"
if app_model.exists():
    INCLUDE_FILES.append((str(app_model), os.path.join("core", "make_app_icon", "app_model.png")))

# 排除不需要的模块
EXCLUDES = [
    "tkinter",
    "unittest",
    "email",
    "xml",
    "xmlrpc",
    "pdb",
    "distutils",
    "test",
    # chardet 是被 requests 的可选导入拖进来的无关包：它自带的 mypyc 编译扩展
    # （chardet/pipeline/orchestrator__mypyc.*.pyd）被冻结后一运行就会 0xc0000005
    # 崩溃。requests 实际用的是 charset_normalizer，排除掉 chardet 即恢复正常。
    "chardet",
]

build_exe_options = {
    "packages": PACKAGES,
    "includes": INCLUDES,
    "include_files": INCLUDE_FILES,
    "excludes": EXCLUDES,
    "optimize": 2,
    "build_exe": "dist/MikaDesktop",
}

# 基础可执行文件配置
base = None
if sys.platform == "win32":
    base = "gui"  # 无控制台窗口的 GUI 应用

executables = [
    Executable(
        script=str(ROOT_DIR / "dock.py"),
        base=base,
        target_name="MikaDesktop.exe",
        icon=str(ROOT_DIR / "core" / "make_app_icon" / "app_model.png"),
    ),
    # 任务栏看门狗：独立进程。主进程被任务管理器强杀时由它把系统任务栏恢复出来
    # （见 taskbar_watchdog.py 与 dock.py 的 _start_taskbar_watchdog）。
    Executable(
        script=str(ROOT_DIR / "taskbar_watchdog.py"),
        base=base,
        target_name="MikaWatchdog.exe",
    ),
]

setup(
    name="MikaDesktop",
    version="0.0.0",
    description="",
    options={"build_exe": build_exe_options},
    executables=executables,
)

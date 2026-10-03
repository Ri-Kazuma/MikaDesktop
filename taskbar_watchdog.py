"""任务栏看门狗：独立进程，在 dock 主进程消失后把系统任务栏恢复出来。

背景
----
dock 运行时会把 Windows 任务栏隐藏（见 ``core/sys32.py`` 的 ``hide_window`` 对
``Shell_TrayWnd`` 调用 ``ShowWindow(SW_HIDE)``）。正常退出时 dock 自己会恢复；但被
任务管理器「结束任务」强杀时，进程内的任何清理代码都来不及执行 —— 强杀用的是
``TerminateProcess``，无法被目标进程拦截，任务栏就会一直停在隐藏状态。

唯一的兜底办法是**由另一个进程从外部观察**：主进程一结束就立刻把任务栏显示回来。
本脚本就是那个进程，由 ``dock.py`` 启动时以独立进程拉起：

    MikaWatchdog.exe <dock进程PID>

它只做两件事：
1. 等待指定 PID 的进程结束；
2. 若此刻没有别的 dock 实例在运行，则 ``ShowWindow(Shell_TrayWnd, SW_SHOW)``。

刻意只依赖 ctypes、不导入 dock 的其它模块：看门狗要尽量小、启动快，不被主程序
的依赖（PySide6 等）拖累。
"""

import ctypes
import sys
import time
from ctypes import wintypes

SW_SHOW = 5
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF

# 与 dock.py 的单实例互斥体同名：用于判断「主进程结束后是不是已经有新实例接管」。
# 若有新实例，就不能显示任务栏，否则会把新实例刚隐藏的任务栏顶出来。
SINGLE_INSTANCE_MUTEX_NAME = "MikaDesktop_SingleInstance_Mutex"
ERROR_ALREADY_EXISTS = 183

# 主进程刚结束时，内核回收它持有的句柄可能还差一点点；等一小会儿再判断，
# 避免误判成「互斥体还在 = 还有实例在跑」。
SETTLE_SECONDS = 0.3

_kernel32 = ctypes.WinDLL("kernel32")
_user32 = ctypes.WinDLL("user32")

# 64 位下句柄不能按默认 c_int 返回，否则会被截断（core/sys32.py 里有同类说明）
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.GetLastError.restype = wintypes.DWORD

_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def _another_dock_running() -> bool:
    """此刻是否已有（新的）dock 实例在运行 —— 用单实例互斥体判断。"""
    handle = _kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    # GetLastError 必须在 CloseHandle 之前读，否则会被 CloseHandle 覆盖。
    last_error = _kernel32.GetLastError()
    if handle:
        _kernel32.CloseHandle(handle)
    return last_error == ERROR_ALREADY_EXISTS


def _show_taskbar() -> None:
    hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
    if hwnd:
        _user32.ShowWindow(hwnd, SW_SHOW)


def _wait_for_process(pid: int) -> None:
    handle = _kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        # 打不开说明进程早没了（或没权限），直接往下走去恢复任务栏
        return
    _kernel32.WaitForSingleObject(handle, INFINITE)
    _kernel32.CloseHandle(handle)


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip().isdigit():
        return
    parent_pid = int(sys.argv[1])
    _wait_for_process(parent_pid)
    time.sleep(SETTLE_SECONDS)
    if not _another_dock_running():
        _show_taskbar()


if __name__ == "__main__":
    main()

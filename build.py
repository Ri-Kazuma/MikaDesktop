"""
使用 PyInstaller 打包 MikaDesktop 项目。

用法:
    1. pip install -r requirements.txt pyinstaller
    2. python build.py build          # 发布版（主程序 + 看门狗 + 拷贝资源）
    3. python build.py build --fast   # 快速构建（复用 build/ 中间结果，迭代用）
    4. python build.py clean          # 清理构建产物

产物布局（与之前的 Nuitka 版本保持一致，程序里的路径逻辑不用改）：
    dist/MikaDesktop/
        MikaDesktop.exe        主程序（PySide6 GUI，无控制台，onedir）
        MikaWatchdog.exe       任务栏看门狗（独立小进程，onefile，无控制台）
        res/                   图标等运行时资源（exe 同级，按 dirname(sys.executable) 定位）
        _internal/             PyInstaller 收集的依赖 + 打进包内的数据文件
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ============================================================================ #
# 项目配置
# ============================================================================ #
ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
OUTPUT_DIR = DIST_DIR / "MikaDesktop"

# PyInstaller 的中间产物（workpath / specpath）统一收在 build/ 下，不污染项目根目录
BUILD_DIR = ROOT_DIR / "build"
SPEC_DIR = BUILD_DIR / "spec"

# 主程序（PySide6 GUI，无控制台）
MAIN_SCRIPT = ROOT_DIR / "app.py"
MAIN_NAME = "MikaDesktop"
MAIN_TARGET = MAIN_NAME + ".exe"

# 看门狗（独立轻量进程，无控制台，只依赖 ctypes）
WATCHDOG_SCRIPT = ROOT_DIR / "taskbar_watchdog.py"
WATCHDOG_NAME = "MikaWatchdog"
WATCHDOG_TARGET = WATCHDOG_NAME + ".exe"

# 应用图标。PyInstaller 在 Windows 上会用 Pillow 自动把 PNG 转成 ICO，
# 所以这里直接给 PNG 即可（本项目本来就依赖 Pillow）。
ICON_PATH = ROOT_DIR / "core" / "make_app_icon" / "app_model.png"

# ---------------------------------------------------------------------------- #
# 资源：构建完成后拷贝到 exe 同级目录
# ---------------------------------------------------------------------------- #
# 程序在冻结后用 ``os.path.dirname(sys.executable)`` 定位这些资源
# （见 app.py 的 script_dir / res_dir），所以必须放在 exe 旁边，而不是打进包内。
INCLUDE_RESOURCES: list[tuple[Path, str]] = [
    # (源路径, 输出目录内的相对路径)
    (ROOT_DIR / "res", "res"),
    (ROOT_DIR / "core" / "make_app_icon" / "app_model.png",
     os.path.join("core", "make_app_icon", "app_model.png")),
]

# ---------------------------------------------------------------------------- #
# 数据文件：打进包内（运行时位于 sys._MEIPASS，onedir 下就是 _internal/）
# ---------------------------------------------------------------------------- #
# 这些文件是通过模块内的 ``__file__`` 相对定位的，冻结后 __file__ 指向包内目录，
# 所以必须用 --add-data 放进去。
ADD_DATA: list[tuple[Path, str]] = [
    # core/make_app_icon/overlay.py: os.path.join(os.path.dirname(__file__), "app_model.png")
    (ROOT_DIR / "core" / "make_app_icon" / "app_model.png", "core/make_app_icon"),
    # features/catch_notify/bridge.py 会在 <包目录>/native/ 下找 NotificationBridge.exe
    (ROOT_DIR / "features" / "catch_notify" / "native" / "NotificationBridge.exe",
     "features/catch_notify/native"),
]

# ---------------------------------------------------------------------------- #
# 隐式导入（代码里写在函数内的 import，PyInstaller 静态分析看不到）
# ---------------------------------------------------------------------------- #
HIDDEN_IMPORTS: list[str] = [
    # core/pinned_apps.py、core/system_status.py、core/catch_ico.py 在函数内延迟导入
    "win32com",
    "win32com.client",
    "win32com.shell",
    "pythoncom",
    "pywintypes",
    # win32timezone 由 pywin32 的 C 扩展在运行期隐式导入（实测：
    # IShellLink.GetPath() 一调用就会去 import，见 core/pinned_apps.py）。
    # 这种 C 层的 import 静态分析看不到，漏了就会在解析任务栏快捷方式时
    # 报 "No module named 'win32timezone'"；win32timezone.py 在 win32/lib/ 下。
    "win32timezone",
]

# 需要整包收集子模块的第三方包。winsdk 的子模块是运行时动态解析的
# （winsdk.system._import_ns_module），静态分析跟不进去，必须显式收集。
# 注意：winsdk 自带 _winrt.pyd（约 48MB），不想要就把它从下面这行删掉。
COLLECT_SUBMODULES: list[str] = [
    "winsdk",
]

# ---------------------------------------------------------------------------- #
# 排除模块（减小体积）
# ---------------------------------------------------------------------------- #
# 注意：
#  1) 不要排除 email / xml —— urllib3 用了 email.utils/email.errors，
#     PIL.Image 用了 xml.etree.ElementTree，排除掉会在运行时 ImportError。
#  2) 不要排除 distutils / setuptools —— PyInstaller 自带
#     pre_safe_import_module/hook-distutils.py，会把 distutils 别名到
#     setuptools._distutils；把它们写进排除列表会让分析直接崩在
#     ValueError: Target module "distutils" already imported as "ExcludedModule(...)"。
EXCLUDE_MODULES: list[str] = [
    "tkinter",
    "unittest",
    "test",
    "pdb",
    "pydoc",
    "doctest",
    "lib2to3",
    "pip",
    # requests 优先用 charset_normalizer，chardet 的导入都包在 try/except 里
    "chardet",
    # 本项目没有用到这些重依赖，避免被间接带上
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
]

# 看门狗只依赖 ctypes，把主程序的重依赖全部排除掉，产物只有几百 KB
WATCHDOG_EXCLUDES: list[str] = EXCLUDE_MODULES + [
    "PySide6",
    "shiboken6",
    "PIL",
    "loguru",
    "psutil",
    "requests",
    "winsdk",
]

# ============================================================================ #
# PyInstaller 命令行
# ============================================================================ #
def configure_console() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 控制台默认是 GBK(cp936)，直接 print emoji（如 🔨）会抛
    UnicodeEncodeError 把构建打断；这里统一改成 UTF-8 + errors=replace。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover
            pass

def pyinstaller_version() -> str:
    try:
        from importlib.metadata import version
        return version("pyinstaller")
    except Exception:  # noqa: BLE001 - 只是打印用
        return "未知版本"

def ensure_pyinstaller() -> bool:
    if importlib.util.find_spec("PyInstaller") is None:
        print("❌ 未安装 PyInstaller。请先执行：")
        print("   %s -m pip install -r requirements.txt pyinstaller" % sys.executable)
        return False
    return True

def pyi_command(*, script: Path, name: str, onefile: bool, workpath: Path,
                icon: Path | None = None,
                excludes: list[str] | None = None,
                hidden: list[str] | None = None,
                collect_submodules: list[str] | None = None,
                add_data: list[tuple[Path, str]] | None = None,
                windowed: bool = True, clean: bool = False) -> list[str]:
    """拼一条 PyInstaller 命令（onedir/onefile 由 onefile 决定）。"""
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd += [
        "--distpath=%s" % DIST_DIR,
        "--workpath=%s" % workpath,
        "--specpath=%s" % SPEC_DIR,
        "--name=%s" % name,
        "--paths=%s" % ROOT_DIR,
        # UPX 压缩过的 PySide6 DLL 偶发崩溃，统一关掉（体积差异不大）
        "--noupx",
        "--log-level=INFO",
        "--onefile" if onefile else "--onedir",
    ]
    if windowed and sys.platform == "win32":
        cmd.append("--windowed")
    if icon is not None and icon.is_file():
        cmd.append("--icon=%s" % icon)
    for pkg in collect_submodules or ():
        cmd.append("--collect-submodules=%s" % pkg)
    for mod in hidden or ():
        cmd.append("--hidden-import=%s" % mod)
    for mod in excludes or ():
        cmd.append("--exclude-module=%s" % mod)
    for src, dst in add_data or ():
        if not src.exists():
            print("⚠️  数据文件不存在，跳过：", src)
            continue
        # 一律用绝对路径，避免 --specpath 改变相对路径的解析基准
        cmd.append("--add-data=%s%s%s" % (src, os.pathsep, dst))
    cmd.append(str(script))
    return cmd

def run_pyinstaller(cmd: list[str], label: str) -> int:
    print("🔨 %s：\n   %s" % (label, " ".join(cmd)))
    result = subprocess.run(cmd, cwd=str(ROOT_DIR))
    return result.returncode

def report_analysis_warnings(workpath: Path, name: str) -> None:
    """打印 PyInstaller 分析阶段的告警中「被本项目代码引用但没找到」的模块。

    PyInstaller 会把分析结果写到 ``warn-<name>.txt``；里面绝大部分是标准库的
    可选依赖（无害），但如果有本项目的模块引用了不存在的模块，通常就是打包漏了，
    这里把它们挑出来提示一下。
    """
    # PyInstaller 6 放在 <workpath>/<name>/ 下，老版本直接在 <workpath>/ 下
    warn_file = workpath / name / ("warn-%s.txt" % name)
    if not warn_file.is_file():
        warn_file = workpath / ("warn-%s.txt" % name)
    if not warn_file.is_file():
        return
    try:
        lines = warn_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    pattern = re.compile(r"imported by (?:app|core|features|taskbar_watchdog)\b")
    ours = [line.strip() for line in lines if "imported by" in line and pattern.search(line)]
    print("📋 分析告警：%s" % warn_file)
    if not ours:
        print("   （没有与本项目代码相关的缺失模块）")
        return
    print("   ⚠️  以下缺失模块被本项目代码引用，请确认是否真的需要：")
    for line in ours[:20]:
        print("      " + line)
    if len(ours) > 20:
        print("      …… 其余 %d 条见上面的文件" % (len(ours) - 20))

# ============================================================================ #
# 分目标构建
# ============================================================================ #
def remove_dir(path: Path, *, attempts: int = 3, delay: float = 1.0) -> bool:
    """删除目录，遇到 Windows 文件占用（WinError 32）时重试几次。

    打包好的程序还在运行时，dist/MikaDesktop 里的 exe 会被锁住；PyInstaller
    自己删不掉会直接抛 PermissionError，这里提前删干净并给出人话提示。
    返回是否删除成功（目录本来就不存在也算成功）。
    """
    for index in range(attempts):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if index < attempts - 1:
            time.sleep(delay)
    return not path.exists()

def find_onefile_exe(name: str) -> Path | None:
    """onefile 模式的产物路径在不同 PyInstaller 版本下不统一，两种都试。"""
    for candidate in (DIST_DIR / name / (name + ".exe"), DIST_DIR / (name + ".exe")):
        if candidate.is_file():
            return candidate
    return None

def build_main(*, output_dir: Path, fast: bool = False) -> int:
    """构建主程序 dist/MikaDesktop/MikaDesktop.exe（onedir 模式）。

    注意：onedir 的 COLLECT 步骤每次都会把 ``dist/MikaDesktop`` 整个删掉重建，
    所以资源拷贝与看门狗构建必须放在这一步之后。
    """
    workpath = BUILD_DIR / "main"
    code = run_pyinstaller(
        pyi_command(
            script=MAIN_SCRIPT,
            name=MAIN_NAME,
            onefile=False,
            workpath=workpath,
            icon=ICON_PATH,
            excludes=EXCLUDE_MODULES,
            hidden=HIDDEN_IMPORTS,
            collect_submodules=COLLECT_SUBMODULES,
            add_data=ADD_DATA,
            # --fast 时不加 --clean，PyInstaller 会复用 workpath 里上次的
            # Analysis/PYZ 结果（没改动的步骤直接跳过），迭代快很多。
            clean=not fast,
        ),
        "构建主程序",
    )
    report_analysis_warnings(workpath, MAIN_NAME)
    if code != 0:
        return code

    exe = output_dir / MAIN_TARGET
    if not exe.is_file():
        print("❌ 没找到主程序构建产物：", exe)
        return 2
    return 0

def build_watchdog(*, output_dir: Path, fast: bool = False) -> int:
    """构建看门狗 MikaWatchdog.exe，然后搬到主程序目录旁。

    看门狗刻意只依赖 ctypes，所以用 onefile 模式打包成单文件，避免占目录。
    不收集 PySide6，体积会非常小。
    """
    workpath = BUILD_DIR / "watchdog"
    code = run_pyinstaller(
        pyi_command(
            script=WATCHDOG_SCRIPT,
            name=WATCHDOG_NAME,
            onefile=True,
            workpath=workpath,
            icon=ICON_PATH,
            excludes=WATCHDOG_EXCLUDES,
            windowed=True,
            clean=not fast,
        ),
        "构建看门狗",
    )
    if code != 0:
        return code

    exe = find_onefile_exe(WATCHDOG_NAME)
    if exe is None:
        print("❌ 没找到看门狗构建产物。")
        return 2
    target = output_dir / WATCHDOG_TARGET
    shutil.move(str(exe), str(target))
    # onefile 也会在 dist/ 下留一个同名空目录，顺手清掉（保留 workpath 以加速增量）
    shutil.rmtree(DIST_DIR / WATCHDOG_NAME, ignore_errors=True)
    return 0

def copy_resources(output_dir: Path) -> None:
    """拷贝 INCLUDE_RESOURCES 里列出的资源文件/目录到 exe 同级目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for src, dst_rel in INCLUDE_RESOURCES:
        if not src.exists():
            print("⚠️  资源不存在，跳过：", src)
            continue
        dst = output_dir / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print("📁 拷贝目录：%s  →  %s" % (src, dst))
        else:
            shutil.copy2(src, dst)
            print("📄 拷贝文件：%s  →  %s" % (src, dst))

# ============================================================================ #
# 子命令
# ============================================================================ #
def cmd_build(args) -> int:
    fast = bool(args.fast)
    print("=" * 64)
    print("MikaDesktop PyInstaller 构建")
    print("  输出目录  :", OUTPUT_DIR)
    print("  PyInstaller:", pyinstaller_version())
    print("  模式      :", "快速（复用 build/ 中间结果）" if fast else "发布（完整重建）")
    print("=" * 64)

    if not ensure_pyinstaller():
        return 1

    # 先清掉旧产物：onedir 的 COLLECT 无论如何都会重建这个目录，提前删可以
    # 在「程序还在运行/被杀毒软件占用」时给出明确提示，而不是抛一坨 WinError 32。
    # --fast 只是复用 build/ 下的 PyInstaller 中间结果，与这一步无关。
    if OUTPUT_DIR.exists():
        print("🧹 清理旧产物:", OUTPUT_DIR)
    if not remove_dir(OUTPUT_DIR):
        print("❌ 删除旧产物失败：", OUTPUT_DIR)
        print("   通常是打包好的程序还在运行，或杀毒软件正在扫描该目录。")
        print("   请先关闭 %s / %s 后重试。" % (MAIN_TARGET, WATCHDOG_TARGET))
        return 3
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 主程序
    code = build_main(output_dir=OUTPUT_DIR, fast=fast)
    if code != 0:
        print("❌ 主程序构建失败，退出码", code)
        return code

    # 2) 资源文件（必须和 exe 同级，且要在主程序构建之后拷）
    copy_resources(OUTPUT_DIR)

    # 3) 看门狗
    code = build_watchdog(output_dir=OUTPUT_DIR, fast=fast)
    if code != 0:
        print("❌ 看门狗构建失败，退出码", code)
        return code

    print("\n✅ 构建完成！产物位于：", OUTPUT_DIR)
    print("   主程序  ：", OUTPUT_DIR / MAIN_TARGET)
    print("   看门狗  ：", OUTPUT_DIR / WATCHDOG_TARGET)
    return 0

def cmd_clean(_args) -> int:
    for path in (OUTPUT_DIR, DIST_DIR, BUILD_DIR):
        if path.exists():
            print("🧹 删除：", path)
            shutil.rmtree(path, ignore_errors=True)
    # 兼容早期版本的产物 / 手写的 spec
    for pattern in ("*.spec", "*.build", "*.dist", "*.onefile-build"):
        for p in ROOT_DIR.glob(pattern):
            if p.is_dir():
                print("🧹 删除：", p)
                shutil.rmtree(p, ignore_errors=True)
            else:
                print("🧹 删除：", p)
                p.unlink()
    print("✅ 清理完成")
    return 0

# ============================================================================ #
# 入口
# ============================================================================ #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PyInstaller 打包脚本：MikaDesktop 主程序 + MikaWatchdog 看门狗",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python build.py build\n  python build.py build --fast\n  python build.py clean",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="构建项目")
    p_build.add_argument("--fast", action="store_true",
                         help="快速构建：不加 --clean，复用 build/ 下的 PyInstaller 中间结果（开发验证用）")
    p_build.set_defaults(func=cmd_build)

    p_clean = sub.add_parser("clean", help="清理所有构建产物")
    p_clean.set_defaults(func=cmd_clean)

    return parser

def main(argv=None) -> int:
    configure_console()
    if sys.platform != "win32":
        print("⚠️  本项目是 Windows 桌面程序，建议在 Windows 上打包。继续尝试…")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())

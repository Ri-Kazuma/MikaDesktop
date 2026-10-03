# MikaDesktop

米卡桌面（？）

为 Windows 一体机设计的桌面。

## 功（催）能（命）列表

| 功能 | 状态 |
| ---- | ---- |
| 程序栏 | ✅正常 |
| 误触检测 | ❌滚蛋😡 |
| 全屏让位 | ✅支持 |
| 设置 | ✅正常 |
| 通知提示（按 Toast 内容元素显示，按钮可点） | ✅支持 |
| 右侧扩展窗口（网络 / 音量 / 电源 + 通知中心入口） | ✅支持 |

## 全屏程序让位

有**非系统程序**全屏显示（铺满整块显示器，不是最大化）时，dock 会自动隐藏，
并暂时注销底部的 AppBar 保留区，把整块屏幕让给全屏窗口；全屏结束或切到别的窗口
后，AppBar 与 dock 自动恢复。

* 系统组件（桌面、任务栏、开始菜单、锁屏、UAC 提示、设置、任务管理器…）不会触发让位。
* 判定与去抖逻辑见 `core/fullscreen_watch.py`，设计说明见 [docs/fullscreen_watch.md](docs/fullscreen_watch.md)。
* 开关与例外名单在设置界面 **Dock** 页 →「全屏程序」，或直接改 `settings.json`：

```json
"fullscreen": {
  "enabled": true,
  "poll_interval_ms": 400,
  "enter_confirm": 1,
  "exit_confirm": 2,
  "tolerance": 2,
  "except_processes": []
}
```

## 通知提示（小黑条按 Toast 内容元素显示）

小黑条（XHT）会抓取系统通知并按 **Windows Toast 的内容元素**排版显示：标题、多行
正文、归属文本（「来自 某某」）、提醒 / 闹钟 / 来电场景、进度条、应用图标与横幅图，
以及通知自带的按钮。

* 按钮是**真的能点**：点击后按 `AUMID → 注册表 CustomActivator → CLSID →
  CoCreateInstance → INotificationActivationCallback::Activate` 把 `arguments`
  与输入框内容发回发出通知的应用；打包应用（UWP/MSIX）退回
  `IApplicationActivationManager` 按 AUMID 拉起应用。
* 显示方式仍是 `xht.notify_mode` 的三档（静默 / 只显示 🔔 / 展开），展开时可用
  `notify_actions`（按钮）、`notify_images`（图片）、`notify_click_activates`
  （点击内容同时激活应用）三个开关微调，设置界面在 **XHT** 页。
* 点击 🔔（badge 模式）直接打开**系统通知中心**（多条通知堆在小黑条里逐条点开容易
  乱），点击展开的内容才是「标记已读 / 可选激活应用」。
* 内容元素解析在 `features/catch_notify/toast.py`，激活链路在
  `features/catch_notify/activation.py`，设计说明与限制见 [docs/toast_content.md](docs/toast_content.md)。

```bash
# 只读：看当前通知被解析成了什么内容元素、按钮「如果点下去会怎么做」
python tests/manual_toast_activate.py
# 真的把最新一条通知的第 0 个按钮发回应用（会先确认）
python tests/manual_toast_activate.py --activate 0 --input reply=好的
```

## 右侧扩展窗口（网络 / 音量 / 电源 / 通知中心）

dock 右边那块和它等高的卡片里放了一排状态按钮：

* **网络 / 音量 / 电源**：分别打开 **WLAN 网络列表** / **快速设置主界面** /
  **设置的「电池」页**（Win10 上没有快速设置面板，音量退到操作中心里的音量快速
  操作）；tooltip 显示网络名与介质、音量百分比、电量与充放电状态。
  **没有电池的机器上不显示电源按钮。**
* **通知中心**：点一下打开系统通知中心（小黑条上的 🔔 也是这个行为）。

取值都在 `core/system_status.py`（`GetSystemPowerStatus` / `INetworkListManager` +
`GetAdaptersAddresses` / `IAudioEndpointVolume`），由后台线程 `SystemStatusWorker`
每 2s 轮询；界面在 `core/extension_panel.py`，状态由 `res/` 里的彩色图标表达
（图标是手工用 `core/make_app_icon` 的模板合成的，和 dock 应用图标同一套做法）。
原生面板走 shell URI（`ms-availablenetworks:` → WLAN 列表、`ms-settings:batterysaver`
→ 电池页、`ms-actioncenter:` → 通知中心）；「快速设置主界面」没有 URI，而 `Win+A`
在本机注入不进去（合成按键被系统拦下），所以走"先开 WLAN 页 → UI Automation 点
「后退」回到主界面"。

设计说明与 Win10/Win11 差异见 [docs/extension_panel.md](docs/extension_panel.md)。

<br />

## 快速上手

1. 安装 Python（推荐 3.12 及以上）。
2. [下载本项目](https://codeload.github.com/KazumaRimatsu/MikaDesktop/zip/refs/heads/main)并解压。
3. 打开终端安装依赖：`pip install -r requirements.txt`
4. 在项目目录执行：`python dock.py`

## 项目结构

```
dock.py                  程序栏主入口（界面装配 + 事件分发）
core/
  dock_constants.py      程序栏尺寸 / 配色 / 样式表常量
  dock_extension.py      右侧扩展窗口（与 dock 一并布局、一并让位）
  extension_panel.py     扩展窗口里的状态面板（网络/音量/电源/通知中心）
  system_status.py       系统状态后端（网络 / 音量 / 电源 + 原生面板入口）
  make_app_icon/         图标模板合成（状态图标见 res/，由它合成）
  dock_tooltip.py        图标悬浮提示条
  pinned_apps.py         任务栏固定项（.lnk）解析
  process_manager.py     进程与窗口查询（带短 TTL 缓存）
  process_scan.py        后台进程扫描线程（不阻塞界面）
  fullscreen_watch.py    全屏程序监听（判定规则 + 去抖 + 后台线程）
  catch_ico.py           Windows 图标提取
  config_manager.py      配置读写（原子写入 + 递归深合并）
  sys32.py               Windows API（AppBar 工作区保留、显示器信息）
  text_utils.py          日志参数格式化（唯一实现）
  thread_mgr/            统一线程管理器
features/
  XHT/                   小黑条（时间 / 通知提示）
  process_mgr.py         内置进程管理器
  catch_notify/          抓取 Windows 通知与原始 XML
    toast.py             Toast 内容元素解析（文本角色 / 图片 / 按钮 / 输入框…）
    activation.py        按钮激活（注册表 CustomActivator → COM Activate → 降级）
    native/              WinRT 桥接程序（可选数据源，C#）
docs/
  fullscreen_watch.md    全屏让位的设计说明
  dock_extension.md      右侧扩展窗口的尺寸 / 布局 / 让位说明
  dock_layout.md         dock 布局与按钮间距（窗口宽度必须收敛到布局最小宽度）
  extension_panel.md     扩展窗口状态面板：取值、原生面板与 Win10/11 差异
  toast_content.md       通知内容元素显示与按钮激活的设计说明
  thread_manager.md      统一线程管理器的 API 与用法
res/                     程序栏图标 + 状态图标（用 make_app_icon 模板合成）
tests/                   回归测试（见下）
```

## 运行测试

`tests/` 下是可直接执行的独立脚本（不依赖 pytest）：

```bash
python tests/test_core_fixes.py     # 配置原子写 / 深合并、排除列表、GDI 句柄不泄漏
python tests/test_perf_fixes.py     # 进程信息缓存、图标缓存、后台扫描线程
python tests/test_thread_model.py   # 通知监听线程与线程管理器的统一启停
python tests/test_dock_modules.py   # 常量 / 提示条 / 固定项解析
python tests/test_dock_extension.py # 右侧扩展窗口：尺寸夹取 / 一并布局 / 窗口属性
python tests/test_dock_layout.py    # 布局收敛：内容增删后窗口宽度与分组间距（见 docs/dock_layout.md）
python tests/test_extension_panel.py  # 扩展窗口面板：图标/文案映射、电池显隐、点击行为
python tests/test_toast_content.py  # Toast 内容元素解析 / 按钮激活 / 小黑条渲染
python tests/test_fullscreen_watch.py  # 全屏判定规则 / 去抖 / 监听线程 / 设置界面
python -m features.catch_notify.selftest  # 通知库自检（内容元素 + 通知库端到端）
```

其中 GDI 用例会在 300 次图标提取前后对比进程的 GDI 句柄数量，用来防止
`core/catch_ico.py` 的句柄泄漏回归。

另外**手动验证**脚本会与真实系统交互（只有 `manual_fullscreen_probe.py` 只观察、
不改动系统状态）：

```bash
python tests/manual_fullscreen_probe.py [秒数]   # 观察真实桌面的全屏判定与理由
python tests/manual_appbar_cycle.py              # 真机 AppBar 注册→注销→重新注册
python tests/manual_dock_fullscreen_cycle.py     # 启动真实 dock 实例，走一遍让位/恢复
python tests/manual_extension_panel.py           # 显示状态面板，打开快速设置/通知中心，输出预览图
python tests/manual_toast_activate.py            # 通知内容元素体检（只读；加 --activate 才会真发）
python tests/manual_toast_render.py              # 离屏渲染一张示例通知，看排版
```

<details>
<summary>打包</summary>

打包需要额外安装 `PyInstaller`（不在 `requirements.txt` 里）：

```bash
pip install pyinstaller
python build.py build          # 发布版：主程序 + 看门狗 + 资源
python build.py build --fast   # 快速构建：复用 build/ 中间结果，迭代用
python build.py clean          # 清理 dist/ 与 build/
```

产物在 `dist/MikaDesktop/`：`MikaDesktop.exe`（主程序）、`MikaWatchdog.exe`（任务栏看门狗）、
`res/`（图标资源，与 exe 同级）、`_internal/`（依赖与内置数据文件）。

</details>


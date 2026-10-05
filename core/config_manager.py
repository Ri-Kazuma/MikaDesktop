import copy
import json
import os
import tempfile
from . import log_maker

log = log_maker.logger()

DEFAULT_CONFIG = {
  "dock":{
    "apps": [],
    "except_processes": [
      "shellexperiencehost.exe",
      "applicationframehost.exe",
      "startmenuexperiencehost.exe",
      "widgets.exe",
      "widgetservice.exe",
      "python.exe",
      "wetype_server.exe",
      "wetype_service.exe",
      "wetype_renderer.exe",
      "systemsettings.exe",
      "textinputhost.exe"
    ]
  },
  "nocmd_mode": False,
  "debug": False,
  "fullscreen":{
    # 有非系统程序全屏显示时：注销 AppBar（把工作区还给全屏窗口）并隐藏 dock；
    # 全屏解除后自动恢复。判定规则见 core/fullscreen_watch.py
    "enabled": True,
    "poll_interval_ms": 400,   # 检测间隔（毫秒）
    "enter_confirm": 1,        # 连续命中几轮才判定为"进入全屏"
    "exit_confirm": 2,         # 连续未命中几轮才判定为"退出全屏"（抗 Alt+Tab 抖动）
    "tolerance": 2,            # 覆盖显示器的判定容差（像素）
  },
  "xht":{
      "edge_height": 4,
      "horizontal_edge_margin": 4,
      "drag_threshold": 8,
      "windowpos": "R",
      # 通知提示：显示方式 silent=不展开小黑条 / badge=只显示🔔和未读数 / expand=展开显示内容
      "notify_enabled": True,
      "notify_mode": "badge",
      "notify_duration": 6,
      # Toast 内容元素：按钮（点击后把动作发回应用）、图片（应用图标 / 横幅图）、
      # 点击通知本体时是否顺带激活应用（默认关：点击在本项目里一直是「标记已读」）
      "notify_actions": True,
      "notify_images": True,
      "notify_click_activates": False,
      }
}

def check(file_path):
    """确保配置文件存在：不存在时创建目录并写入默认配置。

    Args:
        file_path: 配置文件的完整路径（如 .../settings.json）
    """
    if not file_path:
        return
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(copy.deepcopy(DEFAULT_CONFIG), f, indent=4)
            log.warning("配置文件不存在，已创建默认配置文件")
        except Exception as e:
            log.error(f"创建默认配置文件 {file_path} 失败: {e}")

def load_config(file_path):
    """加载配置文件"""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # 确保所有必要的键都存在；字典类型的子键也补一遍，这样以后新增设置项时
            # 老配置文件不用手改也能拿到默认值（且都是深拷贝，不会污染 DEFAULT_CONFIG）
            for key, default_value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = copy.deepcopy(default_value)
                elif isinstance(default_value, dict) and isinstance(config.get(key), dict):
                    for sub_key, sub_default in default_value.items():
                        config[key].setdefault(sub_key, copy.deepcopy(sub_default))
            
            return config
        else:
            log.warning(f"Dock配置文件 {file_path} 不存在，将使用默认配置")
            return copy.deepcopy(DEFAULT_CONFIG)
    except Exception as e:
        log.error(f"加载Dock配置文件 {file_path} 失败: {e}")
        return copy.deepcopy(DEFAULT_CONFIG)

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：``override`` 覆盖 ``base``，字典逐层合并而不是整体替换。

    浅层的 ``dict.update`` 会把默认值里 ``override`` 未提及的子键丢掉
    （例如只传了 ``dock.apps`` 时，默认的 ``dock.except_processes`` 就没了），
    所以这里按层递归。``base`` 会被就地修改并返回。
    """
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def save_config(file_path, config):
    """保存配置文件（原子写入）。

    先写同目录下的临时文件，``fsync`` 后用 ``os.replace`` 覆盖目标。这样即使
    写入过程中崩溃或断电，也只会留下一个临时文件，而不会把已有配置截断成
    半截 JSON 导致下次启动读失败。
    """
    if not file_path:
        log.error("保存Dock配置失败: 未提供文件路径")
        return False

    tmp_path = None
    try:
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 递归合并默认值以确保完整性
        merged_config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), config)

        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(file_path) + ".",
            suffix=".tmp",
            dir=parent or None,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged_config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, file_path)
        tmp_path = None

        log.info(f"Dock配置已成功保存到 {file_path}")
        return True
    except Exception as e:
        log.error(f"保存Dock配置文件 {file_path} 失败: {e}")
        return False
    finally:
        # 写失败时清掉临时文件，避免在配置目录里堆垃圾
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
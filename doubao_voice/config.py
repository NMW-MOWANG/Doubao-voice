"""配置读写，配置文件为程序目录下的 config.json。"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"

# name -> resource_id
RESOURCE_IDS = {
    "豆包 2.0 小时版（推荐）": "volc.seedasr.sauc.duration",
    "豆包 2.0 并发版": "volc.seedasr.sauc.concurrent",
    "豆包 1.0 小时版": "volc.bigasr.sauc.duration",
    "豆包 1.0 并发版": "volc.bigasr.sauc.concurrent",
}

ENDPOINTS = {
    "整句输出（推荐）": "nostream",
    "流式输出（边说边出字）": "stream",
}

LANGUAGES = {
    "普通话": "zh-CN",
    "自动识别": "",
    "粤语": "yue-CN",
    "英语": "en-US",
    "日语": "ja-JP",
}

OUTPUT_MODES = {
    "粘贴到光标处": "paste",
    "逐字键入": "type",
    "仅复制到剪贴板": "clipboard",
}

# 粘贴快捷键：终端的粘贴是 Ctrl+Shift+V，别的程序是 Ctrl+V
PASTE_SHORTCUTS = {
    "自动识别（终端用 Ctrl+Shift+V）": "auto",
    "总是 Ctrl+V": "ctrl+v",
    "总是 Ctrl+Shift+V": "ctrl+shift+v",
}

DEFAULTS = {
    # ---- 接口 ----
    "api_key": "",
    "resource_id": "volc.bigasr.sauc.duration",
    "endpoint": "stream",
    "language": "zh-CN",
    # ---- 触发按键（Linux 用 evdev 码：修饰键位:内核键码）----
    "hold_key": "0:97",      # KEY_RIGHTCTRL
    "toggle_key": "3:32",    # Ctrl+Alt+D
    "share_keys": True,
    # ---- 录音 ----
    "live_typing": True,
    "output_mode": "paste",
    "auto_stop": True,
    "silence_ms": 1200,
    "vad_threshold": 200,
    "max_seconds": 300,
    "show_overlay": True,
    "show_window": False,
    "always_on_top": True,
    "notify": False,
    "restore_clipboard": True,
    "paste_shift": False,
    "paste_shortcut": "auto",
    "release_modifiers": True,
    "live_interval_ms": 350,
    "device": -1,
    # ---- 识别结果处理（官方 request 参数）----
    "enable_punc": True,
    "enable_itn": True,
    "enable_ddc": False,
    "output_zh_variant": "",
    "result_type": "full",
    "enable_auto_lang": False,
    "enable_lid": False,
    "enable_nonstream": False,
    # ---- 附加信息 ----
    "show_utterances": False,
    "enable_speaker_info": False,
    "enable_emotion_detection": False,
    "enable_gender_detection": False,
    "enable_age_detection": False,
    # ---- 端点检测（0 = 不指定，用服务端默认）----
    "end_window_size": 0,
    "vad_segment_duration": 0,
    "force_to_speech_time": 0,
    # ---- 语境与过滤 ----
    "hotwords": "",
    "context_text": "",
    "sensitive_system": False,
    "sensitive_empty": "",
    "sensitive_signed": "",
    "enable_poi_fc": False,
    "enable_music_fc": False,
    # ---- 窗口 ----
    "window_pos": "",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS or k == "window_pos"})
        except (OSError, json.JSONDecodeError):
            pass

    env_key = os.environ.get("DOUBAO_API_KEY")
    if env_key and not cfg["api_key"]:
        cfg["api_key"] = env_key
    return cfg


def save(cfg: dict) -> None:
    data = {key: cfg.get(key, default) for key, default in DEFAULTS.items()}
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def save_window_pos(pos: str) -> None:
    """只更新窗口位置，其余字段以磁盘上的为准。

    退出时如果直接把内存里的配置整体写回，会把"运行期间别人改过的配置"覆盖掉——
    比如用 --detect-key 改完按键后重启，旧实例退出时又把旧按键写回去。
    """
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["window_pos"] = pos
    merged = {key: data.get(key, default) for key, default in DEFAULTS.items()}
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def resource_label(resource_id: str) -> str:
    for label, value in RESOURCE_IDS.items():
        if value == resource_id:
            return label
    return resource_id

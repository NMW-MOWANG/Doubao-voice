"""极简运行日志：把会话统计和卡顿写进 voice_input.log。

界面上的提示是短暂的，用户看到也说不清细节；所以关键指标（重连次数、断流时长、最慢发送、
采集端丢音频）都落一份到日志里，下次"卡住"可以直接翻日志定位，不用靠猜。
"""

from __future__ import annotations

import os
import threading
import time

from .config import APP_DIR

LOG_PATH = APP_DIR / "voice_input.log"
MAX_BYTES = 512 * 1024
_lock = threading.Lock()


def write(message: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')}  {message}\n"
    try:
        with _lock:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
                LOG_PATH.unlink()
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass  # 日志写不进去不该影响识别

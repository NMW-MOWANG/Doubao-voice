"""录音浮标（主进程这一侧）。

浮标本身不在这个进程里画，而是交给一个 X11 子进程（overlayd.py）。原因：
* Wayland 下任何新窗口都会抢焦点，焦点一跑，正在打字的窗口就收不到字了；
* X11 的 override-redirect 窗口不受窗口管理器管辖，想抢也抢不到，实测确认过。

所以主进程保持 Wayland（剪贴板、窗口都在 Wayland 这边才正常），浮标丢给
GDK_BACKEND=x11 的子进程，两边用标准输入上的 JSON 行通信。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_COLORS = {
    "recording": "#e8453c",
    "recognizing": "#f5a623",
    "done": "#34a853",
    "error": "#d93025",
}


class Overlay:
    """底部居中的小浮标：麦克风图标 + 音量环 + 一行提示文字。"""

    def __init__(self, enabled: bool = True):
        self.error: str | None = None
        self._enabled = bool(enabled) and bool(os.environ.get("DISPLAY"))
        self._process: subprocess.Popen | None = None
        self._state = ""
        self._text = ""
        self._visible = False
        self._last_level = -1.0
        self._last_sent = 0.0
        self._lock = threading.Lock()
        if enabled and not os.environ.get("DISPLAY"):
            self.error = "没有 X11 显示（DISPLAY 为空），浮标显示不了"

    # ---------- 对外 ----------

    @property
    def available(self) -> bool:
        return self._enabled

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self, state: str, text: str = "") -> None:
        # 状态和文字都没变就不要再通知子进程了：它收到一条就重绘一次整个半透明窗口
        if self._visible and state == self._state and text == self._text:
            return
        self._state = state
        self._text = text
        self._visible = True
        self._send({"cmd": "show", "state": state, "text": text})

    def set_level(self, level: float) -> None:
        if not self._visible:
            return
        level = max(0.0, min(1.0, float(level)))
        now = time.monotonic()
        # 音量表 12 帧/秒足够顺眼，再快只是让子进程白白重绘
        if now - self._last_sent < 0.08 or abs(level - self._last_level) < 0.03:
            return
        self._last_level = level
        self._send({"cmd": "level", "value": round(level, 3)})

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        if self._visible:
            self._send({"cmd": "text", "text": text})

    def tick(self) -> None:
        """识别中的脉动由浮标进程自己动，这边不用管（保留接口）。"""

    def hide(self) -> None:
        self._visible = False
        self._send({"cmd": "hide"})

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    # ---------- 子进程 ----------

    def _send(self, payload: dict) -> None:
        if not self._enabled:
            return
        process = self._ensure_process()
        if process is None or process.stdin is None:
            return
        self._last_sent = time.monotonic()
        try:
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError):
            self._enabled = False
            self.error = "浮标进程退出了，浮标已关闭"

    def _ensure_process(self) -> subprocess.Popen | None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            if self._process is not None:
                self._enabled = False
                self.error = "浮标进程起不来，浮标已关闭"
                return None
            env = dict(os.environ)
            env["GDK_BACKEND"] = "x11"
            env["PYTHONPATH"] = APP_DIR + os.pathsep + env.get("PYTHONPATH", "")
            try:
                self._process = subprocess.Popen(
                    [sys.executable, "-m", "doubao_voice.overlayd"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    cwd=APP_DIR,
                )
            except OSError as exc:
                self._enabled = False
                self.error = f"浮标进程启动失败：{exc}"
                return None
            return self._process

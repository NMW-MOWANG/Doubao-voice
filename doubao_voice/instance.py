"""单实例保护 + 进程间控制。

两个实例同时跑会装两个键盘钩子、各自触发一次录音，把同一段话打两遍。
所以第二次启动不做"报错退出"，而是礼貌地请前一个实例退出再接管——
这样"重新点一下启动"就等于"用新代码重启"。

顺带用同一个 UNIX 套接字当控制通道：`voice_input.py --settings / --quit / --status`
就是连上这个套接字发一行命令，不用第二个进程去碰键盘和麦克风。
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time

_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or ""  # logind 给的私有目录，天然只有自己能进
if _RUNTIME and os.path.isdir(_RUNTIME):
    SOCKET_PATH = os.path.join(_RUNTIME, "doubao-voice.sock")
else:
    SOCKET_PATH = f"/tmp/doubao-voice-{os.getuid()}.sock"

# 命令 → 界面事件（和托盘菜单共用一套事件名）
COMMAND_EVENTS = {
    "quit": ("tray", "quit"),
    "show": ("tray", "show"),
    "settings": ("tray", "settings"),
    "toggle": ("tray", "toggle"),
}


def send(command: str, timeout: float = 2.0) -> str | None:
    """给正在运行的实例发一条命令；没有实例在跑返回 None。"""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(SOCKET_PATH)
        client.sendall((command.strip() + "\n").encode("utf-8"))
        data = client.recv(4096)
    except OSError:
        return None
    finally:
        client.close()
    return data.decode("utf-8", "replace").strip()


class InstanceGuard:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self.error: str | None = None
        self.status_provider = None  # App 填进来，供 --status 用
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    # ---------- 生命周期 ----------

    def acquire(self, timeout: float = 6.0) -> bool:
        """拿到监听权返回 True；发现旧实例就先请它退出，等不到则返回 False。"""
        deadline = time.monotonic() + timeout
        while True:
            if not self._alive():
                if self._bind():
                    return True
                time.sleep(0.2)
                if time.monotonic() >= deadline:
                    return False
                continue
            _ask_quit(self.path)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _alive(self) -> bool:
        if not os.path.exists(self.path):
            return False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.5)
        try:
            client.connect(self.path)
        except OSError:
            return False
        finally:
            try:
                client.close()
            except OSError:
                pass
        return True

    def _bind(self) -> bool:
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)  # 上一个进程崩了留下的死套接字
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(self.path)
            os.chmod(self.path, 0o600)
            server.listen(8)
        except OSError as exc:
            self.error = f"单实例锁建立失败：{exc}"
            return False
        self._server = server
        self._closed = False
        return True

    def release(self) -> None:
        self._closed = True
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass

    # ---------- 控制通道 ----------

    def watch(self, queue) -> None:
        """后台收命令：收到就当成托盘菜单点了对应项。"""
        if self._server is None:
            return
        self._thread = threading.Thread(
            target=self._serve, args=(queue,), daemon=True, name="instance"
        )
        self._thread.start()

    def _serve(self, queue) -> None:
        while not self._closed and self._server is not None:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                conn.settimeout(2.0)
                raw = conn.recv(256).decode("utf-8", "replace").strip().splitlines()
                command = raw[0].strip().lower() if raw else ""
                reply = self._dispatch(command, queue)
                conn.sendall((reply + "\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _dispatch(self, command: str, queue) -> str:
        if command == "status":
            if self.status_provider is None:
                return "未知"
            try:
                return str(self.status_provider())
            except Exception as exc:
                return f"取状态失败：{exc}"
        if command == "ping":
            return "ok"
        event = COMMAND_EVENTS.get(command)
        if event is None:
            return f"未知命令：{command}"
        queue.put(event)
        return "ok"

    def request(self, command: str, timeout: float = 2.0) -> str | None:
        """给自己发一条命令（走同一套通道，避免两套逻辑）。"""
        if self._server is None:
            return None
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(self.path)
            client.sendall((command + "\n").encode("utf-8"))
            return client.recv(4096).decode("utf-8", "replace").strip()
        except OSError:
            return None
        finally:
            client.close()


def _ask_quit(path: str) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(path)
        client.sendall(b"quit\n")
        client.recv(64)
    except OSError:
        pass
    finally:
        client.close()

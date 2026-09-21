"""常驻的 Wayland 剪贴板持有者（纯标准库）。

**为什么要自己写**：本机 GNOME Wayland 下 GTK 自带的剪贴板 API 对后台程序不生效
（实测 wl-paste 读不到 GTK 设进去的内容）；退而求其次用 wl-copy，每次都得 fork 一个
进程、再回读确认选区就位，实测**单次 109 毫秒**。实时上屏每秒要写两三次剪贴板，
这个开销直接变成"字出来得慢"。

这里改成在进程内开一条 Wayland 连接，自己实现 `wl_data_source` 持有选区：

* 更新内容 = 给一个变量赋值（微秒级，不产生任何进程/系统调用）；
* 真的有人粘贴时，合成器会发 `send(mime, fd)`，我们再把手上的当前内容写进那个 fd。

协议只用到 wl_display / wl_registry / wl_seat / wl_data_device_manager 这几个接口，
消息格式是固定的 8 字节头 + 参数，手写 marshalling 就够（不引任何第三方包）。
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time

MIME_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING")

# 对象编号：display 固定 1，其余自己分配
_ID_DISPLAY = 1
_ID_REGISTRY = 2
_ID_SYNC = 3
_ID_SEAT = 4
_ID_MANAGER = 5
_ID_DEVICE = 6

# wl_display
_DISPLAY_SYNC = 0
_DISPLAY_GET_REGISTRY = 1
# wl_registry
_REGISTRY_BIND = 0
_EVENT_REGISTRY_GLOBAL = 0
# wl_data_device_manager
_MANAGER_CREATE_DATA_SOURCE = 0
_MANAGER_GET_DATA_DEVICE = 1
# wl_data_source
_SOURCE_OFFER = 0
_SOURCE_DESTROY = 1
_EVENT_SOURCE_SEND = 1
_EVENT_SOURCE_CANCELLED = 2
# wl_data_device
_DEVICE_SET_SELECTION = 1

_RECV_SIZE = 8192


def _encode(object_id: int, opcode: int, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return struct.pack("<II", object_id, (size << 16) | opcode) + payload


def _uint(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def _string(value: str) -> bytes:
    raw = value.encode("utf-8") + b"\x00"
    return _uint(len(raw)) + raw + b"\x00" * (-len(raw) % 4)


def _align4(value: int) -> int:
    return value + (-value % 4)


def _parse_string(payload: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", payload, offset)
    offset += 4
    text = payload[offset : offset + max(0, length - 1)].decode("utf-8", "replace")
    return text, offset + _align4(length)


def _socket_path() -> str:
    name = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if name.startswith("/"):
        return name
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, name)


class WaylandClipboard(threading.Thread):
    """持有剪贴板选区，内容随时可换；没人粘贴时不产生任何开销。"""

    def __init__(self):
        super().__init__(daemon=True, name="wlclip")
        self.error: str | None = None
        self.available = False
        self._text = ""
        self._text_lock = threading.Lock()
        self._write_lock = threading.Lock()   # 串行化往连接里写协议消息
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._fds: list[int] = []
        self._source_id: int | None = None
        self._next_id = _ID_DEVICE + 1
        self._cancelled = True
        self._ready = threading.Event()
        self._stopping = False

    # ---------- 对外 ----------

    def start(self, timeout: float = 3.0) -> bool:
        """起线程并把选区挂上；成功返回 True。"""
        super().start()
        self._ready.wait(timeout)
        return self.available

    def set_text(self, text: str) -> bool:
        """更新剪贴板内容。选区被别的程序抢走过就顺手抢回来。"""
        if not self.available:
            return False
        with self._text_lock:
            self._text = text
            need_claim = self._cancelled
        if not need_claim:
            return True
        try:
            self._claim()
        except OSError as exc:
            self.error = f"重新占用剪贴板失败：{exc}"
            self.available = False
            return False
        # 选区刚交接，等一下再让调用方按 Ctrl+V（实测刚抢到时立刻粘贴会粘到空内容）
        time.sleep(0.02)
        return True

    def stop(self) -> None:
        self._stopping = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ---------- 线程 ----------

    def run(self) -> None:
        try:
            self._connect()
            self._bind_globals()
        except Exception as exc:
            self.error = f"连不上 Wayland 剪贴板：{exc}"
            self._ready.set()
            return
        self.available = True
        self._ready.set()
        try:
            self._claim()
        except OSError as exc:
            self.error = f"占用剪贴板失败：{exc}"
            self.available = False
            return
        self._loop()

    def _connect(self) -> None:
        path = _socket_path()
        if not os.path.exists(path):
            raise RuntimeError(f"没有 Wayland socket：{path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        sock.settimeout(0.5)
        self._sock = sock

    def _send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise OSError("Wayland 连接已关闭")
        with self._write_lock:
            sock.sendall(data)

    def _bind_globals(self) -> None:
        """要 wl_seat 和 wl_data_device_manager 两个全局对象。"""
        self._send(_encode(_ID_DISPLAY, _DISPLAY_GET_REGISTRY, _uint(_ID_REGISTRY)))
        self._send(_encode(_ID_DISPLAY, _DISPLAY_SYNC, _uint(_ID_SYNC)))

        seat: tuple[int, int] | None = None
        manager: tuple[int, int] | None = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and (seat is None or manager is None):
            for object_id, opcode, payload in self._read_messages():
                if object_id != _ID_REGISTRY or opcode != _EVENT_REGISTRY_GLOBAL:
                    continue
                (name,) = struct.unpack_from("<I", payload, 0)
                interface, offset = _parse_string(payload, 4)
                (version,) = struct.unpack_from("<I", payload, offset)
                if interface == "wl_seat":
                    seat = (name, version)
                elif interface == "wl_data_device_manager":
                    manager = (name, version)
        if seat is None or manager is None:
            raise RuntimeError("合成器没提供 wl_seat / wl_data_device_manager")

        self._send(_encode(_ID_REGISTRY, _REGISTRY_BIND, _uint(seat[0])
                           + _string("wl_seat") + _uint(min(seat[1], 1)) + _uint(_ID_SEAT)))
        self._send(_encode(_ID_REGISTRY, _REGISTRY_BIND, _uint(manager[0])
                           + _string("wl_data_device_manager")
                           + _uint(min(manager[1], 1)) + _uint(_ID_MANAGER)))
        self._send(_encode(_ID_MANAGER, _MANAGER_GET_DATA_DEVICE,
                           _uint(_ID_DEVICE) + _uint(_ID_SEAT)))

    def _claim(self) -> None:
        """新建一个 data_source 并抢下选区（旧 source 作废）。"""
        if self._source_id is not None:
            try:
                self._send(_encode(self._source_id, _SOURCE_DESTROY))
            except OSError:
                pass
            self._source_id = None
        self._next_id += 1
        source_id = self._next_id
        self._send(_encode(_ID_MANAGER, _MANAGER_CREATE_DATA_SOURCE, _uint(source_id)))
        for mime in MIME_TYPES:
            self._send(_encode(source_id, _SOURCE_OFFER, _string(mime)))
        # serial 给 0：剪贴板不需要输入事件的序列号（拖动才需要）
        self._send(_encode(_ID_DEVICE, _DEVICE_SET_SELECTION, _uint(source_id) + _uint(0)))
        self._source_id = source_id
        self._cancelled = False

    # ---------- 事件 ----------

    def _recv_fds(self) -> bytes:
        sock = self._sock
        if sock is None:
            raise OSError("Wayland 连接已关闭")
        data, ancillary, _flags, _addr = sock.recvmsg(
            _RECV_SIZE, socket.CMSG_SPACE(struct.calcsize("i") * 8)
        )
        for level, kind, content in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                count = len(content) // struct.calcsize("i")
                self._fds.extend(struct.unpack_from(f"{count}i", content))
        return data

    def _read_messages(self) -> list[tuple[int, int, bytes]]:
        """读一次并把完整消息切出来；不完整的留着下次拼。"""
        try:
            data = self._recv_fds()
        except socket.timeout:
            return []
        if not data:
            raise OSError("Wayland 连接被对端关闭")
        self._buffer += data
        messages = []
        while len(self._buffer) >= 8:
            object_id, word = struct.unpack_from("<II", self._buffer, 0)
            size = word >> 16
            opcode = word & 0xFFFF
            if size < 8 or len(self._buffer) < size:
                break
            messages.append((object_id, opcode, self._buffer[8:size]))
            self._buffer = self._buffer[size:]
        return messages

    def _loop(self) -> None:
        while not self._stopping:
            try:
                messages = self._read_messages()
            except OSError as exc:
                if not self._stopping:
                    self.error = f"Wayland 连接断了：{exc}"
                    self.available = False
                return
            for object_id, opcode, payload in messages:
                if object_id != self._source_id:
                    continue  # 旧 source 的事件（包括它自己的 cancelled）忽略
                if opcode == _EVENT_SOURCE_SEND:
                    self._serve(payload)
                elif opcode == _EVENT_SOURCE_CANCELLED:
                    self._cancelled = True

    def _serve(self, payload: bytes) -> None:
        """合成器来取内容：把当前文本写进它给的 fd。"""
        _mime, _offset = _parse_string(payload, 0)
        fd = self._fds.pop(0) if self._fds else None
        if fd is None:
            return
        with self._text_lock:
            data = self._text.encode("utf-8")
        try:
            os.write(fd, data)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

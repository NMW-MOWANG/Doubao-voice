"""极简 WebSocket 客户端（RFC 6455），只用标准库，支持自定义握手头。"""

from __future__ import annotations

import base64
import os
import select
import socket
import ssl
import struct

MAX_FRAME = 32 * 1024 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class HandshakeError(WebSocketError):
    def __init__(self, status_line: str, headers: str = "", body: str = ""):
        super().__init__(status_line)
        self.status_line = status_line
        self.status_code = 0
        parts = status_line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            self.status_code = int(parts[1])
        self.headers = headers
        self.body = body


def _parse_url(url: str):
    if not url.startswith(("ws://", "wss://")):
        raise ValueError(f"不支持的 URL: {url}")
    secure = url.startswith("wss://")
    rest = url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    path = "/" + path
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        port = int(port)
    else:
        host, port = hostport, (443 if secure else 80)
    return secure, host, port, path


def _read_error_body(sock: socket.socket, headers: str, already: bytes) -> str:
    """服务端拒绝握手时，把响应体（通常是有用的错误说明）读出来。"""
    length = 0
    for line in headers.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                length = 0
    body = bytes(already)
    if length and len(body) < length:
        sock.settimeout(2.0)
        while len(body) < length:
            try:
                part = sock.recv(length - len(body))
            except (OSError, socket.timeout):
                break
            if not part:
                break
            body += part
    return body.decode("utf-8", "replace").strip()


# 收发用两套时间：
# * 发送/读到一半的容忍度要**足够长**——对端 RTT 200ms 时，一次 TCP 重传就要 0.6~1.4 秒，
#   之前用 0.5 秒会把"网络重传"误判成"连接已死"，于是重连、几秒空白（实测就是这个现象）。
# * 空闲检测用短的 select，保证读线程及时唤醒，但空闲本身不算异常。
SEND_TIMEOUT = 5.0
IDLE_POLL = 0.4


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, connect_timeout: float = 8.0):
        self.url = url
        self.headers = dict(headers or {})
        self.connect_timeout = connect_timeout
        self.send_timeout = SEND_TIMEOUT
        self.idle_poll = IDLE_POLL
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._send_lock = None
        self._fragments = bytearray()
        self._fragment_opcode = OP_BINARY

    def connect(self) -> None:
        import threading

        secure, host, port, path = _parse_url(self.url)
        sock = socket.create_connection((host, port), timeout=self.connect_timeout)
        if secure:
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except Exception:
                sock.close()
                raise
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        sock.sendall(request)

        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            try:
                part = sock.recv(4096)
            except socket.timeout:
                sock.close()
                raise WebSocketError("握手超时")
            if not part:
                sock.close()
                raise WebSocketError("握手阶段连接被关闭")
            raw += part
            if len(raw) > 65536:
                sock.close()
                raise WebSocketError("握手响应异常")

        head, _, rest = bytes(raw).partition(b"\r\n\r\n")
        text = head.decode("utf-8", "replace")
        status_line = text.split("\r\n", 1)[0].strip()
        if " 101 " not in status_line + " ":
            body = _read_error_body(sock, text, rest)
            sock.close()
            raise HandshakeError(status_line, text, body)

        self._sock = sock
        self._buf = bytearray(rest)
        self._send_lock = threading.Lock()
        sock.settimeout(self.send_timeout)

    def _has_data(self, timeout: float) -> bool:
        """缓冲区里有数据，或者 socket 上可读（select 等待，不消耗数据）。"""
        if self._buf:
            return True
        if self._sock is None:
            return False
        try:
            readable, _, _ = select.select([self._sock], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(readable)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            part = self._sock.recv(65536)
            if not part:
                raise WebSocketError("连接已关闭")
            self._buf += part
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out

    def recv_frame(self) -> tuple[int, bytes]:
        """读取一个完整消息，返回 (opcode, payload)。

        空闲（一直没数据）抛 socket.timeout，调用方当没事继续；
        读到一半超时说明这条连接已经不可信，抛 WebSocketError 让调用方重连——
        否则下次会把半个帧体当成帧头读，整个流就错位了。
        """
        if not self._has_data(self.idle_poll):
            raise socket.timeout("空闲")
        b0, b1 = self._read_exact(2)
        try:
            return self._recv_body(b0, b1)
        except socket.timeout as exc:
            raise WebSocketError("读到一半超时，连接状态不可信") from exc

    def _recv_body(self, b0: int, b1: int) -> tuple[int, bytes]:
        while True:
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            if length > MAX_FRAME:
                raise WebSocketError("数据帧过大")

            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            elif opcode == OP_CLOSE:
                return OP_CLOSE, payload
            elif opcode == OP_CONT:
                self._fragments += payload
                if b0 & 0x80:
                    data = self._fragments
                    self._fragments = bytearray()
                    return self._fragment_opcode, bytes(data)
            elif not b0 & 0x80:
                self._fragments = bytearray(payload)
                self._fragment_opcode = opcode
            else:
                return opcode, payload

            b0, b1 = self._read_exact(2)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def send(self, payload: bytes) -> None:
        self._send_frame(OP_BINARY, payload)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

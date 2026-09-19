"""豆包流式语音识别（SAUC）二进制帧协议。

帧结构（全部大端）：

    +------+------+------+------+----------+------------+---------+
    | 0x11 | type | 0x11 | 0x00 | seq/int32| size/uint32| payload |
    +------+------+------+------+----------+------------+---------+
     byte0  byte1  byte2  byte3

byte1 高 4 位为消息类型，低 4 位为标志位；payload 为 gzip 压缩的 JSON 或音频。
"""

from __future__ import annotations

import gzip
import json
import struct
from typing import Any

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 1  # 头长度，单位：4 字节

CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ERROR_RESPONSE = 0b1111

FLAG_POS_SEQUENCE = 0b0001
FLAG_NEG_WITH_SEQUENCE = 0b0011

SERIALIZATION_JSON = 0b0001
COMPRESSION_GZIP = 0b0001


def _header(message_type: int, flags: int) -> bytes:
    return bytes(
        [
            (PROTOCOL_VERSION << 4) | HEADER_SIZE,
            (message_type << 4) | flags,
            (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP,
            0x00,
        ]
    )


def build_full_request(payload: dict, seq: int = 1) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return (
        _header(CLIENT_FULL_REQUEST, FLAG_POS_SEQUENCE)
        + struct.pack(">iI", seq, len(body))
        + body
    )


def build_audio_request(pcm: bytes, seq: int, is_last: bool = False) -> bytes:
    if is_last:
        flags = FLAG_NEG_WITH_SEQUENCE
        seq = -seq
    else:
        flags = FLAG_POS_SEQUENCE
    body = gzip.compress(pcm)
    return (
        _header(CLIENT_AUDIO_ONLY_REQUEST, flags)
        + struct.pack(">iI", seq, len(body))
        + body
    )


def parse_response(msg: bytes) -> dict[str, Any]:
    if len(msg) < 4:
        raise ValueError("响应帧过短")

    header_size = msg[0] & 0x0F
    message_type = msg[1] >> 4
    flags = msg[1] & 0x0F
    compression = msg[2] & 0x0F
    payload = msg[header_size * 4 :]

    out: dict[str, Any] = {
        "message_type": message_type,
        "code": 0,
        "is_last_package": False,
        "payload_msg": None,
    }

    if flags & 0x01:
        out["sequence"] = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]
    if flags & 0x02:
        out["is_last_package"] = True
    if flags & 0x04:
        out["event"] = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]

    if message_type == SERVER_FULL_RESPONSE:
        payload = payload[4:]  # payload_size
    elif message_type == SERVER_ERROR_RESPONSE:
        if len(payload) >= 8:
            out["code"] = struct.unpack(">i", payload[:4])[0]
            payload = payload[8:]
        else:
            out["code"] = -1
            payload = b""

    if payload and compression == COMPRESSION_GZIP:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            return out

    if payload:
        try:
            out["payload_msg"] = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    return out


def result_text(response: dict) -> str:
    """从响应中取出累计识别文本。"""
    msg = response.get("payload_msg")
    if not isinstance(msg, dict):
        return ""
    result = msg.get("result")
    if isinstance(result, list):
        result = result[0] if result else None
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text
    return ""


def result_duration(response: dict) -> int:
    msg = response.get("payload_msg")
    if isinstance(msg, dict):
        info = msg.get("audio_info")
        if isinstance(info, dict):
            return int(info.get("duration") or 0)
    return 0


def response_error(response: dict) -> str:
    """非 0 状态码时返回可读的错误说明，正常时返回空串。"""
    msg = response.get("payload_msg")
    code = response.get("code") or 0
    detail = ""
    if isinstance(msg, dict):
        code = msg.get("code") or code
        for key in ("message", "msg", "error"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                detail = value
                break
    elif msg:
        detail = str(msg)

    if not code and not detail:
        return ""
    if response.get("message_type") == SERVER_ERROR_RESPONSE and not detail:
        detail = "服务端返回错误帧"
    return f"[{code}] {detail}" if code else detail

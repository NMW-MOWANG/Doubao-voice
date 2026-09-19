"""豆包语音识别会话：把音频流送进 wss 接口，拿回文本。"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

from . import protocol
from .wsclient import HandshakeError, WebSocketClient, WebSocketError

URL_NOSTREAM = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
URL_STREAM = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

SAMPLE_RATE = 16000

AUTH_HINT = (
    "鉴权失败：请检查 API Key 是否正确、是否已开通对应的语音识别服务，"
    "以及「资源 ID」是否与已购买的版本一致。"
)

NO_KEY_HINT = "尚未填写 API Key，请点「设置」填入。"


def _auth_message(exc: HandshakeError) -> str:
    detail = f"（服务端：{exc.body}）" if exc.body else ""
    if exc.status_code == 403:
        return (
            f"当前账号没有开通这个资源{detail}。"
            "2.0 与 1.0 的「资源 ID」不通用，请在「设置 > 资源 ID」里换成已开通的版本。"
        )
    if exc.status_code == 401:
        return f"API Key 无效或已被停用{detail}。"
    return f"服务端拒绝了连接：{exc.status_line}{detail}"


class AsrError(Exception):
    pass


class AuthError(AsrError):
    pass


CANCELLED = "__cancelled__"


def _words(text) -> list[str]:
    """把「词A,词B，词C」这种输入拆成列表，中英文逗号都认。"""
    raw = str(text or "").replace("，", ",").replace("、", ",")
    return [word.strip() for word in raw.split(",") if word.strip()]


class AsrClient:
    def __init__(
        self,
        cfg: dict,
        on_partial=None,
        audio_format: str = "pcm",
        sample_rate: int = SAMPLE_RATE,
        bits: int = 16,
        channels: int = 1,
    ):
        self.cfg = cfg
        self.on_partial = on_partial
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.bits = bits
        self.channels = channels
        self._send_lock = threading.Lock()
        self._ack = threading.Event()
        self._last = threading.Event()
        self._closed = threading.Event()
        self._error: str | None = None
        self._text = ""
        self._duration = 0

    # ---------- 对外接口 ----------

    def transcribe(self, source, cancel: threading.Event | None = None, segment_ms: int = 200) -> str:
        """source.read(timeout) -> bytes（暂无数据返回 b""，结束返回 None）。"""
        segment_bytes = max(
            320, self.sample_rate * (self.bits // 8) * self.channels * segment_ms // 1000
        )
        ws = WebSocketClient(self._url(), self._headers(), connect_timeout=8.0)
        try:
            ws.connect()
        except HandshakeError as exc:
            if exc.status_code in (401, 403):
                raise AuthError(_auth_message(exc)) from exc
            raise AsrError(_auth_message(exc)) from exc
        except (OSError, WebSocketError) as exc:
            raise AsrError(f"无法连接识别服务：{exc}") from exc

        reader = threading.Thread(target=self._read_loop, args=(ws,), daemon=True)
        reader.start()

        try:
            ws.send(protocol.build_full_request(self._payload()))
            if not self._ack.wait(10):
                raise AsrError(self._error or "等待服务端确认超时")
            if self._error:
                raise AsrError(self._error)

            seq = 2
            pending = None
            buf = bytearray()
            while True:
                if cancel is not None and cancel.is_set():
                    raise AsrError(CANCELLED)
                chunk = source.read(0.3)
                if chunk is None:
                    break
                if not chunk:
                    continue
                buf += chunk
                while len(buf) >= segment_bytes:
                    seg = bytes(buf[:segment_bytes])
                    del buf[:segment_bytes]
                    if pending is not None:
                        self._send(ws, protocol.build_audio_request(pending, seq))
                        seq += 1
                    pending = seg

            tail = bytes(buf)
            if pending is None and not tail:
                raise AsrError("没有采集到音频")
            if pending is not None:
                if tail:
                    self._send(ws, protocol.build_audio_request(pending, seq))
                    seq += 1
                else:
                    tail = pending
                    pending = None
            if tail:
                self._send(ws, protocol.build_audio_request(tail, seq, is_last=True))

            if not self._last.wait(30):
                raise AsrError(self._error or "识别超时，未收到最终结果")
            if self._error:
                raise AsrError(self._error)
            return self._text
        finally:
            self._closed.set()
            ws.close()
            reader.join(1.0)

    # ---------- 内部 ----------

    def _url(self) -> str:
        return URL_STREAM if self.cfg.get("endpoint") == "stream" else URL_NOSTREAM

    def _headers(self) -> dict:
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            raise AuthError(NO_KEY_HINT)
        return {
            "X-Api-Key": key,
            "X-Api-Resource-Id": self.cfg.get("resource_id") or "volc.seedasr.sauc.duration",
            "X-Api-Request-Id": str(uuid.uuid4()),
        }

    def _payload(self) -> dict:
        cfg = self.cfg
        request: dict = {
            "model_name": "bigmodel",
            "enable_itn": bool(cfg.get("enable_itn", True)),
            "enable_punc": bool(cfg.get("enable_punc", True)),
            "enable_ddc": bool(cfg.get("enable_ddc", False)),
            "result_type": cfg.get("result_type") or "full",
        }

        # 二遍识别只在双向流式接口上有效，nostream 接口显式关掉
        if cfg.get("endpoint") != "stream":
            request["enable_nonstream"] = False
        elif cfg.get("enable_nonstream"):
            request["enable_nonstream"] = True

        language = cfg.get("language")
        if language:
            request["language"] = language

        if cfg.get("enable_auto_lang"):
            request["enable_auto_lang"] = True
        if cfg.get("enable_lid"):
            request["enable_lid"] = True

        variant = cfg.get("output_zh_variant")
        if variant:
            request["output_zh_variant"] = variant

        # 附加信息：说话人 / 情绪 / 性别 / 年龄都要配合分句信息才有地方返回
        needs_utterances = False
        if cfg.get("enable_speaker_info"):
            request["enable_speaker_info"] = True
            needs_utterances = True
        for key in (
            "enable_emotion_detection",
            "enable_gender_detection",
            "enable_age_detection",
        ):
            if cfg.get(key):
                request[key] = True
                needs_utterances = True
        if needs_utterances or cfg.get("show_utterances"):
            request["show_utterances"] = True

        # 端点检测：只在用户改过默认值时才发，减少对服务端默认行为的干扰
        for key in ("end_window_size", "vad_segment_duration", "force_to_speech_time"):
            try:
                value = int(cfg.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                request[key] = value

        if cfg.get("enable_poi_fc"):
            request["enable_poi_fc"] = True
        if cfg.get("enable_music_fc"):
            request["enable_music_fc"] = True

        filter_empty = _words(cfg.get("sensitive_empty"))
        filter_signed = _words(cfg.get("sensitive_signed"))
        if cfg.get("sensitive_system") or filter_empty or filter_signed:
            request["sensitive_words_filter"] = json.dumps(
                {
                    "system_reserved_filter": bool(cfg.get("sensitive_system")),
                    "filter_with_empty": filter_empty,
                    "filter_with_signed": filter_signed,
                },
                ensure_ascii=False,
            )

        hotwords = [line.strip() for line in str(cfg.get("hotwords") or "").splitlines()]
        hotwords = [word for word in hotwords if word]
        context_text = str(cfg.get("context_text") or "").strip()
        if hotwords or context_text:
            context: dict = {}
            if hotwords:
                context["hotwords"] = [{"word": word} for word in hotwords]
            if context_text:
                context["context_type"] = "dialog_ctx"
                context["context_data"] = [{"speaker": "user", "text": context_text}]
            request["corpus"] = {"context": json.dumps(context, ensure_ascii=False)}

        return {
            "user": {"uid": "doubao-voice"},
            "audio": {
                "format": self.audio_format,
                "codec": "raw",
                "rate": self.sample_rate,
                "bits": self.bits,
                "channel": self.channels,
            },
            "request": request,
        }

    def _send(self, ws: WebSocketClient, data: bytes) -> None:
        with self._send_lock:
            ws.send(data)

    def _read_loop(self, ws: WebSocketClient) -> None:
        try:
            while not self._closed.is_set():
                try:
                    opcode, data = ws.recv_frame()
                except socket.timeout:
                    continue
                except (OSError, WebSocketError, AttributeError):
                    return
                if opcode == 0x8:
                    return
                if opcode not in (0x1, 0x2):
                    continue

                try:
                    response = protocol.parse_response(data)
                except ValueError:
                    continue

                if not self._ack.is_set():
                    self._ack.set()

                error = protocol.response_error(response)
                if error:
                    self._error = error
                    self._last.set()
                    return

                duration = protocol.result_duration(response)
                if duration:
                    self._duration = duration
                text = protocol.result_text(response)
                if text and text != self._text:
                    self._text = text
                    if self.on_partial:
                        try:
                            self.on_partial(text)
                        except Exception:
                            pass

                if response.get("is_last_package"):
                    self._last.set()
                    return
        except Exception as exc:  # 兜底，避免线程静默退出
            self._error = self._error or f"接收数据异常：{exc}"
            self._last.set()
        finally:
            self._ack.set()


class FileSource:
    """把整个音频文件的字节按接近实时的节奏切片，喂给识别接口。"""

    def __init__(self, data: bytes, segment_bytes: int, segment_ms: int = 200, realtime: bool = True):
        self.data = data
        self.segment_bytes = max(320, segment_bytes)
        self.delay = segment_ms / 1000.0 if realtime else 0.0
        self.offset = 0

    def read(self, timeout: float = 0.3) -> bytes | None:
        if self.offset >= len(self.data):
            return None
        chunk = self.data[self.offset : self.offset + self.segment_bytes]
        self.offset += len(chunk)
        if self.delay:
            time.sleep(self.delay * 0.9)
        return chunk

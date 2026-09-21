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

# 重连相关：网络抖一下不该让整段话丢掉
REPLAY_CAP_SECONDS = 30.0     # 攒着准备重放的音频上限（内存/流量兜底）
ACK_WAIT_SECONDS = 5.0
SLOW_SEND_SECONDS = 0.8      # 单次发送超过这个时间就提示（正常几毫秒）
STALL_SECONDS = 1.5          # 连着这么久没收到任何响应就提示
FORCE_RECONNECT_SECONDS = 5.0  # 断流这么久就主动换一条新连接（TCP 半死时能顶开）
FINAL_WAIT_SECONDS = 6.0
RETRY_BACKOFF = (0.5, 1.2, 2.5)   # 重连前的等待，次数也就是重试次数
_RETRYABLE = (OSError, socket.timeout, WebSocketError)

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
        on_notice=None,
        on_tick=None,
        on_log=None,
        audio_format: str = "pcm",
        sample_rate: int = SAMPLE_RATE,
        bits: int = 16,
        channels: int = 1,
    ):
        self.cfg = cfg
        self.on_partial = on_partial
        self.on_notice = on_notice
        self.on_tick = on_tick
        self.on_log = on_log
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
        # ---- 断线重连相关 ----
        self.warning: str | None = None       # 给界面提示用（例如"网络抖动，已自动重连"）
        self.reconnects = 0                   # 本次会话重连了几次
        self.all_audio = bytearray()          # 攒着用于重放的音频
        self._replay_cap = int(
            self.sample_rate * (self.bits // 8) * self.channels * REPLAY_CAP_SECONDS
        )
        self._audio_truncated = False         # 音频超出上限被截过（重放就补不全了）
        self._prefix_text = ""                # 截断重放时，重连前已经识别到的文字
        self._session_text = ""               # 当前这条连接自己的识别结果
        self._connection_error: BaseException | None = None
        self._last_response_at: float | None = None
        self._force_reconnect = False
        self._conn_sent = 0
        self.stalls = 0                        # 响应断流次数
        self.max_send_seconds = 0.0            # 单次发送最久用了多久
        self._last_slow_notice_at = 0.0
        self._generation = 0
        self._segment_bytes = 0
        self._reader: threading.Thread | None = None

    # ---------- 对外接口 ----------

    def transcribe(self, source, cancel: threading.Event | None = None, segment_ms: int = 200) -> str:
        """source.read(timeout) -> bytes（暂无数据返回 b""，结束返回 None）。

        网络抖一下不该让整段话丢掉，所以这里的做法是：
        * 采集到的音频全部攒在 all_audio 里（有上限），发送进度记在 conn.sent；
        * 任何一次发送失败（超时/断连）就重连，把攒下的音频整段重放，新连接从头转写；
        * 重放到一半发现音频被截断过，就把重连前识别到的文字当成前缀拼上（宁可接缝处
          有点重复，也不能丢）；
        * 实在救不回来但已经识别出文字时，返回这些文字并在 warning 里说明，不整段丢弃。
        """
        self._segment_bytes = max(
            320, self.sample_rate * (self.bits // 8) * self.channels * segment_ms // 1000
        )
        # 握手 + 等确认超过 1.2 秒就告诉用户「在连」，免得干等着不知道发生了什么
        watchdog = threading.Timer(1.2, self._warn_slow_start)
        watchdog.daemon = True
        watchdog.start()
        conn = self._open()
        watchdog.cancel()
        stall_watch = threading.Thread(target=self._watch_stalls, daemon=True)
        stall_watch.start()
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise AsrError(CANCELLED)
                if self._force_reconnect:
                    self._force_reconnect = False
                    self._notice("这条连接像是卡死了，正在换一条…")
                    self._reconnect(conn)
                chunk = source.read(0.3)
                if self.on_tick:
                    try:
                        self.on_tick()
                    except Exception:
                        pass
                if chunk is None:
                    break
                if chunk:
                    self._remember(chunk)
                    self._drain(conn)
                elif self._connection_error is not None:
                    # 空闲时发现连接已经死了：先重连补齐，别等到收尾才发现
                    self._reconnect(conn)

            if conn.sent == 0 and not self.all_audio:
                raise AsrError("没有采集到音频")
            self._push(conn, bytes(self.all_audio[conn.sent:]), is_last=True)

            if not self._last.wait(FINAL_WAIT_SECONDS):
                # 收尾卡住的两种情形：连接明确报错，或者服务端一直不回话（静默断流）
                stalled = (
                    self._last_response_at is not None
                    and time.monotonic() - self._last_response_at > STALL_SECONDS
                )
                if (self._connection_error is not None or stalled) and (
                    self.reconnects <= len(RETRY_BACKOFF)
                ):
                    self._notice("收尾阶段没等到结果，正在换一条连接重来…")
                    self._log(
                        f"收尾阶段断流（连接错误={self._connection_error!r}，"
                        f"静默={stalled}），重连重来"
                    )
                    self._reconnect(conn)
                    self._push(conn, bytes(self.all_audio[conn.sent:]), is_last=True)
                if not self._last.wait(FINAL_WAIT_SECONDS):
                    raise AsrError(self._error or "识别超时，未收到最终结果")
            if self._error:
                raise AsrError(self._error)
            return self._text
        except AsrError as exc:
            if self._text.strip() and str(exc) != CANCELLED:
                self.warning = f"{exc}；已保留识别到的部分"
                return self._text
            raise
        finally:
            self._closed.set()
            conn.ws.close()
            if self._reader is not None:
                self._reader.join(1.0)
            self._log(
                f"会话结束：重连 {self.reconnects} 次，断流 {self.stalls} 次，"
                f"最慢一次发送 {self.max_send_seconds:.2f}s，文字 {len(self._text)} 字"
            )

    def _watch_stalls(self) -> None:
        """连着 STALL_SECONDS 收不到服务端任何响应就提示一次；恢复时再说一句。"""
        announced = False
        while not self._closed.is_set():
            time.sleep(0.4)
            if self._last_response_at is None or self._closed.is_set():
                continue
            gap = time.monotonic() - self._last_response_at
            if gap > STALL_SECONDS and self._ack.is_set() and not announced:
                announced = True
                self.stalls += 1
                self._notice(f"网络卡住了 {gap:.0f} 秒，识别结果会晚一点出来…")
                self._log(f"响应断流 {gap:.1f}s（连接内已发 {self._sent_this_conn()} 字节）")
            if (gap > FORCE_RECONNECT_SECONDS and self._ack.is_set()
                    and not self._force_reconnect and self.reconnects <= len(RETRY_BACKOFF)):
                self._force_reconnect = True
                self._log(f"断流 {gap:.1f}s，主动换一条新连接")
            elif gap < STALL_SECONDS and announced:
                announced = False
                self._notice("已恢复，正在把刚才那段补齐…")

    def _sent_this_conn(self) -> int:
        return self._conn_sent

    def _warn_slow_start(self) -> None:
        if self._ack.is_set() or not self.on_notice:
            return
        try:
            self.on_notice("正在连接识别服务…（网络较慢）")
        except Exception:
            pass

    # ---------- 连接 ----------

    def _connect_ws(self):
        ws = WebSocketClient(self._url(), self._headers(), connect_timeout=8.0)
        try:
            ws.connect()
        except HandshakeError as exc:
            if exc.status_code in (401, 403):
                raise AuthError(_auth_message(exc)) from exc
            raise AsrError(_auth_message(exc)) from exc
        except (OSError, WebSocketError) as exc:
            raise AsrError(f"无法连接识别服务：{exc}") from exc
        return ws

    def _open(self):
        """建连 + 下发配置 + 等服务端确认；网络抖动时自己重试几次。"""
        last: BaseException | None = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            if attempt:
                time.sleep(RETRY_BACKOFF[attempt - 1])
            if attempt:
                self.reconnects += 1     # 初次连接的重试也算，日志里数字才实在
            conn = _Connection(self._connect_ws(), self._generation)
            self._ack.clear()
            self._connection_error = None
            try:
                # 必须先把接收线程跑起来再去等 ack——否则没人读 socket，ack 永远等不到
                self._reader = self._start_reader(conn)
                self._send(conn.ws, protocol.build_full_request(self._payload()))
                if not self._ack.wait(ACK_WAIT_SECONDS):
                    raise AsrError(self._error or "等待服务端确认超时")
                if self._error:
                    raise AsrError(self._error)
                return conn
            except AuthError:
                raise
            except (AsrError, *_RETRYABLE) as exc:
                last = exc
                try:
                    conn.ws.close()
                except Exception:
                    pass
        raise AsrError(f"无法建立识别连接：{last}")

    def _reconnect(self, conn) -> None:
        """断线重连：新开一条连接，把"已经发出去过"的音频重放一遍。"""
        self.reconnects += 1
        if self.reconnects == 1:
            self.warning = "网络抖动，已自动重连并补齐音频"
        if self.on_notice:
            try:
                self.on_notice(f"网络抖动，正在重连补齐音频（第 {self.reconnects} 次）…")
            except Exception:
                pass
        try:
            conn.ws.close()
        except Exception:
            pass
        # 被截断过就说明重放补不全前面的音频，那就把已经识别到的文字留作前缀
        self._prefix_text = "" if not self._audio_truncated else self._text
        self._session_text = ""
        self._text = self._prefix_text
        self._last.clear()
        self._generation += 1
        sent = conn.sent
        conn = self._replace_connection(conn)
        data = bytes(self.all_audio[:sent])
        offset = 0
        while offset < len(data):
            seg = data[offset : offset + self._segment_bytes]
            offset += len(seg)
            self._send(conn.ws, protocol.build_audio_request(seg, conn.seq))
            conn.seq += 1
        conn.sent = sent

    def _replace_connection(self, conn):
        ws = self._connect_ws()
        conn.ws = ws
        conn.generation = self._generation
        conn.seq = 2
        conn.sent = 0
        self._ack.clear()
        self._connection_error = None
        self._reader = self._start_reader(conn)
        try:
            self._send(ws, protocol.build_full_request(self._payload()))
        except _RETRYABLE as exc:
            raise AsrError(f"重连失败：{exc}") from exc
        if not self._ack.wait(ACK_WAIT_SECONDS):
            raise AsrError("重连后等待服务端确认超时")
        return conn

    def _start_reader(self, conn):
        reader = threading.Thread(
            target=self._read_loop, args=(conn.ws, conn.generation), daemon=True
        )
        reader.start()
        return reader

    # ---------- 音频缓冲与发送 ----------

    def _remember(self, chunk: bytes) -> None:
        """攒音频供重连重放；超过上限就丢最早的（并记下"补不全了"）。"""
        self.all_audio += chunk
        overflow = len(self.all_audio) - self._replay_cap
        if overflow > 0:
            del self.all_audio[:overflow]
            self._audio_truncated = True

    def _drain(self, conn) -> None:
        """把攒够一整段（segment）的音频发出去。"""
        while len(self.all_audio) - conn.sent >= self._segment_bytes:
            seg = bytes(self.all_audio[conn.sent : conn.sent + self._segment_bytes])
            self._push(conn, seg)

    def _push(self, conn, data: bytes, is_last: bool = False) -> None:
        """发一段音频；断了就重连（重放已发出的部分），再重发这一段。

        重放只补"失败前已经发出去的那部分"，未发的这一段留给下面重发，
        这样既不会把音频喂两遍，末包的标记（负序号）也不会丢。
        """
        for attempt in range(len(RETRY_BACKOFF) + 1):
            try:
                self._send(conn.ws, protocol.build_audio_request(data, conn.seq, is_last))
                conn.seq += 1
                conn.sent = min(len(self.all_audio), conn.sent + len(data))
                self._conn_sent = conn.sent
                return
            except AuthError:
                raise
            except (*_RETRYABLE, AsrError) as exc:
                if attempt >= len(RETRY_BACKOFF):
                    raise AsrError(f"网络不稳定，重连 {attempt} 次仍失败：{exc}") from exc
                time.sleep(RETRY_BACKOFF[attempt])
                try:
                    self._reconnect(conn)
                except AuthError:
                    raise
                except (AsrError, *_RETRYABLE):
                    continue  # 这次重连也没成，退避后再来

    # ---------- 请求内容 ----------

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

    # ---------- 接收 ----------

    def _send(self, ws: WebSocketClient, data: bytes) -> None:
        started = time.monotonic()
        with self._send_lock:
            ws.send(data)
        used = time.monotonic() - started
        if used > self.max_send_seconds:
            self.max_send_seconds = used
        if used > SLOW_SEND_SECONDS:
            # 提示限频（否则一秒刷好几条），日志照记不误
            now = time.monotonic()
            if now - self._last_slow_notice_at > 5.0:
                self._last_slow_notice_at = now
                self._notice(f"网络慢：这次发送等了 {used:.1f} 秒…")
            self._log(f"发送耗时 {used:.2f}s（音频 {len(data)} 字节）")

    def _notice(self, message: str) -> None:
        if self.on_notice:
            try:
                self.on_notice(message)
            except Exception:
                pass

    def _log(self, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass

    def _read_loop(self, ws: WebSocketClient, generation: int) -> None:
        try:
            while not self._closed.is_set() and generation == self._generation:
                try:
                    opcode, data = ws.recv_frame()
                except socket.timeout:
                    continue
                except (OSError, WebSocketError, AttributeError) as exc:
                    self._connection_error = exc
                    return
                if opcode == 0x8:
                    self._connection_error = WebSocketError("服务端关闭了连接")
                    return
                if opcode not in (0x1, 0x2):
                    continue

                try:
                    response = protocol.parse_response(data)
                except ValueError:
                    continue

                self._last_response_at = time.monotonic()
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
                text = protocol.result_text(response) or ""
                if text and text != self._session_text:
                    self._session_text = text
                    combined = self._prefix_text + text
                    if combined != self._text:
                        self._text = combined
                        if self.on_partial:
                            try:
                                self.on_partial(combined)
                            except Exception:
                                pass

                if response.get("is_last_package"):
                    self._last.set()
                    return
        except Exception as exc:  # 兜底，避免线程静默退出
            self._error = self._error or f"接收数据异常：{exc}"
            self._connection_error = exc
            self._last.set()
        finally:
            self._ack.set()


class _Connection:
    """一条连接 + 这条连接上的发送进度。"""

    def __init__(self, ws: WebSocketClient, generation: int):
        self.ws = ws
        self.generation = generation
        self.seq = 2          # 序号从 2 开始（1 是配置包）
        self.sent = 0         # all_audio 里已经发出去的字节数


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

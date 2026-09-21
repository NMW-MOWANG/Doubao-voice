"""麦克风采集（Linux / ALSA）。

Windows 版直接调 winmm，Linux 这边用 `arecord` 子进程读裸 PCM：不用装任何
Python 音频库，走的是系统自带的 ALSA（Ubuntu 上由 PipeWire 或 PulseAudio 兜底），
设备列表和默认设备也都跟系统设置一致。

采集线程把音频切成固定长度的小块塞进队列，同时做能量统计，用于音量显示和
静音自动停止（VAD）。
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import shutil
import subprocess
import threading
import time
from array import array

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16bit
CHANNELS = 1

# 流启动爆音的判定。实测特征：爆音是"削顶"的——满量程样本占比 54%~100%；
# 而正常说话/环境噪声一律是 0%（音量再大也不会大面积贴着 ±32768）。
# 所以判据用"满量程样本占比"，比看 RMS 准得多，也不会把用户刚开口的第一个字当爆音清掉。
GUARD_MS = 1200
CLIP_RATIO = 0.05
CLIP_LEVEL = 32000

_CARD_RE = re.compile(
    r"^card (\d+): .*?\[([^\]]+)\], device (\d+): .*?\[([^\]]+)\]", re.MULTILINE
)


def rms_of(chunk: bytes) -> float:
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * value
    return (total / len(samples)) ** 0.5


def _pipewire_available() -> bool:
    socket = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "pipewire-0"
    )
    return os.path.exists(socket) and bool(shutil.which("pw-record"))


def _pipewire_sources() -> list[tuple[str, str]]:
    """PipeWire 里的采集节点，返回 [(node.name, 显示名)]。

    本机（Ubuntu GNOME）的声卡由 PipeWire 独占，直接开 `plughw:0,0` 是"设备忙"，
    所以选设备要报服务器里的节点名，让 pw-record 去连。
    """
    dump = shutil.which("pw-dump")
    if not dump:
        return []
    try:
        done = subprocess.run([dump], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        data = json.loads(done.stdout.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    sources = []
    for item in data:
        props = ((item.get("info") or {}).get("props")) or {}
        if "Source" not in str(props.get("media.class", "")):
            continue
        name = props.get("node.name")
        if not name or str(props.get("media.class")) == "Video/Source":
            continue
        label = props.get("node.description") or props.get("node.nick") or name
        sources.append((str(name), str(label)))
    return sources


def _alsa_capture_devices() -> list[tuple[str, str]]:
    """从 `arecord -l` 里解析出硬件设备，作为没有 PipeWire 时的退路。"""
    arecord = shutil.which("arecord")
    if not arecord:
        return []
    try:
        done = subprocess.run(
            [arecord, "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = done.stdout.decode("utf-8", "replace")
    devices = []
    for card, card_name, device, device_name in _CARD_RE.findall(text):
        devices.append((f"{card_name} · {device_name}", f"plughw:{card},{device}"))
    return devices


def list_input_devices() -> list[str]:
    """返回可选设备名列表，索引 0 为系统默认设备（配置里存 -1）。"""
    names = ["系统默认设备"]
    names.extend(label for _target, label in _audio_targets())
    return names


def _audio_targets() -> list[tuple[str, str]]:
    """[(传给录音程序的目标, 显示名)]，按后端自动选。"""
    if _pipewire_available():
        sources = _pipewire_sources()
        if sources:
            return sources
    return _alsa_capture_devices()


def device_spec(device) -> tuple[str, str | None]:
    """把配置里的设备换算成 (后端, 目标)：后端是 "pipewire" 或 "alsa"。"""
    if isinstance(device, str) and device.strip():
        name = device.strip()
        if _pipewire_available() and not name.startswith(("plughw:", "hw:", "default")):
            return ("pipewire", name)
        return ("alsa", name)
    try:
        index = int(device)
    except (TypeError, ValueError):
        index = -1
    if index <= 0:
        return ("pipewire", None) if _pipewire_available() else ("alsa", "default")
    targets = _audio_targets()
    if index <= len(targets):
        target = targets[index - 1][0]
        return ("pipewire", target) if _pipewire_available() else ("alsa", target)
    return ("pipewire", None) if _pipewire_available() else ("alsa", "default")


def pcm_for_device(device) -> str:
    """给人看的设备描述，--list-devices 和自检里用。"""
    kind, target = device_spec(device)
    if kind == "pipewire":
        return f"pipewire:{target or '默认设备'}"
    return str(target)


class Recorder:
    """把麦克风音频写进队列；read() 返回 b"" 表示暂时没有新数据，None 表示录音结束。"""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        chunk_ms: int = 100,
        buffer_count: int = 6,
        device=-1,
        level_callback=None,
        on_auto_stop=None,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.buffer_count = buffer_count
        self.device = device
        self.level_callback = level_callback
        self.on_auto_stop = on_auto_stop

        # VAD 参数
        self.auto_stop = False
        self.silence_ms = 1200
        self.vad_threshold = 400
        self.max_seconds = 60
        self.min_speech_ms = 300

        self.level = 0.0
        self.total_ms = 0
        self.lost_ms = 0.0        # 采集端丢掉的音频估算（墙钟 vs 实际收到的时长）
        self.auto_stopped = False
        self.error: str | None = None
        self._first_chunk_at: float | None = None

        self._queue: queue.Queue[bytes] = queue.Queue()
        self._stop_flag = threading.Event()
        self._finished = threading.Event()
        self._opened = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None

        self._speech_ms = 0
        self._silence_ms = 0

    # ---------- 对外接口 ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._opened.wait(4.0):
            self._stop_flag.set()
            raise RuntimeError(self.error or "麦克风打开超时")

    def read(self, timeout: float = 0.3) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None if self._finished.is_set() else b""

    def stop(self) -> None:
        self._stop_flag.set()

    @property
    def running(self) -> bool:
        return not self._finished.is_set()

    def close(self, timeout: float = 3.0) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def elapsed(self) -> float:
        return self.total_ms / 1000.0

    def enough_audio(self, minimum_ms: int = 200) -> bool:
        return self.total_ms >= minimum_ms

    # ---------- 采集线程 ----------

    def _command(self, kind: str, target: str | None) -> list[str]:
        if kind == "pipewire":
            if not shutil.which("pw-record"):
                raise RuntimeError("找不到 pw-record，请安装 pipewire-bin")
            command = [
                shutil.which("pw-record"),
                "--rate", str(self.sample_rate), "--channels", str(CHANNELS),
                "--format", "s16", "--latency", f"{self.chunk_ms}ms",
            ]
            if target:
                command += ["--target", target]
            return command + ["-"]
        if not shutil.which("arecord"):
            raise RuntimeError("找不到 arecord，请安装 alsa-utils：sudo apt install alsa-utils")
        return [
            shutil.which("arecord"), "-q", "-D", target or "default",
            "-t", "raw", "-f", "S16_LE",
            "-r", str(self.sample_rate), "-c", str(CHANNELS),
            "--period-time", str(self.chunk_ms * 1000),
            "--buffer-time", str(max(self.chunk_ms, 200) * 1000),
        ]

    def _candidates(self) -> list[tuple[str, str | None]]:
        kind, target = device_spec(self.device)
        if kind == "alsa":
            return [("alsa", target), ("alsa", "default"), ("alsa", "pipewire")]
        # 默认设备或指定的 PipeWire 节点：失败再退回 ALSA 那边（本机 default 就是
        # PipeWire 的 ALSA 桥，所以这只是换个入口，不是换设备）
        return [("pipewire", target), ("alsa", "default"), ("alsa", "pipewire")]

    def _run(self) -> None:
        chunk_bytes = max(320, self.sample_rate * SAMPLE_WIDTH * self.chunk_ms // 1000)
        error = ""
        try:
            for kind, target in self._candidates():
                if self._stop_flag.is_set():
                    break
                try:
                    command = self._command(kind, target)
                except RuntimeError as exc:
                    error = str(exc)
                    break
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                self._process = process
                try:
                    if self._pump(process, chunk_bytes):
                        return  # 采到数据了，剩下的收尾交给 finally
                finally:
                    self._shutdown_process()
                error = self._read_error(process) or error
                if self._stop_flag.is_set():
                    break
            if self.total_ms == 0 and not self._stop_flag.is_set():
                self.error = f"无法打开麦克风：{error or '设备不可用'}"
        except Exception as exc:  # 采集线程不能把异常抛给主线程
            self.error = f"录音失败：{exc}"
        finally:
            self._opened.set()
            self._finished.set()

    def _read_error(self, process: subprocess.Popen) -> str:
        try:
            data = process.stderr.read() or b""
        except (OSError, ValueError):
            return ""
        text = data.decode("utf-8", "replace").strip()
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _pump(self, process: subprocess.Popen, chunk_bytes: int) -> bool:
        """读音频直到收到停止信号。返回 True 表示真的采到了数据。"""
        fd = process.stdout.fileno()
        buffer = b""
        got_audio = False
        deadline = time.monotonic() + 1.5  # 等第一块数据，等不到就当这个设备打不开
        stop_at: float | None = None
        grace = self.chunk_ms / 1000.0 + 0.05

        while True:
            if self._stop_flag.is_set():
                if stop_at is None:
                    # 松手那一刻收集端还有一块没读回来，多等一个周期把它收干净
                    stop_at = time.monotonic()
                elif time.monotonic() - stop_at >= grace:
                    return got_audio
            if process.poll() is not None and not buffer:
                return got_audio

            try:
                readable, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                return got_audio
            if readable:
                try:
                    data = os.read(fd, chunk_bytes * 4)
                except OSError:
                    return got_audio
                if not data:
                    return got_audio
                buffer += data
                if not got_audio:
                    got_audio = True
                    self._opened.set()  # 第一块数据到手就算开好了
                deadline = float("inf")

            while len(buffer) >= chunk_bytes:
                chunk, buffer = buffer[:chunk_bytes], buffer[chunk_bytes:]
                self._handle_chunk(chunk)
                if self._stop_flag.is_set():
                    break

            if not got_audio and time.monotonic() > deadline:
                return False

    def _shutdown_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _handle_chunk(self, data: bytes) -> None:
        strength = rms_of(data)
        if self._first_chunk_at is None:
            self._first_chunk_at = time.monotonic()
        else:
            # 墙钟走了多久 vs 实际拿到多少音频；差得多说明采集端缓冲溢出丢了数据
            self.lost_ms = max(
                0.0, (time.monotonic() - self._first_chunk_at) * 1000 - self.total_ms
            )
        # 采集流刚打开的一小段时间里，本机（PipeWire/ALSA）经常吐出满量程的爆音
        # （实测约半数打开会碰上，持续 0.3~0.9 秒，RMS 直接顶到 32768）。这是流启动
        # 的假信号，放进识别请求只会让开头多出一串噪声，所以在这一小段里把明显
        # 削顶的块换成等长静音——既清掉噪声，也不打乱音频时间轴。
        # 阈值取得很高（正常人声到不了），过了这段时间就不再管。
        if self.total_ms < GUARD_MS and clip_ratio(data) > CLIP_RATIO:
            self.total_ms += self.chunk_ms
            self._queue.put(b"\x00" * len(data))
            return

        self._queue.put(data)
        self.total_ms += self.chunk_ms

        self.level = min(1.0, strength / 8000.0)
        if self.level_callback:
            try:
                self.level_callback(strength)
            except Exception:
                pass

        if self.auto_stop:
            if strength >= self.vad_threshold:
                self._speech_ms += self.chunk_ms
                self._silence_ms = 0
            elif self._speech_ms >= self.min_speech_ms:
                self._silence_ms += self.chunk_ms
                if self._silence_ms >= self.silence_ms:
                    self._finish_early()
                    return

        if self.max_seconds and self.total_ms >= self.max_seconds * 1000:
            self._finish_early()

    def _finish_early(self) -> None:
        self.auto_stopped = True
        self._stop_flag.set()
        if self.on_auto_stop:
            try:
                self.on_auto_stop()
            except Exception:
                pass


def clip_ratio(chunk: bytes) -> float:
    """满量程（削顶）样本占比，用来分辨"流启动爆音"和"正常响亮的声音"。"""
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    clipped = 0
    for value in samples:
        if value >= CLIP_LEVEL or value <= -CLIP_LEVEL:
            clipped += 1
    return clipped / len(samples)


def measure_noise(device=-1, seconds: float = 1.2) -> float:
    """静音环境下测一段，返回平均 RMS，用于推荐 VAD 阈值。

    样本太少（比如正好撞上流启动爆音、被清成静音）就返回 0，让调用方别拿它当依据。
    """
    rec = Recorder(device=device, chunk_ms=100)
    rec.start()
    try:
        deadline = time.monotonic() + seconds
        values = []
        while time.monotonic() < deadline:
            chunk = rec.read(0.2)
            if chunk and any(chunk):  # 全零的块是爆音守卫填的，不算数
                values.append(rms_of(chunk))
    finally:
        rec.close()
    if len(values) < 4:
        return 0.0
    return sum(values) / len(values)

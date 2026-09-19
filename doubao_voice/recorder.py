"""麦克风采集：直接用 winmm（waveIn*）实现，不依赖任何第三方音频库。

采集线程把音频切成固定长度的小块塞进队列，同时做能量统计用于
音量显示和静音自动停止（VAD）。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from array import array
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0
MAXPNAMELEN = 32

winmm.waveInGetNumDevs.restype = wintypes.UINT

winmm.waveInGetDevCapsW.argtypes = [
    ctypes.c_size_t,
    ctypes.c_void_p,
    wintypes.UINT,
]
winmm.waveInGetDevCapsW.restype = wintypes.UINT

winmm.waveInOpen.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
]
winmm.waveInOpen.restype = wintypes.UINT
winmm.waveInClose.argtypes = [ctypes.c_void_p]
winmm.waveInClose.restype = wintypes.UINT
winmm.waveInPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
winmm.waveInPrepareHeader.restype = wintypes.UINT
winmm.waveInUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
winmm.waveInUnprepareHeader.restype = wintypes.UINT
winmm.waveInAddBuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
winmm.waveInAddBuffer.restype = wintypes.UINT
winmm.waveInStart.argtypes = [ctypes.c_void_p]
winmm.waveInStart.restype = wintypes.UINT
winmm.waveInStop.argtypes = [ctypes.c_void_p]
winmm.waveInStop.restype = wintypes.UINT
winmm.waveInReset.argtypes = [ctypes.c_void_p]
winmm.waveInReset.restype = wintypes.UINT

winmm.waveInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
winmm.waveInGetErrorTextW.restype = wintypes.UINT


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_void_p),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.c_void_p),
]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


def _error_text(code: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    if winmm.waveInGetErrorTextW(code, buf, 256) == MMSYSERR_NOERROR:
        return buf.value
    return f"winmm 错误码 {code}"


def list_input_devices() -> list[str]:
    """返回输入设备名称列表，索引 0 为系统默认设备。"""
    names = ["系统默认设备"]
    count = winmm.waveInGetNumDevs()
    for index in range(count):
        caps = WAVEINCAPSW()
        if winmm.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == MMSYSERR_NOERROR:
            names.append(caps.szPname or f"设备 {index + 1}")
        else:
            names.append(f"设备 {index + 1}")
    return names


def rms_of(chunk: bytes) -> float:
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * value
    return (total / len(samples)) ** 0.5


class Recorder:
    """把麦克风音频写进队列；read() 返回 b"" 表示暂时没有新数据，None 表示录音结束。"""

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_ms: int = 100,
        buffer_count: int = 6,
        device: int = -1,
        level_callback=None,
        on_auto_stop=None,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.buffer_count = buffer_count
        self.device = WAVE_MAPPER if device is None or device < 0 else device
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
        self.auto_stopped = False
        self.error: str | None = None

        self._queue: queue.Queue[bytes] = queue.Queue()
        self._stop_flag = threading.Event()
        self._finished = threading.Event()
        self._opened = threading.Event()
        self._thread: threading.Thread | None = None

        self._speech_ms = 0
        self._silence_ms = 0

    # ---------- 对外接口 ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._opened.wait(2.0):
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

    def close(self, timeout: float = 1.5) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def elapsed(self) -> float:
        return self.total_ms / 1000.0

    def enough_audio(self, minimum_ms: int = 200) -> bool:
        return self.total_ms >= minimum_ms

    # ---------- 采集线程 ----------

    def _run(self) -> None:
        handle = ctypes.c_void_p()
        fmt = WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=1,
            nSamplesPerSec=self.sample_rate,
            nAvgBytesPerSec=self.sample_rate * 2,
            nBlockAlign=2,
            wBitsPerSample=16,
            cbSize=0,
        )
        code = winmm.waveInOpen(
            ctypes.byref(handle), self.device, ctypes.byref(fmt), None, None, CALLBACK_NULL
        )
        if code != MMSYSERR_NOERROR:
            self.error = f"无法打开麦克风：{_error_text(code)}"
            self._opened.set()
            self._finished.set()
            return

        chunk_bytes = max(320, self.sample_rate * 2 * self.chunk_ms // 1000)
        buffers: list[ctypes.Array] = []
        headers: list[WAVEHDR] = []
        try:
            for _ in range(self.buffer_count):
                buf = ctypes.create_string_buffer(chunk_bytes)
                header = WAVEHDR(
                    lpData=ctypes.addressof(buf),
                    dwBufferLength=chunk_bytes,
                    dwBytesRecorded=0,
                    dwFlags=0,
                )
                if winmm.waveInPrepareHeader(
                    handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR)
                ) != MMSYSERR_NOERROR:
                    raise RuntimeError("waveInPrepareHeader 失败")
                buffers.append(buf)
                headers.append(header)

            for header in headers:
                if winmm.waveInAddBuffer(
                    handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR)
                ) != MMSYSERR_NOERROR:
                    raise RuntimeError("waveInAddBuffer 失败")

            code = winmm.waveInStart(handle)
            if code != MMSYSERR_NOERROR:
                raise RuntimeError(f"无法开始录音：{_error_text(code)}")
        except Exception as exc:
            self.error = str(exc)
            self._opened.set()
            for header in headers:
                winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
            winmm.waveInClose(handle)
            self._finished.set()
            return

        self._opened.set()
        stop_requested_at: float | None = None
        grace = self.chunk_ms / 1000.0 + 0.02
        try:
            while True:
                for index, header in enumerate(headers):
                    if not header.dwFlags & WHDR_DONE:
                        continue
                    size = int(header.dwBytesRecorded)
                    data = buffers[index].raw[:size] if size else b""
                    if winmm.waveInUnprepareHeader(
                        handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR)
                    ) != MMSYSERR_NOERROR:
                        continue
                    header.dwBytesRecorded = 0
                    header.dwFlags = 0
                    header.dwBufferLength = chunk_bytes
                    winmm.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
                    winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
                    if data:
                        self._handle_chunk(data)

                if self._stop_flag.is_set():
                    # 松手那一刻，驱动里还有一块缓冲没录满。立刻停会把这块丢掉
                    # （实测正好 100ms，一个字的长度），所以多等一个缓冲周期把它收回来。
                    if stop_requested_at is None:
                        stop_requested_at = time.monotonic()
                    elif time.monotonic() - stop_requested_at >= grace:
                        break
                time.sleep(0.008)
        finally:
            winmm.waveInStop(handle)
            winmm.waveInReset(handle)
            for header in headers:
                winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
            winmm.waveInClose(handle)
            self._finished.set()

    def _handle_chunk(self, data: bytes) -> None:
        self._queue.put(data)
        self.total_ms += self.chunk_ms

        strength = rms_of(data)
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
                    self.auto_stopped = True
                    self._stop_flag.set()
                    if self.on_auto_stop:
                        try:
                            self.on_auto_stop()
                        except Exception:
                            pass
                    return

        if self.max_seconds and self.total_ms >= self.max_seconds * 1000:
            self.auto_stopped = True
            self._stop_flag.set()
            if self.on_auto_stop:
                try:
                    self.on_auto_stop()
                except Exception:
                    pass


def measure_noise(device: int = -1, seconds: float = 1.2) -> float:
    """静音环境下测一段，返回平均 RMS，用于推荐 VAD 阈值。"""
    rec = Recorder(device=device, chunk_ms=100)
    rec.start()
    try:
        deadline = time.monotonic() + seconds
        values = []
        while time.monotonic() < deadline:
            chunk = rec.read(0.2)
            if chunk:
                values.append(rms_of(chunk))
    finally:
        rec.close()
    if not values:
        return 0.0
    return sum(values) / len(values)

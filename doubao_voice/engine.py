"""录音 → 识别 → 输出 的流程编排，与界面解耦，通过事件队列回报状态。"""

from __future__ import annotations

import threading
import time

from . import asr, output, winutil
from .recorder import Recorder

MIN_AUDIO_MS = 200
FOCUS_SETTLE = 0.08


class Session:
    """一次录音识别会话。事件队列中可能出现：

    ("state", phase)         状态变化：idle / recording / recognizing
    ("partial", text)        识别中的中间结果
    ("result", text, note)   识别成功，note 说明文本去向
    ("notice", message)      提醒（不算错误）
    ("error", message)       出错
    ("cancelled",)           用户取消
    ("empty",)               没有识别到内容
    """

    def __init__(self, cfg: dict, events):
        self.cfg = cfg
        self.events = events
        self.recorder: Recorder | None = None
        self.last_text = ""
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._target: int | None = None

    # ---------- 状态 ----------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.recorder is not None

    def phase(self) -> str:
        with self._lock:
            recorder = self.recorder
        if recorder is None:
            return "idle"
        return "recording" if recorder.running else "recognizing"

    def level(self) -> float:
        recorder = self.recorder
        return recorder.level if recorder else 0.0

    def elapsed(self) -> float:
        recorder = self.recorder
        return recorder.elapsed() if recorder else 0.0

    # ---------- 控制 ----------

    def start(self, target_window: int | None = None, hold: bool = False) -> None:
        """hold=True 表示"按住说话"：由松手来结束，因此不用静音自动停。"""
        with self._lock:
            if self.recorder is not None:
                return
            self._cancel.clear()
            self._target = target_window
            recorder = Recorder(device=int(self.cfg.get("device", -1)))
            recorder.auto_stop = bool(self.cfg.get("auto_stop", True)) and not hold
            recorder.silence_ms = int(self.cfg.get("silence_ms", 1200))
            recorder.vad_threshold = int(self.cfg.get("vad_threshold", 200))
            recorder.max_seconds = int(self.cfg.get("max_seconds", 300))
            try:
                recorder.start()
            except Exception as exc:
                self.events.put(("error", str(exc)))
                return
            self.recorder = recorder
            self._worker = threading.Thread(
                target=self._run, args=(recorder,), daemon=True, name="asr"
            )
            self._worker.start()
        self.events.put(("state", "recording"))

    def stop(self) -> None:
        with self._lock:
            recorder = self.recorder
        if recorder:
            recorder.stop()

    def cancel(self) -> None:
        self._cancel.set()
        self.stop()

    # ---------- 工作线程 ----------

    def _run(self, recorder: Recorder) -> None:
        typer: output.LiveTyper | None = None
        live = bool(self.cfg.get("live_typing"))
        # 实时上屏必须走流式接口，这里强制覆盖，避免配置里两个开关互相打架
        settings = dict(self.cfg)
        if live:
            settings["endpoint"] = "stream"
        try:
            if live:
                if self._target and winutil.focus_window(self._target):
                    time.sleep(FOCUS_SETTLE)
                    typer = output.LiveTyper(self._target)
                else:
                    live = False
                    self.events.put(("notice", "没能找到输入窗口，改为识别完成后一次性粘贴"))

            if self._cancel.is_set():
                return

            client = asr.AsrClient(
                settings,
                on_partial=lambda text: self._on_partial(text, typer),
            )
            text = client.transcribe(recorder, self._cancel)

            if self._cancel.is_set():
                self.events.put(("cancelled",))
            elif recorder.total_ms < MIN_AUDIO_MS:
                self.events.put(("error", "录音太短，没有内容可识别"))
            elif not text.strip():
                self.events.put(("empty",))
            else:
                self.last_text = text
                self.events.put(("result", text, self._finish(typer, text)))
        except asr.AsrError as exc:
            if str(exc) == asr.CANCELLED or self._cancel.is_set():
                self.events.put(("cancelled",))
            else:
                self.events.put(("error", str(exc)))
        except Exception as exc:  # 任何意外都不该让界面卡住
            self.events.put(("error", f"识别失败：{exc}"))
        finally:
            try:
                recorder.close()
            except Exception:
                pass
            with self._lock:
                self.recorder = None
                self._worker = None
            self.events.put(("state", "idle"))

    def _on_partial(self, text: str, typer: output.LiveTyper | None) -> None:
        self.events.put(("partial", text))
        if typer is not None:
            typer.update(text)

    def _finish(self, typer: output.LiveTyper | None, text: str) -> str:
        if typer is None:
            return self._deliver(text)

        typed = typer.finish(text)
        if typer.error or typer.lost_focus:
            try:
                winutil.set_clipboard_text(text)
            except Exception:
                pass
            reason = typer.error or "输入窗口中途失焦"
            return f"{reason}，完整文本已复制到剪贴板"

        self.last_text = typed or text
        return f"已边听边打上去（{len(typed)} 字）"

    def _deliver(self, text: str) -> str:
        mode = self.cfg.get("output_mode", "paste")
        try:
            if mode == "clipboard":
                winutil.set_clipboard_text(text)
                return "已复制到剪贴板"

            winutil.set_clipboard_text(text)

            if mode == "type":
                if winutil.focus_window(self._target):
                    time.sleep(FOCUS_SETTLE)
                    if winutil.type_text(text):
                        return "已键入文本"
                return "已复制到剪贴板（未能激活目标窗口，请手动 Ctrl+V）"

            focused = winutil.focus_window(self._target)
            time.sleep(FOCUS_SETTLE)
            if focused and winutil.send_ctrl_v():
                return "已粘贴到光标处"
            return "已复制到剪贴板（未能激活目标窗口，请手动 Ctrl+V）"
        except Exception as exc:
            return f"文本已识别，但输出失败：{exc}"

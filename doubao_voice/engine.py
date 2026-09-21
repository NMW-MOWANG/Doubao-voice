"""录音 → 识别 → 输出 的流程编排，与界面解耦，通过事件队列回报状态。"""

from __future__ import annotations

import threading
import time

from . import applog, asr, linuxutil, output, windowinfo
from .recorder import Recorder

MIN_AUDIO_MS = 200


def resolve_paste_shift(cfg: dict, refresh: bool = False) -> bool:
    """决定这次粘贴用 Ctrl+V 还是 Ctrl+Shift+V。

    优先级：配置里指定的 → 自动识别（问焦点窗口是不是终端）→ 识别不出来时用手动开关。
    """
    mode = str(cfg.get("paste_shortcut") or "auto").strip().lower()
    if mode in ("ctrl+shift+v", "ctrl_shift_v", "shift"):
        return True
    if mode in ("ctrl+v", "ctrl_v", "plain"):
        return False
    verdict = windowinfo.focused_is_terminal(refresh=refresh)
    if verdict is None:
        return bool(cfg.get("paste_shift", False))  # 查不出来就别瞎猜，听用户的
    return verdict


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

    def __init__(self, cfg: dict, events, injector=None):
        self.cfg = cfg
        self.events = events
        self.injector = injector
        self.recorder: Recorder | None = None
        self.last_text = ""
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._keep_clipboard = False
        self._paste_shift = False
        self._typer: output.LiveTyper | None = None

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

    def start(self, hold: bool = False) -> None:
        """hold=True 表示"按住说话"：由松手来结束，因此不用静音自动停。"""
        with self._lock:
            if self.recorder is not None:
                return
            self._cancel.clear()
            self._keep_clipboard = False
            recorder = Recorder(device=self.cfg.get("device", -1))
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
        started_at = time.monotonic()
        typer: output.LiveTyper | None = None
        saved_clipboard: str | None = None
        # 一次会话里问一次就够了：用户录的时候不应该切窗口
        self._paste_shift = resolve_paste_shift(self.cfg)
        if self._clipboard_guard_enabled():
            try:
                saved_clipboard = linuxutil.get_clipboard_text()
            except Exception:
                saved_clipboard = None

        live = bool(self.cfg.get("live_typing")) and self._injection_ready()
        settings = dict(self.cfg)
        if live:
            settings["endpoint"] = "stream"  # 实时上屏必须走流式接口
        elif bool(self.cfg.get("live_typing")):
            self.events.put(("notice", "按键注入不可用，改为识别完成后一次性粘贴"))

        try:
            if self._cancel.is_set():
                return
            if live and self.injector is not None:
                typer = output.LiveTyper(
                    self.injector,
                    paste_shift=self._paste_shift,
                    interval_ms=int(self.cfg.get("live_interval_ms", output.DEFAULT_INTERVAL_MS)),
                )
                self._typer = typer

            client = asr.AsrClient(
                settings,
                on_partial=lambda text: self._on_partial(text, typer),
                on_notice=lambda message: self.events.put(("notice", message)),
                # 上屏的 flush 交给识别线程（在界面线程里写剪贴板会冻住界面和提示）
                on_tick=(typer.tick if typer is not None else None),
                on_log=applog.write,
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
                note = self._finish(typer, text)
                extra = []
                if getattr(client, "warning", None):
                    extra.append(str(client.warning))
                lost = getattr(recorder, "lost_ms", 0.0)
                if lost > 400:
                    extra.append(f"采集端丢了约 {lost / 1000:.1f} 秒音频（系统忙时会这样）")
                if extra:
                    note = f"{note}；" + "；".join(extra)
                applog.write(
                    f"识别完成：{len(text)} 字，用时 {time.monotonic() - started_at:.1f}s，"
                    f"重连 {getattr(client, 'reconnects', 0)} 次，"
                    f"断流 {getattr(client, 'stalls', 0)} 次，"
                    f"最慢发送 {getattr(client, 'max_send_seconds', 0.0):.2f}s，"
                    f"采集端丢 {getattr(recorder, 'lost_ms', 0.0):.0f}ms"
                )
                self.events.put(("result", text, note))
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
            self._restore_clipboard(saved_clipboard)
            with self._lock:
                self.recorder = None
                self._worker = None
                self._typer = None
            self.events.put(("state", "idle"))

    def tick(self) -> None:
        """界面定时器叫的：把攒着还没上屏的中间结果补上去。"""
        typer = self._typer
        if typer is not None:
            typer.tick()

    def _on_partial(self, text: str, typer: output.LiveTyper | None) -> None:
        self.events.put(("partial", text))
        if typer is not None:
            typer.update(text)

    def _finish(self, typer: output.LiveTyper | None, text: str) -> str:
        if typer is None:
            return self._deliver(text)

        typed = typer.finish(text)
        if typer.error:
            try:
                linuxutil.set_clipboard_text(text)
                self._keep_clipboard = True
            except Exception:
                pass
            return f"{typer.error}，完整文本已复制到剪贴板"

        self.last_text = typed or text
        return f"已边听边打上去（{len(typed)} 字）"

    def _deliver(self, text: str) -> str:
        mode = self.cfg.get("output_mode", "paste")
        paste_shift = getattr(self, "_paste_shift", False)
        injection = self._injection_ready()
        try:
            if mode == "clipboard":
                linuxutil.set_clipboard_text(text)
                self._keep_clipboard = True
                return "已复制到剪贴板"

            if mode == "type" and injection and linuxutil.can_type(text):
                self.injector.type_text(text)
                return "已键入文本"

            linuxutil.set_clipboard_text(text)
            if injection:
                self.injector.paste(shift=paste_shift)
                return "已粘贴到光标处"
            self._keep_clipboard = True
            return "已复制到剪贴板（按键注入不可用，请手动按 Ctrl+V）"
        except Exception as exc:
            return f"文本已识别，但输出失败：{exc}"

    # ---------- 辅助 ----------

    def _injection_ready(self) -> bool:
        return self.injector is not None and self.injector.available

    def _clipboard_guard_enabled(self) -> bool:
        """边说边出字会反复借用剪贴板，说完要不要还原，看配置和能不能注入。"""
        if not self.cfg.get("restore_clipboard", True):
            return False
        if not self._injection_ready():
            return False  # 注入不可用时剪贴板就是最终交付方式，不能还原掉
        return True

    def _restore_clipboard(self, saved: str | None) -> None:
        if saved is None or self._keep_clipboard or not saved:
            return
        try:
            linuxutil.set_clipboard_text(saved)
        except Exception:
            pass

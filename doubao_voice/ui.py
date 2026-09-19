"""界面层。

程序形态是一个常驻后台的小工具：
* 平时没有任何窗口，只有托盘图标；
* 按下录音键时桌面底部出现一个麦克风浮标，提示"在听"；
* 主界面和设置面板按需从托盘打开，关掉不影响运行。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from . import config, engine, instance, keyhook, tray, winutil
from .overlay import Overlay
from .recorder import list_input_devices, measure_noise
from .settings import SettingsDialog

FONT = "Microsoft YaHei UI"

DOT_COLORS = {
    "idle": "#9aa0a6",
    "recording": "#e8453c",
    "recognizing": "#f5a623",
    "error": "#d93025",
}

FLASH_SECONDS = {"done": 1.6, "error": 5.0, "notice": 3.0}

STATE_TITLES = {
    "recording": "正在录音",
    "recognizing": "识别中…",
    "done": "完成",
    "error": "出错了",
    "notice": "",
    "idle": "",
}


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.guard = instance.InstanceGuard()
        if not self.guard.acquire():
            raise RuntimeError(
                "已经有一个「豆包语音输入」在运行，而且它没能退出。"
                "请在任务管理器里结束 pythonw.exe 后重试。"
            )
        self.events: queue.Queue = queue.Queue()
        self.guard.watch(self.events)
        self.session = engine.Session(cfg, self.events)
        self.target_window: int | None = None
        self.result_text = ""
        self.message = "已启动，按录音键说话"
        self.state = "idle"
        self._flash_state = "idle"
        self._flash_text = ""
        self._flash_until = 0.0
        self._capturing: str | None = None
        self._settings: SettingsDialog | None = None
        self._started_at = time.monotonic()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("豆包语音输入")
        self.root.report_callback_exception = self._on_tk_error

        self.overlay = Overlay(self.root)

        self.tray = tray.TrayIcon(self.events)
        self.tray.start()
        self.tray.wait_ready()

        self.hook = keyhook.KeyboardHook(
            self.events, share_keys=bool(self.cfg.get("share_keys", True))
        )
        self.hook.set_bindings(self._bindings())
        self.hook.start()
        self.hook.wait_ready()

        self.window = MainWindow(self.root, self)
        if self.cfg.get("show_window"):
            self.window.show()

        problem = self.hook.error or self.tray.error
        if problem:
            self._notify("error", problem)
        if self.tray.error:
            # 托盘没了就没有任何入口，把主界面亮出来兜底
            self.window.show()
        if not (self.cfg.get("api_key") or "").strip():
            self._notify("notice", "还没填 API Key，从托盘图标进去设置")
            self.root.after(600, self.open_settings)

        self.root.after(50, self._poll)

    # ------------------------------------------------------------ 事件循环

    def _poll(self) -> None:
        try:
            hwnd = winutil.foreground_window()
            if hwnd and not winutil.own_foreground_window():
                self.target_window = hwnd
        except Exception:
            pass

        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass

        self._refresh()
        self.root.after(50, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "key":
            self._on_key(event[1], event[2])
        elif kind == "keycap":
            self._on_key_captured(*event[1:])
        elif kind == "tray":
            self._on_tray(event[1])
        elif kind == "partial":
            self.message = event[1]
            self.window.set_text(event[1])
        elif kind == "result":
            self.result_text = event[1]
            self.message = event[2]
            self.window.set_text(event[1])
            self._notify("done", event[2])
        elif kind == "error":
            self.message = event[1]
            self.window.set_text(event[1], error=True)
            self._notify("error", event[1])
        elif kind == "notice":
            self.message = event[1]
            self._notify("notice", event[1])
        elif kind == "cancelled":
            self.message = "已取消这次录音"
            self._notify("notice", "已取消")
        elif kind == "empty":
            self.message = "没有识别到内容"
            self._notify("notice", "没有识别到内容")
        elif kind == "calibrated":
            self.notify_calibration(event[1])
        elif kind == "calibrate_failed":
            self._notify("error", event[1])

    def _refresh(self) -> None:
        phase = self.session.phase()
        now = time.monotonic()

        if phase == "recording":
            state, text = "recording", ""
        elif phase == "recognizing":
            state, text = "recognizing", ""
        elif now < self._flash_until:
            state, text = self._flash_state, self._flash_text
        else:
            state, text = "idle", ""

        if self.cfg.get("show_overlay", True) and state != "idle":
            title = STATE_TITLES.get(state, "")
            detail = text if state in ("error", "notice", "done") else ""
            self.overlay.show(state, f"{title}　{detail}".strip())
            if phase == "recording":
                self.overlay.set_level(self.session.level())
            elif phase == "recognizing":
                self.overlay.tick()
        elif self.overlay.visible:
            self.overlay.hide()

        if state != self.state:
            self.state = state
            self.tray.set_state(state)

        self.window.refresh(phase, self.session, self.message)

    def _notify(self, state: str, text: str) -> None:
        self._flash_state = state
        self._flash_text = text
        self._flash_until = time.monotonic() + FLASH_SECONDS.get(state, 2.0)

    # ------------------------------------------------------------ 按键

    def _bindings(self) -> dict:
        return {
            "hold": keyhook.parse_spec(self.cfg.get("hold_key", "")),
            "toggle": keyhook.parse_spec(self.cfg.get("toggle_key", "")),
        }

    def _on_key(self, name: str, action: str) -> None:
        if name == "hold":
            if action == "down":
                self.begin(hold=True)
            else:
                self.session.stop()
        elif name == "toggle" and action == "down":
            self.toggle()

    def begin(self, hold: bool) -> None:
        if self.session.busy:
            return
        if not (self.cfg.get("api_key") or "").strip():
            self._notify("error", "还没填 API Key")
            self.open_settings()
            return
        self.session.start(self.target_window, hold=hold)

    def toggle(self) -> None:
        if self.session.phase() == "recording":
            self.session.stop()
        elif not self.session.busy:
            self.begin(hold=False)

    def cancel(self) -> None:
        if self.session.phase() == "recording":
            self.session.cancel()

    def capture_key(self, slot: str) -> None:
        self._capturing = slot
        self.hook.begin_capture()

    def _on_key_captured(self, mods: int, vk: int, scan: int) -> None:
        slot, self._capturing = self._capturing, None
        if not slot:
            return
        spec = keyhook.format_spec(mods, vk, scan)
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.apply_key_capture(slot, spec)

    # ------------------------------------------------------------ 托盘

    def _on_tray(self, action: str) -> None:
        if action == "settings":
            self.open_settings()
        elif action == "show":
            self.window.toggle()
        elif action == "quit":
            self.quit()

    # ------------------------------------------------------------ 其他

    def copy_result(self) -> None:
        if not self.result_text:
            return
        try:
            winutil.set_clipboard_text(self.result_text)
            self.message = "已复制到剪贴板"
        except Exception as exc:
            self.message = f"复制失败：{exc}"

    def calibrate(self) -> None:
        self.message = "正在检测环境噪声，请保持安静…"

        def worker() -> None:
            try:
                value = measure_noise(device=int(self.cfg.get("device", -1)))
                self.events.put(("calibrated", value))
            except Exception as exc:
                self.events.put(("calibrate_failed", f"环境噪声检测失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def notify_calibration(self, noise_rms: float) -> None:
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.apply_calibration(noise_rms)

    def open_settings(self) -> None:
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.lift()
            self._settings.focus_set()
            return
        self._settings = SettingsDialog(self)

    def apply_config(self, new_cfg: dict) -> None:
        self.cfg.clear()
        self.cfg.update(new_cfg)
        config.save(self.cfg)
        self.hook.set_bindings(self._bindings())
        self.hook.set_share_keys(bool(self.cfg.get("share_keys", True)))
        self.window.apply_config()
        self.message = "设置已保存"
        self._notify("notice", "设置已保存")

    def _on_tk_error(self, exc_type, exc_value, exc_tb) -> None:
        self.message = f"界面异常：{exc_value}"

    def quit(self) -> None:
        try:
            self.session.cancel()
        except Exception:
            pass
        try:
            # 只落盘窗口位置：整体写回会把"运行期间被外部改过的配置"覆盖掉
            position = self.cfg.get("window_pos", "")
            if self.window.winfo_viewable():
                position = self.window.position()
            config.save_window_pos(position)
        except Exception:
            pass
        for stopper in (self.hook.stop, self.tray.stop):
            try:
                stopper()
            except Exception:
                pass
        # 留点时间让托盘线程把图标摘掉，否则通知区会留个幽灵图标
        self.root.after(400, self._shutdown)

    def _shutdown(self) -> None:
        self.guard.release()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


class MainWindow(tk.Toplevel):
    """按需打开的主界面：看结果、手动开始、改设置。"""

    def __init__(self, master: tk.Misc, app: App):
        super().__init__(master)
        self.app = app
        self.title("豆包语音输入")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.report_callback_exception = app._on_tk_error
        self._build()
        self.withdraw()

    def _build(self) -> None:
        pad = {"padx": 10}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad, pady=(10, 4))

        self.dot = tk.Canvas(top, width=12, height=12, highlightthickness=0)
        self.dot.pack(side="left")
        self._dot_item = self.dot.create_oval(1, 1, 11, 11, fill=DOT_COLORS["idle"], outline="")

        self.status = ttk.Label(top, text="待机", font=(FONT, 11, "bold"))
        self.status.pack(side="left", padx=(6, 0))
        ttk.Button(top, text="设置", width=6, command=self.app.open_settings).pack(side="right")
        self.keys_label = ttk.Label(top, foreground="#888")
        self.keys_label.pack(side="right", padx=(0, 8))

        self.meter = ttk.Progressbar(self, maximum=100, length=100)
        self.meter.pack(fill="x", **pad, pady=(0, 6))

        self.preview = tk.Text(
            self, height=6, wrap="word", relief="flat", bg="#f4f5f7", fg="#202124",
            font=(FONT, 10), state="disabled", cursor="arrow",
        )
        self.preview.pack(fill="both", expand=True, **pad)
        self.preview.tag_configure("error", foreground=DOT_COLORS["error"])

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **pad, pady=(8, 4))
        self.toggle_button = ttk.Button(buttons, text="开始录音", command=self.app.toggle)
        self.toggle_button.pack(side="left", fill="x", expand=True)
        self.copy_button = ttk.Button(
            buttons, text="复制", width=6, command=self.app.copy_result, state="disabled"
        )
        self.copy_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="隐藏", width=6, command=self.hide).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="退出", width=6, command=self.app.quit).pack(side="left", padx=(6, 0))

        self.message = ttk.Label(self, foreground="#5f6368", anchor="w", wraplength=380)
        self.message.pack(fill="x", **pad, pady=(0, 10))

        self.apply_config()
        self.geometry(self._geometry())

    def _geometry(self) -> str:
        width, height = 420, 280
        saved = (self.app.cfg.get("window_pos") or "").strip()
        if saved.startswith(("+", "-")):
            return f"{width}x{height}{saved}"
        screen_w = self.winfo_screenwidth()
        return f"{width}x{height}+{max(0, screen_w - width - 60)}+120"

    def apply_config(self) -> None:
        self.attributes("-topmost", bool(self.app.cfg.get("always_on_top", True)))
        hold = keyhook.key_label(*keyhook.parse_spec(self.app.cfg.get("hold_key", "")))
        toggle = keyhook.key_label(*keyhook.parse_spec(self.app.cfg.get("toggle_key", "")))
        self.keys_label.configure(text=f"长按 {hold}　·　切换 {toggle}")

    def refresh(self, phase: str, session: engine.Session, message: str) -> None:
        if phase == "recording":
            self.status.configure(text=f"录音中 {session.elapsed():.1f}s")
            self.dot.itemconfigure(self._dot_item, fill=DOT_COLORS["recording"])
            self.meter["value"] = min(100.0, session.level() * 100.0)
            self.toggle_button.configure(text="停止", state="normal")
        elif phase == "recognizing":
            self.status.configure(text="识别中…")
            self.dot.itemconfigure(self._dot_item, fill=DOT_COLORS["recognizing"])
            self.meter["value"] = 0
            self.toggle_button.configure(text="识别中…", state="disabled")
        else:
            color = DOT_COLORS["error"] if self.app.state == "error" else DOT_COLORS["idle"]
            self.status.configure(text="待机")
            self.dot.itemconfigure(self._dot_item, fill=color)
            self.meter["value"] = 0
            self.toggle_button.configure(text="开始录音", state="normal")
        if message:
            self.message.configure(text=message)
        self.copy_button.configure(state="normal" if self.app.result_text else "disabled")

    def set_text(self, text: str, error: bool = False) -> None:
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.tag_add("error" if error else "ok", "1.0", "end")
        self.preview.see("end")
        self.preview.configure(state="disabled")

    def position(self) -> str:
        return f"+{self.winfo_x()}+{self.winfo_y()}"

    def show(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self) -> None:
        self.withdraw()

    def toggle(self) -> None:
        if self.winfo_viewable():
            self.hide()
        else:
            self.show()


# 设置面板见 settings.py（参数较多，单独成模块）


def run(cfg: dict) -> None:
    enable_dpi_awareness()
    try:
        app = App(cfg)
    except RuntimeError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("豆包语音输入", str(exc))
        root.destroy()
        return
    app.run()

"""界面层（GTK3）。

形态和原来的 Windows 版一致：
* 平时没有任何窗口，控制入口在顶栏的托盘图标上（GNOME 用 StatusNotifierItem）；
* 按下录音键时桌面底部出现一个麦克风浮标；
* 主界面和设置面板按需打开，关掉不影响后台运行。

本机没装 tkinter（也装不了），项目里已装的 GUI 库是 PyGObject，所以界面用 GTK3，
跑在 Wayland 后端（剪贴板、焦点都由 Wayland 管）；浮标例外，见 overlay.py。
"""

from __future__ import annotations

import queue
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import config, engine, instance, keyhook, linuxutil, tray  # noqa: E402
from .overlay import Overlay  # noqa: E402
from .recorder import measure_noise  # noqa: E402
from .settings import SettingsDialog  # noqa: E402

POLL_MS = 50          # 录音/识别中、或者窗口可见时的轮询间隔
IDLE_POLL_MS = 250    # 待机且没有窗口时的间隔（省电）

DOT_COLORS = {
    "idle": "#9aa0a6",
    "recording": "#e8453c",
    "recognizing": "#f5a623",
    "error": "#d93025",
}

FLASH_SECONDS = {"done": 2.0, "error": 6.0, "notice": 4.0}

STATE_TITLES = {
    "recording": "正在录音",
    "recognizing": "识别中…",
    "done": "完成",
    "error": "出错了",
    "notice": "",
    "idle": "",
}


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.guard = instance.InstanceGuard()
        if not self.guard.acquire():
            raise RuntimeError(
                "已经有一个「豆包语音输入」在运行，而且它没能退出。"
                "可以先执行 python3 voice_input.py --quit 再启动。"
            )
        self.events: queue.Queue = queue.Queue()
        self.guard.watch(self.events)
        self.guard.status_provider = self._status_text

        self.injector = linuxutil.KeyInjector()
        self.injector_error: str | None = None
        self.session = engine.Session(cfg, self.events, self.injector)

        self.result_text = ""
        self.message = "已启动，按录音键说话"
        self.state = "idle"
        self._flash_state = "idle"
        self._flash_text = ""
        self._flash_until = 0.0
        self._capturing: str | None = None
        self._settings: SettingsDialog | None = None

        self.overlay = Overlay(enabled=bool(cfg.get("show_overlay", True)))
        self.tray = tray.TrayIcon(self.events)
        self.hook = keyhook.KeyboardHook(
            self.events, share_keys=bool(cfg.get("share_keys", True))
        )
        self._first_run = not config.CONFIG_PATH.exists()

        self.window = MainWindow(self)
        self._start()

    # ------------------------------------------------------------ 启动

    def _start(self) -> None:
        # 发粘贴前先问钩子"用户还按着哪些修饰键"，把残留的修饰键抬起来
        self.injector.set_modifier_source(self.hook.held_modifier_codes)
        self.injector.set_release_stuck(bool(self.cfg.get("release_modifiers", True)))
        try:
            self.injector.open()
        except RuntimeError as exc:
            self.injector_error = str(exc)

        self.tray.start()
        self.tray.wait_ready(2.0)

        self.hook.set_bindings(self._bindings())
        self.hook.start()
        self.hook.wait_ready(1.5)

        if self.cfg.get("show_window"):
            self.window.show()

        for problem in (self.injector_error, self.hook.error, self.tray.error):
            if problem:
                self._notify("error", problem)

        if self.tray.error:
            self.window.show()  # 托盘没起来就没有入口了，把主界面亮出来兜底

        if not (self.cfg.get("api_key") or "").strip():
            self._notify("notice", "还没填 API Key，先打开设置填一下")
            GLib.timeout_add(600, self.open_settings)

        if self._first_run:
            self._auto_calibrate()  # 头一次跑，先摸一下这台机器的环境噪声

        GLib.timeout_add(IDLE_POLL_MS, self._poll)

    def _auto_calibrate(self) -> None:
        """首次启动自动测一次环境噪声。

        默认阈值（200）是按 Windows 那台机器的安静环境定的，本机环境噪声 RMS 就有
        六千多，用默认值"静音自动停止"永远不会触发，所以第一次跑就把阈值调到位。
        """

        def worker() -> None:
            try:
                noise = measure_noise(device=self.cfg.get("device", -1), seconds=2.0)
            except Exception:
                return
            if noise <= 0:
                return
            threshold = max(80, int(noise * 2.5))
            self.cfg["vad_threshold"] = threshold
            try:
                config.save(self.cfg)
            except Exception:
                pass
            self.events.put(
                ("notice", f"已按环境噪声（{noise:.0f}）把灵敏度设为 {threshold}，设置里可改")
            )
            if self._settings is not None and self._settings.winfo_exists():
                self._settings.apply_calibration(noise)

        threading.Thread(target=worker, daemon=True, name="calibrate").start()

    # ------------------------------------------------------------ 事件循环

    def _poll(self) -> bool:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        busy = self.session.busy or self.state != "idle"
        self._refresh()
        interval = POLL_MS if (busy or self.window.get_visible()) else IDLE_POLL_MS
        GLib.timeout_add(interval, self._poll)
        return False

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

        # 窗口没露出来就别折腾 GTK 了（隐藏时刷新等于白烧 CPU）
        if self.window.get_visible():
            self.window.refresh(phase, self.session, self.message)

    def _notify(self, state: str, text: str) -> None:
        self._flash_state = state
        self._flash_text = text
        self._flash_until = time.monotonic() + FLASH_SECONDS.get(state, 2.0)
        if state == "error" or (self.cfg.get("notify") and state in ("done", "notice")):
            title = STATE_TITLES.get(state, "") or "豆包语音输入"
            # 系统通知是同步 D-Bus 调用（超时 2 秒），别放在界面线程里等
            threading.Thread(
                target=lambda: linuxutil.notify(title, text), daemon=True
            ).start()

    def _status_text(self) -> str:
        phase = self.session.phase()
        return {
            "recording": "正在录音",
            "recognizing": "识别中",
        }.get(phase, "待机")

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
        self.session.start(hold=hold)

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

    def _on_key_captured(self, mods: int, code: int) -> None:
        slot, self._capturing = self._capturing, None
        if not slot:
            return
        spec = keyhook.format_spec(mods, code)
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.apply_key_capture(slot, spec)

    # ------------------------------------------------------------ 托盘 / 窗口

    def _on_tray(self, action: str) -> None:
        if action == "settings":
            self.open_settings()
        elif action == "show":
            self.window.toggle()
        elif action == "toggle":
            self.toggle()          # 命令行 --toggle / 托盘菜单都走这里
        elif action == "quit":
            self.quit()
        elif action.startswith("mode:"):
            self.toggle_mode(action.split(":", 1)[1])

    def toggle_mode(self, mode: str) -> None:
        """托盘菜单里切换"长按说话 / 按一下开始"，等价于把两个键对调。"""
        hold, toggle = self.cfg.get("hold_key", ""), self.cfg.get("toggle_key", "")
        self.cfg["hold_key"], self.cfg["toggle_key"] = toggle, hold
        config.save(self.cfg)
        self.hook.set_bindings(self._bindings())
        self.window.apply_config()
        self.tray.set_mode(mode)
        self.message = f"已切换为：{'长按说话' if mode == 'hold' else '按一下开始 / 再按结束'}"
        self._notify("notice", self.message)

    def copy_result(self) -> None:
        if not self.result_text:
            return
        try:
            linuxutil.set_clipboard_text(self.result_text)
            self.message = "已复制到剪贴板"
        except Exception as exc:
            self.message = f"复制失败：{exc}"

    def calibrate(self) -> None:
        self.message = "正在检测环境噪声，请保持安静…"

        def worker() -> None:
            try:
                value = measure_noise(device=self.cfg.get("device", -1))
                self.events.put(("calibrated", value))
            except Exception as exc:
                self.events.put(("calibrate_failed", f"环境噪声检测失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def notify_calibration(self, noise_rms: float) -> None:
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.apply_calibration(noise_rms)

    def open_settings(self) -> None:
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.present()
            return
        self._settings = SettingsDialog(self)

    def apply_config(self, new_cfg: dict) -> None:
        self.cfg.clear()
        self.cfg.update(new_cfg)
        config.save(self.cfg)
        self.hook.set_bindings(self._bindings())
        self.hook.set_share_keys(bool(self.cfg.get("share_keys", True)))
        self.injector.set_release_stuck(bool(self.cfg.get("release_modifiers", True)))
        self.overlay = Overlay(enabled=bool(self.cfg.get("show_overlay", True)))
        self.window.apply_config()
        self.message = "设置已保存"
        self._notify("notice", "设置已保存")

    # ------------------------------------------------------------ 退出

    def quit(self) -> None:
        try:
            self.session.cancel()
        except Exception:
            pass
        for stopper in (self.hook.stop, self.tray.stop):
            try:
                stopper()
            except Exception:
                pass
        try:
            self.overlay.close()
        except Exception:
            pass
        try:
            self.injector.close()
        except Exception:
            pass
        self.guard.release()
        GLib.timeout_add(200, Gtk.main_quit)

    def run(self) -> None:
        Gtk.main()


class MainWindow(Gtk.Window):
    """按需打开的主界面：看结果、手动开始、改设置。"""

    def __init__(self, app: App):
        super().__init__(title="豆包语音输入")
        self.app = app
        self.set_default_size(440, 300)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", self._on_delete)
        self._build()
        self.hide()

    def _on_delete(self, *_args) -> bool:
        self.hide()
        return True  # 关掉只是隐藏，后台继续跑

    def _build(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.add(box)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(top, False, False, 0)

        self.dot = Gtk.Label()
        self.dot.set_markup(self._dot_markup(DOT_COLORS["idle"]))
        top.pack_start(self.dot, False, False, 0)

        self.status = Gtk.Label(label="待机")
        self.status.set_xalign(0)
        top.pack_start(self.status, False, False, 0)

        settings_button = Gtk.Button(label="设置")
        settings_button.connect("clicked", lambda *_: self.app.open_settings())
        top.pack_end(settings_button, False, False, 0)

        self.keys_label = Gtk.Label()
        self.keys_label.get_style_context().add_class("dim-label")
        top.pack_end(self.keys_label, False, False, 0)

        self.meter = Gtk.ProgressBar()
        box.pack_start(self.meter, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.preview = Gtk.TextView()
        self.preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preview.set_editable(False)
        self.preview.set_cursor_visible(False)
        scroller.add(self.preview)
        box.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(buttons, False, False, 0)
        self.toggle_button = Gtk.Button(label="开始录音")
        self.toggle_button.connect("clicked", lambda *_: self.app.toggle())
        buttons.pack_start(self.toggle_button, True, True, 0)
        self.copy_button = Gtk.Button(label="复制")
        self.copy_button.connect("clicked", lambda *_: self.app.copy_result())
        self.copy_button.set_sensitive(False)
        buttons.pack_start(self.copy_button, False, False, 0)
        hide_button = Gtk.Button(label="隐藏")
        hide_button.connect("clicked", lambda *_: self.hide())
        buttons.pack_start(hide_button, False, False, 0)
        quit_button = Gtk.Button(label="退出")
        quit_button.connect("clicked", lambda *_: self.app.quit())
        buttons.pack_start(quit_button, False, False, 0)

        self.message = Gtk.Label()
        self.message.set_xalign(0)
        self.message.set_line_wrap(True)
        self.message.get_style_context().add_class("dim-label")
        box.pack_start(self.message, False, False, 0)

        self.apply_config()

    @staticmethod
    def _dot_markup(color: str) -> str:
        return f'<span foreground="{color}" size="large">●</span>'

    def apply_config(self) -> None:
        self.set_keep_above(bool(self.app.cfg.get("always_on_top", True)))
        hold = keyhook.key_label(*keyhook.parse_spec(self.app.cfg.get("hold_key", "")))
        toggle = keyhook.key_label(*keyhook.parse_spec(self.app.cfg.get("toggle_key", "")))
        self.keys_label.set_text(f"长按 {hold}　·　切换 {toggle}")

    def refresh(self, phase: str, session: engine.Session, message: str) -> None:
        if phase == "recording":
            self.status.set_text(f"录音中 {session.elapsed():.1f}s")
            self.dot.set_markup(self._dot_markup(DOT_COLORS["recording"]))
            self.meter.set_fraction(min(1.0, session.level()))
            self.toggle_button.set_label("停止")
            self.toggle_button.set_sensitive(True)
        elif phase == "recognizing":
            self.status.set_text("识别中…")
            self.dot.set_markup(self._dot_markup(DOT_COLORS["recognizing"]))
            self.meter.set_fraction(0.0)
            self.toggle_button.set_label("识别中…")
            self.toggle_button.set_sensitive(False)
        else:
            color = DOT_COLORS["error"] if self.app.state == "error" else DOT_COLORS["idle"]
            self.status.set_text("待机")
            self.dot.set_markup(self._dot_markup(color))
            self.meter.set_fraction(0.0)
            self.toggle_button.set_label("开始录音")
            self.toggle_button.set_sensitive(True)
        if message:
            self.message.set_text(message)
        self.copy_button.set_sensitive(bool(self.app.result_text))

    def set_text(self, text: str, error: bool = False) -> None:
        self.preview.get_buffer().set_text(text)
        self.message.get_style_context().remove_class("error")
        if error:
            self.message.get_style_context().add_class("error")

    def show(self) -> None:
        self.show_all()
        self.present()

    def toggle(self) -> None:
        if self.get_visible():
            self.hide()
        else:
            self.show()


def run(cfg: dict) -> None:
    try:
        app = App(cfg)
    except RuntimeError as exc:
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="豆包语音输入",
        )
        dialog.format_secondary_text(str(exc))
        dialog.run()
        dialog.destroy()
        return
    app.run()

"""录音浮标进程（X11 / XWayland），由 overlay.py 拉起。

单独一个进程、而且强制走 X11，是为了拿 override-redirect 窗口：
这种窗口不受窗口管理器管辖，永远抢不到焦点——正在打字的窗口不会掉焦点，
字才能打进正确的地方。Wayland 那边没有等价能力（新窗口一定抢焦点）。

从标准输入收 JSON 行：
    {"cmd": "show", "state": "recording", "text": "正在录音"}
    {"cmd": "level", "value": 0.42}
    {"cmd": "text", "text": "识别中…"}
    {"cmd": "hide"}
标准输入关闭（父进程没了）就自己退出。
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ["GDK_BACKEND"] = "x11"  # 必须在 import gi 之前

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

WIDTH, HEIGHT = 340, 60
MARGIN_BOTTOM = 110
RING_LEFT, RING_TOP, RING_SIZE = 6, 6, 48

STATE_COLORS = {
    "recording": (0.91, 0.27, 0.24),
    "recognizing": (0.96, 0.65, 0.14),
    "done": (0.20, 0.66, 0.33),
    "error": (0.85, 0.19, 0.16),
}


class OverlayWindow(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.state = ""
        self.text = ""
        self.level = 0.0
        self._pulse = 0.0
        self._extent = 0.0
        self._layout = None          # Pango 布局缓存（每帧重建排版很贵）
        self._layout_text = None

        screen = Gdk.Screen.get_default()
        self.set_app_paintable(True)
        self.set_visual(screen.get_rgba_visual())
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_keep_above(True)
        self.set_size_request(WIDTH, HEIGHT)
        self.connect("draw", self._on_draw)

        self.realize()
        self._make_override_redirect()
        self._make_click_through()
        self.move(*self._place())
        self.show_all()
        self.hide()

        GLib.timeout_add(40, self._animate)
        GLib.timeout_add(16, lambda: False)

    # ---------- 窗口属性 ----------

    def _make_override_redirect(self) -> None:
        window = self.get_window()
        if window is not None:
            window.set_override_redirect(True)

    def _make_click_through(self) -> None:
        """整块区域点击穿透，免得浮标挡住鼠标。"""
        window = self.get_window()
        if window is None:
            return
        try:
            window.input_shape_combine_region(cairo.Region(), 0, 0)
        except Exception:
            pass

    def _place(self) -> tuple[int, int]:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        x = geometry.x + (geometry.width - WIDTH) // 2
        y = geometry.y + geometry.height - HEIGHT - MARGIN_BOTTOM
        return max(0, x), max(0, y)

    # ---------- 外部指令 ----------

    def apply(self, payload: dict) -> None:
        command = payload.get("cmd")
        if command == "show":
            self.state = str(payload.get("state") or "recording")
            self.text = str(payload.get("text") or "")
            self.level = 0.0
            self._pulse = 0.0
            self.move(*self._place())
            self._make_override_redirect()
            self._make_click_through()
            self.show_all()
            self.queue_draw()
        elif command == "level":
            self.level = max(0.0, min(1.0, float(payload.get("value") or 0.0)))
            self.queue_draw()
        elif command == "text":
            self.text = str(payload.get("text") or "")
            self.queue_draw()
        elif command == "hide":
            self.hide()

    # ---------- 画 ----------

    def _animate(self) -> bool:
        if self.state == "recognizing" and self.get_visible():
            self._pulse = (self._pulse + 0.06) % 1.0
            self.queue_draw()
        return True

    def _on_draw(self, _widget, cr) -> bool:
        # 先清成完全透明，再画一个半透明圆角底板
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        color = STATE_COLORS.get(self.state, STATE_COLORS["recording"])
        radius = HEIGHT / 2
        cr.new_sub_path()
        cr.arc(WIDTH - radius, radius, radius, -1.5708, 0)
        cr.arc(WIDTH - radius, HEIGHT - radius, radius, 0, 1.5708)
        cr.arc(radius, HEIGHT - radius, radius, 1.5708, 3.1416)
        cr.arc(radius, radius, radius, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgba(0.12, 0.13, 0.15, 0.92)
        cr.fill()

        # 圆环 + 音量弧
        center = (RING_LEFT + RING_SIZE / 2, RING_TOP + RING_SIZE / 2)
        ring_radius = RING_SIZE / 2 - 2
        cr.set_line_width(2)
        cr.set_source_rgba(*color, 1.0)
        cr.arc(center[0], center[1], ring_radius, 0, 6.2832)
        cr.stroke()

        extent = self._extent_for_state()
        if extent:
            cr.set_line_width(3.5)
            cr.set_source_rgba(*color, 1.0)
            cr.arc(center[0], center[1], ring_radius, -1.5708, -1.5708 + extent)
            cr.stroke()

        self._draw_mic(cr, center)

        if self.text:
            if self._layout is None:
                self._layout = Pango.Layout(self.get_pango_context())
                self._layout.set_font_description(Pango.FontDescription("10.5"))
                self._layout.set_width(int((WIDTH - RING_LEFT - RING_SIZE - 16) * Pango.SCALE))
                self._layout.set_ellipsize(Pango.EllipsizeMode.END)
            if self._layout_text != self.text:
                self._layout.set_text(self.text, -1)
                self._layout_text = self.text
            PangoCairo.update_layout(cr, self._layout)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(RING_LEFT + RING_SIZE + 10, HEIGHT / 2 - 9)
            PangoCairo.show_layout(cr, self._layout)
        return False

    def _extent_for_state(self) -> float:
        if self.state == "recording":
            return -6.2832 * max(0.0, min(1.0, self.level))
        if self.state == "recognizing":
            return -6.2832 * (0.15 + 0.45 * abs(1 - 2 * self._pulse))
        return 0.0

    def _draw_mic(self, cr, center: tuple[float, float]) -> None:
        x, y = center
        cr.set_source_rgba(1, 1, 1, 0.95)
        # 话筒头
        cr.save()
        cr.translate(x, y - 3)
        cr.scale(1.0, 1.35)
        cr.arc(0, 0, 4.6, 0, 6.2832)
        cr.restore()
        cr.fill()
        # 拾音罩
        cr.set_line_width(2)
        cr.arc(x, y - 1, 8.6, 0.35, 2.79)
        cr.stroke()
        # 支架
        cr.move_to(x, y + 7.6)
        cr.line_to(x, y + 11)
        cr.move_to(x - 4, y + 11)
        cr.line_to(x + 4, y + 11)
        cr.stroke()


def _stdin_reader(window: OverlayWindow) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            GLib.idle_add(window.apply, payload)
    GLib.idle_add(Gtk.main_quit)


def _watch_parent(window) -> None:
    """父进程没了就退出（stdin 也可能因为别的原因没读到 EOF，多一层保险）。"""
    while True:
        time.sleep(2.0)
        if os.getppid() == 1:
            GLib.idle_add(Gtk.main_quit)
            return


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("没有 DISPLAY，浮标进程退出", file=sys.stderr)
        return 1

    # 先等父进程的指令再决定要不要建窗口：这样父进程一没（stdin EOF）就能立刻退出，
    # 不会在建窗口的过程中卡住变成残留进程。
    import threading

    first = sys.stdin.readline()
    if not first.strip():
        return 0
    try:
        window = OverlayWindow()
    except Exception as exc:  # 画不出来就安静退出，主程序会回退到别的方式提示
        print(f"浮标窗口创建失败：{exc}", file=sys.stderr)
        return 1

    try:
        import json

        window.apply(json.loads(first))
    except ValueError:
        pass

    threading.Thread(target=_stdin_reader, args=(window,), daemon=True).start()
    threading.Thread(target=_watch_parent, args=(window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

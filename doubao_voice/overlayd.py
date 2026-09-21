"""录音浮标进程（X11 / XWayland），由 overlay.py 拉起。

单独一个进程、而且强制走 X11，是为了拿 override-redirect 窗口：
这种窗口不受窗口管理器管辖，永远抢不到焦点——正在打字的窗口不会掉焦点，
字才能打进正确的地方。Wayland 那边没有等价能力（新窗口一定抢焦点）。

两种渲染器，二选一（见 pick_renderer()）：
    webkit  把 prototype 的 embed 页面（voice-glow / metal-fx 那套特效）嵌进来画；
            窗口、状态协议、点击穿透全不变，只是「怎么画」交给网页。约 450MB 内存。
    cairo   老办法，GTK 直接画胶囊 + 圆环 + 文字，没有特效，但几乎不占内存。
    换渲染器不改协议：下面这套 JSON 行两种渲染器都认。

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

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# cairo 渲染器要 pycairo 加上 GI 的 cairo 类型转换（python3-gi-cairo）；WebKit 渲染器
# 两样都不要。所以分开按需加载，缺哪一个都不影响另一个。
try:
    import cairo  # noqa: E402

    gi.require_foreign("cairo")
except ImportError:  # pragma: no cover
    cairo = None  # type: ignore[assignment]

try:
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo  # noqa: E402
except (ImportError, ValueError):  # pragma: no cover
    Pango = PangoCairo = None  # type: ignore[assignment]

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMBED_HTML = os.path.join(APP_DIR, "prototype", "dist", "embed.html")
RENDERER_ENV = "DOUBAO_OVERLAY_RENDERER"

WIDTH, HEIGHT = 340, 60
MARGIN_BOTTOM = 110
RING_LEFT, RING_TOP, RING_SIZE = 6, 6, 48


def _webkit_available() -> bool:
    """WebKit2 typelib 在不在（在只说明能导入，页面能不能跑是另一回事）。"""
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def desired_renderer() -> str:
    """想要哪个渲染器：环境变量 > config.json 的 overlay_renderer > auto。

    环境变量优先，方便临时试：DOUBAO_OVERLAY_RENDERER=cairo python3 voice_input.py
    """
    want = (os.environ.get(RENDERER_ENV) or "").strip().lower()
    if not want:
        try:
            from .config import load

            want = str(load().get("overlay_renderer") or "auto").strip().lower()
        except Exception:  # 配置读不了就当 auto，不能因为配置出错让浮标不显示
            want = "auto"
    return want or "auto"


def pick_renderer() -> str:
    """返回 'webkit' 或 'cairo'。

    auto：构建产物在、WebKit 可用，就用 WebKit（有特效），否则安静地退回 cairo。
    """
    want = desired_renderer()
    if want == "cairo":
        return "cairo"
    if want == "webkit":
        if not os.path.isfile(EMBED_HTML):
            print(f"要 WebKit 渲染器，但找不到 {EMBED_HTML}（先 npm run build），退回 cairo",
                  file=sys.stderr)
            return "cairo"
        if not _webkit_available():
            print("要 WebKit 渲染器，但 WebKit2 不可用，退回 cairo", file=sys.stderr)
            return "cairo"
        return "webkit"
    if os.path.isfile(EMBED_HTML) and _webkit_available():
        return "webkit"
    return "cairo"

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


class WebOverlay(OverlayWindow):
    """WebKit 渲染器：把 prototype 的 embed 页面嵌进来，特效就是页面里那一套。

    窗口外形（override-redirect / 不抢焦点 / 点击穿透 / 底部居中）直接继承老渲染器，
    所以换的只是「怎么画」；协议也不变，apply() 收到的帧原样转给页面的
    window.voiceOverlay.apply()。

    页面加载要几百毫秒，这期间来的帧先攒着，页面一就绪就补发（页面会改 document.title
    通知我们）。攒的是「最近一条状态」和「最近一条音量」，不会把整段历史灌进去。
    """

    READY_TITLE = "voice-overlay-ready"

    def __init__(self, on_fatal=None) -> None:
        self._ready = False
        self._pending_state: dict | None = None
        self._pending_level: dict | None = None
        self._fatal = False
        self._on_fatal = on_fatal
        super().__init__()

        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2  # noqa: PLC0415

        self.webview = WebKit2.WebView()
        # 不设这个，窗口会是白底，圆角外就不透了
        self.webview.set_background_color(Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=0.0))
        self.webview.set_can_focus(False)
        settings = self.webview.get_settings()
        for setter in ("set_enable_webgl", "set_allow_file_access_from_file_urls"):
            try:
                getattr(settings, setter)(True)
            except AttributeError:
                pass
        self.webview.connect("notify::title", self._on_title)
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("load-failed", self._on_load_failed)
        self.add(self.webview)
        self.webview.load_uri("file://" + EMBED_HTML)

    # ---------- 跟 cairo 版同名的接口 ----------

    def _animate(self) -> bool:
        return False  # 动画归页面管，这边不用自己重绘

    def _on_draw(self, _widget, cr) -> bool:
        # 底板由页面画，这里只把窗口清成完全透明，免得 WebView 底下压着一层黑
        if cairo is not None:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
        return False

    def apply(self, payload: dict) -> None:
        command = payload.get("cmd")
        if command == "show":
            self.state = str(payload.get("state") or "recording")
            self.text = str(payload.get("text") or "")
            self.level = 0.0
            self.move(*self._place())
            self._make_override_redirect()
            self._ensure_pass_through()
            self.show_all()
            self._pending_level = None
            self._queue(payload, is_state=True)
        elif command == "level":
            self.level = max(0.0, min(1.0, float(payload.get("value") or 0.0)))
            self._queue({"cmd": "level", "value": round(self.level, 3)}, is_state=False)
        elif command == "text":
            self.text = str(payload.get("text") or "")
            self._queue(payload, is_state=True)
        elif command == "hide":
            self.hide()
            self._pending_level = None
            self._queue(payload, is_state=True)

    # ---------- 往页面推帧 ----------

    def _queue(self, payload: dict, *, is_state: bool) -> None:
        if is_state:
            self._pending_state = payload
        else:
            self._pending_level = payload
        if self._ready:
            self._eval(payload)

    def _flush(self) -> bool:
        """把攒下的帧补发给页面。重复发没关系，页面那边是幂等的。"""
        if self._pending_state is not None:
            self._eval(self._pending_state)
        if self._pending_level is not None:
            self._eval(self._pending_level)
        return False

    def _eval(self, payload: dict) -> None:
        script = "window.voiceOverlay && window.voiceOverlay.apply(%s);" % json.dumps(
            payload, ensure_ascii=False
        )
        try:
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        except Exception as exc:  # pragma: no cover
            print(f"给浮标页面推帧失败：{exc}", file=sys.stderr)

    def _ensure_pass_through(self) -> None:
        """点击穿透要设两处：顶层窗口 + WebView 自己的 Gdk 窗口。

        只设顶层的话，WebView 那个子窗口照样接住鼠标，浮标就会挡住下面的窗口。
        """
        for widget in (self, self.webview):
            handle = widget.get_window()
            if handle is None:
                continue
            try:
                handle.set_pass_through(True)
                continue
            except (AttributeError, TypeError):  # pragma: no cover
                pass
            if cairo is not None:
                try:
                    handle.input_shape_combine_region(cairo.Region(), 0, 0)
                except Exception:
                    pass

    # ---------- 页面事件 ----------

    def _on_title(self, view, _param) -> None:
        if self._ready or view.get_title() != self.READY_TITLE:
            return
        self._ready = True
        self._flush()

    def _on_load_changed(self, view, event) -> None:
        from gi.repository import WebKit2  # noqa: PLC0415

        if event == WebKit2.LoadEvent.FINISHED:
            # WebView 的子窗口这会儿才存在，补一次穿透
            self._ensure_pass_through()
            # 标题握手没来也兜一下，别让浮标空着
            GLib.timeout_add(500, self._flush)

    def _on_load_failed(self, view, event, uri, error) -> bool:
        if self._fatal:
            return False
        self._fatal = True
        print(f"浮标页面加载失败（{uri}）：{error.message}", file=sys.stderr)
        if self._on_fatal is not None:
            self._on_fatal()
        return False


def _stdin_reader(apply) -> None:
    """把 stdin 上的 JSON 行交给主线程的 apply。

    传进来的是 OverlayHost.apply（而不是窗口对象），这样窗口被换成 cairo 版之后，
    读 stdin 的线程还指着对的那个。
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            GLib.idle_add(apply, payload)
    GLib.idle_add(Gtk.main_quit)


def _watch_parent(window) -> None:
    """父进程没了就退出（stdin 也可能因为别的原因没读到 EOF，多一层保险）。"""
    while True:
        time.sleep(2.0)
        if os.getppid() == 1:
            GLib.idle_add(Gtk.main_quit)
            return


class OverlayHost:
    """持有当前浮标窗口。

    存在的理由只有一个：WebKit 页面加载失败时能悄悄换成 cairo 窗口重画一遍，而不是
    给用户一个空白浮标——overlayd 的 stderr 在 app 里是丢掉的（overlay.py 给了 DEVNULL），
    不换就等于无声失败。
    """

    def __init__(self, renderer: str) -> None:
        self.renderer = renderer
        self.last: dict | None = None
        self.window = self._build(renderer)

    def _build(self, renderer: str):
        if renderer == "webkit":
            return WebOverlay(on_fatal=self._fallback)
        return OverlayWindow()

    def apply(self, payload: dict) -> None:
        self.last = payload
        self.window.apply(payload)

    def _fallback(self) -> None:
        if self.renderer == "cairo":
            return
        try:
            fallback = self._build("cairo")
        except Exception as exc:
            print(f"退回 cairo 也失败了：{exc}", file=sys.stderr)
            return
        old, self.window = self.window, fallback
        self.renderer = "cairo"
        old.destroy()
        if self.last is not None:
            self.window.apply(self.last)


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

    renderer = pick_renderer()
    try:
        host = OverlayHost(renderer)
    except Exception as exc:
        if renderer == "webkit":
            print(f"WebKit 浮标创建失败（{exc}），退回 cairo", file=sys.stderr)
            try:
                host = OverlayHost("cairo")
            except Exception as inner:  # pragma: no cover
                print(f"浮标窗口创建失败：{inner}", file=sys.stderr)
                return 1
        else:
            print(f"浮标窗口创建失败：{exc}", file=sys.stderr)
            return 1

    try:
        host.apply(json.loads(first))
    except ValueError:
        pass

    threading.Thread(target=_stdin_reader, args=(host.apply,), daemon=True).start()
    threading.Thread(target=_watch_parent, args=(host.window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

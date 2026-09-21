#!/usr/bin/env python3
"""WebKitGTK 可行性探针 —— 只测，不集成。跑在目标机（有 DISPLAY 的那台）上。

它把 overlayd.py 的窗口条件原样复刻一遍（X11 override-redirect、不抢焦点、点击穿透、
透明背景），然后在里面放一个 WebKit2.WebView，回答四个问题：

  1. WebGL2 有没有？RENDERER 字符串是硬件 GPU 还是 llvmpipe（软件渲染）？
  2. 这个窗口会不会抢焦点？（有 xdotool 就从外面问活动窗口有没有被换掉；没有就人肉打字测）
  3. 背景真的透明吗？（视觉上自己看）
  4. 整个进程树（含 WebKit 的 WebProcess/NetworkProcess/GPUProcess）吃多少内存和 CPU？

用法：
    # 先用自带的最小页面做体检（不需要任何后台服务）
    python3 prototype/webkit_probe.py --seconds 15

    # 再量真实开销：先起 bridge，再指过去（两个效果会一直动，等于最坏情况）
    python3 prototype/bridge.py &
    python3 prototype/webkit_probe.py --url http://127.0.0.1:8765/ --seconds 20

    # 想对比"不穿透/会抢焦点"的窗口是什么样，加 --no-override
    python3 prototype/webkit_probe.py --no-override --seconds 10

小提示：`sudo apt install xdotool` 装上它，「抢焦点」这条就能自动判；不装也能跑，
只是那条结论要你自己盯着看。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("GDK_BACKEND", "x11")  # 和 overlayd.py 一致，必须在 import gi 之前

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

# pycairo：GTK 的 draw 回调给的就是 cairo.Context，但模块本身要自己 import
# （Ubuntu 上是 python3-gi-cairo，一般随 python3-gi 一起装）。缺了就退化成"不清屏"。
try:
    import cairo  # noqa: E402
except ImportError:  # pragma: no cover
    cairo = None  # type: ignore[assignment]

CLK = os.sysconf("SC_CLK_TCK")

# 体检页：透明底 + 一个和浮标同尺寸的胶囊，顺便把 WebGL2 的情况写进 document.title
PROBE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body{margin:0;background:transparent;overflow:hidden}
  .pill{width:340px;height:60px;border-radius:30px;background:rgba(31,33,38,.92);
        display:flex;align-items:center;gap:13px;padding:0 18px 0 14px;box-sizing:border-box}
  .ring{width:44px;height:44px;border:2px solid #e8453c;border-radius:50%}
  .txt{color:#fff;font:13px -apple-system,"Noto Sans CJK SC",sans-serif}
</style></head>
<body>
  <div class="pill"><span class="ring"></span><span class="txt" id="info">探测中…</span></div>
<script>
(function () {
  function report(o) {
    document.title = JSON.stringify(o);
    var el = document.getElementById('info');
    if (el) el.textContent = o.webgl2 ? ('WebGL2 ✓ ' + o.renderer) : 'WebGL2 ✗';
  }
  try {
    var gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) { report({ webgl2: false }); return; }
    var ext = gl.getExtension('WEBGL_debug_renderer_info');
    report({
      webgl2: true,
      renderer: (ext && gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) || gl.getParameter(gl.RENDERER),
      vendor: (ext && gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)) || gl.getParameter(gl.VENDOR),
      version: gl.getParameter(gl.VERSION)
    });
  } catch (e) {
    report({ webgl2: false, error: String(e) });
  }
})();
</script>
</body></html>
"""


# ---------- /proc 采样：整棵进程树（WebKit 会另起好几个进程）----------

def _ppid_map() -> dict[int, int]:
    out: dict[int, int] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        rest = data[data.rfind(b")") + 2:].split()
        if len(rest) > 1:
            out[int(name)] = int(rest[1])
    return out


def _tree(root: int) -> list[int]:
    parents = _ppid_map()
    kids: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        kids.setdefault(ppid, []).append(pid)
    seen: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        seen.append(pid)
        stack.extend(kids.get(pid, []))
    return seen


def _rss_mb(pids: list[int]) -> float:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
                        break
        except OSError:
            pass
    return total / 1024.0


def _ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        rest = data[data.rfind(b")") + 2:].split()
        if len(rest) > 12:
            total += int(rest[11]) + int(rest[12])  # utime + stime
    return total


# ---------- 窗口 ----------

def _active_window() -> str | None:
    """问 X 服务器「现在谁拥有键盘焦点」。

    只看 self.window.has_toplevel_focus() 是不够的：这个窗口设了 accept_focus=False，
    本来就不该拿到焦点，所以它返回 False 什么也证明不了。真正要证明的是
    「原来那个窗口一直是焦点」——所以得从外面问。
    xdotool 没有的话返回 None，那就只能靠人肉测试（见 _summary 的提示）。
    """
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


class Probe:
    def __init__(self, url: str | None, override: bool, click_through: bool,
                 width: int, height: int, seconds: float):
        self.url = url
        self.override = override
        self.click_through = click_through
        self.seconds = seconds
        self.webgl: dict | None = None
        self.started = time.monotonic()
        self.t0_ticks: int | None = None
        self.focus_hits = 0
        self.samples = 0
        self.focus_before: str | None = None
        self.focus_after: str | None = None
        self.focus_stolen = False

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        screen = Gdk.Screen.get_default()
        self.window.set_app_paintable(True)
        self.window.set_visual(screen.get_rgba_visual())
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.window.set_keep_above(True)
        self.window.set_size_request(width, height)

        self.webview = WebKit2.WebView()
        # 透明背景：不设这个默认是白的
        self.webview.set_background_color(Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=0.0))
        self.webview.connect("notify::title", self._on_title)
        self.window.add(self.webview)
        self.window.connect("draw", self._on_draw)

        self.window.realize()
        if override:
            handle = self.window.get_window()
            if handle is not None:
                handle.set_override_redirect(True)
        if click_through:
            self._make_click_through()
        self._place(width, height)
        self.window.show_all()

    def _on_draw(self, _widget, cr) -> bool:
        # 清成完全透明，才看得出桌面透不透过来
        if cairo is not None:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
        return False

    def _make_click_through(self) -> None:
        handle = self.window.get_window()
        if handle is None:
            return
        if cairo is None:
            print("  ! 没有 pycairo（python3-gi-cairo），跳过点击穿透设置")
            return
        try:
            handle.input_shape_combine_region(cairo.Region(), 0, 0)
        except Exception as exc:
            print(f"  ! 点击穿透设置失败：{exc}")

    def _place(self, width: int, height: int) -> None:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        x = geometry.x + (geometry.width - width) // 2
        y = geometry.y + geometry.height - height - 110
        self.window.move(max(0, x), max(0, y))

    def _on_title(self, view, _param) -> None:
        title = view.get_title()
        if not title or not title.startswith("{"):
            return
        try:
            self.webgl = json.loads(title)
        except ValueError:
            pass

    def start(self) -> None:
        if self.url:
            self.webview.load_uri(self.url)
        else:
            self.webview.load_html(PROBE_HTML, "file:///")
        self.focus_before = _active_window()
        self.t0_ticks = _ticks(_tree(os.getpid()))
        GLib.timeout_add(1000, self._sample)

    def _sample(self) -> bool:
        pids = _tree(os.getpid())
        elapsed = time.monotonic() - self.started
        rss = _rss_mb(pids)
        ticks = _ticks(pids)
        cpu = (ticks - (self.t0_ticks or ticks)) / CLK / max(elapsed, 0.001) * 100.0
        focused = self.window.has_toplevel_focus()
        self.focus_hits += 1 if focused else 0
        active = _active_window()
        if self.focus_before and active and active != self.focus_before:
            self.focus_stolen = True
        self.focus_after = active
        self.samples += 1
        print(
            f"  t={elapsed:5.1f}s  进程树 {len(pids):2d} 个  RSS {rss:7.1f} MB  "
            f"CPU {cpu:5.1f}%  焦点={'有(!)' if focused else '无'}"
            + (f"  活动窗口={'被换掉(!)' if self.focus_stolen else '没变'}" if self.focus_before else "")
        )
        if elapsed >= self.seconds:
            self._summary(rss, cpu)
            Gtk.main_quit()
            return False
        return True

    def _summary(self, rss: float, cpu: float) -> None:
        print("\n================ 结论 ================")
        if self.webgl is None:
            print("WebGL2  : 没收到回报（页面没加载完，或标题没更新）")
        elif self.webgl.get("webgl2"):
            renderer = str(self.webgl.get("renderer", "?"))
            software = any(k in renderer.lower() for k in ("llvmpipe", "softpipe", "swiftshader", "software"))
            print(f"WebGL2  : ✓ 有")
            print(f"  renderer = {renderer}")
            print(f"  vendor   = {self.webgl.get('vendor', '?')}")
            print(f"  version  = {self.webgl.get('version', '?')}")
            print(f"  → {'⚠ 软件渲染！常驻浮标上 CPU 会很难看' if software else '✓ 看起来是硬件加速'}")
        else:
            print(f"WebGL2  : ✗ 没有 —— metal-fx 会静默退化成普通子元素（{self.webgl.get('error', '')}）")
        if not self.focus_before:
            print("焦点    : 没装 xdotool，从外面问不了 —— 自己试：把光标放记事本里一直打字，")
            print("          全程字都应该进记事本；串到别处就是抢焦点了（绝不能接受）")
        elif self.focus_stolen:
            print(f"焦点    : ⚠ 活动窗口从 {self.focus_before} 变成了 {self.focus_after} —— 抢焦点了，绝不能接受")
        else:
            print(f"焦点    : ✓ 活动窗口全程还是 {self.focus_before}（没被换掉），也没抢到自己的焦点")
        print(f"内存    : 结束时进程树 RSS ≈ {rss:.0f} MB")
        print(f"CPU     : 全程均值 ≈ {cpu:.1f}%（这是整棵树的占用）")
        print("透明    : 这条得你自己看 —— 胶囊以外应该能看见桌面")
        print("=====================================")


def main() -> int:
    parser = argparse.ArgumentParser(description="WebKitGTK 可行性探针（只测不集成）")
    parser.add_argument("--url", help="要加载的地址；不给就用自带的最小体检页")
    parser.add_argument("--seconds", type=float, default=15.0, help="采样时长，默认 15 秒")
    parser.add_argument("--width", type=int, default=340)
    parser.add_argument("--height", type=int, default=60)
    parser.add_argument("--no-override", action="store_true", help="不设 override-redirect（做对照）")
    parser.add_argument("--no-click-through", action="store_true", help="不设点击穿透（做对照）")
    args = parser.parse_args()

    if not os.environ.get("DISPLAY"):
        print("没有 DISPLAY，探针要在有图形界面的目标机上跑", file=sys.stderr)
        return 1

    print(f"WebKitGTK 探针：{'URL ' + args.url if args.url else '自带体检页'}")
    print(f"  尺寸 {args.width}x{args.height}，override-redirect={not args.no_override}，"
          f"点击穿透={not args.no_click_through}")
    if args.url:
        print("  （量真实开销时建议先起 bridge.py；页面里的两个效果会一直动，等于最坏情况）")
    print()

    probe = Probe(
        url=args.url,
        override=not args.no_override,
        click_through=not args.no_click_through,
        width=args.width,
        height=args.height,
        seconds=args.seconds,
    )
    probe.start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

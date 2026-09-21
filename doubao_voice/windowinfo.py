"""判断"现在打字的目标窗口是不是终端"。

为什么需要它：终端的粘贴快捷键是 `Ctrl+Shift+V`，别的程序是 `Ctrl+V`；发错了字就进不去。
照 doubao-murmur 的思路，应该是"查焦点窗口是谁，命中终端白名单就用 Ctrl+Shift+V"，
而不是让用户自己去设置里切。

难点在"怎么查焦点窗口"。GNOME Wayland 下没有官方接口，本机实测的三条路：

1. **AT-SPI（无障碍总线）**——屏幕阅读器那一套，工作在工具包层，Wayland/X11 通吃。
   实测本机 GNOME 50 可用：注册表里有 15 个应用，能读到带 ACTIVE/FOCUSED 状态的窗口，
   当前焦点的终端（ptyxis）能被认出来。缺点：没开无障碍的应用查不到，一次遍历几百毫秒，
   所以按会话缓存，不在粘贴热路径上反复查。
2. **X11（`_NET_ACTIVE_WINDOW` + WM_CLASS）**——只在 X11 会话、或焦点窗口是 XWayland 程序时
   有效。实测 GNOME Wayland 下这个提示不跟着 Wayland 窗口走（拿到的窗口连 WM_CLASS 都没有）。
3. **问用户**——查不到就用手动配置，绝不瞎猜。
"""

from __future__ import annotations

import subprocess
import threading
import time

# 终端白名单：AT-SPI 给的是应用名（如 "ptyxis"、"org.gnome.Nautilus"），X11 给的是 WM_CLASS。
# 匹配规则见 looks_like_terminal()：整体小写后取"原串"和"最后一段点分名"两个候选。
TERMINAL_NAMES = {
    # GNOME 系
    "gnome-terminal", "gnome-terminal-server", "org.gnome.terminal", "terminal",
    "ptyxis", "org.gnome.ptyxis",          # Ubuntu 26.04 起的默认终端
    "kgx", "org.gnome.console", "console",  # GNOME Console
    "tilix", "io.elementary.terminal",
    # KDE 系
    "konsole", "org.kde.konsole", "yakuake", "org.kde.yakuake",
    # 常见第三方终端
    "alacritty", "kitty", "foot", "footclient", "wezterm", "org.wezfurlong.wezterm",
    "ghostty", "com.mitchellh.ghostty", "warp", "warp-terminal", "dev.warp.warp",
    "hyper", "terminator", "sakura", "roxterm", "guake", "tilda", "terminology",
    "cool-retro-term", "contour", "rio", "blackbox", "tabby", "tabby-terminal",
    "xfce4-terminal", "lxterminal", "deepin-terminal", "qterminal",
    # 老牌 / 极简
    "xterm", "urxvt", "rxvt", "rxvt-unicode", "st", "st-256color", "mlterm", "aterm",
}

# AT-SPI 里 gnome-shell 自己永远有个 FOCUSED 的 "Main stage"，不能当用户窗口
_SHELL_APPS = {"gnome-shell", "gnome-shell-calendar-server"}

_CACHE_TTL = 8.0
_cache_lock = threading.Lock()
_cache: tuple[float, list[str], list[str], bool | None] = (0.0, [], [], None)


def looks_like_terminal(name: str) -> bool:
    """应用名/WM class → 是不是终端。"""
    text = str(name or "").strip().lower()
    if not text:
        return False
    candidates = {text}
    if "." in text:
        candidates.add(text.rsplit(".", 1)[-1])
    return bool(candidates & TERMINAL_NAMES)


# ---------------------------------------------------------------- AT-SPI


def _atspi_focused_apps(timeout_ms: int = 1500) -> list[str]:
    """走一遍无障碍树，返回"当前带 ACTIVE/FOCUSED 状态的窗口"所属的应用名。"""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import GLib, Gio
    except (ImportError, ValueError):
        return []

    try:
        session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        address = session.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
            None, None, Gio.DBusCallFlags.NONE, timeout_ms, None,
        ).unpack()[0]
        conn = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
    except Exception:
        return []

    def call(dest, path, interface, method, params=None, timeout=timeout_ms):
        return conn.call_sync(
            dest, path, interface, method, params, None,
            Gio.DBusCallFlags.NONE, timeout, None,
        )

    def prop(dest, path, interface, name) -> str:
        try:
            value = call(dest, path, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (interface, name))).unpack()[0]
            return str(value.unpack() if hasattr(value, "unpack") else value)
        except Exception:
            return ""

    def state_bit(mask, bit: int) -> bool:
        words = list(mask)
        index = bit // 32
        return index < len(words) and bool(words[index] >> (bit % 32) & 1)

    STATE_ACTIVE, STATE_FOCUSED = 1, 12

    try:
        apps = call("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root",
                    "org.a11y.atspi.Accessible", "GetChildren").unpack()[0]
    except Exception:
        return []

    focused: list[str] = []
    active: list[str] = []
    for bus, path in apps:
        app_name = prop(bus, path, "org.a11y.atspi.Accessible", "Name")
        if not app_name or app_name in _SHELL_APPS:
            continue
        try:
            children = call(bus, path, "org.a11y.atspi.Accessible", "GetChildren",
                            None, timeout=600).unpack()[0]
        except Exception:
            continue
        for child_bus, child_path in children:
            try:
                state = call(child_bus, child_path, "org.a11y.atspi.Accessible", "GetState",
                             None, timeout=600).unpack()[0]
            except Exception:
                continue
            if state_bit(state, STATE_FOCUSED):
                focused.append(app_name)
            elif state_bit(state, STATE_ACTIVE):
                active.append(app_name)
    return focused, active


# ---------------------------------------------------------------- X11 兜底


def _x11_active_class() -> str:
    """X11 会话（或焦点是 XWayland 窗口）时的一手信息。"""
    import shutil

    xprop = shutil.which("xprop")
    if not xprop:
        return ""
    try:
        root = subprocess.run([xprop, "-root", "_NET_ACTIVE_WINDOW"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=2).stdout.decode("utf-8", "replace")
        window_id = ""
        for token in root.split("#")[-1].replace(",", " ").split():
            if token.startswith("0x"):
                window_id = token
                break
        if not window_id or window_id == "0x0":
            return ""
        out = subprocess.run([xprop, "-id", window_id, "WM_CLASS"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=2).stdout.decode("utf-8", "replace")
        # WM_CLASS(STRING) = "terminator", "Terminator"
        values = out.partition("=")[2]
        for part in values.split(","):
            name = part.strip().strip('"').lower()
            if name:
                return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


# ---------------------------------------------------------------- 对外


def focused_apps(refresh: bool = False) -> list[str]:
    """当前焦点窗口所属的应用名（FOCUSED 的排在 ACTIVE 前面）。"""
    focused, active = _candidates(refresh=refresh)
    return focused + active


def _candidates(refresh: bool = False) -> tuple[list[str], list[str]]:
    """(FOCUSED 的应用, ACTIVE 的应用)。查不到就是两个空表。"""
    global _cache
    with _cache_lock:
        stamp, focused, active, _verdict = _cache
        if not refresh and (focused or active) and time.monotonic() - stamp < _CACHE_TTL:
            return list(focused), list(active)

    focused, active = _atspi_focused_apps()
    if not focused and not active:
        x11 = _x11_active_class()
        if x11:
            active = [x11]

    verdict = _decide(focused, active)
    with _cache_lock:
        _cache = (time.monotonic(), list(focused), list(active), verdict)
    return focused, active


def _decide(focused: list[str], active: list[str]) -> bool | None:
    """FOCUSED 更精确，有就只信它；否则看 ACTIVE 里有没有终端。

    实测有些程序（比如 Chrome）即使没焦点也一直报 ACTIVE，所以不能简单地把
    ACTIVE 全当成"当前焦点"；但没有更好的信号时，宁可回答"是终端"——
    因为发错快捷键的代价不对称：终端里发 Ctrl+V 什么都粘不进去，
    而多数非终端程序收到 Ctrl+Shift+V 也只是"粘贴为纯文本"。
    """
    if focused:
        return any(looks_like_terminal(app) for app in focused)
    if active:
        return any(looks_like_terminal(app) for app in active)
    return None


def focused_is_terminal(refresh: bool = False) -> bool | None:
    """焦点窗口是不是终端。None 表示查不出来（调用方该回退到手动配置）。"""
    focused, active = _candidates(refresh=refresh)
    return _decide(focused, active)


def describe() -> str:
    """给 --self-test 用的一行诊断。"""
    focused, active = _candidates(refresh=True)
    if not focused and not active:
        return "查不到焦点窗口（AT-SPI 没结果、X11 也没有提示）"
    verdict = _decide(focused, active)
    tag = "终端" if verdict else ("不是终端" if verdict is False else "未知")
    parts = []
    if focused:
        parts.append("FOCUSED=" + "/".join(focused))
    if active:
        parts.append("ACTIVE=" + "/".join(active))
    return f"{tag}（{'；'.join(parts)}）"

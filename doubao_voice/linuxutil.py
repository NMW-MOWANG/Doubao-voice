"""Linux 平台层：剪贴板、按键注入、设备与权限检查。

底层机制和 Windows 版（SendInput + 剪贴板 API）完全不同：

* **按键注入**走 `/dev/uinput`（内核级虚拟键盘）。这是 Wayland 下唯一能往"当前
  焦点窗口"打字的办法——XTEST / xdotool 的注入只对 XWayland 窗口有效，原生
  Wayland 窗口收不到。
* **剪贴板**走 `wl-clipboard`（wl-copy / wl-paste）。实测本机 GNOME Wayland 下
  GTK 自带的剪贴板 API 对没有窗口的后台程序不生效（wl-paste 读不到 GTK 设的
  内容），而 wl-copy 作为纯 Wayland 客户端可以正常发布选区。

两者都不需要额外的 Python 包，但需要一次性放开设备权限（见 setup_linux.sh）。
"""

from __future__ import annotations

import array
import fcntl
import os
import struct
import subprocess
import threading
import time

UINPUT_PATH = "/dev/uinput"
KEYBOARD_NAME = "Doubao Voice Virtual Keyboard"

# ---------------------------------------------------------------- ioctl 编号


def _ioc(direction: int, type_: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (type_ << 8) | number


_UINPUT_BASE = ord("U")
UI_DEV_CREATE = _ioc(0, _UINPUT_BASE, 1, 0)
UI_DEV_DESTROY = _ioc(0, _UINPUT_BASE, 2, 0)
UI_SET_EVBIT = _ioc(1, _UINPUT_BASE, 100, 4)
UI_SET_KEYBIT = _ioc(1, _UINPUT_BASE, 101, 4)
UI_DEV_SETUP = _ioc(1, _UINPUT_BASE, 3, 92)  # struct uinput_setup：8 + 80 + 4

EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0

KEY_ESC = 1
KEY_1, KEY_0 = 2, 11
KEY_MINUS, KEY_EQUAL = 12, 13
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_Q, KEY_P, KEY_A, KEY_L, KEY_Z, KEY_M = 16, 25, 30, 38, 44, 50
KEY_LEFTBRACE, KEY_RIGHTBRACE = 26, 27
KEY_ENTER = 28
KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_SEMICOLON, KEY_APOSTROPHE, KEY_GRAVE = 39, 40, 41
KEY_LEFTSHIFT, KEY_BACKSLASH = 42, 43
KEY_RIGHTSHIFT = 54
KEY_COMMA, KEY_DOT, KEY_SLASH = 51, 52, 53
KEY_SPACE = 57
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126

# 发粘贴前要检查/抬起的修饰键（左右各一套）
_MODIFIER_KEY_CODES = (29, 97, 56, 100, 42, 54, 125, 126)

_UNSHIFTED = {
    "1": KEY_1, "2": KEY_1 + 1, "3": KEY_1 + 2, "4": KEY_1 + 3, "5": KEY_1 + 4,
    "6": KEY_1 + 5, "7": KEY_1 + 6, "8": KEY_1 + 7, "9": KEY_1 + 8, "0": KEY_0,
    "-": KEY_MINUS, "=": KEY_EQUAL,
    "q": KEY_Q, "w": KEY_Q + 1, "e": KEY_Q + 2, "r": KEY_Q + 3, "t": KEY_Q + 4,
    "y": KEY_Q + 5, "u": KEY_Q + 6, "i": KEY_Q + 7, "o": KEY_Q + 8, "p": KEY_Q + 9,
    "[": KEY_LEFTBRACE, "]": KEY_RIGHTBRACE,
    "a": KEY_A, "s": KEY_A + 1, "d": KEY_A + 2, "f": KEY_A + 3, "g": KEY_A + 4,
    "h": KEY_A + 5, "j": KEY_A + 6, "k": KEY_A + 7, "l": KEY_A + 8,
    ";": KEY_SEMICOLON, "'": KEY_APOSTROPHE, "`": KEY_GRAVE, "\\": KEY_BACKSLASH,
    "z": KEY_Z, "x": KEY_Z + 1, "c": KEY_Z + 2, "v": KEY_Z + 3, "b": KEY_Z + 4,
    "n": KEY_Z + 5, "m": KEY_Z + 6,
    ",": KEY_COMMA, ".": KEY_DOT, "/": KEY_SLASH,
    " ": KEY_SPACE, "\n": KEY_ENTER, "\t": KEY_TAB,
}

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=",
    "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`",
    "<": ",", ">": ".", "?": "/",
}

# 虚拟键盘声明自己有哪些按键。内核只转发"能力位图里有"的按键（没声明的键码会被
# 直接丢掉——第一版漏了退格 14，表现就是"退格完全没反应"），所以这里干脆把常用范围
# 全声明上，省得以后加了按键又踩同一个坑。
KEYBITS = list(range(1, 256))

_EVENT = struct.Struct("llHHi")  # struct input_event（64 位）
_SETUP = struct.Struct("HHHH80sI")  # struct uinput_setup


def can_type(text: str) -> bool:
    """这段文本能不能用键盘直接敲出来（纯 ASCII 布局内字符）。中文得走剪贴板。"""
    for char in text:
        if char in _UNSHIFTED or char in _SHIFTED:
            continue
        if char.isascii() and char.isalpha():
            continue
        if char.isascii() and char.isupper():
            continue
        return False
    return True


class KeyInjector:
    """uinput 虚拟键盘：退格、Ctrl+V、以及纯 ASCII 文本的逐字键入。"""

    def __init__(self) -> None:
        self.error: str | None = None
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._modifier_source = None
        self.release_stuck = True

    def set_modifier_source(self, source) -> None:
        """注册"现在用户还按着哪些修饰键"的来源（键盘钩子的 held_modifier_codes）。"""
        self._modifier_source = source

    def set_release_stuck(self, value: bool) -> None:
        self.release_stuck = bool(value)

    def release_modifiers(self) -> int:
        """把用户还按着的修饰键先抬起来。

        不这么做的话：热键绑在 Alt / Shift / Super 上时，我们合成的 Ctrl+V 会变成
        Ctrl+Alt+V（或 Ctrl+Shift+V），目标程序要么当快捷键吃掉、要么干脆不粘贴——
        实测就是这么丢字的。Windows 版的原项目同样处理（doubao-murmur 的
        release_stuck_modifiers，用 GetAsyncKeyState 查、再补 key-up）。
        """
        if self._fd is None or not self.release_stuck or self._modifier_source is None:
            return 0
        try:
            held = set(self._modifier_source() or ())
        except Exception:
            return 0
        held &= set(_MODIFIER_KEY_CODES)
        for code in sorted(held):
            self._emit(code, 0)
        return len(held)

    # ---------- 设备 ----------

    @property
    def available(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        with self._lock:
            if self._fd is not None:
                return
            if not os.path.exists(UINPUT_PATH):
                raise RuntimeError(f"找不到 {UINPUT_PATH}，内核没编 uinput 模块")
            if not os.access(UINPUT_PATH, os.W_OK):
                raise RuntimeError(
                    f"没有 {UINPUT_PATH} 的写权限。请运行一次：sudo bash setup_linux.sh"
                )
            fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
            try:
                # 注意：内核的 UI_SET_EVBIT / UI_SET_KEYBIT 收的是"值本身"而不是指针
                # （传 buffer 会被当成一个巨大的位号，内核直接返回 EINVAL）。
                for event_type in (EV_KEY, EV_SYN):
                    fcntl.ioctl(fd, UI_SET_EVBIT, int(event_type))
                for code in KEYBITS:
                    fcntl.ioctl(fd, UI_SET_KEYBIT, int(code))
                name = KEYBOARD_NAME.encode()[:79]
                setup = _SETUP.pack(0x03, 0x444F, 0x5642, 1, name, 0)  # BUS_USB / "DO" "VB"
                fcntl.ioctl(fd, UI_DEV_SETUP, setup)
                fcntl.ioctl(fd, UI_DEV_CREATE)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (13, 1):
                    raise RuntimeError(
                        f"无法创建虚拟键盘（{exc.strerror}）。请运行一次：sudo bash setup_linux.sh"
                    ) from exc
                raise RuntimeError(f"创建虚拟键盘失败：{exc}") from exc
            self._fd = fd
            time.sleep(0.25)  # 等内核把设备注册好，太快发送会丢事件

    def close(self) -> None:
        with self._lock:
            if self._fd is None:
                return
            try:
                fcntl.ioctl(self._fd, UI_DEV_DESTROY)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # ---------- 事件 ----------

    def _emit(self, code: int, value: int) -> None:
        assert self._fd is not None
        os.write(self._fd, _EVENT.pack(0, 0, EV_KEY, code, value))
        os.write(self._fd, _EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))

    def tap(self, code: int, shift: bool = False, delay: float = 0.004) -> None:
        if self._fd is None:
            raise RuntimeError("虚拟键盘没有打开")
        if shift:
            self._emit(KEY_LEFTSHIFT, 1)
        self._emit(code, 1)
        self._emit(code, 0)
        if shift:
            self._emit(KEY_LEFTSHIFT, 0)
        if delay:
            time.sleep(delay)

    def backspaces(self, count: int) -> None:
        if count <= 0:
            return
        if self._fd is None:
            raise RuntimeError("虚拟键盘没有打开")
        for _ in range(min(count, 200)):
            self._emit(KEY_BACKSPACE, 1)
            self._emit(KEY_BACKSPACE, 0)

    def paste(self, shift: bool = False) -> None:
        """按 Ctrl+V（终端类窗口用 Ctrl+Shift+V）。发之前先把用户按着的修饰键抬起来。"""
        if self._fd is None:
            raise RuntimeError("虚拟键盘没有打开")
        self.release_modifiers()
        key_v = KEY_Z + 3  # KEY_V
        self._emit(KEY_LEFTCTRL, 1)
        if shift:
            self._emit(KEY_LEFTSHIFT, 1)
        self._emit(key_v, 1)
        self._emit(key_v, 0)
        if shift:
            self._emit(KEY_LEFTSHIFT, 0)
        self._emit(KEY_LEFTCTRL, 0)

    def ctrl_v(self) -> None:
        self.paste()

    def type_text(self, text: str) -> None:
        """逐字键入；只支持 ASCII 布局内的字符，其余请走剪贴板。"""
        if self._fd is None:
            raise RuntimeError("虚拟键盘没有打开")
        for char in text:
            if char in _SHIFTED:
                self.tap(_UNSHIFTED[_SHIFTED[char]], shift=True)
            elif char in _UNSHIFTED:
                self.tap(_UNSHIFTED[char])
            elif char.isascii() and char.isalpha():
                self.tap(_UNSHIFTED[char.lower()], shift=char.isupper())
            else:
                raise RuntimeError(f"没法用键盘直接打出这个字符：{char!r}")


# ---------------------------------------------------------------- 剪贴板


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def clipboard_tools() -> tuple[str | None, str | None]:
    return _which("wl-copy"), _which("wl-paste")


def set_clipboard_text(text: str, wait: bool = True) -> None:
    """把文本放进系统剪贴板（wl-copy 会 fork 一个进程持有选区）。

    wl-copy 的父进程在"选区真正交接完成"之前就退出了，紧接着按 Ctrl+V 有可能粘到
    空内容（实测偶发），所以默认会回读一次确认就位再返回。
    """
    wl_copy, wl_paste = clipboard_tools()
    if not wl_copy:
        raise RuntimeError("缺少 wl-copy，请先安装：sudo apt install wl-clipboard")
    done = subprocess.run(
        [wl_copy, "--type", "text/plain;charset=utf-8"],
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        # 必须也丢掉 stderr：wl-copy 会 fork 一个后台进程持有选区，它继承了管道的话
        # subprocess 会一直等到那个后台进程退出（表现为"卡住直到超时"）。
        stderr=subprocess.DEVNULL,
        timeout=6,
    )
    if done.returncode != 0:
        raise RuntimeError(f"写入剪贴板失败（wl-copy 退出码 {done.returncode}）")
    if not wait or not wl_paste:
        return
    # 先直接查一次再退避重试：wl-copy 返回时（约 50ms，等焦点那段时间）内容通常已经就位，
    # 一上来就睡 20ms 是白等。
    for attempt in range(15):
        if attempt:
            time.sleep(0.02)
        if get_clipboard_text() == text:
            return


def get_clipboard_text(timeout: float = 1.0) -> str:
    """读剪贴板；读不到（空的或不是文本）返回空串。"""
    _, wl_paste = clipboard_tools()
    if not wl_paste:
        return ""
    try:
        done = subprocess.run(
            [wl_paste, "--no-newline"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- 会话与权限


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() or "unknown"


def missing_permissions() -> list[str]:
    """列出还缺哪些设备权限，供启动时提示和 --self-test 使用。"""
    problems: list[str] = []
    if not os.path.exists(UINPUT_PATH):
        problems.append(f"{UINPUT_PATH} 不存在（内核没编 uinput）")
    elif not os.access(UINPUT_PATH, os.W_OK):
        problems.append(f"{UINPUT_PATH} 不可写（按键注入会用不了）")
    if not can_read_keyboard():
        problems.append("/dev/input/event* 不可读（全局热键会用不了）")
    wl_copy, wl_paste = clipboard_tools()
    if not wl_copy or not wl_paste:
        problems.append("没装 wl-clipboard（剪贴板会用不了）：sudo apt install wl-clipboard")
    return problems


def can_read_keyboard() -> bool:
    from .keyhook import keyboard_devices

    for path in keyboard_devices():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        os.close(fd)
        return True
    return False


def setup_hint() -> str:
    return "运行一次 sudo bash setup_linux.sh 放开设备权限，然后重新启动本程序。"


def notify(summary: str, body: str = "", timeout_ms: int = 4000) -> None:
    """发一条桌面通知（不抢焦点，识别结果和错误提示都可以走它）。"""
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib, Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                ("豆包语音输入", 0, "", summary, body, [], {}, int(timeout_ms)),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
    except Exception:
        pass  # 通知发不出去不影响主流程

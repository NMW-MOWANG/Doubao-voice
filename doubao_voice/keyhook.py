"""全局键盘钩子（Linux / evdev）。

Windows 版用的是 WH_KEYBOARD_LL，这里没有等价的用户态接口，只能直接读
`/dev/input/event*`。好处是同一条路子覆盖 X11 和 Wayland：

* 能拿到「按键抬起」事件，所以可以按住说话；
* 能拿到任意按键（包括多媒体键、部分键盘上报的 KEY_FN）；
* 只读设备、不独占，按键照常传给其他程序（和 Windows 版的"旁听"一致）。

代价是需要读 `/dev/input/event*` 的权限（setup_linux.sh 里用 udev 的 uaccess
标签放开，不需要把用户加进 input 组，也不用重新登录）。
"""

from __future__ import annotations

import fcntl
import glob
import os
import select
import struct
import threading
import time

from .linuxutil import KEYBOARD_NAME, setup_hint

MOD_CONTROL = 0x0001
MOD_ALT = 0x0002
MOD_SHIFT = 0x0004
MOD_SUPER = 0x0008

EV_KEY = 0x01
_EVENT = struct.Struct("llHHi")  # struct input_event，64 位下 24 字节

# EVIOCGKEY(len)：读"内核里当前按着哪些键"，等价于 Windows 的 GetAsyncKeyState
KEY_BITMAP_BYTES = 96  # (KEY_MAX(0x2ff) + 8) / 8
_EVIOCGKEY = (2 << 30) | (KEY_BITMAP_BYTES << 16) | (ord("E") << 8) | 0x18

_MODIFIER_CODES = {
    29: MOD_CONTROL,   # KEY_LEFTCTRL
    97: MOD_CONTROL,   # KEY_RIGHTCTRL
    56: MOD_ALT,       # KEY_LEFTALT
    100: MOD_ALT,      # KEY_RIGHTALT
    42: MOD_SHIFT,     # KEY_LEFTSHIFT
    54: MOD_SHIFT,     # KEY_RIGHTSHIFT
    125: MOD_SUPER,    # KEY_LEFTMETA
    126: MOD_SUPER,    # KEY_RIGHTMETA
}

KEY_NAMES = {
    1: "Esc", 14: "Backspace", 15: "Tab", 28: "Enter", 29: "左Ctrl", 42: "左Shift",
    54: "右Shift", 55: "小键盘*", 56: "左Alt", 57: "空格", 58: "CapsLock",
    69: "NumLock", 70: "ScrollLock", 97: "右Ctrl", 98: "小键盘/", 99: "PrintScreen",
    100: "右Alt", 102: "Home", 103: "↑", 104: "PageUp", 105: "←", 106: "→",
    107: "End", 108: "↓", 109: "PageDown", 110: "Insert", 111: "Delete",
    113: "静音", 114: "音量-", 115: "音量+", 116: "电源", 119: "Pause", 121: "小键盘,",
    122: "小键盘-", 123: "小键盘+", 125: "左Super", 126: "右Super", 127: "菜单键",
    128: "停止", 138: "帮助", 142: "睡眠", 143: "唤醒", 150: "上网", 152: "锁屏",
    155: "邮件", 158: "后退", 159: "前进", 163: "下一曲", 164: "播放/暂停",
    165: "上一曲", 166: "停止播放", 172: "主页", 173: "刷新", 183: "F13", 184: "F14",
    185: "F15", 186: "F16", 187: "F17", 188: "F18", 189: "F19", 190: "F20",
    191: "F21", 192: "F22", 193: "F23", 194: "F24", 224: "亮度-", 225: "亮度+",
    226: "媒体选择", 227: "切换显示", 228: "键盘背光", 229: "休眠", 238: "无线开关",
    240: "未知", 248: "麦克风静音", 464: "Fn", 530: "触摸板开关",
}
KEY_NAMES.update({2 + index: "1234567890"[index] for index in range(10)})
KEY_NAMES.update({12: "-", 13: "=", 26: "[", 27: "]", 39: ";", 40: "'", 41: "`",
                  43: "\\", 51: ",", 52: ".", 53: "/"})
KEY_NAMES.update({16 + index: "qwertyuiop"[index].upper() for index in range(10)})
KEY_NAMES.update({30 + index: "asdfghjkl"[index].upper() for index in range(9)})
KEY_NAMES.update({44 + index: "zxcvbnm"[index].upper() for index in range(7)})
KEY_NAMES.update({59 + index: f"F{index + 1}" for index in range(10)})
KEY_NAMES.update({87: "F11", 88: "F12"})

# 旧版 Windows 配置里的 vk 码 → evdev 码，用来兼容从旧配置沿用过来的按键设置
_LEGACY_VK = {
    0x08: 14, 0x09: 15, 0x0D: 28, 0x13: 119, 0x14: 58, 0x1B: 1, 0x20: 57,
    0x21: 104, 0x22: 109, 0x23: 107, 0x24: 102, 0x25: 105, 0x26: 103, 0x27: 106,
    0x28: 108, 0x2C: 99, 0x2D: 110, 0x2E: 111, 0x5B: 125, 0x5C: 126, 0x5D: 127,
    0xA0: 42, 0xA1: 54, 0xA2: 29, 0xA3: 97, 0xA4: 56, 0xA5: 100,
    0xBA: 39, 0xBB: 13, 0xBC: 51, 0xBD: 12, 0xBE: 52, 0xBF: 53, 0xC0: 41,
    0xDB: 26, 0xDC: 43, 0xDD: 27, 0xDE: 40,
}
_LEGACY_LETTERS = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
}


def _legacy_vk_to_code(vk: int) -> int:
    if vk in _LEGACY_VK:
        return _LEGACY_VK[vk]
    if 0x41 <= vk <= 0x5A:
        return _LEGACY_LETTERS.get(chr(vk + 32).lower(), 0)
    if 0x30 <= vk <= 0x39:
        return 2 + (vk - 0x30 if vk != 0x30 else 9)
    if 0x70 <= vk <= 0x7B:
        index = vk - 0x70
        return [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88][index]
    return 0


# ---------------------------------------------------------------- 按键规格


def format_spec(mods: int, code: int) -> str:
    return f"{int(mods)}:{int(code)}"


def parse_spec(text: str) -> tuple[int, int]:
    """把配置里的按键规格解析成 (修饰键, evdev 码)。兼容旧版三段式。"""
    parts = str(text or "").split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return (0, 0)
    if len(numbers) == 2:
        return (numbers[0], numbers[1])
    if len(numbers) == 3:  # 旧 Windows 配置：修饰键:虚拟键码:扫描码
        return (numbers[0], _legacy_vk_to_code(numbers[1]))
    return (0, 0)


def key_label(mods: int, code: int) -> str:
    if not mods and not code:
        return "未设置"
    name = KEY_NAMES.get(code, f"未知键(code={code})")
    prefix = []
    if mods & MOD_CONTROL:
        prefix.append("Ctrl")
    if mods & MOD_ALT:
        prefix.append("Alt")
    if mods & MOD_SHIFT:
        prefix.append("Shift")
    if mods & MOD_SUPER:
        prefix.append("Super")
    return "+".join(prefix + [name])


# ---------------------------------------------------------------- 设备发现


def _device_name(event_path: str) -> str:
    node = os.path.basename(event_path)  # event4
    try:
        with open(f"/sys/class/input/{node}/device/name", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _capability_bitmap(event_path: str) -> list[int]:
    node = os.path.basename(event_path)
    try:
        with open(f"/sys/class/input/{node}/device/capabilities/key", encoding="utf-8") as handle:
            return [int(word, 16) for word in handle.read().split()]
    except (OSError, ValueError):
        return []


def _bit_is_set(words: list[int], bit: int) -> bool:
    index = len(words) - 1 - bit // 64  # 内核按"高位字在前"打印
    if index < 0:
        return False
    return bool(words[index] >> (bit % 64) & 1)


def looks_like_keyboard(event_path: str) -> bool:
    """能打出字母的才算键盘，滤掉电源键、视频总线这些只有 kbd 句柄的假货。"""
    words = _capability_bitmap(event_path)
    if not words:
        return True  # 读不到能力位就宁可多收，反正读了没坏处
    return _bit_is_set(words, 30) and _bit_is_set(words, 44)  # KEY_A / KEY_Z


def keyboard_devices() -> list[str]:
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if _device_name(path) == KEYBOARD_NAME:
            continue  # 跳过本程序自己注入用的虚拟键盘，否则会自己触发自己
        if looks_like_keyboard(path):
            devices.append(path)
    return devices


# ---------------------------------------------------------------- 钩子线程


class KeyboardHook(threading.Thread):
    """全局键盘钩子线程。

    事件（投递到 out_queue）：
        ("key", "hold", "down"/"up")
        ("key", "toggle", "down")
        ("keycap", mods, code)      捕获模式下捕获到的按键

    share_keys 在 Linux 下没有副作用可开关：内核这一层只能"只读旁听"或者整台
    键盘独占（EVIOCGRAB），后者会把整个键盘从系统里拿走，显然不能这么做。
    所以这个参数保留只为兼容配置，实际始终是旁听。
    """

    def __init__(self, out_queue, share_keys: bool = True):
        super().__init__(daemon=True, name="keyhook")
        self.queue = out_queue
        self.share_keys = bool(share_keys)
        self.capture_swallows = True
        self.error: str | None = None
        self.devices: list[str] = []
        self._bindings: dict[str, tuple[int, int]] = {}
        self._held: set[int] = set()
        self._mods = 0
        self._capturing = False
        self._stopping = False
        self._ready = threading.Event()
        self._fds: dict[int, str] = {}
        self._pending: dict[int, bytes] = {}

    # ---------- 外部接口 ----------

    def set_bindings(self, bindings: dict[str, tuple[int, int]]) -> None:
        self._bindings = dict(bindings)

    def set_share_keys(self, value: bool) -> None:
        self.share_keys = bool(value)

    def begin_capture(self) -> None:
        self._capturing = True

    def cancel_capture(self) -> None:
        self._capturing = False

    def wait_ready(self, timeout: float = 1.5) -> None:
        self._ready.wait(timeout)

    def stop(self) -> None:
        self._stopping = True

    def held_modifier_codes(self) -> set[int]:
        """现在还按着的修饰键（重按物理键盘上看，不看我们注入的）。

        EVIOCGKEY 是内核给的按键位图，和 Windows 的 GetAsyncKeyState 一个意思：
        发粘贴前用它判断"用户是不是还按着 Alt 之类"，先把那个键抬起来。
        """
        held: set[int] = set()
        if not self._fds:
            return held
        buffer = bytearray(KEY_BITMAP_BYTES)
        for fd in list(self._fds):
            try:
                fcntl.ioctl(fd, _EVIOCGKEY, buffer)
            except OSError:
                continue
            for code in _MODIFIER_CODES:
                if buffer[code // 8] >> (code % 8) & 1:
                    held.add(code)
        return held

    # ---------- 线程 ----------

    def run(self) -> None:
        if not os.path.isdir("/dev/input"):
            self.error = "没有 /dev/input，这台机器上装不了全局热键"
        self._scan()  # 先扫一遍再报"就绪"，这样调用方读 error/devices 时结果一定是真的
        self._ready.set()
        next_scan = time.monotonic() + 2.0
        while not self._stopping:
            now = time.monotonic()
            if now >= next_scan:
                self._scan()
                next_scan = now + 2.0
            if not self._fds:
                time.sleep(0.2)
                continue
            try:
                readable, _, _ = select.select(list(self._fds), [], [], 0.35)
            except (OSError, ValueError):
                self._close_all()
                continue
            for fd in readable:
                self._drain(fd)
        self._close_all()

    def _scan(self) -> None:
        """开新插上的键盘、放下拔掉的，并记录权限问题。"""
        paths = keyboard_devices()
        opened = {path: fd for fd, path in self._fds.items()}
        for path, fd in list(opened.items()):
            if path not in paths:  # 拔掉了
                self._forget(fd)
        for path in paths:
            if path in opened:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError:
                if not self.error:
                    self.error = f"没有读键盘设备的权限，全局热键用不了。{setup_hint()}"
                continue
            except OSError:
                continue
            self._fds[fd] = path
            self._pending[fd] = b""
        self.devices = list(self._fds.values())
        if not self._fds and not self.error:
            self.error = "没找到可读的键盘设备，全局热键用不了"

    def _close_all(self) -> None:
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        self._pending.clear()

    def _drain(self, fd: int) -> None:
        try:
            data = os.read(fd, _EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            self._forget(fd)
            return
        if not data:
            self._forget(fd)
            return
        buffer = self._pending.get(fd, b"") + data
        complete = len(buffer) - len(buffer) % _EVENT.size
        self._pending[fd] = buffer[complete:]
        for offset in range(0, complete, _EVENT.size):
            _, _, event_type, code, value = _EVENT.unpack_from(buffer, offset)
            if event_type == EV_KEY:
                self._handle(code, value)

    def _forget(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass
        self._fds.pop(fd, None)
        self._pending.pop(fd, None)
        self.devices = list(self._fds.values())

    # ---------- 事件处理 ----------

    def _handle(self, code: int, value: int) -> None:
        down = value == 1
        modifier = _MODIFIER_CODES.get(code, 0)
        if modifier:
            if down:
                self._mods |= modifier
            elif value == 0:
                self._mods &= ~modifier

        if self._capturing and down:
            self._capturing = False
            self.queue.put(("keycap", self._mods & ~modifier, code))
            return

        if value == 2:  # 按住不放的自动重复，不重复触发（也拦不住，只能不理）
            return

        for name, (mods, bind_code) in self._bindings.items():
            if not bind_code or bind_code != code:
                continue
            if mods and (self._mods & mods) != mods:
                continue
            if name == "hold":
                if down:
                    if code in self._held:
                        return
                    self._held.add(code)
                    self.queue.put(("key", "hold", "down"))
                else:
                    self._held.discard(code)
                    self.queue.put(("key", "hold", "up"))
                return
            if down:
                if code in self._held:
                    return
                self._held.add(code)
                self.queue.put(("key", "toggle", "down"))
            else:
                self._held.discard(code)
            return

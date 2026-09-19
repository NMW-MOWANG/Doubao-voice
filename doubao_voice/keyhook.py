"""底层键盘钩子（WH_KEYBOARD_LL）。

比 RegisterHotKey 强的地方：
* 能拿到「按键抬起」事件，所以可以实现按住说话；
* 可以捕获任意按键，包括系统不认的组合（某些键盘的 Fn 键）。

注意：钩子回调运行在系统输入路径上，必须极快返回；这里只做查表 + 投队列。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from .winutil import INJECT_TAG, MSG, WM_QUIT

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 只忽略本程序自己注入的按键（带 INJECT_TAG 标记），避免"打字触发录音"的回路。
# 别的软件注入的按键（AutoHotkey、键盘驱动软件等）照常识别——把 Fn 映射成
# 别的键的绕法就是靠这个才能生效。测试时可临时置 False。
IGNORE_INJECTED = True

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x00000010

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

_MODIFIER_VKS = {
    0x10: MOD_SHIFT,
    0x11: MOD_CONTROL,
    0x12: MOD_ALT,
    0xA0: MOD_SHIFT,
    0xA1: MOD_SHIFT,
    0xA2: MOD_CONTROL,
    0xA3: MOD_CONTROL,
    0xA4: MOD_ALT,
    0xA5: MOD_ALT,
}

KEY_NAMES = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x13: "Pause",
    0x14: "CapsLock",
    0x1B: "Esc",
    0x20: "空格",
    0x21: "PageUp",
    0x22: "PageDown",
    0x23: "End",
    0x24: "Home",
    0x25: "←",
    0x26: "↑",
    0x27: "→",
    0x28: "↓",
    0x2C: "PrintScreen",
    0x2D: "Insert",
    0x2E: "Delete",
    0x5B: "Win",
    0x5C: "Win",
    0x5D: "Menu",
    0xA0: "左Shift",
    0xA1: "右Shift",
    0xA2: "左Ctrl",
    0xA3: "右Ctrl",
    0xA4: "左Alt",
    0xA5: "右Alt",
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
user32.CallNextHookEx.restype = ctypes.c_ssize_t


def format_spec(mods: int, vk: int, scan: int) -> str:
    return f"{int(mods)}:{int(vk)}:{int(scan)}"


def parse_spec(text: str) -> tuple[int, int, int]:
    parts = str(text or "").split(":")
    if len(parts) != 3:
        return (0, 0, 0)
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (0, 0, 0)


def key_label(mods: int, vk: int, scan: int) -> str:
    if not vk and not scan:
        return "未设置"
    name = KEY_NAMES.get(vk)
    if name is None:
        if 0x70 <= vk <= 0x87:
            name = f"F{vk - 0x6F}"
        elif 0x41 <= vk <= 0x5A:
            name = chr(vk)
        elif 0x30 <= vk <= 0x39:
            name = chr(vk)
        else:
            name = f"未知键(vk=0x{vk:02X} scan=0x{scan:02X})"
    prefix = []
    if mods & MOD_CONTROL:
        prefix.append("Ctrl")
    if mods & MOD_ALT:
        prefix.append("Alt")
    if mods & MOD_SHIFT:
        prefix.append("Shift")
    return "+".join(prefix + [name])


class KeyboardHook(threading.Thread):
    """全局键盘钩子线程。

    事件（投递到 out_queue）：
        ("key", "hold", "down"/"up")
        ("key", "toggle", "down")
        ("keycap", mods, vk, scan)   捕获模式下捕获到的按键

    share_keys 为 True 时只"旁听"按键，事件照旧放行给其他程序；
    为 False 时把按键吃掉，其他程序收不到（老式热键的行为）。
    """

    def __init__(self, out_queue, share_keys: bool = True):
        super().__init__(daemon=True, name="keyhook")
        self.queue = out_queue
        self.share_keys = share_keys
        self.capture_swallows = True  # 监视模式下设 False，按键照常传给其他程序
        self.error: str | None = None
        self._bindings: dict[str, tuple[int, int, int]] = {}
        self._held: set[int] = set()
        self._mods = 0
        self._capturing = False
        self._captured_vk: int | None = None
        self._thread_id: int | None = None
        self._hook = None
        self._ready = threading.Event()
        self._proc = HOOKPROC(self._callback)  # 必须保住引用，否则回调被回收

    # ---------- 外部接口 ----------

    def set_bindings(self, bindings: dict[str, tuple[int, int, int]]) -> None:
        self._bindings = dict(bindings)  # 整体替换，回调里读的是快照

    def set_share_keys(self, value: bool) -> None:
        self.share_keys = bool(value)

    def begin_capture(self) -> None:
        self._capturing = True

    def cancel_capture(self) -> None:
        self._capturing = False

    def wait_ready(self, timeout: float = 1.5) -> None:
        self._ready.wait(timeout)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # ---------- 钩子线程 ----------

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self.error = f"键盘钩子安装失败（错误 {ctypes.get_last_error()}），长按说话可能不可用"
        self._ready.set()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _callback(self, n_code, w_param, l_param):
        if n_code != 0:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = int(info.vkCode)
        scan = int(info.scanCode)
        injected = bool(info.flags & LLKHF_INJECTED)
        tagged = int(info.dwExtraInfo or 0) == INJECT_TAG
        if IGNORE_INJECTED and injected and tagged:
            # 只跳过自己注入的按键，别人注入的照常处理
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
        modifier = _MODIFIER_VKS.get(vk, 0)
        if modifier:
            if down:
                self._mods |= modifier
            else:
                self._mods &= ~modifier

        if self._captured_vk is not None:
            if not down and vk == self._captured_vk:
                self._captured_vk = None
                return 1
            self._captured_vk = None

        if self._capturing and down:
            self._capturing = False
            self.queue.put(("keycap", self._mods, vk, scan))
            if not self.capture_swallows:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            self._captured_vk = vk
            return 1

        for name, (mods, bind_vk, bind_scan) in self._bindings.items():
            if not bind_vk and not bind_scan:
                continue
            # vk 为 0 或 0xFF 的键（Fn、多媒体键等）上报值不可靠，必须连扫描码一起比
            if bind_vk and bind_vk != 0xFF:
                if bind_vk != vk:
                    continue
            elif bind_scan:
                if bind_scan != scan or (bind_vk and bind_vk != vk):
                    continue
            elif bind_vk != vk:
                continue

            if name == "hold":
                if down:
                    if vk in self._held:  # 长按时的自动重复，一律吞掉，免得往输入框刷一串
                        return 1
                    self._held.add(vk)
                    self.queue.put(("key", "hold", "down"))
                else:
                    self._held.discard(vk)
                    self.queue.put(("key", "hold", "up"))
                return self._decide(n_code, w_param, l_param)

            # toggle：按下时触发一次；带修饰键的组合只在修饰键按住时才算命中
            if down:
                if (mods & self._mods) != mods:
                    break
                if vk in self._held:
                    return self._decide(n_code, w_param, l_param)
                self._held.add(vk)
                self.queue.put(("key", "toggle", "down"))
                return self._decide(n_code, w_param, l_param)
            if vk in self._held:
                self._held.discard(vk)
                return self._decide(n_code, w_param, l_param)
            break

        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _decide(self, n_code, w_param, l_param):
        """共用模式下把按键放行给其他程序，独占模式下吃掉。"""
        if self.share_keys:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        return 1

"""Windows 相关的小工具：剪贴板、模拟按键、前台窗口。"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_QUIT = 0x0012

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_BACK = 0x08
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 给本程序自己注入的按键打个标记，键盘钩子据此忽略它们（避免"打字触发录音"的回路），
# 同时不会误伤别的软件注入的按键（例如用 AutoHotkey 把 Fn 映射成别的键）。
INJECT_TAG = 0x444F5642  # "DOVB"


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", wintypes.UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


# ---------------------------------------------------------------- 剪贴板

def set_clipboard_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise RuntimeError("无法打开剪贴板，请稍后重试")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("剪贴板内存分配失败")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise RuntimeError("剪贴板内存锁定失败")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise RuntimeError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 按键注入

def _send(inputs: list) -> bool:
    if not inputs:
        return True
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))
    return sent == len(inputs)


def _key_event(vk: int, up: bool = False) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.u.ki = KEYBDINPUT(
        wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=INJECT_TAG
    )
    return item


def _char_event(unit: int, up: bool = False) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    item.u.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=INJECT_TAG)
    return item


def send_ctrl_v() -> bool:
    return _send(
        [
            _key_event(VK_CONTROL),
            _key_event(VK_V),
            _key_event(VK_V, up=True),
            _key_event(VK_CONTROL, up=True),
        ]
    )


def send_backspaces(count: int) -> bool:
    """连续退格，用于"边说边打"时回退被修正的字。"""
    if count <= 0:
        return True
    events = []
    for _ in range(min(count, 200)):
        events.append(_key_event(VK_BACK))
        events.append(_key_event(VK_BACK, up=True))
    return _send(events)


def type_text(text: str) -> bool:
    """逐字键入，遇到不支持的窗口可能无效。"""
    raw = text.encode("utf-16-le")
    units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
    ok = True
    index = 0
    while index < len(units):
        unit = units[index]
        if unit in (0x0A, 0x0D):
            ok = _send([_key_event(VK_RETURN), _key_event(VK_RETURN, up=True)]) and ok
            index += 1
            continue
        if 0xD800 <= unit <= 0xDBFF and index + 1 < len(units):
            low = units[index + 1]
            # 代理对必须成对下发，中间不能插入抬起事件
            ok = (
                _send(
                    [
                        _char_event(unit),
                        _char_event(low),
                        _char_event(low, up=True),
                        _char_event(unit, up=True),
                    ]
                )
                and ok
            )
            index += 2
            continue
        ok = _send([_char_event(unit), _char_event(unit, up=True)]) and ok
        index += 1
        time.sleep(0.004)
    return ok


# ---------------------------------------------------------------- 窗口

def foreground_window() -> int | None:
    hwnd = user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def own_foreground_window() -> bool:
    hwnd = foreground_window()
    return bool(hwnd) and window_process_id(hwnd) == os.getpid()


def focus_window(hwnd: int | None) -> bool:
    """把焦点还给目标窗口，成功返回 True。"""
    if not hwnd or not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return False
    if foreground_window() == hwnd:
        return True
    for _ in range(3):
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        time.sleep(0.05)
        if foreground_window() == hwnd:
            return True
    return False

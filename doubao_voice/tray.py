"""系统托盘图标。

程序常驻后台后需要一个不占屏幕的入口，用来打开设置、切换录音方式、退出。
图标是运行时用代码画出来的（不依赖任何图片资源）。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000

WM_TRAY_CALLBACK = WM_APP + 1
WM_TRAY_STATE = WM_APP + 2
WM_TRAY_MODE = WM_APP + 3

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING = 0x0000
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

HWND_MESSAGE = ctypes.c_void_p(-3)
CLASS_NAME = "DoubaoVoiceTrayWnd"

ID_SETTINGS = 1001
ID_MODE_HOLD = 1002
ID_MODE_TOGGLE = 1003
ID_SHOW = 1004
ID_QUIT = 1005

STATE_CODES = {"idle": 0, "recording": 1, "recognizing": 2, "error": 3}
STATE_COLORS = {
    "idle": (150, 156, 163),
    "recording": (232, 69, 60),
    "recognizing": (245, 166, 35),
    "error": (217, 48, 37),
}
STATE_TIPS = {
    "idle": "豆包语音输入 — 待机",
    "recording": "豆包语音输入 — 正在录音",
    "recognizing": "豆包语音输入 — 识别中",
    "error": "豆包语音输入 — 出错了，双击查看",
}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ---------------- 图标绘制 ----------------

def _in_mic(u: float, v: float) -> bool:
    """在 32x32 设计网格里判断点是否落在麦克风图形上。"""
    if ((u - 16.0) / 5.2) ** 2 + ((v - 13.0) / 8.2) ** 2 <= 1.0:
        return True
    dx, dy = u - 16.0, v - 15.0
    radius = (dx * dx + dy * dy) ** 0.5
    if dy >= 0.5 and 7.8 <= radius <= 10.6:
        return True
    if 15.0 <= u <= 17.0 and 25.0 <= v <= 28.5:
        return True
    if 11.0 <= u <= 21.0 and 28.5 <= v <= 30.5:
        return True
    return False


def _icon_pixels(size: int, rgb: tuple[int, int, int]) -> bytes:
    red, green, blue = rgb
    samples = 4
    buffer = bytearray(size * size * 4)
    step = 32.0 / size
    for y in range(size):
        for x in range(size):
            hits = 0
            for sy in range(samples):
                for sx in range(samples):
                    u = (x + (sx + 0.5) / samples) * step
                    v = (y + (sy + 0.5) / samples) * step
                    if _in_mic(u, v):
                        hits += 1
            offset = (y * size + x) * 4
            buffer[offset] = blue
            buffer[offset + 1] = green
            buffer[offset + 2] = red
            buffer[offset + 3] = int(255 * hits / (samples * samples))
    return bytes(buffer)


def make_icon(rgb: tuple[int, int, int], size: int = 32):
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = size
    info.bmiHeader.biHeight = -size  # 自上而下，和像素数组顺序一致
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0  # BI_RGB

    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateBitmap.restype = ctypes.c_void_p
    user32.CreateIconIndirect.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(
        None, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
    )
    if not color:
        return None
    ctypes.memmove(bits, _icon_pixels(size, rgb), size * size * 4)
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    icon_info = ICONINFO(fIcon=True, xHotspot=0, yHotspot=0, hbmMask=mask, hbmColor=color)
    handle = user32.CreateIconIndirect(ctypes.byref(icon_info))
    gdi32.DeleteObject(ctypes.c_void_p(color))
    if mask:
        gdi32.DeleteObject(ctypes.c_void_p(mask))
    return handle


# ---------------- 托盘线程 ----------------

class TrayIcon(threading.Thread):
    """事件：("tray", "settings"/"show"/"quit"/"mode:hold"/"mode:toggle")"""

    def __init__(self, out_queue, tooltip: str = "豆包语音输入"):
        super().__init__(daemon=True, name="tray")
        self.queue = out_queue
        self.tooltip = tooltip
        self.error: str | None = None
        self._hwnd = None
        self._thread_id = None
        self._state = "idle"
        self._mode = "hold"
        self._icons: dict[str, int] = {}
        self._ready = threading.Event()
        self._wndproc = WNDPROC(self._on_message)
        self._setup_api()

    def _setup_api(self) -> None:
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
        ]
        user32.CreatePopupMenu.restype = ctypes.c_void_p
        user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.restype = ctypes.c_int
        user32.TrackPopupMenu.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.DestroyMenu.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    # ---------- 外部接口 ----------

    def wait_ready(self, timeout: float = 2.0) -> None:
        self._ready.wait(timeout)

    def set_state(self, state: str) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_TRAY_STATE, STATE_CODES.get(state, 0), 0)

    def set_mode(self, mode: str) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_TRAY_MODE, 0 if mode == "hold" else 1, 0)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT

    # ---------- 线程 ----------

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        instance = kernel32.GetModuleHandleW(None)

        window_class = WNDCLASSW(
            lpfnWndProc=self._wndproc,
            hInstance=instance,
            lpszClassName=CLASS_NAME,
        )
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            if ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                self.error = f"托盘窗口类注册失败：{ctypes.get_last_error()}"
                self._ready.set()
                return

        self._hwnd = user32.CreateWindowExW(
            0, CLASS_NAME, self.tooltip, 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, instance, None,
        )
        if not self._hwnd:
            self.error = f"托盘窗口创建失败：{ctypes.get_last_error()}"
            self._ready.set()
            return

        data = self._make_data()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            self.error = "托盘图标添加失败"
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._make_data()))
        for handle in self._icons.values():
            user32.DestroyIcon(ctypes.c_void_p(handle))
        self._icons.clear()

    # ---------- 消息处理 ----------

    def _on_message(self, hwnd, message, w_param, l_param):
        if message == WM_TRAY_CALLBACK:
            event = int(l_param)
            if event in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP):
                self._show_menu()
            elif event == WM_LBUTTONDBLCLK:
                self.queue.put(("tray", "settings"))
        elif message == WM_TRAY_STATE:
            self._apply_state(int(w_param))
        elif message == WM_TRAY_MODE:
            self._mode = "hold" if int(w_param) == 0 else "toggle"
        return user32.DefWindowProcW(hwnd, message, w_param, l_param)

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        checked = MF_CHECKED if self._mode == "hold" else 0
        user32.AppendMenuW(menu, MF_STRING | checked, ID_MODE_HOLD, "长按说话")
        checked = MF_CHECKED if self._mode == "toggle" else 0
        user32.AppendMenuW(menu, MF_STRING | checked, ID_MODE_TOGGLE, "按一下开始 / 再按结束")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示主界面")
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "设置…")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self._hwnd)
        command = user32.TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            point.x, point.y, 0, self._hwnd, None,
        )
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

        action = {
            ID_SETTINGS: "settings",
            ID_SHOW: "show",
            ID_QUIT: "quit",
            ID_MODE_HOLD: "mode:hold",
            ID_MODE_TOGGLE: "mode:toggle",
        }.get(command)
        if action:
            self.queue.put(("tray", action))

    def _apply_state(self, code: int) -> None:
        state = next((k for k, v in STATE_CODES.items() if v == code), "idle")
        self._state = state
        data = self._make_data()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _icon(self) -> int:
        if self._state not in self._icons:
            handle = make_icon(STATE_COLORS.get(self._state, STATE_COLORS["idle"]))
            self._icons[self._state] = handle or 0
        return self._icons.get(self._state) or 0

    def _make_data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self._icon()
        data.szTip = self.tooltip or STATE_TIPS.get(self._state, "豆包语音输入")
        return data

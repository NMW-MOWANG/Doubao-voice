"""录音时浮在桌面上的小图标。

要求：绝不抢焦点（否则正在打字的窗口会丢焦点，文字就进错地方了），
所以除了置顶 + 无边框，还要给窗口加上 WS_EX_NOACTIVATE 和 WS_EX_TRANSPARENT。
"""

from __future__ import annotations

import ctypes
import tkinter as tk

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

TRANSPARENT_COLOR = "#ff00ff"

STATE_COLORS = {
    "recording": "#e8453c",
    "recognizing": "#f5a623",
    "done": "#34a853",
    "error": "#d93025",
}

WIDTH, HEIGHT = 320, 56
MARGIN_BOTTOM = 120


def _set_extended_style(hwnd: int, extra: int) -> None:
    getter = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    setter = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    getter.restype = ctypes.c_ssize_t
    setter.restype = ctypes.c_ssize_t
    style = getter(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    setter(ctypes.c_void_p(hwnd), GWL_EXSTYLE, style | extra)


class Overlay:
    """底部居中的小浮标：麦克风图标 + 音量环 + 一行提示文字。"""

    def __init__(self, master: tk.Misc):
        self.window = tk.Toplevel(master)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.window,
            width=WIDTH,
            height=HEIGHT,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
        )
        self.canvas.pack()

        self._ring = self.canvas.create_oval(4, 4, 52, 52, outline=STATE_COLORS["recording"], width=2, fill="#1f2023")
        self._level = self.canvas.create_arc(
            4, 4, 52, 52, start=90, extent=0, style="arc",
            outline=STATE_COLORS["recording"], width=3,
        )
        self.canvas.create_oval(20, 13, 36, 33, fill="#ffffff", outline="")
        self.canvas.create_arc(15, 20, 41, 44, start=200, extent=140, style="arc", outline="#ffffff", width=2)
        self.canvas.create_line(28, 42, 28, 47, fill="#ffffff", width=2)
        self.canvas.create_line(21, 47, 35, 47, fill="#ffffff", width=2)
        self._text = self.canvas.create_text(
            64, HEIGHT // 2, anchor="w", text="", fill="#ffffff",
            font=("Microsoft YaHei UI", 11),
        )

        self.window.update_idletasks()
        self._place()
        self.window.withdraw()
        self._make_click_through()
        self.state = ""
        self._pulse = 0.0

    # ---------- 对外 ----------

    def show(self, state: str, text: str = "") -> None:
        self.state = state
        color = STATE_COLORS.get(state, STATE_COLORS["recording"])
        self.canvas.itemconfigure(self._ring, outline=color)
        self.canvas.itemconfigure(self._level, outline=color)
        self.canvas.itemconfigure(self._text, text=text)
        if not self.window.winfo_viewable():
            self._place()
            self.window.deiconify()
            self.window.lift()

    def set_level(self, level: float) -> None:
        if self.state != "recording":
            return
        extent = -max(0.0, min(1.0, level)) * 360.0
        self.canvas.itemconfigure(self._level, extent=extent)

    def tick(self) -> None:
        """识别中时让圆环脉动，给用户"它在干活"的感觉。"""
        if self.state != "recognizing" or not self.window.winfo_viewable():
            return
        self._pulse = (self._pulse + 0.12) % 1.0
        extent = -(0.15 + 0.45 * abs(1 - 2 * self._pulse)) * 360.0
        self.canvas.itemconfigure(self._level, extent=extent)

    def set_text(self, text: str) -> None:
        self.canvas.itemconfigure(self._text, text=text)

    def hide(self) -> None:
        self.state = ""
        self.window.withdraw()

    @property
    def visible(self) -> bool:
        return bool(self.window.winfo_viewable())

    # ---------- 内部 ----------

    def _place(self) -> None:
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(0, (screen_w - WIDTH) // 2)
        y = max(0, screen_h - HEIGHT - MARGIN_BOTTOM)
        self.window.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

    def _make_click_through(self) -> None:
        try:
            hwnd = self.window.winfo_id()
            for handle in (hwnd, user32.GetParent(ctypes.c_void_p(hwnd)) or 0):
                if handle:
                    _set_extended_style(
                        handle, WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                    )
        except Exception:
            pass

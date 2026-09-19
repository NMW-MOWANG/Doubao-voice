"""按键捕获：按一下某个键，把它设成录音键。

专门解决 Fn 这类"不知道系统收不收得到"的按键——捕获不到就说明键盘把 Fn
做在了硬件层，Windows 根本收不到，任何软件都无能为力。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk

from . import config, keyhook
from .ui import enable_dpi_awareness

FONT = "Microsoft YaHei UI"
MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
SLOT_NAMES = {"hold": "长按键", "toggle": "切换键"}


class KeyDetector:
    """弹一个小窗，把按下的第一个非修饰键交回来。"""

    def __init__(self, slot: str = "hold", timeout: float = 180.0):
        self.slot = slot
        self.timeout = timeout
        self.result: tuple[int, int, int] | None = None
        self.events: queue.Queue = queue.Queue()
        self.hook = keyhook.KeyboardHook(self.events, share_keys=False)
        self._deadline = 0.0
        self._root: tk.Tk | None = None

    def run(self) -> tuple[int, int, int] | None:
        enable_dpi_awareness()
        root = tk.Tk()
        self._root = root
        root.title("设置录音键")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True, padx=16, pady=14)

        ttk.Label(
            frame,
            text=f"请按一下你想用作「{SLOT_NAMES.get(self.slot, self.slot)}」的按键",
            font=(FONT, 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="想设为 Fn 就直接按 Fn；按 Esc 取消",
            foreground="#888",
            font=(FONT, 9),
        ).pack(anchor="w", pady=(2, 10))

        self.status = tk.StringVar(value="等待按键…")
        ttk.Label(frame, textvariable=self.status, foreground="#1a73e8", font=(FONT, 11)).pack(anchor="w")

        self.seen = tk.StringVar(value="")
        ttk.Label(
            frame, textvariable=self.seen, foreground="#5f6368", font=(FONT, 9), wraplength=320
        ).pack(anchor="w", pady=(6, 0))

        self.countdown = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.countdown, foreground="#aaa", font=(FONT, 9)).pack(
            anchor="w", pady=(10, 0)
        )

        ttk.Button(frame, text="取消", width=8, command=root.destroy).pack(anchor="e", pady=(10, 0))

        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        root.geometry(f"+{max(0, (screen_w - 380) // 2)}+{max(0, screen_h // 3)}")
        root.lift()
        root.focus_force()

        self.hook.start()
        self.hook.wait_ready()
        self.hook.begin_capture()
        self._deadline = time.monotonic() + self.timeout

        root.after(60, self._poll)
        root.mainloop()
        self.hook.stop()
        return self.result

    def _poll(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "keycap":
                    self._on_key(*event[1:])
        except queue.Empty:
            pass

        left = max(0.0, self._deadline - time.monotonic())
        self.countdown.set(f"最多等待 {left:.0f} 秒")
        if left <= 0:
            self.status.set("没有捕获到任何按键")
            root.after(1200, root.destroy)
            return
        root.after(60, self._poll)

    def _on_key(self, mods: int, vk: int, scan: int) -> None:
        root = self._root
        if root is None:
            return

        if vk in MODIFIER_VKS:
            self.status.set("这是修饰键，请再按一个别的键")
            self.hook.begin_capture()
            return

        if vk == 0x1B and not mods:  # Esc
            root.destroy()
            return

        label = keyhook.key_label(mods, vk, scan)
        self.result = (mods, vk, scan)
        self.status.set(f"已捕获：{label}")
        self.seen.set(f"vk=0x{vk:02X}　scan=0x{scan:02X}　mods={mods}")
        root.after(900, root.destroy)


class KeyWatcher:
    """实时显示每一个按下的键，用来判断某个键到底有没有进 Windows。

    不吞按键、不绑定任何键，纯粹旁观：先按一个普通键确认工具有反应，
    再按 Fn——有反应就说明 Fn 能设，没反应就是键盘没把它交给系统。
    """

    def __init__(self, seconds: float = 120.0, expect: str = "Fn"):
        self.seconds = seconds
        self.expect = expect
        self.claimed = False
        self.events: queue.Queue = queue.Queue()
        self.hook = keyhook.KeyboardHook(self.events, share_keys=True)
        self.hook.capture_swallows = False
        self._deadline = 0.0
        self._root: tk.Tk | None = None
        self._order: list[str] = []

    def run(self) -> list[str]:
        enable_dpi_awareness()
        root = tk.Tk()
        self._root = root
        root.title("按键监视器")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True, padx=16, pady=14)
        ttk.Label(
            frame, text=f"请只按一下 {self.expect} 键", font=(FONT, 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"先按一个普通键（比如 A）确认工具有反应，再按 {self.expect}",
            foreground="#888",
            font=(FONT, 9),
        ).pack(anchor="w", pady=(2, 10))

        self.status = tk.StringVar(value="还没有收到任何按键…")
        ttk.Label(frame, textvariable=self.status, foreground="#1a73e8", font=(FONT, 11)).pack(anchor="w")

        self.body = tk.Text(
            frame, width=44, height=8, relief="flat", bg="#f4f5f7", font=("Consolas", 10)
        )
        self.body.pack(fill="both", expand=True, pady=(8, 0))
        self.body.configure(state="disabled")

        self.countdown = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.countdown, foreground="#aaa", font=(FONT, 9)).pack(
            anchor="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            buttons,
            text=f"我按过 {self.expect} 了，但上面没显示",
            command=self._claim,
        ).pack(side="left")
        ttk.Button(buttons, text="关闭", width=8, command=root.destroy).pack(side="right")

        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"+{max(0, (screen_w - 460) // 2)}+{max(0, screen_h // 4)}")
        root.lift()

        self.hook.start()
        self.hook.wait_ready()
        self.hook.begin_capture()
        self._deadline = time.monotonic() + self.seconds
        root.after(60, self._poll)
        root.mainloop()
        self.hook.stop()
        return self._order

    def _claim(self) -> None:
        """用户声明"我按过了但没反应"——把歧义记录清楚。"""
        self.claimed = True
        self.status.set(f"已记录：按过 {self.expect}，但工具没有收到")
        print(f"  用户声明：按过 {self.expect}，但监视器没有收到任何对应按键", flush=True)
        if self._root is not None:
            self._root.after(700, self._root.destroy)

    def _poll(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "keycap":
                    self._on_key(*event[1:])
                    self.hook.begin_capture()  # 继续听下一个键
        except queue.Empty:
            pass

        left = max(0.0, self._deadline - time.monotonic())
        self.countdown.set(f"监视中，还剩 {left:.0f} 秒")
        if left <= 0:
            root.destroy()
            return
        root.after(60, self._poll)

    def _on_key(self, mods: int, vk: int, scan: int) -> None:
        label = keyhook.key_label(mods, vk, scan)
        line = f"{label:<22} vk=0x{vk:02X}  scan=0x{scan:02X}  mods={mods}"
        self._order.append(line)
        print(f"  收到按键: {line}", flush=True)
        self.status.set(f"最近一个：{label}")
        body = self.body
        body.configure(state="normal")
        body.insert("end", line + "\n")
        body.see("end")
        body.configure(state="disabled")


def restart_app() -> bool:
    """重新拉起常驻程序；新实例会请旧的自己退出。"""
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = python
    script = os.path.join(str(config.APP_DIR), "voice_input.py")
    if not os.path.exists(script):
        return False
    subprocess.Popen([pythonw, script], cwd=str(config.APP_DIR), close_fds=True)
    return True


def cli(cfg: dict, slot: str, timeout: float, restart: bool) -> int:
    detected = KeyDetector(slot=slot, timeout=timeout).run()
    name = SLOT_NAMES.get(slot, slot)

    if detected is None:
        print("没有捕获到任何按键。")
        print()
        print("如果你按了 Fn 也没反应，说明这个键盘的 Fn 由硬件直接处理，")
        print("按键事件根本不会进 Windows —— 任何软件都收不到它。")
        print("建议先运行 `python voice_input.py --watch-keys` 确认：")
        print("先按一个普通键，再按 Fn，对比两者有没有被收到。")
        print("可行的绕法：用键盘厂商的驱动软件把 Fn 映射成一个不常用的键（比如 F9），")
        print("再把这个工具运行一次、按那个键。")
        return 1

    mods, vk, scan = detected
    cfg = dict(cfg)
    cfg[f"{slot}_key"] = keyhook.format_spec(mods, vk, scan)
    config.save(cfg)

    print(f"已把「{name}」设为：{keyhook.key_label(mods, vk, scan)}")
    print(f"　　vk=0x{vk:02X}　scan=0x{scan:02X}　mods={mods}")
    print(f"配置已写入 {config.CONFIG_PATH}")

    if restart:
        if restart_app():
            print("已重新启动常驻程序，新按键立即生效。")
        else:
            print("没找到 voice_input.py，请手动重新启动程序。")
    else:
        print("重新启动程序后生效。")
    return 0


def watch_cli(seconds: float, expect: str = "Fn") -> int:
    watcher = KeyWatcher(seconds=seconds, expect=expect)
    seen = watcher.run()
    print()
    if seen:
        print(f"共收到 {len(seen)} 个按键：")
        for line in seen:
            print("  " + line)
    else:
        print("整个监视期间一个按键都没收到。")

    print()
    if watcher.claimed:
        print(f"你已确认按过 {expect} 但没被收到 —— 结论明确：")
        print(f"这把键盘没有把 {expect} 的按键事件交给 Windows，任何软件都收不到它。")
        print("只能靠键盘厂商的驱动软件把 Fn 映射成别的键（比如 F9），或者换个键。")
    elif seen:
        print(f"上面有这些键。如果里面没有 {expect}，而你确实单独按过它，那就是键盘没交给系统。")
    return 0 if seen else 1

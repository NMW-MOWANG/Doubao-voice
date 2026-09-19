"""单实例保护。

两个实例同时跑会装两个键盘钩子：互相干扰（旧钩子吞按键会让新代码失效），
还会各自触发一次录音、把同一段话打两遍。

所以第二次启动时不做"报错退出"，而是礼貌地请前一个实例退出再接管——
这样"重新双击启动"就等于"用新代码重启"，改完配置不用去任务管理器。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF

MUTEX_NAME = "Local\\DoubaoVoiceInput.Mutex"
QUIT_EVENT_NAME = "Local\\DoubaoVoiceInput.Quit"

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.SetEvent.argtypes = [ctypes.c_void_p]
kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


class InstanceGuard:
    def __init__(self):
        self.error: str | None = None
        self._mutex = None
        self._quit_event = None

    def acquire(self, timeout: float = 6.0) -> bool:
        """拿到锁返回 True；发现旧实例就先请它退出，等不到则返回 False。"""
        deadline = time.monotonic() + timeout
        while True:
            handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
            if not handle:
                self.error = f"创建互斥体失败：{ctypes.get_last_error()}"
                return True  # 拿不到锁也不该拦着用户用
            if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
                self._mutex = handle
                event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
                if event:
                    kernel32.ResetEvent(event)  # 清掉可能残留的信号
                self._quit_event = event
                return True

            kernel32.CloseHandle(handle)
            self._ask_previous_to_quit()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _ask_previous_to_quit(self) -> None:
        event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT_NAME)
        if event:
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)

    def watch(self, queue) -> None:
        """后台等退出信号：收到就当成托盘菜单里的"退出"处理。"""
        if not self._quit_event:
            return

        def waiter() -> None:
            kernel32.WaitForSingleObject(self._quit_event, INFINITE)
            queue.put(("tray", "quit"))

        threading.Thread(target=waiter, daemon=True, name="instance-watch").start()

    def release(self) -> None:
        for handle in (self._quit_event, self._mutex):
            if handle:
                kernel32.CloseHandle(handle)
        self._quit_event = None
        self._mutex = None

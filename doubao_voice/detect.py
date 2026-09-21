"""按键捕获，用来设置录音键 / 排查按键到底有没有进系统。

Linux 这边比 Windows 简单：不需要弹一个窗口来接按键，直接读 evdev 就行，
所以这个模块在命令行里跑，不需要图形界面。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time

from . import config, keyhook
from .linuxutil import setup_hint

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class KeyCapture:
    """临时起一个键盘钩子，按键事件从队列里一个一个取。"""

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue()
        self.hook = keyhook.KeyboardHook(self.queue, share_keys=True)

    def __enter__(self) -> "KeyCapture":
        self.hook.start()
        self.hook.wait_ready(1.0)
        return self

    def __exit__(self, *_exc) -> None:
        self.hook.stop()

    def next_key(self, timeout: float) -> tuple[int, int] | None:
        self.hook.begin_capture()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                event = self.queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if event[0] == "keycap":
                return (event[1], event[2])


def _describe_devices(capture: KeyCapture) -> None:
    devices = capture.hook.devices or keyhook.keyboard_devices()
    if devices:
        print("正在监听的设备：")
        for path in devices:
            print(f"  {path}  {keyhook._device_name(path)}")
    print()


def _permission_problem(capture: KeyCapture) -> int | None:
    hook = capture.hook
    if hook.error:
        print(hook.error, file=sys.stderr)
        return 3
    if not hook.devices:
        print(
            "一个键盘设备都打不开：要么没有键盘，要么没有读 /dev/input/event* 的权限。",
            file=sys.stderr,
        )
        print(setup_hint(), file=sys.stderr)
        return 3
    return None


def watch_cli(seconds: float = 120.0) -> int:
    print(f"实时显示按键（{seconds:.0f} 秒后自动结束，Ctrl+C 也能停）。")
    print("按一下想用的键，看这里的输出：")
    print("  * 有对应的 code → 这个键能用，可以用 --detect-key 设成录音键；")
    print("  * 什么都没冒出来 → 键盘把 Fn 这类键做在硬件层了，系统根本收不到，")
    print("    只能先在键盘自己的驱动/固件里把它映射成别的键（比如右 Ctrl）。")
    print()
    with KeyCapture() as capture:
        problem = _permission_problem(capture)
        if problem is not None:
            return problem
        _describe_devices(capture)

        deadline = time.monotonic() + max(1.0, seconds)
        while time.monotonic() < deadline:
            key = capture.next_key(min(1.0, max(0.1, deadline - time.monotonic())))
            if key is None:
                continue
            mods, code = key
            print(f"  {keyhook.key_label(mods, code)}   →  {keyhook.format_spec(mods, code)}"
                  f"   （修饰键 {mods}，键码 {code}）", flush=True)
    return 0


def cli(cfg: dict, slot: str, timeout: float, restart: bool) -> int:
    name = "长按键" if slot == "hold" else "切换键"
    print(f"请按一下要设为「{name}」的那个键（{timeout:.0f} 秒内）…")
    with KeyCapture() as capture:
        problem = _permission_problem(capture)
        if problem is not None:
            return problem
        _describe_devices(capture)

        key = capture.next_key(timeout)
        if key is None:
            print("等超时了，没有收到任何按键。", file=sys.stderr)
            return 1
        mods, code = key
        spec = keyhook.format_spec(mods, code)
        print(f"捕获到：{keyhook.key_label(mods, code)}  →  {spec}")

    fresh = config.load()
    if slot == "hold":
        fresh["hold_key"] = spec
    else:
        fresh["toggle_key"] = spec
    config.save(fresh)
    print(f"已写入配置文件：{config.CONFIG_PATH}")

    if restart:
        restart_app()
    else:
        print("正在运行的实例需要重启才会用上新按键（或者在设置里点保存）。")
    return 0


def restart_app() -> None:
    from . import instance

    if instance.send("quit") is not None:
        print("已请旧实例退出，正在重启…")
        for _ in range(40):
            if instance.send("ping") is None:
                break
            time.sleep(0.25)
    env = dict(os.environ)
    env["PYTHONPATH"] = APP_DIR + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, "voice_input.py")],
        cwd=APP_DIR,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("新实例已启动。")

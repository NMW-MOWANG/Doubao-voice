"""把识别结果实时打进当前光标位置。

流式识别的文字是不断增长的，偶尔还会回头改前面的字（标点、数字规范化、
把"二十六"写成"26"）。所以这里维护一份「已经打进去的内容」，每次拿到新结果
只做差异操作：能续写就只补新字，需要改动就退格到最长公共前缀再重打。

Linux 和 Windows 的差别：
* **拿不到"当前焦点窗口是谁"**（Wayland 没有这个接口），原版"目标窗口失焦就
  停手"的保护没有了——录的时候别切窗口；
* **文字一律靠剪贴板粘贴**，不逐字敲键盘：uinput 注入的按键会先经过输入法，
  本机的输入法是 libpinyin（中文拼音），注入的 ASCII 字母会被吃进拼音候选
  （实测 "hello world" 变成"和来o"）。粘贴不经过输入法，中英文都准；
* **上屏要攒着一起打**：mutter 要求"写剪贴板的那一刻必须拥有键盘焦点"，所以每次
  写剪贴板都是一次"抢焦点 → 写 → 还焦点"的往返，实测单次约 100 毫秒，还会打断
  焦点。中间结果一秒能来四五次，每次都写既慢又扰民，所以默认攒 350ms（或满 6 个
  字、或说到句号问号）再一起上屏，把写入次数压到三分之一左右。
"""

from __future__ import annotations

import threading
import time

from . import linuxutil

LIVE_BACKSPACE_LIMIT = 40   # 单次回退上限，防止识别结果跳变时把用户已有的内容删掉
FINAL_BACKSPACE_LIMIT = 200

SENTENCE_ENDINGS = "。！？!?；;\n"  # 碰到这些就立刻上屏，不等攒够

DEFAULT_INTERVAL_MS = 350
DEFAULT_MIN_CHARS = 6


def common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class LiveTyper:
    """把增量文本敲进当前焦点窗口。

    为了少写几次剪贴板，中间结果会先攒在 _pending 里，满足下面任一条件才真的上屏：
    * 攒的时间超过 min_interval（界面定时器 tick() 也会来催）；
    * 本次新增的字数达到 min_chars；
    * 文本以句末标点结尾；
    * 这是第一段（立刻给用户一点反应）。
    """

    def __init__(
        self,
        injector,
        paste_shift: bool = False,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        min_chars: int = DEFAULT_MIN_CHARS,
    ):
        self.injector = injector
        self.paste_shift = bool(paste_shift)
        self.min_interval = max(0.0, float(interval_ms) / 1000.0)
        self.min_chars = max(1, int(min_chars))
        self.committed = ""
        self.error: str | None = None
        self.used_clipboard = False
        self._pending: str | None = None
        self._last_apply = 0.0
        # 这个对象同时被"接收线程"（partial 回调）和"识别线程"（tick/finish）碰，
        # 必须串行化，否则 committed 会被两边一起改乱。
        self._lock = threading.RLock()

    # ---------- 对外 ----------

    def update(self, text: str, limit: int = LIVE_BACKSPACE_LIMIT) -> None:
        with self._lock:
            if self.error or not text or text == self.committed:
                return
            self._pending = text
            if self._should_flush(text):
                self.flush(limit)

    def tick(self) -> None:
        """把攒着的文本补上去（由识别线程驱动，不要在界面线程里调——里面要做剪贴板 IO）。"""
        with self._lock:
            if self._pending is None or self.error:
                return
            if time.monotonic() - self._last_apply >= self.min_interval:
                self.flush()

    def flush(self, limit: int = LIVE_BACKSPACE_LIMIT) -> None:
        with self._lock:
            text, self._pending = self._pending, None
            if text is None or self.error:
                return
            self._apply(text, limit)
            self._last_apply = time.monotonic()

    def finish(self, text: str) -> str:
        """收尾校正，返回最终真正打进去的文本。"""
        with self._lock:
            self._pending = None
            if not self.error:
                self._apply(text, FINAL_BACKSPACE_LIMIT)
                self._last_apply = time.monotonic()
            return self.committed

    # ---------- 内部 ----------

    def _should_flush(self, text: str) -> bool:
        if not self.committed or self._last_apply == 0.0:
            return True
        if len(text) - len(self.committed) >= self.min_chars:
            return True
        if text[-1:] in SENTENCE_ENDINGS:
            return True
        return time.monotonic() - self._last_apply >= self.min_interval

    def _apply(self, text: str, limit: int) -> None:
        if self.error or not text or text == self.committed:
            return
        if not self.injector.available:
            self.error = "按键注入不可用"
            return

        if not text.startswith(self.committed):
            prefix = common_prefix_length(self.committed, text)
            back = len(self.committed) - prefix
            if back > limit:
                return  # 差异过大，等最终结果再处理
            if back:
                try:
                    self.injector.backspaces(back)
                except Exception as exc:
                    self.error = f"退格失败：{exc}"
                    return
            self.committed = self.committed[:prefix]

        delta = text[len(self.committed):]
        if delta:
            try:
                self._insert(delta)
            except Exception as exc:
                self.error = f"键入失败：{exc}"
                return
            self.committed = text

    def _insert(self, text: str) -> None:
        # 一律走"剪贴板 + Ctrl+V"，不逐字敲键盘：本机的输入法是 libpinyin（中文拼音），
        # uinput 注入的 ASCII 字母会被输入法吃进拼音候选里（实测 "hello world" 变成"和来o"），
        # 而粘贴不经过输入法，中英文都准。
        linuxutil.set_clipboard_text(text)
        self.injector.paste(shift=self.paste_shift)
        self.used_clipboard = True

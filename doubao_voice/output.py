"""把识别结果实时打进当前光标位置。

流式识别的文字是不断增长的，偶尔还会回头改前面的字（标点、数字规范化、
把"二十六"写成"26"）。所以这里维护一份「已经打进目标窗口的内容」，
每次拿到新结果只做差异操作：能续写就只补新字，需要改动就退格到最长公共前缀再重打。
"""

from __future__ import annotations

from . import winutil

LIVE_BACKSPACE_LIMIT = 40   # 单次回退上限，防止识别结果跳变时把用户已有的内容删掉
FINAL_BACKSPACE_LIMIT = 200


def common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class LiveTyper:
    """负责把增量文本敲进 target 窗口。目标窗口失焦就停手，绝不往别处打字。"""

    def __init__(self, target: int | None):
        self.target = target
        self.committed = ""
        self.lost_focus = False
        self.error: str | None = None

    def _focused(self) -> bool:
        if not self.target:
            return False
        return winutil.foreground_window() == self.target

    def update(self, text: str, limit: int = LIVE_BACKSPACE_LIMIT) -> None:
        if self.error or not text or text == self.committed:
            return
        if not self._focused():
            self.lost_focus = True
            return

        if not text.startswith(self.committed):
            prefix = common_prefix_length(self.committed, text)
            back = len(self.committed) - prefix
            if back > limit:
                return  # 差异过大，等最终结果再处理
            if back and not winutil.send_backspaces(back):
                self.error = "退格失败"
                return
            self.committed = self.committed[:prefix]

        delta = text[len(self.committed) :]
        if delta:
            if not winutil.type_text(delta):
                self.error = "键入失败"
                return
            self.committed = text

    def finish(self, text: str) -> str:
        """收尾校正，返回最终真正打进目标窗口的文本。"""
        if not self.error:
            if not self._focused():
                self.lost_focus = True
            else:
                self.update(text, limit=FINAL_BACKSPACE_LIMIT)
        return self.committed

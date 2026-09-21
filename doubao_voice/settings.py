"""设置面板（GTK3）。

参数较多，按官方接口文档分成几个标签页：接口 / 按键 / 录音 / 识别 / 高级。
面板是常驻托盘程序的附属窗口，关掉不影响程序运行；保存时用控件里的值组装一份
完整配置交给主程序，界面上没有的字段（窗口位置等）原样保留。

Linux 平台差异如实写在界面上：

* 内核这一层只有"只读旁听"和"整台键盘独占"（EVIOCGRAB）两种，后者会把整块
  键盘从系统里拿走，不可接受。所以「不独占按键」在 Linux 上没有开关，做成禁用。
* 多数键盘的 Fn 键不产生按键事件，设不了；`--detect-key` 可以确认键盘到底发不
  发这个键。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # noqa: E402

from . import config, keyhook
from .linuxutil import missing_permissions, setup_hint
from .recorder import list_input_devices

STATUS_COLOR = "#1a73e8"
ERROR_COLOR = "#c5221f"

LABEL_WIDTH = 110

ZH_VARIANTS = {
    "不转换": "",
    "简体 → 繁体（大陆）": "traditional",
    "简体 → 台湾正体": "tw",
    "简体 → 香港繁体": "hk",
}

RESULT_TYPES = {
    "整体返回（full）": "full",
    "只返回新增分句（single）": "single",
}

# Linux 版新增的三个开关；config.py 里还没有这几个 key 时用这里的默认值
FALLBACK_DEFAULTS = {
    "notify": False,
    "restore_clipboard": True,
    "paste_shift": False,
}

SHARE_KEYS_TOOLTIP = (
    "Linux 上录音键始终与其它软件共用：内核层只有「只读旁听」和「整台键盘独占」"
    "两种做法，后者会把整块键盘从系统里拿走，所以这一项没有开关。"
)
FN_NOTE = (
    "多数键盘的 Fn 键不产生按键事件，设不了。想知道自己的键盘发不发这个键，"
    "跑一次 python3 voice_input.py --detect-key 按几下就知道了。"
)


def _default(key: str, fallback=None):
    if key in config.DEFAULTS:
        return config.DEFAULTS[key]
    if key in FALLBACK_DEFAULTS:
        return FALLBACK_DEFAULTS[key]
    return fallback


def _dim_label(text: str, wrap: bool = False) -> Gtk.Label:
    """灰色说明文字。"""
    label = Gtk.Label(label=text, xalign=0)
    label.get_style_context().add_class("dim-label")
    if wrap:
        label.set_line_wrap(True)
        label.set_max_width_chars(64)
    return label


def _markup_label(text: str, color: str, wrap: bool = False) -> Gtk.Label:
    label = Gtk.Label(xalign=0)
    label.set_markup(f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>')
    if wrap:
        label.set_line_wrap(True)
        label.set_max_width_chars(64)
    return label


def _normalize_spec(spec: str) -> str:
    """把配置里的按键规格（可能还是旧版三段式）换成当前格式，解析不出来就原样留着。"""
    mods, code = keyhook.parse_spec(spec)
    return keyhook.format_spec(mods, code) if code else str(spec or "")


class _Group:
    """标签页里的一组控件：内部按「标签 | 控件 | 说明」三列排。"""

    def __init__(self, page: Gtk.Box, title: str, hint: str = ""):
        frame = Gtk.Frame(label=title, shadow_type=Gtk.ShadowType.ETCHED_IN)
        frame.set_margin_top(6)
        page.pack_start(frame, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=8, row_spacing=4, border_width=8)
        frame.add(self.grid)
        self._row = 0
        if hint:
            self.note(hint)

    def line(self, label: str = "", hint: str = "") -> Gtk.Box:
        """新起一行，返回往里放控件的水平盒子。"""
        if label:
            text = Gtk.Label(label=label, xalign=0)
            text.set_size_request(LABEL_WIDTH, -1)
            self.grid.attach(text, 0, self._row, 1, 1)
        box = Gtk.Box(spacing=6)
        box.set_hexpand(True)
        self.grid.attach(box, 1, self._row, 1, 1)
        if hint:
            note = _dim_label(hint)
            self.grid.attach(note, 2, self._row, 1, 1)
        self._row += 1
        return box

    def note(self, text: str) -> Gtk.Label:
        """整行的灰色说明。"""
        label = _dim_label(text, wrap=True)
        self.grid.attach(label, 0, self._row, 3, 1)
        self._row += 1
        return label

    def span(self, widget: Gtk.Widget, hint: str = "") -> None:
        """整行的控件（多行文本框之类）。"""
        self.grid.attach(widget, 0, self._row, 3, 1)
        self._row += 1
        if hint:
            self.note(hint)


class SettingsDialog(Gtk.Window):
    """设置面板。关掉（点 X 或「取消」）就是销毁，winfo_exists() 随即变 False。"""

    def __init__(self, app):
        super().__init__(title="豆包语音输入 · 设置")
        self.app = app
        self.devices = list_input_devices()
        self.specs = {
            "hold": _normalize_spec(app.cfg.get("hold_key", "")),
            "toggle": _normalize_spec(app.cfg.get("toggle_key", "")),
        }
        self._checks: dict[str, Gtk.CheckButton] = {}
        self._spins: dict[str, Gtk.SpinButton] = {}
        self._entries: dict[str, Gtk.Entry] = {}
        self._texts: dict[str, Gtk.TextView] = {}
        self._combos: dict[str, tuple[Gtk.ComboBoxText, dict[str, str]]] = {}
        self._keys: list[str] = []
        self._key_names: dict[str, Gtk.Label] = {}
        self._key_specs: dict[str, Gtk.Entry] = {}
        self._key_buttons: dict[str, Gtk.Button] = {}
        self._alive = True

        self.set_border_width(10)
        self.set_default_size(760, 660)
        self.connect("destroy", self._on_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(outer)

        problems = missing_permissions()
        if problems:
            banner = _markup_label(
                "设备权限不完整：\n• " + "\n• ".join(problems) + "\n" + setup_hint(),
                ERROR_COLOR,
                wrap=True,
            )
            outer.pack_start(banner, False, False, 0)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        outer.pack_start(notebook, True, True, 0)
        for title, builder in (
            ("接口", self._tab_api),
            ("按键", self._tab_keys),
            ("录音", self._tab_record),
            ("识别", self._tab_recognize),
            ("高级", self._tab_advanced),
        ):
            builder(self._page(notebook, title))

        footer = Gtk.Box(spacing=8)
        outer.pack_start(footer, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(46)
        footer.pack_start(self.status, True, True, 0)
        for text, handler in (
            ("取消", lambda _button: self.destroy()),
            ("保存", lambda _button: self.save()),
            ("恢复默认值", lambda _button: self.restore_defaults()),
        ):
            button = Gtk.Button(label=text)
            button.connect("clicked", handler)
            footer.pack_end(button, False, False, 0)

        self.show_all()

    # ------------------------------------------------------------ 对外接口

    def present(self) -> None:
        """显示并前置窗口。"""
        self.show_all()
        Gtk.Window.present(self)

    def winfo_exists(self) -> bool:
        """窗口还在就 True。"""
        return self._alive

    def apply_key_capture(self, slot: str, spec: str) -> None:
        """主程序捕获到按键后回填。"""
        if slot not in self.specs:
            return
        self.specs[slot] = _normalize_spec(spec)
        self._show_spec(slot)
        self._key_buttons[slot].set_label("按下按键…")
        if "未知键" in keyhook.key_label(*keyhook.parse_spec(spec)):
            self._set_status("已设为未知键，可能是 Fn；能用就用。" + FN_NOTE)

    def apply_calibration(self, noise_rms: float) -> None:
        """噪声检测结果回填建议阈值。"""
        threshold = max(80, int(noise_rms * 2.5))
        self._spins["vad_threshold"].set_value(threshold)
        self.calibration.set_text(f"实测环境噪声 {noise_rms:.0f}，已填入 {threshold}")

    def save(self) -> None:
        """组装完整配置交给主程序，通过校验后销毁窗口。"""
        cfg = self._collect()
        if not cfg["api_key"]:
            self._set_status("请先填 API Key", error=True)
            return
        for slot in ("hold", "toggle"):
            if not keyhook.parse_spec(self.specs[slot])[1]:
                self._set_status("「长按键」和「切换键」都要按一次按键", error=True)
                return
        if cfg.get("enable_auto_lang") and (cfg.get("hotwords") or cfg.get("context_text")):
            self._set_status("自动识别语种和热词/上下文不能同时用，请去掉一个", error=True)
            return
        self.app.apply_config(cfg)
        self.destroy()

    def restore_defaults(self) -> None:
        """把控件恢复成默认值，点「保存」才生效。"""
        defaults = dict(config.DEFAULTS)
        defaults.update(FALLBACK_DEFAULTS)
        for key in self._keys:
            self._set_value(key, defaults.get(key))
        for slot in ("hold", "toggle"):
            self.specs[slot] = _normalize_spec(defaults.get(f"{slot}_key", ""))
            self._show_spec(slot)
        self.device_combo.set_active(self._device_index(defaults.get("device", -1)))
        self._set_status("已恢复默认值，点「保存」生效")

    # ------------------------------------------------------------ 布局工具

    def _on_destroy(self, _widget) -> None:
        self._alive = False

    def _set_status(self, text: str, error: bool = False) -> None:
        color = ERROR_COLOR if error else STATUS_COLOR
        self.status.set_markup(f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>')

    def _page(self, notebook: Gtk.Notebook, title: str) -> Gtk.Box:
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.NONE)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        box.set_hexpand(True)
        box.set_valign(Gtk.Align.START)
        scrolled.add(box)
        notebook.append_page(scrolled, Gtk.Label(label=title))
        return box

    def _note(self, page: Gtk.Box, text: str) -> None:
        label = _dim_label(text, wrap=True)
        label.set_margin_top(6)
        label.set_margin_start(14)
        page.pack_start(label, False, False, 0)

    def _check_widget(
        self, key: str, text: str, default: bool = False, tooltip: str = ""
    ) -> Gtk.CheckButton:
        button = Gtk.CheckButton(label=text)
        button.set_active(bool(self.app.cfg.get(key, _default(key, default))))
        if tooltip:
            button.set_tooltip_text(tooltip)
        self._checks[key] = button
        self._keys.append(key)
        return button

    def _check(
        self,
        group: _Group,
        key: str,
        text: str,
        hint: str = "",
        default: bool = False,
        tooltip: str = "",
    ) -> Gtk.CheckButton:
        row = group.line(hint=hint)
        button = self._check_widget(key, text, default, tooltip)
        row.pack_start(button, False, False, 0)
        return button

    def _combo_widget(
        self, key: str, options: dict, default: str = ""
    ) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        labels = list(options)
        values = list(options.values())
        for name in labels:
            combo.append_text(name)
        current = self.app.cfg.get(key, default)
        combo.set_active(values.index(current) if current in values else 0)
        self._combos[key] = (combo, dict(options))
        self._keys.append(key)
        return combo

    def _combo(self, group: _Group, key: str, label: str, options: dict, default: str = "") -> None:
        row = group.line(label)
        row.pack_start(self._combo_widget(key, options, default), False, False, 0)

    def _spin_button(
        self, key: str, low: int, high: int, step: int, width: int = 7
    ) -> Gtk.SpinButton:
        adjustment = Gtk.Adjustment(
            value=int(self.app.cfg.get(key, _default(key, low))),
            lower=low,
            upper=high,
            step_increment=step,
            page_increment=step * 10,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=step, digits=0)
        spin.set_numeric(True)
        spin.set_width_chars(width)
        self._spins[key] = spin
        self._keys.append(key)
        return spin

    def _spin(
        self,
        group: _Group,
        key: str,
        label: str,
        low: int,
        high: int,
        step: int,
        hint: str = "",
        width: int = 7,
    ) -> None:
        row = group.line(label, hint)
        row.pack_start(self._spin_button(key, low, high, step, width), False, False, 0)

    def _entry_widget(self, key: str, width: int = 34) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_width_chars(width)
        entry.set_text(str(self.app.cfg.get(key, "")))
        self._entries[key] = entry
        self._keys.append(key)
        return entry

    def _entry(
        self, group: _Group, key: str, label: str, hint: str = "", width: int = 34
    ) -> None:
        row = group.line(label, hint)
        row.pack_start(self._entry_widget(key, width), False, False, 0)

    def _text(
        self, group: _Group, key: str, title: str, hint: str = "", height: int = 4
    ) -> None:
        group.note(title)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(str(self.app.cfg.get(key, "")))
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, height * 24)
        scrolled.add(view)
        self._texts[key] = view
        self._keys.append(key)
        group.span(scrolled, hint)

    # ------------------------------------------------------------ 控件读写

    def _value_of(self, key: str):
        if key in self._combos:
            combo, options = self._combos[key]
            current = combo.get_active_text()
            if current in options:
                return options[current]
            return next(iter(options.values()), "")
        if key in self._checks:
            return bool(self._checks[key].get_active())
        if key in self._spins:
            return int(self._spins[key].get_value_as_int())
        if key in self._entries:
            return self._entries[key].get_text()
        buffer = self._texts[key].get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()

    def _set_value(self, key: str, value) -> None:
        if key in self._combos:
            combo, options = self._combos[key]
            for index, name in enumerate(options):
                if options[name] == value:
                    combo.set_active(index)
                    return
            combo.set_active(0)
        elif key in self._checks:
            self._checks[key].set_active(bool(value))
        elif key in self._spins:
            self._spins[key].set_value(int(value if value is not None else 0))
        elif key in self._entries:
            self._entries[key].set_text("" if value is None else str(value))
        else:
            self._texts[key].get_buffer().set_text("" if value is None else str(value))

    def _collect(self) -> dict:
        """当前控件值 + 界面上没提到的字段 = 一份完整配置。"""
        cfg = dict(config.DEFAULTS)
        cfg.update(FALLBACK_DEFAULTS)
        cfg.update(self.app.cfg)
        for key in self._keys:
            cfg[key] = self._value_of(key)
        cfg["api_key"] = str(cfg.get("api_key", "")).strip()
        cfg["hold_key"] = self.specs["hold"]
        cfg["toggle_key"] = self.specs["toggle"]
        cfg["endpoint"] = "stream" if cfg.get("live_typing") else "nostream"
        active = self.device_combo.get_active()
        cfg["device"] = active - 1 if active > 0 else -1
        return cfg

    # ------------------------------------------------------------ 各标签页

    def _tab_api(self, page: Gtk.Box) -> None:
        group = _Group(page, "账号与模型")
        self._entry(group, "api_key", "API Key", "控制台 > API Key 管理", width=38)
        self._combo(group, "resource_id", "资源 ID", config.RESOURCE_IDS, "")
        self._combo(group, "language", "识别语种", config.LANGUAGES, "zh-CN")

        group2 = _Group(page, "设备")
        row = group2.line("麦克风")
        combo = Gtk.ComboBoxText()
        for name in self.devices:
            combo.append_text(name)
        combo.set_active(self._device_index(self.app.cfg.get("device", -1)))
        row.pack_start(combo, False, False, 0)
        self.device_combo = combo

        self._note(
            page,
            "识别语种选「自动识别」时，模型自己判断语种；\n指定普通话可以得到更稳的结果。",
        )

    def _tab_keys(self, page: Gtk.Box) -> None:
        group = _Group(page, "录音键")
        self._key_line(group, "hold", "长按键", "按住说话，松开就停")
        self._key_line(group, "toggle", "切换键", "按一下开始，再按一下结束")

        group2 = _Group(page, "按键归属")
        check = self._check(
            group2,
            "share_keys",
            "不独占按键",
            hint="Linux 上无效",
            default=True,
            tooltip=SHARE_KEYS_TOOLTIP,
        )
        check.set_sensitive(False)
        note = group2.note(SHARE_KEYS_TOOLTIP)
        note.set_tooltip_text(SHARE_KEYS_TOOLTIP)
        group2.note("按住录音键期间长按产生的自动连发会被挡掉，不会往输入框刷一串。")
        group2.note(FN_NOTE)

    def _tab_record(self, page: Gtk.Box) -> None:
        group = _Group(page, "输出方式")
        self._check(
            group, "live_typing", "边说边出字", hint="实时打进光标处（走流式接口）", default=True
        )
        self._combo(group, "output_mode", "非实时时", config.OUTPUT_MODES, "paste")
        self._spin(
            group, "live_interval_ms", "上屏间隔", 80, 2000, 50,
            "毫秒。越小越跟手，越大写的次数越少（每次上屏都要抢一次键盘焦点）",
        )
        group.note("「逐字键入」在开着中文输入法时会被输入法吃掉（字母变成拼音候选），日常用「粘贴到光标处」。")
        group.note("Linux 上所有输出都靠剪贴板 + Ctrl+V，所以识别过程中剪贴板会被临时占用。")

        group2 = _Group(page, "结束方式")
        row = group2.line()
        row.pack_start(
            self._check_widget("auto_stop", "静音自动停止", default=True), False, False, 0
        )
        row.pack_start(Gtk.Label(label="静音"), False, False, 0)
        row.pack_start(self._spin_button("silence_ms", 400, 8000, 100, width=6), False, False, 0)
        row.pack_start(Gtk.Label(label="毫秒"), False, False, 0)
        row.pack_start(_dim_label("（长按模式不生效）"), False, False, 0)
        self._spin(group2, "max_seconds", "最长录音", 5, 1800, 30, "秒（兜底保护）")

        group3 = _Group(page, "灵敏度")
        row = group3.line("静音阈值", "低于它算静音")
        row.pack_start(self._spin_button("vad_threshold", 50, 20000, 50, width=8), False, False, 0)
        calibrate = Gtk.Button(label="检测环境噪声")
        calibrate.connect("clicked", self._on_calibrate)
        row.pack_start(calibrate, False, False, 0)
        self.calibration = _dim_label("")
        row.pack_start(self.calibration, False, False, 0)

        group4 = _Group(page, "剪贴板与通知")
        self._check(
            group4,
            "restore_clipboard",
            "说完还原原来的剪贴板",
            hint="边说边出字会反复占用剪贴板，说完把原内容放回去",
            default=True,
        )
        self._combo(group4, "paste_shortcut", "粘贴快捷键", config.PASTE_SHORTCUTS, "auto")
        group4.note(
            "自动识别靠无障碍接口（AT-SPI）问「现在焦点窗口是谁」：命中终端名单就发 Ctrl+Shift+V，"
            "否则 Ctrl+V；查不出来时回退到这里的固定选择。"
            "本机常见的终端（ptyxis、gnome-terminal、kitty、konsole…）都在名单里。"
        )
        self._check(group4, "notify", "同时发系统通知", hint="识别完成后额外发一条通知")

        group5 = _Group(page, "界面")
        self._check(group5, "show_overlay", "录音时桌面显示浮标", default=True)
        self._check(group5, "always_on_top", "主界面置顶", default=True)

    def _tab_recognize(self, page: Gtk.Box) -> None:
        group = _Group(page, "文本处理")
        self._check(group, "enable_punc", "智能标点", default=True)
        self._check(
            group,
            "enable_itn",
            "数字规范化",
            hint="把「一九七零年」写成「1970 年」",
            default=True,
        )
        self._check(
            group,
            "enable_ddc",
            "语义顺滑",
            hint="会连「呀、啊、呢、吧」这类语气词一起删掉",
        )

        group2 = _Group(page, "语种")
        self._check(group2, "enable_auto_lang", "自动识别语种", hint="与热词/上下文不能同时用")
        self._check(
            group2, "enable_lid", "中英文及方言识别", hint="普通话、粤语、四川话等自动判断"
        )

        group3 = _Group(page, "输出形态")
        self._combo(group3, "output_zh_variant", "繁体输出", ZH_VARIANTS, "")
        self._combo(group3, "result_type", "结果返回方式", RESULT_TYPES, "full")
        self._check(group3, "enable_nonstream", "流式二遍识别", hint="仅流式接口有效，更准但更慢")

        group4 = _Group(page, "附加信息", "开启后会一并返回，副作用是数据量变大")
        self._check(group4, "show_utterances", "输出分句信息")
        self._check(group4, "enable_speaker_info", "说话人分离", hint="自动开启分句")
        self._check(group4, "enable_emotion_detection", "情绪检测")
        self._check(group4, "enable_gender_detection", "性别检测")
        self._check(group4, "enable_age_detection", "年龄检测")

    def _tab_advanced(self, page: Gtk.Box) -> None:
        group = _Group(page, "端点检测", "0 表示不指定，用服务端默认值")
        self._spin(group, "end_window_size", "判停静音阈值", 0, 5000, 100, "ms，默认 800")
        self._spin(group, "vad_segment_duration", "分句静音阈值", 0, 10000, 100, "ms，默认 3000")
        self._spin(group, "force_to_speech_time", "起始强制有声", 0, 5000, 100, "ms，防开头被切")

        group2 = _Group(page, "热词与上下文", "提高专有名词的识别率")
        self._text(
            group2,
            "hotwords",
            "热词（每行一个）",
            "例如：豆包语音输入 / 火山引擎，最多 5000 词",
            height=3,
        )
        self._text(
            group2,
            "context_text",
            "对话上下文（可留空）",
            "把上一句对话贴进来，帮助模型理解语境",
            height=3,
        )

        group3 = _Group(page, "敏感词过滤")
        self._check(group3, "sensitive_system", "使用系统内置词库", hint="命中的词替换成 *")
        self._entry(group3, "sensitive_empty", "替换为空", "逗号分隔", width=30)
        self._entry(group3, "sensitive_signed", "替换为 *", "逗号分隔", width=30)

        group4 = _Group(page, "领域推荐词")
        self._check(group4, "enable_poi_fc", "地名优先（POI）")
        self._check(group4, "enable_music_fc", "音乐优先（Music）")

    # ------------------------------------------------------------ 交互

    def _device_index(self, device) -> int:
        try:
            index = int(device) + 1
        except (TypeError, ValueError):
            index = 0
        if not self.devices:
            return -1
        return min(max(index, 0), len(self.devices) - 1)

    def _key_line(self, group: _Group, slot: str, label: str, hint: str) -> None:
        row = group.line(label, hint)
        name = Gtk.Label(xalign=0)
        name.set_size_request(150, -1)
        spec = Gtk.Entry()
        spec.set_width_chars(10)
        spec.set_editable(False)
        spec.set_can_focus(False)
        spec.set_tooltip_text("配置里保存的按键编码（修饰键:按键码）")
        button = Gtk.Button(label="按下按键…")
        button.connect("clicked", lambda _widget: self._capture(slot))
        row.pack_start(name, False, False, 0)
        row.pack_start(spec, False, False, 0)
        row.pack_start(button, False, False, 0)
        self._key_names[slot] = name
        self._key_specs[slot] = spec
        self._key_buttons[slot] = button
        self._show_spec(slot)

    def _show_spec(self, slot: str) -> None:
        spec = self.specs.get(slot, "")
        self._key_specs[slot].set_text(spec)
        self._key_names[slot].set_text(keyhook.key_label(*keyhook.parse_spec(spec)))

    def _capture(self, slot: str) -> None:
        self._key_buttons[slot].set_label("请按键…")
        self._key_names[slot].set_text("等待按键…")
        self._set_status("")
        self.app.capture_key(slot)

    def _on_calibrate(self, _widget) -> None:
        self.calibration.set_text("请保持安静…")
        self.app.calibrate()

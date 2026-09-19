"""设置面板。

参数较多，按官方接口文档分成几个标签页。注意一个坑：主窗口是隐藏的（root 被
withdraw），此时**不能**对本面板调用 transient(root)——Tk 会把 transient 的子窗口
跟着父窗口一起隐藏，结果就是面板永远打不开（实测几何一直是 1x1、viewable=False）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import config, keyhook
from .recorder import list_input_devices

FONT = "Microsoft YaHei UI"
HINT_COLOR = "#888"

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


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.devices = list_input_devices()
        self.specs = {
            "hold": app.cfg.get("hold_key", ""),
            "toggle": app.cfg.get("toggle_key", ""),
        }
        self.vars: dict[str, tk.Variable] = {}
        self.combos: dict[str, tuple[list[str], list]] = {}
        self.texts: dict[str, tk.Text] = {}
        self._field: dict[str, tk.Widget] = {}

        self.title("豆包语音输入 · 设置")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._tab_api(notebook)
        self._tab_keys(notebook)
        self._tab_record(notebook)
        self._tab_recognize(notebook)
        self._tab_advanced(notebook)

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=12, pady=(8, 12))
        self.status = ttk.Label(footer, text="", foreground="#1a73e8", wraplength=430)
        self.status.pack(side="left")
        ttk.Button(footer, text="取消", width=8, command=self.destroy).pack(side="right")
        ttk.Button(footer, text="保存", width=8, command=self.save).pack(side="right", padx=(0, 8))

        self.update_idletasks()
        self._center()
        self.lift()
        self.focus_force()
        # grab 要等窗口真正显示出来再拿，否则可能把整个程序锁死
        self.after(150, self._grab)

    # ------------------------------------------------------------ 布局工具

    def _grab(self) -> None:
        try:
            if self.winfo_viewable():
                self.grab_set()
        except tk.TclError:
            pass

    def _center(self) -> None:
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _page(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        return frame

    def _group(self, page: ttk.Frame, title: str, hint: str = "") -> ttk.Frame:
        box = ttk.LabelFrame(page, text=title)
        box.pack(fill="x", padx=10, pady=(8, 0))
        if hint:
            ttk.Label(box, text=hint, foreground=HINT_COLOR).pack(anchor="w", padx=8, pady=(2, 0))
        return box

    def _line(self, box: ttk.Frame, label: str = "", hint: str = "") -> ttk.Frame:
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=3)
        if label:
            ttk.Label(row, text=label, width=14, anchor="w").pack(side="left")
        if hint:
            ttk.Label(row, text=hint, foreground=HINT_COLOR).pack(side="right")
        return row

    def _check(self, box, key: str, text: str, hint: str = "", default: bool = False) -> None:
        var = tk.BooleanVar(value=bool(self.app.cfg.get(key, default)))
        self.vars[key] = var
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(row, text=text, variable=var).pack(side="left")
        if hint:
            ttk.Label(row, text="  " + hint, foreground=HINT_COLOR).pack(side="left")

    def _combo(self, box, key: str, label: str, options: dict, default: str = "") -> None:
        labels = list(options)
        values = list(options.values())
        current = self.app.cfg.get(key, default)
        var = tk.StringVar(value=labels[values.index(current)] if current in values else labels[0])
        self.vars[key] = var
        self.combos[key] = (labels, values)
        row = self._line(box, label)
        ttk.Combobox(row, textvariable=var, values=labels, state="readonly", width=22).pack(
            side="left"
        )

    def _spin(self, box, key: str, label: str, low: int, high: int, step: int, hint: str = "") -> None:
        var = tk.IntVar(value=int(self.app.cfg.get(key, low)))
        self.vars[key] = var
        row = self._line(box, label, hint)
        ttk.Spinbox(row, from_=low, to=high, increment=step, width=7, textvariable=var).pack(
            side="left"
        )

    def _entry(self, box, key: str, label: str, hint: str = "", width: int = 34) -> None:
        var = tk.StringVar(value=str(self.app.cfg.get(key, "")))
        self.vars[key] = var
        row = self._line(box, label, hint)
        ttk.Entry(row, textvariable=var, width=width).pack(side="left")

    def _text(self, box, key: str, title: str, hint: str, height: int = 4) -> None:
        ttk.Label(box, text=title).pack(anchor="w", padx=8, pady=(6, 0))
        widget = tk.Text(box, height=height, width=46, wrap="word", relief="solid", borderwidth=1)
        widget.pack(fill="x", padx=8, pady=(2, 6))
        widget.insert("1.0", str(self.app.cfg.get(key, "")))
        self.texts[key] = widget
        if hint:
            ttk.Label(box, text=hint, foreground=HINT_COLOR, wraplength=430).pack(
                anchor="w", padx=8, pady=(0, 4)
            )

    # ------------------------------------------------------------ 各标签页

    def _tab_api(self, notebook) -> None:
        page = self._page(notebook, "接口")
        box = self._group(page, "账号与模型")
        self._entry(box, "api_key", "API Key", "控制台 > API Key 管理", width=38)
        self._combo(box, "resource_id", "资源 ID", config.RESOURCE_IDS, "")
        self._combo(box, "language", "识别语种", config.LANGUAGES, "zh-CN")

        box2 = self._group(page, "设备")
        var = tk.StringVar(
            value=self.devices[
                min(max(int(self.app.cfg.get("device", -1)) + 1, 0), len(self.devices) - 1)
            ]
        )
        self.vars["__device__"] = var
        row = self._line(box2, "麦克风")
        ttk.Combobox(row, textvariable=var, values=self.devices, state="readonly", width=30).pack(
            side="left"
        )

        ttk.Label(
            page,
            text="识别语种选「自动识别」时，模型自己判断语种；\n"
                 "指定普通话可以得到更稳的结果。",
            foreground=HINT_COLOR,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(8, 0))

    def _tab_keys(self, notebook) -> None:
        page = self._page(notebook, "按键")
        box = self._group(page, "录音键")

        self.hold_label = tk.StringVar(value=self._label("hold"))
        self.toggle_label = tk.StringVar(value=self._label("toggle"))

        row = self._line(box, "长按键", "按住说话，松开就停")
        ttk.Label(row, textvariable=self.hold_label, width=16).pack(side="left")
        ttk.Button(row, text="按下按键…", width=11, command=lambda: self._capture("hold")).pack(
            side="left"
        )

        row = self._line(box, "切换键", "按一下开始，再按一下结束")
        ttk.Label(row, textvariable=self.toggle_label, width=16).pack(side="left")
        ttk.Button(row, text="按下按键…", width=11, command=lambda: self._capture("toggle")).pack(
            side="left"
        )

        box2 = self._group(page, "按键归属")
        self._check(box2, "share_keys", "不独占按键", "其他软件照样收到录音键", default=True)
        ttk.Label(
            page,
            text="不独占时，按住录音键期间其他软件也会收到一次该按键；\n"
                 "长按产生的连发会被挡掉，不会往输入框刷一串。",
            foreground=HINT_COLOR,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(8, 0))

    def _tab_record(self, notebook) -> None:
        page = self._page(notebook, "录音")
        box = self._group(page, "输出方式")
        self._check(box, "live_typing", "边说边出字", "实时打进光标处（走流式接口）", default=True)
        self._combo(box, "output_mode", "非实时时", config.OUTPUT_MODES, "paste")

        box2 = self._group(page, "结束方式")
        auto = tk.BooleanVar(value=bool(self.app.cfg.get("auto_stop", True)))
        self.vars["auto_stop"] = auto
        row = ttk.Frame(box2)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="静音自动停止", variable=auto).pack(side="left")
        ttk.Label(row, text="静音").pack(side="left", padx=(10, 2))
        silence = tk.IntVar(value=int(self.app.cfg.get("silence_ms", 1200)))
        self.vars["silence_ms"] = silence
        ttk.Spinbox(row, from_=400, to=8000, increment=100, width=6, textvariable=silence).pack(
            side="left"
        )
        ttk.Label(row, text="毫秒").pack(side="left", padx=(2, 0))
        ttk.Label(row, text="（长按模式不生效）", foreground=HINT_COLOR).pack(side="left", padx=(8, 0))
        self._spin(box2, "max_seconds", "最长录音", 5, 1800, 30, "秒（兜底保护）")

        box3 = self._group(page, "灵敏度")
        row = self._line(box3, "静音阈值", "低于它算静音")
        threshold = tk.IntVar(value=int(self.app.cfg.get("vad_threshold", 200)))
        self.vars["vad_threshold"] = threshold
        ttk.Spinbox(row, from_=50, to=20000, increment=50, width=8, textvariable=threshold).pack(
            side="left"
        )
        ttk.Button(row, text="检测环境噪声", command=self.app.calibrate).pack(side="left", padx=8)
        self.calibration = ttk.Label(row, text="", foreground="#1a73e8")
        self.calibration.pack(side="left")

        box4 = self._group(page, "界面")
        self._check(box4, "show_overlay", "录音时桌面显示浮标", default=True)
        self._check(box4, "always_on_top", "主界面置顶", default=True)

    def _tab_recognize(self, notebook) -> None:
        page = self._page(notebook, "识别")
        box = self._group(page, "文本处理")
        self._check(box, "enable_punc", "智能标点", default=True)
        self._check(box, "enable_itn", "数字规范化", "把「一九七零年」写成「1970 年」", default=True)
        self._check(
            box, "enable_ddc", "语义顺滑",
            "会连「呀、啊、呢、吧」这类语气词一起删掉",
        )

        box2 = self._group(page, "语种")
        self._check(box2, "enable_auto_lang", "自动识别语种", "与热词/上下文不能同时用")
        self._check(box2, "enable_lid", "中英文及方言识别", "普通话、粤语、四川话等自动判断")

        box3 = self._group(page, "输出形态")
        self._combo(box3, "output_zh_variant", "繁体输出", ZH_VARIANTS, "")
        self._combo(box3, "result_type", "结果返回方式", RESULT_TYPES, "full")
        self._check(box3, "enable_nonstream", "流式二遍识别", "仅流式接口有效，更准但更慢")

        box4 = self._group(page, "附加信息", "开启后会一并返回，副作用是数据量变大")
        self._check(box4, "show_utterances", "输出分句信息")
        self._check(box4, "enable_speaker_info", "说话人分离", "自动开启分句")
        self._check(box4, "enable_emotion_detection", "情绪检测")
        self._check(box4, "enable_gender_detection", "性别检测")
        self._check(box4, "enable_age_detection", "年龄检测")

    def _tab_advanced(self, notebook) -> None:
        page = self._page(notebook, "高级")
        box = self._group(page, "端点检测", "0 表示不指定，用服务端默认值")
        self._spin(box, "end_window_size", "判停静音阈值", 0, 5000, 100, "ms，默认 800")
        self._spin(box, "vad_segment_duration", "分句静音阈值", 0, 10000, 100, "ms，默认 3000")
        self._spin(box, "force_to_speech_time", "起始强制有声", 0, 5000, 100, "ms，防开头被切")

        box2 = self._group(page, "热词与上下文", "提高专有名词的识别率")
        self._text(
            box2, "hotwords", "热词（每行一个）",
            "例如：豆包语音输入 / 火山引擎，最多 5000 词",
            height=3,
        )
        self._text(
            box2, "context_text", "对话上下文（可留空）",
            "把上一句对话贴进来，帮助模型理解语境",
            height=3,
        )

        box3 = self._group(page, "敏感词过滤")
        self._check(box3, "sensitive_system", "使用系统内置词库", "命中的词替换成 *")
        self._entry(box3, "sensitive_empty", "替换为空", "逗号分隔", width=30)
        self._entry(box3, "sensitive_signed", "替换为 *", "逗号分隔", width=30)

        box4 = self._group(page, "领域推荐词")
        self._check(box4, "enable_poi_fc", "地名优先（POI）")
        self._check(box4, "enable_music_fc", "音乐优先（Music）")

    # ------------------------------------------------------------ 交互

    def _label(self, slot: str) -> str:
        return keyhook.key_label(*keyhook.parse_spec(self.specs.get(slot, "")))

    def _capture(self, slot: str) -> None:
        (self.hold_label if slot == "hold" else self.toggle_label).set("请按下按键…")
        self.status.configure(text="")
        self.app.capture_key(slot)

    def apply_key_capture(self, slot: str, spec: str) -> None:
        self.specs[slot] = spec
        label = keyhook.key_label(*keyhook.parse_spec(spec))
        (self.hold_label if slot == "hold" else self.toggle_label).set(label)
        if "未知键" in label:
            self.status.configure(text="已设为未知键，可能是 Fn；能用就用")

    def apply_calibration(self, noise_rms: float) -> None:
        threshold = max(80, int(noise_rms * 3))
        self.vars["vad_threshold"].set(threshold)
        self.calibration.configure(text=f"环境噪声 {noise_rms:.0f}，已填入 {threshold}")

    def save(self) -> None:
        cfg = dict(self.app.cfg)
        for key, var in self.vars.items():
            if key.startswith("__"):
                continue
            if key in self.combos:
                labels, values = self.combos[key]
                cfg[key] = values[labels.index(var.get())]
            elif isinstance(var, tk.BooleanVar):
                cfg[key] = bool(var.get())
            elif isinstance(var, tk.IntVar):
                cfg[key] = int(var.get())
            else:
                cfg[key] = var.get()
        for key, widget in self.texts.items():
            cfg[key] = widget.get("1.0", "end-1c").strip()

        cfg["api_key"] = str(cfg.get("api_key", "")).strip()
        cfg["hold_key"] = self.specs["hold"]
        cfg["toggle_key"] = self.specs["toggle"]
        cfg["endpoint"] = "stream" if cfg.get("live_typing") else "nostream"
        try:
            cfg["device"] = self.devices.index(self.vars["__device__"].get()) - 1
        except (ValueError, KeyError):
            cfg["device"] = -1

        if not cfg["api_key"]:
            self.status.configure(text="请先填 API Key")
            return
        for slot, spec in self.specs.items():
            if not keyhook.parse_spec(spec)[1]:
                self.status.configure(text="「长按键」和「切换键」都要按一次按键")
                return
        if cfg.get("enable_auto_lang") and (cfg.get("hotwords") or cfg.get("context_text")):
            self.status.configure(text="自动识别语种和热词/上下文不能同时用，请去掉一个")
            return

        self.app.apply_config(cfg)
        self.destroy()

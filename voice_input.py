#!/usr/bin/env python3
"""豆包语音输入 —— 常驻后台的语音转文字小工具（Linux 版）。

默认以托盘程序形态启动：没有主窗口，按住录音键说话，字实时打进光标处。
加上 --file 可在命令行里识别一个 wav 文件，用来验证 API Key 是否可用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doubao_voice import asr, config  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="voice_input",
        description="豆包语音输入：把麦克风说的话变成文字",
    )
    parser.add_argument("--file", metavar="WAV", help="识别本地 wav 文件并把结果打印到终端")
    parser.add_argument("--api-key", help="临时指定 API Key")
    parser.add_argument("--resource-id", help="临时指定资源 ID")
    parser.add_argument("--language", help="识别语种，例如 zh-CN")
    parser.add_argument("--endpoint", choices=["nostream", "stream"], help="识别接口")
    parser.add_argument("--list-devices", action="store_true", help="列出麦克风设备后退出")
    parser.add_argument(
        "--detect-key", action="store_true",
        help="按一下按键就把它设为录音键（用来设 Fn 这类键）",
    )
    parser.add_argument(
        "--as", dest="key_slot", choices=("hold", "toggle"), default="hold",
        help="--detect-key 要把捕获到的键设成哪个位置，默认长按键",
    )
    parser.add_argument(
        "--key-timeout", type=float, default=180.0, help="--detect-key 等待按键的秒数"
    )
    parser.add_argument(
        "--restart", action="store_true", help="--detect-key 完成后重启常驻程序"
    )
    parser.add_argument(
        "--watch-keys", action="store_true",
        help="实时显示按下的每个按键，用来判断某个键到底有没有进系统",
    )
    parser.add_argument(
        "--seconds", type=float, default=120.0, help="--watch-keys 监视的秒数"
    )
    parser.add_argument("--show-config", action="store_true", help="打印当前配置后退出")
    parser.add_argument("--self-test", action="store_true", help="检查权限、设备和剪贴板后退出")
    parser.add_argument(
        "--type-test", action="store_true",
        help="把两段样例文字打进当前光标位置，用来验证按键注入链路",
    )

    # 控制正在运行的实例
    parser.add_argument("--show", action="store_true", help="让正在运行的实例显示主界面")
    parser.add_argument("--settings", action="store_true", help="让正在运行的实例打开设置")
    parser.add_argument("--toggle", action="store_true", help="让正在运行的实例开始/结束录音")
    parser.add_argument("--quit", action="store_true", help="让正在运行的实例退出")
    parser.add_argument("--status", action="store_true", help="查看正在运行的实例的状态")
    return parser.parse_args(argv)


def read_wav(path: str) -> tuple[int, int, int, bytes]:
    """读取 wav，返回 (声道数, 位深, 采样率, 纯 PCM 数据)。"""
    if not os.path.isfile(path):
        raise SystemExit(f"找不到文件：{path}")
    try:
        with wave.open(path, "rb") as handle:
            if handle.getcomptype() != "NONE":
                raise SystemExit("只支持未压缩的 PCM wav，请先用工具转成 16kHz 单声道 wav")
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise SystemExit(f"无法读取 wav 文件：{exc}") from exc
    if width != 2:
        raise SystemExit("只支持 16 位采样，请先用工具转成 16kHz 单声道 16bit wav")
    return channels, width * 8, rate, frames


def run_file(cfg: dict, path: str) -> int:
    if not (cfg.get("api_key") or "").strip():
        print("缺少 API Key：请先运行程序在「设置」里填写，或加 --api-key 参数。", file=sys.stderr)
        return 2

    channels, bits, rate, pcm = read_wav(path)

    seconds = len(pcm) / max(1, rate * channels * (bits // 8))
    print(f"音频：{path}（{rate}Hz / {channels} 声道 / {bits}bit，约 {seconds:.1f} 秒）")
    print(f"接口：{cfg['endpoint']}  资源：{cfg['resource_id']}  语种：{cfg.get('language') or '自动'}")
    print("识别中…")

    # 只送纯 PCM（不带 WAV 头），两个接口都接受这种声明方式
    client = asr.AsrClient(cfg, audio_format="pcm", sample_rate=rate, bits=bits, channels=channels)
    source = asr.FileSource(pcm, segment_bytes=rate * channels * (bits // 8) // 5)
    try:
        text = client.transcribe(source)
    except asr.AsrError as exc:
        print(f"识别失败：{exc}", file=sys.stderr)
        return 1

    if text.strip():
        print(f"\n识别结果：{text}")
        return 0
    print("\n没有识别到内容。", file=sys.stderr)
    return 1


def run_type_test() -> int:
    """把两段样例文字真的打进当前光标位置，用来验证注入链路（要装完权限才有用）。"""
    from doubao_voice import linuxutil

    injector = linuxutil.KeyInjector()
    try:
        injector.open()
    except RuntimeError as exc:
        print(f"没法开始：{exc}", file=sys.stderr)
        return 1

    print("3 秒后会把两段文字打进你**当前光标所在的窗口**：")
    print("  1) 逐字键入：doubao-voice typing test")
    print("  2) 剪贴板粘贴：豆包语音输入中文测试")
    print("想取消就把光标点到别处，或者按 Ctrl+C。")
    try:
        time.sleep(3)
        injector.type_text("doubao-voice typing test ")
        linuxutil.set_clipboard_text("豆包语音输入中文测试")
        injector.paste(shift=False)
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        injector.close()
    print("已发送。上一步那个窗口里应该出现了这两段文字；没有的话看 --self-test 的输出。")
    return 0


def run_self_test(cfg: dict) -> int:
    from doubao_voice import keyhook, linuxutil, recorder

    ok = True
    print("== 环境 ==")
    print(f"  会话类型：{linuxutil.session_type()} / 桌面 {os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    print(f"  配置文件：{config.CONFIG_PATH}")

    print("== 设备权限 ==")
    problems = linuxutil.missing_permissions()
    if problems:
        ok = False
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"  → {linuxutil.setup_hint()}")
    else:
        print("  ✓ uinput / /dev/input / wl-clipboard 都可用")

    print("== 键盘 ==")
    devices = keyhook.keyboard_devices()
    if not devices:
        ok = False
        print("  ✗ 没找到键盘设备")
    for path in devices:
        print(f"  · {path}  {keyhook._device_name(path)}")
    spec = keyhook.parse_spec(cfg.get("hold_key", ""))
    print(f"  长按键：{keyhook.key_label(*spec)}   切换键："
          f"{keyhook.key_label(*keyhook.parse_spec(cfg.get('toggle_key', '')))}")

    print("== 焦点窗口（决定粘贴用 Ctrl+V 还是 Ctrl+Shift+V）==")
    from doubao_voice import engine as engine_mod
    from doubao_voice import windowinfo

    print(f"  {windowinfo.describe()}")
    chord = "Ctrl+Shift+V" if engine_mod.resolve_paste_shift(cfg) else "Ctrl+V"
    print(f"  当前配置 paste_shortcut={cfg.get('paste_shortcut', 'auto')!r} → 会用 {chord}")
    print(f"  终端名单命中示例：ptyxis={windowinfo.looks_like_terminal('ptyxis')}，"
          f"kitty={windowinfo.looks_like_terminal('kitty')}")

    print("== 麦克风 ==")
    for index, name in enumerate(recorder.list_input_devices()):
        print(f"  [{index - 1:>2}] {name}")
    try:
        noise = recorder.measure_noise(device=cfg.get("device", -1), seconds=2.0)
        if noise > 0:
            print(f"  环境噪声 RMS ≈ {noise:.0f}（当前阈值 {cfg.get('vad_threshold')}）")
        else:
            print("  没测到有效样本（可能撞上采集流启动的爆音），稍后重跑一次")
    except Exception as exc:
        ok = False
        print(f"  ✗ 录音失败：{exc}")

    print("== 按键注入 ==")
    from doubao_voice.linuxutil import KeyInjector

    injector = KeyInjector()
    try:
        injector.open()
        print("  ✓ 虚拟键盘创建成功（uinput 可用）")
    except RuntimeError as exc:
        ok = False
        print(f"  ✗ {exc}")
    finally:
        injector.close()

    print("== 剪贴板 ==")
    try:
        linuxutil.set_clipboard_text("豆包语音输入自检")
        back = linuxutil.get_clipboard_text()
        if back == "豆包语音输入自检":
            print("  ✓ wl-copy / wl-paste 正常")
        else:
            ok = False
            print(f"  ✗ 写进去又读回来不一致：{back!r}")
    except Exception as exc:
        ok = False
        print(f"  ✗ {exc}")

    print("== 常驻实例 ==")
    from doubao_voice import instance

    status = instance.send("status")
    print(f"  {'运行中：' + status if status else '当前没有实例在跑'}")

    print("\n结论：" + ("全部通过，可以正常使用。" if ok else "有项目没通过，按上面的提示处理后重跑本命令。"))
    return 0 if ok else 1


def run_control(args) -> int | None:
    from doubao_voice import instance

    command = None
    for flag, name in (("quit", "quit"), ("show", "show"), ("settings", "settings"),
                       ("toggle", "toggle"), ("status", "status")):
        if getattr(args, flag):
            command = name
            break
    if command is None:
        return None

    reply = instance.send(command)
    if reply is None:
        print("没有正在运行的实例。直接运行 python3 voice_input.py 启动它。", file=sys.stderr)
        return 1
    if command == "status":
        print(reply)
    else:
        print("已发送指令：" + command)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = config.load()
    if args.api_key:
        cfg["api_key"] = args.api_key.strip()
    if args.resource_id:
        cfg["resource_id"] = args.resource_id.strip()
    if args.language is not None:
        cfg["language"] = args.language.strip()
    if args.endpoint:
        cfg["endpoint"] = args.endpoint

    control = run_control(args)
    if control is not None:
        return control

    if args.type_test:
        return run_type_test()

    if args.self_test:
        return run_self_test(cfg)

    if args.list_devices:
        from doubao_voice.recorder import list_input_devices

        for index, name in enumerate(list_input_devices()):
            print(f"[{index - 1:>2}] {name}")
        return 0

    if args.show_config:
        masked = dict(cfg)
        if masked.get("api_key"):
            masked["api_key"] = masked["api_key"][:6] + "…" + masked["api_key"][-4:]
        print(json.dumps(masked, ensure_ascii=False, indent=2))
        print(f"\n配置文件：{config.CONFIG_PATH}")
        return 0

    if args.file:
        return run_file(cfg, args.file)

    if args.detect_key:
        from doubao_voice import detect

        return detect.cli(cfg, args.key_slot, args.key_timeout, args.restart)

    if args.watch_keys:
        from doubao_voice import detect

        return detect.watch_cli(args.seconds)

    from doubao_voice import ui

    ui.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

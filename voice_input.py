#!/usr/bin/env python3
"""豆包语音输入 —— 常驻后台的 Windows 语音转文字小工具。

默认以托盘程序形态启动：没有主窗口，按住录音键说话，字实时打进光标处。
加上 --file 可在命令行里识别一个 wav 文件，用来验证 API Key 是否可用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doubao_voice import asr, config  # noqa: E402


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
        help="弹出捕获窗口，按一下按键就把它设为录音键（专门用来设 Fn 这类键）",
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
        help="实时显示按下的每个按键，用来判断 Fn 之类按键到底有没有进 Windows",
    )
    parser.add_argument(
        "--seconds", type=float, default=120.0, help="--watch-keys 监视的秒数"
    )
    parser.add_argument("--show-config", action="store_true", help="打印当前配置后退出")
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

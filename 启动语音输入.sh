#!/usr/bin/env bash
# 双击/命令行启动，等价于 python3 voice_input.py
cd "$(dirname "$(readlink -f "$0")")"
exec python3 voice_input.py "$@"

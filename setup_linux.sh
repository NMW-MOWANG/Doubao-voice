#!/usr/bin/env bash
# 一次性放开「豆包语音输入」需要的设备权限（需要 root）。
#
#   sudo bash setup_linux.sh          放开权限
#   sudo bash setup_linux.sh --remove 撤销（删掉规则文件）
#
# 为什么需要它（两件事在 Linux 上没有免权限的做法）：
#   1. 读 /dev/input/event*  → 全局热键。Wayland 下没有别的接口能拿到「按键抬起」，
#      而 RegisterHotKey / XGrabKey 那套只对 X11 窗口有效。
#   2. 写 /dev/uinput       → 往当前焦点窗口注入按键（粘贴、退格）。XTEST / xdotool
#      的注入原生 Wayland 窗口收不到。
#
# 用的是 udev 的 uaccess 标签：只给"当前登录会话的那个用户"发 ACL，不需要把用户
# 加进 input 组，也不用重新登录。注销脚本时权限自动收回。
set -euo pipefail

RULES=/etc/udev/rules.d/60-doubao-voice.rules
TARGET_USER="${SUDO_USER:-$(id -un)}"

if [[ $EUID -ne 0 ]]; then
    echo "需要 root：sudo bash setup_linux.sh" >&2
    exit 1
fi

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$RULES"
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=misc --sysname-match=uinput || true
    udevadm trigger --subsystem-match=input --action=change || true
    echo "已删除 $RULES，设备权限恢复原样。"
    exit 0
fi

echo "== 1/3 写 udev 规则 =="
cat > "$RULES" <<'EOF'
# 豆包语音输入：读键盘事件（全局热键）+ 写 uinput（按键注入）
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess"
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF
echo "   $RULES"

echo "== 2/3 让规则生效 =="
udevadm control --reload-rules
udevadm trigger --subsystem-match=misc --sysname-match=uinput
udevadm trigger --subsystem-match=input --action=change
sleep 1

echo "== 3/3 检查 =="
ok=1
if [[ -w /dev/uinput ]] || { command -v getfacl >/dev/null && getfacl -p /dev/uinput 2>/dev/null | grep -q "user:$TARGET_USER:"; }; then
    echo "   ✓ /dev/uinput 可以写（按键注入可用）"
else
    echo "   ✗ /dev/uinput 还是不能写。注销再登录一次（或重启）后重跑本脚本即可。"
    ok=0
fi
event_ok=0
for dev in /dev/input/event*; do
    if [[ -r "$dev" ]] || { command -v getfacl >/dev/null && getfacl -p "$dev" 2>/dev/null | grep -q "user:$TARGET_USER:"; }; then
        event_ok=1
        break
    fi
done
if [[ $event_ok -eq 1 ]]; then
    echo "   ✓ /dev/input/event* 可以读（全局热键可用）"
else
    echo "   ✗ /dev/input/event* 还是不能读。注销再登录一次后重跑本脚本即可。"
    ok=0
fi

if ! command -v wl-copy >/dev/null; then
    echo "== 补装 wl-clipboard（剪贴板）=="
    if command -v apt-get >/dev/null; then
        apt-get install -y wl-clipboard || echo "   ! 安装失败，请手动执行：sudo apt install wl-clipboard" >&2
    else
        echo "   ! 请手动安装 wl-clipboard（剪贴板要用它）" >&2
    fi
else
    echo "   ✓ 已有 wl-clipboard"
fi

echo
if [[ $ok -eq 1 ]]; then
    echo "搞定，现在可以用 ${TARGET_USER} 的身份跑 python3 voice_input.py 了。"
    echo "自检：python3 voice_input.py --self-test"
else
    echo "规则已装好，但权限还没到位：注销重新登录（不用重启）后再跑一次本脚本确认。"
fi

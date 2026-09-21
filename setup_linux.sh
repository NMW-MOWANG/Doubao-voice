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

# 浮标（overlayd.py）除了 GTK3，还要 GI 能把 cairo 类型交给 draw 回调。光有 python3-gi
# 不够 —— python3-gi-cairo 不随它一起装，缺了它浮标的清屏（透明）和点击穿透会静默失效：
# 点击穿透那段是 except Exception: pass，清了屏的回调则根本不会被调用，只在 stderr 留一句
# "Couldn't find foreign struct converter for 'cairo.Context'"。
if python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; gi.require_foreign('cairo')" 2>/dev/null; then
    echo "   ✓ 已有 GTK3 + cairo 绑定"
else
    echo "== 补装浮标要用的 GTK3 + cairo 绑定 =="
    if command -v apt-get >/dev/null; then
        apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 ||
            echo "   ! 安装失败，请手动执行：sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0" >&2
    else
        echo "   ! 请手动安装 python3-gi / python3-gi-cairo / gir1.2-gtk-3.0" >&2
    fi
fi

echo
if [[ $ok -eq 1 ]]; then
    echo "搞定，现在可以用 ${TARGET_USER} 的身份跑 python3 voice_input.py 了。"
    echo "自检：python3 voice_input.py --self-test"
else
    echo "规则已装好，但权限还没到位：注销重新登录（不用重启）后再跑一次本脚本确认。"
fi

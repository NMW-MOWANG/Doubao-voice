#!/usr/bin/env bash
# 把「豆包语音输入」装进桌面环境（不需要 root）：
#   * 复制桌面项到 ~/.local/share/applications，应用列表里就能搜到
#   * 加 --autostart 时再装一份自启动（登录后自动常驻后台）
#
#   bash install_launcher.sh              只装桌面项
#   bash install_launcher.sh --autostart  顺便开机自启
#   bash install_launcher.sh --remove     全部撤掉
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
NAME="豆包语音输入.desktop"
PYTHON="$(command -v python3)"

install_entries() {
    mkdir -p "$DESKTOP_DIR"
    cat > "$DESKTOP_DIR/$NAME" <<EOF
[Desktop Entry]
Type=Application
Name=豆包语音输入
GenericName=语音输入法
Comment=按住右 Ctrl 说话，字直接进光标处
Exec=$PYTHON $APP_DIR/voice_input.py
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=false
Actions=Settings;Quit;

[Desktop Action Settings]
Name=打开设置
Exec=$PYTHON $APP_DIR/voice_input.py --settings

[Desktop Action Quit]
Name=退出
Exec=$PYTHON $APP_DIR/voice_input.py --quit
EOF
    echo "已安装桌面项：$DESKTOP_DIR/$NAME"
    command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
}

install_autostart() {
    mkdir -p "$AUTOSTART_DIR"
    cp "$DESKTOP_DIR/$NAME" "$AUTOSTART_DIR/$NAME"
    echo "已设置登录自启：$AUTOSTART_DIR/$NAME"
}

remove_entries() {
    rm -f "$DESKTOP_DIR/$NAME" "$AUTOSTART_DIR/$NAME"
    command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "已移除桌面项和自启动。"
}

case "${1:-}" in
    --autostart) install_entries; install_autostart ;;
    --remove)    remove_entries ;;
    *)           install_entries ;;
esac

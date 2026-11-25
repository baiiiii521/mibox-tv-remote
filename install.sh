#!/bin/bash
# 电视遥控器桥接服务 —— 一键安装/启动（macOS LaunchAgent 或 Linux systemd）
# 用法：bash install.sh            安装并启动（开机自启）
#      bash install.sh stop       停止
#      bash install.sh status     看状态
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || true)"
LABEL="local.tvremote"
PORT="8795"

if [ -z "$PY" ]; then echo "找不到 python3，请先安装 Python 3"; exit 1; fi
if ! command -v adb >/dev/null 2>&1; then
  echo "找不到 adb，请先装：brew install android-platform-tools"; exit 1
fi

OS="$(uname -s)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT="/etc/systemd/system/$LABEL.service"

status_mac() { launchctl list | grep -q "$LABEL" && echo "运行中" || echo "未运行"; }

case "${1:-install}" in
  stop)
    if [ "$OS" = "Darwin" ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$PLIST"
      echo "已停止并移除开机自启"
    else
      sudo systemctl disable --now "$LABEL" || true
      echo "已停止"
    fi
    ;;
  status)
    if [ "$OS" = "Darwin" ]; then status_mac; else systemctl is-active "$LABEL" || true; fi
    curl -s -m 4 "http://127.0.0.1:$PORT/api/status" | head -c 300; echo
    ;;
  install)
    if [ "$OS" = "Darwin" ]; then
      mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
      cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/err.log</string>
</dict>
</plist>
PLISTEOF
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST"
      launchctl kickstart -k "gui/$(id -u)/$LABEL"
      echo "已安装 LaunchAgent：$PLIST"
    else
      sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=TV Remote Bridge
After=network-online.target

[Service]
Type=simple
ExecStart=$PY $DIR/server.py
WorkingDirectory=$DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
      sudo systemctl daemon-reload
      sudo systemctl enable --now "$LABEL"
      echo "已安装 systemd 服务：$UNIT"
    fi
    sleep 2
    IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}')
    echo "手机打开：http://$IP:$PORT"
    ;;
  *)
    echo "用法: bash install.sh [install|stop|status]"; exit 1
    ;;
esac

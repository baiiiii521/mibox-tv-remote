#!/data/data/com.termux/files/usr/bin/bash
# 在 Termux 里一键部署：bash scripts/termux-setup.sh
# 前提：Termux 已装好（建议 GitHub 的 github-debug 版），且项目已放到 $HOME/tv-remote
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/tv-remote}"

echo "== 1/4 安装依赖 =="
pkg update -y >/dev/null 2>&1 || true
pkg install -y python android-tools
# 新版 android-tools 需要较新的 libc++，否则 adb 报 cannot locate symbol
pkg install -y libc++

echo "== 2/4 保持唤醒 =="
command -v termux-wake-lock >/dev/null && termux-wake-lock || true

echo "== 3/4 开机自启（需装 Termux:Boot）=="
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/tvremote.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sleep 20
cd $DEST && nohup python3 server.py >> termux.log 2>&1 &
BOOT
chmod +x "$HOME/.termux/boot/tvremote.sh"

echo "== 4/4 启动服务 =="
cd "$DEST"
pkill -f "^python3 server[.]py$" 2>/dev/null || true
sleep 1
nohup python3 server.py >> termux.log 2>&1 &
sleep 5
tail -3 termux.log

IP=$(ip route get 1 2>/dev/null | awk '{print $NF; exit}')
echo
echo "手机浏览器打开：http://${IP:-127.0.0.1}:8795"
echo
echo "提示："
echo "  1) Android 12+ 需要执行（用电脑 adb 或本机 shell）："
echo "     settings put global settings_enable_monitor_phantom_procs false"
echo "     dumpsys deviceidle whitelist +com.termux"
echo "  2) 想要桌面一键图标：装 Termux:Widget，把 shortcuts/遥控器.sh 放到 ~/.shortcuts/"
echo "     并给 Termux 授予「显示在其他应用上层」权限"

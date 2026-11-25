#!/data/data/com.termux/files/usr/bin/bash
# 一键遥控器：确保桥接服务在跑，然后打开遥控页面
export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export PATH=$PREFIX/bin:/system/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
export TMPDIR=$PREFIX/tmp

LOG="$HOME/tv-remote/shortcut.log"
echo "=== $(date '+%F %T') 图标被点击 ===" >> "$LOG"

# 用 python 探活（Termux 不保证装了 curl，python 一定有）
if ! python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8795/api/status',timeout=2)" >/dev/null 2>&1; then
  echo "服务没在跑，先启动" >> "$LOG"
  cd "$HOME/tv-remote" || exit 1
  nohup python3 server.py >> termux.log 2>&1 &
  sleep 3
fi

termux-open-url "http://127.0.0.1:8795/" >>"$LOG" 2>&1
echo "termux-open-url rc=$?" >> "$LOG"

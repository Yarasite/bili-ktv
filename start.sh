#!/bin/bash
# 启动点歌台：安装依赖、打开大屏播放页，并在运行期间阻止 Mac 休眠。
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8080}"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

TV_URL="http://localhost:${PORT}/tv"
(sleep 1.5; open -na "Google Chrome" --args --new-window --autoplay-policy=no-user-gesture-required "$TV_URL" 2>/dev/null \
  || open "$TV_URL") &

export PORT
exec caffeinate -dims .venv/bin/python server.py

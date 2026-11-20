#!/bin/bash
# 一键启动点歌台 + 内网穿透：输入云服务器 IP 和密码即可通过公网点歌，Ctrl+C 同时关闭两者。
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8080}"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -c "import aiohttp, qrcode, paramiko" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

TV_URL="http://localhost:${PORT}/tv"
(
  for _ in $(seq 1 120); do
    curl -s -o /dev/null "http://localhost:${PORT}/api/info" && break
    sleep 1
  done
  open -na "Google Chrome" --args --new-window --autoplay-policy=no-user-gesture-required "$TV_URL" 2>/dev/null \
    || open "$TV_URL"
) &
OPENER=$!
trap 'kill $OPENER 2>/dev/null || true' EXIT

export PORT
caffeinate -dims .venv/bin/python remote.py "$@"

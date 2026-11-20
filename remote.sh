#!/bin/bash
# 一键内网穿透启动点歌台：输入云服务器公网 IP，自动配置 SSH 反向隧道并启动点歌服务。
# 按 Ctrl+C（或关闭终端窗口）退出时，自动关闭隧道和点歌服务，并提醒关闭云服务器计费。
#
# 用法：./remote.sh [服务器IP]
# 可选环境变量：SSH_USER（默认询问，回车为 root）、SSH_PORT（默认 22）、PORT（本机端口，默认 8080）、
#               REMOTE_PORTS（服务器对外端口候选列表）、NO_BROWSER=1（不自动打开大屏页）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
TUNNEL_PORT="${TUNNEL_PORT:-$((PORT + 1))}"
SSH_PORT="${SSH_PORT:-22}"
# 云防火墙默认放行的端口（22 留给 SSH 本身），按顺序选第一个空闲的
REMOTE_PORTS="${REMOTE_PORTS:-80 8888 6606 443 3389 5900 5901 5902 5903 5904 5905 5906 5907 5908 5909 5910}"
STATE_DIR="$PWD/.tunnel"
KNOWN_HOSTS="$STATE_DIR/known_hosts"
TUNNEL_LOG="$STATE_DIR/tunnel.log"
KEY="$HOME/.ssh/id_ed25519"

info() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
okay() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

SERVER_IP="${1:-}"
[ -n "$SERVER_IP" ] || read -rp "请输入云服务器公网 IP: " SERVER_IP
SERVER_IP="$(echo "$SERVER_IP" | tr -d '[:space:]')"
[[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "IP 格式不对：$SERVER_IP"
if [ -z "${SSH_USER:-}" ]; then
  read -rp "SSH 用户名 [root]: " SSH_USER
  SSH_USER="${SSH_USER:-root}"
fi
TARGET="$SSH_USER@$SERVER_IP"
START_TS=$(date +%s)

remind_billing() {
  local used=$(( $(date +%s) - START_TS ))
  local dur="$((used / 3600)) 小时 $((used % 3600 / 60)) 分钟"
  printf '\n\033[1;33m%s\033[0m\n' "=================================================================="
  printf '\033[1;33m  ⚠️  别忘了关闭云服务器计费！\033[0m\n'
  printf '\033[1;33m  服务器 %s 本次使用约 %s。\033[0m\n' "$SERVER_IP" "$dur"
  printf '\033[1;33m  按量计费的实例「关机」通常仍会收费，请到云控制台「销毁 / 退还」实例，\033[0m\n'
  printf '\033[1;33m  或确认该实例已开启「关机不收费」后再关机。\033[0m\n'
  printf '\033[1;33m%s\033[0m\n\n' "=================================================================="
  nohup osascript -e "display alert \"别忘了关闭云服务器计费\" message \"点歌台和内网穿透已关闭。服务器 $SERVER_IP 本次使用约 $dur。按量计费实例关机通常仍会收费，请到云控制台销毁或退还实例。\" as critical" \
    >/dev/null 2>&1 &
}

CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  echo
  if [ -n "${TUNNEL_LOOP_PID:-}" ]; then
    local children
    children=$(pgrep -P "$TUNNEL_LOOP_PID" 2>/dev/null || true)
    kill "$TUNNEL_LOOP_PID" 2>/dev/null || true
    [ -z "$children" ] || kill $children 2>/dev/null || true
    okay "内网穿透已关闭"
  fi
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    okay "点歌服务已停止"
  fi
  remind_billing
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# ---------- 本机准备 ----------
mkdir -p "$STATE_DIR"
port_busy "$PORT" && die "本机端口 $PORT 已被占用（点歌台是否已经在运行？）"
port_busy "$TUNNEL_PORT" && die "本机端口 $TUNNEL_PORT 已被占用，可用 TUNNEL_PORT=xxxx 指定其他端口"

if [ ! -x .venv/bin/python ]; then
  info "安装 Python 依赖…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f "$KEY" ]; then
  info "生成本机 SSH 密钥 $KEY"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -N "" -q -f "$KEY"
fi

# 按小时开的服务器经常重装，同一 IP 的主机指纹会变，所以使用独立的 known_hosts 并在每次运行时重新信任
ssh-keygen -R "$SERVER_IP" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true
ssh-keygen -R "[$SERVER_IP]:$SSH_PORT" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true
SSH_OPTS=(-p "$SSH_PORT" -i "$KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN_HOSTS"
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

# ---------- 免密登录 ----------
info "连接服务器 $TARGET …"
if ! ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  info "首次连接需要输入一次服务器密码，用来安装免密登录公钥"
  ssh "${SSH_OPTS[@]}" -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password "$TARGET" \
    'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; k=$(cat); grep -qxF "$k" ~/.ssh/authorized_keys || echo "$k" >> ~/.ssh/authorized_keys' \
    < "$KEY.pub" || die "登录失败。若 root 不允许密码登录，可用 SSH_USER=ubuntu ./remote.sh 重试"
  ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" true || die "免密登录配置失败"
fi
okay "SSH 免密登录正常"

# ---------- 服务器配置：允许隧道监听公网地址、挑选空闲端口、放行系统防火墙 ----------
REMOTE_SETUP=$(cat <<'EOF'
set -e
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  sudo -n true 2>/dev/null || { echo "ERR=当前用户没有免密 sudo 权限，请改用 root 登录"; exit 1; }
  SUDO="sudo -n"
fi
CFG=/etc/ssh/sshd_config
SSHD=$(command -v sshd || echo /usr/sbin/sshd)
if ! $SUDO "$SSHD" -T 2>/dev/null | grep -qi '^gatewayports clientspecified'; then
  if ! $SUDO grep -qx 'GatewayPorts clientspecified' "$CFG"; then
    $SUDO cp "$CFG" "$CFG.ktv.bak"
    $SUDO sed -i '1i GatewayPorts clientspecified' "$CFG"
  fi
  if ! $SUDO "$SSHD" -t; then
    [ -f "$CFG.ktv.bak" ] && $SUDO cp "$CFG.ktv.bak" "$CFG"
    echo "ERR=sshd 配置校验失败，已还原"; exit 1
  fi
  $SUDO systemctl reload ssh 2>/dev/null || $SUDO systemctl reload sshd 2>/dev/null \
    || $SUDO service ssh reload 2>/dev/null || $SUDO service sshd reload
  echo "INFO=已开启 sshd GatewayPorts"
fi

CHOSEN=""
for p in "$@"; do
  if [ "$p" -lt 1024 ] && [ "$(id -u)" -ne 0 ]; then continue; fi
  if command -v ss >/dev/null 2>&1; then
    line=$($SUDO ss -ltnp "sport = :$p" 2>/dev/null | tail -n +2)
  else
    line=$( (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && echo busy || true)
  fi
  if [ -z "$line" ]; then CHOSEN=$p; break; fi
  if echo "$line" | grep -q '"sshd"'; then
    for pid in $(echo "$line" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do $SUDO kill "$pid" 2>/dev/null || true; done
    sleep 1
    echo "INFO=已清理端口 $p 上残留的旧隧道"
    CHOSEN=$p; break
  fi
done
[ -n "$CHOSEN" ] || { echo "ERR=候选端口（$*）都被占用"; exit 1; }

if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  $SUDO ufw allow "$CHOSEN/tcp" >/dev/null && echo "INFO=ufw 已放行 $CHOSEN"
fi
if command -v firewall-cmd >/dev/null 2>&1 && $SUDO firewall-cmd --state >/dev/null 2>&1; then
  $SUDO firewall-cmd --add-port="$CHOSEN/tcp" >/dev/null && echo "INFO=firewalld 已放行 $CHOSEN"
fi
echo "PORT=$CHOSEN"
EOF
)

info "配置服务器…"
SETUP_OUT=$(ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" bash -s -- $REMOTE_PORTS <<<"$REMOTE_SETUP" 2>&1) || true
while IFS= read -r line; do
  case "$line" in INFO=*) okay "${line#INFO=}" ;; esac
done <<<"$SETUP_OUT"
ERR_LINE=$(grep '^ERR=' <<<"$SETUP_OUT" || true)
[ -z "$ERR_LINE" ] || die "${ERR_LINE#ERR=}"
REMOTE_PORT=$(grep '^PORT=' <<<"$SETUP_OUT" | tail -1 | cut -d= -f2)
[ -n "$REMOTE_PORT" ] || die "服务器配置失败：$SETUP_OUT"
okay "服务器对外端口：$REMOTE_PORT"

if [ "$REMOTE_PORT" = 80 ]; then PUBLIC_URL="http://$SERVER_IP"; else PUBLIC_URL="http://$SERVER_IP:$REMOTE_PORT"; fi
ACCESS_KEY=$(openssl rand -hex 4)
ENTRY_URL="$PUBLIC_URL/?k=$ACCESS_KEY"

# ---------- 启动点歌服务 ----------
info "启动点歌服务…"
PORT="$PORT" TUNNEL_PORT="$TUNNEL_PORT" PUBLIC_URL="$PUBLIC_URL" ACCESS_KEY="$ACCESS_KEY" .venv/bin/python server.py &
SERVER_PID=$!
caffeinate -dims -w "$SERVER_PID" &
for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/api/info" && break
  kill -0 "$SERVER_PID" 2>/dev/null || die "点歌服务启动失败"
  sleep 0.3
done

# ---------- 建立隧道（断线自动重连） ----------
info "建立内网穿透隧道…"
: >"$TUNNEL_LOG"
(
  trap 'exit 0' TERM
  while true; do
    ssh "${SSH_OPTS[@]}" -o BatchMode=yes -o ExitOnForwardFailure=yes -N \
      -R "0.0.0.0:$REMOTE_PORT:127.0.0.1:$TUNNEL_PORT" "$TARGET"
    echo "[$(date '+%F %T')] 隧道断开（退出码 $?），3 秒后重连"
    sleep 3
  done
) >>"$TUNNEL_LOG" 2>&1 &
TUNNEL_LOOP_PID=$!

TUNNEL_OK=0
for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -m 4 -w '%{http_code}' "$ENTRY_URL" || true)
  if [ "$code" = 302 ]; then TUNNEL_OK=1; break; fi
  sleep 1
done
if [ "$TUNNEL_OK" = 1 ]; then
  okay "内网穿透已连通"
else
  warn "暂时无法从公网访问 $PUBLIC_URL ，请检查云服务器安全组/防火墙是否放行 TCP $REMOTE_PORT"
  warn "隧道日志：$TUNNEL_LOG"
  tail -5 "$TUNNEL_LOG" 2>/dev/null || true
fi

echo
echo "=================================================================="
printf '  公网点歌地址（任意网络可用）：\033[1;32m%s\033[0m\n' "$ENTRY_URL"
echo "  大屏播放页：http://localhost:$PORT/tv （大屏二维码已切换为公网地址）"
echo "  按 Ctrl+C 关闭点歌台和内网穿透"
echo "=================================================================="
.venv/bin/python -c "import qrcode,sys; q=qrcode.QRCode(border=1); q.add_data(sys.argv[1]); q.print_ascii(invert=True)" "$ENTRY_URL" || true

if [ -z "${NO_BROWSER:-}" ]; then
  TV_URL="http://localhost:$PORT/tv"
  open -na "Google Chrome" --args --new-window --autoplay-policy=no-user-gesture-required "$TV_URL" 2>/dev/null \
    || open "$TV_URL"
fi

wait "$SERVER_PID" || true
warn "点歌服务已退出"

"""一键内网穿透：用 SSH 反向隧道把云服务器的公网端口转发到本机点歌台。

点歌服务和隧道同生共死：任一方退出都会关闭另一方，并提醒关停按量计费的服务器。
"""

import argparse
import getpass
import json
import os
import re
import secrets
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import paramiko

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "data" / "remote.json"
LOCAL_PORT = int(os.environ.get("PORT", "8080"))
TUNNEL_PORT = int(os.environ.get("TUNNEL_PORT", "8081"))
SSHD_DROPIN = "/etc/ssh/sshd_config.d/00-bili-ktv.conf"
SSHD_SETTINGS = "GatewayPorts clientspecified\nClientAliveInterval 15\nClientAliveCountMax 3\n"


class SetupError(Exception):
    pass


def say(msg: str = ""):
    try:
        print(msg, flush=True)
    except OSError:
        pass


def ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


class Remote:
    def __init__(self, host: str, user: str, password: str, port: int):
        self.host, self.user, self.password, self.port = host, user, password, port

    def connect(self) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(
                self.host, username=self.user, password=self.password, timeout=10,
                banner_timeout=15, auth_timeout=15, look_for_keys=False, allow_agent=False,
            )
        except paramiko.AuthenticationException:
            raise SetupError("SSH 登录失败：用户名或密码错误")
        except (OSError, paramiko.SSHException) as e:
            raise SetupError(f"连不上服务器 {self.host}:22（{e}）")
        c.get_transport().set_keepalive(15)
        return c

    def run(self, client: paramiko.SSHClient, cmd: str, sudo: bool = False, check: bool = True) -> str:
        if sudo:
            cmd = f"sudo -S -p '' sh -c {shlex.quote(cmd)}"
        stdin, out, err = client.exec_command(cmd, timeout=30)
        if sudo:
            stdin.write(self.password + "\n")
            stdin.flush()
        code = out.channel.recv_exit_status()
        o, e = out.read().decode(errors="replace"), err.read().decode(errors="replace")
        if check and code:
            raise SetupError(f"服务器命令执行失败：{cmd}\n{(e or o).strip()}")
        return o

    def setup(self):
        """开启 sshd 的 GatewayPorts（否则反向隧道只绑 127.0.0.1），并清理占用端口的旧隧道。"""
        c = self.connect()
        try:
            self._free_port(c)
            if not self._gateway_enabled(c):
                say("      开启 sshd GatewayPorts …")
                self.run(c, f"printf %s {shlex.quote(SSHD_SETTINGS)} > {SSHD_DROPIN}", sudo=True)
                self._reload_sshd(c)
                if not self._gateway_enabled(c):
                    self.run(
                        c,
                        "sed -i 's/^#\\?\\s*GatewayPorts.*/GatewayPorts clientspecified/' /etc/ssh/sshd_config && "
                        "(grep -q '^GatewayPorts' /etc/ssh/sshd_config || echo 'GatewayPorts clientspecified' >> /etc/ssh/sshd_config)",
                        sudo=True,
                    )
                    self._reload_sshd(c)
                if not self._gateway_enabled(c):
                    raise SetupError("无法开启 sshd 的 GatewayPorts，请检查服务器 /etc/ssh/sshd_config")
            if "Status: active" in self.run(c, "ufw status 2>/dev/null || true", sudo=True):
                say(f"      服务器 ufw 防火墙已启用，放行 {self.port}/tcp …")
                self.run(c, f"ufw allow {self.port}/tcp", sudo=True)
        finally:
            c.close()

    def _gateway_enabled(self, c) -> bool:
        out = self.run(c, "sshd -T 2>/dev/null | grep -i '^gatewayports'", sudo=True, check=False).lower()
        return "clientspecified" in out or "yes" in out

    def _reload_sshd(self, c):
        self.run(c, "sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)", sudo=True)

    def _free_port(self, c):
        out = self.run(c, f"ss -ltnpH 'sport = :{self.port}'", sudo=True, check=False)
        if not out.strip():
            return
        holders = re.findall(r'\("([^"]+)",pid=(\d+)', out)
        if holders and all(name.startswith("sshd") for name, _ in holders):
            say(f"      端口 {self.port} 被上次残留的隧道占用，正在清理 …")
            self.run(c, "kill " + " ".join(sorted({pid for _, pid in holders})), sudo=True, check=False)
            time.sleep(1)
            return
        names = ", ".join(sorted({n for n, _ in holders})) or "未知进程"
        raise SetupError(f"服务器端口 {self.port} 已被 {names} 占用，请换一个防火墙已放行的端口")


def pipe(chan: paramiko.Channel):
    try:
        sock = socket.create_connection(("127.0.0.1", TUNNEL_PORT), timeout=5)
    except OSError:
        chan.close()
        return
    sock.settimeout(None)
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [], 60)
            if sock in r:
                data = sock.recv(65536)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(65536)
                if not data:
                    break
                sock.sendall(data)
    except (OSError, paramiko.SSHException):
        pass
    finally:
        chan.close()
        sock.close()


class Tunnel(threading.Thread):
    """维持反向隧道：服务器 0.0.0.0:port → 本机 127.0.0.1:TUNNEL_PORT，掉线自动重连。"""

    def __init__(self, remote: Remote):
        super().__init__(daemon=True)
        self.remote = remote
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = None

    def run(self):
        backoff = 2
        while not self.stop_event.is_set():
            client = None
            try:
                client = self.remote.connect()
                transport = client.get_transport()
                transport.request_port_forward("0.0.0.0", self.remote.port)
                if self.ready.is_set():
                    say("🔗 内网穿透已重新连接")
                self.ready.set()
                backoff = 2
                while transport.is_active() and not self.stop_event.is_set():
                    chan = transport.accept(1)
                    if chan is not None:
                        threading.Thread(target=pipe, args=(chan,), daemon=True).start()
                if transport.is_active():
                    try:
                        transport.cancel_port_forward("0.0.0.0", self.remote.port)
                    except paramiko.SSHException:
                        pass
            except (SetupError, OSError, paramiko.SSHException) as e:
                if not self.ready.is_set():
                    self.error = str(e) if isinstance(e, SetupError) else f"建立隧道失败：{e}"
                    self.ready.set()
                    return
                say(f"⚠️  内网穿透断开（{e}），{backoff} 秒后重连 …")
            finally:
                if client:
                    client.close()
            if not self.stop_event.is_set():
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)

    def stop(self):
        self.stop_event.set()
        self.join(timeout=5)


def wait_local_server(proc: subprocess.Popen, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def public_check(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


def remind_billing(host: str):
    msg = f"内网穿透已关闭。服务器 {host} 是按量计费的，记得去云服务器控制台关机或销毁实例，停止计费！"
    say()
    say("=" * 64)
    say("  ⚠️  " + msg)
    say("  腾讯云控制台: https://console.cloud.tencent.com/cvm/instance")
    say("=" * 64)
    script = f'display alert "点歌台已关闭" message {json.dumps(msg, ensure_ascii=False)} as warning'
    try:
        subprocess.Popen(["osascript", "-e", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def main():
    parser = argparse.ArgumentParser(description="点歌台 + 一键内网穿透")
    parser.add_argument("--host", help="服务器公网 IP")
    parser.add_argument("--user", help="SSH 用户名（默认 ubuntu）")
    parser.add_argument("--port", type=int, help="服务器上对外开放的端口（默认 8888）")
    args = parser.parse_args()

    cfg = load_config()
    say("==== B站点歌台 · 内网穿透 ====")
    host = args.host or ""
    while not host:
        host = ask("服务器公网 IP")
    user = args.user or ask("SSH 用户名", cfg.get("user", "ubuntu"))
    password = os.environ.get("KTV_SSH_PASSWORD") or getpass.getpass("SSH 密码（输入时不显示）: ")
    port = args.port or int(ask("公网端口（需在服务器防火墙放行）", str(cfg.get("port", 8888))))
    save_config({"user": user, "port": port})

    remote = Remote(host, user, password, port)
    say("[1/4] 连接服务器并检查配置 …")
    remote.setup()

    key = secrets.token_urlsafe(6)
    public_url = f"http://{host}:{port}/?key={key}"
    say("[2/4] 启动点歌服务 …")
    env = {**os.environ, "TUNNEL_PORT": str(TUNNEL_PORT), "ACCESS_KEY": key, "PUBLIC_URL": public_url}
    server = subprocess.Popen([sys.executable, str(BASE / "server.py")], env=env)
    tunnel = None
    try:
        if not wait_local_server(server):
            raise SetupError(f"点歌服务启动失败（端口 {LOCAL_PORT} 或 {TUNNEL_PORT} 可能被占用）")

        say("[3/4] 建立内网穿透隧道 …")
        tunnel = Tunnel(remote)
        tunnel.start()
        tunnel.ready.wait(40)
        if tunnel.error or not tunnel.ready.is_set():
            raise SetupError(tunnel.error or "建立隧道超时")

        say("[4/4] 从公网自检 …")
        if public_check(f"http://{host}:{port}/api/info?key={key}"):
            say("      ✅ 公网访问正常")
        else:
            say(f"      ⚠️  公网访问不通，请确认云服务器安全组/防火墙已放行 TCP {port}")

        say()
        say("=" * 64)
        say(f"  📱 公网点歌地址（4G/5G 也能用）: {public_url}")
        say("     大屏二维码已切换为这个地址，扫码即可进入")
        say("  ⛔ 按 Ctrl+C 关闭点歌台，内网穿透会一起关闭")
        say("=" * 64)

        while server.poll() is None and tunnel.is_alive():
            time.sleep(1)
        if server.poll() is not None:
            say("点歌服务已退出")
    finally:
        if tunnel:
            tunnel.stop()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()
        remind_billing(host)


def _exit_on_signal(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGHUP, _exit_on_signal)
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
    except SetupError as e:
        say(f"\n❌ {e}")
        sys.exit(1)

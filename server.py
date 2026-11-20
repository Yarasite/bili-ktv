"""局域网 B 站点歌台。

手机访问 http://<Mac 局域网 IP>:8080/ 点歌，大屏浏览器打开 http://localhost:8080/tv 播放。
"""

import ipaddress
import json
import logging
import os
import random
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

from bili import REFERER, UA, BiliClient, BiliError

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
STATE_FILE = BASE / "data" / "state.json"
PORT = int(os.environ.get("PORT", "8080"))
# 内网穿透入口：隧道流量只进这个仅监听 127.0.0.1 的端口，并要求访问密钥
TUNNEL_PORT = int(os.environ.get("TUNNEL_PORT") or 0)
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
HISTORY_MAX = 30
QUEUE_MAX = 200

log = logging.getLogger("ktv")
bili = BiliClient(sessdata=os.environ.get("BILI_SESSDATA") or None)


PROXY_NET = ipaddress.ip_network("198.18.0.0/15")


def lan_ips() -> List[str]:
    """本机可被局域网访问的 IPv4，私有网段优先；跳过回环、链路本地和代理软件 TUN 网卡（198.18/15）。"""
    found = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=2).stdout
        iface = ""
        for line in out.splitlines():
            if line and not line[0].isspace():
                iface = line.split(":", 1)[0]
            m = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if m and not iface.startswith(("lo", "utun", "gif", "stf")):
                found.append(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    ips = []
    for ip in found:
        addr = ipaddress.ip_address(ip)
        if not (addr.is_loopback or addr.is_link_local or addr in PROXY_NET) and ip not in ips:
            ips.append(ip)
    ips.sort(key=lambda ip: not ipaddress.ip_address(ip).is_private)
    return ips or ["127.0.0.1"]


def lan_ip() -> str:
    return os.environ.get("HOST_IP") or lan_ips()[0]


class Room:
    def __init__(self):
        self.now: Optional[dict] = None
        self.queue: List[dict] = []
        self.history: List[dict] = []
        self.player: dict = {}
        self.clients: Dict[web.WebSocketResponse, str] = {}
        self._load()

    def _load(self):
        try:
            d = json.loads(STATE_FILE.read_text("utf-8"))
            self.now, self.queue, self.history = d.get("now"), d.get("queue", []), d.get("history", [])
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("读取 %s 失败，使用空列表", STATE_FILE)

    def _save(self):
        STATE_FILE.parent.mkdir(exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"now": self.now, "queue": self.queue, "history": self.history}, ensure_ascii=False),
            "utf-8",
        )
        tmp.replace(STATE_FILE)

    def snapshot(self) -> dict:
        return {"type": "state", "now": self.now, "queue": self.queue, "history": self.history}

    async def broadcast(self, msg: dict, role: Optional[str] = None):
        for ws, r in list(self.clients.items()):
            if role and r != role:
                continue
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.pop(ws, None)

    async def changed(self):
        self._save()
        await self.broadcast(self.snapshot())

    def find(self, item_id: str) -> Optional[dict]:
        if self.now and self.now["id"] == item_id:
            return self.now
        for it in self.queue + self.history:
            if it["id"] == item_id:
                return it
        return None

    def queue_index(self, item_id: str) -> int:
        for i, it in enumerate(self.queue):
            if it["id"] == item_id:
                return i
        return -1

    def advance(self):
        if self.now:
            self.history.insert(0, {**self.now, "played_at": time.time()})
            del self.history[HISTORY_MAX:]
        self.now = self.queue.pop(0) if self.queue else None
        self.player = {}

    async def enqueue(self, meta: dict, nick: str) -> dict:
        for i, it in enumerate(([self.now] if self.now else []) + self.queue):
            if it["bvid"] == meta["bvid"] and it["cid"] == meta["cid"]:
                where = "正在播放" if (self.now and i == 0) else f"已在列表第 {i if self.now else i + 1} 位"
                raise BiliError(f"这首{where}，不用重复点啦")
        if len(self.queue) >= QUEUE_MAX:
            raise BiliError("播放列表已满")
        item = {**meta, "id": uuid.uuid4().hex[:12], "added_by": nick, "added_at": time.time()}
        item.pop("played_at", None)
        self.queue.append(item)
        started = self.now is None
        if started:
            self.advance()
        await self.changed()
        return {"item": item, "position": 0 if started else len(self.queue), "started": started}


room = Room()


def ok(**data) -> web.Response:
    return web.json_response({"ok": True, **data})


def fail(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


async def body_of(request: web.Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def nick_of(body: dict) -> str:
    return (str(body.get("nick") or "").strip()[:20]) or "匿名"


async def api_state(request):
    return web.json_response({**room.snapshot(), "player": room.player})


async def api_add(request):
    body = await body_of(request)
    text = str(body.get("url") or "").strip()[:2000]
    if not text:
        return fail("请粘贴 B 站视频链接")
    try:
        ident, page = await bili.resolve(text)
        meta = await bili.fetch_video(ident, page)
        return ok(**await room.enqueue(meta, nick_of(body)))
    except BiliError as e:
        return fail(str(e))
    except Exception:
        log.exception("点歌失败: %s", text)
        return fail("获取视频信息失败，请稍后重试", 500)


async def api_replay(request):
    body = await body_of(request)
    item = room.find(str(body.get("id")))
    if not item:
        return fail("找不到这首歌")
    try:
        meta = {k: v for k, v in item.items() if k not in ("id", "added_by", "added_at")}
        return ok(**await room.enqueue(meta, nick_of(body)))
    except BiliError as e:
        return fail(str(e))


async def api_remove(request):
    i = room.queue_index(str((await body_of(request)).get("id")))
    if i < 0:
        return fail("这首已经不在列表里了")
    room.queue.pop(i)
    await room.changed()
    return ok()


async def api_top(request):
    i = room.queue_index(str((await body_of(request)).get("id")))
    if i < 0:
        return fail("这首已经不在列表里了")
    room.queue.insert(0, room.queue.pop(i))
    await room.changed()
    return ok()


async def api_shuffle(request):
    random.shuffle(room.queue)
    await room.changed()
    return ok()


async def api_clear(request):
    room.queue.clear()
    await room.changed()
    return ok()


async def api_next(request):
    """切到下一首。带 id 时只有它仍是当前歌曲才切换，避免多端同时点击或播放结束时重复跳过。"""
    item_id = (await body_of(request)).get("id")
    if item_id and (not room.now or room.now["id"] != item_id):
        return ok(skipped=False)
    room.advance()
    await room.changed()
    return ok(skipped=True)


CONTROL_ACTIONS = {"toggle", "pause", "play", "restart", "seek", "volume"}


async def api_control(request):
    body = await body_of(request)
    action = body.get("action")
    if action not in CONTROL_ACTIONS:
        return fail("未知操作")
    if not any(r == "tv" for r in room.clients.values()):
        return fail("大屏播放页未连接")
    value = body.get("value")
    await room.broadcast(
        {"type": "command", "action": action, "value": value if isinstance(value, (int, float)) else None},
        role="tv",
    )
    return ok()


STREAM_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Last-Modified", "ETag")


async def api_stream(request):
    """代理 B 站视频流（CDN 校验 Referer 且地址与请求 IP 绑定），透传 Range 以支持拖动进度。"""
    item = room.find(request.match_info["id"])
    if not item:
        raise web.HTTPNotFound()

    headers = {"User-Agent": UA, "Referer": REFERER, "Accept-Encoding": "identity"}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    upstream = None
    for attempt in range(2):
        try:
            url = await bili.play_url(item["bvid"], item["cid"], force=attempt > 0)
            upstream = await bili.session.get(url, headers=headers)
        except (BiliError, aiohttp.ClientError) as e:
            log.warning("获取视频流失败 %s: %s", item["bvid"], e)
            return web.Response(status=502, text=str(e))
        if upstream.status < 400 or attempt == 1:
            break
        upstream.release()

    try:
        resp = web.StreamResponse(status=upstream.status)
        for h in STREAM_HEADERS:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "no-store"
        await resp.prepare(request)
        async for chunk in upstream.content.iter_chunked(256 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    except (ConnectionResetError, aiohttp.ClientError):
        return resp
    finally:
        upstream.release()


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    role = "tv" if request.query.get("role") == "tv" else "remote"
    room.clients[ws] = role
    await ws.send_json(room.snapshot())
    if room.player:
        await ws.send_json({"type": "player", **room.player})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT or role != "tv":
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if data.get("type") == "player":
                room.player = {k: data.get(k) for k in ("id", "position", "duration", "paused", "volume", "mode")}
                await room.broadcast({"type": "player", **room.player}, role="remote")
    finally:
        room.clients.pop(ws, None)
    return ws


def remote_url() -> str:
    return PUBLIC_URL or f"http://{lan_ip()}:{PORT}/"


@web.middleware
async def tunnel_guard(request, handler):
    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    if not TUNNEL_PORT or not sockname or sockname[1] != TUNNEL_PORT:
        return await handler(request)
    if request.query.get("key") == ACCESS_KEY:
        resp = await handler(request)
        if not resp.prepared:
            resp.set_cookie("ktv_key", ACCESS_KEY, max_age=7 * 86400, httponly=True, samesite="Lax")
        return resp
    if request.cookies.get("ktv_key") == ACCESS_KEY:
        return await handler(request)
    return web.Response(status=403, text="访问密钥无效，请扫描大屏上的二维码进入点歌台。")


async def api_info(request):
    return ok(url=remote_url())


async def api_qr(request):
    img = qrcode.make(remote_url(), image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return web.Response(body=img.to_string(), content_type="image/svg+xml")


def page(name: str):
    async def handler(request):
        return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
    return handler


async def on_startup(app):
    await bili.start()


async def on_shutdown(app):
    for ws in list(room.clients):
        await ws.close()
    await bili.close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[tunnel_guard])
    app.router.add_get("/", page("index.html"))
    app.router.add_get("/tv", page("tv.html"))
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/info", api_info)
    app.router.add_get("/api/qr.svg", api_qr)
    app.router.add_get("/api/stream/{id}", api_stream)
    for name, h in {
        "add": api_add, "replay": api_replay, "remove": api_remove, "top": api_top,
        "shuffle": api_shuffle, "clear": api_clear, "next": api_next, "control": api_control,
    }.items():
        app.router.add_post(f"/api/{name}", h)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    print("=" * 56)
    print(f"  手机点歌（同一 Wi-Fi）: http://{lan_ip()}:{PORT}/")
    if PUBLIC_URL:
        print(f"  手机点歌（公网）:       {PUBLIC_URL}")
    others = [ip for ip in lan_ips() if ip != lan_ip()]
    if others:
        print(f"  其他可用地址: {', '.join(f'http://{ip}:{PORT}/' for ip in others)}")
    print(f"  大屏播放页:            http://localhost:{PORT}/tv")
    print("=" * 56, flush=True)
    socks = []
    if TUNNEL_PORT:
        if not ACCESS_KEY:
            raise SystemExit("设置了 TUNNEL_PORT 时必须同时设置 ACCESS_KEY")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", TUNNEL_PORT))
        socks.append(s)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, sock=socks or None, print=None)

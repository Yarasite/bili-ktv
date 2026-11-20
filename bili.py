"""B 站接口封装：解析分享链接、获取视频信息与可直接播放的 mp4 地址。"""

import re
import time
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.I)
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
SHORT_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn", "bili33.cn")

PLAY_URL_TTL = 20 * 60


class BiliError(Exception):
    pass


def _is_short_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in SHORT_HOSTS)


def _https(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


class BiliClient:
    def __init__(self, sessdata: Optional[str] = None, quality: int = 80):
        self.sessdata = sessdata
        self.quality = quality
        self.session: Optional[aiohttp.ClientSession] = None
        self._buvid: Optional[str] = None
        self._url_cache: Dict[Tuple[str, int], Tuple[str, float]] = {}

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": UA, "Referer": REFERER},
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=30),
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def _ensure_buvid(self):
        if self._buvid is not None:
            return
        self._buvid = ""
        try:
            async with self.session.get("https://api.bilibili.com/x/frontend/finger/spi") as r:
                d = (await r.json(content_type=None))["data"]
                self._buvid = f"buvid3={d['b_3']}; buvid4={d['b_4']}"
        except Exception:
            pass

    def _cookie(self) -> str:
        parts = [p for p in (self._buvid,) if p]
        if self.sessdata:
            parts.append(f"SESSDATA={self.sessdata}")
        return "; ".join(parts)

    async def _api(self, url: str, params: dict) -> dict:
        await self._ensure_buvid()
        headers = {"Cookie": self._cookie()} if self._cookie() else {}
        try:
            async with self.session.get(url, params=params, headers=headers) as r:
                if r.status != 200:
                    raise BiliError(f"B 站接口返回 HTTP {r.status}")
                d = await r.json(content_type=None)
        except aiohttp.ClientError as e:
            raise BiliError(f"无法连接 B 站：{e}") from e
        if d.get("code") != 0:
            raise BiliError(f"B 站接口错误：{d.get('message') or d.get('code')}")
        return d["data"]

    async def _expand_short(self, url: str) -> str:
        for _ in range(3):
            host = urlparse(url).hostname or ""
            if not _is_short_host(host):
                return url
            try:
                async with self.session.get(url, allow_redirects=False) as r:
                    loc = r.headers.get("Location")
            except aiohttp.ClientError as e:
                raise BiliError(f"短链解析失败：{e}") from e
            if not loc:
                raise BiliError("短链解析失败，链接可能已失效")
            url = loc
        return url

    async def resolve(self, text: str) -> Tuple[dict, int]:
        """从用户粘贴的文本（链接、分享文案、BV/av 号）中解析出视频标识和分 P。"""
        text = text.strip()
        m = URL_RE.search(text)
        url = await self._expand_short(m.group(0)) if m else None
        target = url or text

        page = 1
        if url:
            p = parse_qs(urlparse(url).query).get("p", [""])[0]
            if p.isdigit():
                page = int(p)

        bv = BV_RE.search(target)
        if bv:
            return {"bvid": bv.group(0)}, page
        av = AV_RE.search(target)
        if av:
            return {"aid": av.group(1)}, page
        if url and ("/bangumi/" in url or "/cheese/" in url):
            raise BiliError("暂不支持番剧、电影和课程，请分享普通视频链接")
        raise BiliError("没有识别到 B 站视频（需要包含 BV 号或 b23.tv 短链）")

    async def fetch_video(self, ident: dict, page: int = 1) -> dict:
        data = await self._api("https://api.bilibili.com/x/web-interface/view", ident)
        pages = data.get("pages") or []
        if not 1 <= page <= len(pages):
            page = 1
        pg = pages[page - 1] if pages else {"cid": data["cid"], "part": "", "duration": data["duration"]}
        return {
            "bvid": data["bvid"],
            "cid": pg["cid"],
            "page": page,
            "pages": len(pages) or 1,
            "title": data["title"],
            "part": pg.get("part", "") if len(pages) > 1 else "",
            "cover": _https(data.get("pic", "")),
            "owner": (data.get("owner") or {}).get("name", ""),
            "duration": pg.get("duration") or data.get("duration") or 0,
        }

    async def play_url(self, bvid: str, cid: int, force: bool = False) -> str:
        key = (bvid, cid)
        now = time.time()
        cached = self._url_cache.get(key)
        if cached and not force and cached[1] > now:
            return cached[0]
        data = await self._api(
            "https://api.bilibili.com/x/player/playurl",
            {
                "bvid": bvid,
                "cid": cid,
                "qn": self.quality,
                "fnval": 1,
                "fnver": 0,
                "platform": "html5",
                "high_quality": 1,
            },
        )
        durl = data.get("durl") or []
        if not durl:
            raise BiliError("没有拿到可播放的视频地址")
        url = durl[0]["url"]
        self._url_cache = {k: v for k, v in self._url_cache.items() if v[1] > now}
        self._url_cache[key] = (url, now + PLAY_URL_TTL)
        return url

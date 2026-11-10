"""Read-only WhatsLink lookup; never submits a downloader task."""

import asyncio
import ipaddress
import json
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from .models import normalize_magnet
from .system_proxy import windows_proxy_rules

ENDPOINT = "https://whatslink.info/api/v1/link"
SOURCE = "https://whatslink.info/"


def _windows_route(url: str) -> str:
    proxy, bypass = windows_proxy_rules(url)
    target = urlsplit(url)
    host = target.hostname.lower()
    authority = f"{host}:{target.port or 443}"
    for rule in bypass.lower().replace(",", ";").replace(" ", ";").split(";"):
        if rule == "<local>" and "." not in host:
            return ""
        if rule and any(fnmatchcase(value, rule) for value in (host, authority, url.lower())):
            return ""
    entries = proxy.replace(";", " ").split()
    # Windows can supply one endpoint per protocol or a single proxy for all protocols.
    by_scheme = {
        key.strip().lower(): value.strip()
        for key, _, value in (entry.partition("=") for entry in entries)
        if key.strip().lower() in ("http", "https", "socks") and value
    }
    selected = by_scheme.get(target.scheme, "") if by_scheme else next(iter(entries), "")
    return "http://" + selected if selected and "://" not in selected else selected


def resolve_proxy(value: str, url: str = ENDPOINT) -> str | None:
    """Choose the route for this URL without retrying through a different route."""
    value = value.strip()
    if value.lower() == "system":
        if sys.platform == "win32":
            try:
                value = _windows_route(url)
            except OSError:
                raise ValueError("无法解析 Windows 系统代理，请检查系统代理设置") from None
        else:
            target = urlsplit(url)
            value = "" if proxy_bypass(target.netloc) else getproxies().get(target.scheme, "")
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.port == 0
            or any(c.isspace() for c in value)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("预览代理须为 HTTP(S) URL、system 或留空直连") from None
    return value


def clean_text(value, limit=180):
    if not isinstance(value, str):
        return "未知"
    # Remote strings are text, never interpreted as CQ markup or commands.
    return " ".join("".join(c for c in value if c.isprintable()).split())[:limit] or "未知"


def public_image_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return None
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if (
            url.scheme != "https"
            or not host
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            return None
        if (
            host == "localhost"
            or "." not in host
            or host.endswith((".local", ".localhost", ".internal"))
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    except ValueError:
        return None


def parse_result(payload: dict, infohash: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("预览服务返回格式异常")
    if payload.get("error") or payload.get("error_code"):
        raise ValueError("预览服务未能解析此链接；不代表资源无法下载")
    name = payload.get("name")
    has_name = isinstance(name, str) and name.strip().lower() not in ("", infohash)
    # An unindexed hash can come back with a placeholder file count of one.
    has_metadata = any(payload.get(k) for k in ("file_type", "size", "screenshots"))
    if not (has_name or has_metadata or payload.get("count") not in (None, 0, 1)):
        raise ValueError("预览服务暂未收录此资源；不代表资源无法下载")

    def number(key):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        # Bounds also exclude NaN/inf and malformed gigantic API values.
        return int(value) if 0 <= value <= 10**18 else None

    screenshots = []
    candidates = payload.get("screenshots", [])
    if isinstance(candidates, list):
        for entry in candidates[:30]:
            if isinstance(entry, dict) and (url := public_image_url(entry.get("screenshot"))):
                if url not in screenshots:
                    screenshots.append(url)
    return {
        "hash": infohash,
        "name": clean_text(name),
        "file_type": clean_text(payload.get("file_type"), 30),
        "size": number("size"),
        "count": number("count"),
        "screenshots": screenshots[:3],
    }


def render(result: dict) -> str:
    amount = result["size"]
    amount = f"{amount / 1024 / 1024:.1f} MiB" if amount is not None else "未知"
    count = result["count"] if result["count"] is not None else "未知"
    lines = [
        "磁力预览（未创建下载任务）",
        "名称：" + result["name"],
        "类型：" + result["file_type"],
        f"大小：{amount}；文件数：{count}",
        "资源哈希：" + result["hash"],
    ]
    if result["screenshots"]:
        lines.extend(f"截图{i}：{url}" for i, url in enumerate(result["screenshots"], 1))
    else:
        lines.append("暂无可用截图；未收录截图不代表资源不可下载。")
    lines.extend(["以上为第三方查询结果，不保证完整或与资源一致。", "信息来源：" + SOURCE])
    return "\n".join(lines)


class PreviewService:
    def __init__(self, cooldown=15, cache_seconds=3600, image_hosts=None, proxy=None):
        self.cooldown = max(1, cooldown)
        self.cache_seconds = max(30, cache_seconds)
        self.cache = OrderedDict()
        self.chats = OrderedDict()
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.image_hosts = (
            image_hosts if image_hosts is not None else ["whatslink.info", ".whatslink.info"]
        )
        self.follow_system = isinstance(proxy, str) and proxy.strip().lower() == "system"
        fixed_proxy = None if self.follow_system else resolve_proxy(proxy or "")
        self.http = None if self.follow_system else self._client(fixed_proxy)
        self.connection_mode = "系统代理" if self.follow_system else ("代理" if fixed_proxy else "直连")

    @staticmethod
    def _client(proxy):
        return httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, proxy=proxy)

    @asynccontextmanager
    async def _connection(self, url):
        if not self.follow_system:
            yield self.http, self.connection_mode
            return
        # WinHTTP may have to fetch and evaluate a PAC script. Keep it off the event loop.
        try:
            proxy = await asyncio.wait_for(asyncio.to_thread(resolve_proxy, "system", url), 10)
        except TimeoutError:
            raise ValueError("读取系统代理超时，请检查系统代理设置") from None
        async with self._client(proxy) as client:
            yield client, "代理" if proxy else "直连"

    async def _read_preview(self, client, key):
        # Submit only the hash, without names, tracker passkeys, or task IDs.
        async with client.stream(
            "GET", ENDPOINT, params={"url": "magnet:?xt=urn:btih:" + key}
        ) as response:
            if response.status_code == 429:
                self.next_request = time.monotonic() + 60
                raise ValueError("预览服务额度或频率受限，请至少一分钟后重试")
            if response.status_code == 404:
                raise ValueError("预览服务暂未收录此资源")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 262144:
                    raise ValueError("预览服务响应超过大小限制")
        return parse_result(json.loads(body), key)

    async def lookup(self, magnet: str, chat: str) -> dict:
        result, = await self._lookup_results((magnet,), chat)
        if isinstance(result, ValueError):
            raise result
        return result

    async def lookup_batch(self, magnets: list[str], chat: str) -> list[dict]:
        """Preview one message's distinct magnets under a single chat cooldown."""
        return [
            result for result in await self._lookup_results(magnets, chat)
            if isinstance(result, dict)
        ]

    async def _lookup_results(self, magnets, chat):
        now = time.monotonic()
        if now - self.chats.get(chat, -1e20) < self.cooldown:
            raise ValueError(f"本聊天预览过于频繁，请等待 {self.cooldown} 秒后再试")
        keys = list(dict.fromkeys(normalize_magnet(magnet)[1] for magnet in magnets))
        cached = {}
        for key in keys:
            entry = self.cache.get(key)
            if entry and now - entry[0] < self.cache_seconds:
                cached[key] = entry[1]
                self.cache.move_to_end(key)
        needs_network = len(cached) != len(keys)
        if needs_network:
            # Cached previews need no network slot. Rejected lookups consume no chat cooldown.
            if self.lock.locked():
                raise ValueError("预览服务忙，请稍后重试")
            if now < self.next_request:
                raise ValueError("预览服务请求间隔限制中，请稍后重试")
        self.chats[chat] = now
        self.chats.move_to_end(chat)
        if len(self.chats) > 1024:
            self.chats.popitem(last=False)
        if not needs_network:
            return [cached[key] for key in keys]
        async with self.lock:
            results = []
            for key in keys:
                if key in cached:
                    results.append(cached[key])
                    continue
                delay = self.next_request - time.monotonic()
                if delay > 3:
                    continue  # An HTTP 429 suspended network lookups.
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    results.append(await self._fetch_preview(key))
                except ValueError as exc:
                    results.append(exc)
            return results

    async def _fetch_preview(self, key):
        mode = self.connection_mode
        try:
            async with self._connection(ENDPOINT) as (client, mode):
                self.next_request = time.monotonic() + 3
                try:
                    result = await self._read_preview(client, key)
                except httpx.ConnectError:
                    # The connection failed before an API response. Reuse the chosen route.
                    await asyncio.sleep(1)
                    self.next_request = time.monotonic() + 3
                    result = await self._read_preview(client, key)
        except httpx.TimeoutException:
            raise ValueError(
                f"预览服务请求超时（{mode}），请稍后重试"
            ) from None
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"预览服务返回 HTTP {exc.response.status_code}（{mode}）"
            ) from None
        except httpx.HTTPError as exc:
            from astrbot.api import logger

            # No exception text: it may expose proxy passwords or query URLs.
            logger.warning("WhatsLink connection failed: %s (%s)", type(exc).__name__, mode)
            raise ValueError(
                f"预览服务连接失败（{mode}，{type(exc).__name__}），请检查代理或网络"
            ) from None
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("预览服务未返回有效 JSON，可能被拦截或服务异常") from None
        self.cache[key] = (time.monotonic(), result)
        self.cache.move_to_end(key)
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    async def image(self, url: str) -> bytes | None:
        """Download small allowlisted screenshots; never forward arbitrary URLs to QQ."""
        url = public_image_url(url)
        if not url:
            return None
        host = urlsplit(url).hostname.lower()
        if not any(
            host == item.lower() if not item.startswith(".") else host.endswith(item.lower())
            for item in self.image_hosts
            if item
        ):
            return None
        try:
            async with self._connection(url) as (client, _), client.stream("GET", url) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";")[0] not in (
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                ):
                    return None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        return None
            raw = bytes(data)
            if raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (
                raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
            ):
                return raw
        except (httpx.HTTPError, ValueError):
            return None
        return None

    async def close(self):
        if self.http is not None:
            await self.http.aclose()

"""Read-only WhatsLink lookup; never submits a downloader task."""

import asyncio
import ipaddress
import json
import logging
import time
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies

import httpx

from .models import normalize_magnet

ENDPOINT = "https://whatslink.info/api/v1/link"
SOURCE = "https://whatslink.info/"


def registry_proxies() -> dict:
    """Windows fallback without mutating the process-wide proxy environment."""
    reader = getattr(urllib.request, "getproxies_registry", None)
    return reader() if reader else {}


def resolve_proxy(value: str) -> str | None:
    """Follow existing OS/environment HTTPS proxy, or use an explicit URL."""
    value = value.strip()
    if value.lower() == "system":
        proxies = getproxies()
        value = proxies.get("https") or proxies.get("http") or ""
        if not value:
            # AstrBot sets no_proxy even when no HTTP proxy is configured. Python
            # then returns {'no': ...} and skips the Windows registry entirely.
            proxies = registry_proxies()
            value = proxies.get("https") or proxies.get("http") or ""
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("预览代理须为 HTTP(S) URL、system 或留空直连")
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
    if not any(payload.get(k) for k in ("name", "file_type", "size", "count", "screenshots")):
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
        "name": clean_text(payload.get("name")),
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
        self.http = httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False, proxy=proxy
        )
        self.connection_mode = "代理" if proxy else "直连"

    async def lookup(self, magnet: str, chat: str) -> dict:
        _, key = normalize_magnet(magnet)
        now = time.monotonic()
        if now - self.chats.get(chat, -1e20) < self.cooldown:
            raise ValueError(f"本聊天预览过于频繁，请等待 {self.cooldown} 秒后再试")
        self.chats[chat] = now
        self.chats.move_to_end(chat)
        if len(self.chats) > 1024:
            self.chats.popitem(last=False)
        # Only one request is accepted at once; do not accumulate waiting handlers.
        if self.lock.locked():
            raise ValueError("预览服务忙，请稍后重试")
        async with self.lock:
            cached = self.cache.get(key)
            if cached and now - cached[0] < self.cache_seconds:
                self.cache.move_to_end(key)
                return cached[1]
            if now < self.next_request:
                raise ValueError("预览服务请求间隔限制中，请稍后重试")
            self.next_request = now + 3
            try:
                # Submit only the hash, without names, tracker passkeys, or task IDs.
                async with self.http.stream(
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
                result = parse_result(json.loads(body), key)
            except httpx.TimeoutException:
                raise ValueError(
                    f"预览服务请求超时（{self.connection_mode}），请稍后重试"
                ) from None
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"预览服务返回 HTTP {exc.response.status_code}（{self.connection_mode}）"
                ) from None
            except httpx.HTTPError as exc:
                # No exception text: it may expose proxy passwords or query URLs.
                logging.getLogger(__name__).warning(
                    "WhatsLink connection failed: %s (%s)", type(exc).__name__, self.connection_mode
                )
                raise ValueError(
                    f"预览服务连接失败（{self.connection_mode}，{type(exc).__name__}），请检查代理或网络"
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
            async with self.http.stream("GET", url) as response:
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
        except httpx.HTTPError:
            return None
        return None

    async def close(self):
        await self.http.aclose()

from __future__ import annotations

import base64
import ipaddress
from urllib.parse import urlparse

import httpx

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def extract_image_url(message: object) -> str | None:
    """Get the first http(s) image URL from an OneBot message."""
    try:
        segments = list(message)  # type: ignore[arg-type]
    except TypeError:
        return None
    for segment in segments:
        if getattr(segment, "type", None) != "image":
            continue
        data = getattr(segment, "data", {}) or {}
        url = data.get("url") or data.get("file")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _is_safe_host(host: str, *, allow_private: bool = False) -> bool:
    host = host.lower()
    if allow_private:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _detect_mime(data: bytes, header: str | None) -> str:
    if header:
        mime = header.split(";", 1)[0].strip().lower()
        if mime in _ALLOWED_MIME:
            return mime
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def fetch_image_bytes(
    url: str,
    *,
    max_bytes: int = 8_000_000,
    timeout_seconds: float = 15.0,
    allow_private_hosts: bool = False,
) -> tuple[bytes, str] | None:
    """Download a safe http(s) image and return ``(bytes, mime)``."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if not _is_safe_host(parsed.hostname, allow_private=allow_private_hosts):
        return None

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; QQ-AI-Bot/0.1)"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    return None
                return data, _detect_mime(data, response.headers.get("content-type"))
    except httpx.HTTPError:
        return None


async def download_image_as_data_url(
    url: str,
    *,
    max_bytes: int = 8_000_000,
    timeout_seconds: float = 15.0,
    allow_private_hosts: bool = False,
) -> str | None:
    fetched = await fetch_image_bytes(
        url,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        allow_private_hosts=allow_private_hosts,
    )
    if fetched is None:
        return None
    data, mime = fetched
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"

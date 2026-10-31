from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot

from .policy import PolicyViolation, check_public_text
from .settings import Settings

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
_QZONE_HOST = "user.qzone.qq.com"
_ALLOWED_PATHS = (
    "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
    "/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
    "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
    "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_delcomment",
)
_TAG_RE = re.compile(r"<[^>]+>")
_JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.DOTALL)
_UIN_RE = re.compile(r"^\d{5,20}$")
_TID_RE = re.compile(r"^[\w.-]{1,100}$")


class QzoneError(RuntimeError):
    pass


@dataclass(slots=True)
class QzoneComment:
    comment_id: str
    uin: str
    nickname: str
    content: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "uin": self.uin,
            "nickname": self.nickname,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class QzonePost:
    tid: str
    unikey: str
    content: str
    created_at: str
    images: list[str] = field(default_factory=list)
    like_count: int = 0
    liked: bool = False
    comments: list[QzoneComment] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tid": self.tid,
            "unikey": self.unikey,
            "content": self.content,
            "created_at": self.created_at,
            "images": list(self.images),
            "like_count": self.like_count,
            "liked": self.liked,
            "comments": [comment.as_dict() for comment in self.comments],
        }


@dataclass(slots=True)
class QzoneAuth:
    uin: str
    cookies: str
    bkn: str
    expires_at: float


def _text(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    raw = _TAG_RE.sub(" ", raw)
    return re.sub(r"[ \t\r\f\v]+", " ", raw).strip()


def _timestamp(value: Any) -> str:
    try:
        number = int(str(value or "0").strip())
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number, CN_TZ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _parse_json(text: str) -> Any:
    value = (text or "").strip()
    if not value:
        raise QzoneError("空间返回了空响应")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    match = _JSONP_RE.match(value)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"callback\s*\(\s*(\{.*?\})\s*\)", value, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    raise QzoneError("空间返回的不是可识别 JSON，接口可能改版或被风控")


def _error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "error", "wording"):
        value = _text(payload.get(key))
        if value:
            return value[:200]
    code = payload.get("code", payload.get("ret", ""))
    return f"错误码 {code}" if str(code) not in {"", "0"} else ""


def _is_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    ret = payload.get("ret", payload.get("code", 0))
    if str(ret) not in {"0", "", "None"}:
        return False
    subcode = payload.get("subcode", 0)
    return str(subcode) in {"0", "", "None"}


def _ensure_success(payload: Any, action: str) -> None:
    if not _is_success(payload):
        detail = _error_text(payload)
        raise QzoneError(f"{action}失败：{detail or '空间接口拒绝'}")


class QzoneService:
    """QQ 空间 H5 补全。

    NapCat 只提供发/删说说；这里用同一登录态调用空间 H5 接口，补列表、点赞、
    评论和删评论。Cookie 只缓存在内存里，不写日志，也不进审计。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._auth: QzoneAuth | None = None
        self._auth_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0
        self._comment_times: deque[float] = deque()
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.settings.qzone_web_enabled)

    def _bot(self) -> Bot:
        bots = get_bots()
        bot = next(iter(bots.values()), None) if bots else None
        if bot is None:
            raise QzoneError("NapCat 没有连接")
        return bot

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=float(self.settings.qzone_request_timeout),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def auth(self, bot: Bot | None = None, *, force: bool = False) -> QzoneAuth:
        if not self.enabled:
            raise QzoneError("空间 H5 功能未开启")
        now = time.monotonic()
        if not force and self._auth is not None and self._auth.expires_at > now:
            return self._auth
        async with self._auth_lock:
            if not force and self._auth is not None and self._auth.expires_at > now:
                return self._auth
            active = bot or self._bot()
            login = await active.call_api("get_login_info")
            if not isinstance(login, dict):
                raise QzoneError("拿不到当前 QQ 登录信息")
            uin = str(login.get("user_id") or login.get("uin") or "").strip()
            if not _UIN_RE.fullmatch(uin):
                raise QzoneError("当前 QQ 登录信息里没有有效号码")
            try:
                raw = await active.call_api("get_cookies", domain="qzone.qq.com")
            except Exception as exc:
                raise QzoneError(f"获取空间登录态失败：{exc}") from exc
            if not isinstance(raw, dict):
                raise QzoneError("空间登录态格式不对")
            cookies = str(raw.get("cookies") or "").strip()
            bkn = str(raw.get("bkn") or "").strip()
            if not bkn:
                try:
                    token = await active.call_api("get_csrf_token")
                    if isinstance(token, dict):
                        bkn = str(token.get("token") or "").strip()
                except Exception:
                    bkn = ""
            if not cookies or not bkn:
                raise QzoneError("空间登录态不完整，可能没登录或被风控")
            self._auth = QzoneAuth(
                uin=uin,
                cookies=cookies,
                bkn=bkn,
                expires_at=now + 600.0,
            )
            return self._auth

    def clear_auth(self) -> None:
        self._auth = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: QzoneAuth,
    ) -> Any:
        if path not in _ALLOWED_PATHS:
            raise QzoneError("空间接口路径不在白名单")
        url = f"https://{_QZONE_HOST}{path}"
        headers = {
            "Cookie": auth.cookies,
            "Referer": "https://user.qzone.qq.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }
        try:
            client = await self._http()
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise QzoneError(f"空间网络请求失败：{exc}") from exc
        if response.status_code in {401, 403}:
            self.clear_auth()
            raise QzoneError("空间登录态失效，请重新登录 QQ")
        if response.status_code >= 400:
            raise QzoneError(f"空间接口 HTTP {response.status_code}")
        return _parse_json(response.text)

    async def list_posts(
        self,
        bot: Bot | None = None,
        *,
        target_uin: str | None = None,
        limit: int = 20,
        pos: int = 0,
    ) -> list[QzonePost]:
        active = bot or self._bot()
        auth = await self.auth(active)
        uin = (target_uin or auth.uin).strip()
        if not _UIN_RE.fullmatch(uin):
            raise QzoneError("空间 QQ 号格式不对")
        count = max(1, min(int(limit), 20))
        payload = await self._request(
            "GET",
            "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
            params={
                "uin": uin,
                "ftype": 0,
                "sort": 0,
                "pos": max(0, int(pos)),
                "num": count,
                "replynum": 100,
                "g_tk": auth.bkn,
                "format": "json",
                "code_version": 1,
                "need_private_comment": 1,
            },
            auth=auth,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("msglist"), list):
            payload = data
        msglist = payload.get("msglist") if isinstance(payload, dict) else None
        if not isinstance(msglist, list):
            detail = _error_text(payload)
            raise QzoneError(f"空间列表读取失败：{detail or '接口没有返回 msglist'}")
        posts: list[QzonePost] = []
        for item in msglist:
            if not isinstance(item, dict):
                continue
            post = self._parse_post(item, self_uin=auth.uin, owner_uin=uin)
            if post is not None:
                posts.append(post)
        return posts

    @staticmethod
    def _parse_post(
        item: dict[str, Any],
        *,
        self_uin: str,
        owner_uin: str | None = None,
    ) -> QzonePost | None:
        tid = str(item.get("tid") or item.get("id") or "").strip()
        if not tid or not _TID_RE.fullmatch(tid):
            return None
        content = _text(item.get("content") or item.get("rt_con") or item.get("summary"))
        images: list[str] = []
        pics = item.get("pic") or item.get("pics") or []
        if isinstance(pics, dict):
            pics = [pics]
        if isinstance(pics, list):
            for pic in pics:
                if isinstance(pic, str):
                    url = pic.strip()
                elif isinstance(pic, dict):
                    url = str(
                        pic.get("url1")
                        or pic.get("url2")
                        or pic.get("url3")
                        or pic.get("url")
                        or ""
                    ).strip()
                else:
                    url = ""
                if url and url not in images:
                    images.append(url)
        likes = item.get("likes") or []
        liked = False
        like_count = 0
        if isinstance(likes, list):
            like_count = len(likes)
            liked = any(
                str(like.get("uin") or like.get("user_id") or "") == self_uin
                for like in likes
                if isinstance(like, dict)
            )
        elif isinstance(likes, int):
            like_count = max(0, likes)
        if isinstance(item.get("like_count"), int):
            like_count = max(like_count, int(item["like_count"]))
        if item.get("liked") is True or str(item.get("islike") or "") == "1":
            liked = True
        comments: list[QzoneComment] = []
        raw_comments = item.get("commentlist") or item.get("comments") or []
        if isinstance(raw_comments, list):
            for raw in raw_comments:
                if not isinstance(raw, dict):
                    continue
                comment_id = str(
                    raw.get("commentid")
                    or raw.get("commentId")
                    or raw.get("id")
                    or raw.get("tid")
                    or ""
                ).strip()
                if not comment_id:
                    continue
                comments.append(
                    QzoneComment(
                        comment_id=comment_id,
                        uin=str(raw.get("uin") or raw.get("user_id") or ""),
                        nickname=_text(raw.get("name") or raw.get("nickname")),
                        content=_text(raw.get("content")),
                        created_at=_timestamp(raw.get("create_time") or raw.get("created_time")),
                    )
                )
        unikey = str(item.get("unikey") or item.get("curkey") or "").strip()
        if not unikey:
            unikey = f"http://user.qzone.qq.com/{owner_uin or self_uin}/mood/{tid}"
        return QzonePost(
            tid=tid,
            unikey=unikey,
            content=content,
            created_at=_timestamp(item.get("created_time") or item.get("createTime") or item.get("time")),
            images=images,
            like_count=like_count,
            liked=liked,
            comments=comments,
        )

    async def _find_unikey(self, active: Bot, tid: str, auth: QzoneAuth) -> str:
        posts = await self.list_posts(active, limit=20)
        for post in posts:
            if post.tid == tid and post.unikey:
                return post.unikey
        raise QzoneError("这条动态不在最近列表里，缺少 unikey；请从空间列表复制 unikey 再试")

    async def _before_write(self, kind: str) -> None:
        rate = float(self.settings.qzone_rate_seconds)
        async with self._write_lock:
            now = time.monotonic()
            if kind == "comment":
                cutoff = now - 3600
                while self._comment_times and self._comment_times[0] < cutoff:
                    self._comment_times.popleft()
                if len(self._comment_times) >= int(self.settings.qzone_comment_hourly_limit):
                    raise QzoneError("这一小时空间评论次数到上限了，先歇会儿")
            wait = rate - (now - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            if kind == "comment":
                self._comment_times.append(time.monotonic())
            self._last_write = time.monotonic()

    async def set_like(
        self,
        bot: Bot | None,
        *,
        tid: str,
        active: bool = True,
        unikey: str = "",
    ) -> dict[str, Any]:
        tid = tid.strip()
        if not _TID_RE.fullmatch(tid):
            raise QzoneError("tid 格式不对")
        active_bot = bot or self._bot()
        auth = await self.auth(active_bot)
        key = unikey.strip() or await self._find_unikey(active_bot, tid, auth)
        await self._before_write("like")
        payload = await self._request(
            "POST",
            "/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
            params={"g_tk": auth.bkn},
            data={
                "opuin": auth.uin,
                "unikey": key,
                "fid": tid,
                "from": "1",
                "active": "0" if active else "1",
                "curlikekey": "",
                "plat": "qzone",
                "source": "1",
            },
            auth=auth,
        )
        _ensure_success(payload, "点赞" if active else "取消赞")
        return {"tid": tid, "active": active}

    async def comment(
        self,
        bot: Bot | None,
        *,
        tid: str,
        content: str,
    ) -> dict[str, Any]:
        tid = tid.strip()
        if not _TID_RE.fullmatch(tid):
            raise QzoneError("tid 格式不对")
        try:
            text = check_public_text(content.strip(), max_chars=200)
        except PolicyViolation as exc:
            raise QzoneError(str(exc)) from exc
        active_bot = bot or self._bot()
        auth = await self.auth(active_bot)
        await self._before_write("comment")
        payload = await self._request(
            "POST",
            "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
            params={"g_tk": auth.bkn},
            data={
                "topicId": f"{auth.uin}_{tid}",
                "content": text,
                "hostUin": auth.uin,
                "paramstr": "",
                "feedsType": "100",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "format": "json",
                "ref": f"http://user.qzone.qq.com/{auth.uin}",
                "srcid": "311",
                "plat": "qzone",
                "source": "1",
            },
            auth=auth,
        )
        _ensure_success(payload, "空间评论")
        comment_id = ""
        if isinstance(payload, dict):
            comment_id = str(
                payload.get("commentid")
                or payload.get("commentId")
                or payload.get("id")
                or ""
            ).strip()
        return {"tid": tid, "comment_id": comment_id, "content": text}

    async def delete_comment(
        self,
        bot: Bot | None,
        *,
        tid: str,
        comment_id: str,
    ) -> dict[str, Any]:
        tid = tid.strip()
        comment_id = comment_id.strip()
        if not _TID_RE.fullmatch(tid):
            raise QzoneError("tid 格式不对")
        if not _TID_RE.fullmatch(comment_id):
            raise QzoneError("comment_id 格式不对")
        active_bot = bot or self._bot()
        auth = await self.auth(active_bot)
        await self._before_write("comment")
        payload = await self._request(
            "POST",
            "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_delcomment",
            params={"g_tk": auth.bkn},
            data={
                "topicId": f"{auth.uin}_{tid}",
                "commentId": comment_id,
                "hostUin": auth.uin,
                "paramstr": "",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "format": "json",
                "ref": f"http://user.qzone.qq.com/{auth.uin}",
                "srcid": "311",
            },
            auth=auth,
        )
        _ensure_success(payload, "删除空间评论")
        return {"tid": tid, "comment_id": comment_id}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auth_cached": self._auth is not None,
            "uin": self._auth.uin if self._auth else "",
            "rate_seconds": float(self.settings.qzone_rate_seconds),
            "comment_hourly_limit": int(self.settings.qzone_comment_hourly_limit),
            "last_error": self.last_error,
        }

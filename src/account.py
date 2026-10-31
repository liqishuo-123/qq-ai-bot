from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from .image_utils import fetch_image_bytes
from .policy import PolicyViolation, check_public_text
from .settings import Settings

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_·\-]{1,16}$")
_TARGET_RE = re.compile(r"^\d{5,12}$")
_STATUS_MAP = {
    "在线": 10,
    "离开": 30,
    "隐身": 40,
    "忙碌": 50,
    "q我吧": 60,
    "请勿打扰": 70,
}
_ALLOWED_STATUS = set(_STATUS_MAP.values())


class AccountError(RuntimeError):
    pass


class AccountService:
    """Owner-only actions on the bot's own QQ account and the current group."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _call(self, bot: Bot, action: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(bot.call_api(action, **kwargs), timeout=30.0)
        except TimeoutError as exc:
            raise AccountError("NapCat 响应超时") from exc
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise AccountError(detail[:300]) from exc

    async def _image_payload(self, url: str) -> str:
        if not url or not url.startswith(("http://", "https://")):
            raise AccountError("需要一张 http(s) 图片")
        limit = max(1, min(int(self.settings.image_max_bytes), 5_000_000))
        fetched = await fetch_image_bytes(
            url,
            max_bytes=limit,
            allow_private_hosts=bool(self.settings.image_allow_private_hosts),
        )
        if fetched is None:
            raise AccountError("图片下载失败或超过大小限制")
        data, _mime = fetched
        if not data:
            raise AccountError("图片是空的")
        return "base64://" + base64.b64encode(data).decode("ascii")

    async def set_avatar(self, bot: Bot, url: str) -> None:
        payload = await self._image_payload(url)
        await self._call(bot, "set_qq_avatar", file=payload)

    async def set_nickname(self, bot: Bot, nickname: str) -> str:
        value = nickname.strip()
        if not _NAME_RE.fullmatch(value):
            raise AccountError("昵称需要 1–16 个中文、字母、数字或 ·_-")
        try:
            value = check_public_text(value, max_chars=16)
        except PolicyViolation as exc:
            raise AccountError(str(exc)) from exc
        await self._call(bot, "set_qq_profile", nickname=value)
        return value

    async def set_signature(self, bot: Bot, signature: str) -> str:
        value = signature.strip()
        if not value:
            raise AccountError("签名不能为空")
        try:
            value = check_public_text(value, max_chars=80)
        except PolicyViolation as exc:
            raise AccountError(str(exc)) from exc
        await self._call(bot, "set_self_longnick", longNick=value)
        return value

    async def set_status(self, bot: Bot, value: str) -> int:
        text = value.strip().lower()
        if text.isdigit():
            status = int(text)
        else:
            status = _STATUS_MAP.get(text, 0)
        if status not in _ALLOWED_STATUS:
            raise AccountError("状态只能是：在线、离开、隐身、忙碌、Q我吧、请勿打扰")
        await self._call(bot, "set_online_status", status=status, ext_status=0, battery_status=0)
        return status

    async def send_like(self, bot: Bot, target: str, times: int = 10) -> int:
        if not _TARGET_RE.fullmatch(str(target)):
            raise AccountError("QQ 号格式不对")
        count = max(1, min(int(times), 10))
        await self._call(bot, "send_like", user_id=str(target), times=count)
        return count

    async def poke(self, bot: Bot, group_id: str | int, target: str) -> None:
        if not _TARGET_RE.fullmatch(str(target)):
            raise AccountError("QQ 号格式不对")
        await self._call(
            bot,
            "send_poke",
            group_id=str(group_id),
            user_id=str(target),
        )

    async def set_group_portrait(self, bot: Bot, group_id: str | int, url: str) -> None:
        payload = await self._image_payload(url)
        result = await self._call(
            bot,
            "set_group_portrait",
            group_id=str(group_id),
            file=payload,
        )
        if isinstance(result, dict) and int(result.get("result", 0) or 0) != 0:
            raise AccountError(str(result.get("errMsg") or "设置群头像失败"))

    async def set_special_title(
        self,
        bot: Bot,
        group_id: str | int,
        target: str,
        title: str,
    ) -> str:
        if not _TARGET_RE.fullmatch(str(target)):
            raise AccountError("QQ 号格式不对")
        value = title.strip()
        if len(value) > 20:
            raise AccountError("专属头衔最多 20 个字")
        await self._call(
            bot,
            "set_group_special_title",
            group_id=str(group_id),
            user_id=str(target),
            special_title=value,
        )
        return value

    async def group_sign(self, bot: Bot, group_id: str | int) -> None:
        await self._call(bot, "send_group_sign", group_id=str(group_id))

    async def set_essence(self, bot: Bot, message_id: str | int) -> None:
        await self._call(bot, "set_essence_msg", message_id=str(message_id))

    async def remove_essence(self, bot: Bot, message_id: str | int) -> None:
        await self._call(bot, "del_essence_msg", message_id=str(message_id))

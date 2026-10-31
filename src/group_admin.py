from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from .policy import can_send_proactive_group, check_group_notice

logger = logging.getLogger(__name__)

_AT_RE = re.compile(r"^\s*(\d{5,12})\b")
_DURATION_RE = re.compile(r"^(\d+)\s*(s|秒|m|分|分钟|h|小时|d|天)?$", re.IGNORECASE)
_ROLE_CACHE_SECONDS = 60.0


class GroupActionError(RuntimeError):
    pass


def target_from_event(event: GroupMessageEvent) -> str | None:
    """Prefer a replied message, then the first @ in the message."""
    reply = getattr(event, "reply", None)
    if reply is not None:
        user_id = getattr(getattr(reply, "sender", None), "user_id", None)
        if user_id and str(user_id) != str(event.self_id):
            return str(user_id)
    for segment in event.get_message():
        if getattr(segment, "type", None) != "at":
            continue
        data = getattr(segment, "data", {}) or {}
        qq = str(data.get("qq", ""))
        if qq and qq != "all" and qq != str(event.self_id):
            return qq
    return None


def target_from_args(args: str, *, explicit: str | None = None) -> tuple[str | None, str]:
    """Return (target, remaining text). Explicit reply/at wins.

    A numeric target at the start of args is accepted for convenience.
    """
    value = args.strip()
    if explicit:
        return explicit, value
    match = _AT_RE.match(value)
    if match:
        return match.group(1), value[match.end() :].strip()
    return None, value


def parse_duration(value: str, *, default_seconds: int = 600) -> int:
    text = (value or "").strip().lower()
    if not text:
        return default_seconds
    match = _DURATION_RE.fullmatch(text)
    if not match:
        raise GroupActionError("时长格式不对，例如 10m、1h、30s")
    amount = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit in {"m", "分", "分钟"}:
        seconds = amount * 60
    elif unit in {"h", "小时"}:
        seconds = amount * 3600
    elif unit in {"d", "天"}:
        seconds = amount * 86400
    else:
        seconds = amount
    if seconds < 1:
        raise GroupActionError("时长至少 1 秒")
    return min(seconds, 30 * 86400)


class GroupAdminService:
    def __init__(self, superuser_ids: set[str] | None = None) -> None:
        self.superuser_ids = {str(item) for item in (superuser_ids or set())}
        self._roles: dict[tuple[str, str], tuple[str, float]] = {}
        self._bot_roles: dict[str, tuple[str, float]] = {}

    async def _call(self, bot: Bot, action: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(
                bot.call_api(action, **kwargs),
                timeout=30.0,
            )
        except TimeoutError as exc:
            raise GroupActionError("NapCat 响应超时") from exc
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise GroupActionError(detail[:300]) from exc

    async def get_member_role(self, bot: Bot, group_id: str | int, user_id: str | int) -> str:
        key = (str(group_id), str(user_id))
        cached = self._roles.get(key)
        now = time.monotonic()
        if cached and now - cached[1] < _ROLE_CACHE_SECONDS:
            return cached[0]
        data = await self._call(
            bot,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=True,
        )
        role = str((data or {}).get("role", "member")).lower()
        self._roles[key] = (role, now)
        return role

    async def get_bot_role(self, bot: Bot, group_id: str | int) -> str:
        key = str(group_id)
        cached = self._bot_roles.get(key)
        now = time.monotonic()
        if cached and now - cached[1] < _ROLE_CACHE_SECONDS:
            return cached[0]
        role = await self.get_member_role(bot, group_id, bot.self_id)
        self._bot_roles[key] = (role, now)
        return role

    async def require_admin(self, bot: Bot, event: GroupMessageEvent) -> str:
        role = await self.get_bot_role(bot, event.group_id)
        if role not in {"admin", "owner"}:
            raise GroupActionError("我还不是这个群的管理员，先在群设置里给我管理员。")
        return role

    async def require_owner(self, bot: Bot, event: GroupMessageEvent) -> str:
        role = await self.get_bot_role(bot, event.group_id)
        if role != "owner":
            raise GroupActionError("这个操作需要我是群主。")
        return role

    async def guard_target(self, bot: Bot, event: GroupMessageEvent, target: str) -> None:
        if str(target) == str(event.self_id):
            raise GroupActionError("不能操作我自己。")
        if str(target) == str(event.user_id):
            raise GroupActionError("不能操作发指令的人自己。")
        if str(target) in self.superuser_ids:
            raise GroupActionError("不能操作主人。")
        role = await self.get_member_role(bot, event.group_id, target)
        if role == "owner":
            raise GroupActionError("不能操作群主。")

    async def mute(self, bot: Bot, event: GroupMessageEvent, target: str, seconds: int) -> None:
        await self.require_admin(bot, event)
        await self.guard_target(bot, event, target)
        await self._call(
            bot,
            "set_group_ban",
            group_id=int(event.group_id),
            user_id=int(target),
            duration=int(seconds),
        )

    async def unmute(self, bot: Bot, event: GroupMessageEvent, target: str) -> None:
        await self.require_admin(bot, event)
        await self.guard_target(bot, event, target)
        await self._call(
            bot,
            "set_group_ban",
            group_id=int(event.group_id),
            user_id=int(target),
            duration=0,
        )

    async def kick(self, bot: Bot, event: GroupMessageEvent, target: str) -> None:
        await self.require_admin(bot, event)
        await self.guard_target(bot, event, target)
        await self._call(
            bot,
            "set_group_kick",
            group_id=int(event.group_id),
            user_id=int(target),
            reject_add_request=False,
        )

    async def recall(self, bot: Bot, event: GroupMessageEvent, message_id: str) -> None:
        await self.require_admin(bot, event)
        await self._call(bot, "delete_msg", message_id=int(message_id))

    async def notice(self, bot: Bot, event: GroupMessageEvent, content: str) -> None:
        role = await self.require_admin(bot, event)
        if not can_send_proactive_group(role):
            raise GroupActionError("主动发群消息需要机器人是管理员。")
        text = check_group_notice(content)
        await self._call(
            bot,
            "_send_group_notice",
            group_id=int(event.group_id),
            content=text,
            pinned=0,
            type=1,
            confirm_required=0,
        )

    async def set_card(self, bot: Bot, event: GroupMessageEvent, target: str, card: str) -> None:
        await self.require_admin(bot, event)
        await self.guard_target(bot, event, target)
        value = card.strip()
        if not value or len(value) > 30:
            raise GroupActionError("群名片需要 1–30 个字。")
        await self._call(
            bot,
            "set_group_card",
            group_id=int(event.group_id),
            user_id=int(target),
            card=value,
        )

    async def set_whole_ban(self, bot: Bot, event: GroupMessageEvent, enable: bool) -> None:
        await self.require_admin(bot, event)
        await self._call(
            bot,
            "set_group_whole_ban",
            group_id=int(event.group_id),
            enable=enable,
        )

    async def set_admin(self, bot: Bot, event: GroupMessageEvent, target: str, enable: bool) -> None:
        await self.require_owner(bot, event)
        await self.guard_target(bot, event, target)
        await self._call(
            bot,
            "set_group_admin",
            group_id=int(event.group_id),
            user_id=int(target),
            enable=enable,
        )
        self._roles.pop((str(event.group_id), str(target)), None)

    async def set_group_name(self, bot: Bot, event: GroupMessageEvent, name: str) -> None:
        await self.require_admin(bot, event)
        value = name.strip()
        if not 1 <= len(value) <= 30:
            raise GroupActionError("群名需要 1–30 个字。")
        await self._call(
            bot,
            "set_group_name",
            group_id=int(event.group_id),
            group_name=value,
        )

    def clear_cache(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._roles.clear()
            self._bot_roles.clear()
            return
        self._roles = {key: value for key, value in self._roles.items() if key[0] != str(group_id)}
        self._bot_roles.pop(str(group_id), None)

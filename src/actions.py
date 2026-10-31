from __future__ import annotations

import json
import logging
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from .guild import GuildService
from .qzone import QzoneService
from .skills import EXECUTORS, SKILL_DEFS, SkillService

logger = logging.getLogger(__name__)

MAX_PARAM_CHARS = 4000
_MAX_STR_PARAMS = 8000


class ActionError(RuntimeError):
    pass


def _sanitize_params(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ActionError("参数必须是 JSON 对象")
    try:
        encoded = json.dumps(params, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ActionError("参数无法序列化为 JSON") from exc
    if len(encoded) > MAX_PARAM_CHARS:
        raise ActionError("参数太大，最多 4000 字符")
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        name = str(key).strip()
        if not name or len(name) > 64 or any(ch in name for ch in "\r\n\0"):
            raise ActionError(f"参数名不合法：{name[:32]}")
        if isinstance(value, str) and len(value) > _MAX_STR_PARAMS:
            raise ActionError(f"参数 {name} 太长")
        cleaned[name] = value
    return cleaned


class ActionService:
    """统一能力表执行器：权限 → 内容过滤 → 路由 executor → 审计。

    - executor="onebot"：直接调用 NapCat 的同名 action（api.* 技能）。
    - executor="qzone"：路由到 QzoneService。
    - executor="guild"：路由到 GuildService。
    - executor="core"：不走这里；这些技能有专门的 QQ 命令与控制台入口。
    """

    def __init__(
        self,
        settings: Any,
        skills: SkillService,
        qzone: QzoneService,
        guild: GuildService,
    ) -> None:
        self.settings = settings
        self.skills = skills
        self.qzone = qzone
        self.guild = guild

    def catalog(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for skill in SKILL_DEFS.values():
            items.append(
                {
                    "id": skill.skill_id,
                    "name": skill.name,
                    "category": skill.category,
                    "description": skill.description,
                    "risk": skill.risk,
                    "executor": skill.executor,
                    "readonly": skill.readonly,
                    "group_only": skill.group_only,
                    "default_min_role": skill.default_min_role,
                }
            )
        return items

    async def execute(
        self,
        skill_id: str,
        *,
        params: dict[str, Any] | None = None,
        bot: Bot | None = None,
        role: str = "owner",
        actor: str = "console",
        scope_type: str = "private",
        scope_id: str = "self",
    ) -> dict[str, Any]:
        skill = SKILL_DEFS.get(skill_id)
        if skill is None:
            raise ActionError(f"能力表里没有这个动作：{skill_id}")
        if skill.executor not in EXECUTORS:
            raise ActionError("动作执行器不合法")
        if skill.executor == "core":
            raise ActionError("这个技能有专门入口，不在统一执行器里运行")
        cleaned = _sanitize_params(params)
        try:
            await self.skills.check_role(
                skill_id,
                role=role,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        except Exception as exc:
            await self.skills.audit(
                skill_id,
                scope_type=scope_type,
                scope_id=scope_id,
                actor_id=actor,
                args=cleaned.get("args", ""),
                status="denied",
                error=str(exc),
            )
            raise ActionError(str(exc)) from exc
        try:
            result = await self._dispatch(skill.executor, skill.skill_id, cleaned, bot)
        except Exception as exc:
            message = str(exc)[:300]
            await self.skills.audit(
                skill_id,
                scope_type=scope_type,
                scope_id=scope_id,
                actor_id=actor,
                args=cleaned.get("args", ""),
                status="failed",
                error=message,
            )
            raise ActionError(message) from exc
        await self.skills.audit(
            skill_id,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_id=actor,
            target_id=str(result.get("tid", result.get("message_id", "")) or "")[:64]
            if isinstance(result, dict)
            else "",
            args=cleaned.get("args", ""),
            status="ok",
        )
        return {"skill_id": skill_id, "executor": skill.executor, "result": result}

    async def _dispatch(
        self,
        executor: str,
        skill_id: str,
        params: dict[str, Any],
        bot: Bot | None,
    ) -> Any:
        if executor == "onebot":
            active = bot
            if active is None:
                raise ActionError("NapCat 没有连接")
            action = skill_id.removeprefix("api.")
            try:
                return await active.call_api(action, **params)
            except Exception as exc:
                raise ActionError(f" NapCat 调用 {action} 失败：{exc}") from exc
        if executor == "qzone":
            return await self._qzone(skill_id, params, bot)
        if executor == "guild":
            return await self._guild(skill_id, params, bot)
        raise ActionError("动作执行器不合法")

    async def _qzone(self, skill_id: str, params: dict[str, Any], bot: Bot | None) -> Any:
        target = str(params.get("uin") or params.get("target_uin") or "").strip() or None
        tid = str(params.get("tid") or "").strip()
        if skill_id == "qzone.list":
            limit = int(params.get("num") or params.get("limit") or 20)
            pos = int(params.get("pos") or 0)
            posts = await self.qzone.list_posts(bot, target_uin=target, limit=limit, pos=pos)
            return {"posts": [post.as_dict() for post in posts]}
        if skill_id == "qzone.like":
            active = str(params.get("active", "1")).strip() in {"1", "true", "True", "on"}
            return await self.qzone.set_like(
                bot,
                tid=tid,
                active=active,
                unikey=str(params.get("unikey") or ""),
            )
        if skill_id == "qzone.comment":
            content = str(params.get("content") or "").strip()
            if not content:
                raise ActionError("评论内容不能为空")
            return await self.qzone.comment(bot, tid=tid, content=content)
        if skill_id == "qzone.comment_delete":
            comment_id = str(params.get("comment_id") or "").strip()
            return await self.qzone.delete_comment(
                bot,
                tid=tid,
                comment_id=comment_id,
            )
        if skill_id == "qzone.delete":
            raise ActionError("删说说要走 NapCat 的 delete_qzone_msg 或控制台空间面板")
        raise ActionError("空间动作不合法")

    async def _guild(self, skill_id: str, params: dict[str, Any], bot: Bot | None) -> Any:
        if skill_id == "guild.status":
            return await self.guild.status(bot)
        if skill_id == "guild.list":
            return await self.guild.list_channels(bot)
        if skill_id == "guild.history":
            channel_id = str(params.get("channel_id") or "").strip() or None
            limit = int(params.get("limit") or 20)
            return await self.guild.history(bot, channel_id=channel_id, limit=limit)
        if skill_id == "guild.send":
            content = str(params.get("content") or "").strip()
            if not content:
                raise ActionError("频道消息内容不能为空")
            channel_id = str(params.get("channel_id") or "").strip() or None
            return await self.guild.send(bot, content, channel_id=channel_id)
        raise ActionError("频道动作不合法")

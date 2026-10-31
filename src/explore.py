from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from .autonomy import AutonomyService
from .db import Database
from .settings import Settings

logger = logging.getLogger(__name__)
_SENSITIVE_RE = re.compile(
    r"(?i)(token|cookie|skey|password|authorization|csrf)[\"']?\s*[:=]\s*[\"']?[^,}\s]+"
)


@dataclass(frozen=True, slots=True)
class Capability:
    action: str
    category: str
    description: str
    needs_group: bool = False
    needs_self: bool = False


# 只探测只读接口：探索模式不应该因为“好奇”去踢人、发消息或改资料。
CAPABILITIES: tuple[Capability, ...] = (
    Capability("get_login_info", "账号", "当前登录的 QQ 账号"),
    Capability("get_status", "账号", "NapCat 在线状态"),
    Capability("get_version_info", "账号", "QQ/NapCat 版本"),
    Capability("get_group_list", "群聊", "机器人加入的群"),
    Capability("get_friend_list", "好友", "机器人好友列表"),
    Capability("get_recent_contact", "消息", "最近联系人"),
    Capability("get_group_info", "群聊", "群名称、人数等资料", needs_group=True),
    Capability("get_group_info_ex", "群聊", "群扩展资料", needs_group=True),
    Capability("get_group_member_list", "群成员", "群成员列表", needs_group=True),
    Capability(
        "get_group_member_info",
        "群成员",
        "机器人在群里的身份",
        needs_group=True,
        needs_self=True,
    ),
    Capability("get_group_msg_history", "消息", "群历史消息", needs_group=True),
    Capability("get_group_notice", "群聊", "群公告列表", needs_group=True),
    Capability("get_essence_msg_list", "群聊", "群精华消息", needs_group=True),
    Capability("get_group_honor_info", "群聊", "群荣誉信息", needs_group=True),
    Capability("get_group_at_all_remain", "群聊", "剩余 @全体成员 次数", needs_group=True),
    Capability("get_group_shut_list", "群管理", "当前被禁言成员", needs_group=True),
    Capability("get_group_file_system_info", "群文件", "群文件容量", needs_group=True),
    Capability("get_group_root_files", "群文件", "群根目录文件", needs_group=True),
    Capability("get_stranger_info", "账号", "查询一个 QQ 的公开资料", needs_self=True),
)


class ExploreError(RuntimeError):
    pass


class ExplorationService:
    """Read-only QQ capability probing plus explore-mode state.

    The mode itself lives in AutonomyService so the autonomy gate has one
    source of truth. In explore mode the bot accepts all normal skill requests
    without an LLM veto; the hard policy checks in the skill handlers still run.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        autonomy: AutonomyService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.autonomy = autonomy
        self._run_lock = asyncio.Lock()

    async def enabled(self) -> bool:
        state = await self.autonomy.get_state()
        return bool(state.enabled and state.mode == "explore")

    async def set_enabled(self, enabled: bool) -> bool:
        state = await self.autonomy.get_state()
        await self.autonomy.set_state(
            enabled=True,
            directive=state.directive,
            mode="explore" if enabled else "normal",
        )
        return enabled

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "action": item.action,
                "category": item.category,
                "description": item.description,
                "needs_group": item.needs_group,
                "needs_self": item.needs_self,
                "read_only": True,
            }
            for item in CAPABILITIES
        ]

    async def status(self) -> dict[str, Any]:
        state = await self.autonomy.get_state()
        findings = await self.db.list_explore_findings(limit=100)
        return {
            "enabled": bool(state.enabled and state.mode == "explore"),
            "mode": state.mode,
            "autonomy_enabled": state.enabled,
            "findings": [self._finding_dict(item) for item in findings],
            "catalog": self.catalog(),
            "note": "探索模式只调用只读接口，不会踢人、发消息或改资料；自主判断在该模式下全放行。",
        }

    async def run(self, bot: Bot, *, group_id: str | None = None) -> list[dict[str, Any]]:
        if not await self.enabled():
            raise ExploreError("探索模式未开启")
        async with self._run_lock:
            return await self._run_cycle(bot, group_id=group_id)

    async def _run_cycle(self, bot: Bot, *, group_id: str | None = None) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        supported: list[str] = []
        resolved_group = group_id

        discovered = await self._call(bot, "get_supported_actions", {})
        if discovered["ok"]:
            supported = self._action_names(discovered["data"])
            await self._record(
                "get_supported_actions",
                category="发现",
                status="ok",
                summary=f"发现 {len(supported)} 个 OneBot 动作",
                detail=self._preview(supported[:120]),
            )
            findings.append(
                {
                    "action": "get_supported_actions",
                    "status": "ok",
                    "summary": f"发现 {len(supported)} 个 OneBot 动作",
                }
            )
        else:
            await self._record(
                "get_supported_actions",
                category="发现",
                status="error",
                summary=discovered["error"],
            )
            findings.append(
                {
                    "action": "get_supported_actions",
                    "status": "error",
                    "summary": discovered["error"],
                }
            )

        groups = await self._call(bot, "get_group_list", {})
        if groups["ok"] and isinstance(groups["data"], list):
            group_ids = [
                str(item.get("group_id"))
                for item in groups["data"]
                if isinstance(item, dict) and item.get("group_id")
            ]
            if resolved_group is None and group_ids:
                resolved_group = group_ids[0]
            await self._record(
                "get_group_list",
                category="群聊",
                status="ok",
                summary=f"机器人加入了 {len(group_ids)} 个群",
                detail=self._preview(groups["data"]),
            )
            findings.append(
                {
                    "action": "get_group_list",
                    "status": "ok",
                    "summary": f"机器人加入了 {len(group_ids)} 个群",
                }
            )
        elif groups["ok"]:
            findings.append({"action": "get_group_list", "status": "error", "summary": "返回格式不对"})
        else:
            await self._record(
                "get_group_list",
                category="群聊",
                status="error",
                summary=groups["error"],
            )
            findings.append(
                {"action": "get_group_list", "status": "error", "summary": groups["error"]}
            )

        self_id = str(getattr(bot, "self_id", "") or "")
        probes = [
            item
            for item in CAPABILITIES
            if item.action not in {"get_supported_actions", "get_group_list"}
        ]
        if supported:
            known = set(supported)
            probes = [item for item in probes if item.action in known]

        budget = max(1, min(int(self.settings.explore_probe_limit), 20))
        for capability in probes[:budget]:
            if not self._params_ready(capability, resolved_group, self_id):
                continue
            params = self._params(capability, resolved_group, self_id)
            result = await self._call(bot, capability.action, params)
            if result["ok"]:
                summary = self._summary(result["data"])
                status = "ok"
                detail = self._preview(result["data"])
            else:
                summary = result["error"]
                status = "error"
                detail = ""
            await self._record(
                capability.action,
                category=capability.category,
                status=status,
                summary=summary,
                detail=detail,
            )
            findings.append(
                {
                    "action": capability.action,
                    "category": capability.category,
                    "status": status,
                    "summary": summary,
                }
            )

        return findings

    async def _record(
        self,
        action: str,
        *,
        category: str,
        status: str,
        summary: str,
        detail: str = "",
    ) -> None:
        await self.db.add_explore_finding(
            action,
            category=category,
            status=status,
            summary=summary,
            detail=detail,
        )

    async def _call(self, bot: Bot, action: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await asyncio.wait_for(
                bot.call_api(action, **params),
                timeout=max(3.0, min(float(self.settings.explore_timeout), 60.0)),
            )
            return {"ok": True, "data": data, "error": ""}
        except TimeoutError:
            return {"ok": False, "data": None, "error": "NapCat 响应超时"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": str(exc).strip()[:300] or "接口失败"}

    @staticmethod
    def _params_ready(capability: Capability, group_id: str | None, self_id: str) -> bool:
        if capability.needs_group and not group_id:
            return False
        if capability.needs_self and not self_id:
            return False
        return True

    @staticmethod
    def _params(capability: Capability, group_id: str | None, self_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if capability.needs_group and group_id:
            params["group_id"] = group_id
        if capability.needs_self and self_id:
            params["user_id"] = self_id
        if capability.action == "get_group_msg_history":
            params["count"] = 1
        if capability.action == "get_group_honor_info":
            params["type"] = "all"
        return params

    @staticmethod
    def _action_names(data: Any) -> list[str]:
        if isinstance(data, list):
            return [str(item) for item in data if isinstance(item, str)]
        if isinstance(data, dict):
            actions = data.get("actions")
            if isinstance(actions, list):
                return [str(item) for item in actions if isinstance(item, str)]
            return [str(key) for key in data.keys()]
        return []

    @staticmethod
    def _summary(data: Any) -> str:
        if isinstance(data, list):
            return f"返回 {len(data)} 条"
        if isinstance(data, dict):
            return "字段：" + "、".join(list(data.keys())[:12])
        text = str(data or "").replace("\n", " ").strip()
        return text[:200] or "返回空"

    @staticmethod
    def _preview(data: Any) -> str:
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(data)
        text = _SENSITIVE_RE.sub(r"\1=***", text)
        return text[:1800]

    @staticmethod
    def _finding_dict(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "action": item.action,
            "category": item.category,
            "status": item.status,
            "summary": item.summary,
            "detail": item.detail,
            "created_at": item.created_at,
        }

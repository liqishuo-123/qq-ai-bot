from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from nonebot.adapters.onebot.v11 import MessageEvent

from .db import Database
from .llm import DeepSeekClient, LLMError
from .persona import Persona
from .settings import Settings
from .skills import ROLE_LABELS, SKILL_DEFS, SkillService

logger = logging.getLogger(__name__)
_STATE_CACHE_SECONDS = 3.0
_SKIP_SKILLS = {"brain.search", "brain.read"}


class AutonomyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AutonomyState:
    enabled: bool
    directive: str
    mode: str = "normal"
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    allow: bool
    reason: str
    source: str = "bot"


class AutonomyService:
    """Bot-level judgement, with console directives above the bot.

    The console can disable judgement (then normal skill permissions apply) or
    write a directive that the decision model must follow. Hard policy checks
    still run before and after this layer.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: DeepSeekClient,
        persona: Persona,
        skills: SkillService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.persona = persona
        self.skills = skills
        self.error_handler: Callable[[str, str], None] | None = None
        self._state_cache: tuple[float, AutonomyState] | None = None
        self._cache: dict[tuple[str, str, str, str], tuple[float, AutonomyDecision]] = {}

    async def get_state(self) -> AutonomyState:
        now = time.monotonic()
        if self._state_cache and now - self._state_cache[0] < _STATE_CACHE_SECONDS:
            return self._state_cache[1]
        raw_enabled = await self.db.get_social_state("autonomy.enabled")
        enabled = self.settings.autonomy_enabled if raw_enabled is None else raw_enabled == "1"
        directive = await self.db.get_social_state("autonomy.directive") or ""
        raw_mode = await self.db.get_social_state("autonomy.mode")
        default_mode = "explore" if self.settings.explore_enabled else "normal"
        mode = raw_mode if raw_mode in {"normal", "explore"} else default_mode
        updated_at = await self.db.get_social_state("autonomy.updated_at") or ""
        state = AutonomyState(
            enabled=bool(enabled),
            directive=directive[:600],
            mode=mode,
            updated_at=updated_at,
        )
        self._state_cache = (now, state)
        return state

    async def set_state(
        self,
        *,
        enabled: bool,
        directive: str,
        mode: str = "normal",
    ) -> AutonomyState:
        text = "".join(
            ch for ch in str(directive or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32
        ).strip()
        text = text[:600]
        clean_mode = mode if mode in {"normal", "explore"} else "normal"
        await self.db.set_social_state("autonomy.enabled", "1" if enabled else "0")
        await self.db.set_social_state("autonomy.directive", text)
        await self.db.set_social_state("autonomy.mode", clean_mode)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        await self.db.set_social_state("autonomy.updated_at", now)
        self._state_cache = None
        self._cache.clear()
        logger.info(
            "Autonomy state changed: enabled=%s mode=%s directive_chars=%s",
            enabled,
            clean_mode,
            len(text),
        )
        return AutonomyState(
            enabled=enabled,
            directive=text,
            mode=clean_mode,
            updated_at=now,
        )

    async def review(
        self,
        event: MessageEvent,
        skill_id: str,
        *,
        target_id: str | None = None,
        args: str = "",
    ) -> AutonomyDecision:
        state = await self.get_state()
        if not state.enabled:
            return AutonomyDecision(True, "控制台关闭了自主判断", "console")
        if state.mode == "explore":
            return AutonomyDecision(True, "探索模式，自主意识全开", "explore")
        if skill_id in _SKIP_SKILLS:
            return AutonomyDecision(True, "只读操作", "bot")

        role = self.skills.role_for_event(event)
        key = (skill_id, target_id or "", args[:600], role)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < max(5, self.settings.autonomy_cache_seconds):
            return cached[1]

        skill = SKILL_DEFS.get(skill_id)
        label = skill.name if skill else skill_id
        try:
            payload, result = await self.llm.decide_action(
                persona=self.persona,
                action=label,
                skill_id=skill_id,
                role=ROLE_LABELS.get(role, role),
                args=args,
                target=target_id or "",
                console_directive=state.directive,
            )
            await self.db.log_usage(
                "autonomy",
                skill_id,
                result.model,
                result.input_tokens,
                result.output_tokens,
            )
        except LLMError as exc:
            if self.error_handler:
                self.error_handler("autonomy", str(exc))
            return AutonomyDecision(False, "我现在脑子卡住了，这事先不干", "error")

        allow = payload.get("allow")
        if isinstance(allow, str):
            allow = allow.strip().lower() in {"1", "true", "yes", "allow", "可以"}
        if not isinstance(allow, bool):
            allow = False
        reason = str(payload.get("reason") or "").strip()[:80]
        if not reason:
            reason = "我懒得动" if not allow else "行"
        decision = AutonomyDecision(allow, reason, "bot")
        self._cache[key] = (now, decision)
        return decision

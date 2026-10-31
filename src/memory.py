from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .db import Database
from .llm import DeepSeekClient
from .models import MemoryItem
from .safety import contains_sensitive
from .settings import Settings

if TYPE_CHECKING:
    from .brain import BrainService

logger = logging.getLogger(__name__)

_ALLOWED_CATEGORIES = {"fact", "preference", "event", "relationship"}


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: DeepSeekClient,
        brain: BrainService | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.brain = brain
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def schedule_extract(self, *, scope_type: str, scope_id: str, user_id: str, session_id: int) -> None:
        if not self.settings.memory_enabled:
            return
        task = asyncio.create_task(
            self.maybe_extract(
                scope_type=scope_type,
                scope_id=scope_id,
                user_id=user_id,
                session_id=session_id,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def maybe_extract(self, *, scope_type: str, scope_id: str, user_id: str, session_id: int) -> None:
        try:
            if not self.settings.memory_enabled:
                return
            count = await self.db.count_messages(session_id)
            if count < self.settings.memory_extract_every:
                return
            if count % self.settings.memory_extract_every != 0:
                return

            lock = self._locks.setdefault(session_id, asyncio.Lock())
            if lock.locked():
                return
            async with lock:
                messages = await self.db.get_messages(session_id, limit=self.settings.memory_extract_every)
                if not messages:
                    return
                # 群聊只抽取当前用户自己的发言，避免把别人的隐私写进记忆。
                selected = [
                    item
                    for item in messages
                    if item.role == "assistant" or (item.role == "user" and item.user_id == user_id)
                ]
                if not selected:
                    return
                transcript = "\n".join(
                    f"{'助手' if item.role == 'assistant' else '用户'}：{item.content}" for item in selected
                )
                extracted = await self.llm.extract_memories(transcript)

            accepted: list[str] = []
            for item in extracted[:5]:
                content = str(item.get("content", "")).strip()
                if not (2 <= len(content) <= 200):
                    continue
                if contains_sensitive(content):
                    continue
                category = str(item.get("category", "fact"))
                if category not in _ALLOWED_CATEGORIES:
                    category = "fact"
                try:
                    confidence = float(item.get("confidence", 0.8))
                except (TypeError, ValueError):
                    confidence = 0.8
                if confidence < 0.55:
                    continue
                added = await self.db.add_memory(
                    scope_type,
                    scope_id,
                    content,
                    user_id=user_id if scope_type == "group" else None,
                    category=category,
                    confidence=confidence,
                    ttl_days=self.settings.memory_ttl_days,
                )
                if added:
                    accepted.append(content)
            if accepted and self.brain is not None:
                try:
                    await self.brain.capture_memories(
                        scope_type=scope_type,
                        scope_id=scope_id,
                        user_id=user_id,
                        contents=accepted,
                    )
                except Exception:
                    logger.exception("Brain capture failed for %s:%s", scope_type, scope_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Memory extraction failed for %s:%s", scope_type, scope_id)

    async def remember(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        content: str,
        category: str = "fact",
        confidence: float = 1.0,
    ) -> bool:
        content = content.strip()
        if not content or contains_sensitive(content):
            return False
        return await self.db.add_memory(
            scope_type,
            scope_id,
            content,
            user_id=user_id if scope_type == "group" else None,
            category=category,
            confidence=confidence,
            ttl_days=self.settings.memory_ttl_days,
        )

    async def list_memories(
        self, *, scope_type: str, scope_id: str, user_id: str, limit: int = 20
    ) -> list[MemoryItem]:
        return await self.db.list_memories(
            scope_type,
            scope_id,
            user_id=user_id if scope_type == "group" else None,
            limit=limit,
        )

    async def forget_user(self, *, scope_type: str, scope_id: str, user_id: str) -> int:
        return await self.db.delete_user_memories(scope_type, scope_id, user_id)

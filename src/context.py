from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .llm import DeepSeekClient
from .models import SessionState, StoredMessage
from .persona import Persona
from .settings import Settings

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


class ContextBuilder:
    def __init__(self, settings: Settings, db: Database, llm: DeepSeekClient, persona: Persona) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.persona = persona
        self._summary_locks: dict[int, asyncio.Lock] = {}

    async def build(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        nickname: str | None,
        text: str,
        style_hint: str | None = None,
        brain_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        session = await self.db.get_or_create_session(scope_type, scope_id)
        history = await self.db.get_messages(session.id, limit=self.settings.context_messages)
        memories = await self.db.search_memories(
            scope_type,
            scope_id,
            text,
            user_id=user_id if scope_type == "group" else None,
            limit=self.settings.memory_limit,
        )

        scene = "QQ 群聊" if scope_type == "group" else "QQ 私聊"
        now = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
        system_parts = [
            self.persona.system_prompt(),
            f"\n当前场景：{scene}。现在时间：{now}（北京时间）。",
        ]
        if nickname:
            system_parts.append(f"当前说话的人叫：{nickname}。")
        if style_hint:
            system_parts.append("\n" + style_hint.strip())
        if session.summary:
            system_parts.append(f"\n之前聊过的事（摘要）：\n{session.summary}")
        if memories:
            memory_text = "\n".join(f"- {item.content}" for item in memories)
            system_parts.append(
                "\n你记得这些信息（只在相关时自然使用，不要逐条复述，也不要暴露记忆系统）：\n" + memory_text
            )
        if brain_hint:
            system_parts.append("\n" + brain_hint.strip())

        messages: list[dict[str, Any]] = [{"role": "system", "content": "\n".join(system_parts)}]
        for item in history:
            messages.append(self._format_history(item))

        # 控制上下文体积。保留最后几条，从最旧的历史开始丢。
        while len(messages) > 2 and self._approx_chars(messages) > self.settings.max_context_chars:
            messages.pop(1)

        return messages

    def _format_history(self, item: StoredMessage) -> dict[str, Any]:
        if item.role == "assistant":
            return {"role": "assistant", "content": item.content}
        name = item.nickname or "用户"
        return {"role": "user", "content": f"{name}：{item.content}"}

    @staticmethod
    def _approx_chars(messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                total += sum(len(str(part)) for part in content)
        return total

    async def summarize_if_needed(self, session: SessionState) -> None:
        count = await self.db.count_messages(session.id)
        if count <= self.settings.summary_trigger:
            return
        lock = self._summary_locks.setdefault(session.id, asyncio.Lock())
        if lock.locked():
            return

        async with lock:
            count = await self.db.count_messages(session.id)
            if count <= self.settings.summary_trigger:
                return
            drop_count = max(1, count - self.settings.summary_keep)
            old_messages = await self.db.get_oldest_messages(session.id, drop_count)
            if not old_messages:
                return
            transcript = "\n".join(
                f"{'助手' if item.role == 'assistant' else (item.nickname or '用户')}：{item.content}"
                for item in old_messages
            )
            previous = session.summary.strip()
            payload = f"已有摘要：\n{previous}\n\n新增对话：\n{transcript}" if previous else transcript
            try:
                summary = await self.llm.summarize(payload)
            except Exception:
                logger.exception("Failed to summarize session %s", session.id)
                return
            await self.db.set_summary(session.id, summary[:2000])
            await self.db.delete_messages_through(session.id, old_messages[-1].id)
            logger.info("Summarized session %s, dropped %s old messages", session.id, len(old_messages))

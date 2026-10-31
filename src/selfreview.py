from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .brain import BrainService
from .budget import TokenBudget
from .db import Database
from .llm import DeepSeekClient
from .persona import Persona
from .settings import Settings
from .style import normalize_profile

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))



_STYLE_FIELDS = ("summary", "length", "rhythm", "tone", "punctuation", "emoji")
_STYLE_LISTS = ("phrases", "moves", "avoid")


class SelfReviewError(RuntimeError):
    pass


class SelfReviewService:
    """自我批评循环：定期审视自己的回复 + 反馈 + AI 味统计，修正风格画像。

    - 画像 patch 只允许改 style 字段，身份/性格/安全边界不可触碰；
    - confidence 低于阈值只写诊断、不合并；
    - 每次演进写 style_evolution，brain_note 自动沉淀进 data/brain/。
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: DeepSeekClient,
        persona: Persona,
        brain: BrainService,
        budget: TokenBudget | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.persona = persona
        self.brain = brain
        self.budget = budget
        self.error_handler: Callable[[str, str], None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._reply_counters: dict[tuple[str, str], int] = {}
        self._last_review: dict[tuple[str, str], float] = {}
        self._neg_since_review: dict[tuple[str, str], int] = {}

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _record_error(self, source: str, message: str) -> None:
        if self.error_handler is not None:
            try:
                self.error_handler(source, message)
            except Exception:
                logger.exception("Selfreview error handler failed")

    def _lock_for(self, scope_type: str, scope_id: str) -> asyncio.Lock:
        return self._locks.setdefault((scope_type, scope_id), asyncio.Lock())

    # ------------------------------------------------------------------
    def note_reply(self, *, scope_type: str, scope_id: str) -> None:
        """每条机器人回复后调用；攒够条数自动触发一次自我批评。"""
        if not self.settings.selfreview_enabled:
            return
        key = (scope_type, scope_id)
        self._reply_counters[key] = self._reply_counters.get(key, 0) + 1
        if self._reply_counters[key] < max(1, int(self.settings.selfreview_interval_messages)):
            return
        self._reply_counters[key] = 0
        self.schedule(scope_type, scope_id, trigger="auto_review")

    def note_explicit_neg(self, *, scope_type: str, scope_id: str) -> None:
        key = (scope_type, scope_id)
        self._neg_since_review[key] = self._neg_since_review.get(key, 0) + 1
        if self._neg_since_review[key] >= max(1, int(self.settings.selfreview_neg_trigger)):
            self._neg_since_review[key] = 0
            self.schedule(scope_type, scope_id, trigger="explicit_feedback")

    def schedule(self, scope_type: str, scope_id: str, *, trigger: str) -> None:
        task = asyncio.create_task(self._safe_review(scope_type, scope_id, trigger, force=False))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_review(self, scope_type: str, scope_id: str, trigger: str, *, force: bool) -> None:
        try:
            await self.review(scope_type, scope_id, trigger=trigger, force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Self review failed for %s:%s", scope_type, scope_id)
            self._record_error("selfreview", f"{scope_type}:{scope_id} 自我批评失败：{exc}")

    # ------------------------------------------------------------------
    def _since(self, days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")

    async def _gather_exchanges(self, scope_type: str, scope_id: str) -> tuple[str, int]:
        from .safety import clean_style_sample

        session = await self.db.get_or_create_session(scope_type, scope_id)
        messages = await self.db.get_messages(session.id, limit=24)
        lines: list[str] = []
        used = 0
        for message in reversed(messages):
            sample = clean_style_sample(message.content, max_chars=160)
            if sample is None:
                continue
            role = "我" if message.role == "assistant" else "对方"
            lines.append(f"{role}：{sample}")
            if message.role == "assistant":
                used += 1
            if used >= 10:
                break
        return "\n".join(reversed(lines)), used

    async def review(
        self,
        scope_type: str,
        scope_id: str,
        *,
        trigger: str = "manual",
        force: bool = False,
    ) -> dict[str, Any]:
        if not self.settings.selfreview_enabled and not force:
            raise SelfReviewError("自我批评功能已关闭")
        key = (scope_type, scope_id)
        if not force:
            last = self._last_review.get(key)
            gap = max(1, int(self.settings.selfreview_min_gap_minutes)) * 60
            if last is not None and time.monotonic() - last < gap:
                raise SelfReviewError("离上次自我批评还太近，先让它消化消化")
        lock = self._lock_for(scope_type, scope_id)
        async with lock:
            return await self._review(scope_type, scope_id, trigger)

    async def _review(self, scope_type: str, scope_id: str, trigger: str) -> dict[str, Any]:
        if self.budget is not None:
            try:
                budget = await self.budget.status()
                if budget.exhausted:
                    raise SelfReviewError("今日 token 预算已用完，跳过自我批评")
            except SelfReviewError:
                raise
            except Exception:
                logger.exception("Failed to check token budget for selfreview")

        since = self._since(7)
        feedback_stats = await self.db.feedback_stats(
            scope_type=scope_type, scope_id=scope_id, since=since
        )
        deai_stats = await self.db.deai_stats(since=since)
        exchanges_text, assistant_count = await self._gather_exchanges(scope_type, scope_id)
        if assistant_count == 0:
            raise SelfReviewError("最近的会话里没有可以复盘的回复")

        profile_row = await self.db.get_style_profile(scope_type, scope_id)
        profile = profile_row.profile if profile_row else {}
        raw = await self.llm.self_review(
            self.persona,
            profile,
            exchanges_text,
            feedback_stats,
            deai_stats,
        )
        diagnosis = str(raw.get("diagnosis") or "").strip()[:300]
        brain_note = str(raw.get("brain_note") or "").strip()[:120]
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}

        self._last_review[(scope_type, scope_id)] = time.monotonic()

        result: dict[str, Any] = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "trigger": trigger,
            "diagnosis": diagnosis,
            "brain_note": brain_note,
            "confidence": round(confidence, 2),
            "applied": False,
            "fields_changed": [],
            "version_before": int(profile_row.version) if profile_row else 0,
            "version_after": int(profile_row.version) if profile_row else 0,
            "feedback_stats": feedback_stats,
            "deai_stats": deai_stats,
        }

        # 回填上一次演化的 after 指标
        previous = await self.db.latest_style_evolution(scope_type=scope_type, scope_id=scope_id)
        if previous is not None and previous["to_version"] > 0:
            try:
                await self.db.fill_style_evolution_after(
                    previous["id"],
                    feedback_after=float(feedback_stats.get("avg_score", 0.0)),
                    deai_after=float(deai_stats.get("avg_score", 0.0)),
                )
            except Exception:
                logger.exception("Failed to backfill evolution stats")

        if not diagnosis:
            diagnosis = "（模型没有给出诊断）"
        if profile_row is None or not patch:
            # 没有画像或没有 patch：只写诊断，不合并
            await self.db.add_style_evolution(
                scope_type=scope_type,
                scope_id=scope_id,
                from_version=int(profile_row.version) if profile_row else 0,
                to_version=int(profile_row.version) if profile_row else 0,
                trigger=trigger,
                fields_changed=[],
                diagnosis=diagnosis,
                feedback_before=float(feedback_stats.get("avg_score", 0.0)),
                feedback_after=float(feedback_stats.get("avg_score", 0.0)),
                deai_before=float(deai_stats.get("avg_score", 0.0)),
                deai_after=float(deai_stats.get("avg_score", 0.0)),
            )
            if profile_row is None:
                result["note"] = "这个范围还没有风格画像，先让风格学习攒样本"
            await self._sediment(scope_type, scope_id, brain_note, result)
            return result

        if confidence < float(self.settings.style_min_confidence):
            result["note"] = f"置信度 {confidence:.2f} 低于阈值，只记录不应用"
            await self.db.add_style_evolution(
                scope_type=scope_type,
                scope_id=scope_id,
                from_version=int(profile_row.version),
                to_version=int(profile_row.version),
                trigger=trigger,
                fields_changed=[],
                diagnosis=diagnosis,
                feedback_before=float(feedback_stats.get("avg_score", 0.0)),
                feedback_after=float(feedback_stats.get("avg_score", 0.0)),
                deai_before=float(deai_stats.get("avg_score", 0.0)),
                deai_after=float(deai_stats.get("avg_score", 0.0)),
            )
            await self._sediment(scope_type, scope_id, brain_note, result)
            return result

        merged = normalize_profile(patch, old=profile_row.profile)
        if not merged:
            result["note"] = "patch 没有产生有效字段，跳过应用"
            return result
        fields_changed = sorted(
            key
            for key in (*_STYLE_FIELDS, *_STYLE_LISTS)
            if key in merged and merged.get(key) != profile_row.profile.get(key)
        )
        if not fields_changed:
            result["note"] = "patch 没有实际改动"
            return result

        await self.db.add_style_version(
            profile_row.id,
            profile_row.version,
            profile_row.profile,
            profile_row.sample_count,
            profile_row.confidence,
        )
        await self.db.prune_style_versions(
            profile_row.id,
            max(0, int(self.settings.style_retention_versions)),
        )
        new_version = profile_row.version + 1
        merged_confidence = float(merged.get("confidence", profile_row.confidence))
        await self.db.upsert_style_profile(
            scope_type,
            scope_id,
            profile=merged,
            sample_count=profile_row.sample_count,
            confidence=merged_confidence,
            enabled=profile_row.enabled,
            version=new_version,
        )
        await self.db.add_style_evolution(
            scope_type=scope_type,
            scope_id=scope_id,
            from_version=int(profile_row.version),
            to_version=new_version,
            trigger=trigger,
            fields_changed=fields_changed,
            diagnosis=diagnosis,
            feedback_before=float(feedback_stats.get("avg_score", 0.0)),
            feedback_after=float(feedback_stats.get("avg_score", 0.0)),
            deai_before=float(deai_stats.get("avg_score", 0.0)),
            deai_after=float(deai_stats.get("avg_score", 0.0)),
        )
        result["applied"] = True
        result["fields_changed"] = fields_changed
        result["version_after"] = new_version
        self._neg_since_review[(scope_type, scope_id)] = 0
        await self._sediment(scope_type, scope_id, brain_note, result)
        return result

    async def _sediment(
        self,
        scope_type: str,
        scope_id: str,
        brain_note: str,
        result: dict[str, Any],
    ) -> None:
        """把一次自我批评沉淀成大脑的风格进化笔记。"""
        if not self.settings.style_evolution_brain:
            return
        content_lines = [
            f"范围：{scope_type}:{scope_id}",
            f"触发：{result.get('trigger', 'manual')}",
            f"诊断：{result.get('diagnosis', '')}",
        ]
        if result.get("fields_changed"):
            content_lines.append(f"改动字段：{'、'.join(result['fields_changed'])}")
        if result.get("applied"):
            content_lines.append(f"画像版本：{result.get('version_before')} → {result.get('version_after')}")
        if brain_note:
            content_lines.append(f"经验：{brain_note}")
        content = "\n".join(line for line in content_lines if line.split("：", 1)[-1])
        if not content.strip():
            return
        stamp = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M")
        title = f"风格进化 {stamp}"
        try:
            note = await self.brain.create_note(
                title=title,
                content=content,
                tags="风格进化,style",
                scope_type="global",
                visibility="global",
                source="auto",
                for_auto=True,
            )
            result["brain_note_id"] = note.id
        except Exception as exc:
            self._record_error("selfreview", f"风格进化笔记写入失败：{exc}")

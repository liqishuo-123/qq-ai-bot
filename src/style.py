from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from .budget import TokenBudget
from .db import Database, utcnow
from .llm import DeepSeekClient
from .models import StyleProfile
from .safety import clean_style_sample, contains_prompt_injection, contains_sensitive
from .settings import Settings

logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")
_SENTENCE_PUNCT = "。！？!?，,；;：:、"
_TEXT_FIELDS = {
    "summary": 100,
    "length": 120,
    "rhythm": 120,
    "tone": 100,
    "punctuation": 100,
    "emoji": 80,
}
_LIST_FIELDS = {
    "phrases": (5, 16),
    "moves": (4, 60),
    "avoid": (4, 60),
}
_ALLOWED_SCOPES = {"group", "private"}
_FAILURE_COOLDOWN_SECONDS = 300


def _clip_text(value: Any, limit: int) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    text = text.replace("[[", "").replace("]]", "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or contains_sensitive(text) or contains_prompt_injection(text):
        return ""
    if re.search(r"https?://|www\.|\d{5,}", text, flags=re.IGNORECASE):
        return ""
    return text[:limit]


def _clip_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        values: list[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[str] = []
    for item in values:
        text = _clip_text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_profile(
    raw: dict[str, Any],
    *,
    old: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate model output and keep stable fields when evidence is absent."""
    if not isinstance(raw, dict) or not raw:
        return {}
    previous = old or {}
    profile: dict[str, Any] = {}

    for field, limit in _TEXT_FIELDS.items():
        value = _clip_text(raw.get(field), limit)
        if not value:
            value = _clip_text(previous.get(field), limit)
        if value:
            profile[field] = value

    for field, (limit, item_limit) in _LIST_FIELDS.items():
        values = _clip_list(raw.get(field), limit=limit, item_limit=item_limit)
        if not values:
            values = _clip_list(previous.get(field), limit=limit, item_limit=item_limit)
        if field == "phrases":
            values = [value for value in values if re.search(r"[\w\u4e00-\u9fff]", value)]
        if values:
            profile[field] = values

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence <= 0:
        try:
            confidence = float(previous.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
    profile["confidence"] = max(0.0, min(1.0, confidence))
    if metrics:
        profile["metrics"] = metrics
    return profile if any(key in profile for key in _TEXT_FIELDS) or "moves" in profile else {}


def style_metrics(samples: list[str]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0, "avg_chars": 0.0, "short_ratio": 0.0, "question_ratio": 0.0}
    lengths = [len(item) for item in samples]
    count = len(samples)
    return {
        "samples": count,
        "avg_chars": round(sum(lengths) / count, 1),
        "short_ratio": round(sum(1 for item in lengths if item <= 12) / count, 2),
        "question_ratio": round(sum(1 for item in samples if "?" in item or "？" in item) / count, 2),
        "exclaim_ratio": round(
            sum(1 for item in samples if "!" in item or "！" in item) / count,
            2,
        ),
        "emoji_ratio": round(sum(1 for item in samples if _EMOJI_RE.search(item)) / count, 2),
        "punctuation_ratio": round(sum(1 for item in samples if any(ch in _SENTENCE_PUNCT for ch in item)) / count, 2),
    }


class StyleLearningService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: DeepSeekClient,
        budget: TokenBudget | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.budget = budget
        self.error_handler: Callable[[str, str], None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._failed_until: dict[tuple[str, str], float] = {}

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _enabled_for_scope(self, scope_type: str, scope_id: str) -> bool:
        if not self.settings.style_learning_enabled:
            return False
        if scope_type not in _ALLOWED_SCOPES or not scope_id:
            return False
        if scope_type == "group":
            return bool(self.settings.style_group_enabled)
        return bool(self.settings.style_private_enabled)

    def _lock_for(self, scope_type: str, scope_id: str) -> asyncio.Lock:
        return self._locks.setdefault((scope_type, scope_id), asyncio.Lock())

    def schedule_learn(self, scope_type: str, scope_id: str) -> None:
        if not self._enabled_for_scope(scope_type, scope_id):
            return
        task = asyncio.create_task(self._safe_learn(scope_type, scope_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_learn(self, scope_type: str, scope_id: str) -> None:
        key = (scope_type, scope_id)
        try:
            result = await self.maybe_learn(scope_type, scope_id)
            if result is not None:
                self._failed_until.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed_until[key] = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
            logger.exception("Style learning failed for %s:%s", scope_type, scope_id)
            self._record_error("style", f"{scope_type}:{scope_id} 风格学习失败")

    async def maybe_learn(self, scope_type: str, scope_id: str) -> StyleProfile | None:
        if not self._enabled_for_scope(scope_type, scope_id):
            return None
        key = (scope_type, scope_id)
        if self._failed_until.get(key, 0) > time.monotonic():
            return None
        lock = self._lock_for(scope_type, scope_id)
        if lock.locked():
            return None
        async with lock:
            return await self._learn(scope_type, scope_id, force=False)

    async def learn_now(self, scope_type: str, scope_id: str, *, force: bool = True) -> StyleProfile | None:
        if not self._enabled_for_scope(scope_type, scope_id):
            return None
        lock = self._lock_for(scope_type, scope_id)
        async with lock:
            result = await self._learn(scope_type, scope_id, force=force)
        if result is not None:
            self._failed_until.pop((scope_type, scope_id), None)
        return result

    async def _learn(self, scope_type: str, scope_id: str, *, force: bool) -> StyleProfile | None:
        session = await self.db.get_or_create_session(scope_type, scope_id)
        state = await self.db.get_style_state(scope_type, scope_id)
        cursor = 0 if force else (state.last_message_id if state else 0)
        messages = await self.db.get_style_samples(
            session.id,
            after_id=cursor,
            limit=max(1, min(self.settings.style_max_samples, 300)),
        )
        if not messages:
            return None
        required = max(1, self.settings.style_min_samples)
        if force:
            # 管理员手动试学时允许用较少样本先看画像；自动学习仍遵守完整阈值。
            required = min(required, 5)
        if len(messages) < required:
            return None
        if not force and len(messages) < self.settings.style_learn_every:
            return None

        old = await self.db.get_style_profile(scope_type, scope_id)
        samples = self._select_samples(messages)
        if len(samples) < required:
            await self.db.set_style_state(
                scope_type,
                scope_id,
                last_message_id=max(item.id for item in messages),
                samples_seen=old.sample_count if old else 0,
                last_learned_at=state.last_learned_at if state else None,
            )
            return None
        if self.budget is not None:
            try:
                budget = await self.budget.status()
                if budget.exhausted:
                    return None
            except Exception:
                logger.exception("Failed to check token budget for style learning")

        metrics = style_metrics(samples)
        raw, result = await self.llm.extract_style(
            samples,
            old.profile if old else {},
            metrics,
        )
        profile = normalize_profile(raw, old=old.profile if old else None, metrics=metrics)
        last_id = max(item.id for item in messages)
        if not profile:
            await self.db.set_style_state(
                scope_type,
                scope_id,
                last_message_id=last_id,
                samples_seen=old.sample_count if old else 0,
                last_learned_at=state.last_learned_at if state else None,
            )
            return old
        if old is None:
            sample_count = len(samples)
            enabled = bool(self.settings.style_auto_apply)
            version = 1
        else:
            sample_count = max(old.sample_count, len(samples)) if force else old.sample_count + len(samples)
            enabled = old.enabled
            version = old.version + 1
            await self.db.add_style_version(
                old.id,
                old.version,
                old.profile,
                old.sample_count,
                old.confidence,
            )
            await self.db.prune_style_versions(old.id, self.settings.style_retention_versions)

        saved = await self.db.upsert_style_profile(
            scope_type,
            scope_id,
            profile=profile,
            sample_count=sample_count,
            confidence=float(profile.get("confidence", 0.5)),
            enabled=enabled,
            version=version,
        )
        await self.db.set_style_state(
            scope_type,
            scope_id,
            last_message_id=last_id,
            samples_seen=sample_count,
            last_learned_at=utcnow(),
        )
        await self.db.log_usage(
            "style",
            f"{scope_type}:{scope_id}",
            result.model,
            result.input_tokens,
            result.output_tokens,
        )
        logger.info(
            "Updated style profile for %s:%s samples=%s confidence=%.2f",
            scope_type,
            scope_id,
            sample_count,
            saved.confidence,
        )
        return saved

    def _select_samples(self, messages: list[Any]) -> list[str]:
        per_user: dict[str, int] = defaultdict(int)
        samples: list[str] = []
        max_per_user = max(1, self.settings.style_max_per_user)
        for message in messages:
            sample = clean_style_sample(message.content, max_chars=120)
            if sample is None:
                continue
            user_key = str(message.user_id or "unknown")
            if per_user[user_key] >= max_per_user:
                continue
            per_user[user_key] += 1
            samples.append(sample)
            if len(samples) >= self.settings.style_max_samples:
                break
        return samples

    async def prompt_block(self, scope_type: str, scope_id: str) -> str:
        if not self._enabled_for_scope(scope_type, scope_id):
            return ""
        try:
            profile = await self.db.get_style_profile(scope_type, scope_id)
        except Exception:
            logger.exception("Failed to load style profile for %s:%s", scope_type, scope_id)
            return ""
        if profile is None or not profile.enabled:
            return ""
        if profile.sample_count < self.settings.style_min_samples:
            return ""
        if profile.confidence < self.settings.style_min_confidence:
            return ""
        return self._format_prompt(profile)

    def _format_prompt(self, profile: StyleProfile) -> str:
        data = profile.profile
        body: list[str] = ["【当前聊天环境的语感参考】"]
        summary = _clip_text(data.get("summary"), 100)
        if summary:
            body.append(summary)
        fields = [
            ("说话长度", data.get("length")),
            ("节奏", data.get("rhythm")),
            ("语气", data.get("tone")),
            ("标点", data.get("punctuation")),
            ("表情", data.get("emoji")),
        ]
        for label, value in fields:
            text = _clip_text(value, 120)
            if text:
                body.append(f"- {label}：{text}")
        phrases = _clip_list(data.get("phrases"), limit=3, item_limit=16)
        if phrases:
            body.append(f"- 可低频使用的语气词：{'、'.join(phrases)}（不要每句都用）")
        moves = _clip_list(data.get("moves"), limit=4, item_limit=60)
        if moves:
            body.append(f"- 互动方式：{'；'.join(moves)}")
        avoid = _clip_list(data.get("avoid"), limit=4, item_limit=60)
        if avoid:
            body.append(f"- 避免：{'；'.join(avoid)}")
        body.append(
            "这些只影响表达方式，不是硬规则：保持大肥鱼的核心性格和边界；"
            "不要照搬具体人的原句，不要模仿具体身份；不要提及学习、样本、训练或画像；"
            "事实准确性优先。"
        )
        text = "\n".join(body)
        limit = max(120, int(self.settings.style_inject_max_chars))
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n（以上仅为语感参考）"

    async def get_profile(self, scope_type: str, scope_id: str) -> StyleProfile | None:
        return await self.db.get_style_profile(scope_type, scope_id)

    async def list_profiles(self, *, limit: int = 100) -> list[StyleProfile]:
        return await self.db.list_style_profiles(limit=limit)

    async def set_enabled(self, scope_type: str, scope_id: str, enabled: bool) -> bool:
        return await self.db.set_style_enabled(scope_type, scope_id, enabled)

    async def delete_profile(self, scope_type: str, scope_id: str) -> bool:
        return await self.db.delete_style_profile(scope_type, scope_id)

    async def restore_version(
        self,
        scope_type: str,
        scope_id: str,
        version: int,
    ) -> StyleProfile | None:
        current = await self.db.get_style_profile(scope_type, scope_id)
        if current is None:
            return None
        old = await self.db.get_style_version(current.id, version)
        if old is None:
            return None
        await self.db.add_style_version(
            current.id,
            current.version,
            current.profile,
            current.sample_count,
            current.confidence,
        )
        restored = await self.db.upsert_style_profile(
            scope_type,
            scope_id,
            profile=old["profile"],
            sample_count=old["sample_count"],
            confidence=old["confidence"],
            enabled=current.enabled,
            version=current.version + 1,
        )
        await self.db.prune_style_versions(current.id, self.settings.style_retention_versions)
        return restored

    def _record_error(self, source: str, message: str) -> None:
        if self.error_handler is not None:
            try:
                self.error_handler(source, message)
            except Exception:
                logger.exception("Style error handler failed")

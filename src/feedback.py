from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .db import Database
from .settings import Settings

logger = logging.getLogger(__name__)

# 显式评价词：命中且带指向性（含“你/它”或引用机器人）才算强信号。
_NEG_RE = re.compile(r"(像机器人|机器人吧|机器人说的|ai味|AI味|好官方|太官方|说人话|你是机器人|你是AI|你是ai|好机械|太机械)")
_POS_RE = re.compile(r"(挺有意思|有点意思|真逗|像真人|挺自然|会说话|学到了|有内味了|活人感)")

_NEG_LABELS = {
    "像机器人": "像机器人",
    "机器人吧": "像机器人",
    "机器人说的": "像机器人",
    "ai味": "AI 味",
    "AI味": "AI 味",
    "好官方": "太官方",
    "太官方": "太官方",
    "说人话": "不接地气",
    "你是机器人": "像机器人",
    "你是AI": "像机器人",
    "你是ai": "像机器人",
    "好机械": "太机械",
    "太机械": "太机械",
}


@dataclass(slots=True)
class PendingReply:
    scope_type: str
    scope_id: str
    user_id: str
    persona_version: int
    deai_score: int
    started: float
    msg_count: int = 0
    replied: bool = False
    explicit_neg: bool = False
    neg_label: str = ""
    explicit_pos: bool = False
    sticker_reply: bool = False
    command_cancel: bool = False
    last_message_at: float = 0.0
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass(slots=True)
class CorrectionEvent:
    label: str
    created_at: float


class FeedbackService:
    """回复效果反馈：只记录行为信号，不保存对方消息原文。

    register() 在每次机器人回复后调用，开一个观察窗；
    observe() 由聊天处理器对每条用户消息调用，更新匹配中的观察窗；
    窗口结束或被 command_cancel 关闭时合成一次分数落库。
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.on_explicit_neg: Callable[[str, str], None] | None = None
        self._pending: dict[tuple[str, str, str], PendingReply] = {}
        self._corrections: dict[tuple[str, str], CorrectionEvent] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.feedback_enabled)

    def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._pending.clear()

    # ------------------------------------------------------------------
    def _window(self) -> float:
        return float(self.settings.feedback_observe_seconds)

    def _private_on(self) -> bool:
        return bool(self.settings.feedback_private_enabled)

    def register(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        persona_version: int = 0,
        deai_score: int = 0,
    ) -> None:
        if not self.enabled:
            return
        if scope_type == "private" and not self._private_on():
            return
        key = (scope_type, scope_id, user_id)
        window = self._window()
        pending = PendingReply(
            scope_type=scope_type,
            scope_id=scope_id,
            user_id=user_id,
            persona_version=persona_version,
            deai_score=deai_score,
            started=time.monotonic(),
            last_message_at=time.monotonic(),
        )
        old = self._pending.get(key)
        if old is not None and old.task is not None:
            old.task.cancel()
        pending.task = asyncio.create_task(self._finalize_later(key, window))
        self._tasks.add(pending.task)
        pending.task.add_done_callback(self._tasks.discard)
        self._pending[key] = pending

    async def _finalize_later(self, key: tuple[str, str, str], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        pending = self._pending.pop(key, None)
        if pending is not None:
            try:
                await self._commit(pending)
            except Exception:
                logger.exception("Failed to commit reply feedback")

    async def _commit(self, pending: PendingReply) -> None:
        silent = pending.msg_count == 0
        score = (
            20 * int(pending.msg_count >= 2)
            + 12 * int(pending.replied)
            + 8 * int(pending.sticker_reply)
            + 15 * int(pending.explicit_pos)
            - 25 * int(pending.explicit_neg)
            - 10 * int(pending.command_cancel)
            - 3 * int(silent and pending.scope_type == "group")
        )
        score = int(max(-100, min(100, score)))
        signals = {
            "replied": pending.replied,
            "followup": pending.msg_count >= 2,
            "ignored": pending.msg_count >= 1 and not pending.replied and not pending.explicit_neg,
            "left_silent": silent,
            "explicit_neg": pending.explicit_neg,
            "explicit_pos": pending.explicit_pos,
            "sticker_reply": pending.sticker_reply,
            "command_cancel": pending.command_cancel,
            "msg_count": pending.msg_count,
        }
        await self.db.add_reply_feedback(
            scope_type=pending.scope_type,
            scope_id=pending.scope_id,
            persona_version=pending.persona_version,
            deai_score=pending.deai_score,
            signals=signals,
            score=score,
        )
        if pending.explicit_neg:
            self._corrections[(pending.scope_type, pending.scope_id)] = CorrectionEvent(
                label=pending.neg_label or "有 AI 味",
                created_at=time.monotonic(),
            )
            if self.on_explicit_neg is not None:
                try:
                    self.on_explicit_neg(pending.scope_type, pending.scope_id)
                except Exception:
                    logger.exception("Explicit-neg handler failed")

    # ------------------------------------------------------------------
    def observe(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        text: str,
        directed_at_bot: bool,
        sticker: bool = False,
    ) -> None:
        """Feed an incoming user message into any open observation window."""
        if not self.enabled:
            return
        pending = self._pending.get((scope_type, scope_id, user_id))
        if pending is None:
            return
        if time.monotonic() - pending.started > self._window():
            return
        pending.msg_count += 1
        pending.last_message_at = time.monotonic()
        if directed_at_bot:
            pending.replied = True
        if sticker:
            pending.sticker_reply = True
        value = (text or "").strip()
        if value:
            if pending.explicit_neg:
                pass
            elif _NEG_RE.search(value):
                pending.explicit_neg = True
                matched = _NEG_RE.search(value)
                pending.neg_label = _NEG_LABELS.get(str(matched.group(0)) if matched else "", "有 AI 味")
                self._corrections[(scope_type, scope_id)] = CorrectionEvent(
                    label=pending.neg_label,
                    created_at=time.monotonic(),
                )
                if self.on_explicit_neg is not None:
                    try:
                        self.on_explicit_neg(scope_type, scope_id)
                    except Exception:
                        logger.exception("Explicit-neg handler failed")
                # 强负向反馈：提前结束观察窗，尽快落库
                if pending.task is not None:
                    pending.task.cancel()
                task = asyncio.create_task(self._finalize_now(pending))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif _POS_RE.search(value):
                pending.explicit_pos = True

    async def _finalize_now(self, pending: PendingReply) -> None:
        await asyncio.sleep(0)
        self._pending.pop((pending.scope_type, pending.scope_id, pending.user_id), None)
        try:
            await self._commit(pending)
        except Exception:
            logger.exception("Failed to commit reply feedback")

    def note_command_cancel(self, *, scope_type: str, scope_id: str, user_id: str) -> None:
        """/关闭 触发的强负向信号：立即结束该用户的观察窗。"""
        pending = self._pending.get((scope_type, scope_id, user_id))
        if pending is None:
            return
        pending.command_cancel = True
        if pending.task is not None:
            pending.task.cancel()
        task = asyncio.create_task(self._finalize_now(pending))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    async def correction_hint(self, scope_type: str, scope_id: str) -> str:
        """即时修正指令：最近的显式负反馈在 10 分钟内有效。"""
        event = self._corrections.get((scope_type, scope_id))
        if event is None:
            return ""
        if time.monotonic() - event.created_at > 600:
            self._corrections.pop((scope_type, scope_id), None)
            return ""
        return (
            f"对方刚才觉得你说话{event.label}。这一条回复更口语、更像随手指的字，"
            "别用书面腔，短一点。"
        )

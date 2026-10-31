from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from nonebot import get_bots, get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg

from ..account import AccountError, AccountService
from ..actions import ActionService
from ..admin import register_admin
from ..autonomy import AutonomyService
from ..brain import BrainError, BrainService
from ..budget import BudgetExceeded, TokenBudget
from ..composer import sanitize_text, split_reply, typing_delay
from ..context import ContextBuilder
from ..db import Database
from ..deai import DeAiService
from ..explore import ExplorationService, ExploreError
from ..feedback import FeedbackService
from ..group_admin import (
    GroupActionError,
    GroupAdminService,
    parse_duration,
    target_from_args,
    target_from_event,
)
from ..guild import GuildError, GuildService
from ..image_utils import download_image_as_data_url, extract_image_url
from ..llm import DeepSeekClient, LLMError
from ..memory import MemoryService
from ..models import BrainNote, StickerRecord
from ..persona import Persona, load_persona
from ..plugin_loader import PluginRegistry
from ..policy import PolicyViolation, check_chat_text
from ..presence import PresenceError, PresenceService
from ..qzone import QzoneError, QzoneService
from ..selfreview import SelfReviewService
from ..settings import Settings, get_settings
from ..skills import ROLE_RANK, SkillDenied, SkillService
from ..stickers import StickerService
from ..streaming import StreamSender
from ..style import StyleLearningService
from ..trigger import TriggerContext, is_at_bot, is_reply_to_bot, mentions_name

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))

driver = get_driver()


@dataclass(slots=True)
class ReplyPayload:
    text: str
    sticker: StickerRecord | None = None
    deai_meta: dict[str, Any] | None = None


class Runtime:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        persona: Persona,
        llm: DeepSeekClient,
        context: ContextBuilder,
        memory: MemoryService,
        plugins: PluginRegistry,
        budget: TokenBudget,
        stickers: StickerService,
        style: StyleLearningService,
        skills: SkillService,
        group_admin: GroupAdminService,
        brain: BrainService,
        presence: PresenceService,
        account: AccountService,
        autonomy: AutonomyService,
        explore: ExplorationService,
        qzone: QzoneService,
        guild: GuildService,
        deai: DeAiService,
        feedback: FeedbackService,
        selfreview: SelfReviewService,
        actions: ActionService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.persona = persona
        self.llm = llm
        self.context = context
        self.memory = memory
        self.plugins = plugins
        self.budget = budget
        self.stickers = stickers
        self.style = style
        self.skills = skills
        self.group_admin = group_admin
        self.brain = brain
        self.presence = presence
        self.account = account
        self.autonomy = autonomy
        self.explore = explore
        self.qzone = qzone
        self.guild = guild
        self.deai = deai
        self.feedback = feedback
        self.selfreview = selfreview
        self.actions = actions
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_group_reply: dict[str, float] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.errors: deque[dict[str, str]] = deque(maxlen=50)

    def record_error(self, source: str, message: str) -> None:
        self.errors.appendleft(
            {
                "time": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                "source": source,
                "message": str(message)[:500],
            }
        )

    def clear_errors(self, source: str) -> None:
        """连上之后把这类陈旧错误清掉，避免控制台永远挂一条红字。"""
        for item in list(self.errors):
            if item.get("source") == source:
                self.errors.remove(item)

    def lock_for(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


runtime: Runtime | None = None


def _get_runtime() -> Runtime:
    if runtime is None:
        raise RuntimeError("Bot runtime is not initialized")
    return runtime


async def _explore_loop(rt: Runtime) -> None:
    await asyncio.sleep(max(10, int(rt.settings.explore_startup_delay_seconds)))
    next_run = 0.0
    interval = max(60, int(rt.settings.explore_interval_minutes) * 60)
    while True:
        try:
            active = await rt.explore.enabled()
            if not active:
                next_run = 0.0
            elif time.monotonic() >= next_run:
                bots = get_bots()
                bot = next(iter(bots.values()), None) if bots else None
                if bot is not None:
                    await rt.explore.run(bot)
                    next_run = time.monotonic() + interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rt.record_error("explore", str(exc))
        await asyncio.sleep(60)


@driver.on_startup
async def _startup() -> None:
    global runtime
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    persona = load_persona(settings.persona_file)
    llm = DeepSeekClient(settings)
    context = ContextBuilder(settings, db, llm, persona)
    brain = BrainService(settings, db)
    brain.ensure_dirs()
    memory = MemoryService(settings, db, llm, brain)
    plugins = PluginRegistry(settings)
    plugins.load_all()
    budget = TokenBudget(settings, db)
    stickers = StickerService(settings, db, llm)
    style = StyleLearningService(settings, db, llm, budget)
    skills = SkillService(settings, db)
    await skills.ensure_defaults()
    group_admin = GroupAdminService(settings.superuser_ids)
    presence = PresenceService(settings, db, brain, persona, llm)
    account = AccountService(settings)
    autonomy = AutonomyService(settings, db, llm, persona, skills)
    explore = ExplorationService(settings, db, autonomy)
    qzone = QzoneService(settings)
    guild = GuildService(settings, db)
    deai = DeAiService(settings, db, llm, budget)
    feedback = FeedbackService(settings, db)
    selfreview = SelfReviewService(settings, db, llm, persona, brain, budget)
    actions = ActionService(settings, skills, qzone, guild)
    runtime = Runtime(
        settings=settings,
        db=db,
        persona=persona,
        llm=llm,
        context=context,
        memory=memory,
        plugins=plugins,
        budget=budget,
        stickers=stickers,
        style=style,
        skills=skills,
        group_admin=group_admin,
        brain=brain,
        presence=presence,
        account=account,
        autonomy=autonomy,
        explore=explore,
        qzone=qzone,
        guild=guild,
        deai=deai,
        feedback=feedback,
        selfreview=selfreview,
        actions=actions,
    )
    style.error_handler = runtime.record_error
    presence.error_handler = runtime.record_error
    autonomy.error_handler = runtime.record_error
    selfreview.error_handler = runtime.record_error
    feedback.on_explicit_neg = runtime.selfreview.note_explicit_neg
    await presence.start()
    if not settings.llm_ready:
        logger.warning("模型服务没配全（缺 LLM_BASE_URL 或 LLM_MODEL），聊天会失败。双击「配置向导.bat」选一家。")
    elif settings.llm_needs_key and not settings.llm_api_key:
        logger.warning("LLM_API_KEY 没填，聊天会失败。双击「配置向导.bat」补上。")
    if not settings.superuser_ids:
        logger.warning("SUPERUSERS is empty; /插件 will be inaccessible")
    register_admin(runtime)
    runtime.spawn(_explore_loop(runtime))
    logger.info(
        "QQ AI Bot ready. persona=%s provider=%s model=%s",
        persona.display_name,
        settings.llm_provider_name,
        settings.llm_model,
    )
    ws_urls = getattr(driver.config, "onebot_ws_urls", None) or set()
    logger.info(
        "[..] 正在连接 NapCat：%s",
        "、".join(sorted(str(url) for url in ws_urls)) or "（.env 里没有配 ONEBOT_WS_URLS）",
    )
    logger.info("[..] 10 秒内没看到 [OK] 已连上 NapCat，就是 NapCat 还没启动，或者端口不对。")


@driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    host = str(getattr(driver.config, "host", "127.0.0.1") or "127.0.0.1")
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    port = getattr(driver.config, "port", 8080)
    logger.info("")
    logger.info("[OK] 已连上 NapCat，机器人 QQ=%s —— 成功了，去群里 @ 它试试", bot.self_id)
    logger.info("[OK] 控制台地址：http://%s:%s/admin", host, port)
    logger.info("")
    if runtime is not None:
        runtime.clear_errors("onebot")


@driver.on_bot_disconnect
async def _on_bot_disconnect(bot: Bot) -> None:
    logger.warning(
        "[!] 和 NapCat 断开了（QQ=%s）。程序每 3 秒自动重连；一直不恢复说明 NapCat 或 QQ 已经退出了。",
        bot.self_id,
    )
    if runtime is not None:
        runtime.record_error("onebot", f"机器人 {bot.self_id} 断开连接")


@driver.on_shutdown
async def _shutdown() -> None:
    global runtime
    if runtime is None:
        return
    tasks = list(runtime.tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await runtime.presence.close()
    await runtime.qzone.close()
    await runtime.guild.close()
    await runtime.memory.close()
    await runtime.style.close()
    await runtime.feedback.close()
    await runtime.selfreview.close()
    await runtime.llm.close()
    await runtime.db.close()
    runtime = None


def _uid(event: MessageEvent) -> str:
    return str(event.get_user_id())


def _nickname(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    return (
        getattr(sender, "card", None)
        or getattr(sender, "nickname", None)
        or _uid(event)
    )


def _is_command_text(rt: Runtime, text: str) -> bool:
    starts = tuple(item for item in rt.settings.command_start if item)
    return bool(starts) and text.lstrip().startswith(starts)


def _is_superuser(rt: Runtime, event: MessageEvent) -> bool:
    return _uid(event) in rt.settings.superuser_ids


def _is_group_admin(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent) and getattr(event.sender, "role", None) in {"admin", "owner"}


async def _can_manage_group(rt: Runtime, event: GroupMessageEvent) -> bool:
    if _is_superuser(rt, event) or _is_group_admin(event):
        return True
    group = await rt.db.ensure_group(str(event.group_id))
    return group.activated_by == _uid(event)


def _event_scope(event: MessageEvent) -> tuple[str, str]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", _uid(event)


async def _send_bubbles(
    rt: Runtime,
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: StickerRecord | None = None,
    typing: bool = True,
    max_bubbles: int | None = None,
) -> None:
    bubbles = split_reply(
        text,
        max_bubbles=max_bubbles or rt.settings.reply_max_bubbles,
        max_chars=rt.settings.reply_max_chars,
    )
    if not bubbles and sticker is None:
        return
    if typing:
        delay = typing_delay(text, rt.settings.typing_delay_min, rt.settings.typing_delay_max)
        await asyncio.sleep(delay)

    scope_type, scope_id = _event_scope(event)
    for index, bubble in enumerate(bubbles):
        if index:
            jitter = random.uniform(0.7, 1.3)
            await asyncio.sleep(max(0.0, rt.settings.reply_bubble_delay * jitter))
        attach_sticker = sticker is not None and index == len(bubbles) - 1
        message: str | Message = bubble
        if attach_sticker:
            try:
                data_url = await rt.stickers.data_url(sticker)
                segments = []
                if bubble:
                    segments.append(MessageSegment.text(bubble))
                segments.append(MessageSegment.image(data_url))
                message = Message(segments)
            except Exception:
                logger.exception("Failed to read sticker %s; falling back to text", sticker.id)
                attach_sticker = False
        try:
            await bot.send(event, message)
            if attach_sticker:
                try:
                    await rt.stickers.mark_sent(sticker.id, scope_type, scope_id)
                except Exception:
                    logger.exception("Failed to record sticker usage")
        except Exception:
            logger.exception("Failed to send QQ message")
            if attach_sticker and bubble:
                try:
                    await bot.send(event, bubble)
                except Exception:
                    logger.exception("Failed to send QQ text fallback")

    if not bubbles and sticker is not None:
        try:
            data_url = await rt.stickers.data_url(sticker)
            await bot.send(event, Message(MessageSegment.image(data_url)))
            try:
                await rt.stickers.mark_sent(sticker.id, scope_type, scope_id)
            except Exception:
                logger.exception("Failed to record sticker usage")
        except Exception:
            logger.exception("Failed to send sticker-only message")


def _history_text(text: str, image_url: str | None) -> str:
    if image_url:
        return f"{text} [图片]".strip()
    return text or "[图片]"


async def _safe_send(bot: Bot, event: MessageEvent, text: str) -> None:
    try:
        await bot.send(event, text)
    except Exception:
        logger.exception("Failed to send QQ message")


def _make_streamer(rt: Runtime, bot: Bot, event: MessageEvent) -> StreamSender:
    return StreamSender(
        bot_name=rt.persona.display_name,
        max_bubbles=rt.settings.reply_max_bubbles,
        max_chars=rt.settings.reply_max_chars,
        send=lambda text: _safe_send(bot, event, text),
    )





async def _store_user_message(
    rt: Runtime,
    *,
    scope_type: str,
    scope_id: str,
    user_id: str,
    nickname: str,
    text: str,
) -> None:
    session = await rt.db.get_or_create_session(scope_type, scope_id)
    await rt.db.add_message(
        session.id,
        "user",
        text or "[图片]",
        user_id=user_id,
        nickname=nickname,
    )
    rt.style.schedule_learn(scope_type, scope_id)


async def _generate_reply(
    rt: Runtime,
    *,
    scope_type: str,
    scope_id: str,
    user_id: str,
    nickname: str,
    text: str,
    image_url: str | None,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
) -> ReplyPayload:
    await rt.budget.check()
    session = await rt.db.get_or_create_session(scope_type, scope_id)
    user_text = _history_text(text, image_url)
    await rt.db.add_message(
        session.id,
        "user",
        user_text,
        user_id=user_id,
        nickname=nickname,
    )
    rt.style.schedule_learn(scope_type, scope_id)
    style_hint = await rt.style.prompt_block(scope_type, scope_id)
    try:
        correction = await rt.feedback.correction_hint(scope_type, scope_id)
    except Exception:
        logger.exception("Feedback correction lookup failed")
        correction = ""
    if correction:
        style_hint = f"{style_hint}\n{correction}".strip() if style_hint else correction
    try:
        brain_hint = await rt.brain.prompt_block(
            user_text,
            scope_type=scope_type,
            scope_id=scope_id,
        )
    except Exception:
        logger.exception("Brain lookup failed")
        brain_hint = ""
    messages = await rt.context.build(
        scope_type=scope_type,
        scope_id=scope_id,
        user_id=user_id,
        nickname=nickname,
        text=user_text,
        style_hint=style_hint,
        brain_hint=brain_hint,
    )
    explicit_sticker = rt.stickers.is_explicit_request(text)
    if not rt.settings.stream_replies:
        catalog = await rt.stickers.catalog_for(scope_type, scope_id, explicit=explicit_sticker)
        if catalog and messages:
            messages[0]["content"] = f"{messages[0].get('content', '')}{catalog}"
    image_data_url = None
    if image_url and rt.settings.image_enabled:
        image_data_url = await download_image_as_data_url(
            image_url,
            max_bytes=rt.settings.image_max_bytes,
            allow_private_hosts=rt.settings.image_allow_private_hosts,
        )

    try:
        if image_data_url is None and on_delta is not None and rt.settings.stream_replies:
            result = await rt.llm.chat_stream(messages, on_delta=on_delta)
        else:
            result = await rt.llm.chat(messages, image_data_url=image_data_url)
    except LLMError as exc:
        # 某些模型/网关会拒绝特定图片格式；降级为纯文本再试一次。
        if image_data_url is not None and exc.status_code == 400:
            logger.warning("Vision request rejected, retrying without image: %s", exc)
            result = await rt.llm.chat(messages)
        else:
            raise
    reply = sanitize_text(result.text, rt.persona.display_name)
    try:
        reply = check_chat_text(reply)
    except PolicyViolation as exc:
        rt.record_error("policy", f"回复被拦截：{exc}")
        reply = "这个不聊，换一个。"
    reply, sticker_id = rt.stickers.parse_marker(reply)

    # 去 AI 化闸门：检测 → 分级改写（失败/关闭时发原文）
    deai_meta: dict[str, Any] | None = None
    if rt.settings.deai_enabled and reply:
        try:
            reply, deai_meta = await rt.deai.gate(
                reply,
                persona=rt.persona,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        except Exception:
            logger.exception("DeAI gate failed; sending original reply")
            deai_meta = None

    sticker: StickerRecord | None = None
    if sticker_id is not None:
        candidate = await rt.db.get_sticker(sticker_id)
        if candidate is not None and candidate.enabled:
            if await rt.stickers.can_send(scope_type, scope_id, explicit=explicit_sticker):
                sticker = candidate
    if sticker is None and explicit_sticker and not rt.settings.stream_replies:
        if await rt.stickers.can_send(scope_type, scope_id, explicit=True):
            sticker = await rt.stickers.pick_explicit()

    if not reply and sticker is None:
        reply = "嗯？"
    await rt.db.add_message(session.id, "assistant", reply or "[表情]")
    await rt.db.log_usage(scope_type, scope_id, result.model, result.input_tokens, result.output_tokens)
    if scope_type == "group":
        await rt.db.record_bot_reply(scope_id, user_id)
    rt.memory.schedule_extract(
        scope_type=scope_type,
        scope_id=scope_id,
        user_id=user_id,
        session_id=session.id,
    )
    rt.spawn(rt.context.summarize_if_needed(session))
    rt.selfreview.note_reply(scope_type=scope_type, scope_id=scope_id)
    return ReplyPayload(text=reply, sticker=sticker, deai_meta=deai_meta)


async def _handle_private(rt: Runtime, bot: Bot, event: PrivateMessageEvent) -> None:
    user_id = _uid(event)
    if not await rt.db.mark_processed("private", user_id, str(event.message_id)):
        return
    if _uid(event) == str(event.self_id):
        return
    rt.spawn(
        rt.stickers.ingest_event(
            event.get_message(),
            scope_type="private",
            scope_id=user_id,
            user_id=user_id,
            nickname=_nickname(event),
            message_id=str(event.message_id),
        )
    )
    user = await rt.db.ensure_user(user_id, event.sender.nickname)
    if not user.private_active:
        return
    text = event.get_plaintext().strip()
    if _is_command_text(rt, text):
        return
    image_url = extract_image_url(event.get_message())
    if not text and not image_url:
        return

    rt.feedback.observe(
        scope_type="private",
        scope_id=user_id,
        user_id=user_id,
        text=text,
        directed_at_bot=True,
    )

    nickname = _nickname(event)
    streamer = (
        _make_streamer(rt, bot, event)
        if rt.settings.stream_replies and rt.settings.reply_max_bubbles > 1
        else None
    )
    async with rt.lock_for(f"private:{user_id}"):
        try:
            payload = await _generate_reply(
                rt,
                scope_type="private",
                scope_id=user_id,
                user_id=user_id,
                nickname=nickname,
                text=text,
                image_url=image_url,
                on_delta=streamer.on_delta if streamer else None,
            )
            if streamer and streamer.sent_prefix:
                await streamer.finish(payload.text)
            else:
                await _send_bubbles(rt, bot, event, payload.text, sticker=payload.sticker)
            rt.feedback.register(
                scope_type="private",
                scope_id=user_id,
                user_id=user_id,
                deai_score=int((payload.deai_meta or {}).get("score", 0)),
            )
            if await rt.budget.maybe_warn():
                await _safe_send(bot, event, "今天 token 用到 80% 了，省着点喂。")
        except BudgetExceeded as exc:
            logger.warning("Daily token budget exhausted: %s", exc)
            await _safe_send(bot, event, "今天 token 已经造完了，明天再来喂鱼。")
        except LLMError as exc:
            logger.warning("LLM error in private chat: %s", exc)
            rt.record_error("private-llm", str(exc))
            if not (streamer and streamer.sent_prefix):
                await _safe_send(bot, event, "我这会儿有点卡，等下再说。")
        except Exception as exc:
            logger.exception("Private chat failed")
            rt.record_error("private", str(exc))
            if not (streamer and streamer.sent_prefix):
                await _safe_send(bot, event, "我这会儿脑子没接上，稍后再试。")


def _group_trigger_context(rt: Runtime, event: GroupMessageEvent, group: Any) -> TriggerContext:
    text = event.get_plaintext()
    return TriggerContext(
        active=group.active,
        mode=group.mode,
        is_at_bot=bool(event.to_me) or is_at_bot(event.get_message(), event.self_id),
        is_reply_to_bot=is_reply_to_bot(event.reply, event.self_id),
        mentions_name=mentions_name(
            text,
            [*rt.persona.names, *rt.settings.nickname, rt.presence.nickname],
        ),
        same_user_as_last_reply=group.last_bot_reply_user == _uid(event),
        seconds_since_bot_reply=rt.db.seconds_since_last_bot_reply(group),
        followup_window=rt.settings.group_followup_window,
    )


async def _handle_group(rt: Runtime, bot: Bot, event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    user_id = _uid(event)
    if not await rt.db.mark_processed("group", group_id, str(event.message_id)):
        return
    if event.user_id == event.self_id:
        return
    rt.spawn(
        rt.stickers.ingest_event(
            event.get_message(),
            scope_type="group",
            scope_id=group_id,
            user_id=user_id,
            nickname=_nickname(event),
            message_id=str(event.message_id),
        )
    )

    group = await rt.db.ensure_group(group_id)
    text = event.get_plaintext().strip()
    if _is_command_text(rt, text):
        return
    image_url = extract_image_url(event.get_message())
    if not text and not image_url:
        return

    at_bot = bool(event.to_me) or is_at_bot(event.get_message(), event.self_id)
    reply_to_bot = is_reply_to_bot(event.reply, event.self_id)
    named = mentions_name(
        text,
        [*rt.persona.names, *rt.settings.nickname, rt.presence.nickname],
    )
    rt.feedback.observe(
        scope_type="group",
        scope_id=group_id,
        user_id=user_id,
        text=text,
        directed_at_bot=bool(at_bot or reply_to_bot or named),
        sticker=any(
            getattr(segment, "type", None) in {"face", "mface", "bface"}
            for segment in event.get_message()
        ),
    )
    if not group.active:
        if not at_bot:
            return
        await rt.db.activate_group(group_id, user_id)
        group = await rt.db.ensure_group(group_id)
        await _safe_send(bot, event, f"{rt.persona.display_name} 来了，已开启。发 /关闭 可以让我闭嘴。")

    trigger = _group_trigger_context(rt, event, group)
    if not trigger.should_reply():
        await _store_user_message(
            rt,
            scope_type="group",
            scope_id=group_id,
            user_id=user_id,
            nickname=_nickname(event),
            text=_history_text(text, image_url),
        )
        return

    now = time.monotonic()
    if (
        group.mode == "full"
        and not at_bot
        and now - rt.last_group_reply.get(group_id, 0.0) < rt.settings.group_cooldown
    ):
        await _store_user_message(
            rt,
            scope_type="group",
            scope_id=group_id,
            user_id=user_id,
            nickname=_nickname(event),
            text=_history_text(text, image_url),
        )
        return

    streamer = (
        _make_streamer(rt, bot, event)
        if rt.settings.stream_replies and rt.settings.reply_max_bubbles > 1
        else None
    )
    async with rt.lock_for(f"group:{group_id}"):
        try:
            payload = await _generate_reply(
                rt,
                scope_type="group",
                scope_id=group_id,
                user_id=user_id,
                nickname=_nickname(event),
                text=text,
                image_url=image_url,
                on_delta=streamer.on_delta if streamer else None,
            )
            if streamer and streamer.sent_prefix:
                await streamer.finish(payload.text)
            else:
                await _send_bubbles(rt, bot, event, payload.text, sticker=payload.sticker)
            rt.feedback.register(
                scope_type="group",
                scope_id=group_id,
                user_id=user_id,
                deai_score=int((payload.deai_meta or {}).get("score", 0)),
            )
            if await rt.budget.maybe_warn():
                await _safe_send(bot, event, "今天 token 用到 80% 了，省着点喂。")
            rt.last_group_reply[group_id] = time.monotonic()
        except BudgetExceeded as exc:
            logger.warning("Daily token budget exhausted: %s", exc)
            await _safe_send(bot, event, "今天 token 已经造完了，明天再来喂鱼。")
        except LLMError as exc:
            logger.warning("LLM error in group chat: %s", exc)
            rt.record_error("group-llm", str(exc))
            if not (streamer and streamer.sent_prefix):
                await _safe_send(bot, event, "我这会儿有点卡，等下再说。")
        except Exception as exc:
            logger.exception("Group chat failed")
            rt.record_error("group", str(exc))
            if not (streamer and streamer.sent_prefix):
                await _safe_send(bot, event, "我这会儿脑子没接上，稍后再试。")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
help_cmd = on_command("帮助", aliases={"help", "菜单"}, priority=1, block=True)
close_cmd = on_command("关闭", aliases={"close", "off"}, priority=1, block=True)
open_cmd = on_command("开启", aliases={"open", "on"}, priority=1, block=True)
status_cmd = on_command("状态", aliases={"status"}, priority=1, block=True)
mode_cmd = on_command("模式", aliases={"mode"}, priority=1, block=True)
reset_cmd = on_command("重置", aliases={"reset"}, priority=1, block=True)
persona_cmd = on_command("人设", aliases={"persona"}, priority=1, block=True)
remember_cmd = on_command("记住", aliases={"remember"}, priority=1, block=True)
memory_cmd = on_command("记忆", aliases={"memories"}, priority=1, block=True)
forget_cmd = on_command("忘记我", aliases={"forgetme"}, priority=1, block=True)
plugins_cmd = on_command("插件", aliases={"plugins"}, priority=1, block=True)
mute_cmd = on_command("禁言", aliases={"mute"}, priority=1, block=True)
unmute_cmd = on_command("解禁", aliases={"unmute"}, priority=1, block=True)
recall_cmd = on_command("撤回", aliases={"recall"}, priority=1, block=True)
notice_cmd = on_command("公告", aliases={"notice"}, priority=1, block=True)
card_cmd = on_command("改名片", aliases={"card"}, priority=1, block=True)
kick_cmd = on_command("踢", aliases={"kick"}, priority=1, block=True)
wholeban_cmd = on_command("全员禁言", aliases={"wholeban"}, priority=1, block=True)
setadmin_cmd = on_command("设管理", aliases={"setadmin"}, priority=1, block=True)
groupname_cmd = on_command("群名", aliases={"groupname"}, priority=1, block=True)
skills_cmd = on_command("技能", aliases={"skills"}, priority=1, block=True)
explore_cmd = on_command("探索", aliases={"explore"}, priority=1, block=True)
brain_cmd = on_command("大脑", aliases={"brain"}, priority=1, block=True)
profile_cmd = on_command("主页", aliases={"profile"}, priority=1, block=True)
qzone_cmd = on_command("发空间", aliases={"qzone"}, priority=1, block=True)
qzone_list_cmd = on_command("空间", aliases={"qzonelist"}, priority=1, block=True)
qzone_delete_cmd = on_command("删空间", aliases={"qzonedel"}, priority=1, block=True)
guild_cmd = on_command("频道", aliases={"guild"}, priority=1, block=True)
avatar_cmd = on_command("头像", aliases={"avatar"}, priority=1, block=True)
nickname_cmd = on_command("昵称", aliases={"nickname"}, priority=1, block=True)
signature_cmd = on_command("签名", aliases={"signature"}, priority=1, block=True)
online_cmd = on_command("在线", aliases={"onlinestatus"}, priority=1, block=True)
like_cmd = on_command("点赞", aliases={"like"}, priority=1, block=True)
poke_cmd = on_command("戳", aliases={"poke"}, priority=1, block=True)
group_portrait_cmd = on_command("群头像", aliases={"groupportrait"}, priority=1, block=True)
group_title_cmd = on_command("群头衔", aliases={"grouptitle"}, priority=1, block=True)
group_sign_cmd = on_command("群签到", aliases={"groupsign"}, priority=1, block=True)
essence_cmd = on_command("精华", aliases={"essence"}, priority=1, block=True)
unessence_cmd = on_command("取消精华", aliases={"unessence"}, priority=1, block=True)


async def _audit_skill(
    rt: Runtime,
    event: MessageEvent,
    skill_id: str,
    *,
    status: str,
    target_id: str | None = None,
    args: str = "",
    error: str = "",
) -> None:
    scope_type, scope_id = _event_scope(event)
    await rt.skills.audit(
        skill_id,
        scope_type=scope_type,
        scope_id=scope_id,
        actor_id=_uid(event),
        target_id=target_id,
        args=args,
        status=status,
        error=error,
    )


async def _skill_allowed(
    rt: Runtime,
    bot: Bot,
    event: MessageEvent,
    skill_id: str,
    *,
    target_id: str | None = None,
    args: str = "",
) -> bool:
    try:
        await rt.skills.check(skill_id, event)
    except SkillDenied as exc:
        await _audit_skill(
            rt,
            event,
            skill_id,
            status="denied",
            target_id=target_id,
            args=args,
            error=str(exc),
        )
        await _safe_send(bot, event, str(exc))
        return False

    try:
        decision = await rt.autonomy.review(
            event,
            skill_id,
            target_id=target_id,
            args=args,
        )
    except Exception as exc:
        logger.exception("Autonomy review failed for %s", skill_id)
        await _audit_skill(
            rt,
            event,
            skill_id,
            status="denied",
            target_id=target_id,
            args=args,
            error=f"自主判断异常：{exc}",
        )
        await _safe_send(bot, event, "我脑子刚卡了一下，这事先不干。")
        return False

    if not decision.allow:
        await _audit_skill(
            rt,
            event,
            skill_id,
            status="denied",
            target_id=target_id,
            args=args,
            error=f"自主拒绝：{decision.reason}",
        )
        await _safe_send(bot, event, f"我不想干：{decision.reason}")
        return False
    return True


async def _skill_failed(
    rt: Runtime,
    bot: Bot,
    event: MessageEvent,
    skill_id: str,
    exc: Exception,
    *,
    target_id: str | None = None,
    args: str = "",
) -> None:
    await _audit_skill(
        rt,
        event,
        skill_id,
        status="failed",
        target_id=target_id,
        args=args,
        error=str(exc),
    )
    await _safe_send(bot, event, f"没成：{exc}")


def _require_group(event: MessageEvent) -> GroupMessageEvent | None:
    if isinstance(event, GroupMessageEvent):
        return event
    return None


def _event_image_url(event: MessageEvent) -> str | None:
    url = extract_image_url(event.get_message())
    if url:
        return url
    reply = getattr(event, "reply", None)
    if reply is not None:
        return extract_image_url(getattr(reply, "message", None))
    return None


def _event_image_urls(event: MessageEvent) -> list[str]:
    urls: list[str] = []
    try:
        segments = list(event.get_message())
    except TypeError:
        segments = []
    for segment in segments:
        if getattr(segment, "type", None) != "image":
            continue
        data = getattr(segment, "data", {}) or {}
        url = data.get("url") or data.get("file")
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    if not urls:
        reply = getattr(event, "reply", None)
        if reply is not None:
            urls.extend(_event_image_urls(reply))  # type: ignore[arg-type]
    return urls[:9]


def _reply_message_id(event: MessageEvent) -> str | None:
    reply = getattr(event, "reply", None)
    message_id = getattr(reply, "message_id", None) if reply is not None else None
    return str(message_id) if message_id else None


async def _brain_note_line(note: BrainNote, *, full: bool = False) -> str:
    scope = "全局" if note.scope_type == "global" else f"{note.scope_type}:{note.scope_id}"
    body = note.content if full else note.content.replace("\n", " ")[:120]
    return f"#{note.id} [{scope}] {note.title}\n{body[:1800]}"


@help_cmd.handle()
async def _help() -> None:
    await help_cmd.finish(
        "命令：\n"
        "/关闭  /开启  /状态  /模式 智能|全量\n"
        "/重置  /人设  /插件\n"
        "/禁言 @人 10m  /解禁 @人  /撤回\n"
        "/公告 <内容>  /改名片 @人 <名片>\n"
        "/踢 @人  /全员禁言 开|关  /设管理 @人 开|关  /群名 <名字>\n"
        "/技能  /功能 [类别|搜 词]  /探索 [跑]  /大脑 [查|看|记|删]  /主页 [优化|应用|历史|复盘]\n"
        "/发空间 [权限=好友] [内容] [图片]  /空间 [列表|点赞|评论|删评论]  /删空间 <tid>  /风格\n"
        "/频道 [状态|列表|设置|发送|历史]  /头像 [图片]  /昵称 <名字>  /签名 <签名>  /在线 <状态>  /点赞 @人\n"
        "/戳 @人  /群头像 [图片]  /群头衔 @人 <头衔>  /群签到\n"
        "/精华  /取消精华（先回复一条消息）\n"
        "/记住 <内容>  /记忆  /忘记我\n"
        "群聊第一次 @ 我开启；之后叫名字、回复我，或接着聊就行。"
    )


@close_cmd.handle()
async def _close(event: MessageEvent) -> None:
    rt = _get_runtime()
    scope_type, scope_id = _event_scope(event)
    if isinstance(event, GroupMessageEvent):
        if not await _can_manage_group(rt, event):
            await close_cmd.finish("这事得群主、管理员或者激活我的人来。")
        await rt.db.deactivate_group(str(event.group_id))
        rt.feedback.note_command_cancel(
            scope_type=scope_type, scope_id=scope_id, user_id=_uid(event)
        )
        await close_cmd.finish("好，我闭嘴。")
    await rt.db.set_private_active(_uid(event), False)
    rt.feedback.note_command_cancel(
        scope_type=scope_type, scope_id=scope_id, user_id=_uid(event)
    )
    await close_cmd.finish("好，我先不回了。发 /开启 可以叫我。")


@open_cmd.handle()
async def _open(event: MessageEvent) -> None:
    rt = _get_runtime()
    if isinstance(event, GroupMessageEvent):
        if not await _can_manage_group(rt, event):
            await open_cmd.finish("这事得群主、管理员或者激活我的人来。")
        await rt.db.activate_group(str(event.group_id), _uid(event))
        await open_cmd.finish("我回来了。")
    await rt.db.set_private_active(_uid(event), True)
    await open_cmd.finish("好，我在。")


@status_cmd.handle()
async def _status(event: MessageEvent) -> None:
    rt = _get_runtime()
    if isinstance(event, GroupMessageEvent):
        group = await rt.db.ensure_group(str(event.group_id))
        state = "已开启" if group.active else "已关闭"
        await status_cmd.finish(f"群聊：{state}\n模式：{group.mode}")
    user = await rt.db.ensure_user(_uid(event))
    state = "已开启" if user.private_active else "已关闭"
    await status_cmd.finish(f"私聊：{state}")


@mode_cmd.handle()
async def _mode(event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    if not isinstance(event, GroupMessageEvent):
        await mode_cmd.finish("私聊不需要模式。")
    group = await rt.db.ensure_group(str(event.group_id))
    value = args.extract_plain_text().strip().lower()
    if not value:
        await mode_cmd.finish(f"当前模式：{group.mode}（可选：智能 / 全量）")
    mapping = {"智能": "smart", "smart": "smart", "全量": "full", "full": "full"}
    if value not in mapping:
        await mode_cmd.finish("只支持：/模式 智能 或 /模式 全量")
    if not await _can_manage_group(rt, event):
        await mode_cmd.finish("这事得群主、管理员或者激活我的人来。")
    new_mode = mapping[value]
    await rt.db.set_group_mode(str(event.group_id), new_mode)
    await mode_cmd.finish(f"模式切到{'智能' if new_mode == 'smart' else '全量'}了。")


@reset_cmd.handle()
async def _reset(event: MessageEvent) -> None:
    rt = _get_runtime()
    if isinstance(event, GroupMessageEvent):
        if not await _can_manage_group(rt, event):
            await reset_cmd.finish("这事得群主、管理员或者激活我的人来。")
        await rt.db.clear_session("group", str(event.group_id))
        await reset_cmd.finish("这个群的上下文清掉了。")
    await rt.db.clear_session("private", _uid(event))
    await reset_cmd.finish("我们刚才聊的上下文清掉了。")


@persona_cmd.handle()
async def _persona(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if raw in {"复盘", "自我批评", "review"}:
        if not _is_superuser(rt, event):
            await persona_cmd.finish("复盘只有主人能点。")
        scope_type, scope_id = _event_scope(event)
        await _safe_send(bot, event, "我看看自己最近说的话……")
        try:
            result = await rt.selfreview.review(scope_type, scope_id, trigger="manual", force=True)
        except Exception as exc:
            await persona_cmd.finish(f"复盘没成：{exc}")
            return
        lines = [f"诊断：{result.get('diagnosis') or '（没有给出诊断）'}"]
        if result.get("applied"):
            fields = "、".join(result.get("fields_changed") or [])
            lines.append(
                f"画像已更新：v{result.get('version_before')} → v{result.get('version_after')}"
                + (f"（改了 {fields}）" if fields else "")
            )
        elif result.get("note"):
            lines.append(result["note"])
        if result.get("brain_note"):
            lines.append(f"沉淀：{result['brain_note']}")
        await persona_cmd.finish("\n".join(lines))
        return
    persona = rt.persona
    traits = "、".join(persona.traits) if persona.traits else "自然"
    await persona_cmd.finish(
        f"{persona.display_name}\n"
        f"{persona.identity}\n"
        f"关系：{persona.relationship}\n"
        f"性格：{traits}\n"
        f"毒舌强度：{persona.intensity}/3"
    )


async def _ensure_active_scope(rt: Runtime, event: MessageEvent) -> tuple[str, str, str] | None:
    if isinstance(event, GroupMessageEvent):
        group = await rt.db.ensure_group(str(event.group_id))
        if not group.active:
            return None
        return "group", str(event.group_id), _uid(event)
    return "private", _uid(event), _uid(event)


@remember_cmd.handle()
async def _remember(event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    content = args.extract_plain_text().strip()
    if not content:
        await remember_cmd.finish("用法：/记住 你喜欢什么、叫什么、有什么约定")
    scope = await _ensure_active_scope(rt, event)
    if scope is None:
        await remember_cmd.finish("先 @ 我开启群聊，再让我记。")
    scope_type, scope_id, user_id = scope
    ok = await rt.memory.remember(
        scope_type=scope_type,
        scope_id=scope_id,
        user_id=user_id,
        content=content,
    )
    await remember_cmd.finish("记住了。" if ok else "这句我不太方便记，换个说法？")


@memory_cmd.handle()
async def _memories(event: MessageEvent) -> None:
    rt = _get_runtime()
    scope = await _ensure_active_scope(rt, event)
    if scope is None:
        await memory_cmd.finish("先 @ 我开启群聊，再查记忆。")
    scope_type, scope_id, user_id = scope
    items = await rt.memory.list_memories(
        scope_type=scope_type,
        scope_id=scope_id,
        user_id=user_id,
        limit=10,
    )
    if not items:
        await memory_cmd.finish("还没记住什么。")
    lines = [f"{index}. {item.content}" for index, item in enumerate(items, start=1)]
    await memory_cmd.finish("我记得：\n" + "\n".join(lines))


@forget_cmd.handle()
async def _forget(event: MessageEvent) -> None:
    rt = _get_runtime()
    scope = await _ensure_active_scope(rt, event)
    if scope is None:
        await forget_cmd.finish("先 @ 我开启群聊，再操作。")
    scope_type, scope_id, user_id = scope
    count = await rt.memory.forget_user(
        scope_type=scope_type,
        scope_id=scope_id,
        user_id=user_id,
    )
    await forget_cmd.finish(f"删掉了 {count} 条关于你的记忆。")


@plugins_cmd.handle()
async def _plugins(event: MessageEvent) -> None:
    rt = _get_runtime()
    if not _is_superuser(rt, event):
        await plugins_cmd.finish("只有 Bot 主人能看。")
    await plugins_cmd.finish(rt.plugins.summary())


@mute_cmd.handle()
async def _mute(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip()
    target, rest = target_from_args(raw, explicit=target_from_event(group))
    if not target:
        await _safe_send(bot, event, "用法：/禁言 @人 10m，或回复一条消息再发。")
        return
    if not await _skill_allowed(rt, bot, event, "group.mute", target_id=target, args=raw):
        return
    try:
        seconds = parse_duration(rest)
        await rt.group_admin.mute(bot, group, target, seconds)
    except (GroupActionError, ValueError) as exc:
        await _skill_failed(rt, bot, event, "group.mute", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "group.mute", status="ok", target_id=target, args=f"{seconds}s")
    await mute_cmd.finish(f"禁言了 {target}，{seconds} 秒。")


@unmute_cmd.handle()
async def _unmute(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip()
    target, _rest = target_from_args(raw, explicit=target_from_event(group))
    if not target:
        await _safe_send(bot, event, "用法：/解禁 @人，或回复一条消息再发。")
        return
    if not await _skill_allowed(rt, bot, event, "group.unmute", target_id=target, args=raw):
        return
    try:
        await rt.group_admin.unmute(bot, group, target)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.unmute", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "group.unmute", status="ok", target_id=target)
    await unmute_cmd.finish(f"给 {target} 解禁了。")


@recall_cmd.handle()
async def _recall(bot: Bot, event: MessageEvent) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    reply = getattr(event, "reply", None)
    if reply is None:
        await _safe_send(bot, event, "回复要撤回的那条消息，再发 /撤回。")
        return
    if not await _skill_allowed(rt, bot, event, "group.recall", args=str(reply.message_id)):
        return
    try:
        await rt.group_admin.recall(bot, group, str(reply.message_id))
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.recall", exc)
        return
    await _audit_skill(rt, event, "group.recall", status="ok", args=str(reply.message_id))
    await recall_cmd.finish("撤了。")


@notice_cmd.handle()
async def _notice(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    content = args.extract_plain_text().strip()
    if not content:
        await _safe_send(bot, event, "用法：/公告 公告内容")
        return
    if not await _skill_allowed(rt, bot, event, "group.notice", args=content[:200]):
        return
    try:
        await rt.group_admin.notice(bot, group, content)
    except (GroupActionError, PolicyViolation) as exc:
        await _skill_failed(rt, bot, event, "group.notice", exc, args=content[:200])
        return
    await _audit_skill(rt, event, "group.notice", status="ok", args=content[:200])
    await notice_cmd.finish("公告发了。")


@card_cmd.handle()
async def _card(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip()
    target, card = target_from_args(raw, explicit=target_from_event(group))
    if not target or not card:
        await _safe_send(bot, event, "用法：/改名片 @人 新名片")
        return
    if not await _skill_allowed(rt, bot, event, "group.card", target_id=target, args=raw):
        return
    try:
        await rt.group_admin.set_card(bot, group, target, card)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.card", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "group.card", status="ok", target_id=target, args=card[:100])
    await card_cmd.finish("改了。")


@kick_cmd.handle()
async def _kick(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip()
    target, _rest = target_from_args(raw, explicit=target_from_event(group))
    if not target:
        await _safe_send(bot, event, "用法：/踢 @人，或回复一条消息再发。")
        return
    if not await _skill_allowed(rt, bot, event, "group.kick", target_id=target, args=raw):
        return
    try:
        await rt.group_admin.kick(bot, group, target)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.kick", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "group.kick", status="ok", target_id=target)
    await kick_cmd.finish(f"把 {target} 请出去了。")


@wholeban_cmd.handle()
async def _wholeban(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip().lower()
    if raw in {"开", "开启", "on", "1", "true"}:
        enable = True
    elif raw in {"关", "关闭", "off", "0", "false"}:
        enable = False
    else:
        await _safe_send(bot, event, "用法：/全员禁言 开 或 /全员禁言 关")
        return
    if not await _skill_allowed(rt, bot, event, "group.whole_ban", args=raw):
        return
    try:
        await rt.group_admin.set_whole_ban(bot, group, enable)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.whole_ban", exc, args=raw)
        return
    await _audit_skill(rt, event, "group.whole_ban", status="ok", args=raw)
    await wholeban_cmd.finish("全员禁言开了。" if enable else "全员禁言关了。")


@setadmin_cmd.handle()
async def _setadmin(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    raw = args.extract_plain_text().strip()
    target, rest = target_from_args(raw, explicit=target_from_event(group))
    value = rest.lower()
    if not target or value not in {"开", "开启", "on", "1", "true", "关", "关闭", "off", "0", "false"}:
        await _safe_send(bot, event, "用法：/设管理 @人 开|关")
        return
    enable = value in {"开", "开启", "on", "1", "true"}
    if not await _skill_allowed(rt, bot, event, "group.set_admin", target_id=target, args=raw):
        return
    try:
        await rt.group_admin.set_admin(bot, group, target, enable)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.set_admin", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "group.set_admin", status="ok", target_id=target, args=str(enable))
    await setadmin_cmd.finish("设置好了。")


@groupname_cmd.handle()
async def _groupname(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await _safe_send(bot, event, "这个只能在群里用。")
        return
    name = args.extract_plain_text().strip()
    if not name:
        await _safe_send(bot, event, "用法：/群名 新名字")
        return
    if not await _skill_allowed(rt, bot, event, "group.name", args=name[:100]):
        return
    try:
        await rt.group_admin.set_group_name(bot, group, name)
    except GroupActionError as exc:
        await _skill_failed(rt, bot, event, "group.name", exc, args=name[:100])
        return
    await _audit_skill(rt, event, "group.name", status="ok", args=name[:100])
    await groupname_cmd.finish("群名改了。")


@skills_cmd.handle()
async def _skills(event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    scope_type, scope_id = _event_scope(event)
    role = rt.skills.role_for_event(event)
    raw = args.extract_plain_text().strip()
    items = await rt.skills.list_effective(scope_type, scope_id)
    rank = ROLE_RANK.get(role, 0)
    visible = [
        item
        for item in items
        if item["enabled"]
        and rank >= ROLE_RANK.get(item["effective_min_role"], 0)
        and (not item["group_only"] or isinstance(event, GroupMessageEvent))
    ]
    if not visible:
        await skills_cmd.finish("当前没有可用技能。")
    if raw:
        # 按类别列出明细
        wanted = raw.casefold()
        matched = [item for item in visible if item["category"].casefold() == wanted]
        if not matched:
            matched = [item for item in visible if wanted in item["id"].casefold() or wanted in item["name"]]
        if not matched:
            await skills_cmd.finish(f"没有叫「{raw}」的技能类别。发 /技能 看类别列表。")
        lines = [f"{item['name']}（{item['id']}）" for item in matched[:30]]
        more = f"\n…还有 {len(matched) - 30} 个" if len(matched) > 30 else ""
        await skills_cmd.finish(f"「{raw}」下的技能：\n" + "\n".join(lines) + more)
    # 类别摘要：技能太多，QQ 里只给分类和数量
    counter: dict[str, int] = {}
    for item in visible:
        counter[item["category"]] = counter.get(item["category"], 0) + 1
    lines = [f"{name} ×{count}" for name, count in sorted(counter.items())]
    await skills_cmd.finish(
        f"当前可用 {len(visible)} 个技能：\n"
        + "\n".join(lines)
        + "\n用 /技能 <类别> 看明细。"
    )


function_cmd = on_command("功能", aliases={"actions", "能力"}, priority=1, block=True)


@function_cmd.handle()
async def _functions(event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    items = [item for item in rt.actions.catalog() if item["executor"] != "core"]
    if not items:
        await function_cmd.finish("统一能力表是空的。")
    if raw.startswith("搜"):
        keyword = raw[1:].strip()
        if keyword:
            items = [
                item
                for item in items
                if keyword.casefold() in item["id"].casefold()
                or keyword in item["name"]
                or keyword in item["description"]
            ]
            lines = [
                f"{item['name']}（{item['id']}）{item['executor']}{'·只读' if item['readonly'] else ''}"
                for item in items[:25]
            ]
            await function_cmd.finish(
                "搜到这些：\n" + ("\n".join(lines) if lines else "没有匹配的能力。")
            )
    counter: dict[str, int] = {}
    for item in items:
        counter[item["category"]] = counter.get(item["category"], 0) + 1
    if raw and raw.casefold() not in {"搜", "搜索"}:
        wanted = raw.casefold()
        matched = [item for item in items if item["category"].casefold() == wanted]
        if not matched:
            matched = [item for item in items if wanted in item["id"].casefold() or wanted in item["name"]]
        if matched:
            lines = [
                f"{item['name']}（{item['id']}）{item['executor']}{'·只读' if item['readonly'] else ''}"
                for item in matched[:30]
            ]
            more = f"\n…还有 {len(matched) - 30} 个" if len(matched) > 30 else ""
            await function_cmd.finish(f"「{raw}」下的能力：\n" + "\n".join(lines) + more)
    executors: dict[str, int] = {}
    for item in items:
        executors[item["executor"]] = executors.get(item["executor"], 0) + 1
    lines = [f"{name} ×{count}" for name, count in sorted(counter.items())]
    summary = "、".join(f"{name} {count}" for name, count in sorted(executors.items()))
    await function_cmd.finish(
        f"统一能力表 {len(items)} 项（{summary}）：\n"
        + "\n".join(lines)
        + "\n用 /功能 <类别> 看明细，/功能 搜 <词> 搜索。执行在控制台“功能中心”。"
    )


style_cmd = on_command("风格", aliases={"mystyle"}, priority=1, block=True)


@style_cmd.handle()
async def _style(event: MessageEvent) -> None:
    rt = _get_runtime()
    scope_type, scope_id = _event_scope(event)
    profile = await rt.db.get_style_profile(scope_type, scope_id)
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
    feedback = await rt.db.feedback_stats(scope_type=scope_type, scope_id=scope_id, since=since)
    deai = await rt.db.deai_stats(since=since)
    lines = [
        f"本范围画像：v{profile.version}（{'启用' if profile.enabled else '停用'}，置信度 {profile.confidence:.2f}）"
        if profile
        else "本范围画像：还没有（让风格学习先攒样本）",
        f"近 7 天互动反馈：{feedback['count']} 次，均分 {feedback['avg_score']}",
        f"近 7 天 AI 味：{deai['count']} 条检测，均分 {deai['avg_score']}，改写 {deai['rewrites']} 条",
    ]
    if deai["top_rules"]:
        top = "、".join(f"{item['rule']}×{item['hits']}" for item in deai["top_rules"][:3])
        lines.append(f"最容易犯的 AI 腔：{top}")
    await style_cmd.finish("\n".join(lines))


def _brain_scope_ok(event: MessageEvent, note: BrainNote) -> bool:
    if note.scope_type == "global":
        return True
    scope_type, scope_id = _event_scope(event)
    return note.scope_type == scope_type and note.scope_id == scope_id


@explore_cmd.handle()
async def _explore(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip().lower()
    status = await rt.explore.status()
    if not status["enabled"]:
        await explore_cmd.finish("探索模式还没开。让主人在控制台“探索模式”里打开。")
    if raw not in {"跑", "run", "开始", "scan"}:
        latest = status["findings"][:5]
        lines = [f"{item['action']} · {item['summary']}" for item in latest]
        body = "\n".join(lines) if lines else "还没探测过。发 /探索 跑 开始。"
        await explore_cmd.finish(f"探索模式：开\n最近发现：\n{body}")
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    try:
        findings = await rt.explore.run(bot, group_id=group_id)
    except ExploreError as exc:
        await explore_cmd.finish(str(exc))
    except Exception as exc:
        rt.record_error("explore", str(exc))
        await explore_cmd.finish("探索时接口抽风了，稍后再试。")
    if not findings:
        await explore_cmd.finish("这轮没探到东西，可能 NapCat 没连上。")
    lines = [f"{item['action']} · {item['summary']}" for item in findings[:8]]
    await explore_cmd.finish("探索了一轮：\n" + "\n".join(lines))


@brain_cmd.handle()
async def _brain(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    scope_type, scope_id = _event_scope(event)
    raw = args.extract_plain_text().strip()
    if not raw:
        notes = await rt.db.list_brain_notes(scope_type=scope_type, scope_id=scope_id, limit=10)
        if not notes:
            await brain_cmd.finish("大脑里还没有当前范围的笔记。")
        lines = [await _brain_note_line(note) for note in notes]
        await brain_cmd.finish("最近的笔记：\n" + "\n\n".join(lines))

    parts = raw.split(maxsplit=1)
    action = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if action == "查":
        if not rest:
            await brain_cmd.finish("用法：/大脑 查 关键词")
        if not await _skill_allowed(rt, bot, event, "brain.search", args=rest):
            return
        notes = await rt.brain.search(rest, scope_type=scope_type, scope_id=scope_id)
        if not notes:
            await _audit_skill(rt, event, "brain.search", status="ok", args=rest)
            await brain_cmd.finish("没搜到。")
        lines = [await _brain_note_line(note) for note in notes]
        await _audit_skill(rt, event, "brain.search", status="ok", args=rest)
        await brain_cmd.finish("\n\n".join(lines))

    if action == "看":
        try:
            note_id = int(rest)
        except ValueError:
            await brain_cmd.finish("用法：/大脑 看 ID")
        if not await _skill_allowed(rt, bot, event, "brain.read", args=rest):
            return
        note = await rt.db.get_brain_note(note_id)
        if note is None or not _brain_scope_ok(event, note):
            await brain_cmd.finish("没有这条笔记，或不在当前范围。")
        if note.visibility == "owner" and not _is_superuser(rt, event):
            await brain_cmd.finish("这条笔记只有主人能看。")
        await _audit_skill(rt, event, "brain.read", status="ok", args=str(note_id))
        await brain_cmd.finish(await _brain_note_line(note, full=True))

    if action == "记":
        if "|" not in rest:
            await brain_cmd.finish("用法：/大脑 记 标题 | 内容")
        title, content = [item.strip() for item in rest.split("|", 1)]
        if not title or not content:
            await brain_cmd.finish("标题和内容都不能空。")
        if not await _skill_allowed(rt, bot, event, "brain.write", args=title):
            return
        try:
            note = await rt.brain.create_note(
                title=title,
                content=content,
                tags="手动",
                scope_type=scope_type,
                scope_id=scope_id,
                visibility="scope",
                source="manual",
                source_user_id=_uid(event),
            )
        except BrainError as exc:
            await _skill_failed(rt, bot, event, "brain.write", exc, args=title)
            return
        await _audit_skill(rt, event, "brain.write", status="ok", target_id=str(note.id), args=title)
        await brain_cmd.finish(f"记下了，编号 #{note.id}。")

    if action == "删":
        try:
            note_id = int(rest)
        except ValueError:
            await brain_cmd.finish("用法：/大脑 删 ID")
        if not await _skill_allowed(rt, bot, event, "brain.delete", target_id=str(note_id), args=rest):
            return
        note = await rt.db.get_brain_note(note_id)
        if note is None or not _brain_scope_ok(event, note):
            await brain_cmd.finish("没有这条笔记，或不在当前范围。")
        await rt.brain.delete_note(note_id)
        await _audit_skill(rt, event, "brain.delete", status="ok", target_id=str(note_id))
        await brain_cmd.finish(f"删掉 #{note_id} 了。")

    await brain_cmd.finish("用法：/大脑 [查|看|记|删]")


@profile_cmd.handle()
async def _profile(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if not raw or raw == "状态":
        current = rt.presence.current
        if current is None:
            await profile_cmd.finish("还没有生成主页资料。")
        await profile_cmd.finish(
            f"{current.nickname}\n签名：{current.signature}\n简介：{current.bio}\n"
            f"标签：{'、'.join(current.tags) or '无'}"
        )
    if raw == "优化":
        if not await _skill_allowed(rt, bot, event, "profile.optimize"):
            return
        try:
            profile = await rt.presence.optimize_and_apply(bot)
        except PresenceError as exc:
            await _skill_failed(rt, bot, event, "profile.optimize", exc)
            return
        await profile_cmd.finish(f"主页换了：{profile.nickname}\n{profile.signature}")
    if raw == "应用":
        if not await _skill_allowed(rt, bot, event, "profile.apply"):
            return
        try:
            profile = await rt.presence.apply_profile(bot)
        except PresenceError as exc:
            await _skill_failed(rt, bot, event, "profile.apply", exc)
            return
        await profile_cmd.finish(f"已应用到 QQ：{profile.nickname}")
    if raw == "历史":
        if not await _skill_allowed(rt, bot, event, "profile.apply"):
            return
        versions = rt.presence.list_history()
        if not versions:
            await profile_cmd.finish("还没有历史版本。")
        lines = [f"{item['version']} · {item['profile'].get('nickname', '')}" for item in versions]
        await profile_cmd.finish("主页历史：\n" + "\n".join(lines))
    if raw.startswith("恢复 "):
        if not await _skill_allowed(rt, bot, event, "profile.apply"):
            return
        version = raw[3:].strip()
        try:
            profile = await rt.presence.restore_history(version, bot)
        except PresenceError as exc:
            await _skill_failed(rt, bot, event, "profile.apply", exc)
            return
        await profile_cmd.finish(f"恢复到：{profile.nickname}")
    await profile_cmd.finish("用法：/主页 [优化|应用|历史|恢复 版本]")


@qzone_cmd.handle()
async def _qzone(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    if not await _skill_allowed(rt, bot, event, "qzone.post"):
        return
    content = args.extract_plain_text().strip()
    # 可选前缀：权限=公开|好友|自己（1/4/64），按次指定本条动态可见范围
    ugc_right: int | None = None
    right_match = re.match(r"权限\s*[=＝:：]\s*(公开|好友|自己|私密)\s*", content)
    if right_match:
        label = right_match.group(1)
        ugc_right = {"公开": 1, "好友": 4, "自己": 64, "私密": 64}[label]
        content = content[right_match.end():].strip()
    images = _event_image_urls(event)
    try:
        result = await rt.presence.post_qzone(
            bot=bot,
            content=content or None,
            images=images,
            source="manual",
            ugc_right=ugc_right,
        )
    except (PolicyViolation, PresenceError) as exc:
        await _skill_failed(rt, bot, event, "qzone.post", exc)
        return
    right_label = {1: "公开", 4: "好友可见", 64: "仅自己"}.get(result.get("ugc_right", 1), "")
    await qzone_cmd.finish(
        f"发了（{right_label}）：{result['content']}\ntid={result['tid']}"
    )


def _qzone_post_line(post: Any, *, index: int) -> str:
    comments = len(getattr(post, "comments", []) or [])
    content = str(getattr(post, "content", "") or "").replace("\n", " ")[:80]
    unikey = str(getattr(post, "unikey", "") or "")
    suffix = f" unikey={unikey}" if unikey else ""
    return (
        f"{index}. {getattr(post, 'created_at', '')} · 赞{getattr(post, 'like_count', 0)}"
        f" 评{comments} · {content}\ntid={getattr(post, 'tid', '')}{suffix}"
    )


@qzone_list_cmd.handle()
async def _qzone_list(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if not raw:
        if not await _skill_allowed(rt, bot, event, "qzone.list"):
            return
        posts = await rt.db.list_social_posts(limit=10)
        if not posts:
            await qzone_list_cmd.finish("最近没发空间。用 /空间 列表 拉一下实时空间。")
        lines = [f"{post.created_at} [{post.status}] {post.content[:40]} ({post.tid})" for post in posts]
        await qzone_list_cmd.finish("最近的空间动态：\n" + "\n".join(lines))
    action, _, rest = raw.partition(" ")
    action = action.strip()
    rest = rest.strip()
    if action in {"列表", "list"}:
        if not await _skill_allowed(rt, bot, event, "qzone.list", args=rest):
            return
        target = rest.split()[0] if rest else None
        try:
            posts = await rt.qzone.list_posts(bot, target_uin=target, limit=5)
        except QzoneError as exc:
            await _skill_failed(rt, bot, event, "qzone.list", exc)
            return
        if not posts:
            await qzone_list_cmd.finish("空间里没拉到动态。")
        await _audit_skill(rt, event, "qzone.list", status="ok", args=target or "self")
        await qzone_list_cmd.finish(
            "实时空间：\n" + "\n\n".join(_qzone_post_line(post, index=i + 1) for i, post in enumerate(posts))
        )
    if action in {"点赞", "like", "取消赞", "unlike"}:
        parts = rest.split(maxsplit=1)
        tid = parts[0] if parts else ""
        if not tid:
            await qzone_list_cmd.finish("用法：/空间 点赞 <tid> [unikey]")
        unikey = parts[1].strip() if len(parts) > 1 else ""
        active = action in {"点赞", "like"}
        skill_id = "qzone.like"
        if not await _skill_allowed(rt, bot, event, skill_id, target_id=tid, args=tid):
            return
        try:
            result = await rt.qzone.set_like(bot, tid=tid, active=active, unikey=unikey)
        except QzoneError as exc:
            await _skill_failed(rt, bot, event, skill_id, exc, target_id=tid)
            return
        await _audit_skill(rt, event, skill_id, status="ok", target_id=tid)
        await qzone_list_cmd.finish("点赞了。" if result["active"] else "取消赞了。")
    if action in {"评论", "comment"}:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            await qzone_list_cmd.finish("用法：/空间 评论 <tid> <内容>")
        tid, content = parts[0], parts[1].strip()
        if not await _skill_allowed(rt, bot, event, "qzone.comment", target_id=tid, args=content[:100]):
            return
        try:
            result = await rt.qzone.comment(bot, tid=tid, content=content)
        except QzoneError as exc:
            await _skill_failed(rt, bot, event, "qzone.comment", exc, target_id=tid)
            return
        await _audit_skill(rt, event, "qzone.comment", status="ok", target_id=tid)
        await qzone_list_cmd.finish(
            f"评论了：{result['content']}" + (f"\ncomment_id={result['comment_id']}" if result["comment_id"] else "")
        )
    if action in {"删评论", "delete_comment", "delcomment"}:
        parts = rest.split()
        if len(parts) < 2:
            await qzone_list_cmd.finish("用法：/空间 删评论 <tid> <comment_id>")
        tid, comment_id = parts[0], parts[1]
        if not await _skill_allowed(
            rt, bot, event, "qzone.comment_delete", target_id=tid, args=comment_id
        ):
            return
        try:
            await rt.qzone.delete_comment(bot, tid=tid, comment_id=comment_id)
        except QzoneError as exc:
            await _skill_failed(rt, bot, event, "qzone.comment_delete", exc, target_id=tid)
            return
        await _audit_skill(rt, event, "qzone.comment_delete", status="ok", target_id=tid)
        await qzone_list_cmd.finish("评论删了。")
    await qzone_list_cmd.finish("用法：/空间 [列表 [QQ]|点赞 <tid> [unikey]|取消赞 <tid> [unikey]|评论 <tid> <内容>|删评论 <tid> <comment_id>]")


@qzone_delete_cmd.handle()
async def _qzone_delete(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    tid = args.extract_plain_text().strip()
    if not tid:
        await _safe_send(bot, event, "用法：/删空间 <tid>")
        return
    if not await _skill_allowed(rt, bot, event, "qzone.delete", target_id=tid, args=tid):
        return
    try:
        await rt.presence.delete_qzone(tid, bot)
    except PresenceError as exc:
        await _skill_failed(rt, bot, event, "qzone.delete", exc, target_id=tid)
        return
    await qzone_delete_cmd.finish("删了。")


@guild_cmd.handle()
async def _guild(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    action, _, rest = raw.partition(" ")
    action = action.strip()
    rest = rest.strip()
    if not raw or action in {"状态", "status"}:
        if not await _skill_allowed(rt, bot, event, "guild.status"):
            return
        try:
            status = await rt.guild.status(bot)
        except GuildError as exc:
            await _skill_failed(rt, bot, event, "guild.status", exc)
            return
        backend = {"plugin": "同账号插件", "official": "官方机器人", "auto": "自动", "off": "关闭"}.get(
            str(status["backend"]), str(status["backend"])
        )
        await _audit_skill(rt, event, "guild.status", status="ok")
        await guild_cmd.finish(
            f"频道后端：{backend}\n"
            f"guild_id：{status['guild_id'] or '未设置'}\n"
            f"channel_id：{status['channel_id'] or '未设置'}\n"
            f"同账号插件：{'可用' if status['plugin_available'] else '不可用'}\n"
            f"官方机器人：{'已配置' if status['official_configured'] else '未配置'}"
            + (f"\n最近错误：{status['last_error']}" if status.get("last_error") else "")
        )
    if action in {"列表", "list"}:
        if not await _skill_allowed(rt, bot, event, "guild.list"):
            return
        try:
            data = await rt.guild.list_channels(bot)
        except GuildError as exc:
            await _skill_failed(rt, bot, event, "guild.list", exc)
            return
        channels = data.get("channels") or []
        if not channels:
            await guild_cmd.finish("没有可用的频道列表；先用 /频道 设置 <guild_id> <channel_id> 配一个。")
        lines = [
            f"{item.get('name') or '未命名'} · id={item.get('id') or item.get('channel_id') or ''}"
            for item in channels[:10]
            if isinstance(item, dict)
        ]
        await _audit_skill(rt, event, "guild.list", status="ok")
        await guild_cmd.finish("频道列表：\n" + "\n".join(lines))
    if action in {"设置", "config"}:
        parts = rest.split()
        if len(parts) < 2:
            await guild_cmd.finish("用法：/频道 设置 <guild_id> <channel_id> [auto|plugin|official|off]")
        if not await _skill_allowed(rt, bot, event, "guild.config", args=rest[:160]):
            return
        try:
            config = await rt.guild.set_config(
                guild_id=parts[0],
                channel_id=parts[1],
                backend=parts[2] if len(parts) > 2 else None,
            )
        except GuildError as exc:
            await _skill_failed(rt, bot, event, "guild.config", exc)
            return
        await _audit_skill(rt, event, "guild.config", status="ok", args=rest[:160])
        await guild_cmd.finish(
            f"频道配置好了：{config.backend} · guild={config.guild_id} channel={config.channel_id}"
        )
    if action in {"发送", "send"}:
        parts = rest.split(maxsplit=1)
        channel_id = ""
        content = rest
        if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) >= 5:
            channel_id, content = parts[0], parts[1]
        if not content.strip():
            await guild_cmd.finish("用法：/频道 发送 [channel_id] <内容>")
        if not await _skill_allowed(rt, bot, event, "guild.send", args=content[:160]):
            return
        try:
            result = await rt.guild.send(bot, content, channel_id=channel_id or None)
        except GuildError as exc:
            await _skill_failed(rt, bot, event, "guild.send", exc)
            return
        await _audit_skill(rt, event, "guild.send", status="ok", args=content[:160])
        await guild_cmd.finish(
            f"频道发言成功（{result['backend']}）"
            + (f"\nmessage_id={result['message_id']}" if result.get("message_id") else "")
        )
    if action in {"历史", "history"}:
        limit = 20
        if rest.isdigit():
            limit = max(1, min(int(rest), 50))
        if not await _skill_allowed(rt, bot, event, "guild.history", args=str(limit)):
            return
        try:
            data = await rt.guild.history(bot, limit=limit)
        except GuildError as exc:
            await _skill_failed(rt, bot, event, "guild.history", exc)
            return
        messages = data.get("messages") or []
        if not messages:
            await guild_cmd.finish("频道最近没有拉到消息。")
        lines = []
        for item in messages[:limit]:
            if not isinstance(item, dict):
                continue
            author = item.get("author") or {}
            name = author.get("username") if isinstance(author, dict) else ""
            content = str(item.get("content") or "").replace("\n", " ")[:100]
            lines.append(f"{name or item.get('author_id') or ''}：{content}")
        await _audit_skill(rt, event, "guild.history", status="ok")
        await guild_cmd.finish("频道最近消息：\n" + "\n".join(lines))
    await guild_cmd.finish(
        "用法：/频道 [状态|列表|设置 <guild_id> <channel_id> [后端]|发送 [channel_id] <内容>|历史 [条数]]"
    )


@avatar_cmd.handle()
async def _avatar(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    url = _event_image_url(event)
    if url is None and raw.startswith(("http://", "https://")):
        url = raw
    if url is None:
        await avatar_cmd.finish("发一张图片并配 /头像，或回复一张图片发 /头像。")
    if not await _skill_allowed(rt, bot, event, "account.avatar", args="image"):
        return
    try:
        await rt.account.set_avatar(bot, url)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.avatar", exc, args="image")
        return
    await _audit_skill(rt, event, "account.avatar", status="ok", args="image")
    await avatar_cmd.finish("头像换了。")


@nickname_cmd.handle()
async def _set_nickname(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if not raw:
        await nickname_cmd.finish("用法：/昵称 <名字>")
    if not await _skill_allowed(rt, bot, event, "account.nickname", args=raw):
        return
    try:
        value = await rt.account.set_nickname(bot, raw)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.nickname", exc, args=raw)
        return
    await _audit_skill(rt, event, "account.nickname", status="ok", args=value)
    await nickname_cmd.finish(f"昵称改成 {value} 了。")


@signature_cmd.handle()
async def _signature(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if not raw:
        await signature_cmd.finish("用法：/签名 <签名>")
    if not await _skill_allowed(rt, bot, event, "account.signature", args=raw):
        return
    try:
        value = await rt.account.set_signature(bot, raw)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.signature", exc, args=raw)
        return
    await _audit_skill(rt, event, "account.signature", status="ok", args=value)
    await signature_cmd.finish(f"签名改成：{value}")


@online_cmd.handle()
async def _online(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    if not raw:
        await online_cmd.finish("用法：/在线 在线|离开|隐身|忙碌|Q我吧|请勿打扰")
    if not await _skill_allowed(rt, bot, event, "account.status", args=raw):
        return
    try:
        code = await rt.account.set_status(bot, raw)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.status", exc, args=raw)
        return
    await _audit_skill(rt, event, "account.status", status="ok", args=str(code))
    await online_cmd.finish(f"在线状态设置了：{raw}（{code}）")


@like_cmd.handle()
async def _like(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    raw = args.extract_plain_text().strip()
    target, rest = target_from_args(raw, explicit=target_from_event(event))
    if not target:
        await like_cmd.finish("用法：/点赞 @人 [次数]，或回复一条消息发 /点赞。")
    times = int(rest) if rest.isdigit() else 10
    if not await _skill_allowed(rt, bot, event, "account.like", target_id=target, args=raw):
        return
    try:
        count = await rt.account.send_like(bot, target, times)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.like", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "account.like", status="ok", target_id=target, args=str(count))
    await like_cmd.finish(f"给 {target} 点了 {count} 个赞。")


@poke_cmd.handle()
async def _poke(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await poke_cmd.finish("这个只能在群里用。")
    raw = args.extract_plain_text().strip()
    target, _rest = target_from_args(raw, explicit=target_from_event(group))
    if not target:
        await poke_cmd.finish("用法：/戳 @人，或回复一条消息发 /戳。")
    if str(target) == str(event.self_id):
        await poke_cmd.finish("不能戳我自己。")
    if not await _skill_allowed(rt, bot, event, "account.poke", target_id=target, args=raw):
        return
    try:
        await rt.account.poke(bot, group.group_id, target)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "account.poke", exc, target_id=target, args=raw)
        return
    await _audit_skill(rt, event, "account.poke", status="ok", target_id=target)
    await poke_cmd.finish(f"戳了 {target}。")


@group_portrait_cmd.handle()
async def _group_portrait(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await group_portrait_cmd.finish("这个只能在群里用。")
    raw = args.extract_plain_text().strip()
    url = _event_image_url(event)
    if url is None and raw.startswith(("http://", "https://")):
        url = raw
    if url is None:
        await group_portrait_cmd.finish("发一张图片并配 /群头像，或回复一张图片发 /群头像。")
    if not await _skill_allowed(rt, bot, event, "group.portrait", args="image"):
        return
    try:
        await rt.group_admin.require_admin(bot, group)
        await rt.account.set_group_portrait(bot, group.group_id, url)
    except (GroupActionError, AccountError) as exc:
        await _skill_failed(rt, bot, event, "group.portrait", exc, args="image")
        return
    await _audit_skill(rt, event, "group.portrait", status="ok", args="image")
    await group_portrait_cmd.finish("群头像换了。")


@group_title_cmd.handle()
async def _group_title(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await group_title_cmd.finish("这个只能在群里用。")
    raw = args.extract_plain_text().strip()
    target, rest = target_from_args(raw, explicit=target_from_event(group))
    if not target:
        await group_title_cmd.finish("用法：/群头衔 @人 <头衔>；留空头衔就是取消。")
    if not await _skill_allowed(rt, bot, event, "group.special_title", target_id=target, args=raw):
        return
    try:
        await rt.group_admin.require_admin(bot, group)
        value = await rt.account.set_special_title(bot, group.group_id, target, rest)
    except (GroupActionError, AccountError) as exc:
        await _skill_failed(rt, bot, event, "group.special_title", exc, target_id=target, args=raw)
        return
    await _audit_skill(
        rt, event, "group.special_title", status="ok", target_id=target, args=value
    )
    await group_title_cmd.finish(f"{target} 的头衔：{value or '已取消'}")


@group_sign_cmd.handle()
async def _group_sign(bot: Bot, event: MessageEvent) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    if group is None:
        await group_sign_cmd.finish("这个只能在群里用。")
    if not await _skill_allowed(rt, bot, event, "group.sign"):
        return
    try:
        await rt.account.group_sign(bot, group.group_id)
    except AccountError as exc:
        await _skill_failed(rt, bot, event, "group.sign", exc)
        return
    await _audit_skill(rt, event, "group.sign", status="ok")
    await group_sign_cmd.finish("群打卡完成。")


async def _essence_action(
    bot: Bot,
    event: MessageEvent,
    *,
    remove: bool,
) -> None:
    rt = _get_runtime()
    group = _require_group(event)
    command = unessence_cmd if remove else essence_cmd
    if group is None:
        await command.finish("这个只能在群里用。")
    message_id = _reply_message_id(group)
    if message_id is None:
        await command.finish("先回复一条消息，再发这个命令。")
    if not await _skill_allowed(
        rt, bot, event, "group.essence", target_id=message_id
    ):
        return
    try:
        await rt.group_admin.require_admin(bot, group)
        if remove:
            await rt.account.remove_essence(bot, message_id)
        else:
            await rt.account.set_essence(bot, message_id)
    except (GroupActionError, AccountError) as exc:
        await _skill_failed(rt, bot, event, "group.essence", exc, target_id=message_id)
        return
    await _audit_skill(
        rt, event, "group.essence", status="ok", target_id=message_id, args="remove" if remove else "set"
    )
    await command.finish("已取消精华。" if remove else "设成精华了。")


@essence_cmd.handle()
async def _essence(bot: Bot, event: MessageEvent) -> None:
    await _essence_action(bot, event, remove=False)


@unessence_cmd.handle()
async def _unessence(bot: Bot, event: MessageEvent) -> None:
    await _essence_action(bot, event, remove=True)


@on_message(priority=10, block=False).handle()
async def _chat(bot: Bot, event: MessageEvent) -> None:
    rt = runtime
    if rt is None:
        logger.warning("Received a message before runtime startup; ignored")
        return
    if isinstance(event, GroupMessageEvent):
        await _handle_group(rt, bot, event)
    elif isinstance(event, PrivateMessageEvent):
        await _handle_private(rt, bot, event)

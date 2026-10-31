from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot

from .brain import BrainService
from .composer import sanitize_text
from .db import Database
from .llm import DeepSeekClient
from .persona import Persona
from .policy import PolicyViolation, check_public_text
from .settings import Settings

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
_PROFILE_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_·\-]{1,16}$")


class PresenceError(RuntimeError):
    pass


@dataclass(slots=True)
class SelfProfile:
    nickname: str
    signature: str
    bio: str
    tags: list[str] = field(default_factory=list)
    mood: str = ""
    source: str = "manual"
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "nickname": self.nickname,
            "signature": self.signature,
            "bio": self.bio,
            "tags": list(self.tags),
            "mood": self.mood,
            "source": self.source,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SelfProfile:
        tags = raw.get("tags") or []
        return cls(
            nickname=str(raw.get("nickname") or ""),
            signature=str(raw.get("signature") or ""),
            bio=str(raw.get("bio") or ""),
            tags=[str(item) for item in tags] if isinstance(tags, list) else [],
            mood=str(raw.get("mood") or ""),
            source=str(raw.get("source") or "manual"),
            updated_at=str(raw.get("updated_at") or ""),
        )


class PresenceService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        brain: BrainService,
        persona: Persona,
        llm: DeepSeekClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.brain = brain
        self.persona = persona
        self.llm = llm
        self.root = brain.profile_dir
        self.current_path = self.root / "current.json"
        self.history_dir = self.root / "history"
        self.current: SelfProfile | None = None
        self.error_handler: Callable[[str, str], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        self.ensure_dirs()
        if not self.current_path.is_file():
            return
        try:
            raw = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read current profile: %s", self.current_path)
            return
        if isinstance(raw, dict):
            self.current = SelfProfile.from_dict(raw)

    @property
    def nickname(self) -> str:
        return self.current.nickname if self.current else ""

    def _record_error(self, source: str, message: str) -> None:
        if self.error_handler:
            self.error_handler(source, message)

    def _validate(self, profile: SelfProfile, *, source: str = "auto") -> SelfProfile:
        nickname = profile.nickname.strip()
        signature = sanitize_text(profile.signature, self.persona.display_name)[:80].strip()
        bio = sanitize_text(profile.bio, self.persona.display_name)[:300].strip()
        mood = sanitize_text(profile.mood, self.persona.display_name)[:60].strip()
        tags = [tag.strip()[:12] for tag in profile.tags if tag.strip()][:5]
        if not _PROFILE_NAME_RE.fullmatch(nickname):
            raise PresenceError("昵称需要 1–16 个中文、字母、数字或 ·_-")
        if not signature:
            raise PresenceError("签名不能为空")
        if not bio:
            raise PresenceError("简介不能为空")
        try:
            check_public_text(signature, max_chars=80)
            check_public_text(bio, max_chars=300)
            if mood:
                check_public_text(mood, max_chars=60)
        except PolicyViolation as exc:
            raise PresenceError(str(exc)) from exc
        return SelfProfile(
            nickname=nickname,
            signature=signature,
            bio=bio,
            tags=tags,
            mood=mood,
            source=source,
            updated_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        )

    def _write_current(self, profile: SelfProfile) -> None:
        self.ensure_dirs()
        temp = self.current_path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(profile.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.current_path)
        self.current = profile

    def _archive_current(self) -> None:
        if self.current is None:
            return
        self.ensure_dirs()
        stamp = datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S-%f")
        path = self.history_dir / f"{stamp}.json"
        path.write_text(
            json.dumps(self.current.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        versions = sorted(self.history_dir.glob("*.json"), key=lambda item: item.name, reverse=True)
        for old in versions[20:]:
            old.unlink(missing_ok=True)

    async def generate_profile(self) -> SelfProfile:
        notes = await self.db.list_global_brain_notes(limit=8)
        note_text = "\n".join(f"- {note.title}：{note.content[:300]}" for note in notes)
        try:
            raw, _result = await self.llm.extract_profile(
                self.persona,
                note_text,
                self.current.as_dict() if self.current else None,
            )
        except Exception as exc:
            raise PresenceError(f"生成主页失败：{exc}") from exc
        profile = SelfProfile.from_dict(raw)
        profile.source = "auto"
        return self._validate(profile, source="auto")

    async def save_draft(self, profile: SelfProfile) -> SelfProfile:
        async with self._lock:
            checked = self._validate(profile, source=profile.source or "manual")
            self._write_current(checked)
            return checked

    async def apply_profile(self, bot: Bot | None = None, profile: SelfProfile | None = None) -> SelfProfile:
        target = profile or self.current
        if target is None:
            raise PresenceError("还没有可应用的主页资料")
        target = self._validate(target, source=target.source or "manual")
        active_bot = bot or self._first_bot()
        if active_bot is None:
            raise PresenceError("NapCat 没有连接")
        async with self._lock:
            try:
                await asyncio.wait_for(
                    active_bot.call_api(
                        "set_qq_profile",
                        nickname=target.nickname,
                        personal_note=target.signature,
                    ),
                    timeout=30.0,
                )
            except Exception as exc:
                self._record_error("profile", str(exc))
                raise PresenceError(f"设置 QQ 资料失败：{exc}") from exc
            self._archive_current()
            self._write_current(target)
            await self.db.add_skill_audit(
                "profile.apply",
                scope_type="private",
                scope_id="self",
                actor_id="auto" if target.source == "auto" else "manual",
                target_id=None,
                args=f"nickname={target.nickname}",
                status="ok",
            )
            return target

    async def optimize_and_apply(self, bot: Bot | None = None) -> SelfProfile:
        profile = await self.generate_profile()
        return await self.apply_profile(bot, profile)

    def list_history(self) -> list[dict[str, Any]]:
        self.ensure_dirs()
        items: list[dict[str, Any]] = []
        for path in sorted(self.history_dir.glob("*.json"), key=lambda item: item.name, reverse=True)[:20]:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append({"version": path.name, "profile": raw})
        return items

    async def restore_history(self, version: str, bot: Bot | None = None) -> SelfProfile:
        name = Path(version).name
        if name != version or not name.endswith(".json"):
            raise PresenceError("历史版本名不合法")
        path = self.history_dir / name
        if not path.is_file():
            raise PresenceError("这个主页版本不存在")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PresenceError("主页历史版本损坏") from exc
        profile = SelfProfile.from_dict(raw)
        profile.source = "manual"
        return await self.apply_profile(bot, profile)

    async def generate_qzone_post(self) -> str:
        notes = await self.db.list_global_brain_notes(limit=8)
        note_text = "\n".join(f"- {note.title}：{note.content[:240]}" for note in notes)
        old_profile = self.current.as_dict() if self.current else None
        try:
            text, _result = await self.llm.compose_qzone_post(
                self.persona,
                old_profile,
                note_text,
            )
        except Exception as exc:
            raise PresenceError(f"生成空间动态失败：{exc}") from exc
        value = sanitize_text(text, self.persona.display_name)
        value = check_public_text(value, max_chars=self.settings.qzone_max_chars)
        recent = await self.db.list_social_posts(limit=3)
        normalized = re.sub(r"\s+", "", value)
        if any(re.sub(r"\s+", "", post.content) == normalized for post in recent):
            raise PresenceError("这条动态和最近发过的太像了")
        return value

    async def post_qzone(
        self,
        *,
        bot: Bot | None = None,
        content: str | None = None,
        images: list[str] | None = None,
        source: str = "manual",
        ugc_right: int | None = None,
    ) -> dict[str, Any]:
        text = content.strip() if content else await self.generate_qzone_post()
        text = sanitize_text(text, self.persona.display_name)
        text = check_public_text(text, max_chars=self.settings.qzone_max_chars)
        image_list = self._validate_qzone_images(images or [])
        try:
            right = int(ugc_right) if ugc_right is not None else int(self.settings.qzone_ugc_right)
        except (TypeError, ValueError):
            right = int(self.settings.qzone_ugc_right)
        if right not in {1, 4, 64}:
            right = int(self.settings.qzone_ugc_right)
        active_bot = bot or self._first_bot()
        if active_bot is None:
            raise PresenceError("NapCat 没有连接")
        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    active_bot.call_api(
                        "send_qzone_msg",
                        content=text,
                        images=image_list,
                        ugc_right=right,
                        target_uins=[],
                    ),
                    timeout=45.0,
                )
            except Exception as exc:
                self._record_error("qzone", str(exc))
                await self.db.add_skill_audit(
                    "qzone.post",
                    scope_type="private",
                    scope_id="self",
                    actor_id="auto" if source == "auto" else "manual",
                    target_id=None,
                    args="",
                    status="failed",
                    error=str(exc),
                )
                raise PresenceError(f"发布空间动态失败：{exc}") from exc
            tid = str((result or {}).get("tid", "")).strip() if isinstance(result, dict) else ""
            if not tid:
                raise PresenceError("空间接口没有返回 tid，可能在风控或未登录")
            await self.db.save_social_post(
                tid=tid,
                content=text,
                ugc_right=right,
                source=source,
            )
            await self.db.add_skill_audit(
                "qzone.post",
                scope_type="private",
                scope_id="self",
                actor_id="auto" if source == "auto" else "manual",
                target_id=tid,
                args="",
                status="ok",
            )
            return {"tid": tid, "content": text, "images": image_list, "ugc_right": right}

    @staticmethod
    def _validate_qzone_images(images: list[str]) -> list[str]:
        if not isinstance(images, list):
            raise PresenceError("图片参数格式不对")
        result: list[str] = []
        for raw in images[:9]:
            value = str(raw or "").strip()
            if not value:
                continue
            if value.startswith(("http://", "https://")):
                if len(value) > 2000:
                    raise PresenceError("图片链接太长")
                result.append(value)
                continue
            if value.startswith("base64://"):
                if len(value) > 2_500_000:
                    raise PresenceError("图片太大，最多约 2MB")
                result.append(value)
                continue
            raise PresenceError("空间图片只支持 http(s) 链接或 base64://")
        return result

    async def delete_qzone(self, tid: str, bot: Bot | None = None) -> None:
        active_bot = bot or self._first_bot()
        if active_bot is None:
            raise PresenceError("NapCat 没有连接")
        post = await self.db.get_social_post(tid)
        if post is None:
            raise PresenceError("没有这条空间动态记录")
        try:
            await asyncio.wait_for(
                active_bot.call_api("delete_qzone_msg", tid=tid),
                timeout=30.0,
            )
        except Exception as exc:
            self._record_error("qzone", str(exc))
            raise PresenceError(f"删除空间动态失败：{exc}") from exc
        await self.db.mark_social_post_deleted(tid)
        await self.db.add_skill_audit(
            "qzone.delete",
            scope_type="private",
            scope_id="self",
            actor_id="manual",
            target_id=tid,
            args="",
            status="ok",
        )

    async def status(self) -> dict[str, Any]:
        profile = self.current.as_dict() if self.current else None
        return {
            "profile": profile,
            "posts": [
                {
                    "tid": post.tid,
                    "content": post.content,
                    "source": post.source,
                    "status": post.status,
                    "created_at": post.created_at,
                    "deleted_at": post.deleted_at,
                }
                for post in await self.db.list_social_posts(limit=30)
            ],
            "next": {
                "profile": await self.db.get_social_state("profile_next_due"),
                "qzone": await self.db.get_social_state("qzone_next_due"),
            },
        }

    def _first_bot(self) -> Bot | None:
        bots = get_bots()
        return next(iter(bots.values()), None) if bots else None

    async def start(self) -> None:
        self.load()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._auto_loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(CN_TZ)

    def _quiet(self) -> bool:
        hour = self._now().hour
        start = int(self.settings.qzone_quiet_start)
        end = int(self.settings.qzone_quiet_end)
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _next_due(self, key: str, *, first_delay_minutes: int) -> datetime:
        raw = await self.db.get_social_state(key)
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        due = (self._now() + timedelta(minutes=first_delay_minutes)).replace(microsecond=0)
        await self.db.set_social_state(key, due.isoformat(timespec="seconds"))
        return due

    async def _auto_loop(self) -> None:
        await asyncio.sleep(5)
        profile_due = await self._next_due(
            "profile_next_due",
            first_delay_minutes=self.settings.profile_first_delay_minutes,
        )
        qzone_due = await self._next_due(
            "qzone_next_due",
            first_delay_minutes=self.settings.qzone_first_delay_minutes,
        )
        while True:
            try:
                await asyncio.sleep(300)
                if not self.settings.social_auto_enabled:
                    continue
                now = self._now()
                if self.settings.profile_auto_enabled and now >= profile_due:
                    try:
                        generated = await self.generate_profile()
                        if self.settings.profile_auto_apply:
                            await self.apply_profile(profile=generated)
                        else:
                            await self.save_draft(generated)
                        profile_due = now + timedelta(days=self.settings.profile_update_days)
                        await self.db.set_social_state(
                            "profile_next_due", profile_due.isoformat(timespec="seconds")
                        )
                    except Exception as exc:
                        self._record_error("profile-auto", str(exc))
                        profile_due = now + timedelta(hours=1)
                        await self.db.set_social_state(
                            "profile_next_due", profile_due.isoformat(timespec="seconds")
                        )
                if self.settings.qzone_auto_enabled and now >= qzone_due:
                    if self._quiet():
                        continue
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    count = await self.db.count_social_posts_since(
                        day_start.astimezone(UTC).isoformat(timespec="seconds")
                    )
                    if count >= self.settings.qzone_max_per_day:
                        qzone_due = now + timedelta(hours=self.settings.qzone_interval_hours)
                    else:
                        try:
                            await self.post_qzone(source="auto")
                            qzone_due = now + timedelta(hours=self.settings.qzone_interval_hours)
                        except Exception as exc:
                            self._record_error("qzone-auto", str(exc))
                            qzone_due = now + timedelta(hours=6)
                    await self.db.set_social_state(
                        "qzone_next_due", qzone_due.isoformat(timespec="seconds")
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Presence auto loop failed")
                await asyncio.sleep(60)

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .brain import BrainError
from .composer import sanitize_text
from .guild import GuildError
from .llm import LLMError
from .models import BrainNote, StickerRecord, StyleProfile
from .persona import Persona, PersonaError, load_persona
from .presence import PresenceError, SelfProfile
from .qzone import QzoneError
from .skills import ROLE_LABELS, ROLE_RANK, SkillDenied
from .stickers import normalize_description, normalize_tags

if TYPE_CHECKING:
    from .plugins.chat import Runtime

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
ADMIN_HTML = Path(__file__).with_name("admin.html")
GROUP_ID_RE = re.compile(r"^\d{5,20}$")
STYLE_SCOPE_RE = re.compile(r"^\d{5,20}$")
PERSONA_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
MAX_PERSONA_BYTES = 64_000
PERSONA_HISTORY_LIMIT = 30


class PersonaPayload(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_PERSONA_BYTES)


class GroupPayload(BaseModel):
    active: bool | None = None
    mode: Literal["smart", "full"] | None = None


class TestChatPayload(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class StickerUploadPayload(BaseModel):
    filename: str = Field(default="upload", max_length=200)
    data: str = Field(min_length=1, max_length=8_000_000)
    tags: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=200)


class StickerPatchPayload(BaseModel):
    tags: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


class StylePatchPayload(BaseModel):
    enabled: bool | None = None


class SkillPatchPayload(BaseModel):
    enabled: bool
    min_role: Literal["member", "admin", "group_owner", "owner"]


class SkillGrantPayload(SkillPatchPayload):
    pass


class AutonomyPayload(BaseModel):
    enabled: bool
    directive: str = Field(default="", max_length=600)
    mode: Literal["normal", "explore"] = "normal"


class ExplorePayload(BaseModel):
    enabled: bool


class ExploreRunPayload(BaseModel):
    group_id: str = Field(default="", max_length=20)


class BrainCreatePayload(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=100_000)
    tags: str = Field(default="", max_length=200)
    scope_type: Literal["global", "group", "private"] = "global"
    scope_id: str = Field(default="", max_length=30)
    visibility: Literal["scope", "global", "owner"] = "scope"


class BrainPatchPayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    tags: str | None = Field(default=None, max_length=200)
    visibility: Literal["scope", "global", "owner"] | None = None


class ProfileDraftPayload(BaseModel):
    nickname: str = Field(min_length=1, max_length=16)
    signature: str = Field(min_length=1, max_length=80)
    bio: str = Field(min_length=1, max_length=300)
    tags: str = Field(default="", max_length=100)
    mood: str = Field(default="", max_length=60)
    apply: bool = True


class ProfileRestorePayload(BaseModel):
    version: str = Field(min_length=1, max_length=80)


class QzonePostPayload(BaseModel):
    content: str = Field(default="", max_length=1000)
    images: list[str] = Field(default_factory=list, max_length=9)
    ugc_right: int | None = Field(default=None)


class ActionRunPayload(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class StyleReviewPayload(BaseModel):
    scope_type: Literal["group", "private"] = "group"
    scope_id: str = Field(min_length=1, max_length=32)


class StyleConfigPayload(BaseModel):
    deai_enabled: bool | None = None
    deai_record_only: bool | None = None
    deai_light_threshold: int | None = Field(default=None, ge=0, le=100)
    deai_heavy_threshold: int | None = Field(default=None, ge=0, le=100)
    feedback_enabled: bool | None = None
    selfreview_enabled: bool | None = None
    style_evolution_brain: bool | None = None


class QzoneLikePayload(BaseModel):
    tid: str = Field(min_length=1, max_length=100)
    active: bool = True
    unikey: str = Field(default="", max_length=200)


class QzoneCommentPayload(BaseModel):
    tid: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=200)


class QzoneCommentDeletePayload(BaseModel):
    tid: str = Field(min_length=1, max_length=100)
    comment_id: str = Field(min_length=1, max_length=100)


class GuildConfigPayload(BaseModel):
    backend: Literal["auto", "plugin", "official", "off"] | None = None
    guild_id: str | None = Field(default=None, max_length=30)
    channel_id: str | None = Field(default=None, max_length=30)


class GuildSendPayload(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    channel_id: str = Field(default="", max_length=30)


def _brain_payload(note: BrainNote, *, full: bool = False) -> dict[str, Any]:
    content = note.content if full else note.content.replace("\n", " ")[:500]
    return {
        "id": note.id,
        "title": note.title,
        "content": content,
        "tags": note.tags,
        "scope_type": note.scope_type,
        "scope_id": note.scope_id,
        "visibility": note.visibility,
        "source": note.source,
        "source_user_id": note.source_user_id,
        "byte_size": note.byte_size,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def _now() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _restart_process() -> None:
    root = Path(__file__).resolve().parent.parent
    os.execv(sys.executable, [sys.executable, str(root / "bot.py")])


def _today_start_utc() -> str:
    start = datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def _check_token(runtime: Runtime, token: str | None) -> None:
    expected = str(getattr(runtime.settings, "admin_token", "") or "")
    if not expected:
        raise HTTPException(status_code=503, detail="控制台未启用：请在 .env 设置 ADMIN_TOKEN")
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="token 不正确")


async def _one(runtime: Runtime, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cursor = await runtime.db.conn.execute(sql, params)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _all(runtime: Runtime, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    cursor = await runtime.db.conn.execute(sql, params)
    rows = await cursor.fetchall()
    await cursor.close()
    return list(rows)


def _validate_group_id(group_id: str) -> None:
    if not GROUP_ID_RE.fullmatch(group_id):
        raise HTTPException(status_code=400, detail="群号格式不对")


def _validate_style_scope(scope_type: str, scope_id: str) -> None:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="范围只能是 group 或 private")
    if not STYLE_SCOPE_RE.fullmatch(scope_id):
        raise HTTPException(status_code=400, detail="范围 ID 格式不对")


def _validate_skill_scope(scope_type: str, scope_id: str) -> None:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="范围只能是 group 或 private")
    if not STYLE_SCOPE_RE.fullmatch(scope_id):
        raise HTTPException(status_code=400, detail="范围 ID 格式不对")


def _validate_brain_scope(scope_type: str, scope_id: str) -> None:
    if scope_type not in {"global", "group", "private"}:
        raise HTTPException(status_code=400, detail="大脑范围不合法")
    if scope_type == "global":
        return
    if not GROUP_ID_RE.fullmatch(scope_id):
        raise HTTPException(status_code=400, detail="大脑范围 ID 格式不对")


def _parse_usage_day(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(CN_TZ).date().isoformat()


async def _usage_snapshot(runtime: Runtime, days: int = 7) -> dict[str, Any]:
    days = max(1, min(int(days), 30))
    today = datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = today - timedelta(days=days - 1)
    rows = await _all(
        runtime,
        """
        SELECT scope_type, model, input_tokens, output_tokens, created_at
        FROM usage_logs WHERE created_at >= ?
        ORDER BY created_at ASC
        """,
        (first_day.astimezone(UTC).isoformat(timespec="seconds"),),
    )
    daily = {
        (first_day + timedelta(days=index)).date().isoformat(): {
            "date": (first_day + timedelta(days=index)).date().isoformat(),
            "input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
        }
        for index in range(days)
    }
    by_source: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    today_totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    for row in rows:
        source = str(row["scope_type"] or "other")
        model = str(row["model"] or "unknown")
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        totals["calls"] += 1
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        day = _parse_usage_day(str(row["created_at"] or ""))
        if day in daily:
            daily[day]["calls"] += 1
            daily[day]["input_tokens"] += input_tokens
            daily[day]["output_tokens"] += output_tokens
        for bucket, key in ((by_source, source), (by_model, model)):
            item = bucket.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            item["calls"] += 1
            item["input_tokens"] += input_tokens
            item["output_tokens"] += output_tokens
        if day == today.date().isoformat():
            today_totals["calls"] += 1
            today_totals["input_tokens"] += input_tokens
            today_totals["output_tokens"] += output_tokens
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    today_totals["total_tokens"] = today_totals["input_tokens"] + today_totals["output_tokens"]
    budget = await runtime.budget.status()
    return {
        "days": days,
        "totals": totals,
        "today": today_totals,
        "daily": list(daily.values()),
        "by_source": [{"source": key, **value} for key, value in sorted(by_source.items())],
        "by_model": [{"model": key, **value} for key, value in sorted(by_model.items())],
        "budget": budget.as_dict(),
        "generated_at": _now(),
    }


def _persona_history_dir(runtime: Runtime) -> Path:
    return Path(runtime.settings.data_dir) / "persona_history"


def _backup_persona(path: Path, history_dir: Path) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S-%f")
    target = history_dir / f"{path.stem}-{stamp}{path.suffix or '.yaml'}"
    target.write_bytes(path.read_bytes())
    versions = sorted(history_dir.glob("*.y*ml"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in versions[PERSONA_HISTORY_LIMIT:]:
        old.unlink(missing_ok=True)
    return target


def _save_persona_file(path: Path, content: str, history_dir: Path | None = None) -> Persona:
    tmp = path.with_name(f".{path.name}.upload.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        persona = load_persona(tmp)
        if history_dir is not None and path.is_file():
            _backup_persona(path, history_dir)
        os.replace(tmp, path)
        return persona
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _list_persona_versions(history_dir: Path) -> list[dict[str, Any]]:
    if not history_dir.is_dir():
        return []
    files = [item for item in history_dir.glob("*.y*ml") if item.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "name": item.name,
            "size": item.stat().st_size,
            "modified": datetime.fromtimestamp(item.stat().st_mtime, CN_TZ).isoformat(timespec="seconds"),
        }
        for item in files[:50]
    ]


def _sticker_payload(record: StickerRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tags": record.tags,
        "description": record.description,
        "mime": record.mime,
        "byte_size": record.byte_size,
        "width": record.width,
        "height": record.height,
        "enabled": record.enabled,
        "source_scope_type": record.source_scope_type,
        "source_scope_id": record.source_scope_id,
        "source_nickname": record.source_nickname,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "use_count": record.use_count,
    }


def _decode_upload(data: str) -> bytes:
    raw = data.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="图片数据不是有效的 base64") from exc


def _style_payload(record: StyleProfile) -> dict[str, Any]:
    return {
        "id": record.id,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "summary": record.summary,
        "profile": record.profile,
        "sample_count": record.sample_count,
        "confidence": round(record.confidence, 3),
        "enabled": record.enabled,
        "version": record.version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _persona_payload(runtime: Runtime) -> dict[str, Any]:
    persona = runtime.persona
    return {
        "id": persona.id,
        "display_name": persona.display_name,
        "aliases": list(persona.aliases),
        "intensity": persona.intensity,
        "identity": persona.identity,
        "traits": list(persona.traits),
    }


def register_admin(runtime: Runtime) -> None:
    """Attach the local control panel to the existing FastAPI app."""
    from nonebot import get_app, get_bots

    app = get_app()
    if getattr(app.state, "admin_registered", False):
        app.state.admin_runtime = runtime
        return

    app.state.admin_registered = True
    app.state.admin_runtime = runtime
    app.state.admin_started_at = time.monotonic()
    app.state.admin_started_iso = _now()

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> HTMLResponse:
        try:
            html = await asyncio.to_thread(ADMIN_HTML.read_text, encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail="控制台页面文件缺失") from exc
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/status")
    async def admin_status(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        bots = get_bots()
        accounts = sorted({str(getattr(bot, "self_id", "")) for bot in bots.values()})

        messages = await _one(runtime, "SELECT COUNT(*) AS n FROM messages")
        users = await _one(runtime, "SELECT COUNT(*) AS n FROM users")
        groups = await _one(runtime, "SELECT COUNT(*) AS n FROM chat_groups")
        active_groups = await _one(runtime, "SELECT COUNT(*) AS n FROM chat_groups WHERE active = 1")
        memories = await _one(runtime, "SELECT COUNT(*) AS n FROM memories")
        sticker_count = await runtime.db.count_stickers()
        sticker_enabled = await runtime.db.count_stickers(enabled_only=True)
        style_count = await runtime.db.count_style_profiles()
        style_enabled = await runtime.db.count_style_profiles(enabled_only=True)
        usage = await _one(
            runtime,
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM usage_logs
            """,
        )
        today = await _one(
            runtime,
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM usage_logs
            WHERE created_at >= ?
            """,
            (_today_start_utc(),),
        )
        group_rows = await _all(
            runtime,
            """
            SELECT group_id, active, mode, last_bot_reply_at, updated_at
            FROM chat_groups
            ORDER BY updated_at DESC
            LIMIT 100
            """,
        )

        input_tokens = int(usage["input_tokens"] or 0)
        output_tokens = int(usage["output_tokens"] or 0)
        today_input = int(today["input_tokens"] or 0)
        today_output = int(today["output_tokens"] or 0)
        budget = await runtime.budget.status()
        usage_7d = await _usage_snapshot(runtime, 7)
        brain_stats = await runtime.brain.storage_stats()
        skill_items = await runtime.skills.list_effective()
        qzone_count = await runtime.db.count_social_posts_since("1970-01-01T00:00:00+00:00")
        return {
            "now": _now(),
            "bot": {
                "connected": bool(accounts),
                "accounts": accounts,
                "uptime_seconds": int(time.monotonic() - app.state.admin_started_at),
                "started_at": app.state.admin_started_iso,
            },
            "model": {
                "provider": runtime.settings.llm_provider,
                "provider_name": runtime.settings.llm_provider_name,
                "name": runtime.settings.llm_model,
                "base_url": runtime.settings.llm_base_url,
                "thinking": bool(runtime.settings.thinking),
            },
            "persona": _persona_payload(runtime),
            "budget": budget.as_dict(),
            "stickers": {"count": sticker_count, "enabled": sticker_enabled},
            "styles": {"profiles": style_count, "enabled": style_enabled},
            "brain": {
                **brain_stats,
                "enabled": bool(runtime.settings.brain_enabled),
                "capture": bool(runtime.settings.brain_capture_enabled),
            },
            "skills": {
                "count": len(skill_items),
                "enabled": sum(1 for item in skill_items if item["enabled"]),
            },
            "social": {
                "profile": runtime.presence.current.as_dict() if runtime.presence.current else None,
                "qzone_posts": qzone_count,
                "profile_auto": bool(runtime.settings.profile_auto_enabled),
                "qzone_auto": bool(runtime.settings.qzone_auto_enabled),
                "qzone_web": bool(runtime.settings.qzone_web_enabled),
                "guild_backend": (await runtime.guild.config()).backend,
            },
            "usage_7d": usage_7d,
            "stats": {
                "messages": int(messages["n"] or 0),
                "users": int(users["n"] or 0),
                "groups": int(groups["n"] or 0),
                "active_groups": int(active_groups["n"] or 0),
                "memories": int(memories["n"] or 0),
                "usage": {
                    "calls": int(usage["calls"] or 0),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "today_tokens": today_input + today_output,
                },
            },
            "groups": [
                {
                    "group_id": str(row["group_id"]),
                    "active": bool(row["active"]),
                    "mode": str(row["mode"]),
                    "last_bot_reply_at": row["last_bot_reply_at"],
                    "updated_at": row["updated_at"],
                }
                for row in group_rows
            ],
        }

    @app.get("/admin/api/usage")
    async def admin_usage(
        days: int = 7,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return await _usage_snapshot(runtime, days)

    @app.get("/admin/api/autonomy")
    async def admin_autonomy_get(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        state = await runtime.autonomy.get_state()
        return {
            "enabled": state.enabled,
            "directive": state.directive,
            "mode": state.mode,
            "updated_at": state.updated_at,
            "hierarchy": ["控制台", "大肥鱼", "其他指令"],
            "note": "控制台最高；探索模式会全放行自主判断但仍守硬规则；指令不能违反法律。",
        }

    @app.put("/admin/api/autonomy")
    async def admin_autonomy_put(
        payload: AutonomyPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        state = await runtime.autonomy.set_state(
            enabled=payload.enabled,
            directive=payload.directive,
            mode=payload.mode,
        )
        await runtime.db.add_skill_audit(
            "autonomy.control",
            scope_type="console",
            scope_id="local",
            actor_id="console",
            target_id=None,
            args=(
                ("enabled" if payload.enabled else "disabled")
                + f" mode={payload.mode}"
                + f" directive={payload.directive[:200]}"
            ),
            status="ok",
        )
        return {"ok": True, "state": {
            "enabled": state.enabled,
            "directive": state.directive,
            "mode": state.mode,
            "updated_at": state.updated_at,
        }}

    @app.get("/admin/api/explore")
    async def admin_explore_get(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return await runtime.explore.status()

    @app.put("/admin/api/explore")
    async def admin_explore_put(
        payload: ExplorePayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        enabled = await runtime.explore.set_enabled(payload.enabled)
        await runtime.db.add_skill_audit(
            "explore.control",
            scope_type="console",
            scope_id="local",
            actor_id="console",
            target_id=None,
            args="enabled" if enabled else "disabled",
            status="ok",
        )
        state = await runtime.autonomy.get_state()
        return {
            "ok": True,
            "state": {
                "enabled": enabled,
                "mode": state.mode,
                "autonomy_enabled": state.enabled,
            },
        }

    @app.post("/admin/api/explore/run")
    async def admin_explore_run(
        payload: ExploreRunPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if payload.group_id:
            _validate_group_id(payload.group_id)
        bots = get_bots()
        bot = next(iter(bots.values()), None) if bots else None
        if bot is None:
            raise HTTPException(status_code=503, detail="NapCat 没有连接")
        try:
            findings = await runtime.explore.run(
                bot,
                group_id=payload.group_id or None,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"探索失败：{exc}") from exc
        return {"ok": True, "findings": findings}

    @app.get("/admin/api/skills/audit")
    async def admin_skill_audit(
        limit: int = 100,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return {"items": await runtime.db.list_skill_audit(limit=max(1, min(limit, 500)))}

    @app.get("/admin/api/skills/bot-role")
    async def admin_skill_bot_role(
        group_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_group_id(group_id)
        bots = get_bots()
        bot = next(iter(bots.values()), None) if bots else None
        if bot is None:
            raise HTTPException(status_code=503, detail="NapCat 没有连接")
        try:
            data = await bot.call_api(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(bot.self_id),
                no_cache=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"查询群身份失败：{exc}") from exc
        return {"group_id": group_id, "role": str((data or {}).get("role", "member"))}

    @app.get("/admin/api/skills")
    async def admin_skills(
        scope_type: str = "",
        scope_id: str = "",
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if scope_type or scope_id:
            _validate_skill_scope(scope_type, scope_id)
        items = await runtime.skills.list_effective(scope_type, scope_id)
        grants = (
            await runtime.db.list_skill_grants(scope_type, scope_id)
            if scope_type in {"group", "private"} and scope_id
            else []
        )
        return {
            "items": items,
            "grants": grants,
            "roles": [{"id": key, "label": label, "rank": ROLE_RANK[key]} for key, label in ROLE_LABELS.items()],
        }

    @app.patch("/admin/api/skills/{skill_id}")
    async def admin_skill_update(
        skill_id: str,
        payload: SkillPatchPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            item = await runtime.skills.set_global(
                skill_id,
                enabled=payload.enabled,
                min_role=payload.min_role,
            )
        except SkillDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "item": item}

    @app.put("/admin/api/skills/grants/{scope_type}/{scope_id}/{skill_id}")
    async def admin_skill_grant(
        scope_type: str,
        scope_id: str,
        skill_id: str,
        payload: SkillGrantPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_skill_scope(scope_type, scope_id)
        try:
            item = await runtime.skills.set_grant(
                scope_type,
                scope_id,
                skill_id,
                enabled=payload.enabled,
                min_role=payload.min_role,
            )
        except SkillDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "item": item}

    @app.delete("/admin/api/skills/grants/{scope_type}/{scope_id}/{skill_id}")
    async def admin_skill_grant_delete(
        scope_type: str,
        scope_id: str,
        skill_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_skill_scope(scope_type, scope_id)
        removed = await runtime.skills.clear_grant(scope_type, scope_id, skill_id)
        return {"ok": True, "removed": removed}

    @app.get("/admin/api/brain")
    async def admin_brain_list(
        scope_type: str = "",
        scope_id: str = "",
        q: str = "",
        limit: int = 100,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if scope_type:
            _validate_brain_scope(scope_type, scope_id)
        notes = await runtime.db.list_brain_notes(
            query=q,
            scope_type=scope_type or None,
            scope_id=scope_id or None,
            limit=max(1, min(limit, 500)),
        )
        return {
            "items": [_brain_payload(note) for note in notes],
            "stats": await runtime.brain.storage_stats(),
        }

    @app.post("/admin/api/brain")
    async def admin_brain_create(
        payload: BrainCreatePayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_brain_scope(payload.scope_type, payload.scope_id)
        try:
            note = await runtime.brain.create_note(
                title=payload.title,
                content=payload.content,
                tags=payload.tags,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                visibility=payload.visibility,
                source="admin",
            )
        except BrainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "brain.write",
            scope_type=payload.scope_type,
            scope_id=payload.scope_id or "global",
            actor_id="admin",
            target_id=str(note.id),
            args=payload.title[:200],
            status="ok",
        )
        return {"ok": True, "item": _brain_payload(note, full=True)}

    @app.get("/admin/api/brain/{note_id}")
    async def admin_brain_get(
        note_id: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        note = await runtime.db.get_brain_note(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="笔记不存在")
        return {"item": _brain_payload(note, full=True)}

    @app.patch("/admin/api/brain/{note_id}")
    async def admin_brain_update(
        note_id: int,
        payload: BrainPatchPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            note = await runtime.brain.update_note(
                note_id,
                title=payload.title,
                content=payload.content,
                tags=payload.tags,
                visibility=payload.visibility,
            )
        except BrainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if note is None:
            raise HTTPException(status_code=404, detail="笔记不存在")
        await runtime.db.add_skill_audit(
            "brain.write",
            scope_type=note.scope_type,
            scope_id=note.scope_id or "global",
            actor_id="admin",
            target_id=str(note.id),
            args=note.title[:200],
            status="ok",
        )
        return {"ok": True, "item": _brain_payload(note, full=True)}

    @app.delete("/admin/api/brain/{note_id}")
    async def admin_brain_delete(
        note_id: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        note = await runtime.brain.delete_note(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="笔记不存在")
        await runtime.db.add_skill_audit(
            "brain.delete",
            scope_type=note.scope_type,
            scope_id=note.scope_id or "global",
            actor_id="admin",
            target_id=str(note.id),
            args=note.title[:200],
            status="ok",
        )
        return {"ok": True}

    @app.get("/admin/api/presence")
    async def admin_presence(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        status = await runtime.presence.status()
        status["history"] = runtime.presence.list_history()
        status["settings"] = {
            "social_auto_enabled": bool(runtime.settings.social_auto_enabled),
            "profile_auto_enabled": bool(runtime.settings.profile_auto_enabled),
            "profile_auto_apply": bool(runtime.settings.profile_auto_apply),
            "profile_update_days": runtime.settings.profile_update_days,
            "qzone_auto_enabled": bool(runtime.settings.qzone_auto_enabled),
            "qzone_interval_hours": runtime.settings.qzone_interval_hours,
            "qzone_max_per_day": runtime.settings.qzone_max_per_day,
            "qzone_ugc_right": runtime.settings.qzone_ugc_right,
            "qzone_max_chars": runtime.settings.qzone_max_chars,
            "qzone_web_enabled": bool(runtime.settings.qzone_web_enabled),
            "qzone_rate_seconds": runtime.settings.qzone_rate_seconds,
            "qzone_comment_hourly_limit": runtime.settings.qzone_comment_hourly_limit,
        }
        return status

    @app.post("/admin/api/presence/profile/optimize")
    async def admin_profile_optimize(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            profile = await runtime.presence.optimize_and_apply()
        except PresenceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "item": profile.as_dict()}

    @app.post("/admin/api/presence/profile/apply")
    async def admin_profile_apply(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            profile = await runtime.presence.apply_profile()
        except PresenceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "item": profile.as_dict()}

    @app.post("/admin/api/presence/profile/draft")
    async def admin_profile_draft(
        payload: ProfileDraftPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        profile = SelfProfile(
            nickname=payload.nickname,
            signature=payload.signature,
            bio=payload.bio,
            tags=[item.strip() for item in re.split(r"[,，、]", payload.tags) if item.strip()],
            mood=payload.mood,
            source="manual",
        )
        try:
            saved = await runtime.presence.save_draft(profile)
            if payload.apply:
                saved = await runtime.presence.apply_profile(profile=saved)
        except PresenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "item": saved.as_dict()}

    @app.post("/admin/api/presence/profile/restore")
    async def admin_profile_restore(
        payload: ProfileRestorePayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            profile = await runtime.presence.restore_history(payload.version)
        except PresenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "item": profile.as_dict()}

    @app.post("/admin/api/presence/qzone/generate")
    async def admin_qzone_generate(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            content = await runtime.presence.generate_qzone_post()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "content": content}

    @app.post("/admin/api/presence/qzone/post")
    async def admin_qzone_post(
        payload: QzonePostPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            result = await runtime.presence.post_qzone(
                content=payload.content.strip() or None,
                images=payload.images,
                source="admin",
                ugc_right=payload.ugc_right,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "item": result}

    @app.delete("/admin/api/presence/qzone/{tid}")
    async def admin_qzone_delete(
        tid: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if not re.fullmatch(r"[\w.-]{1,100}", tid):
            raise HTTPException(status_code=400, detail="tid 格式不对")
        try:
            await runtime.presence.delete_qzone(tid)
        except PresenceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/admin/api/qzone")
    async def admin_qzone_list(
        uin: str = "",
        limit: int = 20,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            posts = await runtime.qzone.list_posts(
                target_uin=uin.strip() or None,
                limit=max(1, min(limit, 20)),
            )
        except QzoneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "items": [post.as_dict() for post in posts],
            "status": runtime.qzone.status(),
        }

    @app.post("/admin/api/qzone/like")
    async def admin_qzone_like(
        payload: QzoneLikePayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            result = await runtime.qzone.set_like(
                None,
                tid=payload.tid,
                active=payload.active,
                unikey=payload.unikey,
            )
        except QzoneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "qzone.like",
            scope_type="private",
            scope_id="self",
            actor_id="console",
            target_id=payload.tid,
            args="like" if payload.active else "unlike",
            status="ok",
        )
        return {"ok": True, "item": result}

    @app.post("/admin/api/qzone/comment")
    async def admin_qzone_comment(
        payload: QzoneCommentPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            result = await runtime.qzone.comment(
                None,
                tid=payload.tid,
                content=payload.content,
            )
        except QzoneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "qzone.comment",
            scope_type="private",
            scope_id="self",
            actor_id="console",
            target_id=payload.tid,
            args=payload.content[:200],
            status="ok",
        )
        return {"ok": True, "item": result}

    @app.post("/admin/api/qzone/comment/delete")
    async def admin_qzone_comment_delete(
        payload: QzoneCommentDeletePayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            result = await runtime.qzone.delete_comment(
                None,
                tid=payload.tid,
                comment_id=payload.comment_id,
            )
        except QzoneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "qzone.comment_delete",
            scope_type="private",
            scope_id="self",
            actor_id="console",
            target_id=payload.tid,
            args=payload.comment_id,
            status="ok",
        )
        return {"ok": True, "item": result}

    @app.get("/admin/api/guild")
    async def admin_guild_status(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return await runtime.guild.status(check_plugin=False)

    @app.post("/admin/api/guild/check")
    async def admin_guild_check(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return await runtime.guild.status()

    @app.put("/admin/api/guild/config")
    async def admin_guild_config(
        payload: GuildConfigPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            config = await runtime.guild.set_config(
                backend=payload.backend,
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
            )
        except GuildError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "guild.config",
            scope_type="console",
            scope_id="local",
            actor_id="console",
            target_id=config.guild_id,
            args=f"backend={config.backend} channel={config.channel_id}",
            status="ok",
        )
        return {
            "ok": True,
            "backend": config.backend,
            "guild_id": config.guild_id,
            "channel_id": config.channel_id,
        }

    @app.get("/admin/api/guild/channels")
    async def admin_guild_channels(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            return await runtime.guild.list_channels()
        except GuildError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/admin/api/guild/send")
    async def admin_guild_send(
        payload: GuildSendPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            result = await runtime.guild.send(
                None,
                payload.content,
                channel_id=payload.channel_id or None,
            )
        except GuildError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await runtime.db.add_skill_audit(
            "guild.send",
            scope_type="console",
            scope_id="local",
            actor_id="console",
            target_id=result.get("channel_id"),
            args=payload.content[:200],
            status="ok",
        )
        return {"ok": True, "item": result}

    @app.get("/admin/api/guild/messages")
    async def admin_guild_messages(
        channel_id: str = "",
        limit: int = 20,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        try:
            return await runtime.guild.history(
                None,
                channel_id=channel_id or None,
                limit=max(1, min(limit, 50)),
            )
        except GuildError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/admin/api/errors")
    async def admin_errors(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        return {"items": list(runtime.errors)}

    @app.get("/admin/api/stickers")
    async def admin_list_stickers(
        limit: int = 100,
        q: str = "",
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        rows = await runtime.db.list_stickers(limit=max(1, min(limit, 300)), query=q)
        return {
            "items": [_sticker_payload(record) for record in rows],
            "total": await runtime.db.count_stickers(),
            "enabled": await runtime.db.count_stickers(enabled_only=True),
        }

    @app.post("/admin/api/stickers")
    async def admin_upload_sticker(
        payload: StickerUploadPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        data = _decode_upload(payload.data)
        try:
            record, created = await runtime.stickers.upload(
                data,
                payload.filename,
                tags=payload.tags,
                description=payload.description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "created": created, "item": _sticker_payload(record)}

    @app.get("/admin/api/stickers/{sticker_id}/image")
    async def admin_sticker_image(
        sticker_id: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> Response:
        _check_token(runtime, token)
        record = await runtime.db.get_sticker(sticker_id)
        if record is None:
            raise HTTPException(status_code=404, detail="表情不存在")
        try:
            data, mime = await runtime.stickers.image_bytes(record)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="表情文件不存在") from exc
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "no-store"},
        )

    @app.patch("/admin/api/stickers/{sticker_id}")
    async def admin_update_sticker(
        sticker_id: int,
        payload: StickerPatchPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        record = await runtime.db.get_sticker(sticker_id)
        if record is None:
            raise HTTPException(status_code=404, detail="表情不存在")
        await runtime.db.update_sticker(
            sticker_id,
            tags=normalize_tags(payload.tags) if payload.tags is not None else None,
            description=normalize_description(payload.description) if payload.description is not None else None,
            enabled=payload.enabled,
        )
        updated = await runtime.db.get_sticker(sticker_id)
        return {"ok": True, "item": _sticker_payload(updated) if updated else None}

    @app.delete("/admin/api/stickers/{sticker_id}")
    async def admin_delete_sticker(
        sticker_id: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if not await runtime.stickers.delete(sticker_id):
            raise HTTPException(status_code=404, detail="表情不存在")
        return {"ok": True}

    @app.post("/admin/api/restart")
    async def admin_restart(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        logger.warning("Restart requested from local console")
        asyncio.get_running_loop().call_later(0.8, _restart_process)
        return {"ok": True, "detail": "机器人正在重启"}

    @app.get("/admin/api/persona")
    async def admin_get_persona(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        path = Path(runtime.settings.persona_file)
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return {
            "content": content,
            "persona": _persona_payload(runtime),
        }

    @app.put("/admin/api/persona")
    async def admin_save_persona(
        payload: PersonaPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if len(payload.content.encode("utf-8")) > MAX_PERSONA_BYTES:
            raise HTTPException(status_code=413, detail="人设文件太大")
        path = Path(runtime.settings.persona_file)
        history_dir = _persona_history_dir(runtime)
        try:
            new_persona = await asyncio.to_thread(
                _save_persona_file,
                path,
                payload.content,
                history_dir,
            )
        except (OSError, PersonaError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"人设格式不对：{exc}") from exc
        runtime.persona = new_persona
        if hasattr(runtime, "context"):
            runtime.context.persona = new_persona
        logger.info("Admin updated persona: %s", new_persona.display_name)
        return {"ok": True, "persona": _persona_payload(runtime)}

    @app.get("/admin/api/persona/versions")
    async def admin_persona_versions(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        versions = await asyncio.to_thread(_list_persona_versions, _persona_history_dir(runtime))
        return {"versions": versions}

    @app.post("/admin/api/persona/restore/{name}")
    async def admin_restore_persona(
        name: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if Path(name).name != name or not PERSONA_VERSION_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail="版本文件名不合法")
        history_dir = _persona_history_dir(runtime)
        source = history_dir / name
        if not source.is_file():
            raise HTTPException(status_code=404, detail="这个版本不存在")
        content = await asyncio.to_thread(source.read_text, encoding="utf-8")
        path = Path(runtime.settings.persona_file)
        try:
            new_persona = await asyncio.to_thread(_save_persona_file, path, content, history_dir)
        except (OSError, PersonaError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"人设版本损坏：{exc}") from exc
        runtime.persona = new_persona
        if hasattr(runtime, "context"):
            runtime.context.persona = new_persona
        logger.info("Admin restored persona version: %s", name)
        return {"ok": True, "persona": _persona_payload(runtime)}

    @app.get("/admin/api/memories")
    async def admin_memories(
        limit: int = 100,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        limit = max(1, min(limit, 500))
        rows = await _all(
            runtime,
            """
            SELECT id, scope_type, scope_id, user_id, content, category, confidence,
                   created_at, expires_at
            FROM memories
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {
            "items": [{key: row[key] for key in row.keys()} for row in rows],
            "count": len(rows),
        }

    @app.delete("/admin/api/memories/{memory_id}")
    async def admin_delete_memory(
        memory_id: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        row = await _one(
            runtime,
            "SELECT id, scope_type, scope_id, user_id FROM memories WHERE id = ?",
            (memory_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        deleted = await runtime.db.delete_memory(
            memory_id,
            str(row["scope_type"]),
            str(row["scope_id"]),
            row["user_id"],
        )
        return {"ok": True, "deleted": deleted}

    @app.get("/admin/api/styles")
    async def admin_styles(
        limit: int = 100,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        records = await runtime.style.list_profiles(limit=max(1, min(limit, 200)))
        return {"items": [_style_payload(record) for record in records], "count": len(records)}

    @app.get("/admin/api/styles/{scope_type}/{scope_id}")
    async def admin_style_detail(
        scope_type: str,
        scope_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        record = await runtime.style.get_profile(scope_type, scope_id)
        if record is None:
            raise HTTPException(status_code=404, detail="风格画像不存在")
        return _style_payload(record)

    @app.post("/admin/api/styles/{scope_type}/{scope_id}/learn")
    async def admin_style_learn(
        scope_type: str,
        scope_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        record = await runtime.style.learn_now(scope_type, scope_id, force=True)
        if record is None:
            raise HTTPException(status_code=400, detail="样本不足或学习失败，手动学习至少需要 5 条可用样本")
        return _style_payload(record)

    @app.patch("/admin/api/styles/{scope_type}/{scope_id}")
    async def admin_style_patch(
        scope_type: str,
        scope_id: str,
        payload: StylePatchPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        if payload.enabled is None:
            raise HTTPException(status_code=400, detail="enabled 不能为空")
        changed = await runtime.style.set_enabled(scope_type, scope_id, payload.enabled)
        if not changed:
            raise HTTPException(status_code=404, detail="风格画像不存在")
        record = await runtime.style.get_profile(scope_type, scope_id)
        return {"ok": True, "profile": _style_payload(record) if record else None}

    @app.delete("/admin/api/styles/{scope_type}/{scope_id}")
    async def admin_style_delete(
        scope_type: str,
        scope_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        deleted = await runtime.style.delete_profile(scope_type, scope_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="风格画像不存在")
        return {"ok": True, "deleted": True}

    @app.get("/admin/api/styles/{scope_type}/{scope_id}/versions")
    async def admin_style_versions(
        scope_type: str,
        scope_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        record = await runtime.style.get_profile(scope_type, scope_id)
        if record is None:
            raise HTTPException(status_code=404, detail="风格画像不存在")
        return {"versions": await runtime.db.list_style_versions(record.id, limit=20)}

    @app.post("/admin/api/styles/{scope_type}/{scope_id}/versions/{version}/restore")
    async def admin_style_restore(
        scope_type: str,
        scope_id: str,
        version: int,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_style_scope(scope_type, scope_id)
        record = await runtime.style.restore_version(scope_type, scope_id, version)
        if record is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        return _style_payload(record)

    @app.post("/admin/api/test-chat")
    async def admin_test_chat(
        payload: TestChatPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        system = runtime.persona.system_prompt() + "\n当前场景：本地控制台测试，直接回应用户。"
        started = time.perf_counter()
        try:
            result = await runtime.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload.message},
                ]
            )
        except LLMError as exc:
            runtime.record_error("console-llm", str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        reply = sanitize_text(result.text, runtime.persona.display_name)
        if not reply:
            raise HTTPException(status_code=502, detail="模型返回了空回复")
        await runtime.db.log_usage(
            "admin",
            "console",
            result.model,
            result.input_tokens,
            result.output_tokens,
        )
        return {
            "reply": reply,
            "model": result.model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    @app.post("/admin/api/groups/{group_id}")
    async def admin_update_group(
        group_id: str,
        payload: GroupPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_group_id(group_id)
        if payload.active is not None:
            if payload.active:
                await runtime.db.activate_group(group_id, "web-console")
            else:
                await runtime.db.deactivate_group(group_id)
        if payload.mode is not None:
            await runtime.db.set_group_mode(group_id, payload.mode)
        return {"ok": True}

    @app.post("/admin/api/groups/{group_id}/reset")
    async def admin_reset_group(
        group_id: str,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        _validate_group_id(group_id)
        await runtime.db.clear_session("group", group_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # 统一能力表 / 功能中心
    # ------------------------------------------------------------------
    @app.get("/admin/api/actions")
    async def admin_actions(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        items = runtime.actions.catalog()
        return {"ok": True, "actions": items, "total": len(items)}

    @app.post("/admin/api/actions/{skill_id}/run")
    async def admin_action_run(
        skill_id: str,
        payload: ActionRunPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        if not re.fullmatch(r"[a-z0-9_.]{1,80}", skill_id):
            raise HTTPException(status_code=400, detail="动作 ID 不合法")
        bots = get_bots()
        bot = next(iter(bots.values()), None) if bots else None
        try:
            result = await runtime.actions.execute(
                skill_id,
                params=payload.params,
                bot=bot,
                role="owner",
                actor="console",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # 风格进化（反馈 / 去 AI 化 / 自我批评）
    # ------------------------------------------------------------------
    @app.get("/admin/api/style/evolution")
    async def admin_style_evolution(
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        evolution = await runtime.db.list_style_evolution(limit=50)
        profiles = await runtime.db.list_style_profiles(limit=50)
        return {
            "ok": True,
            "evolution": evolution,
            "profiles": [
                {
                    "scope_type": item.scope_type,
                    "scope_id": item.scope_id,
                    "version": item.version,
                    "enabled": item.enabled,
                    "confidence": item.confidence,
                    "sample_count": item.sample_count,
                    "summary": item.summary,
                }
                for item in profiles
            ],
            "config": {
                "deai_enabled": runtime.settings.deai_enabled,
                "deai_record_only": runtime.settings.deai_record_only,
                "deai_light_threshold": runtime.settings.deai_light_threshold,
                "deai_heavy_threshold": runtime.settings.deai_heavy_threshold,
                "feedback_enabled": runtime.settings.feedback_enabled,
                "selfreview_enabled": runtime.settings.selfreview_enabled,
                "style_evolution_brain": runtime.settings.style_evolution_brain,
            },
        }

    @app.get("/admin/api/style/feedback")
    async def admin_style_feedback(
        scope_type: str = "",
        scope_id: str = "",
        limit: int = 50,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        since = (
            (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=7))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
        items = await runtime.db.list_reply_feedback(
            scope_type=scope_type if scope_type in {"group", "private"} else "",
            scope_id=scope_id if scope_type in {"group", "private"} else "",
            limit=limit,
        )
        stats = await runtime.db.feedback_stats(
            scope_type=scope_type if scope_type in {"group", "private"} else "",
            scope_id=scope_id if scope_type in {"group", "private"} else "",
            since=since,
        )
        return {"ok": True, "items": items, "stats": stats}

    @app.get("/admin/api/style/deai")
    async def admin_style_deai(
        limit: int = 50,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        since = (
            (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=7))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
        logs = await runtime.db.list_deai_logs(limit=limit)
        stats = await runtime.db.deai_stats(since=since)
        return {"ok": True, "logs": logs, "stats": stats}

    @app.post("/admin/api/style/review")
    async def admin_style_review(
        payload: StyleReviewPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        scope_id = payload.scope_id.strip()
        if payload.scope_type == "group" and not re.fullmatch(r"\d{5,20}", scope_id):
            raise HTTPException(status_code=400, detail="群号格式不对")
        if payload.scope_type == "private" and not re.fullmatch(r"\d{5,20}", scope_id):
            raise HTTPException(status_code=400, detail="QQ 号格式不对")
        try:
            result = await runtime.selfreview.review(
                payload.scope_type,
                scope_id,
                trigger="manual",
                force=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "review": result}

    @app.put("/admin/api/style/config")
    async def admin_style_config(
        payload: StyleConfigPayload,
        token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> dict[str, Any]:
        _check_token(runtime, token)
        settings = runtime.settings
        # 运行时开关：改的是当前进程，不落 .env
        if payload.deai_enabled is not None:
            settings.deai_enabled = payload.deai_enabled
        if payload.deai_record_only is not None:
            settings.deai_record_only = payload.deai_record_only
        if payload.deai_light_threshold is not None:
            settings.deai_light_threshold = payload.deai_light_threshold
        if payload.deai_heavy_threshold is not None:
            settings.deai_heavy_threshold = max(
                settings.deai_light_threshold, payload.deai_heavy_threshold
            )
        if payload.feedback_enabled is not None:
            settings.feedback_enabled = payload.feedback_enabled
        if payload.selfreview_enabled is not None:
            settings.selfreview_enabled = payload.selfreview_enabled
        if payload.style_evolution_brain is not None:
            settings.style_evolution_brain = payload.style_evolution_brain
        return {
            "ok": True,
            "config": {
                "deai_enabled": settings.deai_enabled,
                "deai_record_only": settings.deai_record_only,
                "deai_light_threshold": settings.deai_light_threshold,
                "deai_heavy_threshold": settings.deai_heavy_threshold,
                "feedback_enabled": settings.feedback_enabled,
                "selfreview_enabled": settings.selfreview_enabled,
                "style_evolution_brain": settings.style_evolution_brain,
            },
        }

    logger.info("Local admin console enabled at /admin")

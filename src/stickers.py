from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .image_utils import fetch_image_bytes
from .llm import DeepSeekClient, LLMError
from .models import StickerRecord
from .settings import Settings

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_MARKER_RE = re.compile(r"\[\[sticker:(\d+)\]\]")
_REQUEST_RE = re.compile(r"(来|发|整|给).{0,6}(表情|表情包)")
_DESCRIPTION_LIMIT = 80
_TAG_LIMIT = 12
_TAGS_LIMIT = 8


@dataclass(slots=True)
class ImagePart:
    url: str
    sub_type: int | None
    summary: str
    width: int | None = None
    height: int | None = None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_sticker_image(data: dict[str, Any]) -> bool:
    raw = data.get("sub_type")
    if raw is not None and str(raw).strip() != "":
        parsed = _int_or_none(raw)
        if parsed is not None:
            return parsed != 0
    summary = str(data.get("summary") or "")
    return "[动画表情]" in summary or "[表情]" in summary


def extract_sticker_images(message: object) -> list[ImagePart]:
    """Return only image segments that look like QQ stickers."""
    try:
        segments = list(message)  # type: ignore[arg-type]
    except TypeError:
        return []

    result: list[ImagePart] = []
    for segment in segments:
        if getattr(segment, "type", None) != "image":
            continue
        data = getattr(segment, "data", {}) or {}
        if not isinstance(data, dict) or not _is_sticker_image(data):
            continue
        url = data.get("url") or data.get("file")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        result.append(
            ImagePart(
                url=url,
                sub_type=_int_or_none(data.get("sub_type")),
                summary=str(data.get("summary") or ""),
                width=_int_or_none(data.get("width")),
                height=_int_or_none(data.get("height")),
            )
        )
    return result


def _clean_tag(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = value.replace("[[", "").replace("]]", "")
    value = re.sub(r"\s+", " ", value).strip(" ,，、;；")
    return value[:_TAG_LIMIT]


def normalize_tags(tags: Any) -> str:
    """Accept a list or comma/space separated string and return a clean CSV."""
    if isinstance(tags, str):
        values = re.split(r"[,，、;；\s]+", tags)
    elif isinstance(tags, list):
        values = [str(item) for item in tags]
    else:
        values = []
    cleaned: list[str] = []
    for item in values:
        tag = _clean_tag(item)
        if tag and tag not in cleaned:
            cleaned.append(tag)
        if len(cleaned) >= _TAGS_LIMIT:
            break
    return ",".join(cleaned)


def normalize_description(value: Any) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    text = text.replace("[[", "").replace("]]", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_DESCRIPTION_LIMIT]


def parse_tag_json(text: str) -> tuple[str, str]:
    """Parse the small JSON object returned by the vision tagger."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return "", ""
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    tags = normalize_tags(parsed.get("tags"))
    description = normalize_description(parsed.get("description"))
    return tags, description


class StickerService:
    def __init__(self, settings: Settings, db: Database, llm: DeepSeekClient) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self._tag_tasks: set[asyncio.Task[Any]] = set()

    @property
    def enabled(self) -> bool:
        return self.settings.sticker_library_enabled

    # ------------------------------------------------------------------
    # Ingest / storage
    # ------------------------------------------------------------------
    async def ingest_event(
        self,
        message: object,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        nickname: str | None,
        message_id: str,
    ) -> list[int]:
        if not self.enabled or not self.settings.sticker_ingest_enabled:
            return []
        images = extract_sticker_images(message)
        if not images:
            return []

        created_ids: list[int] = []
        for image in images:
            try:
                fetched = await fetch_image_bytes(
                    image.url,
                    max_bytes=self.settings.sticker_max_bytes,
                    allow_private_hosts=self.settings.image_allow_private_hosts,
                )
                if fetched is None:
                    continue
                data, mime = fetched
                record, created = await self.save_bytes(
                    data,
                    mime,
                    source_scope_type=scope_type,
                    source_scope_id=scope_id,
                    source_user_id=user_id,
                    source_nickname=nickname,
                    source_message_id=message_id,
                    width=image.width,
                    height=image.height,
                    enforce_limit=True,
                )
                if created:
                    created_ids.append(record.id)
                    self._schedule_tag(record.id)
            except Exception:
                logger.exception("Failed to ingest sticker from %s", image.url)
        return created_ids

    async def save_bytes(
        self,
        data: bytes,
        mime: str,
        *,
        source_scope_type: str | None = None,
        source_scope_id: str | None = None,
        source_user_id: str | None = None,
        source_nickname: str | None = None,
        source_message_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        tags: str = "",
        description: str = "",
        enforce_limit: bool = True,
    ) -> tuple[StickerRecord, bool]:
        if not self.enabled:
            raise ValueError("Sticker library is disabled")
        if mime not in _MIME_EXT or not _looks_like_image(data, mime):
            raise ValueError("Unsupported sticker image type")
        if not data or len(data) > self.settings.sticker_max_bytes:
            raise ValueError("Sticker image is empty or too large")

        digest = hashlib.sha256(data).hexdigest()
        existing = await self.db.get_sticker_by_hash(digest)
        if existing is not None:
            return existing, False
        if enforce_limit and await self.db.count_stickers() >= self.settings.sticker_max_count:
            raise ValueError("Sticker library is full")

        filename = f"{digest[:24]}{_MIME_EXT[mime]}"
        path = self.settings.sticker_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

        record, created = await self.db.add_sticker(
            sha256=digest,
            filename=filename,
            mime=mime,
            byte_size=len(data),
            width=width,
            height=height,
            tags=normalize_tags(tags),
            description=normalize_description(description),
            source_scope_type=source_scope_type,
            source_scope_id=source_scope_id,
            source_user_id=source_user_id,
            source_nickname=source_nickname,
            source_message_id=source_message_id,
        )
        if not created:
            # Another task inserted the same hash while we were writing.
            return record, False
        if tags or description:
            return record, True
        # The caller decides whether to schedule auto-tagging for uploads.
        return record, True

    async def upload(
        self,
        data: bytes,
        filename: str,
        *,
        tags: str = "",
        description: str = "",
    ) -> tuple[StickerRecord, bool]:
        mime = _detect_mime_for_upload(data, filename)
        record, created = await self.save_bytes(
            data,
            mime,
            source_scope_type="admin",
            source_scope_id="upload",
            source_user_id=None,
            source_nickname=None,
            source_message_id=None,
            tags=tags,
            description=description,
            enforce_limit=True,
        )
        if created and not tags and not description:
            self._schedule_tag(record.id)
        return record, created

    async def read_bytes(self, record: StickerRecord) -> bytes:
        path = self.settings.sticker_dir / record.filename
        return await asyncio.to_thread(path.read_bytes)

    async def image_bytes(self, record: StickerRecord) -> tuple[bytes, str]:
        return await self.read_bytes(record), record.mime

    async def data_url(self, record: StickerRecord) -> str:
        data = await self.read_bytes(record)
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{record.mime};base64,{encoded}"

    async def delete(self, sticker_id: int) -> bool:
        record = await self.db.get_sticker(sticker_id)
        if record is None:
            return False
        await self.db.delete_sticker(sticker_id)
        path = self.settings.sticker_dir / record.filename
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to remove sticker file %s", path)
        return True

    # ------------------------------------------------------------------
    # Auto tagging
    # ------------------------------------------------------------------
    def _schedule_tag(self, sticker_id: int) -> None:
        if not self.settings.sticker_auto_tag:
            return
        task = asyncio.create_task(self.tag_sticker(sticker_id))
        self._tag_tasks.add(task)
        task.add_done_callback(self._tag_tasks.discard)

    async def tag_sticker(self, sticker_id: int) -> bool:
        if not self.settings.sticker_auto_tag:
            return False
        record = await self.db.get_sticker(sticker_id)
        if record is None:
            return False
        try:
            data_url = await self.data_url(record)
            result = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是表情包标注器。看图片，只输出一个 JSON 对象："
                            '{"description":"一句中文描述","tags":["短词1","短词2"]}。'
                            "tags 给 3 到 6 个中文短词，不要解释，不要输出 markdown。"
                        ),
                    },
                    {"role": "user", "content": "给这张表情包打标签。"},
                ],
                max_tokens=180,
                temperature=0.2,
                json_mode=True,
                image_data_url=data_url,
            )
            tags, description = parse_tag_json(result.text)
            if tags or description:
                await self.db.update_sticker(
                    sticker_id,
                    tags=tags,
                    description=description,
                )
            await self.db.log_usage("sticker", "autotag", result.model, result.input_tokens, result.output_tokens)
            return bool(tags or description)
        except (LLMError, OSError, ValueError) as exc:
            logger.warning("Sticker auto-tag failed for %s: %s", sticker_id, exc)
            return False
        except Exception:
            logger.exception("Unexpected sticker auto-tag failure for %s", sticker_id)
            return False

    # ------------------------------------------------------------------
    # Retrieval / limits
    # ------------------------------------------------------------------
    @staticmethod
    def is_explicit_request(text: str) -> bool:
        return bool(_REQUEST_RE.search(text))

    @staticmethod
    def parse_marker(text: str) -> tuple[str, int | None]:
        ids = [int(match.group(1)) for match in _MARKER_RE.finditer(text)]
        clean = _MARKER_RE.sub("", text).strip()
        return clean, (ids[-1] if ids else None)

    async def catalog_for(
        self,
        scope_type: str,
        scope_id: str,
        *,
        explicit: bool = False,
    ) -> str | None:
        if not self.enabled or not self.settings.sticker_auto_send or self.settings.stream_replies:
            return None
        if await self.db.count_stickers(enabled_only=True) <= 0:
            return None
        if not await self.can_send(scope_type, scope_id, explicit=explicit):
            return None
        if not explicit and random.random() > self.settings.sticker_auto_chance:
            return None

        records = await self.db.list_stickers(limit=max(1, self.settings.sticker_catalog_limit), enabled_only=True)
        records = sorted(records, key=lambda item: (item.use_count, item.last_used_at or ""))
        if not records:
            return None
        lines = ["\n\n【可用表情】"]
        for record in records:
            tags = record.tags or "未标注"
            description = record.description or "没有描述"
            lines.append(f"{record.id} | 标签：{tags} | 描述：{description}")
        lines.append("只在确实合适时，在回复最后单独写 [[sticker:ID]]；一次最多一个；不要每轮都用。")
        return "\n".join(lines)

    async def pick_explicit(self) -> StickerRecord | None:
        records = await self.db.list_stickers(limit=100, enabled_only=True)
        return random.choice(records) if records else None

    async def can_send(self, scope_type: str, scope_id: str, *, explicit: bool = False) -> bool:
        if not self.enabled or not self.settings.sticker_auto_send:
            return False
        now = datetime.now(UTC)
        if not explicit:
            last = await self.db.last_sticker_send_at(scope_type, scope_id)
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    if (now - last_dt).total_seconds() < self.settings.sticker_cooldown_seconds:
                        return False
                except ValueError:
                    pass
        since_hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        if await self.db.count_sticker_sends(scope_type, scope_id, since=since_hour) >= self.settings.sticker_hourly_limit:
            return False
        since_day = _cn_day_start_utc(now).isoformat(timespec="seconds")
        if await self.db.count_sticker_sends(scope_type, scope_id, since=since_day) >= self.settings.sticker_daily_limit:
            return False
        return True

    async def mark_sent(self, sticker_id: int, scope_type: str, scope_id: str) -> None:
        await self.db.record_sticker_send(sticker_id, scope_type, scope_id)


def _cn_day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(CN_TZ)
    start_local = datetime.combine(local.date(), time.min, tzinfo=CN_TZ)
    return start_local.astimezone(UTC)


def _looks_like_image(data: bytes, mime: str) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _detect_mime_for_upload(data: bytes, filename: str) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database
from .models import BrainNote
from .safety import contains_prompt_injection, contains_sensitive
from .settings import Settings

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
_VALID_SCOPES = {"group", "private", "global"}
_VALID_VISIBILITY = {"scope", "global", "owner"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class BrainError(ValueError):
    pass


class BrainService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.root = Path(settings.brain_dir).resolve()
        self.notes_dir = self.root / "notes"
        self.profile_dir = self.root / "profile"
        self.qzone_dir = self.root / "qzone"
        self._locks: dict[str, asyncio.Lock] = {}

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "history").mkdir(parents=True, exist_ok=True)
        self.qzone_dir.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(
                "# 大肥鱼的大脑\n\n"
                "这里保存我长期使用的资料和记忆。\n\n"
                "- `notes/`：按编号保存的 Markdown 笔记，正文与 SQLite 索引同步。\n"
                "- `profile/`：主页资料和版本历史。\n"
                "- `qzone/`：空间动态相关的本地状态。\n\n"
                "不要手动修改正在被机器人使用的笔记；控制台会做路径和大小检查。\n",
                encoding="utf-8",
            )

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def note_path(self, note_id: int) -> Path:
        path = (self.notes_dir / f"{int(note_id):04d}.md").resolve()
        if self.root not in path.parents:
            raise BrainError("大脑路径不安全")
        return path

    def _validate(
        self,
        *,
        title: str,
        content: str,
        tags: str,
        scope_type: str,
        scope_id: str,
        visibility: str,
        for_auto: bool = False,
    ) -> None:
        title = title.strip()
        content = content.strip()
        tags = tags.strip()
        if not title or len(title) > 120:
            raise BrainError("标题需要 1–120 个字")
        if not content or len(content.encode("utf-8")) > self.settings.brain_max_note_bytes:
            raise BrainError(f"正文需要在 1–{self.settings.brain_max_note_bytes} 字节以内")
        if scope_type not in _VALID_SCOPES:
            raise BrainError("笔记范围不合法")
        if scope_type != "global" and not scope_id:
            raise BrainError("笔记范围缺少 ID")
        if visibility not in _VALID_VISIBILITY:
            raise BrainError("可见范围不合法")
        if _CONTROL_RE.search(title + content):
            raise BrainError("内容包含控制字符")
        if contains_prompt_injection(content):
            raise BrainError("内容看起来像提示词注入")
        if for_auto and (contains_sensitive(content) or contains_sensitive(title)):
            raise BrainError("自动沉淀内容包含敏感信息")

    async def _write_file(self, note_id: int, content: str) -> None:
        path = self.note_path(note_id)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

        def write() -> None:
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(content, encoding="utf-8")
            temp.replace(path)

        await asyncio.to_thread(write)

    async def _delete_file(self, note_id: int) -> None:
        path = self.note_path(note_id)
        await asyncio.to_thread(path.unlink, True)

    async def create_note(
        self,
        *,
        title: str,
        content: str,
        tags: str = "",
        scope_type: str = "global",
        scope_id: str = "",
        visibility: str = "scope",
        source: str = "manual",
        source_user_id: str | None = None,
        for_auto: bool = False,
    ) -> BrainNote:
        if not self.settings.brain_enabled:
            raise BrainError("大脑功能已关闭")
        if scope_type == "global" and visibility == "scope":
            visibility = "global"
        self._validate(
            title=title,
            content=content,
            tags=tags,
            scope_type=scope_type,
            scope_id=scope_id,
            visibility=visibility,
            for_auto=for_auto,
        )
        if await self.db.count_brain_notes() >= self.settings.brain_max_notes:
            raise BrainError("大脑笔记数量已达上限")
        if await self.db.sum_brain_bytes() + len(content.encode("utf-8")) > 100 * 1024 * 1024:
            raise BrainError("大脑存储已达上限")
        note = await self.db.create_brain_note(
            title=title.strip(),
            content=content.strip(),
            tags=tags.strip(),
            scope_type=scope_type,
            scope_id=scope_id,
            visibility=visibility,
            source=source,
            source_user_id=source_user_id,
        )
        try:
            await self._write_file(note.id, note.content)
        except OSError as exc:
            await self.db.delete_brain_note(note.id)
            raise BrainError("写入笔记文件失败") from exc
        return note

    async def update_note(
        self,
        note_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: str | None = None,
        visibility: str | None = None,
    ) -> BrainNote:
        current = await self.db.get_brain_note(note_id)
        if current is None:
            raise BrainError("笔记不存在")
        new_title = current.title if title is None else title
        new_content = current.content if content is None else content
        new_tags = current.tags if tags is None else tags
        new_visibility = current.visibility if visibility is None else visibility
        if current.scope_type == "global" and new_visibility == "scope":
            new_visibility = "global"
        self._validate(
            title=new_title,
            content=new_content,
            tags=new_tags,
            scope_type=current.scope_type,
            scope_id=current.scope_id,
            visibility=new_visibility,
        )
        old_content = current.content
        await self._write_file(note_id, new_content.strip())
        try:
            updated = await self.db.update_brain_note(
                note_id,
                title=new_title.strip(),
                content=new_content.strip(),
                tags=new_tags.strip(),
                visibility=new_visibility,
            )
        except Exception:
            await self._write_file(note_id, old_content)
            raise
        if updated is None:
            await self._write_file(note_id, old_content)
            raise BrainError("笔记不存在")
        return updated

    async def delete_note(self, note_id: int) -> BrainNote | None:
        note = await self.db.delete_brain_note(note_id)
        if note is None:
            return None
        try:
            await self._delete_file(note.id)
        except OSError:
            logger.warning("Deleted brain row but could not remove file %s", note.id)
        return note

    async def search(
        self,
        query: str,
        *,
        scope_type: str,
        scope_id: str,
        limit: int | None = None,
    ) -> list[BrainNote]:
        if not self.settings.brain_enabled or not query.strip():
            return []
        return await self.db.search_brain_notes(
            query.strip()[:200],
            scope_type=scope_type,
            scope_id=scope_id,
            limit=limit or self.settings.brain_search_limit,
        )

    async def prompt_block(self, text: str, *, scope_type: str, scope_id: str) -> str:
        notes = await self.search(text, scope_type=scope_type, scope_id=scope_id)
        if not notes:
            return ""
        lines: list[str] = []
        used = 0
        for note in notes:
            body = re.sub(r"\s+", " ", note.content).strip()
            line = f"- {note.title}：{body[:240]}"
            if used + len(line) > self.settings.brain_context_chars:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        return "你大脑里的资料（只在相关时自然使用，不要逐条复述，也不要暴露存储系统）：\n" + "\n".join(lines)

    async def remember(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str | None,
        content: str,
    ) -> BrainNote | None:
        return await self.capture_memories(
            scope_type=scope_type,
            scope_id=scope_id,
            user_id=user_id,
            contents=[content],
        )

    async def capture_memories(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str | None,
        contents: list[str],
    ) -> BrainNote | None:
        if not self.settings.brain_enabled or not self.settings.brain_capture_enabled:
            return None
        if scope_type not in {"group", "private"}:
            return None
        clean: list[str] = []
        for item in contents[: self.settings.brain_capture_max_memories]:
            value = re.sub(r"\s+", " ", str(item)).strip()
            if not 2 <= len(value) <= 200:
                continue
            if contains_sensitive(value) or contains_prompt_injection(value):
                continue
            if value not in clean:
                clean.append(value)
        if not clean:
            return None
        day = datetime.now(CN_TZ).date().isoformat()
        label = "群" if scope_type == "group" else "私聊"
        title = f"[{label} {scope_id}] {day} 记忆"
        lock = self._lock_for(f"{scope_type}:{scope_id}:{day}")
        async with lock:
            existing = await self.db.find_brain_note(
                title=title,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if existing is None:
                body = "\n".join(f"- {item}" for item in clean)
                return await self.create_note(
                    title=title,
                    content=body,
                    tags="自动记忆",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    visibility="scope",
                    source="memory",
                    source_user_id=user_id,
                    for_auto=True,
                )
            additions = [item for item in clean if f"- {item}" not in existing.content]
            if not additions:
                return existing
            new_content = existing.content.rstrip() + "\n" + "\n".join(f"- {item}" for item in additions)
            return await self.update_note(existing.id, content=new_content)

    async def storage_stats(self) -> dict[str, int]:
        notes = await self.db.count_brain_notes()
        total = await self.db.sum_brain_bytes()
        files = 0
        disk_bytes = 0

        def scan() -> tuple[int, int]:
            count = 0
            size = 0
            for path in self.notes_dir.glob("*.md"):
                if path.is_file():
                    count += 1
                    size += path.stat().st_size
            return count, size

        try:
            files, disk_bytes = await asyncio.to_thread(scan)
        except OSError:
            pass
        return {"notes": notes, "db_bytes": total, "files": files, "disk_bytes": disk_bytes}

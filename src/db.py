from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    BrainNote,
    ExploreFinding,
    GroupState,
    MemoryItem,
    SessionState,
    SkillSetting,
    SocialPost,
    StickerRecord,
    StoredMessage,
    StyleLearningState,
    StyleProfile,
    UserState,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_groups (
    group_id TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'smart',
    activated_by TEXT,
    activated_at TEXT,
    last_bot_reply_at TEXT,
    last_bot_reply_user TEXT,
    persona_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT,
    private_active INTEGER NOT NULL DEFAULT 1,
    persona_id TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    user_id TEXT,
    nickname TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    user_id TEXT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact',
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id, user_id);

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT,
    scope_id TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_type, scope_id, message_id)
);

CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    tags TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_scope_type TEXT,
    source_scope_id TEXT,
    source_user_id TEXT,
    source_nickname TEXT,
    source_message_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stickers_enabled ON stickers(enabled, created_at);

CREATE TABLE IF NOT EXISTS sticker_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sticker_id INTEGER NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sticker_sends_scope ON sticker_sends(scope_type, scope_id, created_at);

CREATE TABLE IF NOT EXISTS style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_style_profiles_updated ON style_profiles(updated_at);

CREATE TABLE IF NOT EXISTS style_profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, version)
);
CREATE INDEX IF NOT EXISTS idx_style_versions_profile ON style_profile_versions(profile_id, version);

CREATE TABLE IF NOT EXISTS style_learning_state (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    samples_seen INTEGER NOT NULL DEFAULT 0,
    last_learned_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS skill_settings (
    skill_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    min_role TEXT NOT NULL DEFAULT 'owner',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_grants (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    min_role TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_type, scope_id, skill_id)
);

CREATE TABLE IF NOT EXISTS skill_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    actor_id TEXT,
    target_id TEXT,
    args TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_audit_created ON skill_audit(created_at);

CREATE TABLE IF NOT EXISTS brain_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'scope',
    source TEXT NOT NULL DEFAULT 'manual',
    source_user_id TEXT,
    byte_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brain_notes_updated ON brain_notes(updated_at);
CREATE INDEX IF NOT EXISTS idx_brain_notes_scope ON brain_notes(scope_type, scope_id);

CREATE TABLE IF NOT EXISTS social_posts (
    tid TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    ugc_right INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'posted',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_social_posts_created ON social_posts(created_at);

CREATE TABLE IF NOT EXISTS social_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS explore_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    summary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_explore_findings_created ON explore_findings(created_at);

CREATE TABLE IF NOT EXISTS reply_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    reply_msg_id TEXT NOT NULL DEFAULT '',
    persona_version INTEGER NOT NULL DEFAULT 0,
    deai_score INTEGER NOT NULL DEFAULT 0,
    signals TEXT NOT NULL DEFAULT '{}',
    score INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_scope_ts ON reply_feedback(scope_type, scope_id, ts);

CREATE TABLE IF NOT EXISTS deai_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    hit_rules TEXT NOT NULL DEFAULT '[]',
    rewritten INTEGER NOT NULL DEFAULT 0,
    reply_chars INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deai_scope_ts ON deai_logs(scope_type, scope_id, ts);

CREATE TABLE IF NOT EXISTS style_evolution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    from_version INTEGER NOT NULL DEFAULT 0,
    to_version INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL DEFAULT 'auto_review',
    fields_changed TEXT NOT NULL DEFAULT '[]',
    diagnosis TEXT NOT NULL DEFAULT '',
    feedback_before REAL NOT NULL DEFAULT 0,
    feedback_after REAL NOT NULL DEFAULT 0,
    deai_before REAL NOT NULL DEFAULT 0,
    deai_after REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_style_evolution_ts ON style_evolution(scope_type, scope_id, ts);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self._write_lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor.rowcount

    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    async def get_group(self, group_id: str) -> GroupState | None:
        row = await self._one("SELECT * FROM chat_groups WHERE group_id = ?", (group_id,))
        if row is None:
            return None
        return GroupState(
            group_id=row["group_id"],
            active=bool(row["active"]),
            mode=row["mode"],
            activated_by=row["activated_by"],
            activated_at=row["activated_at"],
            last_bot_reply_at=row["last_bot_reply_at"],
            last_bot_reply_user=row["last_bot_reply_user"],
            persona_id=row["persona_id"],
        )

    async def ensure_group(self, group_id: str) -> GroupState:
        group = await self.get_group(group_id)
        if group is not None:
            return group
        now = utcnow()
        await self._write(
            """
            INSERT OR IGNORE INTO chat_groups
                (group_id, active, mode, created_at, updated_at)
            VALUES (?, 0, 'smart', ?, ?)
            """,
            (group_id, now, now),
        )
        group = await self.get_group(group_id)
        if group is None:
            raise RuntimeError(f"Failed to create group state for {group_id}")
        return group

    async def activate_group(self, group_id: str, user_id: str) -> None:
        now = utcnow()
        await self._write(
            """
            INSERT INTO chat_groups
                (group_id, active, mode, activated_by, activated_at, created_at, updated_at)
            VALUES (?, 1, 'smart', ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                active = 1,
                activated_by = excluded.activated_by,
                activated_at = excluded.activated_at,
                updated_at = excluded.updated_at
            """,
            (group_id, user_id, now, now, now),
        )

    async def deactivate_group(self, group_id: str) -> None:
        await self.ensure_group(group_id)
        await self._write(
            "UPDATE chat_groups SET active = 0, updated_at = ? WHERE group_id = ?",
            (utcnow(), group_id),
        )

    async def set_group_mode(self, group_id: str, mode: str) -> None:
        await self.ensure_group(group_id)
        await self._write(
            "UPDATE chat_groups SET mode = ?, updated_at = ? WHERE group_id = ?",
            (mode, utcnow(), group_id),
        )

    async def record_bot_reply(self, group_id: str, user_id: str) -> None:
        await self._write(
            """
            UPDATE chat_groups
            SET last_bot_reply_at = ?, last_bot_reply_user = ?, updated_at = ?
            WHERE group_id = ?
            """,
            (utcnow(), user_id, utcnow(), group_id),
        )

    def seconds_since_last_bot_reply(self, group: GroupState) -> float | None:
        last = _parse_dt(group.last_bot_reply_at)
        if last is None:
            return None
        return max(0.0, (datetime.now(UTC) - last).total_seconds())

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    async def get_user(self, user_id: str) -> UserState | None:
        row = await self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        return UserState(
            user_id=row["user_id"],
            nickname=row["nickname"],
            private_active=bool(row["private_active"]),
            persona_id=row["persona_id"],
        )

    async def ensure_user(self, user_id: str, nickname: str | None = None) -> UserState:
        now = utcnow()
        await self._write(
            """
            INSERT INTO users (user_id, nickname, private_active, created_at, last_seen_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, users.nickname),
                last_seen_at = excluded.last_seen_at
            """,
            (user_id, nickname, now, now),
        )
        user = await self.get_user(user_id)
        if user is None:
            raise RuntimeError(f"Failed to create user {user_id}")
        return user

    async def set_private_active(self, user_id: str, active: bool) -> None:
        await self.ensure_user(user_id)
        await self._write(
            "UPDATE users SET private_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )

    # ------------------------------------------------------------------
    # Sessions and messages
    # ------------------------------------------------------------------
    async def get_session(self, scope_type: str, scope_id: str) -> SessionState | None:
        row = await self._one(
            "SELECT * FROM sessions WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        if row is None:
            return None
        return SessionState(
            id=row["id"], scope_type=row["scope_type"], scope_id=row["scope_id"], summary=row["summary"]
        )

    async def get_or_create_session(self, scope_type: str, scope_id: str) -> SessionState:
        session = await self.get_session(scope_type, scope_id)
        if session is not None:
            return session
        now = utcnow()
        await self._write(
            "INSERT OR IGNORE INTO sessions (scope_type, scope_id, summary, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
            (scope_type, scope_id, now, now),
        )
        session = await self.get_session(scope_type, scope_id)
        if session is None:
            raise RuntimeError(f"Failed to create session {scope_type}:{scope_id}")
        return session

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: str,
        *,
        user_id: str | None = None,
        nickname: str | None = None,
    ) -> int:
        now = utcnow()
        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO messages (session_id, role, content, user_id, nickname, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, role, content, user_id, nickname, now),
            )
            await self.conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            await self.conn.commit()
            return int(cursor.lastrowid or 0)

    async def get_messages(self, session_id: int, limit: int = 16) -> list[StoredMessage]:
        rows = await self._all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows.reverse()
        return [
            StoredMessage(
                id=row["id"],
                role=row["role"],
                content=row["content"],
                user_id=row["user_id"],
                nickname=row["nickname"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_oldest_messages(self, session_id: int, limit: int) -> list[StoredMessage]:
        rows = await self._all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        )
        return [
            StoredMessage(
                id=row["id"],
                role=row["role"],
                content=row["content"],
                user_id=row["user_id"],
                nickname=row["nickname"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_style_samples(
        self,
        session_id: int,
        *,
        after_id: int = 0,
        limit: int = 120,
    ) -> list[StoredMessage]:
        """Return user messages for style learning, in chronological order.

        With no cursor, take the most recent window instead of the oldest
        messages in a long-lived session.
        """
        limit = max(1, min(int(limit), 300))
        if after_id > 0:
            rows = await self._all(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND id > ? AND role = 'user'
                ORDER BY id ASC LIMIT ?
                """,
                (session_id, after_id, limit),
            )
        else:
            rows = await self._all(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            )
            rows.reverse()
        return [
            StoredMessage(
                id=row["id"],
                role=row["role"],
                content=row["content"],
                user_id=row["user_id"],
                nickname=row["nickname"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def count_messages(self, session_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,))
        return int(row["n"]) if row else 0

    async def delete_messages_through(self, session_id: int, max_id: int) -> None:
        await self._write(
            "DELETE FROM messages WHERE session_id = ? AND id <= ?",
            (session_id, max_id),
        )

    async def set_summary(self, session_id: int, summary: str) -> None:
        await self._write(
            "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
            (summary, utcnow(), session_id),
        )

    async def clear_session(self, scope_type: str, scope_id: str) -> None:
        session = await self.get_or_create_session(scope_type, scope_id)
        await self._write("DELETE FROM messages WHERE session_id = ?", (session.id,))
        await self._write("UPDATE sessions SET summary = '', updated_at = ? WHERE id = ?", (utcnow(), session.id))

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------
    async def mark_processed(self, scope_type: str, scope_id: str, message_id: str) -> bool:
        """Return True when this message is new, False when it is a duplicate."""
        cursor = await self.conn.execute(
            "INSERT OR IGNORE INTO processed_messages (scope_type, scope_id, message_id, created_at) VALUES (?, ?, ?, ?)",
            (scope_type, scope_id, message_id, utcnow()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------
    async def add_memory(
        self,
        scope_type: str,
        scope_id: str,
        content: str,
        *,
        user_id: str | None = None,
        category: str = "fact",
        confidence: float = 0.8,
        ttl_days: int = 90,
    ) -> bool:
        content = content.strip()
        if not content:
            return False
        existing = await self._one(
            """
            SELECT id FROM memories
            WHERE scope_type = ? AND scope_id = ? AND user_id IS ? AND content = ?
            """,
            (scope_type, scope_id, user_id, content),
        )
        if existing is not None:
            return False
        now = datetime.now(UTC)
        expires = (now + timedelta(days=max(1, ttl_days))).isoformat(timespec="seconds")
        await self._write(
            """
            INSERT INTO memories
                (scope_type, scope_id, user_id, content, category, confidence, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_type,
                scope_id,
                user_id,
                content[:300],
                category or "fact",
                max(0.0, min(1.0, confidence)),
                now.isoformat(timespec="seconds"),
                expires,
            ),
        )
        return True

    def _memory_scope_sql(
        self, scope_type: str, scope_id: str, user_id: str | None
    ) -> tuple[str, tuple[Any, ...]]:
        if scope_type == "private":
            return "scope_type = 'private' AND scope_id = ?", (scope_id,)
        if user_id is None:
            return "scope_type = 'group' AND scope_id = ? AND user_id IS NULL", (scope_id,)
        return (
            "scope_type = 'group' AND scope_id = ? AND (user_id IS NULL OR user_id = ?)",
            (scope_id, user_id),
        )

    async def list_memories(
        self,
        scope_type: str,
        scope_id: str,
        *,
        user_id: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        where, params = self._memory_scope_sql(scope_type, scope_id, user_id)
        rows = await self._all(
            f"""
            SELECT * FROM memories
            WHERE {where} AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY id DESC LIMIT ?
            """,
            (*params, utcnow(), limit),
        )
        return [self._memory_from_row(row) for row in rows]

    async def search_memories(
        self,
        scope_type: str,
        scope_id: str,
        query: str,
        *,
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[MemoryItem]:
        keywords = [part for part in re.split(r"[^\w\u4e00-\u9fff]+", query) if len(part) >= 2][:6]
        if not keywords:
            return await self.list_memories(scope_type, scope_id, user_id=user_id, limit=limit)
        where, params = self._memory_scope_sql(scope_type, scope_id, user_id)
        like_clause = " OR ".join("content LIKE ?" for _ in keywords)
        rows = await self._all(
            f"""
            SELECT * FROM memories
            WHERE {where}
              AND (expires_at IS NULL OR expires_at > ?)
              AND ({like_clause})
            ORDER BY confidence DESC, id DESC LIMIT ?
            """,
            (*params, utcnow(), *[f"%{keyword}%" for keyword in keywords], limit),
        )
        return [self._memory_from_row(row) for row in rows]

    async def delete_user_memories(self, scope_type: str, scope_id: str, user_id: str) -> int:
        if scope_type == "private":
            return await self._write(
                "DELETE FROM memories WHERE scope_type = 'private' AND scope_id = ?",
                (scope_id,),
            )
        return await self._write(
            "DELETE FROM memories WHERE scope_type = 'group' AND scope_id = ? AND user_id = ?",
            (scope_id, user_id),
        )

    async def delete_memory(self, memory_id: int, scope_type: str, scope_id: str, user_id: str | None) -> int:
        where, params = self._memory_scope_sql(scope_type, scope_id, user_id)
        return await self._write(
            f"DELETE FROM memories WHERE id = ? AND {where}",
            (memory_id, *params),
        )

    @staticmethod
    def _memory_from_row(row: aiosqlite.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            user_id=row["user_id"],
            content=row["content"],
            category=row["category"],
            confidence=float(row["confidence"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    # ------------------------------------------------------------------
    # Style learning
    # ------------------------------------------------------------------
    async def get_style_state(self, scope_type: str, scope_id: str) -> StyleLearningState | None:
        row = await self._one(
            "SELECT * FROM style_learning_state WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        if row is None:
            return None
        return StyleLearningState(
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            last_message_id=int(row["last_message_id"] or 0),
            samples_seen=int(row["samples_seen"] or 0),
            last_learned_at=row["last_learned_at"],
            updated_at=row["updated_at"],
        )

    async def set_style_state(
        self,
        scope_type: str,
        scope_id: str,
        *,
        last_message_id: int,
        samples_seen: int,
        last_learned_at: str | None,
    ) -> None:
        now = utcnow()
        await self._write(
            """
            INSERT INTO style_learning_state (
                scope_type, scope_id, last_message_id, samples_seen, last_learned_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                samples_seen = excluded.samples_seen,
                last_learned_at = excluded.last_learned_at,
                updated_at = excluded.updated_at
            """,
            (scope_type, scope_id, max(0, int(last_message_id)), max(0, int(samples_seen)), last_learned_at, now),
        )

    async def get_style_profile(self, scope_type: str, scope_id: str) -> StyleProfile | None:
        row = await self._one(
            "SELECT * FROM style_profiles WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        return self._style_profile_from_row(row) if row else None

    async def list_style_profiles(self, *, limit: int = 100) -> list[StyleProfile]:
        rows = await self._all(
            "SELECT * FROM style_profiles ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        return [self._style_profile_from_row(row) for row in rows]

    async def count_style_profiles(self, *, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM style_profiles"
        if enabled_only:
            sql += " WHERE enabled = 1"
        row = await self._one(sql)
        return int(row["n"]) if row else 0

    async def upsert_style_profile(
        self,
        scope_type: str,
        scope_id: str,
        *,
        profile: dict[str, Any],
        sample_count: int,
        confidence: float,
        enabled: bool,
        version: int,
    ) -> StyleProfile:
        now = utcnow()
        payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        await self._write(
            """
            INSERT INTO style_profiles (
                scope_type, scope_id, profile_json, sample_count, confidence,
                enabled, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                profile_json = excluded.profile_json,
                sample_count = excluded.sample_count,
                confidence = excluded.confidence,
                enabled = excluded.enabled,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (
                scope_type,
                scope_id,
                payload,
                max(0, int(sample_count)),
                max(0.0, min(1.0, float(confidence))),
                1 if enabled else 0,
                max(1, int(version)),
                now,
                now,
            ),
        )
        record = await self.get_style_profile(scope_type, scope_id)
        if record is None:
            raise RuntimeError("Failed to save style profile")
        return record

    async def set_style_enabled(self, scope_type: str, scope_id: str, enabled: bool) -> bool:
        changed = await self._write(
            "UPDATE style_profiles SET enabled = ?, updated_at = ? WHERE scope_type = ? AND scope_id = ?",
            (1 if enabled else 0, utcnow(), scope_type, scope_id),
        )
        return bool(changed)

    async def delete_style_profile(self, scope_type: str, scope_id: str) -> bool:
        record = await self.get_style_profile(scope_type, scope_id)
        if record is None:
            return False
        await self._write("DELETE FROM style_profile_versions WHERE profile_id = ?", (record.id,))
        await self._write(
            "DELETE FROM style_profiles WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        await self._write(
            "DELETE FROM style_learning_state WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        return True

    async def add_style_version(
        self,
        profile_id: int,
        version: int,
        profile: dict[str, Any],
        sample_count: int,
        confidence: float,
    ) -> None:
        await self._write(
            """
            INSERT OR IGNORE INTO style_profile_versions (
                profile_id, version, profile_json, sample_count, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(profile_id),
                int(version),
                json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
                int(sample_count),
                max(0.0, min(1.0, float(confidence))),
                utcnow(),
            ),
        )

    async def list_style_versions(self, profile_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._all(
            """
            SELECT version, profile_json, sample_count, confidence, created_at
            FROM style_profile_versions
            WHERE profile_id = ?
            ORDER BY version DESC LIMIT ?
            """,
            (int(profile_id), max(1, min(int(limit), 100))),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                profile = json.loads(row["profile_json"])
            except (TypeError, json.JSONDecodeError):
                profile = {}
            result.append(
                {
                    "version": int(row["version"]),
                    "profile": profile if isinstance(profile, dict) else {},
                    "sample_count": int(row["sample_count"]),
                    "confidence": float(row["confidence"]),
                    "created_at": row["created_at"],
                }
            )
        return result

    async def get_style_version(self, profile_id: int, version: int) -> dict[str, Any] | None:
        row = await self._one(
            """
            SELECT version, profile_json, sample_count, confidence, created_at
            FROM style_profile_versions
            WHERE profile_id = ? AND version = ?
            """,
            (int(profile_id), int(version)),
        )
        if row is None:
            return None
        try:
            profile = json.loads(row["profile_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(profile, dict):
            return None
        return {
            "version": int(row["version"]),
            "profile": profile,
            "sample_count": int(row["sample_count"]),
            "confidence": float(row["confidence"]),
            "created_at": row["created_at"],
        }

    async def prune_style_versions(self, profile_id: int, keep: int) -> None:
        keep = max(0, int(keep))
        if keep <= 0:
            await self._write("DELETE FROM style_profile_versions WHERE profile_id = ?", (int(profile_id),))
            return
        await self._write(
            """
            DELETE FROM style_profile_versions
            WHERE profile_id = ?
              AND id NOT IN (
                  SELECT id FROM style_profile_versions
                  WHERE profile_id = ?
                  ORDER BY version DESC
                  LIMIT ?
              )
            """,
            (int(profile_id), int(profile_id), keep),
        )

    @staticmethod
    def _style_profile_from_row(row: aiosqlite.Row) -> StyleProfile:
        try:
            profile = json.loads(row["profile_json"])
        except (TypeError, json.JSONDecodeError):
            profile = {}
        return StyleProfile(
            id=int(row["id"]),
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            profile=profile if isinstance(profile, dict) else {},
            sample_count=int(row["sample_count"] or 0),
            confidence=float(row["confidence"] or 0),
            enabled=bool(row["enabled"]),
            version=int(row["version"] or 1),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    async def get_skill_setting(self, skill_id: str) -> SkillSetting | None:
        row = await self._one("SELECT * FROM skill_settings WHERE skill_id = ?", (skill_id,))
        return self._skill_setting_from_row(row) if row else None

    async def list_skill_settings(self) -> list[SkillSetting]:
        rows = await self._all("SELECT * FROM skill_settings ORDER BY skill_id")
        return [self._skill_setting_from_row(row) for row in rows]

    async def upsert_skill_setting(self, skill_id: str, *, enabled: bool, min_role: str) -> SkillSetting:
        now = utcnow()
        await self._write(
            """
            INSERT INTO skill_settings (skill_id, enabled, min_role, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                enabled = excluded.enabled,
                min_role = excluded.min_role,
                updated_at = excluded.updated_at
            """,
            (skill_id, 1 if enabled else 0, min_role, now),
        )
        setting = await self.get_skill_setting(skill_id)
        if setting is None:
            raise RuntimeError("Failed to save skill setting")
        return setting

    async def get_skill_grant(self, scope_type: str, scope_id: str, skill_id: str) -> dict[str, Any] | None:
        row = await self._one(
            """
            SELECT scope_type, scope_id, skill_id, enabled, min_role, updated_at
            FROM skill_grants
            WHERE scope_type = ? AND scope_id = ? AND skill_id = ?
            """,
            (scope_type, scope_id, skill_id),
        )
        if row is None:
            return None
        return {
            "scope_type": str(row["scope_type"]),
            "scope_id": str(row["scope_id"]),
            "skill_id": str(row["skill_id"]),
            "enabled": bool(row["enabled"]),
            "min_role": str(row["min_role"]),
            "updated_at": row["updated_at"],
        }

    async def list_skill_grants(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        rows = await self._all(
            """
            SELECT scope_type, scope_id, skill_id, enabled, min_role, updated_at
            FROM skill_grants
            WHERE scope_type = ? AND scope_id = ?
            ORDER BY skill_id
            """,
            (scope_type, scope_id),
        )
        return [
            {
                "scope_type": str(row["scope_type"]),
                "scope_id": str(row["scope_id"]),
                "skill_id": str(row["skill_id"]),
                "enabled": bool(row["enabled"]),
                "min_role": str(row["min_role"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def upsert_skill_grant(
        self,
        scope_type: str,
        scope_id: str,
        skill_id: str,
        *,
        enabled: bool,
        min_role: str,
    ) -> dict[str, Any]:
        await self._write(
            """
            INSERT INTO skill_grants (scope_type, scope_id, skill_id, enabled, min_role, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_id, skill_id) DO UPDATE SET
                enabled = excluded.enabled,
                min_role = excluded.min_role,
                updated_at = excluded.updated_at
            """,
            (scope_type, scope_id, skill_id, 1 if enabled else 0, min_role, utcnow()),
        )
        grant = await self.get_skill_grant(scope_type, scope_id, skill_id)
        if grant is None:
            raise RuntimeError("Failed to save skill grant")
        return grant

    async def delete_skill_grant(self, scope_type: str, scope_id: str, skill_id: str) -> bool:
        changed = await self._write(
            "DELETE FROM skill_grants WHERE scope_type = ? AND scope_id = ? AND skill_id = ?",
            (scope_type, scope_id, skill_id),
        )
        return bool(changed)

    async def add_skill_audit(
        self,
        skill_id: str,
        *,
        scope_type: str,
        scope_id: str,
        actor_id: str | None,
        target_id: str | None,
        args: str,
        status: str,
        error: str = "",
    ) -> None:
        await self._write(
            """
            INSERT INTO skill_audit (
                skill_id, scope_type, scope_id, actor_id, target_id, args, status, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                scope_type,
                scope_id,
                actor_id,
                target_id,
                str(args)[:500],
                status[:20],
                str(error)[:500],
                utcnow(),
            ),
        )
        row = await self._one("SELECT COUNT(*) AS n FROM skill_audit")
        if row and int(row["n"] or 0) > 1000:
            await self._write(
                """
                DELETE FROM skill_audit
                WHERE id NOT IN (SELECT id FROM skill_audit ORDER BY id DESC LIMIT 1000)
                """
            )

    async def list_skill_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._all(
            """
            SELECT id, skill_id, scope_type, scope_id, actor_id, target_id, args, status, error, created_at
            FROM skill_audit ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )
        return [
            {
                "id": int(row["id"]),
                "skill_id": str(row["skill_id"]),
                "scope_type": str(row["scope_type"]),
                "scope_id": str(row["scope_id"]),
                "actor_id": row["actor_id"],
                "target_id": row["target_id"],
                "args": str(row["args"] or ""),
                "status": str(row["status"]),
                "error": str(row["error"] or ""),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _skill_setting_from_row(row: aiosqlite.Row) -> SkillSetting:
        return SkillSetting(
            skill_id=str(row["skill_id"]),
            enabled=bool(row["enabled"]),
            min_role=str(row["min_role"]),
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Brain
    # ------------------------------------------------------------------
    async def create_brain_note(
        self,
        *,
        title: str,
        content: str,
        tags: str,
        scope_type: str,
        scope_id: str,
        visibility: str,
        source: str,
        source_user_id: str | None,
    ) -> BrainNote:
        now = utcnow()
        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO brain_notes (
                    title, content, tags, scope_type, scope_id, visibility,
                    source, source_user_id, byte_size, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    content,
                    tags,
                    scope_type,
                    scope_id,
                    visibility,
                    source,
                    source_user_id,
                    len(content.encode("utf-8")),
                    now,
                    now,
                ),
            )
            note_id = int(cursor.lastrowid or 0)
            await self.conn.commit()
        note = await self.get_brain_note(note_id)
        if note is None:
            raise RuntimeError("Failed to create brain note")
        return note

    async def get_brain_note(self, note_id: int) -> BrainNote | None:
        row = await self._one("SELECT * FROM brain_notes WHERE id = ?", (int(note_id),))
        return self._brain_note_from_row(row) if row else None

    async def find_brain_note(self, *, title: str, scope_type: str, scope_id: str) -> BrainNote | None:
        row = await self._one(
            """
            SELECT * FROM brain_notes
            WHERE title = ? AND scope_type = ? AND scope_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (title, scope_type, scope_id),
        )
        return self._brain_note_from_row(row) if row else None

    async def list_brain_notes(
        self,
        *,
        query: str = "",
        scope_type: str | None = None,
        scope_id: str | None = None,
        visibility: str | None = None,
        limit: int = 100,
    ) -> list[BrainNote]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type:
            clauses.append("scope_type = ?")
            params.append(scope_type)
            if scope_type != "global":
                clauses.append("scope_id = ?")
                params.append(scope_id or "")
        if visibility:
            clauses.append("visibility = ?")
            params.append(visibility)
        if query.strip():
            like = f"%{query.strip()}%"
            clauses.append("(title LIKE ? OR tags LIKE ? OR content LIKE ?)")
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 2000)))
        rows = await self._all(
            f"SELECT * FROM brain_notes {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        return [self._brain_note_from_row(row) for row in rows]

    async def search_brain_notes(
        self,
        query: str,
        *,
        scope_type: str,
        scope_id: str,
        limit: int = 3,
    ) -> list[BrainNote]:
        like = f"%{query.strip()}%"
        rows = await self._all(
            """
            SELECT * FROM brain_notes
            WHERE visibility != 'owner'
              AND (scope_type = 'global' OR (scope_type = ? AND scope_id = ?))
              AND (title LIKE ? OR tags LIKE ? OR content LIKE ?)
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (scope_type, scope_id, like, like, like, max(1, min(int(limit), 20))),
        )
        return [self._brain_note_from_row(row) for row in rows]

    async def update_brain_note(
        self,
        note_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: str | None = None,
        visibility: str | None = None,
    ) -> BrainNote | None:
        note = await self.get_brain_note(note_id)
        if note is None:
            return None
        new_content = note.content if content is None else content
        await self._write(
            """
            UPDATE brain_notes SET
                title = ?, content = ?, tags = ?, visibility = ?,
                byte_size = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                note.title if title is None else title,
                new_content,
                note.tags if tags is None else tags,
                note.visibility if visibility is None else visibility,
                len(new_content.encode("utf-8")),
                utcnow(),
                int(note_id),
            ),
        )
        return await self.get_brain_note(note_id)

    async def delete_brain_note(self, note_id: int) -> BrainNote | None:
        note = await self.get_brain_note(note_id)
        if note is None:
            return None
        await self._write("DELETE FROM brain_notes WHERE id = ?", (int(note_id),))
        return note

    async def list_global_brain_notes(self, *, limit: int = 20) -> list[BrainNote]:
        rows = await self._all(
            """
            SELECT * FROM brain_notes
            WHERE visibility = 'global'
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        )
        return [self._brain_note_from_row(row) for row in rows]

    async def count_brain_notes(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM brain_notes")
        return int(row["n"]) if row else 0

    async def sum_brain_bytes(self) -> int:
        row = await self._one("SELECT COALESCE(SUM(byte_size), 0) AS n FROM brain_notes")
        return int(row["n"]) if row else 0

    @staticmethod
    def _brain_note_from_row(row: aiosqlite.Row) -> BrainNote:
        return BrainNote(
            id=int(row["id"]),
            title=str(row["title"]),
            content=str(row["content"]),
            tags=str(row["tags"] or ""),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"] or ""),
            visibility=str(row["visibility"]),
            source=str(row["source"]),
            source_user_id=row["source_user_id"],
            byte_size=int(row["byte_size"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Exploration
    # ------------------------------------------------------------------
    async def add_explore_finding(
        self,
        action: str,
        *,
        category: str = "",
        status: str = "ok",
        summary: str = "",
        detail: str = "",
    ) -> ExploreFinding:
        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO explore_findings (action, category, status, summary, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(action)[:120],
                    str(category)[:40],
                    str(status)[:20],
                    str(summary)[:300],
                    str(detail)[:2000],
                    utcnow(),
                ),
            )
            await self.conn.commit()
            finding_id = int(cursor.lastrowid or 0)
        row = await self._one("SELECT * FROM explore_findings WHERE id = ?", (finding_id,))
        if row is None:
            raise RuntimeError("Failed to save explore finding")
        return self._explore_finding_from_row(row)

    async def list_explore_findings(self, *, limit: int = 100) -> list[ExploreFinding]:
        rows = await self._all(
            "SELECT * FROM explore_findings ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )
        return [self._explore_finding_from_row(row) for row in rows]

    async def clear_explore_findings(self) -> int:
        return await self._write("DELETE FROM explore_findings")

    @staticmethod
    def _explore_finding_from_row(row: aiosqlite.Row) -> ExploreFinding:
        return ExploreFinding(
            id=int(row["id"]),
            action=str(row["action"]),
            category=str(row["category"] or ""),
            status=str(row["status"] or "ok"),
            summary=str(row["summary"] or ""),
            detail=str(row["detail"] or ""),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Reply feedback / de-AI logs / style evolution
    # ------------------------------------------------------------------
    async def add_reply_feedback(
        self,
        *,
        scope_type: str,
        scope_id: str,
        reply_msg_id: str = "",
        persona_version: int = 0,
        deai_score: int = 0,
        signals: dict[str, Any],
        score: int,
    ) -> None:
        await self._write(
            "INSERT INTO reply_feedback (ts, scope_type, scope_id, reply_msg_id, persona_version,"
            " deai_score, signals, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                scope_type,
                scope_id,
                reply_msg_id,
                int(persona_version),
                int(deai_score),
                json.dumps(signals, ensure_ascii=False),
                int(max(-100, min(100, score))),
            ),
        )

    async def list_reply_feedback(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reply_feedback"
        params: list[Any] = []
        if scope_type and scope_id:
            sql += " WHERE scope_type = ? AND scope_id = ?"
            params.extend([scope_type, scope_id])
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        rows = await self._all(sql, tuple(params))
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                signals = json.loads(row["signals"] or "{}")
            except json.JSONDecodeError:
                signals = {}
            result.append(
                {
                    "id": int(row["id"]),
                    "ts": row["ts"],
                    "scope_type": row["scope_type"],
                    "scope_id": row["scope_id"],
                    "persona_version": int(row["persona_version"]),
                    "deai_score": int(row["deai_score"]),
                    "signals": signals,
                    "score": int(row["score"]),
                }
            )
        return result

    async def feedback_stats(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        since: str = "",
    ) -> dict[str, Any]:
        sql = "SELECT COUNT(*) AS n, AVG(score) AS avg_score FROM reply_feedback WHERE ts >= ?"
        params: list[Any] = [since]
        if scope_type and scope_id:
            sql += " AND scope_type = ? AND scope_id = ?"
            params.extend([scope_type, scope_id])
        row = await self._one(sql, tuple(params))
        count = int(row["n"]) if row else 0
        avg = float(row["avg_score"]) if row and row["avg_score"] is not None else 0.0
        return {"count": count, "avg_score": round(avg, 1)}

    async def add_deai_log(
        self,
        *,
        scope_type: str,
        scope_id: str,
        score: int,
        hit_rules: list[str],
        rewritten: int,
        reply_chars: int,
    ) -> None:
        await self._write(
            "INSERT INTO deai_logs (ts, scope_type, scope_id, score, hit_rules, rewritten, reply_chars)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                scope_type,
                scope_id,
                int(score),
                json.dumps(hit_rules, ensure_ascii=False),
                int(rewritten),
                int(reply_chars),
            ),
        )

    async def list_deai_logs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM deai_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                rules = json.loads(row["hit_rules"] or "[]")
            except json.JSONDecodeError:
                rules = []
            result.append(
                {
                    "id": int(row["id"]),
                    "ts": row["ts"],
                    "scope_type": row["scope_type"],
                    "scope_id": row["scope_id"],
                    "score": int(row["score"]),
                    "hit_rules": rules,
                    "rewritten": int(row["rewritten"]),
                    "reply_chars": int(row["reply_chars"]),
                }
            )
        return result

    async def deai_stats(self, *, since: str = "") -> dict[str, Any]:
        row = await self._one(
            "SELECT COUNT(*) AS n, AVG(score) AS avg_score, SUM(rewritten) AS rewrites"
            " FROM deai_logs WHERE ts >= ?",
            (since,),
        )
        count = int(row["n"]) if row else 0
        rules_rows = await self._all(
            "SELECT hit_rules FROM deai_logs WHERE ts >= ?",
            (since,),
        )
        counter: dict[str, int] = {}
        for item in rules_rows:
            try:
                rules = json.loads(item["hit_rules"] or "[]")
            except json.JSONDecodeError:
                continue
            for rule in rules:
                counter[str(rule)] = counter.get(str(rule), 0) + 1
        top = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "count": count,
            "avg_score": round(float(row["avg_score"]), 1) if row and row["avg_score"] is not None else 0.0,
            "rewrites": int(row["rewrites"] or 0) if row else 0,
            "top_rules": [{"rule": name, "hits": hits} for name, hits in top],
        }

    async def add_style_evolution(
        self,
        *,
        scope_type: str,
        scope_id: str,
        from_version: int,
        to_version: int,
        trigger: str,
        fields_changed: list[str],
        diagnosis: str,
        feedback_before: float,
        feedback_after: float,
        deai_before: float,
        deai_after: float,
    ) -> None:
        await self._write(
            "INSERT INTO style_evolution (ts, scope_type, scope_id, from_version, to_version,"
            " trigger, fields_changed, diagnosis, feedback_before, feedback_after,"
            " deai_before, deai_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                scope_type,
                scope_id,
                int(from_version),
                int(to_version),
                trigger,
                json.dumps(fields_changed, ensure_ascii=False),
                diagnosis[:200],
                float(feedback_before),
                float(feedback_after),
                float(deai_before),
                float(deai_after),
            ),
        )

    async def list_style_evolution(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM style_evolution ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                fields = json.loads(row["fields_changed"] or "[]")
            except json.JSONDecodeError:
                fields = []
            result.append(
                {
                    "id": int(row["id"]),
                    "ts": row["ts"],
                    "scope_type": row["scope_type"],
                    "scope_id": row["scope_id"],
                    "from_version": int(row["from_version"]),
                    "to_version": int(row["to_version"]),
                    "trigger": row["trigger"],
                    "fields_changed": fields,
                    "diagnosis": row["diagnosis"],
                    "feedback_before": float(row["feedback_before"]),
                    "feedback_after": float(row["feedback_after"]),
                    "deai_before": float(row["deai_before"]),
                    "deai_after": float(row["deai_after"]),
                }
            )
        return result

    async def latest_style_evolution(
        self,
        *,
        scope_type: str,
        scope_id: str,
    ) -> dict[str, Any] | None:
        row = await self._one(
            "SELECT * FROM style_evolution WHERE scope_type = ? AND scope_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (scope_type, scope_id),
        )
        if row is None:
            return None
        items = await self.list_style_evolution(limit=100)
        for item in items:
            if item["id"] == int(row["id"]):
                return item
        return None

    async def fill_style_evolution_after(
        self,
        evolution_id: int,
        *,
        feedback_after: float,
        deai_after: float,
    ) -> None:
        await self._write(
            "UPDATE style_evolution SET feedback_after = ?, deai_after = ? WHERE id = ?",
            (float(feedback_after), float(deai_after), int(evolution_id)),
        )

    # ------------------------------------------------------------------
    # Social
    # ------------------------------------------------------------------
    async def save_social_post(
        self,
        *,
        tid: str,
        content: str,
        ugc_right: int,
        source: str,
        status: str = "posted",
        error: str = "",
    ) -> SocialPost:
        await self._write(
            """
            INSERT INTO social_posts (tid, content, ugc_right, source, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tid) DO UPDATE SET
                content = excluded.content,
                ugc_right = excluded.ugc_right,
                source = excluded.source,
                status = excluded.status,
                error = excluded.error
            """,
            (tid, content, int(ugc_right), source, status, str(error)[:500], utcnow()),
        )
        post = await self.get_social_post(tid)
        if post is None:
            raise RuntimeError("Failed to save social post")
        return post

    async def get_social_post(self, tid: str) -> SocialPost | None:
        row = await self._one("SELECT * FROM social_posts WHERE tid = ?", (tid,))
        return self._social_post_from_row(row) if row else None

    async def list_social_posts(self, *, limit: int = 50) -> list[SocialPost]:
        rows = await self._all(
            "SELECT * FROM social_posts ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        return [self._social_post_from_row(row) for row in rows]

    async def mark_social_post_deleted(self, tid: str) -> bool:
        changed = await self._write(
            "UPDATE social_posts SET status = 'deleted', deleted_at = ? WHERE tid = ?",
            (utcnow(), tid),
        )
        return bool(changed)

    async def count_social_posts_since(self, since: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM social_posts WHERE created_at >= ? AND status = 'posted'",
            (since,),
        )
        return int(row["n"]) if row else 0

    async def get_social_state(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM social_state WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    async def set_social_state(self, key: str, value: str) -> None:
        await self._write(
            """
            INSERT INTO social_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, utcnow()),
        )

    @staticmethod
    def _social_post_from_row(row: aiosqlite.Row) -> SocialPost:
        return SocialPost(
            tid=str(row["tid"]),
            content=str(row["content"]),
            ugc_right=int(row["ugc_right"] or 1),
            source=str(row["source"]),
            status=str(row["status"]),
            error=str(row["error"] or ""),
            created_at=row["created_at"],
            deleted_at=row["deleted_at"],
        )

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------
    async def log_usage(
        self,
        scope_type: str,
        scope_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        await self._write(
            """
            INSERT INTO usage_logs (scope_type, scope_id, model, input_tokens, output_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scope_type, scope_id, model, input_tokens, output_tokens, utcnow()),
        )

    # ------------------------------------------------------------------
    # Stickers
    # ------------------------------------------------------------------
    async def add_sticker(
        self,
        *,
        sha256: str,
        filename: str,
        mime: str,
        byte_size: int,
        width: int | None,
        height: int | None,
        tags: str,
        description: str,
        source_scope_type: str | None,
        source_scope_id: str | None,
        source_user_id: str | None,
        source_nickname: str | None,
        source_message_id: str | None,
    ) -> tuple[StickerRecord, bool]:
        """Insert a sticker unless its hash already exists.

        Returns ``(record, created)``.  The caller owns the file and should
        remove its temporary copy when ``created`` is false.
        """
        inserted = await self._write(
            """
            INSERT OR IGNORE INTO stickers (
                sha256, filename, mime, byte_size, width, height, tags, description,
                source_scope_type, source_scope_id, source_user_id, source_nickname,
                source_message_id, enabled, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                sha256,
                filename,
                mime,
                byte_size,
                width,
                height,
                tags,
                description,
                source_scope_type,
                source_scope_id,
                source_user_id,
                source_nickname,
                source_message_id,
                utcnow(),
            ),
        )
        record = await self.get_sticker_by_hash(sha256)
        if record is None:
            raise RuntimeError("Failed to insert sticker")
        return record, bool(inserted)

    async def get_sticker_by_hash(self, sha256: str) -> StickerRecord | None:
        row = await self._one("SELECT * FROM stickers WHERE sha256 = ?", (sha256,))
        return self._sticker_from_row(row) if row else None

    async def get_sticker(self, sticker_id: int) -> StickerRecord | None:
        row = await self._one("SELECT * FROM stickers WHERE id = ?", (sticker_id,))
        return self._sticker_from_row(row) if row else None

    async def list_stickers(self, *, limit: int = 100, query: str = "", enabled_only: bool = False) -> list[StickerRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if query.strip():
            clauses.append("(tags LIKE ? OR description LIKE ? OR source_nickname LIKE ?)")
            like = f"%{query.strip()}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._all(
            f"SELECT * FROM stickers {where} ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(limit, 500))),
        )
        return [self._sticker_from_row(row) for row in rows]

    async def count_stickers(self, *, enabled_only: bool = False) -> int:
        where = "WHERE enabled = 1" if enabled_only else ""
        row = await self._one(f"SELECT COUNT(*) AS n FROM stickers {where}")
        return int(row["n"] or 0) if row else 0

    async def update_sticker(
        self,
        sticker_id: int,
        *,
        tags: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if tags is not None:
            sets.append("tags = ?")
            params.append(tags)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if not sets:
            return False
        params.append(sticker_id)
        return bool(await self._write(f"UPDATE stickers SET {', '.join(sets)} WHERE id = ?", tuple(params)))

    async def delete_sticker(self, sticker_id: int) -> bool:
        return bool(await self._write("DELETE FROM stickers WHERE id = ?", (sticker_id,)))

    async def record_sticker_send(self, sticker_id: int, scope_type: str, scope_id: str) -> None:
        now = utcnow()
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE stickers SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (now, sticker_id),
            )
            await self.conn.execute(
                "INSERT INTO sticker_sends (sticker_id, scope_type, scope_id, created_at) VALUES (?, ?, ?, ?)",
                (sticker_id, scope_type, scope_id, now),
            )
            await self.conn.commit()

    async def last_sticker_send_at(self, scope_type: str, scope_id: str) -> str | None:
        row = await self._one(
            """
            SELECT created_at FROM sticker_sends
            WHERE scope_type = ? AND scope_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (scope_type, scope_id),
        )
        return str(row["created_at"]) if row else None

    async def count_sticker_sends(self, scope_type: str, scope_id: str, *, since: str) -> int:
        row = await self._one(
            """
            SELECT COUNT(*) AS n FROM sticker_sends
            WHERE scope_type = ? AND scope_id = ? AND created_at >= ?
            """,
            (scope_type, scope_id, since),
        )
        return int(row["n"] or 0) if row else 0

    @staticmethod
    def _sticker_from_row(row: aiosqlite.Row) -> StickerRecord:
        return StickerRecord(
            id=row["id"],
            sha256=row["sha256"],
            filename=row["filename"],
            mime=row["mime"],
            byte_size=int(row["byte_size"]),
            width=row["width"],
            height=row["height"],
            tags=row["tags"],
            description=row["description"],
            source_scope_type=row["source_scope_type"],
            source_scope_id=row["source_scope_id"],
            source_user_id=row["source_user_id"],
            source_nickname=row["source_nickname"],
            source_message_id=row["source_message_id"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=int(row["use_count"] or 0),
        )

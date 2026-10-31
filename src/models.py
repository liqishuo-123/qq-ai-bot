from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class GroupState:
    group_id: str
    active: bool
    mode: str
    activated_by: str | None
    activated_at: str | None
    last_bot_reply_at: str | None
    last_bot_reply_user: str | None
    persona_id: str | None


@dataclass(slots=True)
class UserState:
    user_id: str
    nickname: str | None
    private_active: bool
    persona_id: str | None


@dataclass(slots=True)
class SessionState:
    id: int
    scope_type: str
    scope_id: str
    summary: str


@dataclass(slots=True)
class StoredMessage:
    id: int
    role: str
    content: str
    user_id: str | None
    nickname: str | None
    created_at: str


@dataclass(slots=True)
class StickerRecord:
    id: int
    sha256: str
    filename: str
    mime: str
    byte_size: int
    width: int | None
    height: int | None
    tags: str
    description: str
    source_scope_type: str | None
    source_scope_id: str | None
    source_user_id: str | None
    source_nickname: str | None
    source_message_id: str | None
    enabled: bool
    created_at: str
    last_used_at: str | None
    use_count: int

    @property
    def tag_list(self) -> list[str]:
        return [item.strip() for item in self.tags.split(",") if item.strip()]


@dataclass(slots=True)
class StyleProfile:
    id: int
    scope_type: str
    scope_id: str
    profile: dict[str, Any]
    sample_count: int
    confidence: float
    enabled: bool
    version: int
    created_at: str
    updated_at: str

    @property
    def summary(self) -> str:
        value = self.profile.get("summary")
        return str(value) if value else ""


@dataclass(slots=True)
class StyleLearningState:
    scope_type: str
    scope_id: str
    last_message_id: int
    samples_seen: int
    last_learned_at: str | None
    updated_at: str


@dataclass(slots=True)
class SkillSetting:
    skill_id: str
    enabled: bool
    min_role: str
    updated_at: str


@dataclass(slots=True)
class BrainNote:
    id: int
    title: str
    content: str
    tags: str
    scope_type: str
    scope_id: str
    visibility: str
    source: str
    source_user_id: str | None
    byte_size: int
    created_at: str
    updated_at: str

    @property
    def tag_list(self) -> list[str]:
        return [item.strip() for item in self.tags.split(",") if item.strip()]


@dataclass(slots=True)
class ExploreFinding:
    id: int
    action: str
    category: str
    status: str
    summary: str
    detail: str
    created_at: str


@dataclass(slots=True)
class SocialPost:
    tid: str
    content: str
    ugc_right: int
    source: str
    status: str
    error: str
    created_at: str
    deleted_at: str | None


@dataclass(slots=True)
class MemoryItem:
    id: int
    scope_type: str
    scope_id: str
    user_id: str | None
    content: str
    category: str
    confidence: float
    created_at: str
    expires_at: str | None

from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from src.db import Database
from src.settings import Settings
from src.skills import SkillDenied, SkillService


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "superusers": ["90001"],
    }
    values.update(overrides)
    return Settings(**values)


def make_event(user_id: int, role: str = "member", group_id: int = 123456) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=10001,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        message=Message("hi"),
        original_message=Message("hi"),
        raw_message="hi",
        font=0,
        sender=Sender(user_id=user_id, role=role),
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_skill_defaults_all_open_and_console_can_tighten(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = SkillService(make_settings(tmp_path), db)
        await service.ensure_defaults()

        enabled, role = await service.effective_setting("group.kick", "group", "123456")
        assert enabled is True
        assert role == "member"

        saved = await service.set_global("group.mute", enabled=False, min_role="group_owner")
        assert saved["enabled"] is False
        assert saved["min_role"] == "group_owner"

        await service.set_grant(
            "group",
            "123456",
            "brain.write",
            enabled=False,
            min_role="admin",
        )
        enabled, role = await service.effective_setting("brain.write", "group", "123456")
        assert enabled is False
        assert role == "admin"
        enabled, role = await service.effective_setting("brain.write", "group", "999999")
        assert enabled is True
        assert role == "member"

        saved = await service.set_grant(
            "group",
            "123456",
            "brain.delete",
            enabled=True,
            min_role="member",
        )
        assert saved["min_role"] == "member"
        assert await service.clear_grant("group", "123456", "brain.write") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_skill_role_and_scope_checks(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = SkillService(make_settings(tmp_path), db)
        await service.ensure_defaults()

        admin_event = make_event(20001, role="admin")
        member_event = make_event(20002, role="member")
        owner_event = make_event(90001, role="member")
        assert service.role_for_event(admin_event) == "admin"
        assert service.role_for_event(member_event) == "member"
        assert service.role_for_event(owner_event) == "owner"

        await service.check("group.mute", member_event)
        await service.set_global("group.mute", enabled=True, min_role="admin")
        await service.check("group.mute", admin_event)
        with pytest.raises(SkillDenied):
            await service.check("group.mute", member_event)

        await service.set_global("profile.apply", enabled=True, min_role="owner")
        with pytest.raises(SkillDenied):
            await service.check("profile.apply", admin_event)

        await service.set_global("brain.search", enabled=False, min_role="member")
        with pytest.raises(SkillDenied):
            await service.check("brain.search", member_event)

        await service.set_global("group.mute", enabled=False, min_role="group_owner")
        items = await service.list_effective("group", "123456")
        from src.skills import SKILL_DEFS

        assert len(items) == len(SKILL_DEFS)
        mute = next(item for item in items if item["id"] == "group.mute")
        assert mute["enabled"] is False
        assert mute["effective_min_role"] == "group_owner"
        assert mute["overridden"] is False
    finally:
        await db.close()

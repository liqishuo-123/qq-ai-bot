from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from src.group_admin import (
    GroupActionError,
    GroupAdminService,
    parse_duration,
    target_from_args,
    target_from_event,
)


def make_event(user_id: int = 20001, group_id: int = 123456) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=10001,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=10,
        message=Message("hi"),
        original_message=Message("hi"),
        raw_message="hi",
        font=0,
        sender=Sender(user_id=user_id, role="admin"),
        group_id=group_id,
    )


class FakeBot:
    def __init__(self) -> None:
        self.self_id = "10001"
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_group_member_info":
            return {"role": "admin" if str(kwargs["user_id"]) == self.self_id else "member"}
        return {}


def test_parse_duration_and_target_args() -> None:
    assert parse_duration("10m") == 600
    assert parse_duration("1h") == 3600
    assert parse_duration("30") == 30
    assert parse_duration("2天") == 172800
    with pytest.raises(GroupActionError):
        parse_duration("很久")

    target, rest = target_from_args("20002 10m")
    assert target == "20002"
    assert rest == "10m"
    target, rest = target_from_args("10m", explicit="20002")
    assert target == "20002"
    assert rest == "10m"
    assert target_from_args("10m") == (None, "10m")


def test_target_from_event_prefers_reply() -> None:
    event = make_event()
    event.reply = SimpleNamespace(
        sender=SimpleNamespace(user_id=20003),
        message_id=99,
    )
    assert target_from_event(event) == "20003"


@pytest.mark.asyncio
async def test_group_admin_mute_and_protection(tmp_path: Path) -> None:
    service = GroupAdminService({"90001"})
    bot = FakeBot()
    event = make_event()

    await service.mute(bot, event, "20002", 600)
    assert bot.calls[-1] == (
        "set_group_ban",
        {"group_id": 123456, "user_id": 20002, "duration": 600},
    )

    with pytest.raises(GroupActionError):
        await service.guard_target(bot, event, "90001")
    with pytest.raises(GroupActionError):
        await service.guard_target(bot, event, "20001")
    with pytest.raises(GroupActionError):
        await service.guard_target(bot, event, "10001")

    with pytest.raises(GroupActionError):
        await service.require_owner(bot, event)

from pathlib import Path

import pytest

from src.actions import ActionError, ActionService, _sanitize_params
from src.db import Database
from src.qzone import QzonePost
from src.settings import Settings
from src.skills import SKILL_DEFS, SkillService


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, **params):
        self.calls.append((action, params))
        return {"ok": True, "action": action}


class FakeQzone:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_posts(self, bot, *, target_uin=None, limit=20, pos=0):
        self.calls.append(("list_posts", {"target_uin": target_uin, "limit": limit, "pos": pos}))
        return [
            QzonePost(
                tid="tid1",
                unikey="unikey1",
                content="第一条",
                created_at="2026-10-31T10:00:00+08:00",
            )
        ]

    async def set_like(self, bot, *, tid, active=True, unikey=""):
        self.calls.append(("set_like", {"tid": tid, "active": active}))
        return {"tid": tid, "active": active}

    async def comment(self, bot, *, tid, content):
        self.calls.append(("comment", {"tid": tid, "content": content}))
        return {"tid": tid, "comment_id": "c1"}

    async def delete_comment(self, bot, *, tid, comment_id):
        self.calls.append(("delete_comment", {"tid": tid, "comment_id": comment_id}))
        return {"tid": tid, "comment_id": comment_id}


class FakeGuild:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def status(self, bot=None):
        self.calls.append(("status", {}))
        return {"backend": "auto"}

    async def list_channels(self, bot=None):
        self.calls.append(("list_channels", {}))
        return {"backend": "official", "channels": []}

    async def history(self, bot, *, channel_id=None, limit=20):
        self.calls.append(("history", {"channel_id": channel_id, "limit": limit}))
        return {"backend": "official", "messages": []}

    async def send(self, bot, content, *, channel_id=None):
        self.calls.append(("send", {"content": content, "channel_id": channel_id}))
        return {"message_id": "m1"}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)


async def make_service(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    skills = SkillService(settings, db)
    await skills.ensure_defaults()
    qzone = FakeQzone()
    guild = FakeGuild()
    service = ActionService(settings, skills, qzone, guild)
    return service, db


def test_api_catalog_is_populated_and_unique() -> None:
    api_ids = [item.skill_id for item in SKILL_DEFS.values() if item.executor == "onebot"]
    assert len(api_ids) >= 100
    assert len(api_ids) == len(set(api_ids))
    for skill_id in api_ids:
        skill = SKILL_DEFS[skill_id]
        assert skill.skill_id.startswith("api.")
        assert skill.readonly or skill.risk in {"low", "medium", "high"}


def test_qzone_guild_skills_have_executors() -> None:
    assert SKILL_DEFS["qzone.list"].executor == "qzone"
    assert SKILL_DEFS["qzone.list"].readonly is True
    assert SKILL_DEFS["guild.send"].executor == "guild"
    assert SKILL_DEFS["guild.send"].readonly is False


def test_sanitize_params_rejects_junk() -> None:
    assert _sanitize_params(None) == {}
    assert _sanitize_params({"a": 1}) == {"a": 1}
    with pytest.raises(ActionError):
        _sanitize_params("not a dict")
    with pytest.raises(ActionError):
        _sanitize_params({"bad\nname": 1})
    with pytest.raises(ActionError):
        _sanitize_params({"a": "x" * 5000})


@pytest.mark.asyncio
async def test_execute_onebot_action_and_audit(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    bot = FakeBot()
    result = await service.execute(
        "api.get_group_list", params={}, bot=bot, role="owner", actor="console"
    )
    assert result["executor"] == "onebot"
    assert result["result"]["action"] == "get_group_list"
    assert bot.calls and bot.calls[0][0] == "get_group_list"
    audit = await db.list_skill_audit()
    assert audit and audit[0]["skill_id"] == "api.get_group_list"
    assert audit[0]["status"] == "ok"
    await db.close()


@pytest.mark.asyncio
async def test_execute_denied_by_min_role(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    await db.upsert_skill_setting("api.get_friend_list", enabled=True, min_role="owner")
    with pytest.raises(ActionError) as exc:
        await service.execute(
            "api.get_friend_list", params={}, bot=FakeBot(), role="member"
        )
    assert "权限" in str(exc.value)
    audit = await db.list_skill_audit()
    assert audit and audit[0]["status"] == "denied"
    await db.close()


@pytest.mark.asyncio
async def test_execute_disabled_skill_denied(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    await db.upsert_skill_setting("api.get_status", enabled=False, min_role="member")
    with pytest.raises(ActionError):
        await service.execute("api.get_status", params={}, bot=FakeBot(), role="owner")
    await db.close()


@pytest.mark.asyncio
async def test_execute_requires_connected_bot(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    with pytest.raises(ActionError) as exc:
        await service.execute("api.get_login_info", params={}, bot=None)
    assert "NapCat" in str(exc.value)
    await db.close()


@pytest.mark.asyncio
async def test_execute_rejects_core_and_unknown(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    with pytest.raises(ActionError):
        service  # noqa: B018 - 先确认服务可用
        await service.execute("group.kick", params={}, bot=FakeBot())
    with pytest.raises(ActionError):
        await service.execute("api.not_exists_action", params={}, bot=FakeBot())
    with pytest.raises(ActionError):
        await service.execute("no_such_skill", params={}, bot=FakeBot())
    await db.close()


@pytest.mark.asyncio
async def test_execute_qzone_routing(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    result = await service.execute("qzone.list", params={"uin": "123456"}, role="owner")
    assert result["executor"] == "qzone"
    assert result["result"]["posts"][0]["tid"] == "tid1"
    result = await service.execute(
        "qzone.like", params={"tid": "tid1", "active": "1"}, role="owner"
    )
    assert result["result"]["active"] is True
    result = await service.execute(
        "qzone.comment", params={"tid": "tid1", "content": "不错"}, role="owner"
    )
    assert result["result"]["comment_id"] == "c1"
    await db.close()


@pytest.mark.asyncio
async def test_execute_guild_routing(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    result = await service.execute("guild.status", params={}, role="owner")
    assert result["result"]["backend"] == "auto"
    result = await service.execute("guild.list", params={}, role="owner")
    assert result["result"]["backend"] == "official"
    result = await service.execute(
        "guild.history", params={"channel_id": "123", "limit": 5}, role="owner"
    )
    assert result["result"]["backend"] == "official"
    with pytest.raises(ActionError):
        await service.execute("guild.send", params={}, role="owner")
    result = await service.execute(
        "guild.send", params={"content": "大家好", "channel_id": "123"}, role="owner"
    )
    assert result["result"]["message_id"] == "m1"
    await db.close()


@pytest.mark.asyncio
async def test_onebot_failure_is_audited(tmp_path: Path) -> None:
    class BoomBot:
        async def call_api(self, action: str, **params):
            raise RuntimeError("napcat exploded")

    service, db = await make_service(tmp_path)
    with pytest.raises(ActionError) as exc:
        await service.execute("api.get_version_info", params={}, bot=BoomBot())
    assert "get_version_info" in str(exc.value)
    audit = await db.list_skill_audit()
    assert audit and audit[0]["status"] == "failed"
    await db.close()

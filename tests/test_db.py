from pathlib import Path

import pytest

from src.db import Database


@pytest.mark.asyncio
async def test_group_session_and_memory_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        group = await db.ensure_group("10001")
        assert group.active is False
        assert group.mode == "smart"

        await db.activate_group("10001", "20001")
        group = await db.ensure_group("10001")
        assert group.active is True
        assert group.activated_by == "20001"

        await db.set_group_mode("10001", "full")
        assert (await db.ensure_group("10001")).mode == "full"

        await db.record_bot_reply("10001", "20001")
        group = await db.ensure_group("10001")
        assert group.last_bot_reply_user == "20001"
        assert db.seconds_since_last_bot_reply(group) is not None

        session = await db.get_or_create_session("group", "10001")
        await db.add_message(session.id, "user", "我喜欢猫", user_id="20001", nickname="小明")
        await db.add_message(session.id, "assistant", "猫挺好")
        messages = await db.get_messages(session.id)
        assert [item.content for item in messages] == ["我喜欢猫", "猫挺好"]
        assert await db.count_messages(session.id) == 2

        assert await db.mark_processed("group", "10001", "500") is True
        assert await db.mark_processed("group", "10001", "500") is False

        assert await db.add_memory("group", "10001", "喜欢猫", user_id="20001") is True
        assert await db.add_memory("group", "10001", "喜欢猫", user_id="20001") is False
        memories = await db.search_memories("group", "10001", "猫", user_id="20001")
        assert [item.content for item in memories] == ["喜欢猫"]

        assert await db.delete_user_memories("group", "10001", "20001") == 1
        assert await db.list_memories("group", "10001", user_id="20001") == []

        await db.clear_session("group", "10001")
        assert await db.count_messages(session.id) == 0
    finally:
        await db.close()

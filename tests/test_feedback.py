import asyncio  # noqa: E402
from pathlib import Path

import pytest

from src.db import Database
from src.feedback import FeedbackService
from src.settings import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "feedback_enabled": True,
        "feedback_observe_seconds": 300,
        "feedback_private_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


async def make_service(tmp_path: Path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    await db.connect()
    service = FeedbackService(settings, db)
    return service, db


async def drain(service: FeedbackService) -> None:
    """Let quick finalize tasks settle, then cancel the long observation windows."""
    await asyncio.sleep(0.1)
    service.close()


@pytest.mark.asyncio
async def test_register_and_positive_followup_commits(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="group", scope_id="123", user_id="u1", persona_version=3, deai_score=5)
    # 对方紧接着回了机器人两句
    service.observe(
        scope_type="group", scope_id="123", user_id="u1",
        text="你说的对", directed_at_bot=True,
    )
    service.observe(
        scope_type="group", scope_id="123", user_id="u1",
        text="然后呢", directed_at_bot=True,
    )
    # 手动提前结算，避免等满观察窗
    pending = service._pending.pop(("group", "123", "u1"))
    if pending.task:
        pending.task.cancel()
    await service._commit(pending)
    rows = await db.list_reply_feedback(scope_type="group", scope_id="123")
    assert len(rows) == 1
    row = rows[0]
    assert row["persona_version"] == 3
    assert row["deai_score"] == 5
    assert row["signals"]["replied"] is True
    assert row["signals"]["followup"] is True
    assert row["score"] > 0
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_explicit_neg_commits_early_and_sets_correction(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="group", scope_id="456", user_id="u2")
    service.observe(
        scope_type="group", scope_id="456", user_id="u2",
        text="你说话好像机器人啊", directed_at_bot=True,
    )
    await drain(service)
    rows = await db.list_reply_feedback(scope_type="group", scope_id="456")
    assert len(rows) == 1
    assert rows[0]["signals"]["explicit_neg"] is True
    # -25（explicit_neg）+ 12（对方紧接着的话是指向机器人的）= -13，仍为强负向
    assert rows[0]["score"] == -13
    hint = await service.correction_hint("group", "456")
    assert "机器人" in hint
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_command_cancel_counts_negative(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="private", scope_id="u3", user_id="u3")
    service.note_command_cancel(scope_type="private", scope_id="u3", user_id="u3")
    await drain(service)
    rows = await db.list_reply_feedback(scope_type="private", scope_id="u3")
    assert len(rows) == 1
    assert rows[0]["signals"]["command_cancel"] is True
    assert rows[0]["score"] <= -10
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_group_silence_is_mild_negative(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="group", scope_id="789", user_id="u4")
    pending = service._pending.pop(("group", "789", "u4"))
    if pending.task:
        pending.task.cancel()
    await service._commit(pending)
    rows = await db.list_reply_feedback(scope_type="group", scope_id="789")
    assert rows[0]["signals"]["left_silent"] is True
    assert rows[0]["score"] == -3
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_private_silence_is_neutral(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="private", scope_id="u5", user_id="u5")
    pending = service._pending.pop(("private", "u5", "u5"))
    if pending.task:
        pending.task.cancel()
    await service._commit(pending)
    rows = await db.list_reply_feedback(scope_type="private", scope_id="u5")
    assert rows[0]["score"] == 0
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_correction_hint_expires_after_ttl(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    service.register(scope_type="group", scope_id="111", user_id="u6")
    service.observe(
        scope_type="group", scope_id="111", user_id="u6",
        text="太官方了，说人话", directed_at_bot=True,
    )
    await drain(service)
    hint = await service.correction_hint("group", "111")
    assert hint
    # 手动把时间拨到 10 分钟前
    import time as _time

    event = service._corrections[("group", "111")]
    event.created_at = _time.monotonic() - 601
    hint = await service.correction_hint("group", "111")
    assert hint == ""
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_disabled_service_ignores_everything(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path, feedback_enabled=False)
    service.register(scope_type="group", scope_id="1", user_id="u7")
    service.observe(
        scope_type="group", scope_id="1", user_id="u7",
        text="像机器人", directed_at_bot=True,
    )
    await drain(service)
    assert await db.list_reply_feedback() == []
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_private_ignored_when_private_disabled(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path, feedback_private_enabled=False)
    service.register(scope_type="private", scope_id="u8", user_id="u8")
    assert ("private", "u8", "u8") not in service._pending
    await db.close()
    service.close()


@pytest.mark.asyncio
async def test_feedback_stores_no_message_text(tmp_path: Path) -> None:
    """反馈只存行为信号，对方消息原文绝不能落库。"""
    service, db = await make_service(tmp_path)
    secret = "我家住在幸福路 88 号"
    service.register(scope_type="group", scope_id="222", user_id="u9")
    service.observe(
        scope_type="group", scope_id="222", user_id="u9",
        text=secret, directed_at_bot=True,
    )
    pending = service._pending.pop(("group", "222", "u9"))
    if pending.task:
        pending.task.cancel()
    await service._commit(pending)
    rows = await db.list_reply_feedback(scope_type="group", scope_id="222")
    assert rows and secret not in str(rows[0]["signals"])
    # 全表扫描确认原文不落库
    all_rows = await db._all("SELECT * FROM reply_feedback")
    assert all(secret not in str(dict(row)) for row in all_rows)
    await db.close()
    service.close()

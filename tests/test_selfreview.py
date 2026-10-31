import asyncio
from pathlib import Path

import pytest

from src.brain import BrainService
from src.db import Database
from src.persona import Persona
from src.selfreview import SelfReviewService
from src.settings import Settings


class FakeLLM:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or default_payload()
        self.calls: list[dict] = []

    async def self_review(self, persona, profile, exchanges, feedback_stats, deai_stats):
        self.calls.append(
            {
                "profile": dict(profile),
                "exchanges": exchanges,
                "feedback_stats": dict(feedback_stats),
                "deai_stats": dict(deai_stats),
            }
        )
        return self.payload


def default_payload() -> dict:
    return {
        "diagnosis": "总用'总之'收尾，被群友说太官方",
        "patch": {"tone": "不总结、问句收尾", "avoid": ["总之"]},
        "brain_note": "在熟人群里别用书面总结句收尾",
        "confidence": 0.8,
    }


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "brain_dir": tmp_path / "brain",
        "style_min_confidence": 0.35,
        "style_retention_versions": 20,
        "selfreview_enabled": True,
        "selfreview_interval_messages": 120,
        "selfreview_min_gap_minutes": 1,
        "selfreview_neg_trigger": 3,
        "style_evolution_brain": True,
        "brain_enabled": True,
        "brain_max_notes": 2000,
        "brain_max_note_bytes": 65536,
        "brain_capture_max_memories": 20,
        "brain_context_chars": 700,
        "brain_search_limit": 3,
    }
    values.update(overrides)
    return Settings(**values)


def make_persona() -> Persona:
    return Persona(
        id="test",
        display_name="测试鱼",
        identity="测试",
        relationship="朋友",
        traits=["测试"],
        style={},
    )


async def make_service(tmp_path: Path, payload: dict | None = None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    await db.connect()
    brain = BrainService(settings, db)
    brain.ensure_dirs()
    service = SelfReviewService(settings, db, FakeLLM(payload), make_persona(), brain)
    return service, db, settings


async def seed_scope(db: Database, *, with_profile: bool = True) -> None:
    session = await db.get_or_create_session("group", "123456")
    await db.add_message(session.id, "user", "今天好累", user_id="u1", nickname="甲")
    await db.add_message(session.id, "assistant", "总之，先歇着吧。")
    await db.add_message(session.id, "user", "嗯", user_id="u1", nickname="甲")
    if with_profile:
        await db.upsert_style_profile(
            "group",
            "123456",
            profile={"summary": "旧画像", "tone": "口语", "confidence": 0.6},
            sample_count=50,
            confidence=0.6,
            enabled=True,
            version=4,
        )


@pytest.mark.asyncio
async def test_review_applies_patch_and_sediments(tmp_path: Path) -> None:
    service, db, settings = await make_service(tmp_path)
    await seed_scope(db)
    result = await service.review("group", "123456", trigger="manual", force=True)
    assert result["applied"] is True
    assert result["version_before"] == 4
    assert result["version_after"] == 5
    assert "tone" in result["fields_changed"]
    # 画像被更新
    profile = await db.get_style_profile("group", "123456")
    assert profile.version == 5
    assert profile.profile["tone"] == "不总结、问句收尾"
    # 旧版本被归档
    versions = await db.list_style_versions(profile.id)
    assert any(item["version"] == 4 for item in versions)
    # 演化日志
    evolution = await db.list_style_evolution()
    assert len(evolution) == 1
    assert evolution[0]["from_version"] == 4
    assert evolution[0]["to_version"] == 5
    assert "总之" in evolution[0]["diagnosis"]
    # 大脑沉淀
    notes = await db.list_brain_notes(scope_type="global", scope_id="")
    assert any("风格进化" in note.title for note in notes)
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_low_confidence_records_only(tmp_path: Path) -> None:
    service, db, _settings = await make_service(
        tmp_path, payload={**default_payload(), "confidence": 0.1}
    )
    await seed_scope(db)
    result = await service.review("group", "123456", trigger="manual", force=True)
    assert result["applied"] is False
    assert "置信度" in result["note"]
    profile = await db.get_style_profile("group", "123456")
    assert profile.version == 4
    evolution = await db.list_style_evolution()
    assert evolution[0]["to_version"] == 4
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_without_profile_records_diagnosis(tmp_path: Path) -> None:
    service, db, _settings = await make_service(tmp_path)
    await seed_scope(db, with_profile=False)
    result = await service.review("group", "123456", trigger="manual", force=True)
    assert result["applied"] is False
    assert "还没有风格画像" in result["note"]
    evolution = await db.list_style_evolution()
    assert len(evolution) == 1
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_backfills_previous_after_stats(tmp_path: Path) -> None:
    service, db, _settings = await make_service(tmp_path)
    await seed_scope(db)
    await service.review("group", "123456", trigger="manual", force=True)
    # 制造一些反馈和 AI 味数据后再次复盘
    await db.add_reply_feedback(
        scope_type="group", scope_id="123456", signals={"replied": True}, score=30
    )
    await db.add_deai_log(
        scope_type="group", scope_id="123456", score=10, hit_rules=["书面连接词"],
        rewritten=0, reply_chars=20,
    )
    await service.review("group", "123456", trigger="manual", force=True)
    evolution = await db.list_style_evolution()
    # 最早的一条 after 被回填
    oldest = evolution[-1]
    assert oldest["feedback_after"] > 0 or oldest["deai_after"] > 0
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_gap_enforced_without_force(tmp_path: Path) -> None:
    service, db, _settings = await make_service(
        tmp_path, selfreview_min_gap_minutes=30
    )
    await seed_scope(db)
    await service.review("group", "123456", trigger="manual", force=True)
    from src.selfreview import SelfReviewError

    with pytest.raises(SelfReviewError):
        await service.review("group", "123456", trigger="manual", force=False)
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_note_reply_triggers_auto_review(tmp_path: Path) -> None:
    service, db, _settings = await make_service(
        tmp_path, selfreview_interval_messages=2
    )
    await seed_scope(db)
    service.note_reply(scope_type="group", scope_id="123456")
    service.note_reply(scope_type="group", scope_id="123456")
    # schedule 是后台任务，等它跑完
    for _ in range(20):
        await asyncio.sleep(0.05)
        if await db.list_style_evolution():
            break
    evolution = await db.list_style_evolution()
    assert evolution and evolution[0]["trigger"] == "auto_review"
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_note_explicit_neg_triggers_after_threshold(tmp_path: Path) -> None:
    service, db, _settings = await make_service(
        tmp_path, selfreview_neg_trigger=2
    )
    await seed_scope(db)
    service.note_explicit_neg(scope_type="group", scope_id="123456")
    service.note_explicit_neg(scope_type="group", scope_id="123456")
    for _ in range(20):
        await asyncio.sleep(0.05)
        if await db.list_style_evolution():
            break
    evolution = await db.list_style_evolution()
    assert evolution and evolution[0]["trigger"] == "explicit_feedback"
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_brain_note_can_be_disabled(tmp_path: Path) -> None:
    service, db, _settings = await make_service(tmp_path, style_evolution_brain=False)
    await seed_scope(db)
    result = await service.review("group", "123456", trigger="manual", force=True)
    assert result["applied"] is True
    assert "brain_note_id" not in result
    notes = await db.list_brain_notes(scope_type="global", scope_id="")
    assert not any("风格进化" in note.title for note in notes)
    await db.close()
    await service.close()


@pytest.mark.asyncio
async def test_review_invalid_json_payload_is_safe(tmp_path: Path) -> None:
    service, db, _settings = await make_service(
        tmp_path, payload={"diagnosis": "", "patch": "不是字典", "confidence": "abc"}
    )
    await seed_scope(db)
    result = await service.review("group", "123456", trigger="manual", force=True)
    assert result["applied"] is False
    profile = await db.get_style_profile("group", "123456")
    assert profile.version == 4
    await db.close()
    await service.close()

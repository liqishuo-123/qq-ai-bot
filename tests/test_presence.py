import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.brain import BrainService
from src.db import Database
from src.persona import Persona
from src.policy import PolicyViolation
from src.presence import PresenceError, PresenceService, SelfProfile
from src.settings import Settings


class FakeLLM:
    async def extract_profile(self, persona, brain_text, old_profile):
        return (
            {
                "nickname": "大肥鱼",
                "signature": "今天也在认真摸鱼",
                "bio": "一条住在群里的鱼，偶尔说点人话，更多时候在讨饭。",
                "tags": ["摸鱼", "群友", "嘴硬"],
                "mood": "饿",
            },
            SimpleNamespace(model="fake", input_tokens=12, output_tokens=8),
        )

    async def compose_qzone_post(self, persona, profile, brain_text):
        return (
            "今天也没干什么正事，摸了半天鱼，晚上再想吃什么。",
            SimpleNamespace(model="fake", input_tokens=9, output_tokens=7),
        )


class FakeBot:
    def __init__(self) -> None:
        self.self_id = "10001"
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if action == "send_qzone_msg":
            return {"tid": "tid-001"}
        if action == "set_qq_profile":
            return {}
        if action == "delete_qzone_msg":
            return {}
        return {}


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "brain_dir": tmp_path / "brain",
        "qzone_max_chars": 300,
    }
    values.update(overrides)
    return Settings(**values)


def make_persona() -> Persona:
    return Persona(
        id="dafeiyu",
        display_name="大肥鱼",
        identity="群友",
        relationship="熟人",
        traits=["嘴硬", "爱摸鱼"],
        style={"tone": "口语"},
    )


async def make_service(tmp_path: Path) -> tuple[Database, BrainService, PresenceService]:
    settings = make_settings(tmp_path)
    db = Database(tmp_path / "bot.db")
    await db.connect()
    brain = BrainService(settings, db)
    brain.ensure_dirs()
    service = PresenceService(settings, db, brain, make_persona(), FakeLLM())
    service.ensure_dirs()
    return db, brain, service


@pytest.mark.asyncio
async def test_profile_generate_apply_and_restore(tmp_path: Path) -> None:
    db, _brain, service = await make_service(tmp_path)
    bot = FakeBot()
    try:
        generated = await service.generate_profile()
        assert generated.nickname == "大肥鱼"
        assert generated.tags == ["摸鱼", "群友", "嘴硬"]
        assert generated.source == "auto"

        await service.save_draft(generated)
        applied = await service.apply_profile(bot, generated)
        assert applied.nickname == "大肥鱼"
        assert service.nickname == "大肥鱼"
        assert service.current_path.is_file()
        assert bot.calls[0][0] == "set_qq_profile"
        assert bot.calls[0][1]["nickname"] == "大肥鱼"
        history = service.list_history()
        assert len(history) == 1
        version = history[0]["version"]

        changed = await service.save_draft(
            SelfProfile(
                nickname="肥鱼二号",
                signature="换了个签名",
                bio="今天换了个简介，还是那条鱼。",
                tags=["鱼"],
                mood="正常",
            )
        )
        assert changed.nickname == "肥鱼二号"
        await service.apply_profile(bot, changed)
        assert len(service.list_history()) >= 1

        restored = await service.restore_history(version, bot)
        assert restored.nickname == "大肥鱼"
        assert service.current.nickname == "大肥鱼"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_rejects_unsafe_content(tmp_path: Path) -> None:
    db, _brain, service = await make_service(tmp_path)
    try:
        with pytest.raises(PresenceError):
            await service.save_draft(
                SelfProfile(
                    nickname="大肥鱼",
                    signature="加微信领红包",
                    bio="正常简介",
                    tags=[],
                )
            )
        with pytest.raises(PresenceError):
            await service.save_draft(
                SelfProfile(
                    nickname="A" * 20,
                    signature="正常签名",
                    bio="正常简介",
                    tags=[],
                )
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_qzone_generate_post_and_delete(tmp_path: Path) -> None:
    db, _brain, service = await make_service(tmp_path)
    bot = FakeBot()
    try:
        content = await service.generate_qzone_post()
        assert "鱼" in content
        with pytest.raises(PolicyViolation):
            await service.post_qzone(bot=bot, content="看这个 https://example.com", source="manual")

        result = await service.post_qzone(bot=bot, content=content, source="manual")
        assert result["tid"] == "tid-001"
        post = await db.get_social_post("tid-001")
        assert post is not None
        assert post.status == "posted"
        assert post.content == content

        await service.delete_qzone("tid-001", bot)
        deleted = await db.get_social_post("tid-001")
        assert deleted is not None
        assert deleted.status == "deleted"
        assert [call[0] for call in bot.calls[-2:]] == ["send_qzone_msg", "delete_qzone_msg"]

        with pytest.raises(PresenceError):
            await service.delete_qzone("missing", bot)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_qzone_post_with_images(tmp_path: Path) -> None:
    db, _brain, service = await make_service(tmp_path)
    bot = FakeBot()
    try:
        result = await service.post_qzone(
            bot=bot,
            content="今天出门摸鱼",
            images=["https://example.com/a.jpg", "base64://aGVsbG8="],
            source="manual",
        )
        assert result["images"] == ["https://example.com/a.jpg", "base64://aGVsbG8="]
        call = bot.calls[-1]
        assert call[0] == "send_qzone_msg"
        assert call[1]["images"] == result["images"]
        with pytest.raises(PresenceError):
            await service.post_qzone(
                bot=bot,
                content="今天出门摸鱼",
                images=["file:///etc/passwd"],
                source="manual",
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_auto_due_state_is_stable(tmp_path: Path) -> None:
    db, _brain, service = await make_service(tmp_path)
    try:
        due = await service._next_due("profile_next_due", first_delay_minutes=30)
        again = await service._next_due("profile_next_due", first_delay_minutes=30)
        assert due == again
        assert await db.get_social_state("profile_next_due") is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_auto_loop_applies_generated_profile(tmp_path: Path, monkeypatch) -> None:
    db, _brain, service = await make_service(tmp_path)
    service.settings.profile_auto_enabled = True
    service.settings.qzone_auto_enabled = False
    generated = await service.generate_profile()
    applied: list[SelfProfile | None] = []

    async def fake_generate() -> SelfProfile:
        return generated

    async def fake_apply(profile: SelfProfile | None = None) -> SelfProfile:
        applied.append(profile)
        return profile or generated

    async def fake_next_due(key: str, *, first_delay_minutes: int):
        return service._now() - timedelta(seconds=1)

    sleep_calls = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "generate_profile", fake_generate)
    monkeypatch.setattr(service, "apply_profile", fake_apply)
    monkeypatch.setattr(service, "_next_due", fake_next_due)
    monkeypatch.setattr("src.presence.asyncio.sleep", fake_sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await service._auto_loop()
        assert applied == [generated]
    finally:
        await db.close()

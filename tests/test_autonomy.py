from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomy import AutonomyService
from src.db import Database
from src.llm import LLMError, LLMResult
from src.persona import Persona
from src.settings import Settings


class FakeLLM:
    def __init__(self, allow: bool = True, error: bool = False) -> None:
        self.allow = allow
        self.error = error
        self.calls = 0
        self.last: dict = {}

    async def decide_action(self, **kwargs):
        self.calls += 1
        self.last = kwargs
        if self.error:
            raise LLMError("mock failure")
        result = LLMResult(text="", model="mock", input_tokens=12, output_tokens=3)
        return {"allow": self.allow, "reason": "不想" if not self.allow else "行"}, result


class FakeSkills:
    def __init__(self, role: str = "owner") -> None:
        self.role = role

    def role_for_event(self, event) -> str:
        return self.role


def make_persona() -> Persona:
    return Persona(
        id="dafeiyu",
        display_name="大肥鱼",
        identity="一条鱼",
        relationship="群友",
        traits=["嘴硬"],
    )


def make_event():
    return SimpleNamespace(
        user_id=20001,
        self_id=10001,
        group_id=123456,
        sender=SimpleNamespace(role="member"),
    )


@pytest.mark.asyncio
async def test_console_can_disable_autonomy(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        llm = FakeLLM(allow=False)
        service = AutonomyService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            llm,
            make_persona(),
            FakeSkills(),
        )
        await service.set_state(enabled=False, directive="都听群友的")
        decision = await service.review(make_event(), "group.mute", args="10m")
        assert decision.allow is True
        assert decision.source == "console"
        assert llm.calls == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_bot_can_refuse_and_cache(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        llm = FakeLLM(allow=False)
        service = AutonomyService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            llm,
            make_persona(),
            FakeSkills(),
        )
        await service.set_state(enabled=True, directive="不要随便踢人")
        event = make_event()
        first = await service.review(event, "group.kick", target_id="20002", args="@人")
        second = await service.review(event, "group.kick", target_id="20002", args="@人")
        assert first.allow is False
        assert first.reason == "不想"
        assert second == first
        assert llm.calls == 1
        assert llm.last["console_directive"] == "不要随便踢人"
        assert llm.last["role"] == "主人"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_autonomy_allows_and_skips_read_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        llm = FakeLLM(allow=True)
        service = AutonomyService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            llm,
            make_persona(),
            FakeSkills(role="admin"),
        )
        event = make_event()
        decision = await service.review(event, "group.mute", args="10m")
        assert decision.allow is True
        assert llm.calls == 1
        skipped = await service.review(event, "brain.search", args="鱼")
        assert skipped.allow is True
        assert skipped.source == "bot"
        assert llm.calls == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_explore_mode_skips_llm_and_allows(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        llm = FakeLLM(allow=False)
        service = AutonomyService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            llm,
            make_persona(),
            FakeSkills(),
        )
        await service.set_state(enabled=True, directive="", mode="explore")
        decision = await service.review(make_event(), "group.kick", args="@人")
        assert decision.allow is True
        assert decision.source == "explore"
        assert llm.calls == 0
        state = await service.get_state()
        assert state.mode == "explore"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_autonomy_llm_error_is_conservative(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = AutonomyService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            FakeLLM(error=True),
            make_persona(),
            FakeSkills(),
        )
        await service.set_state(enabled=True, directive="")
        decision = await service.review(make_event(), "qzone.post")
        assert decision.allow is False
        assert decision.source == "error"
    finally:
        await db.close()

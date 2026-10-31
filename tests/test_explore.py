from pathlib import Path
from typing import Any

import pytest

from src.autonomy import AutonomyState
from src.db import Database
from src.explore import CAPABILITIES, ExplorationService, ExploreError
from src.settings import Settings


class StubAutonomy:
    def __init__(self) -> None:
        self.state = AutonomyState(enabled=True, directive="", mode="normal")

    async def get_state(self) -> AutonomyState:
        return self.state

    async def set_state(
        self,
        *,
        enabled: bool,
        directive: str,
        mode: str = "normal",
    ) -> AutonomyState:
        self.state = AutonomyState(enabled=enabled, directive=directive, mode=mode)
        return self.state


class FakeBot:
    self_id = 10001

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        if action == "get_supported_actions":
            return [item.action for item in CAPABILITIES] + ["set_group_kick", "send_msg"]
        if action == "get_group_list":
            return [{"group_id": 123456, "group_name": "测试群"}]
        if action == "get_group_member_info":
            return {"role": "member", "user_id": 10001}
        return {"action": action, "ok": True}


@pytest.mark.asyncio
async def test_explore_mode_runs_read_only_probes(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        autonomy = StubAutonomy()
        service = ExplorationService(Settings(_env_file=None, data_dir=tmp_path), db, autonomy)
        assert await service.enabled() is False

        assert await service.set_enabled(True) is True
        assert await service.enabled() is True
        assert autonomy.state.mode == "explore"

        bot = FakeBot()
        findings = await service.run(bot)
        actions = [item["action"] for item in findings]
        assert "get_supported_actions" in actions
        assert "get_group_list" in actions
        assert all(action.startswith("get_") for action in actions)
        assert not any(action.startswith(("set_", "send_")) for action, _ in bot.calls)

        status = await service.status()
        assert status["enabled"] is True
        assert status["findings"]
        assert status["catalog"]

        await service.set_enabled(False)
        assert await service.enabled() is False
        with pytest.raises(ExploreError):
            await service.run(bot)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_explore_catalog_is_read_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = ExplorationService(
            Settings(_env_file=None, data_dir=tmp_path),
            db,
            StubAutonomy(),
        )
        catalog = service.catalog()
        assert len(catalog) >= 10
        assert all(item["action"].startswith("get_") for item in catalog)
        assert all(item["read_only"] for item in catalog)
    finally:
        await db.close()

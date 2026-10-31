from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.db import Database
from src.guild import GuildError, GuildService
from src.settings import Settings


class FakeBot:
    def __init__(self, *, plugin: bool = True) -> None:
        self.plugin = plugin
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        if not self.plugin:
            raise RuntimeError("unsupported action")
        if action == "nc_guild_status":
            return {"available": True}
        if action == "nc_guild_send":
            return {"ok": True, "message_id": "m-1"}
        if action == "nc_guild_history":
            return {"messages": [{"content": "hi", "author": {"username": "鱼"}}]}
        if action == "nc_guild_list":
            return {
                "channels": [
                    {"id": "22", "name": "测试频道", "guild_id": "11"},
                ]
            }
        return {}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "guild_backend": "auto",
        "guild_id": "11",
        "guild_channel_id": "22",
        "guild_rate_seconds": 0,
        "guild_timeout": 5,
    }
    values.update(overrides)
    return Settings(**values)


async def make_service(
    tmp_path: Path,
    *,
    client: httpx.AsyncClient | None = None,
    **overrides: Any,
) -> tuple[Database, GuildService]:
    settings = make_settings(tmp_path, **overrides)
    db = Database(tmp_path / "bot.db")
    await db.connect()
    return db, GuildService(settings, db, client=client)


@pytest.mark.asyncio
async def test_guild_plugin_send_history_and_list(tmp_path: Path) -> None:
    db, service = await make_service(tmp_path)
    bot = FakeBot()
    try:
        status = await service.status(bot)
        assert status["plugin_available"] is True
        assert status["guild_id"] == "11"

        result = await service.send(bot, "频道测试")
        assert result["backend"] == "plugin"
        assert result["message_id"] == "m-1"
        assert bot.calls[-1][0] == "nc_guild_send"
        assert bot.calls[-1][1]["channel_id"] == "22"

        history = await service.history(bot)
        assert history["backend"] == "plugin"
        assert history["messages"][0]["content"] == "hi"

        channels = await service.list_channels(bot)
        assert channels["channels"][0]["id"] == "22"
    finally:
        await service.close()
        await db.close()


@pytest.mark.asyncio
async def test_guild_auto_falls_back_to_official(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "bots.qq.com":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200})
        if request.url.path.endswith("/channels/22/messages"):
            return httpx.Response(200, json={"id": "om-1"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db, service = await make_service(
        tmp_path,
        client=client,
        guild_bot_appid="app-1",
        guild_bot_secret="secret-1",
        guild_bot_token="",
    )
    bot = FakeBot(plugin=False)
    try:
        result = await service.send(bot, "走官方")
        assert result["backend"] == "official"
        assert result["message_id"] == "om-1"
        assert any(request.url.host == "bots.qq.com" for request in requests)
        message_call = next(
            request for request in requests if request.url.path.endswith("/channels/22/messages")
        )
        assert message_call.headers["authorization"] == "QQBot token-1"
    finally:
        await service.close()
        await client.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_guild_official_channels(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bots.qq.com":
            return httpx.Response(200, json={"access_token": "token-2", "expires_in": 7200})
        if request.url.path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "11", "name": "频道"}])
        if request.url.path == "/guilds/11/channels":
            return httpx.Response(200, json=[{"id": "22", "name": "大厅"}])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db, service = await make_service(
        tmp_path,
        client=client,
        guild_backend="official",
        guild_bot_appid="app-1",
        guild_bot_secret="secret-1",
    )
    bot = FakeBot(plugin=False)
    try:
        result = await service.list_channels(bot)
        assert result["backend"] == "official"
        assert result["channels"][0]["name"] == "大厅"
    finally:
        await service.close()
        await client.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_guild_config_and_validation(tmp_path: Path) -> None:
    db, service = await make_service(tmp_path)
    try:
        config = await service.set_config(backend="plugin", guild_id="33", channel_id="44")
        assert config.backend == "plugin"
        assert config.guild_id == "33"
        with pytest.raises(GuildError):
            await service.set_config(backend="bad")
        with pytest.raises(GuildError):
            await service.set_config(guild_id="../../etc")
        await service.set_config(backend="off")
        bot = FakeBot()
        with pytest.raises(GuildError):
            await service.send(bot, "不应发出")
    finally:
        await service.close()
        await db.close()

from pathlib import Path

import pytest

import src.account as account_module
from src.account import AccountError, AccountService
from src.settings import Settings


class FakeBot:
    def __init__(self) -> None:
        self.self_id = "10001"
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        return {}


def make_service(tmp_path: Path) -> AccountService:
    return AccountService(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            brain_dir=tmp_path / "brain",
            image_allow_private_hosts=True,
        )
    )


@pytest.mark.asyncio
async def test_account_avatar_and_group_portrait(tmp_path: Path, monkeypatch) -> None:
    async def fake_fetch(url: str, **kwargs):
        assert url == "http://example.test/a.png"
        return b"png-bytes", "image/png"

    monkeypatch.setattr(account_module, "fetch_image_bytes", fake_fetch)
    service = make_service(tmp_path)
    bot = FakeBot()
    await service.set_avatar(bot, "http://example.test/a.png")
    assert bot.calls[-1] == (
        "set_qq_avatar",
        {"file": "base64://cG5nLWJ5dGVz"},
    )

    await service.set_group_portrait(bot, 123456, "http://example.test/a.png")
    assert bot.calls[-1][0] == "set_group_portrait"
    assert bot.calls[-1][1]["group_id"] == "123456"

    with pytest.raises(AccountError):
        await service.set_avatar(bot, "file:///etc/passwd")


@pytest.mark.asyncio
async def test_account_profile_status_like_and_poke(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    bot = FakeBot()
    assert await service.set_nickname(bot, "大肥鱼") == "大肥鱼"
    assert await service.set_signature(bot, "今天也在摸鱼") == "今天也在摸鱼"
    assert await service.set_status(bot, "忙碌") == 50
    assert await service.set_status(bot, "70") == 70
    assert await service.send_like(bot, "20002", 20) == 10
    await service.poke(bot, 123456, "20002")

    actions = [item[0] for item in bot.calls]
    assert actions == [
        "set_qq_profile",
        "set_self_longnick",
        "set_online_status",
        "set_online_status",
        "send_like",
        "send_poke",
    ]
    assert bot.calls[4][1]["times"] == 10

    with pytest.raises(AccountError):
        await service.set_nickname(bot, "a" * 17)
    with pytest.raises(AccountError):
        await service.set_status(bot, "摸鱼")
    with pytest.raises(AccountError):
        await service.send_like(bot, "not-a-qq")


@pytest.mark.asyncio
async def test_account_group_extras(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    bot = FakeBot()
    assert await service.set_special_title(bot, 123456, "20002", "摸鱼冠军") == "摸鱼冠军"
    await service.group_sign(bot, 123456)
    await service.set_essence(bot, "99")
    await service.remove_essence(bot, "99")
    assert [item[0] for item in bot.calls] == [
        "set_group_special_title",
        "send_group_sign",
        "set_essence_msg",
        "del_essence_msg",
    ]
    with pytest.raises(AccountError):
        await service.set_special_title(bot, 123456, "20002", "头" * 21)

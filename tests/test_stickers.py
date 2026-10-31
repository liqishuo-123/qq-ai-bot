import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import Database
from src.settings import Settings
from src.stickers import (
    StickerService,
    extract_sticker_images,
    normalize_tags,
    parse_tag_json,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nK0AAAAASUVORK5CYII="
)


class FakeLLM:
    async def chat(self, *args, **kwargs):
        return SimpleNamespace(
            text='{"description":"一条胖鱼在哭","tags":["鱼","哭","讨饭"]}',
            model="fake",
            input_tokens=10,
            output_tokens=5,
        )


def make_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        sticker_dir=tmp_path / "stickers",
        sticker_auto_tag=False,
        sticker_auto_chance=1.0,
        sticker_cooldown_seconds=300,
    )
    settings.ensure_dirs()
    return settings


@pytest.mark.asyncio
async def test_sticker_extract_and_save_roundtrip(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = StickerService(settings, db, FakeLLM())  # type: ignore[arg-type]
        message = [
            SimpleNamespace(type="text", data={"text": "看这个"}),
            SimpleNamespace(
                type="image",
                data={"url": "https://example.com/a.gif", "sub_type": 1, "summary": "[动画表情]"},
            ),
        ]
        images = extract_sticker_images(message)
        assert len(images) == 1
        assert images[0].sub_type == 1

        record, created = await service.save_bytes(
            PNG,
            "image/png",
            source_scope_type="group",
            source_scope_id="10001",
            source_user_id="20001",
            source_nickname="小明",
            source_message_id="30001",
        )
        assert created is True
        assert record.sha256
        assert (settings.sticker_dir / record.filename).is_file()
        assert await db.count_stickers() == 1

        duplicate, created_again = await service.save_bytes(PNG, "image/png")
        assert created_again is False
        assert duplicate.id == record.id
        assert await db.count_stickers() == 1
    finally:
        await db.close()


def test_only_sticker_image_segments_are_extracted() -> None:
    normal = [SimpleNamespace(type="image", data={"url": "https://example.com/a.jpg", "sub_type": 0})]
    assert extract_sticker_images(normal) == []

    fallback = [SimpleNamespace(type="image", data={"url": "https://example.com/a.gif", "summary": "[动画表情]"})]
    assert len(extract_sticker_images(fallback)) == 1


def test_marker_and_request_parsing() -> None:
    assert StickerService.parse_marker("哈哈。[[sticker:3]]") == ("哈哈。", 3)
    assert StickerService.parse_marker("[[sticker:1]] 好") == ("好", 1)
    assert StickerService.parse_marker("没有标记") == ("没有标记", None)
    assert StickerService.is_explicit_request("来个表情")
    assert StickerService.is_explicit_request("发个表情包")
    assert not StickerService.is_explicit_request("这个表情包真好笑")


def test_tag_json_parsing() -> None:
    tags, description = parse_tag_json('```json\n{"tags":["鱼","哭"],"description":"胖鱼哭了"}\n```')
    assert tags == "鱼,哭"
    assert description == "胖鱼哭了"
    assert parse_tag_json("不是 json") == ("", "")
    assert normalize_tags("鱼, 哭、讨饭") == "鱼,哭,讨饭"


@pytest.mark.asyncio
async def test_catalog_and_cooldown(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sticker_auto_tag = False
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = StickerService(settings, db, FakeLLM())  # type: ignore[arg-type]
        record, _ = await service.save_bytes(PNG, "image/png", tags="鱼,哭", description="胖鱼哭了")
        catalog = await service.catalog_for("group", "10001", explicit=False)
        assert catalog is not None
        assert "[[sticker:ID]]" in catalog
        assert "鱼,哭" in catalog

        await service.mark_sent(record.id, "group", "10001")
        assert not await service.can_send("group", "10001", explicit=False)
        assert await service.can_send("group", "10001", explicit=True)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_tag_updates_record(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sticker_auto_tag = True
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        service = StickerService(settings, db, FakeLLM())  # type: ignore[arg-type]
        record, _ = await service.save_bytes(PNG, "image/png")
        assert await service.tag_sticker(record.id) is True
        updated = await db.get_sticker(record.id)
        assert updated is not None
        assert updated.tags == "鱼,哭,讨饭"
        assert updated.description == "一条胖鱼在哭"
    finally:
        await db.close()

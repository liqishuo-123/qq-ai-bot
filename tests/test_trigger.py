from types import SimpleNamespace

from src.trigger import TriggerContext, is_at_bot, is_reply_to_bot, mentions_name


def make_ctx(**overrides: object) -> TriggerContext:
    values: dict[str, object] = {
        "active": True,
        "mode": "smart",
        "is_at_bot": False,
        "is_reply_to_bot": False,
        "mentions_name": False,
        "same_user_as_last_reply": False,
        "seconds_since_bot_reply": None,
        "followup_window": 90,
    }
    values.update(overrides)
    return TriggerContext(**values)  # type: ignore[arg-type]


def test_inactive_group_ignores_until_at() -> None:
    assert not make_ctx(active=False).should_reply()
    assert make_ctx(active=False, is_at_bot=True).should_reply()


def test_smart_mode_direct_triggers() -> None:
    assert make_ctx(is_at_bot=True).should_reply()
    assert make_ctx(is_reply_to_bot=True).should_reply()
    assert make_ctx(mentions_name=True).should_reply()


def test_smart_mode_followup_window() -> None:
    assert make_ctx(same_user_as_last_reply=True, seconds_since_bot_reply=30).should_reply()
    assert not make_ctx(same_user_as_last_reply=True, seconds_since_bot_reply=120).should_reply()
    assert not make_ctx(same_user_as_last_reply=False, seconds_since_bot_reply=10).should_reply()


def test_full_mode_replies_to_everything() -> None:
    assert make_ctx(mode="full").should_reply()


def test_name_and_reply_helpers() -> None:
    assert mentions_name("大肥鱼在吗", ["大肥鱼", "肥鱼", "大鱼"])
    assert not mentions_name("今天天气不错", ["大肥鱼"])
    reply = SimpleNamespace(sender=SimpleNamespace(user_id=123))
    assert is_reply_to_bot(reply, 123)
    assert not is_reply_to_bot(reply, 456)
    assert not is_reply_to_bot(None, 123)


def test_is_at_bot_fallback() -> None:
    message = [
        SimpleNamespace(type="text", data={"text": "在吗"}),
        SimpleNamespace(type="at", data={"qq": "123"}),
    ]
    assert is_at_bot(message, 123)
    assert not is_at_bot(message, 456)
    assert not is_at_bot("plain text", 123)

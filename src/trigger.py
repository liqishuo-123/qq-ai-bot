from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TriggerContext:
    active: bool
    mode: str
    is_at_bot: bool
    is_reply_to_bot: bool
    mentions_name: bool
    same_user_as_last_reply: bool
    seconds_since_bot_reply: float | None
    followup_window: float = 90.0

    def should_reply(self) -> bool:
        if not self.active:
            return self.is_at_bot
        if self.is_at_bot or self.is_reply_to_bot or self.mentions_name:
            return True
        if self.mode == "full":
            return True
        if (
            self.same_user_as_last_reply
            and self.seconds_since_bot_reply is not None
            and self.seconds_since_bot_reply <= self.followup_window
        ):
            return True
        return False


def mentions_name(text: str, names: list[str]) -> bool:
    normalized = text.casefold()
    return any(name and name.casefold() in normalized for name in names)


def is_reply_to_bot(reply: object | None, self_id: int | str) -> bool:
    if reply is None:
        return False
    sender = getattr(reply, "sender", None)
    user_id = getattr(sender, "user_id", None)
    return str(user_id) == str(self_id)


def is_at_bot(message: object, self_id: int | str) -> bool:
    """Fallback for implementations that do not set event.to_me."""
    try:
        segments = list(message)  # type: ignore[arg-type]
    except TypeError:
        return False
    for segment in segments:
        if getattr(segment, "type", None) != "at":
            continue
        data = getattr(segment, "data", {}) or {}
        if str(data.get("qq")) == str(self_id):
            return True
    return False

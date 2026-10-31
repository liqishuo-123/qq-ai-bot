from __future__ import annotations

import re

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;…])\s*")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def sanitize_text(text: str, bot_name: str | None = None) -> str:
    value = text.strip()
    if bot_name:
        value = re.sub(rf"^{re.escape(bot_name)}\s*[：:]\s*", "", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _MARKDOWN_LINK_RE.sub(r"\1 \2", value)
    value = _HEADING_RE.sub("", value)
    value = _BULLET_RE.sub("· ", value)
    value = value.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def _chunk_long_block(block: str, limit: int) -> list[str]:
    """Split only when one bubble would exceed the hard character limit."""
    if len(block) <= limit:
        return [block]

    parts = [part.strip() for part in _SENTENCE_RE.split(block) if part.strip()]
    if not parts:
        parts = [block]

    chunks: list[str] = []
    current = ""
    for part in parts:
        while len(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part[:limit])
            part = part[limit:]
        candidate = f"{current}{part}" if current else part
        if current and len(candidate) > limit:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [block[:limit]]


def split_reply(text: str, *, max_bubbles: int = 3, max_chars: int = 500) -> list[str]:
    """Split a model reply by intentional line breaks, not by punctuation.

    A newline from the model means "send this as another QQ message".  Normal
    sentences stay in the same message; a comma or full stop must not create a
    new bubble.  ``max_bubbles`` is a spam guard, never a target.
    """
    value = sanitize_text(text)
    if not value:
        return []

    max_bubbles = max(1, max_bubbles)
    blocks = [part.strip() for part in re.split(r"\n+", value) if part.strip()]
    if not blocks:
        blocks = [value]

    # Drop exact duplicates while preserving the model's intended order.
    unique: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        key = re.sub(r"\s+", " ", block).strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(block)
    blocks = unique or [value]

    # Split only overlong single blocks; this is a transport limit, not style.
    expanded: list[str] = []
    for block in blocks:
        expanded.extend(_chunk_long_block(block, max_chars))

    if len(expanded) > max_bubbles:
        head = expanded[: max_bubbles - 1]
        tail = " ".join(expanded[max_bubbles - 1 :])
        expanded = [*head, tail]

    final: list[str] = []
    remaining = max_chars
    for bubble in expanded:
        if remaining <= 0:
            break
        piece = bubble.strip()[:remaining]
        if piece:
            final.append(piece)
            remaining -= len(piece)
    return final


def typing_delay(text: str, min_seconds: float = 0.4, max_seconds: float = 2.5) -> float:
    if max_seconds <= min_seconds:
        return max(0.0, min_seconds)
    return min(max_seconds, max(min_seconds, min_seconds + len(text) * 0.01))

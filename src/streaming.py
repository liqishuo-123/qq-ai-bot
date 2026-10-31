from __future__ import annotations

from collections.abc import Awaitable, Callable

from .composer import sanitize_text, split_reply


def first_sentence_end(text: str) -> int:
    for index, char in enumerate(text):
        if char in "。！？!?；;…\n":
            return index + 1
    return 0


class StreamSender:
    """Send the first complete sentence while the rest is still streaming."""

    def __init__(
        self,
        *,
        bot_name: str,
        max_bubbles: int,
        max_chars: int,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.bot_name = bot_name
        self.max_bubbles = max_bubbles
        self.max_chars = max_chars
        self.send = send
        self.buffer = ""
        self.sent_clean = ""
        self.sent_prefix = False

    async def on_delta(self, chunk: str) -> None:
        self.buffer += chunk
        if self.sent_prefix:
            return
        cut = first_sentence_end(self.buffer)
        if cut <= 0:
            return
        clean = sanitize_text(self.buffer[:cut], self.bot_name)
        if not clean:
            return
        bubbles = split_reply(clean, max_bubbles=1, max_chars=self.max_chars)
        if not bubbles:
            return
        await self.send(bubbles[0])
        self.sent_clean = bubbles[0]
        self.sent_prefix = True

    async def finish(self, full_text: str) -> None:
        if not self.sent_prefix:
            return
        if not full_text.startswith(self.sent_clean):
            return
        remaining = full_text[len(self.sent_clean) :].strip()
        if not remaining:
            return
        bubbles = split_reply(
            remaining,
            max_bubbles=max(1, self.max_bubbles - 1),
            max_chars=self.max_chars,
        )
        for bubble in bubbles:
            await self.send(bubble)

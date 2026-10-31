import asyncio

from src.streaming import StreamSender, first_sentence_end


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


def test_first_sentence_end() -> None:
    assert first_sentence_end("在。先说") == 2
    assert first_sentence_end("没有标点") == 0
    assert first_sentence_end("？") == 1


def test_stream_sender_sends_first_sentence_then_remainder() -> None:
    async def run() -> list[str]:
        output = FakeSender()
        sender = StreamSender(
            bot_name="大肥鱼",
            max_bubbles=2,
            max_chars=500,
            send=output,
        )
        await sender.on_delta("在。先说")
        await sender.on_delta("清楚，token 免谈。")
        await sender.finish("在。先说清楚，token 免谈。")
        return output.sent

    assert asyncio.run(run()) == ["在。", "先说清楚，token 免谈。"]

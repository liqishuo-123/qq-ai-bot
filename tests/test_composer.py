from src.composer import sanitize_text, split_reply, typing_delay


def test_sanitize_removes_markdown_noise() -> None:
    assert sanitize_text("**你好**，`世界`") == "你好，世界"
    assert sanitize_text("## 标题\n- 一\n- 二") == "标题\n· 一\n· 二"
    assert sanitize_text("大肥鱼：今天吃啥") == "大肥鱼：今天吃啥"
    assert sanitize_text("大肥鱼：今天吃啥", "大肥鱼") == "今天吃啥"


def test_short_reply_stays_one_bubble() -> None:
    assert split_reply("好家伙，这就有点意思了") == ["好家伙，这就有点意思了"]


def test_sentences_stay_one_bubble_without_intentional_newline() -> None:
    assert split_reply("第一句。第二句！") == ["第一句。第二句！"]
    assert split_reply("第一句。第二句！", max_bubbles=1) == ["第一句。第二句！"]


def test_intentional_newlines_become_bubbles() -> None:
    assert split_reply("第一句。\n第二句！") == ["第一句。", "第二句！"]
    assert split_reply("第一句。\n\n第二句！\n第三句。") == ["第一句。", "第二句！", "第三句。"]


def test_duplicate_bubbles_are_removed() -> None:
    assert split_reply("嗯。\n嗯。\n说正经的。") == ["嗯。", "说正经的。"]


def test_bubble_cap_merges_extra_content() -> None:
    assert split_reply("一\n二\n三\n四", max_bubbles=3) == ["一", "二", "三 四"]


def test_long_reply_is_split_and_bounded() -> None:
    text = "第一句。" * 50
    bubbles = split_reply(text, max_bubbles=2, max_chars=500)
    assert 1 <= len(bubbles) <= 2
    assert all(bubbles)
    assert sum(len(item) for item in bubbles) <= 500


def test_split_respects_small_char_limit() -> None:
    bubbles = split_reply("啊" * 1000, max_bubbles=2, max_chars=50)
    assert sum(len(item) for item in bubbles) <= 50


def test_typing_delay_is_bounded() -> None:
    assert 0.4 <= typing_delay("你好", 0.4, 2.5) <= 2.5
    assert typing_delay("x" * 1000, 0.4, 2.5) == 2.5
    assert typing_delay("x", 2.0, 1.0) == 2.0

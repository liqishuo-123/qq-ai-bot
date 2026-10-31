from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import Database
from src.deai import DeAiService, detect
from src.persona import Persona
from src.settings import Settings


class FakeLLM:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(
            text=self.text,
            model="fake",
            input_tokens=10,
            output_tokens=5,
        )


class FakeBudget:
    def __init__(self, exhausted: bool = False) -> None:
        self.exhausted = exhausted

    async def status(self):
        return SimpleNamespace(exhausted=self.exhausted)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "data_dir": tmp_path,
        "deai_enabled": True,
        "deai_light_threshold": 20,
        "deai_heavy_threshold": 50,
        "deai_timeout": 8.0,
        "deai_record_only": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_persona() -> Persona:
    return Persona(
        id="test",
        display_name="测试鱼",
        identity="测试",
        relationship="朋友",
        traits=["测试"],
        style={"avoid": ["作为 AI", "客服腔"]},
    )


async def make_service(tmp_path: Path, llm: FakeLLM | None = None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    await db.connect()
    service = DeAiService(settings, db, llm or FakeLLM(), FakeBudget())
    return service, db


@pytest.mark.asyncio
async def test_detect_clean_reply_scores_zero() -> None:
    report = detect("你才是 AI。成精的胖鱼，懂？")
    assert report.score == 0
    assert report.hits == []


def test_detect_hits_each_rule_family() -> None:
    report = detect("首先，你说得对。总之，希望这能帮到你！")
    assert "书面连接词" in report.hits
    assert "客服礼貌腔" in report.hits


def test_detect_list_residue_and_uniform_sentences() -> None:
    report = detect("1. 吃饭\n2. 睡觉\n3. 打豆豆")
    assert "列表残留" in report.hits
    report = detect("今天好累啊。今天好困啊。今天好饿啊。")
    assert "均匀句长" in report.hits


def test_detect_emoji_stack() -> None:
    report = detect("好好好😂😂😂😂")
    assert "emoji堆砌" in report.hits


def test_detect_caps_score() -> None:
    report = detect("首先其次总之，很高兴为你解答。以上就是我的全部回答。")
    assert 0 < report.score <= 100


@pytest.mark.asyncio
async def test_gate_below_threshold_passes_through(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path)
    text = "行，穷就穷吧。"
    final, meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    assert meta["rewritten"] == 0
    assert await db.list_deai_logs() == []
    await db.close()


@pytest.mark.asyncio
async def test_gate_record_only_keeps_original_but_logs(tmp_path: Path) -> None:
    llm = FakeLLM(text="这句被改过")
    service, db = await make_service(tmp_path, llm=llm, deai_record_only=True)
    text = "总之，希望这能帮到你！"
    final, meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    assert meta["rewritten"] == 0
    logs = await db.list_deai_logs()
    assert len(logs) == 1
    assert logs[0]["score"] >= 20
    assert llm.calls == []
    await db.close()


@pytest.mark.asyncio
async def test_gate_rewrites_when_model_output_is_clean(tmp_path: Path) -> None:
    llm = FakeLLM(text="行，穷就穷吧。")
    service, db = await make_service(tmp_path, llm=llm)
    text = "总之，希望这能帮到你！"
    final, meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == "行，穷就穷吧。"
    assert meta["rewritten"] == 1
    assert llm.calls and "总之" in llm.calls[0]["messages"][1]["content"]
    await db.close()


@pytest.mark.asyncio
async def test_gate_falls_back_to_original_on_unsafe_rewrite(tmp_path: Path) -> None:
    # 改写结果命中人设 avoid 词，必须回退原文
    llm = FakeLLM(text="作为 AI，我帮你。")
    service, db = await make_service(tmp_path, llm=llm)
    text = "总之，希望这能帮到你！"
    final, _meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    await db.close()


@pytest.mark.asyncio
async def test_gate_falls_back_on_llm_failure(tmp_path: Path) -> None:
    class BoomLLM:
        async def chat(self, messages, **kwargs):
            raise RuntimeError("boom")

    service, db = await make_service(tmp_path, llm=BoomLLM())
    text = "首先，这个事情要这样看。"
    final, _meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    await db.close()


@pytest.mark.asyncio
async def test_gate_skips_when_budget_exhausted(tmp_path: Path) -> None:
    service, db = await make_service(tmp_path, llm=FakeLLM(text="x"))
    service.budget = FakeBudget(exhausted=True)
    text = "总之，希望这能帮到你！"
    final, _meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    await db.close()


@pytest.mark.asyncio
async def test_gate_disabled_skips_everything(tmp_path: Path) -> None:
    llm = FakeLLM(text="x")
    service, db = await make_service(tmp_path, llm=llm, deai_enabled=False)
    text = "总之，希望这能帮到你！"
    final, meta = await service.gate(text, persona=make_persona(), scope_type="group", scope_id="1")
    assert final == text
    assert llm.calls == []
    assert await db.list_deai_logs() == []
    await db.close()

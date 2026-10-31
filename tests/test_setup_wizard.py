"""配置向导的单元测试。

向导之前完全没有测试，而它要做「逐行改写 .env、迁移旧键、回滚」这些
改错了就会让机器人起不来的事。这里只测纯逻辑，不发真实网络请求。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import setup_wizard as wizard


def _lines(*items: str) -> list[str]:
    return list(items)


# --------------------------------------------------------------------------- #
# 旧键兼容
# --------------------------------------------------------------------------- #
def test_validate_accepts_legacy_deepseek_keys() -> None:
    """老 .env 只有 DEEPSEEK_*，升级后必须原样能用。"""
    problems, warnings = wizard.validate(
        _lines(
            'SUPERUSERS=["10001"]',
            'NICKNAME=["大肥鱼"]',
            "ADMIN_TOKEN=t",
            "DEEPSEEK_API_KEY=sk-old",
            "DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
            "DEEPSEEK_MODEL=deepseek-v4.1-flash",
        )
    )
    assert problems == []
    assert any("LLM_PROVIDER" in item for item in warnings)


def test_resolve_llm_prefers_new_values() -> None:
    provider, key, base_url, model = wizard._resolve_llm(
        _lines(
            "LLM_PROVIDER=zhipu",
            "LLM_API_KEY=sk-new",
            "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4",
            "LLM_MODEL=glm-5.1",
            "DEEPSEEK_API_KEY=sk-old",
        )
    )
    assert (provider, key, model) == ("zhipu", "sk-new", "glm-5.1")
    assert base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_empty_new_key_falls_back_to_legacy() -> None:
    """新键写了但留空时，不能被空值挤掉旧键。"""
    _, key, _, _ = wizard._resolve_llm(_lines("LLM_API_KEY=", "DEEPSEEK_API_KEY=sk-old"))
    assert key == "sk-old"


def test_provider_guessed_from_legacy_base_url() -> None:
    provider, _, _, _ = wizard._resolve_llm(
        _lines(
            "DEEPSEEK_BASE_URL=https://api.siliconflow.cn/v1",
            "DEEPSEEK_MODEL=x",
        )
    )
    assert provider == "siliconflow"


# --------------------------------------------------------------------------- #
# .env 逐行改写
# --------------------------------------------------------------------------- #
def test_set_value_inserts_after_anchor_not_at_end() -> None:
    lines = _lines("# 模型服务商", "LLM_PROVIDER=zhipu", "LLM_API_KEY=sk-x", "", "# 其他", "FOO=1")
    out = wizard.set_value(lines, "LLM_MODEL", "glm-5.1", anchor="LLM_API_KEY")
    assert out[out.index("LLM_API_KEY=sk-x") + 1] == "LLM_MODEL=glm-5.1"
    assert out[-1] == "FOO=1"


def test_set_value_replaces_and_drops_duplicates() -> None:
    out = wizard.set_value(_lines("LLM_MODEL=old", "X=1", "LLM_MODEL=dup"), "LLM_MODEL", "new")
    assert out == ["LLM_MODEL=new", "X=1"]


def test_comment_out_hides_legacy_values() -> None:
    out = wizard.comment_out(_lines("DEEPSEEK_API_KEY=sk-secret", "LLM_API_KEY=sk-new"), wizard.LEGACY_KEYS)
    text = "\n".join(out)
    assert "sk-secret" not in text
    assert "LLM_API_KEY=sk-new" in text
    assert not any(line.startswith("DEEPSEEK_API_KEY=") for line in out)


def test_comment_out_leaves_existing_comments_alone() -> None:
    original = _lines("# DEEPSEEK_API_KEY=", "LLM_MODEL=x")
    assert wizard.comment_out(original, wizard.LEGACY_KEYS) == original


# --------------------------------------------------------------------------- #
# 校验分级
# --------------------------------------------------------------------------- #
def test_ollama_needs_no_key() -> None:
    problems, _ = wizard.validate(
        _lines(
            'SUPERUSERS=["10001"]',
            'NICKNAME=["大肥鱼"]',
            "ADMIN_TOKEN=t",
            "LLM_PROVIDER=ollama",
            "LLM_MODEL=qwen3",
        )
    )
    assert problems == []


def test_openai_without_key_is_blocking() -> None:
    problems, _ = wizard.validate(
        _lines(
            'SUPERUSERS=["10001"]',
            'NICKNAME=["大肥鱼"]',
            "ADMIN_TOKEN=t",
            "LLM_PROVIDER=openai",
            "LLM_MODEL=gpt-5.1",
        )
    )
    assert any("LLM_API_KEY" in item for item in problems)


def test_bare_superusers_still_rejected() -> None:
    problems, _ = wizard.validate(
        _lines(
            "SUPERUSERS=123456",
            'NICKNAME=["大肥鱼"]',
            "ADMIN_TOKEN=t",
            "LLM_PROVIDER=ollama",
            "LLM_MODEL=qwen3",
        )
    )
    assert any("JSON" in item or "数组" in item for item in problems)


# --------------------------------------------------------------------------- #
# 模型列表
# --------------------------------------------------------------------------- #
def test_rank_models_filters_non_chat_models() -> None:
    ranked = wizard._rank_models(
        ["bge-embedding-v1", "qwen3.6-plus", "whisper-large", "glm-5.1", "text-rerank", "qwen3.6-plus"]
    )
    assert "bge-embedding-v1" not in ranked
    assert "whisper-large" not in ranked
    assert "text-rerank" not in ranked
    assert ranked.count("qwen3.6-plus") == 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
        ([{"id": "a"}], ["a"]),
        ({"models": [{"id": "a"}]}, ["a"]),
        ({"data": []}, []),
    ],
)
def test_fetch_models_handles_response_shapes(monkeypatch, payload, expected) -> None:
    monkeypatch.setattr(wizard, "_request_json", lambda *a, **k: (200, payload))
    models, status = wizard.fetch_models("https://example.com/v1", "key")
    assert models == expected
    assert status == 200


def test_fetch_models_returns_empty_on_error(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_request_json", lambda *a, **k: (404, None))
    assert wizard.fetch_models("https://example.com/v1", "key") == ([], 404)


@pytest.mark.parametrize(
    ("status", "keyword"),
    [(401, "Key"), (404, "地址"), (0, "连不上"), (429, "频繁")],
)
def test_explain_status_mentions_cause(status: int, keyword: str) -> None:
    assert keyword in wizard._explain_status(status)


# --------------------------------------------------------------------------- #
# 写入与回滚
# --------------------------------------------------------------------------- #
def _prepare(tmp_path: Path, monkeypatch, content: str, *, check=None) -> Path:
    env = tmp_path / ".env"
    env.write_text(content, encoding="utf-8")
    monkeypatch.setattr(wizard, "ENV_PATH", env)
    monkeypatch.setattr(wizard, "EXAMPLE_PATH", tmp_path / ".env.example")
    monkeypatch.setattr(wizard, "self_check", lambda: check)
    return env


def test_run_wizard_writes_provider_and_migrates_legacy(tmp_path: Path, monkeypatch) -> None:
    env = _prepare(
        tmp_path,
        monkeypatch,
        'SUPERUSERS=["10001"]\nNICKNAME=["大肥鱼"]\nADMIN_TOKEN=t\n'
        "DEEPSEEK_API_KEY=sk-old\n"
        "DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
        "DEEPSEEK_MODEL=deepseek-v4.1-flash\n",
    )

    assert wizard.run_wizard({"provider": "zhipu", "api_key": "sk-new"}) == 0

    text = env.read_text(encoding="utf-8")
    assert "LLM_PROVIDER=zhipu" in text
    assert "LLM_API_KEY=sk-new" in text
    assert "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4" in text
    assert "LLM_MODEL=glm-5.1" in text
    assert not any(line.startswith("DEEPSEEK_API_KEY=") for line in text.splitlines())


def test_run_wizard_keeps_admin_token_when_rerun(tmp_path: Path, monkeypatch) -> None:
    env = _prepare(
        tmp_path, monkeypatch, 'SUPERUSERS=["10001"]\nNICKNAME=["大肥鱼"]\nADMIN_TOKEN=keepme\n'
    )
    assert wizard.run_wizard({"provider": "deepseek", "api_key": "sk-x"}) == 0
    assert "ADMIN_TOKEN=keepme" in env.read_text(encoding="utf-8")


def test_run_wizard_rolls_back_when_self_check_fails(tmp_path: Path, monkeypatch) -> None:
    original = 'SUPERUSERS=["10001"]\nNICKNAME=["大肥鱼"]\nADMIN_TOKEN=t\n'
    env = _prepare(tmp_path, monkeypatch, original, check="写入校验炸了")

    assert wizard.run_wizard({"provider": "deepseek", "api_key": "sk-x"}) == 1
    assert env.read_text(encoding="utf-8") == original


def test_run_wizard_rejects_unknown_provider(tmp_path: Path, monkeypatch) -> None:
    _prepare(tmp_path, monkeypatch, 'SUPERUSERS=["10001"]\n')
    assert wizard.run_wizard({"provider": "no-such-provider"}) == 1

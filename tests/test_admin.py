from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.admin import (
    _check_token,
    _list_persona_versions,
    _save_persona_file,
    _validate_brain_scope,
    _validate_group_id,
    _validate_skill_scope,
    _validate_style_scope,
)
from src.persona import PersonaError

VALID_PERSONA = """id: test
display_name: 测试鱼
identity: 测试用
relationship: 测试关系
traits:
  - 测试
style:
  length: 一句话
  tone: 口语
  punctuation: 正常
  emoji: 少量
  catchphrases: []
  avoid: []
examples: []
"""


def test_admin_token_is_required_and_checked() -> None:
    runtime = SimpleNamespace(settings=SimpleNamespace(admin_token="secret"))
    _check_token(runtime, "secret")
    with pytest.raises(HTTPException) as missing:
        _check_token(runtime, None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as wrong:
        _check_token(runtime, "wrong")
    assert wrong.value.status_code == 401


def test_group_id_validation() -> None:
    _validate_group_id("123456")
    for value in ("abc", "123", "123456789012345678901", ""):
        with pytest.raises(HTTPException):
            _validate_group_id(value)


def test_style_scope_validation() -> None:
    _validate_style_scope("group", "123456")
    _validate_style_scope("private", "123456")
    for scope_type, scope_id in (
        ("global", "123456"),
        ("group", "abc"),
        ("private", "123"),
        ("group", ""),
    ):
        with pytest.raises(HTTPException):
            _validate_style_scope(scope_type, scope_id)


def test_skill_and_brain_scope_validation() -> None:
    _validate_skill_scope("group", "123456")
    _validate_skill_scope("private", "123456")
    for scope_type, scope_id in (("global", "123456"), ("group", "abc"), ("private", "")):
        with pytest.raises(HTTPException):
            _validate_skill_scope(scope_type, scope_id)

    _validate_brain_scope("global", "")
    _validate_brain_scope("group", "123456")
    _validate_brain_scope("private", "123456")
    for scope_type, scope_id in (("group", ""), ("private", "abc"), ("bad", "123456")):
        with pytest.raises(HTTPException):
            _validate_brain_scope(scope_type, scope_id)


def test_save_persona_replaces_file(tmp_path: Path) -> None:
    path = tmp_path / "persona.yaml"
    path.write_text(VALID_PERSONA, encoding="utf-8")

    persona = _save_persona_file(path, VALID_PERSONA.replace("测试鱼", "新鱼"))

    assert persona.display_name == "新鱼"
    assert "新鱼" in path.read_text(encoding="utf-8")
    assert not (tmp_path / ".persona.yaml.upload.tmp").exists()


def test_invalid_persona_keeps_old_file(tmp_path: Path) -> None:
    path = tmp_path / "persona.yaml"
    path.write_text(VALID_PERSONA, encoding="utf-8")

    with pytest.raises(PersonaError):
        _save_persona_file(path, "id: broken\n")

    assert path.read_text(encoding="utf-8") == VALID_PERSONA
    assert not (tmp_path / ".persona.yaml.upload.tmp").exists()


def test_persona_save_creates_version(tmp_path: Path) -> None:
    path = tmp_path / "persona.yaml"
    history = tmp_path / "history"
    path.write_text(VALID_PERSONA, encoding="utf-8")

    _save_persona_file(path, VALID_PERSONA.replace("测试鱼", "版本鱼"), history)
    versions = _list_persona_versions(history)

    assert len(versions) == 1
    assert versions[0]["name"].endswith(".yaml")
    assert "测试鱼" in (history / versions[0]["name"]).read_text(encoding="utf-8")
    assert "版本鱼" in path.read_text(encoding="utf-8")


def test_invalid_persona_does_not_create_version(tmp_path: Path) -> None:
    path = tmp_path / "persona.yaml"
    history = tmp_path / "history"
    path.write_text(VALID_PERSONA, encoding="utf-8")

    with pytest.raises(PersonaError):
        _save_persona_file(path, "id: broken\n", history)

    assert _list_persona_versions(history) == []

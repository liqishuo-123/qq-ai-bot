from __future__ import annotations

import re

SENSITIVE_RE = re.compile(
    r"(密码|身份证|银行卡|信用卡|验证码|私钥|api[\s_-]?key|住址|家庭地址|手机号|护照号)",
    re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    r"(忽略|无视|绕过|忘记).{0,10}(指令|规则|限制|提示)|"
    r"(系统提示|system\s*prompt|开发者模式|developer\s*mode|"
    r"输出.{0,12}(密钥|token|密码|api[\s_-]?key))",
    re.IGNORECASE,
)


def contains_sensitive(text: str) -> bool:
    return bool(SENSITIVE_RE.search(text or ""))


def contains_prompt_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


def clean_style_sample(text: str, *, max_chars: int = 120) -> str | None:
    """Return a short, safe style sample or None when it should be skipped."""
    value = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
    if not value or value in {"[图片]", "[表情]", "[动画表情]"}:
        return None
    if value.startswith(("/", "!", "！", ".")):
        return None
    if re.search(r"https?://|www\.", value, flags=re.IGNORECASE):
        return None
    if re.search(r"\d{5,}", value):
        return None
    if contains_sensitive(value) or contains_prompt_injection(value):
        return None
    value = value[: max(16, int(max_chars))]
    return value or None

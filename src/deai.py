from __future__ import annotations

import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from .budget import TokenBudget
from .composer import sanitize_text
from .db import Database
from .llm import DeepSeekClient
from .persona import Persona
from .policy import PolicyViolation, check_chat_text
from .settings import Settings

logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")
_SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?…\n]+[。！？!?…]?")

# 规则集：每条 (规则名, 说明, 正则, 单次权重, 计分上限)
# 纯代码、零 token；score 是 0~100 的"AI 味"估计，只决定是否触发改写。
_RULES: tuple[tuple[str, str, re.Pattern[str], int, int], ...] = (
    (
        "书面连接词",
        "首先/其次/总之这类书面连接词",
        re.compile(r"(首先|其次|再次|最后[，,一]|总之|综上|总而言之|换句话说|值得一提的是)"),
        15,
        30,
    ),
    (
        "客服礼貌腔",
        "客服式礼貌用语",
        re.compile(r"(希望这能帮到你|如果还有其他问题|很高兴为你|不客气[!！。~～]|祝你生活愉快|祝你学习进步|随时问我)"),
        20,
        40,
    ),
    (
        "总结式收尾",
        "以上就是/总的来说式总结",
        re.compile(r"(以上就是|这就是我|总的来说|总的来看|综上就是)"),
        15,
        30,
    ),
    (
        "共情模板",
        "我理解你的感受式模板共情",
        re.compile(r"(我理解你的感受|听到这个我很难过|我完全理解|你一定很[难伤痛][^\n]{0,6}吧)"),
        25,
        50,
    ),
    (
        "AI高频词",
        "赋能/维度/层面等 AI 高频词",
        re.compile(r"(赋能|多维度|多维度|各个维度|不同层面|某种程度上|众所周知|不得不说的?是)"),
        12,
        24,
    ),
    (
        "破折号滥用",
        "破折号或波浪号收尾过多",
        re.compile(r"(——|～～|~~)"),
        10,
        20,
    ),
)

_LIST_LINE_RE = re.compile(r"^\s*(?:\d{1,2}[.、)]|[一二三四五六七八九十][、.]|[-•·*])\s*")
_DASH_END_RE = re.compile(r"[~～]\s*$")


@dataclass(slots=True)
class AiFlavorReport:
    score: int = 0
    hits: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def as_meta(self) -> dict[str, Any]:
        return {"score": self.score, "hits": list(self.hits)}


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.findall(text) if part.strip()]


def _rule_hits(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for name, _, pattern, _, _ in _RULES:
        found = pattern.findall(text)
        if found:
            hits.append((name, str(found[0])))
    # 列表残留：连续 ≥2 行以编号/圆点开头
    lines = [line for line in text.splitlines() if line.strip()]
    list_streak = 0
    max_streak = 0
    for line in lines:
        if _LIST_LINE_RE.match(line):
            list_streak += 1
            max_streak = max(max_streak, list_streak)
        else:
            list_streak = 0
    if max_streak >= 2:
        hits.append(("列表残留", f"{max_streak} 行编号/圆点"))
    # 均匀句长：≥3 句且句长标准差过小（机器式节奏）
    sentences = _sentences(text)
    if len(sentences) >= 3:
        lengths = [len(item) for item in sentences]
        if statistics.pstdev(lengths) < 6.0:
            hits.append(("均匀句长", "句长过于一致"))
    # emoji 堆砌
    emojis = _EMOJI_RE.findall(text)
    if len(emojis) >= 4 or (len(emojis) >= 3 and len(set(emojis)) == 1):
        hits.append(("emoji堆砌", f"{len(emojis)} 个"))
    # 波浪号收尾 ≥2 句
    tilde_ends = sum(1 for item in sentences if _DASH_END_RE.search(item))
    if tilde_ends >= 2:
        hits.append(("波浪号收尾", f"{tilde_ends} 句"))
    # 双重因果 + 长解释
    if len(text) > 80 and "因为" in text and "所以" in text:
        hits.append(("解释过多", "因为…所以…"))
    return hits


_WEIGHTS: dict[str, tuple[int, int]] = {
    name: (weight, cap) for name, _, _, weight, cap in _RULES
}
_WEIGHTS.update(
    {
        "列表残留": (25, 25),
        "均匀句长": (15, 15),
        "emoji堆砌": (15, 15),
        "波浪号收尾": (10, 10),
        "解释过多": (15, 15),
    }
)


def detect(text: str) -> AiFlavorReport:
    """Score a reply for AI-flavored writing. Pure rules, no tokens."""
    value = (text or "").strip()
    if not value:
        return AiFlavorReport()
    scores: dict[str, int] = {}
    evidence: list[str] = []
    for name, snippet in _rule_hits(value):
        weight, cap = _WEIGHTS.get(name, (10, 10))
        scores[name] = min(cap, scores.get(name, 0) + weight)
        evidence.append(f"{name}: {snippet[:40]}")
    score = min(100, sum(scores.values()))
    return AiFlavorReport(score=score, hits=sorted(scores), evidence=evidence)


class DeAiError(RuntimeError):
    pass


class DeAiService:
    """去 AI 化闸门：检测 → 分级改写（record_only 模式只记录）。

    改写只调一次、失败即发原文；结果必须再过内容安全与人设 avoid 词，
    任何一步失败都回退原文，绝不阻塞聊天链路。
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: DeepSeekClient,
        budget: TokenBudget | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.budget = budget

    @property
    def enabled(self) -> bool:
        return bool(self.settings.deai_enabled)

    async def gate(
        self,
        text: str,
        *,
        persona: Persona,
        scope_type: str,
        scope_id: str,
    ) -> tuple[str, dict[str, Any]]:
        report = detect(text)
        if not self.enabled or report.score < max(1, int(self.settings.deai_light_threshold)):
            return text, report.as_meta() | {"rewritten": 0}
        rewritten = 0
        final = text
        if not self.settings.deai_record_only and report.score >= 1:
            candidate = await self._rewrite(text, report, persona)
            if candidate:
                final = candidate
                rewritten = 2 if report.score >= int(self.settings.deai_heavy_threshold) else 1
        try:
            await self.db.add_deai_log(
                scope_type=scope_type,
                scope_id=scope_id,
                score=report.score,
                hit_rules=report.hits,
                rewritten=rewritten,
                reply_chars=len(final),
            )
        except Exception:
            logger.exception("Failed to record deai log")
        return final, report.as_meta() | {"rewritten": rewritten}

    async def _rewrite(self, text: str, report: AiFlavorReport, persona: Persona) -> str | None:
        if self.budget is not None:
            try:
                budget = await self.budget.status()
                if budget.exhausted:
                    return None
            except Exception:
                logger.exception("Failed to check token budget for deai rewrite")
        hits_text = "；".join(report.evidence[:6]) or "AI 腔"
        avoid = persona.style.get("avoid") or []
        avoid_text = "、".join(str(item) for item in avoid[:10]) if avoid else "客服腔、书面腔"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 QQ 聊天回复的“去 AI 味”改写器。把回复改得更像真人随手打的字："
                    "意思和态度不变，更短、更口语、可以直接去掉书面腔；"
                    "不要 Markdown、不要编号列表、不要解释、不要道歉，"
                    "只输出改写后的正文。改不出来就原样输出原文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"角色：{persona.display_name}（避免这些腔调：{avoid_text}）\n"
                    f"命中的 AI 腔：{hits_text}\n"
                    f"原文：\n{text[:800]}\n\n"
                    "输出改写后的正文。"
                ),
            },
        ]
        try:
            result = await asyncio.wait_for(
                self.llm.chat(messages, max_tokens=300, temperature=0.7),
                timeout=max(3.0, float(self.settings.deai_timeout)),
            )
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - 改写失败必须发原文
            logger.warning("DeAI rewrite failed, sending original: %s", exc)
            return None
        candidate = sanitize_text(result.text, persona.display_name)
        if not candidate or candidate == text:
            return None
        # 改写结果不能更长、不能引入安全词或人设 avoid 词
        if len(candidate) > len(text) + 40:
            return None
        try:
            candidate = check_chat_text(candidate)
        except PolicyViolation:
            return None
        lowered = candidate.lower()
        for item in avoid:
            word = str(item).strip()
            if word and word.lower() in lowered:
                return None
        for banned in ("作为ai", "作为 ai"):
            if banned in lowered:
                return None
        return candidate

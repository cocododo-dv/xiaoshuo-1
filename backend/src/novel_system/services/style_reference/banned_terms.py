"""风格参考 — 生成期禁用词的字面检查（风格稿门 / styled-draft gate 用）。

2026-09-23 风格参考 v3（P5b）：旧校验层（``validation/``：量化回测、语义回测、禁忌模式语义判定、同步裁决）删除，
这一段字面检查从 ``validation/forbidden_local.py`` 搬到这里原样保留——风格稿门仍按冻结契约里的禁用词（或画像现行
的 ``scope=generation`` 禁用词）逐词字面匹配。命中返回 ``{pattern_statement, matched_excerpt, severity}``（词本身，
短、已在禁用词表里；不含参考原文）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import BannedTermScope


def banned_term_hits(generated_text: str, terms: Iterable[Any]) -> list[dict[str, str]]:
    """纯字面检查：``terms`` 里每个出现在文本里的词记一条（去重、保序）。"""
    if not generated_text:
        return []
    hits: list[dict[str, str]] = []
    for term in dict.fromkeys(str(value or "").strip() for value in terms):
        if not term or term not in generated_text:
            continue
        hits.append({"pattern_statement": term, "matched_excerpt": term, "severity": "error"})
    return hits


def profile_generation_banned_terms(session: Session, profile_id: str) -> list[str]:
    """画像现行的生成期禁用词（``scope=generation``，含受保护专名）。"""
    if not profile_id:
        return []
    repo = StyleReferenceRepository(session)
    return [
        str(term.term or "")
        for term in repo.list_banned_terms(profile_id, scope=BannedTermScope.GENERATION.value)
    ]


def profile_banned_term_hits(generated_text: str, profile_id: str, session: Session) -> list[dict[str, str]]:
    """文本对画像现行生成期禁用词的字面命中。"""
    if not generated_text:
        return []
    return banned_term_hits(generated_text, profile_generation_banned_terms(session, profile_id))


__all__ = ["banned_term_hits", "profile_banned_term_hits", "profile_generation_banned_terms"]

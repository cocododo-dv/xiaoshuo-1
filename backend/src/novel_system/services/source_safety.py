from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Iterable
from typing import Any

from novel_system.db.models import utcnow as now_iso


# BUG-002 hardening: protected-source-term matching must survive trivial
# evasion of a red-line term — intra-term whitespace ("屠 龙"), inserted
# punctuation ("屠-龙" / "龙·族"), and traditional Chinese ("龍族" / "屠龍").
#
# Traditional→simplified folding is intentionally tiny and exists only to make
# explicitly configured/profile-derived protected terms resilient to cosmetic
# variants.  It is not a general Chinese conversion layer.
_TRADITIONAL_TO_SIMPLIFIED = {
    "龍": "龙",
    "愷": "恺",
    "諾": "诺",
    "陳": "陈",
    "爾": "尔",
    "熱": "热",
    "銅": "铜",
    "與": "与",
    "統": "统",
}
_TRAD_SIMP_TABLE = str.maketrans(_TRADITIONAL_TO_SIMPLIFIED)


def _normalize_for_match(text: str) -> str:
    """Normalize text so red-line terms cannot be evaded by cosmetic variants.

    Steps (all conservative — recall-biased, since a missed leak is the costly
    failure here): NFKC fold (e.g. full-width → half-width) → controlled
    traditional→simplified fold → strip every separator/punctuation/format
    character. Letters, digits, and CJK ideographs are NEVER removed, so
    normalization can only collapse obfuscation between glyphs; it can never
    fabricate a protected term out of unrelated alphanumeric/ideographic text.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).translate(_TRAD_SIMP_TABLE)
    cleaned: list[str] = []
    for ch in folded:
        category = unicodedata.category(ch)
        # Z* = separators/whitespace, P* = punctuation, Cf/Cc = format/control
        # (zero-width joiners, BOM, bidi marks, etc.).
        if category[0] in ("Z", "P") or category in ("Cf", "Cc"):
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def _normalize_for_match_with_offsets(text: str) -> tuple[str, list[int]]:
    """与 :func:`_normalize_for_match` 同一规则（NFKC、繁→简、去分隔 / 标点 / 格式字符）再 casefold，
    逐字进行并返回每个规范化字符在原文里的下标——命中位置要能映射回作者自己的正文。"""
    chars: list[str] = []
    offsets: list[int] = []
    for index, raw in enumerate(str(text or "")):
        for ch in unicodedata.normalize("NFKC", raw).translate(_TRAD_SIMP_TABLE):
            category = unicodedata.category(ch)
            if category[0] in ("Z", "P") or category in ("Cf", "Cc"):
                continue
            for folded in ch.casefold():
                chars.append(folded)
                offsets.append(index)
    return "".join(chars), offsets


def normalize_for_term_match(text: str) -> str:
    """受保护专名比对用的规范化（与 :func:`find_protected_term_spans` 同一口径，不带下标）。"""
    return _normalize_for_match_with_offsets(text)[0]


def find_protected_term_spans(
    text: str, terms: Iterable[Any]
) -> list[tuple[str, int, int]]:
    """``terms`` 在 ``text`` 里的每一处出现 ``(term, start, end)``（原文下标，end 不含）。

    匹配口径与 :func:`scan_source_safety` 相同（挡得住插空格 / 换标点 / 繁体的规避）；风格参考 v3 的
    抄袭门用它报位置——位置指向作者自己的正文，报告里不必再写出这个词。
    """
    normalized, offsets = _normalize_for_match_with_offsets(text)
    if not normalized:
        return []
    spans: list[tuple[str, int, int]] = []
    for term in _unique_strings(terms):
        needle = _normalize_for_match(term).casefold()
        if not needle:
            continue
        position = normalized.find(needle)
        while position >= 0:
            spans.append((term, offsets[position], offsets[position + len(needle) - 1] + 1))
            position = normalized.find(needle, position + len(needle))
    spans.sort(key=lambda item: (item[1], item[2]))
    return spans


# No named work or author belongs in a process-wide default.  Keeping a
# source-specific list here used to flag ordinary fantasy terms (for example
# "龙王" and "血统") in projects that had never referenced that source.
#
# Backwards-compatible import alias: callers may still import the symbol, but
# the default is intentionally empty.  Configure project-independent terms via
# NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON; the bound reference profile's
# protected names (generation-scope banned terms) are checked by the v3
# reference copy gate (services/reference_copy_gate.py).
PROTECTED_SOURCE_TERMS: tuple[str, ...] = ()
PROTECTED_SOURCE_TERMS_ENV = "NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON"

def scan_source_safety(
    texts: str | Iterable[str | None],
    *,
    source_profile_ids: Iterable[Any] | None = None,
    protected_terms: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """全局受保护词（环境变量 / 显式传入）的逐词扫描——候选淘汰、导入与分类器复核用。

    绑定的参考书原文与画像的受保护专名由风格参考 v3 的唯一抄袭门查（``reference_copy_gate``）；
    这里原先还读 ``profile_json.source_safety`` 的特征句 / 场景桥，那个键从没有画像写过，已删。
    """
    content = _coerce_text(texts)
    normalized_content = _normalize_for_match(content)
    normalized_content_folded = normalized_content.casefold()
    configured_terms = (
        _unique_strings(protected_terms)
        if protected_terms is not None
        else configured_protected_source_terms()
    )
    # Match on the normalized form (defeats whitespace/punctuation/traditional
    # variants) but still report the canonical simplified term, in
    # configured order — downstream contracts depend on stable ordering.
    blocked_terms = [
        term
        for term in configured_terms
        if term and _normalize_for_match(term).casefold() in normalized_content_folded
    ]
    refs = _unique_strings(source_profile_ids or [])
    payload = {
        "safe": not blocked_terms,
        "blocked_terms": blocked_terms,
        "source_profile_ids": refs,
        "protected_terms_source": (
            "explicit" if protected_terms is not None else "environment" if configured_terms else "none"
        ),
        "coverage": {
            "configured_exact_terms": True,
            "semantic_paraphrase": {
                "status": "not_evaluated",
                "blocking": False,
                "reason": (
                    "deterministic source safety cannot reliably verify semantic or cross-language paraphrase"
                ),
                "recommended_action": "use independent semantic review as advisory evidence",
            },
        },
        "checked_at": now_iso(),
    }
    return payload


def configured_protected_source_terms() -> list[str]:
    """Load optional global safety terms from an explicit JSON-array setting.

    Invalid configuration fails closed with respect to *configuration scope*:
    it contributes no hidden blocklist.  Reference-profile safety remains
    active independently and is the preferred project-scoped mechanism.
    """
    raw = str(os.getenv(PROTECTED_SOURCE_TERMS_ENV, "") or "").strip()
    if not raw:
        return list(PROTECTED_SOURCE_TERMS)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return _unique_strings(payload)


def _coerce_text(texts: str | Iterable[str | None]) -> str:
    if isinstance(texts, str):
        return texts
    return "\n".join(str(item or "") for item in texts)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

"""规则引擎的文本助手：空白压缩、分句、对白、摘录、结尾一拍、动作 / 句式模板。

分句（``_sentences``）与 ``style_reference.text_utils.split_sentences`` 口径不同，不能互换：规则发现的 signal id 按
命中的词 / 句算哈希，分句一变 id 就变（``tests/test_literary_quality_snapshots.py`` 守着）。空白压缩与
``text_utils.compact_ws`` 逐字相同，只留那一份。
"""

from __future__ import annotations

import re

from novel_system.services.style_reference.text_utils import compact_ws as _compact_ws


def _first_present_term(text: str, terms: tuple[str, ...]) -> str:
    lowered = text.lower()
    for term in terms:
        normalized_term = term.lower()
        if normalized_term.strip() and normalized_term in lowered:
            return term
    return ""


def _dialogue_spans(text: str) -> list[str]:
    spans = re.findall(r'"([^"]+)"', text, flags=re.DOTALL)
    spans.extend(re.findall(r"“([^”]+)”", text, flags=re.DOTALL))
    spans.extend(re.findall(r"「([^」]+)」", text, flags=re.DOTALL))
    return [_compact_ws(span) for span in spans if span.strip()]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？.!?]+", text) if part.strip()]


def _action_template(sentence: str) -> str:
    normalized = _compact_ws(sentence)
    lowered = normalized.lower()
    if re.search(r"^[^，。！？]{1,10}低头看着[^，。！？]*，[^。！？]*沉默了片刻", normalized):
        return "pronoun_looked_at_object_then_silence"
    for action in ("turned", "looked", "nodded", "smiled", "sighed", "转身", "低头", "看着", "点头", "笑", "叹气", "沉默"):
        if action in lowered:
            return f"action:{action}"
    return ""


def _syntax_pattern(sentence: str) -> str:
    normalized = _compact_ws(sentence)
    if not normalized:
        return ""
    if re.match(r"^[她他][^，,]{2,24}[，,][^，,]{2,24}$", normalized):
        return "pronoun_phrase_comma_phrase"
    comma_count = normalized.count("，") + normalized.count(",")
    length_bucket = min(len(normalized) // 12, 4)
    return f"comma:{comma_count}:len:{length_bucket}"


def _ending_slice(text: str) -> str:
    normalized = _compact_ws(text)
    if len(normalized) <= 180:
        return normalized
    return normalized[-180:]


def _excerpt(text: str, needle: str) -> str:
    normalized = _compact_ws(text)
    if not normalized:
        return ""
    if not needle:
        return normalized[:180]
    index = normalized.lower().find(needle.lower())
    if index < 0:
        return normalized[:180]
    start = max(0, index - 70)
    end = min(len(normalized), index + len(needle) + 70)
    return normalized[start:end]

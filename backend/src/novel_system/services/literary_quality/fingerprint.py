"""一段文字的质量指纹：动作模板、意象场、句式形状与几类命中词的计数（跨场复用、候选离散度、起草提示都读它）。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from novel_system.services.literary_quality.lexicons import (
    ATMOSPHERIC_IMAGE_TERMS,
    EXPOSITORY_DIALOGUE_TERMS,
    FALSE_CLARITY_TERMS,
    IMAGE_TERMS,
    PERCEPTION_FILTER_TERMS,
    PRESSURE_TERMS,
    SUMMARY_ENDING_TERMS,
)
from novel_system.services.literary_quality.text import (
    _action_template,
    _compact_ws,
    _dialogue_spans,
    _ending_slice,
    _sentences,
    _syntax_pattern,
)


def fingerprint_literary_quality(text: str) -> dict[str, Any]:
    normalized = _compact_ws(text)
    sentences = _sentences(normalized)
    action_templates = Counter(_action_template(sentence) for sentence in sentences)
    action_templates.pop("", None)
    syntax_shapes = Counter(_syntax_pattern(sentence) for sentence in sentences)
    syntax_shapes.pop("", None)

    lowered = normalized.lower()
    image_fields: Counter[str] = Counter()
    for term in tuple(dict.fromkeys((*IMAGE_TERMS, *ATMOSPHERIC_IMAGE_TERMS))):
        if re.fullmatch(r"[a-z]+", term):
            count = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            count = lowered.count(term.lower())
        if count:
            image_fields[term] = count

    dialogue_exposition: Counter[str] = Counter()
    for dialogue in _dialogue_spans(normalized):
        for term in EXPOSITORY_DIALOGUE_TERMS:
            if term.lower() in dialogue.lower():
                dialogue_exposition[term] += 1

    return {
        "action_templates": _top_counter(action_templates, limit=8),
        "image_fields": _top_counter(image_fields, limit=10),
        "syntax_shapes": _top_counter(syntax_shapes, limit=8),
        "false_clarity": [term for term in FALSE_CLARITY_TERMS if term.lower() in lowered],
        "summary_ending": [term for term in SUMMARY_ENDING_TERMS if term.lower() in _ending_slice(normalized).lower()],
        "dialogue_exposition": _top_counter(dialogue_exposition, limit=6),
        "choice_pressure": [term for term in PRESSURE_TERMS if term.lower() in lowered],
        "perception_filter": [term for term in PERCEPTION_FILTER_TERMS if term.lower() in lowered],
    }


def _top_counter(counter: Counter[str], *, limit: int) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count}
        for value, count in counter.most_common(limit)
        if value and count > 0
    ]

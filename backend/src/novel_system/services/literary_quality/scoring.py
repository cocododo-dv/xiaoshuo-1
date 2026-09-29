"""自动诊断分：规则维度的加权分（一个公式）、文字够不够下判断（证据充分度），以及人类评判之下的上限
（``AUTOMATED_DIAGNOSTIC_CEILING``）。"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from novel_system.services.literary_quality.dimensions import (
    AUTOMATED_DIAGNOSTIC_CEILING,
    AUTOMATED_EVIDENCE_TARGET_CHARS,
    AUTOMATED_EVIDENCE_TARGET_SENTENCES,
    DIMENSION_WEIGHTS,
)
from novel_system.services.literary_quality.text import _compact_ws, _sentences


def weighted_score(
    signals: Mapping[str, Mapping[str, Any]],
    dimensions: Iterable[str],
    weights: Mapping[str, float] = DIMENSION_WEIGHTS,
    *,
    forced_ok: Collection[str] = (),
    normalize: bool = False,
    empty: float = 1.0,
) -> float:
    """规则维度的加权分：Σ 分数 × 权重，四舍五入到 4 位（B04-16：以前四处各写一遍）。

    文学质量视图的条目分、成稿门的总分与「人物场景核心」、对抗排名分都走这一个公式——各自只选维度组与权重，
    要不要再乘证据上限见 :func:`automated_diagnostic_assessment`。``forced_ok`` 里的维度按满分算（成稿门：参考
    作者常态的维度）；``normalize`` 时除以这组维度的权重和，权重和不为正时给 ``empty``。没有信号的维度按满分算。
    """
    dims = tuple(dimensions)

    def score(dimension: str) -> float:
        if dimension in forced_ok:
            return 1.0
        return float(signals.get(dimension, {}).get("score", 1.0))

    total = sum(score(dimension) * float(weights.get(dimension, 0.0)) for dimension in dims)
    if not normalize:
        return round(total, 4)
    weight_total = sum(float(weights.get(dimension, 0.0)) for dimension in dims)
    return round(total / weight_total, 4) if weight_total > 0 else empty


def automated_evidence_sufficiency(text: str) -> float:
    """Estimate whether there is enough prose to interpret heuristic signals.

    This intentionally measures evidence volume/shape, not literary merit.
    Short cue lists such as "必须选择，付出代价，她推开门" cannot produce a
    high-confidence automated judgment merely by containing expected words.
    """
    normalized = _compact_ws(text)
    if not normalized:
        return 0.0
    substantive_chars = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", normalized))
    sentence_count = len(_sentences(normalized))
    char_coverage = min(1.0, substantive_chars / AUTOMATED_EVIDENCE_TARGET_CHARS)
    sentence_coverage = min(1.0, sentence_count / AUTOMATED_EVIDENCE_TARGET_SENTENCES)
    # Either enough sustained prose *or* enough sentence-level structure makes
    # the diagnostic usable.  Requiring both would unfairly punish a deliberate
    # long-sentence style; the human-evidence ceiling still prevents either
    # shape from being mistaken for literary excellence.
    return round(max(char_coverage, sentence_coverage), 4)


def automated_diagnostic_assessment(
    text: str,
    *,
    raw_diagnostic_score: float,
) -> dict[str, Any]:
    """Attach honest scope and a human-evidence ceiling to an automatic score."""
    if not text or not text.strip():
        evidence_sufficiency = 0.0
        score = 0.0
    else:
        evidence_sufficiency = automated_evidence_sufficiency(text)
        evidence_multiplier = 0.35 + (0.65 * evidence_sufficiency)
        score = round(
            min(
                AUTOMATED_DIAGNOSTIC_CEILING,
                max(0.0, min(1.0, float(raw_diagnostic_score))) * evidence_multiplier,
            ),
            4,
        )
    return {
        "score": score,
        "raw_diagnostic_score": round(max(0.0, min(1.0, float(raw_diagnostic_score))), 4),
        "evidence_sufficiency": evidence_sufficiency,
        "automated_ceiling": AUTOMATED_DIAGNOSTIC_CEILING,
        "scope": "diagnostic_floor_only",
        "human_judgment_required": True,
        "policy_evidence_eligible": False,
        "upper_bound_requires": "frozen_hidden_human_evidence",
    }


def _evidence_sufficiency_label(text: str) -> str:
    substantive_chars = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
    sentence_count = len(_sentences(text))
    return (
        f"{substantive_chars} substantive characters; {sentence_count} sentence units; "
        f"targets={AUTOMATED_EVIDENCE_TARGET_CHARS}/{AUTOMATED_EVIDENCE_TARGET_SENTENCES}"
    )

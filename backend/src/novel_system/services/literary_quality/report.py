"""「整维已忽略」的判定（成稿门用）：作者在写作台深改里把一维的发现全忽略了，成稿门就不再为这一维出 Q3 警告。

原是文学质量视图的报告层（统一形状的发现、定位、风险簇、跨场复用、推荐的下一步）；那一页与它的三个接口 2026-10-20
退役，只留这两个判定。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from novel_system.services.literary_quality.dimensions import rule_signal_id
from novel_system.services.literary_quality.rules import analyze_literary_quality


def ignored_dimensions_from_findings(findings: Iterable[Mapping[str, Any]], ignored_keys: Iterable[str]) -> set[str]:
    """Dimensions whose every finding in ``findings`` the author has ignored in the writer.

    The ignore list holds signal ids (``rule_signal_id``); a dimension counts as the author's
    decision only when all of its findings were ignored. The final-text gate drops its Q3
    warnings for those dimensions: a finding dismissed in the deep drawer must not come back
    as a warning in 成稿中心.
    """

    ignored = {str(key) for key in ignored_keys or [] if str(key)}
    if not ignored:
        return set()
    by_dimension: dict[str, list[str]] = {}
    for finding in findings:
        by_dimension.setdefault(str(finding.get("dimension") or ""), []).append(rule_signal_id(finding))
    return {
        dimension
        for dimension, ids in by_dimension.items()
        if dimension and ids and all(signal_id in ignored for signal_id in ids)
    }


def ignored_rule_dimensions(text: str, ignored_keys: Iterable[str]) -> set[str]:
    """:func:`ignored_dimensions_from_findings` over the house-rule findings of ``text``."""

    ignored = [str(key) for key in ignored_keys or [] if str(key)]
    if not ignored:
        return set()
    _, findings = analyze_literary_quality(text)
    return ignored_dimensions_from_findings(findings, ignored)

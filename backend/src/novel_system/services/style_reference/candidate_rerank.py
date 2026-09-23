"""Style target + candidate assessment (frozen-profile metric envelope).

2026-09-14 减法:候选重排层(shadow / active 模式、`candidate_rerank.yaml`、基准报告哈希授权、
`StyleCandidateReranker`、`rerank_candidate_pairs`)已删除。2026-09-23 风格参考 v3 起 Best-of-N 候选按
「像不像」读数(``fidelity`` 的 distance)排序,原文重合走唯一抄袭门(``reference_copy_gate``);这里的
内置抄袭守卫(``plagiarism_corpus``)与从未被赋值的 ``combined_score`` 随之删除。留下的是纯函数评分核:
``build_style_target``(冻结画像的量化基线 → 目标包络)与 ``assess_candidate_text``(候选文本对目标的
贴合读数),供 scene_generation 的候选审计读数、风格修复的不退步检查、以及 neutral_first 下的形状包络使用。
候选的 ``plagiarism_*`` 字段由调用方按抄袭门的结果填写。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from novel_system.services.hash_engine import canonical_json
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.metrics import (
    DEFAULT_FLOOR,
    METRIC_NAMES,
    PROSE_SHAPE_METRIC_NAMES,
    TYPE_RATIO_METRICS,
    compute_generated_metrics,
)
from novel_system.services.style_reference.runtime_contract import (
    blend_profile_metric_baselines,
)


SCORER_VERSION = "style_candidate_rerank_v2"
_TEXT_METRICS = tuple(
    name for name in METRIC_NAMES if name not in TYPE_RATIO_METRICS
) + PROSE_SHAPE_METRIC_NAMES

_METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "paragraph_shape": PROSE_SHAPE_METRIC_NAMES,
    "sentence_shape": (
        "avg_sentence_length",
        "sentence_length_std",
        "short_sentence_ratio",
        "long_sentence_ratio",
    ),
    "punctuation_rhythm": (
        "punctuation_density_per_1k",
        "dash_em_density_per_1k",
        "ellipsis_density_per_1k",
        "semicolon_density_per_1k",
        "question_density_per_1k",
    ),
    "register": ("classical_word_ratio", "colloquial_marker_ratio"),
    "figurative_proxy": ("metaphor_density_per_1k", "personification_density_per_1k"),
    # 2026-09-23:感官词表指标随测量核删除,sensory_proxy 组一并去掉
}


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None




@dataclass(frozen=True, slots=True)
class CandidateRerankPolicy:
    """评分核的阈值(不再从 yaml 读取,也没有 shadow / active 模式)。

    ``style_eligible`` 要求候选至少 ``min_substantive_chars`` 个可见字、``min_metric_count``
    个可比指标、置信度 ≥ ``min_confidence``。
    """

    min_substantive_chars: int = 300
    min_metric_count: int = 12
    min_confidence: float = 0.65


@dataclass(frozen=True, slots=True)
class StyleMetricTarget:
    metric: str
    mean: float
    std: float
    tolerance: float
    component_count: int


@dataclass(frozen=True, slots=True)
class StyleTarget:
    profile_ids: tuple[str, ...]
    metrics: Mapping[str, StyleMetricTarget]
    target_hash: str


@dataclass(slots=True)
class CandidateAssessment:
    row_id: str
    quality_score: float
    style_score: float | None = None
    style_confidence: float = 0.0
    metric_count: int = 0
    substantive_chars: int = 0
    group_scores: dict[str, float] = field(default_factory=dict)
    top_deviations: list[dict[str, Any]] = field(default_factory=list)
    plagiarism_checked: bool = False
    plagiarism_passed: bool | None = None
    plagiarism_hit_count: int = 0
    plagiarism_max_match_chars: int = 0
    style_eligible: bool = False
    rank: int | None = None
    selected: bool = False
    selection_reason: str = "quality_order"

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "quality_score": round(self.quality_score, 6),
            "style_score": (
                None if self.style_score is None else round(self.style_score, 6)
            ),
            "style_confidence": round(self.style_confidence, 6),
            "metric_count": self.metric_count,
            "substantive_chars": self.substantive_chars,
            "group_scores": {
                key: round(value, 6) for key, value in sorted(self.group_scores.items())
            },
            "top_deviations": self.top_deviations,
            "plagiarism_checked": self.plagiarism_checked,
            "plagiarism_passed": self.plagiarism_passed,
            "plagiarism_hit_count": self.plagiarism_hit_count,
            "plagiarism_max_match_chars": self.plagiarism_max_match_chars,
            "style_eligible": self.style_eligible,
            "rank": self.rank,
            "selected": self.selected,
            "selection_reason": self.selection_reason,
            "scorer_version": SCORER_VERSION,
        }



def build_style_target(
    profiles: Sequence[Any],
    *,
    floors: Mapping[str, Any] | None = None,
) -> StyleTarget | None:
    """Blend profile baselines using the same generic-to-specific weights as injection."""
    if not profiles:
        return None
    if floors is None:
        try:
            floors = load_yaml_config("tolerance_floors")
        except FileNotFoundError:
            floors = {}

    blended = blend_profile_metric_baselines(profiles)
    targets: dict[str, StyleMetricTarget] = {}
    for metric in _TEXT_METRICS:
        component = blended.get(metric)
        if not isinstance(component, Mapping):
            continue
        target_mean = float(component["mean"])
        target_std = float(component["std"])
        floor = _finite_float((floors or {}).get(metric, DEFAULT_FLOOR))
        if floor is None or floor <= 0:
            floor = DEFAULT_FLOOR
        targets[metric] = StyleMetricTarget(
            metric=metric,
            mean=target_mean,
            std=target_std,
            tolerance=max(target_std * 1.25, floor),
            component_count=int(component.get("component_count", 1)),
        )

    if not targets:
        return None
    profile_ids = tuple(str(getattr(profile, "profile_id", "")) for profile in profiles)
    projection = {
        "scorer_version": SCORER_VERSION,
        "profile_ids": profile_ids,
        "metrics": {
            name: {
                "mean": target.mean,
                "std": target.std,
                "tolerance": target.tolerance,
                "component_count": target.component_count,
            }
            for name, target in sorted(targets.items())
        },
    }
    return StyleTarget(
        profile_ids=profile_ids,
        metrics=targets,
        target_hash=hashlib.sha256(
            canonical_json(projection).encode("utf-8")
        ).hexdigest(),
    )


def _metric_group(metric: str) -> str:
    for group, metrics in _METRIC_GROUPS.items():
        if metric in metrics:
            return group
    return "other"


def assess_candidate_text(
    row_id: str,
    text: str,
    quality_score: float,
    target: StyleTarget | None,
    policy: CandidateRerankPolicy,
) -> CandidateAssessment:
    assessment = CandidateAssessment(row_id=row_id, quality_score=float(quality_score))
    assessment.substantive_chars = len(
        re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text or "")
    )

    if target is not None and text and text.strip():
        actual_metrics = compute_generated_metrics(text)
        grouped: dict[str, list[float]] = {}
        deviations: list[tuple[float, str]] = []
        for metric, metric_target in target.metrics.items():
            actual = _finite_float(actual_metrics.get(metric))
            if actual is None:
                continue
            deviation = abs(actual - metric_target.mean) / max(
                metric_target.tolerance, 1e-9
            )
            # Gaussian closeness makes tolerance meaningful (d=1 -> 0.607) while
            # clipping prevents one noisy lexical proxy from dominating diagnostics.
            closeness = math.exp(-0.5 * min(deviation, 4.0) ** 2)
            grouped.setdefault(_metric_group(metric), []).append(closeness)
            deviations.append((deviation, metric))

        assessment.metric_count = sum(len(values) for values in grouped.values())
        assessment.group_scores = {
            group: sum(values) / len(values)
            for group, values in grouped.items()
            if values
        }
        if assessment.group_scores:
            assessment.style_score = sum(assessment.group_scores.values()) / len(
                assessment.group_scores
            )
        # 老画像没有 2026-08 新增的段落形态 baseline；置信度应按该画像实际可比
        # 的 target 数计算，不能仅因 schema 迭代把历史画像系统性降权。
        metric_coverage = min(
            1.0, assessment.metric_count / max(1, len(target.metrics))
        )
        length_coverage = min(
            1.0, assessment.substantive_chars / max(1, policy.min_substantive_chars)
        )
        assessment.style_confidence = min(metric_coverage, length_coverage)
        assessment.style_eligible = bool(
            assessment.style_score is not None
            and assessment.metric_count >= policy.min_metric_count
            and assessment.substantive_chars >= policy.min_substantive_chars
            and assessment.style_confidence >= policy.min_confidence
        )
        assessment.top_deviations = [
            {"metric": metric, "deviation_ratio": round(deviation, 4)}
            for deviation, metric in sorted(
                deviations, key=lambda item: (-item[0], item[1])
            )[:5]
        ]
    return assessment

"""软质检的分数（风格参考 v3）：按模板声明的刻度换算到 0–1（只换一次）。

参考评审的记录在 ``style_reference.judge.judge_record``（2026-10-14 评审统一）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novel_system.services.qc_validator import apply_style_scores_alias


SOFT_QC_SCORE_FIELDS: tuple[str, ...] = ("style_score", "dimension_scores")


def _normalize_soft_qc_scores(payload: Mapping[str, Any], *, schema: Any = None) -> dict[str, Any]:
    """把软 QC 的分数统一换算到契约的 0–1（风格参考 v3：与准定稿验收共用 ``review_scores``）。**只调一次**——
    模型回答刚到、校验之前（``_qc_run_node_with_degradation``）；之后的环节拿到的都已是 0–1。

    刻度以这一次调用的模板声明为准（``schema`` = 模板的 ``structured_schema``，``style_score`` /
    ``dimension_scores`` 的 ``maximum``）：逐个分数按它换算，不在 ``[0, 刻度]`` 里的丢掉（总分丢掉就是没有总分，
    按维分丢掉那一维，旧的 ``style_dimensions`` 丢掉那一条）。模板没声明刻度（旧提示词快照）时才按这一次回答里的
    全部分数推断量级（``review_scores.score_scale`` 兜底）。只留 16 维里的键；没给 ``style_score`` 而给了按维分时，
    总分取按维分的均值。
    """
    from novel_system.services.review_scores import (
        judge_dimension_scores,
        normalize_score,
        response_score_scale,
    )

    normalized = dict(payload)
    # 旧的 style_scores 别名先摊成 style_dimensions / style_score，再和别的分数一起定刻度、换算——
    # 别名的 0–10 分数以前要到校验时才摊开，错过了换算，整遍软 QC 被判 invalid 豁免（B04-18）
    apply_style_scores_alias(normalized)
    dims = normalized.get("style_dimensions")
    judge = judge_dimension_scores(normalized.get("dimension_scores"))
    dim_scores = [dim.get("score") for dim in dims if isinstance(dim, Mapping)] if isinstance(dims, list) else []
    scale, _source = response_score_scale(
        schema, SOFT_QC_SCORE_FIELDS, [normalized.get("style_score"), *dim_scores, *judge.values()]
    )
    if normalized.get("style_score") is not None:
        normalized["style_score"] = normalize_score(normalized["style_score"], scale)
    if isinstance(dims, list):
        kept_dims: list[Any] = []
        for dim in dims:
            if not isinstance(dim, Mapping):
                kept_dims.append(dim)
                continue
            score = normalize_score(dim.get("score"), scale)
            if score is not None:
                kept_dims.append({**dim, "score": score})
        normalized["style_dimensions"] = kept_dims
    if "dimension_scores" in normalized:
        unit_scores = {key: normalize_score(value, scale) for key, value in judge.items()}
        normalized["dimension_scores"] = {key: value for key, value in unit_scores.items() if value is not None}
        if normalized.get("style_score") is None and normalized["dimension_scores"]:
            values = list(normalized["dimension_scores"].values())
            normalized["style_score"] = round(sum(values) / len(values), 4)
    return normalized

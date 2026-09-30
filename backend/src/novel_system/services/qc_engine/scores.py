"""软质检的分数（风格参考 v3）：按模板声明的刻度换算到 0–1（只换一次），参考评审的记录。"""

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


def _prompt_carries_reference(prompt: Mapping[str, Any] | None) -> bool:
    """这一次评审的提示里真的带着参考（注入器把 ``[STYLE_REFERENCE]`` 块接在 system 提示最前面）——降级、未命中、
    未绑定都不算。软 QC 模板的正文自己就提到「[STYLE_REFERENCE] 块」，所以只认开头，不认包含。"""
    if not isinstance(prompt, Mapping):
        return False
    audit = prompt.get("_style_reference_runtime_audit")
    if not isinstance(audit, Mapping) or str(audit.get("outcome") or "") != "hit":
        return False
    return str(prompt.get("system_prompt") or "").lstrip().startswith("[STYLE_REFERENCE]")


def _reference_judge_record(payload: Mapping[str, Any], *, carried: bool = True) -> dict[str, Any] | None:
    """参考评审的分数（10 分制，落 qc 报告与尝试记录）；没有按维分也没有总分 → None。

    风格参考 v3（L6）：只有这一次评审**是**参考评审时才记——提示里带着参考（``carried``），或回答给了 16 维的按维分。
    没绑定的场景（润色口径）模型也可能顺手给一个 ``style_score``，那不是「像不像」的评分，不能冒充参考评审总分。
    """
    from novel_system.services.review_scores import unit_to_judge_scale

    dims = payload.get("dimension_scores") if isinstance(payload.get("dimension_scores"), Mapping) else {}
    style_score = payload.get("style_score")
    if not dims and style_score is None:
        return None
    if not carried and not dims:
        return None
    return {
        "kind": "reference_judge",
        "scale": "0-10",
        "style_score": unit_to_judge_scale(style_score),
        "dimension_scores": {
            str(key): unit_to_judge_scale(value)
            for key, value in dims.items()
            if unit_to_judge_scale(value) is not None
        },
    }

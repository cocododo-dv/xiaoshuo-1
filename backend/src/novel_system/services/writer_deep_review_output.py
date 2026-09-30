"""写作台深评 / 局部深评 / 局部改写的模型输出归一（从 writer_deep_review 拆出）：只收模板声明的形状，
分数按模板声明的 0–1 刻度收、越界的丢掉。"""

from __future__ import annotations

from statistics import mean
from typing import Any

from novel_system.services.review_scores import normalize_score
from novel_system.services.scene_diagnosis import PASSAGE_RELATION_KINDS, PASSAGE_VERDICTS

LITERARY_REVISION_DIMENSIONS: tuple[str, ...] = (
    "character_contradiction",
    "choice_pressure",
    "relationship_tension",
    "dialogue_subtext",
    "information_rhythm",
    "voice_distinction",
    "image_necessity",
    "repetitive_expression",
    "ending_drive",
    "theme_pressure",
)
DEEP_REVIEW_LENSES: tuple[str, ...] = ("story", "character", "prose", "reader", "theme")


class WriterDeepReviewOutputError(ValueError):
    """The provider completed a call but violated a writer output contract."""


def _normalize_deep_review_output(payload: dict[str, Any]) -> dict[str, Any]:
    findings = _normalize_findings(payload.get("findings"))
    scores = _normalize_scores(payload.get("scores"))
    overall_score = _optional_score(payload.get("overall_score"))
    if overall_score is None:
        overall_score = round(mean(scores.values()), 2) if scores else None
    revision_brief = _normalize_revision_brief(payload.get("revision_brief"), findings)
    normalized_lenses = _normalize_lens_evaluations(payload.get("lens_evaluations"), findings)
    requires_human_review = bool(payload.get("requires_human_review"))
    if any(finding.get("severity") == "blocking" for finding in findings):
        requires_human_review = True
    return {
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
        "requires_human_review": requires_human_review,
        "lens_evaluations": normalized_lenses,
    }

def _normalize_lens_evaluations(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 模型直出的分组是权威来源（校验后采用）;缺失的镜头再从顶层 findings 的
    # lens 标签重建——不把顶层 findings 并入模型已给出的条目,否则同一条发现
    # 会在两处同时出现时被重复计入。
    findings_by_lens: dict[str, list[dict[str, Any]]] = {lens: [] for lens in DEEP_REVIEW_LENSES}
    for finding in findings:
        findings_by_lens[_coerce_lens(finding.get("lens")) or "story"].append(finding)
    by_lens: dict[str, dict[str, Any]] = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        lens = _coerce_lens(item.get("lens"))
        if lens is None:
            continue
        lens_findings = _normalize_findings(item.get("findings"))
        for finding in lens_findings:
            finding["lens"] = lens
        entry = by_lens.get(lens)
        if entry is None:
            by_lens[lens] = {
                "lens": lens,
                "overall_score": _optional_score(item.get("overall_score")),
                "scores": _normalize_scores(item.get("scores")),
                "findings": lens_findings,
                "revision_brief": _normalize_revision_brief(item.get("revision_brief"), lens_findings),
            }
        else:
            entry["findings"].extend(lens_findings)
            entry["revision_brief"].extend(_revision_brief_from_findings(lens_findings))
    for lens in DEEP_REVIEW_LENSES:
        lens_findings = findings_by_lens[lens]
        if lens in by_lens or not lens_findings:
            continue
        by_lens[lens] = {
            "lens": lens,
            "scores": _scores_for_findings(lens_findings),
            "findings": lens_findings,
            "revision_brief": _revision_brief_from_findings(lens_findings),
        }
    return [by_lens[lens] for lens in DEEP_REVIEW_LENSES if lens in by_lens]

def _coerce_lens(value: Any) -> str | None:
    lens = str(value or "").strip().lower()
    return lens if lens in DEEP_REVIEW_LENSES else None

def _normalize_scores(value: Any) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    if isinstance(value, dict):
        for dimension, raw_score in value.items():
            if dimension not in scores:
                continue
            score = _optional_score(raw_score)
            if score is not None:
                scores[dimension] = score
    return scores

def _optional_score(value: Any) -> float | None:
    """深评 / 局部深评模板声明 0–1 分（``structured_schema`` 的 minimum / maximum）：按声明的刻度收，越界的分丢掉
    （不夹成 0 / 1——按 0–10 习惯答的 7.5 夹成满分会假装成一个极端的评分；与 ``review_scores.normalize_score``
    同一口径）。"""
    score = normalize_score(value, 1.0)
    return None if score is None else round(score, 2)

def _lens_overall_score(value: Any, scores: dict[str, float]) -> float | None:
    """镜头行的总分：模型给了（合法的）就用它——0.0 也是分，不当作没给；没给才按各维分取平均。"""
    score = _optional_score(value)
    if score is not None:
        return score
    return round(mean(scores.values()), 2) if scores else None

def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding = dict(item)
        severity = str(finding.get("severity") or finding.get("classification") or "revision")
        if severity not in {"blocking", "revision", "taste", "ignore_ok"}:
            severity = "revision"
        finding["severity"] = severity
        finding["classification"] = str(finding.get("classification") or severity)
        finding["lens"] = _coerce_lens(finding.get("lens")) or "story"
        finding["dimension"] = str(finding.get("dimension") or "choice_pressure")
        finding["issue"] = str(finding.get("issue") or "")
        finding["recommendation"] = str(finding.get("recommendation") or "")
        finding["evidence_excerpt"] = str(finding.get("evidence_excerpt") or "")
        finding["evidence_location"] = str(finding.get("evidence_location") or "source text")
        finding["why_it_matters"] = str(finding.get("why_it_matters") or "")
        finding["scene_form"] = str(finding.get("scene_form") or "plot_scene")
        findings.append(finding)
    return findings

def _normalize_revision_brief(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [dict(item) for item in value if isinstance(item, dict)]
        if items:
            return items
    return _revision_brief_from_findings(findings)

def _scores_for_findings(findings: list[dict[str, Any]]) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    for finding in findings:
        dimension = finding.get("dimension")
        if dimension not in scores:
            continue
        if finding.get("severity") == "blocking":
            scores[dimension] = min(scores[dimension], 0.42)
        elif finding.get("severity") == "revision":
            scores[dimension] = min(scores[dimension], 0.58)
        elif finding.get("severity") == "taste":
            scores[dimension] = min(scores[dimension], 0.72)
    return scores

def _revision_brief_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("severity") == "ignore_ok":
            priority = "optional"
        elif finding.get("severity") == "taste":
            priority = "low"
        elif finding.get("severity") == "blocking":
            priority = "high"
        else:
            priority = "medium"
        brief.append(
            {
                "dimension": finding.get("dimension"),
                "classification": finding.get("severity"),
                "action": finding.get("recommendation"),
                "priority": priority,
                "evidence_excerpt": finding.get("evidence_excerpt", ""),
            }
        )
    return brief

def _normalize_passage_review_output(payload: Any, *, has_finding: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in PASSAGE_VERDICTS:
        verdict = "partly" if has_finding else "no_finding"
    if not has_finding and verdict in {"holds", "partly", "does_not_hold"}:
        verdict = "no_finding"
    findings = _normalize_findings(payload.get("findings"))
    for finding in findings:
        # 跨段发现：另一段的原话 + 标记里的段号（模型看到的是 1 起的序号，存 0 起的段索引）
        related_excerpt = str(finding.get("related_excerpt") or "").strip()
        finding["related_excerpt"] = related_excerpt
        raw_index = finding.get("related_paragraph_index")
        related_index: int | None = None
        if related_excerpt and raw_index not in (None, ""):
            try:
                related_index = int(raw_index) - 1
            except (TypeError, ValueError):
                related_index = None
        finding["related_paragraph_index"] = related_index if related_index is not None and related_index >= 0 else None
        relation = str(finding.get("relation") or "").strip().lower()
        finding["relation"] = relation if relation in PASSAGE_RELATION_KINDS else ("contradiction" if related_excerpt else "")
    return {
        "verdict": verdict,
        "assessment": str(payload.get("assessment") or "").strip(),
        "findings": findings,
        "rewrite_brief": str(payload.get("rewrite_brief") or "").strip(),
    }

def _normalize_patch_output(
    payload: Any,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": "fallback because patch response was not an object",
        }
    patches = payload.get("patches")
    if not isinstance(patches, list) or not patches:
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": str(payload.get("rationale") or "fallback because patch list was empty"),
        }
    options: list[dict[str, Any]] = []
    for index, patch in enumerate(patches[:3], start=1):
        if not isinstance(patch, dict):
            continue
        replacement_text = patch.get("replacement_text")
        if not isinstance(replacement_text, str) or not replacement_text.strip():
            continue
        changed_dimensions = patch.get("changed_dimensions") if isinstance(patch.get("changed_dimensions"), list) else []
        dimensions = [str(item) for item in changed_dimensions if isinstance(item, str) and item.strip()]
        tone = str(patch.get("tone") or (dimensions[0] if dimensions else issue_dimension))
        options.append(
            {
                "option_id": f"option_llm_{index}",
                "tone": tone,
                "label": str(patch.get("label") or f"版本 {index}"),
                "replacement_text": replacement_text.strip(),
                "changed_dimensions": dimensions or [issue_dimension],
                "why_it_helps": str(patch.get("why_it_helps") or patch.get("reason") or ""),
                "target_text_ref": str(patch.get("target_text_ref") or target_text_ref),
                "source_excerpt": str(patch.get("source_excerpt") or source_excerpt),
                "patch_type": str(patch.get("patch_type") or "replace_excerpt"),
            }
        )
    if not options:
        # 模型完全没给可用候选 → 确定性兜底(3 个)，沿用既有语义（离线测试覆盖）
        options = _replacement_options(source_excerpt, issue_dimension)
    elif len(options) < 2:
        # Fix B：模型仅回 1 个合法候选时，用确定性变体补足到 ≥2，保留「多选改写」UX 契约。
        # 诚实纪律：补足项 option_id 带 topup 前缀 + is_fallback_topup 标记可区分、不冒充模型产物；
        # 且补足时 rationale 不得整串落入前端 /offline deterministic/i 正则
        # （否则 ws-writer.jsx 会把整次真实改写误判为「模型不可用」而整体丢弃）。
        existing = {opt["replacement_text"].strip() for opt in options}
        for variant in _replacement_options(source_excerpt, issue_dimension):
            if len(options) >= 2:
                break
            text = str(variant.get("replacement_text") or "").strip()
            if not text or text in existing:
                continue
            options.append(
                {
                    "option_id": f"option_topup_{variant['option_id']}",
                    "tone": str(variant.get("tone") or issue_dimension),
                    "label": f"{variant.get('label') or '备选'}（确定性补足）",
                    "replacement_text": text,
                    "changed_dimensions": [*(variant.get("changed_dimensions") or []), "deterministic_topup"],
                    "why_it_helps": str(variant.get("why_it_helps") or ""),
                    "target_text_ref": target_text_ref,
                    "source_excerpt": source_excerpt,
                    "patch_type": "replace_excerpt",
                    "is_fallback_topup": True,
                }
            )
            existing.add(text)

    rationale = str(payload.get("rationale") or "")
    if any(opt.get("is_fallback_topup") for opt in options):
        rationale = (rationale + "（模型仅返回单个候选，已用确定性变体补足候选数；标注「确定性补足」的选项为非模型产物。）").strip()
    return {
        "replacement_options": options,
        "rationale": rationale,
    }

def _replacement_options(source_excerpt: str, issue_dimension: str) -> list[dict[str, Any]]:
    compressed = source_excerpt.strip().rstrip("。！？")
    return [
        {
            "option_id": "option_shorter",
            "tone": "shorter",
            "label": "更短",
            "replacement_text": f"{compressed}。",
            "changed_dimensions": [issue_dimension, "information_rhythm"],
            "why_it_helps": "压掉解释余量，让动作和停顿自己承担压力。",
        },
        {
            "option_id": "option_sharper",
            "tone": "sharper",
            "label": "更狠",
            "replacement_text": f"{compressed}。她没有补充理由，只把证据袋按进掌心。",
            "changed_dimensions": [issue_dimension, "relationship_tension"],
            "why_it_helps": "让角色拒绝解释，把锋利感放进动作后果。",
        },
        {
            "option_id": "option_subtler",
            "tone": "subtler",
            "label": "更含蓄",
            "replacement_text": f"{compressed}。话音落下后，她先看了一眼门缝。",
            "changed_dimensions": [issue_dimension, "dialogue_subtext"],
            "why_it_helps": "把明说转为观察和回避，保留读者自行判断的空间。",
        },
    ]

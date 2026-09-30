"""写作台深评 / 局部深评 / 局部改写的模型输出归一（从 writer_deep_review 拆出）：只收模板声明的形状，
分数按模板声明的 0–1 刻度收、越界的丢掉；模型没给的不编（没给的维度就没有分、没给的总分就是空，前端显示「—」）。"""

from __future__ import annotations

from typing import Any

from novel_system.services.review_scores import normalize_score
from novel_system.services.scene_diagnosis import (
    AI_DIMENSION_LABELS,
    LENS_LABELS,
    PASSAGE_RELATION_KINDS,
    PASSAGE_VERDICTS,
)

# 深评的十维与五个镜头：与诊断的中文名表同一份（顺序即表的顺序）
LITERARY_REVISION_DIMENSIONS: tuple[str, ...] = tuple(AI_DIMENSION_LABELS)
DEEP_REVIEW_LENSES: tuple[str, ...] = tuple(LENS_LABELS)
# 没给维度的发现记成「评审意见」（与诊断读评审行时的兜底同一个键），不冒充某个具体维度
REVIEW_NOTE_DIMENSION = "review_note"
# 局部改写：原文超过这么多字时只要两个选项（三个 2,000 字的改写放不进一次的输出上限，重评 R12）
PATCH_LONG_SOURCE_CHARS = 1000


class WriterPassagePatchEmpty(ValueError):
    """局部改写没有一个能用的选项（``reason``：``no_options`` 没给 / 全是空的；``paragraphs_collapsed`` 给的都把
    几段挤成了一段）。拒绝式：不再拿写死的句子凑数。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_deep_review_output(payload: dict[str, Any]) -> dict[str, Any]:
    findings = _normalize_findings(payload.get("findings"))
    scores = _normalize_scores(payload.get("scores"))
    # 总分只用模型给的（合法的）：没给 / 越界 → 空，不拿各维分（以前还掺着编出来的 0.78）凑一个平均
    overall_score = _optional_score(payload.get("overall_score"))
    revision_brief = _normalize_revision_brief(payload.get("revision_brief"), findings)
    normalized_lenses = _normalize_lens_evaluations(payload.get("lens_evaluations"), findings)
    # 模型回的 requires_human_review 不再落库：深评后面没有人工复核的流程（审计 B05-17），列留着、取默认值
    return {
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
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
        # 模型没分组的镜头按顶层发现重建：只有发现与改法，没有分数（以前按严重度编 0.42 / 0.58 / 0.72）
        by_lens[lens] = {
            "lens": lens,
            "scores": {},
            "findings": lens_findings,
            "revision_brief": _revision_brief_from_findings(lens_findings),
        }
    return [by_lens[lens] for lens in DEEP_REVIEW_LENSES if lens in by_lens]


def _coerce_lens(value: Any) -> str | None:
    lens = str(value or "").strip().lower()
    return lens if lens in DEEP_REVIEW_LENSES else None


def _normalize_scores(value: Any) -> dict[str, float]:
    """模型给的各维分（只认十个维度、只收合法的分）；没给的维度就没有分——以前一律先填 0.78。"""

    scores: dict[str, float] = {}
    if isinstance(value, dict):
        for dimension, raw_score in value.items():
            if dimension not in LITERARY_REVISION_DIMENSIONS:
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


def _lens_overall_score(value: Any) -> float | None:
    """镜头行的总分：模型给了（合法的）就用它——0.0 也是分，不当作没给；没给就是空。"""
    return _optional_score(value)


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
        finding["dimension"] = str(finding.get("dimension") or "").strip() or REVIEW_NOTE_DIMENSION
        finding["issue"] = str(finding.get("issue") or "")
        finding["recommendation"] = str(finding.get("recommendation") or "")
        finding["evidence_excerpt"] = str(finding.get("evidence_excerpt") or "")
        finding["evidence_location"] = str(finding.get("evidence_location") or "source text")
        finding["why_it_matters"] = str(finding.get("why_it_matters") or "")
        findings.append(finding)
    return findings


def _normalize_revision_brief(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [dict(item) for item in value if isinstance(item, dict)]
        if items:
            return items
    return _revision_brief_from_findings(findings)


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


def _normalize_patch_output(payload: Any, *, source_excerpt: str, issue_dimension: str) -> dict[str, Any]:
    """局部改写的输出（``writer_passage_patch`` v4）：每个选项是一组段落（``paragraphs``，阅读顺序，一段一个字符串）；
    ``replacement_text`` 是按换行拼起来的同一份字（旧的读取方照旧读它）。还没同步提示词快照的安装仍按 v3 回
    ``replacement_text``：按换行拆段。

    拒绝式（作者 2026-09-15「没有模型就不兜底」）：没有一个可用的选项 → :class:`WriterPassagePatchEmpty`，调用方回错；
    只有一个就给一个——以前拿退役演示故事里写死的句子（「证据袋」「门缝」）补足三个 / 两个选项，会被作者插进正文。
    把几段挤成了一段的选项（原文有两段以上，或者一段里挤进了好几句对白而原文没有这样写）不要，不去替作者拼。"""

    if not isinstance(payload, dict):
        raise WriterPassagePatchEmpty("no_options")
    patches = payload.get("patches")
    if not isinstance(patches, list):
        raise WriterPassagePatchEmpty("no_options")
    source_paragraphs = split_paragraphs(source_excerpt)
    options: list[dict[str, Any]] = []
    collapsed = 0
    for index, patch in enumerate(patches[:3], start=1):
        if not isinstance(patch, dict):
            continue
        paragraphs = _option_paragraphs(patch)
        if not paragraphs:
            continue
        if paragraphs_collapsed(source_paragraphs, paragraphs):
            collapsed += 1
            continue
        changed_dimensions = patch.get("changed_dimensions") if isinstance(patch.get("changed_dimensions"), list) else []
        dimensions = [str(item) for item in changed_dimensions if isinstance(item, str) and item.strip()]
        tone = str(patch.get("tone") or (dimensions[0] if dimensions else issue_dimension))
        options.append(
            {
                "option_id": f"option_llm_{index}",
                "tone": tone,
                "label": str(patch.get("label") or f"版本 {index}"),
                "paragraphs": paragraphs,
                "replacement_text": "\n".join(paragraphs),
                "changed_dimensions": dimensions or [issue_dimension],
                "why_it_helps": str(patch.get("why_it_helps") or patch.get("reason") or ""),
                "patch_type": str(patch.get("patch_type") or "replace_excerpt"),
            }
        )
    if not options:
        raise WriterPassagePatchEmpty("paragraphs_collapsed" if collapsed else "no_options")
    return {"replacement_options": options, "rationale": str(payload.get("rationale") or "")}


def split_paragraphs(text: str) -> list[str]:
    """一段选区 / 一个选项按换行拆成段（空行不算段）。"""

    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _option_paragraphs(patch: dict[str, Any]) -> list[str]:
    raw = patch.get("paragraphs")
    if isinstance(raw, list):
        return [part for item in raw if isinstance(item, str) for part in split_paragraphs(item)]
    replacement = patch.get("replacement_text")
    return split_paragraphs(replacement) if isinstance(replacement, str) else []


_OPENING_QUOTES = ("“", "「", "『")


def _utterances(paragraph: str) -> int:
    return sum(paragraph.count(mark) for mark in _OPENING_QUOTES)


def paragraphs_collapsed(source_paragraphs: list[str], option_paragraphs: list[str]) -> bool:
    """这个选项是不是把几段挤成了一段（与起草的「整场挤成一段」同一个意思）：选项只有一段，而原文有两段以上；或者
    这一段里挤进了两句以上的对白、原文却没有哪一段这样写（小说对白一句一段）。"""

    if len(option_paragraphs) != 1:
        return False
    if len(source_paragraphs) >= 2:
        return True
    source_packs_dialogue = any(_utterances(paragraph) >= 2 for paragraph in source_paragraphs)
    return _utterances(option_paragraphs[0]) >= 2 and not source_packs_dialogue

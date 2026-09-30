"""场景正文的确定性门：取正文、文本完整性、对场景卡硬约束的读数、三种验收与房风（去模板）门。

三个验收（首稿 / 风格改写 / 去模板改写）以前各自把「必写组哪些满足 / 禁用词哪些出现 / 完整性标记 / 可见字数 /
长度适配度」重算一遍，来源稿与改写稿的对比块还复制了两份。现在一份文字对一张场景卡只读一次
（:class:`ConstraintSnapshot`），对比只算一次（:class:`ConstraintDiff`），三个验收都是它们的投影——输出的键逐字
不变（写进 AttemptTracker.details_json，续跑从那里读回）。

``_anti_template_quality_gate`` / ``_assess_de_template_rewrite`` / ``_assess_neutral_draft`` 是测试替换的接缝：
流程模块按 ``text_gates.<名字>`` 在调用时取，替换这个模块上的名字即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from novel_system.db.models import SceneCard
from novel_system.services.literary_quality import weighted_score
from novel_system.services.literary_signals import rule_analysis
from novel_system.services.llm_client import LLMResponse
from novel_system.services.qc_constraints import (
    contains_forbidden_term,
    forbidden_terms as card_forbidden_terms,
    required_groups,
    required_groups_missing,
)
from novel_system.services.scene_generation.contracts import SceneGenerationPostprocessError
from novel_system.services.scene_generation.length_policy import LengthPolicy, _length_fitness


_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_BARE_CJK_UNICODE_ESCAPE_RE = re.compile(
    r"(?<=[\u3400-\u9fff])u([0-9a-fA-F]{4})(?=$|[\s\u3000-\u303f\u3400-\u9fff，。！？；：、])"
)
_C1_CONTROL_RE = re.compile(r"[\u0080-\u009f]")
_ORPHAN_LOWERCASE_CJK_RE = re.compile(
    r"(?m)(?:^|[。！？!?；;：:]\s*)[A-Za-z](?=[\u3400-\u9fff])"
)
_MODEL_RESPONSE_ARTIFACT_RE = re.compile(
    r"(?i)(?:```(?:json)?|<ctrl\d+>|\blet me (?:refine|construct|rewrite|check)|"
    r"\bthe (?:actual json|draft looks|final json)|source draft row id|"
    r"[\"']scene_text[\"']\s*:)"
)


# 风格参考 v3 复核（A/B 实测）：模型把 JSON 字符串里的换行吞掉时，整场会回成一整块——对白、叙述、动作挤在
# 一个段落里。中文小说有对白就一定分段：够长、有对白引号、却一个换行都没有 = 文本损坏，与乱码同一级。
PARAGRAPH_COLLAPSE_MIN_CHARS = 800
PARAGRAPH_COLLAPSE_MIN_QUOTES = 4
_DIALOGUE_QUOTE_RE = re.compile(r"[“”「」『』]")


def _paragraphs_collapsed(text: str) -> bool:
    stripped = str(text or "").strip()
    if "\n" in stripped:
        return False
    if _visible_char_count(stripped) < PARAGRAPH_COLLAPSE_MIN_CHARS:
        return False
    return len(_DIALOGUE_QUOTE_RE.findall(stripped)) >= PARAGRAPH_COLLAPSE_MIN_QUOTES


def _scene_text_integrity_markers(text: str) -> list[str]:
    """返回不含正文内容的完整性标记，供改写验收和审计使用。"""
    markers: list[str] = []
    if _paragraphs_collapsed(text):
        markers.append("paragraphs_collapsed")
    if "\ufffd" in text:
        markers.append("replacement_character")
    if "???" in text:
        markers.append("question_mark_placeholder")
    if _C1_CONTROL_RE.search(text):
        markers.append("c1_control_character")
    if _LITERAL_UNICODE_ESCAPE_RE.search(text):
        markers.append("literal_unicode_escape")
    if re.search(r"(?<![A-Za-z0-9_])u[0-9a-fA-F]{4}(?![A-Za-z0-9_])", text):
        markers.append("bare_unicode_escape")
    if _ORPHAN_LOWERCASE_CJK_RE.search(text):
        markers.append("orphan_ascii_before_cjk")
    if _MODEL_RESPONSE_ARTIFACT_RE.search(text):
        markers.append("model_response_artifact")
    return markers


def _visible_char_count(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _normalize_literal_unicode_escapes(text: str) -> str:
    """只还原可无歧义识别的 Unicode 转义残片，不做通用 escape 解码。"""

    def replace_escaped(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        # 单个 surrogate 不是合法正文字符；保留给完整性门拒绝，避免制造坏串。
        if 0xD800 <= codepoint <= 0xDFFF:
            return match.group(0)
        return chr(codepoint)

    normalized = _LITERAL_UNICODE_ESCAPE_RE.sub(replace_escaped, text)

    def replace_bare_cjk(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        if 0x3000 <= codepoint <= 0x303F or 0x3400 <= codepoint <= 0x9FFF:
            return chr(codepoint)
        return match.group(0)

    return _BARE_CJK_UNICODE_ESCAPE_RE.sub(replace_bare_cjk, normalized)


def _extract_scene_text(response: LLMResponse) -> str:
    structured_output = response.structured_output or {}
    scene_text = structured_output.get("scene_text")
    if isinstance(scene_text, str) and scene_text.strip():
        return _normalize_literal_unicode_escapes(scene_text.strip())
    # 中性/风格/补丁/续写各路径共用此提取器，消息不指认具体 stage（审计 P-17）
    raise SceneGenerationPostprocessError(
        llm_call_id=getattr(response, "llm_call_id", None),
        message="llm generation response missing scene_text",
    )


ANTI_TEMPLATE_GATE_DIMENSIONS = {
    "model_voice",
    "image_homogeneity",
    "repetitive_action",
    "template_action_reuse",
    "image_field_reuse",
    "syntax_monotony",
    "false_clarity",
    "summary_ending",
    "expository_dialogue",
    "decorative_imagery",
    "dialogue_as_report",
    "over_explained_motive",
    "false_poetic_closure",
    "self_repetition",
}


# ---------------------------------------------------------------------------
# 对场景卡硬约束的读数
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConstraintSnapshot:
    """一份文字对一张场景卡硬约束的确定性读数：必写组哪些满足、禁用词哪些出现（只经
    ``qc_constraints.forbidden_terms`` 读，防抄袭政策句不是禁用词表）、完整性标记、可见字数与长度带。"""

    required_terms: tuple[str, ...]
    satisfied: frozenset[str]
    forbidden_terms: tuple[str, ...]
    forbidden_present: frozenset[str]
    integrity_markers: tuple[str, ...]
    visible_chars: int
    length_range: tuple[int, int] | None

    @classmethod
    def read(cls, scene: Any, text: str, lengths: LengthPolicy | None) -> ConstraintSnapshot:
        """``lengths`` 为 ``None``：调用方不关心长度（修复简报只看缺项、禁用词与完整性），``length_range`` 为空。

        必写组与禁用词的判定与硬质检、分类器复核、成稿门同一处（``qc_constraints.required_groups_missing`` /
        ``contains_forbidden_term``，批准#11）：一整段分不出 ≥2 字的组时整段算一组（与质检同口径）。"""
        required = tuple(required_groups(scene.must_include_text))
        missing = set(required_groups_missing(scene.must_include_text, text))
        forbidden = tuple(card_forbidden_terms(scene.forbidden_text))
        return cls(
            required_terms=required,
            satisfied=frozenset(term for term in required if term not in missing),
            forbidden_terms=forbidden,
            forbidden_present=frozenset(term for term in forbidden if contains_forbidden_term(term, text)),
            integrity_markers=tuple(_scene_text_integrity_markers(text)),
            visible_chars=_visible_char_count(text),
            length_range=lengths.hard_range() if lengths is not None else None,
        )

    @property
    def missing_required(self) -> list[str]:
        """没满足的必写组（按场景卡上的次序）。"""
        return [term for term in self.required_terms if term not in self.satisfied]

    @property
    def forbidden_hits(self) -> list[str]:
        """出现了的禁用词（按场景卡上的次序）。"""
        return [term for term in self.forbidden_terms if term in self.forbidden_present]

    @property
    def length_score(self) -> float:
        return _length_fitness(self.visible_chars, self.length_range)

    @property
    def length_missed(self) -> bool:
        return self.length_range is not None and self.length_score < 1.0


@dataclass(frozen=True, slots=True)
class ConstraintDiff:
    """改写稿相对事实基线（来源稿 / 权威稿）新添的硬约束问题——基线本来就有的不算。"""

    lost_required: list[str]
    missing_required: list[str]
    missing_without_regression: list[str]
    new_forbidden: list[str]
    integrity_regressed: bool

    @classmethod
    def between(cls, source: ConstraintSnapshot, rewritten: ConstraintSnapshot) -> ConstraintDiff:
        lost = sorted(source.satisfied - rewritten.satisfied)
        missing = sorted(set(rewritten.required_terms) - rewritten.satisfied)
        new_integrity = set(rewritten.integrity_markers) - set(source.integrity_markers)
        return cls(
            lost_required=lost,
            missing_required=missing,
            missing_without_regression=sorted(set(missing) - set(lost)),
            new_forbidden=sorted(rewritten.forbidden_present - source.forbidden_present),
            integrity_regressed=bool(new_integrity)
            or len(rewritten.integrity_markers) > len(source.integrity_markers),
        )

    def safety_reasons(self, rewritten: ConstraintSnapshot) -> list[str]:
        """改写的硬安全原因码，次序固定（长度另算：两个改写验收对长度的口径不同）。"""
        reasons: list[str] = []
        if rewritten.visible_chars < 20:
            reasons.append("rewrite_too_short")
        if self.lost_required:
            reasons.append("required_facts_regressed")
        if self.missing_without_regression:
            reasons.append("required_facts_missing")
        if self.new_forbidden:
            reasons.append("forbidden_content_added")
        if self.integrity_regressed:
            reasons.append("text_integrity_regressed")
        return reasons


def _assess_neutral_draft(scene: SceneCard, content: str, lengths: LengthPolicy) -> dict[str, Any]:
    """首稿 / 中性稿的确定性验收（不合格 → 一次确定性修复）。"""
    snapshot = ConstraintSnapshot.read(scene, content, lengths)
    reasons: list[str] = []
    if snapshot.visible_chars < 20:
        reasons.append("draft_too_short")
    if snapshot.missing_required:
        reasons.append("required_facts_missing")
    if snapshot.forbidden_hits:
        reasons.append("forbidden_content_present")
    if snapshot.integrity_markers:
        reasons.append("text_integrity_invalid")
    if snapshot.length_missed:
        reasons.append("target_length_not_met")
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(snapshot.required_terms),
        "missing_required_fact_count": len(snapshot.missing_required),
        "forbidden_hit_count": len(snapshot.forbidden_hits),
        "visible_chars": snapshot.visible_chars,
        "target_length_range": list(snapshot.length_range) if snapshot.length_range else None,
        "length_score": round(snapshot.length_score, 4),
        "integrity_markers": list(snapshot.integrity_markers),
    }


def _anti_template_quality_gate(
    text: str, *, scene_id: str, chapter_id: str
) -> dict[str, Any]:
    signals, findings = rule_analysis(text)
    # 这组维度的加权分，除以这组的权重和（B04-16：与文学质量视图、成稿门、对抗排名同一个公式）
    score = weighted_score(signals, ANTI_TEMPLATE_GATE_DIMENSIONS, normalize=True)
    risky_findings = [
        {
            **finding,
            "quality_signal_id": f"quality:scene:{scene_id}:{finding.get('dimension')}",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
        }
        for finding in findings
        if finding.get("dimension") in ANTI_TEMPLATE_GATE_DIMENSIONS
    ]
    triggered = bool(risky_findings)
    return {
        "triggered": triggered,
        "rewrite_pass": 1 if triggered else 0,
        "score": score,
        "risk_dimensions": [finding["dimension"] for finding in risky_findings],
        "quality_signal_ids": [
            finding["quality_signal_id"] for finding in risky_findings
        ],
        "findings": risky_findings,
    }


def _assess_style_base_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    rewritten_content: str,
    lengths: LengthPolicy,
) -> dict[str, Any]:
    """第一遍风格改写的硬安全门；审美质量留给后续质量门。"""
    source = ConstraintSnapshot.read(scene, source_content, lengths)
    rewritten = ConstraintSnapshot.read(scene, rewritten_content, lengths)
    diff = ConstraintDiff.between(source, rewritten)
    reasons = diff.safety_reasons(rewritten)
    if rewritten.length_missed:
        reasons.append("target_length_not_met")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(rewritten.required_terms),
        "source_required_fact_matches": len(source.satisfied),
        "rewritten_required_fact_matches": len(rewritten.satisfied),
        "lost_required_fact_count": len(diff.lost_required),
        "missing_required_fact_count": len(diff.missing_required),
        "new_forbidden_count": len(diff.new_forbidden),
        "rewritten_visible_chars": rewritten.visible_chars,
        "target_length_range": list(rewritten.length_range) if rewritten.length_range is not None else None,
        "rewritten_length_score": round(rewritten.length_score, 4),
        "source_integrity_markers": list(source.integrity_markers),
        "rewritten_integrity_markers": list(rewritten.integrity_markers),
    }


def assess_rewrite_regressions(
    scene: SceneCard,
    bundle: Mapping[str, Any] | None,
    *,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """「改一整场」的产物（定稿改写）相对来源稿的确定性回退（风格参考 v3 复核，A/B 实测）。

    在与起草同一个长度带放宽下（:meth:`LengthPolicy.for_scene`）评估改写稿与来源稿，只报改写稿**新添**的问题——
    来源稿本来就有的（例如本来就缺的必写项、本来就不在长度带里）不算回退。返回 ``{"regressed", "reasons",
    "rewritten_integrity_markers"}``；``reasons`` 用 :func:`_assess_style_base_rewrite` 的原因码。"""
    lengths = LengthPolicy.for_scene(bundle, scene)
    rewritten = _assess_style_base_rewrite(
        scene=scene, source_content=source_content, rewritten_content=rewritten_content, lengths=lengths
    )
    baseline = _assess_style_base_rewrite(
        scene=scene, source_content=source_content, rewritten_content=source_content, lengths=lengths
    )
    carried = set(baseline.get("reasons") or [])
    reasons = [reason for reason in rewritten.get("reasons") or [] if reason not in carried]
    return {
        "regressed": bool(reasons),
        "reasons": reasons,
        "rewritten_integrity_markers": list(rewritten.get("rewritten_integrity_markers") or []),
    }


def _assess_de_template_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str | None = None,
    rewritten_content: str,
    source_quality_gate: dict[str, Any],
    style_conformance: dict[str, Any] | None = None,
    lengths: LengthPolicy,
) -> dict[str, Any]:
    """确定性验收一次去模板改写；只阻止可证明的回退，不猜测作者审美。"""
    source_length = _visible_char_count(source_content)
    # 事实 / 禁用词 / 完整性以权威稿为基线（安全修复时是已批准的中性稿），长度与质量仍比来源稿
    safety_source = ConstraintSnapshot.read(scene, authoritative_content or source_content, lengths)
    rewritten = ConstraintSnapshot.read(scene, rewritten_content, lengths)
    diff = ConstraintDiff.between(safety_source, rewritten)
    rewritten_length = rewritten.visible_chars
    length_range = rewritten.length_range
    source_length_score = _length_fitness(source_length, length_range)
    rewritten_length_score = rewritten.length_score

    rewritten_quality_gate = _anti_template_quality_gate(
        rewritten_content,
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
    )
    source_quality_score = float(source_quality_gate.get("score") or 0.0)
    rewritten_quality_score = float(rewritten_quality_gate.get("score") or 0.0)
    source_risk_count = len(source_quality_gate.get("findings") or [])
    rewritten_risk_count = len(rewritten_quality_gate.get("findings") or [])
    source_target_counts = _quality_gate_dimension_counts(source_quality_gate)
    rewritten_target_counts = _quality_gate_dimension_counts(rewritten_quality_gate)
    source_target_evidence_available = any(
        isinstance(finding, dict)
        and str(finding.get("dimension") or "").strip()
        in ANTI_TEMPLATE_GATE_DIMENSIONS
        for finding in (source_quality_gate.get("findings") or [])
    )
    resolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) < count
    )
    unresolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) >= count
    )
    new_quality_risk_dimensions = sorted(
        set(rewritten_target_counts) - set(source_target_counts)
    )
    worsened_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) > count
    )

    reasons = diff.safety_reasons(rewritten)
    if length_range is not None:
        if rewritten_length_score < 1.0:
            reasons.append("target_length_not_met")
    elif source_length > 0:
        length_ratio = rewritten_length / source_length
        if length_ratio < 0.6:
            reasons.append("rewrite_collapsed")
        elif length_ratio > 1.8:
            reasons.append("rewrite_bloated")
    # 安全修复的唯一职责是把被拒风格稿恢复到事实/长度/禁词/文本完整性硬约束内。
    # 此时 source_quality_gate 还人为追加了 style_safety finding，且原稿本身不可交付；
    # 再要求启发式去模板分数不下降，会把已经安全、仍保留风格的修复稿错误退回中性稿。
    # 普通 de-template 改写仍维持严格非回退门。
    enforce_quality_non_regression = authoritative_content is None
    if enforce_quality_non_regression:
        if rewritten_quality_score + 0.005 < source_quality_score:
            reasons.append("anti_template_quality_regressed")
        if rewritten_risk_count > source_risk_count:
            reasons.append("anti_template_risks_increased")
        # A repair must demonstrably remove at least one of the exact dimensions
        # that triggered it.  A flat total-risk count previously accepted edits
        # that merely exchanged one defect for another or left every requested
        # defect untouched.
        # Only demand dimension-by-dimension proof when the source gate carries
        # its actionable findings.  Older checkpoints persisted only a compact
        # ``risk_dimensions`` list; treating that compatibility fallback as
        # full evidence would reject a valid completed rewrite on resume even
        # though the old record cannot support a before/after comparison.
        if source_target_evidence_available:
            if source_target_counts and not resolved_target_dimensions:
                reasons.append("target_quality_defects_not_reduced")
            if worsened_target_dimensions or new_quality_risk_dimensions:
                reasons.append("target_quality_defects_worsened")

    # 普通去模板改写只是对已安全风格稿做局部修补，不能用通用质量收益交换
    # 「像不像」读数上的可证明退步（改写稿比来源稿远出 patch_max_distance_increase）。
    # 仅在两稿读数都可信时启用；安全修复仍以事实/长度/禁词/文本完整性为最高优先级。
    conformance = dict(style_conformance or {})
    enforce_style_non_regression = bool(
        authoritative_content is None and conformance.get("comparable") is True
    )
    if enforce_style_non_regression and conformance.get("regressed") is True:
        reasons.append("style_conformance_regressed")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(rewritten.required_terms),
        "source_required_fact_matches": len(safety_source.satisfied),
        "rewritten_required_fact_matches": len(rewritten.satisfied),
        "lost_required_fact_count": len(diff.lost_required),
        "missing_required_fact_count": len(diff.missing_required),
        "new_forbidden_count": len(diff.new_forbidden),
        "source_visible_chars": source_length,
        "rewritten_visible_chars": rewritten_length,
        "target_length_range": list(length_range) if length_range is not None else None,
        "source_length_score": round(source_length_score, 4),
        "rewritten_length_score": round(rewritten_length_score, 4),
        "source_quality_score": round(source_quality_score, 4),
        "rewritten_quality_score": round(rewritten_quality_score, 4),
        "source_risk_count": source_risk_count,
        "rewritten_risk_count": rewritten_risk_count,
        "source_target_risk_counts": source_target_counts,
        "rewritten_target_risk_counts": rewritten_target_counts,
        "resolved_target_dimensions": resolved_target_dimensions,
        "unresolved_target_dimensions": unresolved_target_dimensions,
        "new_quality_risk_dimensions": new_quality_risk_dimensions,
        "worsened_target_dimensions": worsened_target_dimensions,
        "source_target_evidence_available": source_target_evidence_available,
        "quality_non_regression_enforced": enforce_quality_non_regression,
        "style_non_regression_enforced": enforce_style_non_regression,
        "style_conformance": conformance,
        "source_integrity_markers": list(safety_source.integrity_markers),
        "rewritten_integrity_markers": list(rewritten.integrity_markers),
        "authoritative_source_used": authoritative_content is not None,
    }


def _quality_gate_dimension_counts(quality_gate: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    findings = quality_gate.get("findings") or []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        dimension = str(finding.get("dimension") or "").strip()
        if dimension not in ANTI_TEMPLATE_GATE_DIMENSIONS:
            continue
        counts[dimension] = counts.get(dimension, 0) + 1
    # Some recovery tests and old checkpoints persisted only risk_dimensions.
    # Use them as a one-count fallback without double-counting full findings.
    for raw_dimension in quality_gate.get("risk_dimensions") or []:
        dimension = str(raw_dimension or "").strip()
        if dimension in ANTI_TEMPLATE_GATE_DIMENSIONS and dimension not in counts:
            counts[dimension] = 1
    return dict(sorted(counts.items()))

"""统一质量分级分类器（结果闭环治理 §5.4/§6.1，Wave 2）。

四级分类：
- Q0 数据持久化、来源泄漏、血缘缺失、安全错误 —— 阻断归档，保留正文
- Q1 有确定证据的硬事实冲突 —— 阻断自动归档，交作者确认或修订
- Q2 结构、节奏、钩子、代价、关系转折不足 —— 正文照常交付，醒目警告
- Q3 AI 口癖、比喻、句式、风格与实验指标 —— 只进诊断，不改变状态

分类纪律（§5.4，任何调用方不得绕过）：
- 单个 LLM 判断不能直接生成 Q0/Q1：升级必须由确定性复核器对证据复核通过，
  复核器 id 落 ``verified_by``（§6.1 提案—复核的落库形态）。
- ``blocking`` 由 ``quality_level`` 派生并强制一致（Q2/Q3 恒 false），
  不允许"Q2 但 blocking=true"的自由组合。
- 无法给出确定证据时自动降 Q2，记录 ``downgraded_from`` / ``downgrade_reason``。
- 未注册的 issue_key 一律默认 Q2（保守不阻断）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novel_system.db.models import SceneCard
from novel_system.services.qc_constraints import contains_forbidden_term, forbidden_terms, source_field_satisfied

Q0 = "Q0"
Q1 = "Q1"
Q2 = "Q2"
Q3 = "Q3"
BLOCKING_LEVELS = frozenset({Q0, Q1})

_LEVEL_RECOMMENDED_ACTION = {
    Q0: "resolve_safety_issue",
    Q1: "confirm_or_revise",
    Q2: "review_warning_optional_fix",
    Q3: "diagnostic_only",
}


@dataclass(frozen=True, slots=True)
class IssuePolicy:
    """verified_level：确定性复核通过时的级别；fallback_level：未复核时的级别。

    verified_level ∈ {Q0, Q1} 的条目必须声明 verifier_id；
    fallback_level 永远不得是阻断级（注册表不变量，由测试守住）。
    """

    verified_level: str
    verifier_id: str | None
    fallback_level: str


ISSUE_KEY_POLICY: dict[str, IssuePolicy] = {
    # ---- Q0 候选（数据/来源安全） ----
    "source_leak_risk": IssuePolicy(Q0, "source_safety_scan", Q2),
    "style_plagiarism": IssuePolicy(Q0, "style_plagiarism_ngram", Q2),
    # ---- Q1 候选（有确定证据的硬事实冲突） ----
    "character_pronoun_drift": IssuePolicy(Q1, "character_contract_detector", Q2),
    "event_log_consistency_violation": IssuePolicy(Q1, "narrative_event_log_keyword", Q2),
    "forbidden_text": IssuePolicy(Q1, "scene_card_forbidden_term", Q2),
    "missing_required_text": IssuePolicy(Q1, "scene_card_required_text", Q2),
    "missing_hard_constraint": IssuePolicy(Q1, "scene_card_required_text", Q2),
    # ---- Q2（结构/节奏层；含旧阻断词表中无确定性生产者的键） ----
    "scene_conflict_missing": IssuePolicy(Q2, None, Q2),
    "instruction_residue": IssuePolicy(Q2, None, Q2),
    "mechanical_required_beat_listing": IssuePolicy(Q2, None, Q2),
    "unsupported_event": IssuePolicy(Q2, None, Q2),
    "duplicate_text": IssuePolicy(Q2, None, Q2),
    "character_role_inconsistency": IssuePolicy(Q2, None, Q2),
    "event_log_consistency_llm_flag": IssuePolicy(Q2, None, Q2),
    "theme_relevance_warning": IssuePolicy(Q2, None, Q2),
    # ---- Q3（风格/口癖/实验指标） ----
    "character_pronoun_ambiguity": IssuePolicy(Q3, None, Q3),
    "character_pronoun_continuity": IssuePolicy(Q3, None, Q3),
}

_DEFAULT_POLICY = IssuePolicy(Q2, None, Q2)
_Q3_PREFIXES = ("style_", "tension_")
_EXECUTION_FAILURE_MARKERS = ("_execution_failed", "invalid_hard_qc_payload", "invalid_soft_qc_payload", "continuity_budget")


def _policy_for(issue_key: str) -> IssuePolicy:
    policy = ISSUE_KEY_POLICY.get(issue_key)
    if policy is not None:
        return policy
    if any(issue_key.startswith(prefix) for prefix in _Q3_PREFIXES):
        return IssuePolicy(Q3, None, Q3)
    if any(marker in issue_key for marker in _EXECUTION_FAILURE_MARKERS):
        # QC 自身执行失败不是正文的错：交付照常，只留 Q2 警告（§5.4/§7.7）
        return IssuePolicy(Q2, None, Q2)
    return _DEFAULT_POLICY


# ---------- 确定性复核器（提案 → 复核的唯一升级通道） ----------

def _verify_source_leak(scene: SceneCard | None, content: str, issue: dict[str, Any]) -> dict[str, Any] | None:
    from novel_system.services.source_safety import scan_source_safety

    scan = scan_source_safety(content or "")
    if scan.get("safe", True):
        return None
    blocked = scan.get("blocked_terms") or []
    return {
        "verified_by": "source_safety_scan",
        "authority_ref": f"source_safety:protected_terms:{','.join(blocked[:5])}",
        "evidence_spans": [{"text": term} for term in blocked[:5]],
    }


def _verify_required_text(scene: SceneCard | None, content: str, issue: dict[str, Any]) -> dict[str, Any] | None:
    must_include = getattr(scene, "must_include_text", None) if scene is not None else None
    if not isinstance(must_include, str) or not must_include.strip():
        return None
    if source_field_satisfied(must_include, content or ""):
        return None
    return {
        "verified_by": "scene_card_required_text",
        "authority_ref": f"scene_card:{getattr(scene, 'scene_id', '')}.must_include_text",
        "evidence_spans": [{"text": must_include.strip()[:120]}],
    }


def _verify_forbidden_term(scene: SceneCard | None, content: str, issue: dict[str, Any]) -> dict[str, Any] | None:
    forbidden = getattr(scene, "forbidden_text", None) if scene is not None else None
    if not contains_forbidden_term(forbidden, content or ""):
        return None
    matched = [term for term in forbidden_terms(forbidden) if term in (content or "")]
    return {
        "verified_by": "scene_card_forbidden_term",
        "authority_ref": f"scene_card:{getattr(scene, 'scene_id', '')}.forbidden_text",
        "evidence_spans": [{"text": term} for term in matched[:5]],
    }


_INLINE_VERIFIERS = {
    "source_safety_scan": _verify_source_leak,
    "scene_card_required_text": _verify_required_text,
    "scene_card_forbidden_term": _verify_forbidden_term,
}


def _deterministic_producer_evidence(issue: dict[str, Any], policy: IssuePolicy) -> dict[str, Any] | None:
    """source=deterministic 的 issue 由确定性检测器本身产出——检测器即复核器。"""
    if str(issue.get("source") or "") != "deterministic":
        return None
    if policy.verifier_id is None:
        return None
    authority_ref = None
    details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
    if issue.get("issue_key") == "event_log_consistency_violation":
        entity = details.get("entity_id") or ""
        fact = details.get("fact_key") or ""
        authority_ref = f"event:{entity}.{fact}"
    return {
        "verified_by": policy.verifier_id,
        "authority_ref": authority_ref,
        "evidence_spans": issue.get("evidence_spans") or [],
    }


def _constraint_conflict_evidence(issue: dict[str, Any]) -> dict[str, Any] | None:
    """QC 建议改动的词与场景卡硬约束确定性冲突（既有 _annotate_qc_issues 注解）。"""
    conflicts = issue.get("conflicts_with")
    if not isinstance(conflicts, list) or not conflicts:
        return None
    first = conflicts[0] if isinstance(conflicts[0], dict) else {}
    return {
        "verified_by": "scene_card_constraint_conflict",
        "authority_ref": str(first.get("constraint_source") or "scene_card"),
        "evidence_spans": issue.get("evidence_spans") or [],
    }


def classify_issue(
    issue: dict[str, Any],
    *,
    scene: SceneCard | None = None,
    content: str = "",
) -> dict[str, Any]:
    """把单条 issue 分级为 §6.1 结构；幂等（已分级的 issue 重分级结果一致）。"""
    issue_key = str(issue.get("issue_key") or "").strip() or "unknown"
    policy = _policy_for(issue_key)
    source = str(issue.get("source") or "") or "llm_advisory"

    evidence: dict[str, Any] | None = None
    if policy.verified_level in BLOCKING_LEVELS:
        evidence = _deterministic_producer_evidence(issue, policy)
        if evidence is None and policy.verifier_id in _INLINE_VERIFIERS:
            evidence = _INLINE_VERIFIERS[policy.verifier_id](scene, content, issue)
    if evidence is None:
        # 独立于注册表的确定性升级通道：约束冲突注解（证据在 issue 自身）
        evidence = _constraint_conflict_evidence(issue)
        if evidence is not None and policy.verified_level not in BLOCKING_LEVELS:
            level = Q1
        elif evidence is not None:
            level = policy.verified_level
        else:
            level = policy.fallback_level
    else:
        level = policy.verified_level

    classified: dict[str, Any] = {
        **issue,
        "issue_key": issue_key,
        "quality_level": level,
        "blocking": level in BLOCKING_LEVELS,
        "authority_ref": (evidence or {}).get("authority_ref") or issue.get("authority_ref"),
        "evidence_spans": (evidence or {}).get("evidence_spans") or issue.get("evidence_spans") or [],
        "confidence": issue.get("confidence") if isinstance(issue.get("confidence"), (int, float)) else (1.0 if evidence else 0.5),
        "recommended_action": issue.get("recommended_action") or _LEVEL_RECOMMENDED_ACTION[level],
        "source": source,
        "verified_by": (evidence or {}).get("verified_by"),
    }
    if evidence is None and policy.verified_level in BLOCKING_LEVELS:
        classified["downgraded_from"] = policy.verified_level
        classified["downgrade_reason"] = "no_deterministic_verification"
    return classified


def classify_issues(
    issues: list[Any],
    *,
    scene: SceneCard | None = None,
    content: str = "",
) -> list[dict[str, Any]]:
    return [
        classify_issue(issue, scene=scene, content=content)
        for issue in issues
        if isinstance(issue, dict)
    ]


def has_blocking(classified: list[dict[str, Any]]) -> bool:
    return any(issue.get("blocking") for issue in classified if isinstance(issue, dict))


def blocking_issues(classified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in classified if isinstance(issue, dict) and issue.get("blocking")]

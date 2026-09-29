"""质检意见的确定性加工：约束冲突注解与证据、场景卡核对的取词、确定性连续性 issue、质检模型代词意见的过滤、
修改简报合并。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import object_session

from novel_system.db.models import SceneCard
from novel_system.services.qc_constraints import named_scene_card_sources
from novel_system.services.quality_checks.continuity import (
    CONTINUITY_UNAVAILABLE_KEY,
    EVENT_LOG_VIOLATION_KEY,
    deterministic_continuity_issues,
)


# 质检模型报的代词意见（指代不清 / 代词前后不一）一律不收。以前要有确定性的代词漂移检测作佐证才留下，那个
# 检测器只认声线卡里写的代词，随声线卡一起删了（批准#15，重评 R8）——现在就是整批丢掉，质检结论与以前相同。
# 要不要把它们作为 Q3 提示交给作者，是批次 3 的决定。
LLM_PRONOUN_ISSUE_KEYS = frozenset(
    {
        "character_pronoun_ambiguity",
        "character_pronoun_continuity",
    }
)


def _issue_blob(issues: list[Any], rewrite_brief: list[Any]) -> str:
    parts: list[str] = []
    for issue in issues:
        if isinstance(issue, dict):
            parts.append(str(issue.get("issue_key") or ""))
            parts.append(str(issue.get("message") or ""))
    parts.extend(str(item) for item in rewrite_brief)
    return "\n".join(parts)


def _named_scene_card_source_texts(scene: SceneCard) -> list[tuple[str, str]]:
    # 字段顺序即冲突归因优先级（与 preflight 侧不同：QC 侧含 location 且 hook 优先）。
    return named_scene_card_sources(
        scene, ("hook", "must_include_text", "exit_change", "scene_goal", "location")
    )


def _scene_card_source_texts(scene: SceneCard) -> list[str]:
    """场景卡上的非空文本源（字段 + 节拍）；只拿来判「有没有一处满足」，次序无关。"""
    return [text for _name, text in _named_scene_card_source_texts(scene)]


def _terms_from_qc_text(text: str) -> list[str]:
    terms: list[str] = []
    terms.extend(
        match.strip()
        for match in re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,40})[\"'“”‘’]", text)
    )
    terms.extend(match.strip() for match in re.findall(r"[\u4e00-\u9fff]{2,12}", text))
    terms.extend(
        match.strip() for match in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,40}", text)
    )
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        normalized = term.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        unique.append(normalized)
    return unique


QC_TERM_CHANGE_MARKERS = (
    "replace",
    "remove",
    "delete",
    "avoid",
    "forbid",
    "forbidden",
    "rename",
    "change",
    "cut",
    "neutral clue",
    "neutralize",
    "substitute",
    "替换",
    "删除",
    "去掉",
    "拿掉",
    "避免",
    "不要",
    "不得",
    "禁用",
    "改成",
    "改掉",
    "改写",
    "换成",
)


def _qc_text_requests_term_change(text: str, term: str) -> bool:
    if not text or not term:
        return False
    lowered = text.lower()
    normalized_term = term.lower()
    start = 0
    while True:
        index = lowered.find(normalized_term, start)
        if index < 0:
            return False
        window = lowered[max(0, index - 48) : index + len(normalized_term) + 48]
        if any(marker in window for marker in QC_TERM_CHANGE_MARKERS):
            return True
        start = index + len(normalized_term)


def _constraint_conflicts_for_text(scene: SceneCard, text: str) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for term in _terms_from_qc_text(text):
        if not _qc_text_requests_term_change(text, term):
            continue
        for source_name, source_text in _named_scene_card_source_texts(scene):
            if term in source_text:
                conflicts.append(
                    {
                        "term": term,
                        "constraint_source": source_name,
                        "conflicts_with": "hard_qc",
                        "human_readable_reason": "QC requests changing a term that the scene card requires.",
                    }
                )
                break
    return conflicts


def _evidence_spans_for_text(content: str, text: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for term in _terms_from_qc_text(text):
        start = content.find(term)
        if start < 0:
            continue
        spans.append({"text": term, "start": start, "end": start + len(term)})
        if len(spans) >= 5:
            break
    return spans


def _annotate_qc_issues(
    scene: SceneCard, source_content: str, payload: dict[str, Any]
) -> dict[str, Any]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload
    annotated: list[Any] = []
    for issue in issues:
        if not isinstance(issue, dict):
            annotated.append(issue)
            continue
        blob = " ".join(str(issue.get(key) or "") for key in ("issue_key", "message"))
        conflicts = _constraint_conflicts_for_text(scene, blob)
        evidence_spans = _evidence_spans_for_text(source_content, blob)
        severity = (
            "high"
            if conflicts
            else ("medium" if not payload.get("pass_flag") else "low")
        )
        annotated.append(
            {
                **issue,
                "evidence_spans": issue.get("evidence_spans") or evidence_spans,
                "constraint_source": (
                    conflicts[0]["constraint_source"]
                    if conflicts
                    else issue.get("constraint_source", "source_draft")
                ),
                "conflicts_with": issue.get("conflicts_with") or conflicts,
                "severity": issue.get("severity") or severity,
                "human_readable_reason": issue.get("human_readable_reason")
                or issue.get("message")
                or issue.get("issue_key")
                or "QC issue",
            }
        )
    return {**payload, "issues": annotated}


def _promote_constraint_conflicts_to_human_review(
    payload: dict[str, Any]
) -> dict[str, Any]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload
    has_conflict = any(
        isinstance(issue, dict) and bool(issue.get("conflicts_with"))
        for issue in issues
    )
    if not has_conflict or payload.get("next_action") == "human_review_required":
        return payload
    rewrite_brief = (
        payload.get("rewrite_brief")
        if isinstance(payload.get("rewrite_brief"), list)
        else []
    )
    return {
        **payload,
        "resolution_code": "hard_block_human",
        "pass_flag": False,
        "next_action": "human_review_required",
        "rewrite_brief": [
            *rewrite_brief,
            "Constraint conflict detected: choose whether to keep the scene-card term or revise the conflicting QC instruction.",
        ],
    }


def _reported_duplicate_appears_once(issue_blob: str, content: str) -> bool:
    quoted = re.findall(r"['‘“\"]([^'’”\"]{3,})['’”\"]", issue_blob)
    return bool(quoted) and all(content.count(fragment) <= 1 for fragment in quoted)


def _deterministic_quality_issues(scene: SceneCard, content: str) -> list[dict[str, Any]]:
    """确定性连续性检查（与成稿门同一份：``quality_checks.continuity``），质检这一侧加 severity。

    Wave 2（§5.4 提案—复核）：检测器的产出带 ``source="deterministic"``——检测器即复核器，分类器据此允许
    Q0/Q1 升级；LLM 提案没有这个标，只能走内联复核或降 Q2。事件账本读不出时给一条不阻断的诊断 issue（只记
    异常类型名）。读库用场景卡所在的会话（以前另开一个会话，读的是别的连接已提交的快照）。
    """
    session = object_session(scene)
    if session is not None:
        check = deterministic_continuity_issues(session, scene, content)
    else:  # 没挂在会话上的场景卡：借一个会话只读
        from novel_system.db.session import SessionLocal

        with SessionLocal() as own:
            check = deterministic_continuity_issues(own, scene, content)
    issues = [
        {**issue, "severity": "high"} if issue.get("issue_key") == EVENT_LOG_VIOLATION_KEY else issue
        for issue in check.issues
    ]
    if check.unavailable_error is not None:
        issues.append(
            {
                "issue_key": CONTINUITY_UNAVAILABLE_KEY,
                "severity": "medium",
                "message": "Narrative continuity validation was unavailable; review this scene manually.",
                "source": "system_diagnostic",
                "details": {"error_type": check.unavailable_error, "retryable": True},
            }
        )
    return issues


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        issue_key = str(issue.get("issue_key") or "")
        message = str(issue.get("message") or "")
        key = (issue_key, message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _drop_llm_pronoun_issues(
    *,
    payload: dict[str, Any],
    qc_type: str,
) -> dict[str, Any]:
    """丢掉质检模型的代词意见（``LLM_PRONOUN_ISSUE_KEYS``）；只剩它们撑着的非 pass 结论改判 pass。"""
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload

    kept_issues: list[Any] = []
    removed = False
    for issue in issues:
        if (
            isinstance(issue, dict)
            and str(issue.get("issue_key") or "").strip()
            in LLM_PRONOUN_ISSUE_KEYS
        ):
            removed = True
            continue
        kept_issues.append(issue)
    if not removed:
        return payload

    cleaned = {**payload, "issues": kept_issues}
    if kept_issues or payload.get("next_action") == "pass":
        return cleaned

    if qc_type == "hard_qc":
        return {
            **cleaned,
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "rewrite_brief": [],
        }
    if qc_type == "soft_qc":
        return {
            **cleaned,
            "resolution_code": "soft_pass",
            "pass_flag": True,
            "next_action": "pass",
            "rewrite_brief": [],
            "carry_forward_note": False,
            "note_scope": None,
            "carry_note_text": None,
        }
    return cleaned


def _rewrite_briefs_for_deterministic_issues(issues: list[dict[str, Any]]) -> list[str]:
    briefs: list[str] = []
    for issue in issues:
        issue_key = issue.get("issue_key")
        if issue_key == "mechanical_required_beat_listing":
            briefs.append(
                "将必须出现的剧情节拍自然织入动作和因果，不要在段尾追加清单。"
            )
    return briefs


def _append_unique_rewrite_briefs(
    existing: list[Any], additions: list[str]
) -> list[Any]:
    merged = list(existing)
    seen = {
        str(item).strip() for item in merged if isinstance(item, str) and item.strip()
    }
    for addition in additions:
        if addition.strip() and addition.strip() not in seen:
            merged.append(addition.strip())
            seen.add(addition.strip())
    return merged


def _qc_apply_deterministic_quality_gates(
    scene: SceneCard,
    draft_content: str,
    payload: dict[str, Any],
    *,
    qc_type: str,
) -> dict[str, Any]:
    deterministic_issues = _deterministic_quality_issues(scene, draft_content)
    payload = _drop_llm_pronoun_issues(payload=payload, qc_type=qc_type)
    if not deterministic_issues:
        return payload
    # Wave 2：gate 只做合并——是否改判分支/触发补丁由分级器统一裁决（硬 QC 侧
    # 只有 verified Q0/Q1 才升级为 partial_rewrite；theme/tension 等 Q2/Q3 不再
    # 强制重写；软 QC 侧同样交由分级器,gate 不做任何升级判断）。
    existing_issues = (
        payload.get("issues") if isinstance(payload.get("issues"), list) else []
    )
    rewrite_brief = (
        payload.get("rewrite_brief")
        if isinstance(payload.get("rewrite_brief"), list)
        else []
    )
    merged_issues = _dedupe_issues([*existing_issues, *deterministic_issues])
    return {
        **payload,
        "issues": merged_issues,
        "rewrite_brief": _append_unique_rewrite_briefs(
            rewrite_brief,
            _rewrite_briefs_for_deterministic_issues(deterministic_issues),
        ),
    }

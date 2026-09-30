"""关键场景的匿名候选终选（Wave 3，治理 §5.5 / §6.3）：开门、盲化视图、一次写入的终选。

- :func:`offer_candidates_for_selection`：编排器在风格稿候选排好之后调它开门（``Orchestrator._offer_candidates_for_selection``
  仍是它的名字，测试在实例上调）；
- :func:`candidates_view`：``GET /api/v1/scenes/{scene_id}/style-candidates``；
- :func:`select_candidate`：``POST …/style-candidates/{row_id}/select``（幂等层在路由）。

门是 ``human_review_events`` 里 ``event_source="candidate_selection"``、``details.gate_type="style_candidate_selection"``
的一行；终选一次写入、不能改选（想换一稿就重新起草这一场，重评 R2）。从路由文件与编排器的 mixin 搬到这里（B12-01）。
"""

from __future__ import annotations

import logging
import random
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import HumanReviewEvent, SceneDraft, SceneRunState, utcnow
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.errors import DomainError
from novel_system.services.style_policy import style_policy_for_bundle

_LOGGER = logging.getLogger(__name__)

CANDIDATE_SELECTION_SOURCE = "candidate_selection"
STYLE_CANDIDATE_SELECTION_GATE = "style_candidate_selection"
# 终选时作者可勾的偏好标签（与 ``StyleCandidateSelectRequest.preference_tags`` 的取值同一集合）
PREFERENCE_TAGS: tuple[str, ...] = (
    "style_match",
    "rhythm",
    "voice",
    "imagery",
    "dialogue",
    "overall_quality",
    "plot_fidelity",
)


def normalize_preference_tags(values: Any) -> list[str]:
    """终选时作者勾选的偏好标签:去重、只留白名单(与 StyleCandidateSelectRequest 同集合)。"""
    tags = [str(value or "").strip() for value in (values or [])]
    return list(dict.fromkeys(tag for tag in tags if tag in PREFERENCE_TAGS))


def latest_selection_gate(session: Session, scene_id: str) -> HumanReviewEvent | None:
    """这一场最近的一道风格稿候选终选门（没有 → ``None``）。"""
    events = (
        session.execute(
            select(HumanReviewEvent)
            .where(
                HumanReviewEvent.scene_id == scene_id,
                HumanReviewEvent.event_source == CANDIDATE_SELECTION_SOURCE,
            )
            .order_by(
                HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc()
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        if (event.details_json or {}).get("gate_type") == STYLE_CANDIDATE_SELECTION_GATE:
            return event
    return None


def candidates_view(
    session: Session,
    scene_id: str,
    *,
    include_scores: bool = False,
    diagnostic: bool = False,
) -> dict[str, Any]:
    """Wave 3（治理 §5.5/§6.3）：候选终选取数——默认盲化视图。

    存在终选 gate 时：按 gate 的 blinded_order 输出**完整正文**，默认剥离
    机器分数与预选标记（按分排序展示本身就是泄漏）；`include_scores=true`
    为作者主动展开——附分数但不改顺序。无 gate（标准场/历史诊断）保留旧的
    按分降序形状（`diagnostic` 用途），并标 `blinded:false`。
    """
    AuthorLifecycleService(session).require_active_scene(scene_id)
    from novel_system.services.literary_quality import adversarial_rank_score

    state = session.get(SceneRunState, scene_id)
    gate = latest_selection_gate(session, scene_id)
    criticality_info = None
    if state and state.criticality_level:
        criticality_info = {
            "level": state.criticality_level,
            "reasons": state.criticality_reasons_json or [],
        }

    if gate is not None and not diagnostic:
        details = gate.details_json or {}
        blinded_order = [
            str(r)
            for r in (
                details.get("blinded_order") or details.get("candidate_row_ids") or []
            )
        ]
        candidates = []
        for row_id in blinded_order:
            draft = session.get(SceneDraft, row_id)
            if draft is None:
                continue
            entry: dict[str, Any] = {
                "row_id": draft.row_id,
                "content": draft.content,
                "created_at": str(draft.created_at) if draft.created_at else None,
            }
            if include_scores:
                # 主动展开：分数只做标注，不重排（§5.5）
                entry["adversarial_score"] = round(
                    adversarial_rank_score(draft.content) if draft.content else 0.0, 3
                )
            candidates.append(entry)
        return {
            "scene_id": scene_id,
            "blinded": True,
            "candidates": candidates,
            "total": len(candidates),
            "selection": {
                "decision_status": details.get("decision_status"),
                "selected_row_id": (
                    details.get("selected_row_id")
                    if details.get("decision_status") == "selected"
                    else None
                ),
                "event_id": gate.event_id,
            },
            "criticality": criticality_info,
        }

    # 无终选 gate：旧诊断形状（按分降序、带分数）——仅限非盲化诊断用途
    drafts = list(
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "style_draft",
            )
            .order_by(SceneDraft.created_at.desc())
        )
        .scalars()
        .all()
    )
    selected_row_id = state.current_style_draft_row_id if state else None
    candidates = []
    for d in drafts:
        score = adversarial_rank_score(d.content) if d.content else 0.0
        candidates.append(
            {
                "row_id": d.row_id,
                "adversarial_score": round(score, 3),
                "content_preview": (d.content or "")[:500],
                "content": d.content,
                "selected": d.row_id == selected_row_id,
                "created_at": str(d.created_at) if d.created_at else None,
            }
        )
    candidates.sort(key=lambda c: c["adversarial_score"], reverse=True)
    return {
        "scene_id": scene_id,
        "blinded": False,
        "candidates": candidates,
        "total": len(candidates),
        "criticality": criticality_info,
    }


def select_candidate(
    session: Session,
    scene_id: str,
    row_id: str,
    *,
    actor_ref: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Wave 3（治理 §5.5/§6.3）：作者终选——一次写入 + 锁定。

    相同选择重复提交幂等返回；已存在不同终选记录时新的 select 返回
    409 SELECTION_LOCKED：终选提交后不能改选，想换一稿就重新起草这一场（重评 R2）。记录
    选择耗时/无明显差异标记（§5.5 记录选择、放弃、无明显差异和选择耗时）。``body`` 是
    ``StyleCandidateSelectRequest`` 的字段（no_clear_difference / duration_ms / preference_tags）。
    """
    AuthorLifecycleService(session).require_active_scene(scene_id)
    draft = session.get(SceneDraft, row_id)
    if draft is None or draft.scene_id != scene_id:
        raise DomainError(
            "CANDIDATE_NOT_FOUND",
            f"Style draft candidate {row_id} not found for scene {scene_id}",
            status_code=404,
        )
    state = session.get(SceneRunState, scene_id)
    if state is None:
        raise DomainError(
            "SCENE_STATE_NOT_FOUND", "Scene run state not found", status_code=404
        )

    gate = latest_selection_gate(session, scene_id)
    preference_tags = normalize_preference_tags(body.get("preference_tags"))
    if gate is not None:
        details = dict(gate.details_json or {})
        decision_status = details.get("decision_status")
        if decision_status == "selected":
            if details.get("selected_row_id") == row_id:
                # 相同选择重复提交：幂等返回（§7.4）
                return {
                    "scene_id": scene_id,
                    "selected_row_id": row_id,
                    "decision_status": "selected",
                    "message": "Candidate already selected",
                }
            raise DomainError(
                "SELECTION_LOCKED",
                "terminal selection is locked — re-draft the scene to choose again",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "selected_row_id": details.get("selected_row_id"),
                },
            )
        candidate_row_ids = [
            str(r) for r in (details.get("candidate_row_ids") or [])
        ]
        if candidate_row_ids and row_id not in candidate_row_ids:
            raise DomainError(
                "CANDIDATE_NOT_IN_GATE",
                "candidate is not part of the terminal-selection gate",
                status_code=409,
                details={"scene_id": scene_id, "row_id": row_id},
            )
        # 2026-09-14 减法:终选不再生成「风格反馈」记录(作者选择 vs 机器领先者的一致性,
        # policy_evidence_eligible 恒 False,从未被任何策略消费);只留决定历史。
        details.pop("style_feedback", None)
        details.pop("style_feedback_error_code", None)
        details.pop("style_feedback_history", None)
        details.pop("style_feedback_snapshot", None)
        decided_at = utcnow()
        history = list(details.get("decision_history") or [])
        history.append(
            {
                "action": "select",
                "row_id": row_id,
                "actor_ref": actor_ref,
                "at": decided_at,
                "no_clear_difference": bool(body.get("no_clear_difference")),
                "preference_tags": preference_tags,
                **(
                    {"duration_ms": int(body["duration_ms"])}
                    if isinstance(body.get("duration_ms"), (int, float))
                    else {}
                ),
            }
        )
        gate.details_json = {
            **details,
            "decision_status": "selected",
            "selected_row_id": row_id,
            "decided_at": decided_at,
            "no_clear_difference": bool(body.get("no_clear_difference")),
            "preference_tags": preference_tags,
            "decision_history": history,
        }
        gate.status = "resolved"
    else:
        # 无 gate 的旧路径（标准场直接 select）：首次 select 补建已决 gate，
        # 使终选锁定语义对所有场景生效（§6.3 补充契约）。
        gate = HumanReviewEvent(
            event_id=f"hre_sel_{uuid4().hex[:12]}",
            scene_id=scene_id,
            chapter_id=draft.chapter_id,
            object_ref=f"candidate_selection:{scene_id}",
            event_source=CANDIDATE_SELECTION_SOURCE,
            priority="high",
            status="resolved",
            # 终选一次写入，不再有「重开改选」（重评 R2 复核补充 4）
            allowed_actions_json=["select"],
            details_json={
                "gate_type": STYLE_CANDIDATE_SELECTION_GATE,
                "candidate_row_ids": [row_id],
                "blinded_order": [row_id],
                "decision_status": "selected",
                "selected_row_id": row_id,
                "decided_at": utcnow(),
                "no_clear_difference": bool(body.get("no_clear_difference")),
                "preference_tags": preference_tags,
                "decision_history": [
                    {
                        "action": "select",
                        "row_id": row_id,
                        "actor_ref": actor_ref,
                        "at": utcnow(),
                        "no_clear_difference": bool(
                            body.get("no_clear_difference")
                        ),
                        "preference_tags": preference_tags,
                    }
                ],
                "tokens_used": int(getattr(state, "scene_tokens_used", 0) or 0),
            },
            default_action="select",
        )
        session.add(gate)

    state.current_style_draft_row_id = row_id
    # 治理 §4.3：候选选择也是「最近有效正文」的维护点
    state.latest_valid_draft_row_id = row_id
    session.flush()
    return {
        "scene_id": scene_id,
        "selected_row_id": row_id,
        "decision_status": "selected",
        "message": "Candidate selected for human terminal review",
    }


def offer_candidates_for_selection(
    session: Session, scene: Any, state: Any, bundle: Any, candidates: list[Any]
) -> list[str] | None:
    """Wave 3（§4.4/§5.5）：确定性坏稿淘汰后建立匿名候选终选 gate。

    机器只淘汰空文本与抄袭门（``reference_copy_gate``，候选排序时已查、结论在 ``ranking_audit``）确认抄了
    参考书原文的候选（不按机器分数删，§4.4；受保护专名只提醒、从不淘汰，[批准#12]）；全部无效时返回 None——
    管线继续，由 QC 层裁决，不装作可选。候选按正文去重后不到两份时调用方根本不开这道门
    （编排器的 ``_distinct_candidate_count``）。
    blinded_order 是随机置换（§5.5 展示顺序必须随机化并记录）。

    风格参考 v3（S2 a）：有绑定时，抄袭门「没检查成」的候选（``plagiarism_checked`` 不为 True——读数 / 抄袭门
    抛过异常）也不交给作者盲选（fail-closed；成稿门仍是最后一道）；未绑定的场景没有抄袭门，照旧交付。
    """
    style_bound = bool(getattr(style_policy_for_bundle(bundle), "bound", False))
    valid_candidates: list[Any] = []
    offered_texts: set[str] = set()
    for cand in candidates:
        content = (getattr(cand, "content", "") or "").strip()
        if not content:
            continue
        if content in offered_texts:
            # 风格参考 v3（P5b）：作者手笔直起时没过门的修改槽位保留首稿原文——同样的正文只给作者看一次
            continue
        ranking = getattr(cand, "ranking_audit", None) or {}
        if (
            ranking.get("plagiarism_checked") is True
            and ranking.get("plagiarism_passed") is False
        ):
            continue
        if style_bound and ranking.get("plagiarism_checked") is not True:
            _LOGGER.warning(
                "candidate %s of scene %s was never copy-checked; not offered for blind selection",
                getattr(cand, "row_id", None),
                scene.scene_id,
            )
            continue
        offered_texts.add(content)
        valid_candidates.append(cand)
    valid_row_ids = [str(candidate.row_id) for candidate in valid_candidates]
    if not valid_row_ids:
        _LOGGER.warning(
            "no deterministically valid candidate to offer for scene %s; pipeline continues",
            scene.scene_id,
        )
        return None
    blinded_order = list(valid_row_ids)
    random.shuffle(blinded_order)
    event = HumanReviewEvent(
        event_id=f"hre_sel_{uuid4().hex[:12]}",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        object_ref=f"candidate_selection:{scene.scene_id}",
        event_source=CANDIDATE_SELECTION_SOURCE,
        priority="high",
        status="awaiting_review",
        # 终选一次写入，不再有「重开改选」（重评 R2 复核补充 4）
        allowed_actions_json=["select"],
        details_json={
            "gate_type": STYLE_CANDIDATE_SELECTION_GATE,
            "candidate_row_ids": valid_row_ids,
            "blinded_order": blinded_order,
            "decision_status": "awaiting",
            "selected_row_id": None,
            "tokens_used": int(state.scene_tokens_used or 0),
            "decision_history": [],
        },
        default_action="select",
    )
    session.add(event)
    state.scene_status = "awaiting_candidate_selection"
    state.current_human_review_event_id = event.event_id
    session.flush()
    return valid_row_ids


__all__ = [
    "CANDIDATE_SELECTION_SOURCE",
    "PREFERENCE_TAGS",
    "STYLE_CANDIDATE_SELECTION_GATE",
    "candidates_view",
    "latest_selection_gate",
    "normalize_preference_tags",
    "offer_candidates_for_selection",
    "select_candidate",
]

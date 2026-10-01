"""AI 起草台一场的工作台载荷（``GET /api/v1/scenes/{scene_id}/workbench``）与场景运行结果的风格提示。

从路由文件搬出（B12-01 / X01-20）。默认载荷只给 React 读的那些键（B12-02 / X01-08）：运行态（含生命周期预算）、
作者可见的状态投影、当前的中性稿 / 风格稿 / 终稿、生成摘要（起草方式、风格提示、本场参考窗口、像不像）、
最近一次硬 / 软质检摘要，外加 bundle 的 id 与快照哈希。诊断用的其余部分——章目标、场景卡、章状态、运行预检、
bundle 冻结快照（一场跑完约 140 KB）、终稿的抄袭门读数、文学蓝图、执行合同、场景记忆、准终稿评审摘要、
改写计数、人工审阅、尝试历史——只在 ``include="diagnostics"`` 时给：它们以前每次 GET 都现算（抄袭门要扫
一遍参考书指纹，预检与蓝图各建一个服务），而界面从来不读。

``serialize_generation_summary`` / ``serialize_qc_summary`` / ``serialize_near_final_summary`` 是公开名字，
测试直接调它们；``attach_style_notices`` 给同步运行接口（``run/full``）的结果补上本次运行的风格提示。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RevisionCandidate,
    SceneBundle,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StyleReferenceProfile,
    WriterEvaluation,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_state import compute_author_state
from novel_system.services.chapter_state import chapter_state_snapshot
from novel_system.services.near_final import NEAR_FINAL_REWRITE_TYPE, NEAR_FINAL_RUBRIC_ID
from novel_system.services.reference_copy_gate import check_reference_copy_for_scope
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_budget import lifecycle_budget_payload
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_generation import latest_style_notices
from novel_system.services.scene_run_preflight import SceneRunPreflightService
from novel_system.services.story_slots import planned_chapter_goal, planned_text
from novel_system.services.text_input import clean_backfill_markers

# ``GET …/workbench?include=diagnostics``：连同诊断部分一起给（测试与排障用）
INCLUDE_DIAGNOSTICS = "diagnostics"


class SceneWorkbenchService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def payload(self, scene_id: str, *, diagnostics: bool = False) -> dict[str, Any]:
        """一场的工作台载荷；``diagnostics`` 为真时附上诊断部分（见模块说明）。"""

        session = self.session
        scene = AuthorLifecycleService(session).require_active_scene(scene_id)
        state = session.get(SceneRunState, scene_id)
        bundle = (
            session.get(SceneBundle, state.current_bundle_id)
            if state is not None and state.current_bundle_id
            else None
        )
        neutral = (
            session.get(SceneDraft, state.current_neutral_draft_row_id)
            if state is not None and state.current_neutral_draft_row_id
            else None
        )
        style = (
            session.get(SceneDraft, state.current_style_draft_row_id)
            if state is not None and state.current_style_draft_row_id
            else None
        )
        final = (
            session.get(FinalScene, state.current_final_scene_row_id)
            if state is not None and state.current_final_scene_row_id
            else None
        )
        data: dict[str, Any] = {
            "scene_run_state": {
                "scene_status": state.scene_status if state is not None else "ready",
                "current_bundle_id": (
                    state.current_bundle_id if state is not None else None
                ),
                "current_bundle_hash": (
                    state.current_bundle_hash if state is not None else None
                ),
                "current_final_scene_row_id": (
                    state.current_final_scene_row_id if state is not None else None
                ),
                "lifecycle_budget": (
                    lifecycle_budget_payload(state) if state is not None else None
                ),
            },
            # 治理 §5.3：作者可见状态投影块（完整契约字段）
            "author_state": compute_author_state(session, scene_id, state),
            "bundle": {
                "bundle_id": bundle.bundle_id if bundle else None,
                "bundle_snapshot_hash": bundle.bundle_snapshot_hash if bundle else None,
            },
            "neutral_draft": (
                {"row_id": neutral.row_id, "content": neutral.content}
                if neutral
                else None
            ),
            "style_draft": (
                {"row_id": style.row_id, "content": style.content} if style else None
            ),
            "final_scene": (
                {"row_id": final.row_id, "content": final.content} if final else None
            ),
            "generation_summary": (
                serialize_generation_summary(session, scene_id, state)
                if state is not None
                else None
            ),
            "hard_qc_summary": (
                serialize_qc_summary(
                    _latest_qc_report(session, scene_id, state, "hard_qc")
                )
                if state is not None
                else None
            ),
            "soft_qc_summary": (
                serialize_qc_summary(
                    _latest_qc_report(session, scene_id, state, "soft_qc")
                )
                if state is not None
                else None
            ),
        }
        if diagnostics:
            data["bundle"]["snapshot"] = bundle.frozen_snapshot_json if bundle else None
            data.update(self._diagnostics(scene, state, bundle, final))
        return data

    def _diagnostics(self, scene, state: SceneRunState | None, bundle: SceneBundle | None, final: FinalScene | None) -> dict[str, Any]:
        session = self.session
        scene_id = scene.scene_id
        chapter = session.get(ChapterGoal, scene.chapter_id)
        # 风格参考 v3：终稿过唯一抄袭门的读数（与采纳 / 归档同一道门，同一稿命中缓存）；只读展示，检查失败不拖垮工作台
        try:
            source_safety_scan = check_reference_copy_for_scope(
                session,
                final.content if final else "",
                scope=scene,
                bundle_snapshot=bundle.frozen_snapshot_json if bundle else None,
            ).audit()
        except Exception as exc:  # noqa: BLE001 — 展示用读数
            source_safety_scan = {
                "safe": False,
                "error_code": "SOURCE_SAFETY_UNAVAILABLE",
                "error_type": type(exc).__name__,
            }
        memory = (
            session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == scene_id, SceneMemory.active_flag == 1
                )
            )
            .scalars()
            .first()
        )
        attempts = (
            session.execute(
                select(AttemptTracker)
                .where(AttemptTracker.scene_id == scene_id)
                .order_by(AttemptTracker.attempt_id.asc())
            )
            .scalars()
            .all()
        )
        contract_service = SceneExecutionContractService(session)
        return {
            "chapter_goal": {
                "chapter_id": chapter.chapter_id,
                # 作者写了才有值；雪花物化以前写的样板句（S1 9）与给没目标的章补的「推进本章：<章名>」（S2 1）
                # 算「没规划」（story_slots.planned_text / planned_chapter_goal）
                "chapter_goal": planned_chapter_goal(chapter.chapter_goal, chapter),
                "main_plot_push": planned_chapter_goal(chapter.main_plot_push, chapter) or None,
                "emotional_target": planned_text(chapter.emotional_target),
                "ending_effect": planned_text(chapter.ending_effect),
            },
            "scene_card": {
                "scene_id": scene.scene_id,
                "scene_goal": scene.scene_goal,
                "beats_json": scene.beats_json,
                "must_include_text": clean_backfill_markers(scene.must_include_text),
                "location": scene.location,
            },
            "chapter_state": chapter_state_snapshot(session, scene.chapter_id),
            "run_preflight": SceneRunPreflightService(session).build(scene),
            "source_safety_scan": source_safety_scan,
            # 2026-09-22：终稿的规则维度体检不再随工作台载荷每次轮询重算——它在写作台深改面板
            # （GET /api/v1/scenes/{id}/deep-review）里，和其他诊断来源一起、按作者的忽略清单过滤。
            "literary_blueprint": SceneBlueprintService(session).latest_payload(scene_id),
            "execution_contract": contract_service.serialize(contract_service.latest(scene_id)),
            "scene_memory": (
                {"row_id": memory.row_id, "content": memory.content} if memory else None
            ),
            "near_final_summary": serialize_near_final_summary(session, scene_id),
            "rewrite_counters": {
                "hard_partial_rewrite_count": (
                    state.hard_partial_rewrite_count if state is not None else 0
                ),
                "hard_full_rewrite_count": (
                    state.hard_full_rewrite_count if state is not None else 0
                ),
                "soft_patch_count": state.soft_patch_count if state is not None else 0,
                "repeat_issue_key": (
                    state.repeat_issue_key if state is not None else None
                ),
                "repeat_issue_count": (
                    state.repeat_issue_count if state is not None else 0
                ),
            },
            "human_review_summary": (
                _serialize_human_review_summary(
                    _resolve_human_review_event(session, scene_id, state)
                )
                if state is not None
                else None
            ),
            "attempts": [_serialize_attempt(item) for item in attempts],
        }


def attach_style_notices(session: Session, scene_id: str, result: Any) -> Any:
    """2026-09 风格模仿 v2（W5，规格 §2.W5.6）：把风格链路 notices 透传到场景运行响应。

    notices（STYLE_DRAFT_FALLBACK_NEUTRAL / STYLE_INJECTION_MISS / STYLE_INJECTION_DEGRADED /
    STYLE_PLAGIARISM_HIT / STYLE_BANNED_TERM_HIT / STYLE_GATE_UNAVAILABLE）由
    scene_generation 写进本次运行 style_draft / near_final_rewrite 的 AttemptTracker；这里
    只读不写，结果不是 dict 时原样返回。

    只读**本次运行的 bundle**：bundle id 取运行结果的 ``current_bundle_id``，退而取
    ``SceneRunState.current_bundle_id``；两者都没有（运行在建 bundle 之前早退）时原样返回
    ——绝不读不带 bundle 范围的「场景最近一次」，否则一次在 hard_qc 就被挡下的重跑会带上
    上一次运行、另一个 bundle 的 STYLE_PLAGIARISM_HIT。
    """
    if not isinstance(result, dict):
        return result
    bundle_id = result.get("current_bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id:
        state = session.get(SceneRunState, scene_id)
        bundle_id = state.current_bundle_id if state is not None else None
    if not isinstance(bundle_id, str) or not bundle_id:
        return result
    notices = latest_style_notices(session, scene_id, bundle_id=bundle_id)
    if not notices:
        return result
    existing = result.get("notices")
    merged = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    merged.extend(item for item in notices if item not in merged)
    return {**result, "notices": merged}


def serialize_generation_summary(
    session: Session, scene_id: str, state: SceneRunState
) -> dict | None:
    llm_call = _resolve_generation_llm_call(session, scene_id, state)
    if llm_call is None:
        return None
    summary = {
        "llm_call_id": llm_call.llm_call_id,
        "step": _display_generation_step(llm_call.step),
        "raw_step": llm_call.step,
        "provider": llm_call.provider,
        "model": llm_call.model,
        "prompt_hash": llm_call.prompt_hash,
        "prompt_tokens": llm_call.prompt_tokens,
        "completion_tokens": llm_call.completion_tokens,
        "total_tokens": llm_call.total_tokens,
        "latency_ms": llm_call.latency_ms,
        "finish_reason": llm_call.finish_reason,
        "error_code": llm_call.error_code,
        "created_at": llm_call.created_at,
        # v2（W5）：风格链路 notices（回退中性稿 / 注入未命中或降级 / 抄袭或禁用词命中 /
        # gate 未执行）随生成摘要回读，工作台据此提示作者；无 notice 时为空列表。只读与
        # llm_call 同一次运行（同一 bundle）的 notices。
        "notices": _current_run_style_notices(session, scene_id, state),
        # 2026-09-12 风格直起:本次运行的起草方式(style_first / neutral_first),工作台据此
        # 把中性步位标成「首稿（作者手笔）」或「中性稿」。
        "draft_mode": _current_run_draft_mode(session, scene_id, state),
        # 2026-09-14 风格保真修补(WP4.1):本场提示里实际放入的参考书样例窗口(段落序号闭区间,
        # 无原文;原文由 GET /api/v2/style-reference/books/{book_id}/paragraphs 按需取)。同样只读
        # 本次运行的 bundle;没有带窗口的尝试时为 null。
        "style_windows": _current_run_style_windows(session, scene_id, state),
        # 风格参考 v3(P5b):本次运行的「像不像」——首稿读数、风格步的决定(不调模型 / 定向修改采用 / 保留首稿)、
        # 修改稿读数、软补丁的去留、终稿读数、参考评审分;这次运行没有读数时为 null。
        "style_fidelity": _current_run_style_fidelity(session, scene_id, state),
    }
    return summary


def _current_run_style_fidelity(
    session: Session, scene_id: str, state: SceneRunState
) -> dict | None:
    from novel_system.services.style_fidelity_view import current_run_style_fidelity

    try:
        return current_run_style_fidelity(
            session, scene_id, _resolve_current_run_bundle_id(session, scene_id, state)
        )
    except Exception:  # noqa: BLE001 — 只读展示,读数取不到不影响工作台
        return None


def _current_run_draft_mode(
    session: Session, scene_id: str, state: SceneRunState
) -> str:
    from novel_system.services.style_policy import style_policy_for_bundle
    from novel_system.services.style_reference.binding_config import DRAFT_MODE_NEUTRAL_FIRST

    bundle_id = _resolve_current_run_bundle_id(session, scene_id, state)
    if not bundle_id:
        return DRAFT_MODE_NEUTRAL_FIRST
    bundle_row = session.get(SceneBundle, bundle_id)
    if bundle_row is None:
        return DRAFT_MODE_NEUTRAL_FIRST
    try:
        # 风格参考 v3：起草方式只看这次运行 bundle 的 StylePolicy（未绑定 → neutral_first）
        return style_policy_for_bundle(bundle_row.frozen_snapshot_json).draft_mode
    except Exception:  # noqa: BLE001 — 只读展示,不因契约解析失败影响工作台
        return DRAFT_MODE_NEUTRAL_FIRST


def _current_run_style_notices(
    session: Session, scene_id: str, state: SceneRunState
) -> list[dict]:
    """当前运行 bundle 内的风格链路 notices；解析不出 bundle 时为空（不做无范围回读）。"""
    bundle_id = _resolve_current_run_bundle_id(session, scene_id, state)
    if not bundle_id:
        return []
    return latest_style_notices(session, scene_id, bundle_id=bundle_id)


# WP4.1:本场参考窗口从哪几步的尝试回读,按优先级——风格稿(style_draft)是成稿前最后一次带
# 样例的通道;风格直起下中性步位的首稿同样带窗口;近终稿重写稿作兜底。
STYLE_WINDOW_ATTEMPT_STEPS: tuple[str, ...] = (
    "style_draft",
    "neutral_draft",
    "scene_literary_rewrite",
)
_STYLE_WINDOW_INT_KEYS: tuple[str, ...] = ("chapter", "paragraphs", "chars")


def _style_window_ref(item: Any) -> dict | None:
    """把审计里的一条 few_shot_window_refs 规整成 API 形状;起止段缺失或倒置的丢弃。"""
    if not isinstance(item, dict):
        return None
    start = item.get("start")
    end = item.get("end")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or isinstance(end, bool)
        or start < 0
        or end < start
    ):
        return None
    ref: dict[str, Any] = {
        "start": start,
        "end": end,
        "position": str(item.get("position") or ""),
        "paragraph_type": str(item.get("paragraph_type") or ""),
    }
    for key in _STYLE_WINDOW_INT_KEYS:
        value = item.get(key)
        ref[key] = value if isinstance(value, int) and not isinstance(value, bool) else 0
    if ref["paragraphs"] <= 0:
        ref["paragraphs"] = end - start + 1
    # 风格参考 v3：冻结选窗的引用带窗号（持久化窗口索引里的一窗）与按哪条配额选进来的；旧审计没有窗号，形状不变
    window_no = item.get("window_no")
    if isinstance(window_no, int) and not isinstance(window_no, bool):
        ref["window_no"] = window_no
        ref["slot"] = str(item.get("slot") or "")
        ref["situations"] = _style_window_tags(item.get("situations"))
        # 2026-09-24 O1：窗口标签的「手法」改为这一窗最能示范的维度键（前端按 STYLE_DIMENSION_LABELS 显示）
        ref["dimensions"] = _style_window_tags(item.get("dimensions"))
    return ref


def _style_window_tags(value: Any) -> list[str]:
    return [str(tag) for tag in value if str(tag or "").strip()] if isinstance(value, (list, tuple)) else []


def _attach_style_window_tags(session: Session, book_id: str | None, windows: list[dict]) -> None:
    """带窗号的窗补上学习作业给它打的标签与一句话梗概（与本场预览同一份窗口索引；索引换过 / 书不在就不补）。"""
    numbered = [window for window in windows if "window_no" in window]
    if not book_id or not numbered:
        return
    from novel_system.services.style_reference.scene_preview import window_tag_rows

    try:
        rows = window_tag_rows(session, book_id, [int(window["window_no"]) for window in numbered])
    except Exception:  # noqa: BLE001 — 只读展示：标签取不到就只给区间
        return
    for window in numbered:
        tags = (rows.get(int(window["window_no"])) or {}).get("tags") or {}
        window["situations"] = _style_window_tags(tags.get("situations")) or window.get("situations") or []
        window["moods"] = _style_window_tags(tags.get("moods"))
        window["dimensions"] = _style_window_tags(tags.get("dimensions")) or window.get("dimensions") or []
        window["gist"] = str(tags.get("gist") or "")


def _style_window_source(
    session: Session, runtime_audit: dict
) -> tuple[str | None, str | None]:
    """(profile_id, book_id):样例窗口来自最具体层,契约 profile_ids 以层序排列、最具体层在末尾。"""
    raw_ids = runtime_audit.get("profile_ids")
    profile_ids = [str(item) for item in raw_ids if item] if isinstance(raw_ids, list) else []
    for profile_id in reversed(profile_ids):
        profile = session.get(StyleReferenceProfile, profile_id)
        if profile is not None:
            return profile.profile_id, profile.book_id
    return (profile_ids[-1] if profile_ids else None), None


def _current_run_style_windows(
    session: Session, scene_id: str, state: SceneRunState
) -> dict | None:
    """当前运行 bundle 内实际进入提示的参考书样例窗口(WP4.1);解析不出 bundle 时为 None。

    与 notices 同一范围规则:只看本次运行的 bundle。按 STYLE_WINDOW_ATTEMPT_STEPS 的优先级取
    最近一次 completed 且 ``details_json.style_reference_runtime.few_shot_window_refs`` 非空的
    尝试;返回 ``{step, profile_id, book_id, windows}``,窗口只有段落序号区间与读数,不含原文。
    """
    bundle_id = _resolve_current_run_bundle_id(session, scene_id, state)
    if not bundle_id:
        return None
    for step in STYLE_WINDOW_ATTEMPT_STEPS:
        row = (
            session.execute(
                select(AttemptTracker)
                .where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == step,
                    AttemptTracker.status == "completed",
                    AttemptTracker.source_bundle_id == bundle_id,
                )
                .order_by(AttemptTracker.attempt_id.desc())
            )
            .scalars()
            .first()
        )
        if row is None:
            continue
        runtime_audit = (row.details_json or {}).get("style_reference_runtime")
        if not isinstance(runtime_audit, dict):
            continue
        refs = runtime_audit.get("few_shot_window_refs")
        if not isinstance(refs, list):
            continue
        windows = [ref for ref in (_style_window_ref(item) for item in refs) if ref]
        if not windows:
            continue
        profile_id, book_id = _style_window_source(session, runtime_audit)
        _attach_style_window_tags(session, book_id, windows)
        return {
            "step": step,
            "profile_id": profile_id,
            "book_id": book_id,
            "windows": windows,
        }
    return None


def _resolve_generation_llm_call(
    session: Session, scene_id: str, state: SceneRunState
) -> LlmCall | None:
    if state.current_final_scene_row_id:
        final_scene = session.get(FinalScene, state.current_final_scene_row_id)
        if (
            final_scene is not None
            and final_scene.scene_id == scene_id
            and final_scene.generation_llm_call_id
        ):
            llm_call = session.get(LlmCall, final_scene.generation_llm_call_id)
            if llm_call is not None:
                return llm_call

    for row_id in (
        state.current_style_draft_row_id,
        state.current_neutral_draft_row_id,
    ):
        if not row_id:
            continue
        draft = session.get(SceneDraft, row_id)
        if (
            draft is None
            or draft.scene_id != scene_id
            or not draft.generation_llm_call_id
        ):
            continue
        llm_call = session.get(LlmCall, draft.generation_llm_call_id)
        if llm_call is not None:
            return llm_call
    return None


def _display_generation_step(raw_step: str | None) -> str | None:
    return {
        "scene_literary_rewrite": "literary_rewrite",
        "soft_patch": "style_patch",
        "style_draft": "style_draft",
        "neutral_draft": "neutral_draft",
    }.get(raw_step, raw_step)


def serialize_near_final_summary(session: Session, scene_id: str) -> dict | None:
    latest_attempt = (
        session.execute(
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "near_final_acceptance_review",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        .scalars()
        .first()
    )
    latest_evaluation = (
        session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == "scene",
                WriterEvaluation.object_id == scene_id,
                WriterEvaluation.rubric_id == NEAR_FINAL_RUBRIC_ID,
            )
            .order_by(
                WriterEvaluation.created_at.desc(),
                WriterEvaluation.evaluation_id.desc(),
            )
        )
        .scalars()
        .first()
    )
    if latest_attempt is None and latest_evaluation is None:
        return None
    details = (
        dict(latest_attempt.details_json or {}) if latest_attempt is not None else {}
    )
    revision_candidate = None
    candidate_id = details.get("revision_candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        revision_candidate = session.get(RevisionCandidate, candidate_id)
    if revision_candidate is None:
        revision_candidate = (
            session.execute(
                select(RevisionCandidate)
                .where(
                    RevisionCandidate.object_type == "scene",
                    RevisionCandidate.object_id == scene_id,
                    RevisionCandidate.revision_type == NEAR_FINAL_REWRITE_TYPE,
                )
                .order_by(
                    RevisionCandidate.created_at.desc(),
                    RevisionCandidate.revision_id.desc(),
                )
            )
            .scalars()
            .first()
        )
    near_final_status = latest_attempt.status if latest_attempt is not None else None
    if near_final_status is None and latest_evaluation is not None:
        near_final_status = (
            "human_review_required"
            if latest_evaluation.requires_human_review
            else "revision_required"
        )
    failure_class = details.get("failure_class") or (
        latest_evaluation.failure_class if latest_evaluation is not None else None
    )
    archive_attempt = (
        session.execute(
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "archive",
                AttemptTracker.status == "completed",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        .scalars()
        .first()
    )
    archive_gate = (
        (archive_attempt.details_json or {}).get("final_text_gate")
        if archive_attempt is not None
        else {}
    )
    archived_safe = archive_gate.get("safe_to_archive", archive_gate.get("archivable"))
    author_confirmed_final = bool(archive_gate.get("author_confirmed_final"))
    literary_warnings_unresolved = bool(
        not author_confirmed_final
        and (
            archive_gate.get("literary_warnings_unresolved")
            or near_final_status != "near_final_ready"
            or (latest_evaluation is not None and latest_evaluation.findings_json)
        )
    )
    return {
        "rubric_id": NEAR_FINAL_RUBRIC_ID,
        "near_final_status": near_final_status,
        "pipeline_stage": _near_final_pipeline_stage(near_final_status),
        "failure_class": failure_class,
        "failure_reason": _near_final_failure_label(failure_class),
        # 阶段 D：成稿后的场景三问（坩埚可辨 / 三拍落地 / Yes-No-Maybe），非阻断
        "scene_story_check": details.get("scene_story_check"),
        "auto_rewrite_eligible": (
            bool(latest_evaluation.auto_rewrite_eligible)
            if latest_evaluation is not None
            and latest_evaluation.auto_rewrite_eligible is not None
            else None
        ),
        "contract_field_refs": (
            latest_evaluation.contract_field_refs_json
            if latest_evaluation is not None
            else {}
        ),
        "promotion_blockers": (
            latest_evaluation.promotion_blockers_json
            if latest_evaluation is not None
            else []
        ),
        "evaluation_id": (
            latest_evaluation.evaluation_id
            if latest_evaluation is not None
            else details.get("evaluation_id")
        ),
        "revision_candidate_id": (
            revision_candidate.revision_id if revision_candidate is not None else None
        ),
        "revision_candidate_status": (
            revision_candidate.status if revision_candidate is not None else None
        ),
        "overall_score": (
            latest_evaluation.overall_score if latest_evaluation is not None else None
        ),
        "requires_human_review": (
            bool(latest_evaluation.requires_human_review)
            if latest_evaluation is not None
            else False
        ),
        "safe_to_archive": bool(archived_safe) if archived_safe is not None else None,
        "literary_warnings_unresolved": literary_warnings_unresolved,
        "author_confirmed_final": author_confirmed_final,
        "finality": {
            "safe_to_archive": (
                bool(archived_safe) if archived_safe is not None else None
            ),
            "literary_warnings_unresolved": literary_warnings_unresolved,
            "author_confirmed_final": author_confirmed_final,
        },
        "findings": (
            latest_evaluation.findings_json if latest_evaluation is not None else []
        ),
        "revision_brief": (
            latest_evaluation.revision_brief_json
            if latest_evaluation is not None
            else []
        ),
        "stage_order": [
            "Planning",
            "Drafting",
            "Rewriting",
            "Acceptance Review",
            "Near-final",
        ],
        "created_at": (
            latest_evaluation.created_at
            if latest_evaluation is not None
            else latest_attempt.created_at
        ),
    }


def _near_final_pipeline_stage(status: str | None) -> str:
    return {
        "near_final_ready": "Near-final",
        "revision_required": "Acceptance Review",
        "human_review_required": "Acceptance Review",
    }.get(status or "", "Planning")


def _near_final_failure_label(failure_class: Any) -> str | None:
    if not isinstance(failure_class, str) or not failure_class:
        return None
    return {
        "fact_blocker": "fact",
        "scene_structure_failure": "structure",
        "character_flatness": "character",
        "prose_model_voice": "prose",
        "ending_weakness": "prose",
        "chapter_payoff_gap": "chapter",
        "reference_safety": "safety",
    }.get(failure_class, failure_class)


def _latest_qc_report(
    session: Session, scene_id: str, state: SceneRunState, qc_type: str
) -> QcReport | None:
    if state.current_qc_report_id:
        current_report = session.get(QcReport, state.current_qc_report_id)
        if current_report is not None and current_report.scene_id == scene_id:
            if current_report.qc_type == qc_type:
                return current_report
            if current_report.source_bundle_id:
                return _latest_qc_report_for_bundle(
                    session, scene_id, current_report.source_bundle_id, qc_type
                )

    current_bundle_id = _resolve_current_run_bundle_id(session, scene_id, state)
    if not current_bundle_id:
        return None
    return _latest_qc_report_for_bundle(session, scene_id, current_bundle_id, qc_type)


def _latest_qc_report_for_bundle(
    session: Session, scene_id: str, bundle_id: str, qc_type: str
) -> QcReport | None:
    return (
        session.execute(
            select(QcReport)
            .where(
                QcReport.scene_id == scene_id,
                QcReport.qc_type == qc_type,
                QcReport.source_bundle_id == bundle_id,
            )
            .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        )
        .scalars()
        .first()
    )


def _resolve_current_run_bundle_id(
    session: Session, scene_id: str, state: SceneRunState
) -> str | None:
    if state.current_bundle_id:
        return state.current_bundle_id

    if state.current_final_scene_row_id:
        final_scene = session.get(FinalScene, state.current_final_scene_row_id)
        if (
            final_scene is not None
            and final_scene.scene_id == scene_id
            and final_scene.source_bundle_id
        ):
            return final_scene.source_bundle_id

    for row_id in (
        state.current_style_draft_row_id,
        state.current_neutral_draft_row_id,
    ):
        if not row_id:
            continue
        draft = session.get(SceneDraft, row_id)
        if draft is not None and draft.scene_id == scene_id and draft.source_bundle_id:
            return draft.source_bundle_id

    return None


def serialize_qc_summary(report: QcReport | None) -> dict | None:
    if report is None:
        return None
    summary = {
        "qc_report_id": report.qc_report_id,
        "qc_type": report.qc_type,
        "pass_flag": None if report.pass_flag is None else bool(report.pass_flag),
        "resolution_code": report.resolution_code,
        "issue_keys": _extract_issue_keys(report.issues_json or []),
        "next_action": report.next_action,
        "rewrite_brief": _extract_rewrite_brief(report.rewrite_brief_json or []),
        "created_at": report.created_at,
    }
    if report.issues_json:
        summary["issues"] = report.issues_json
    evidence_spans = _extract_evidence_spans(report.issues_json or [])
    if evidence_spans:
        summary["evidence_spans"] = evidence_spans
    return summary


def _extract_issue_keys(entries: list[dict]) -> list[str]:
    issue_keys: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        issue_key = entry.get("issue_key")
        if isinstance(issue_key, str) and issue_key.strip():
            issue_keys.append(issue_key.strip())
    return issue_keys


def _extract_evidence_spans(entries: list[dict]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_spans = entry.get("evidence_spans")
        if isinstance(entry_spans, list):
            spans.extend(span for span in entry_spans if isinstance(span, dict))
    return spans


def _extract_rewrite_brief(entries: list[dict]) -> list[str]:
    rewrite_brief: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        instruction = entry.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            rewrite_brief.append(instruction.strip())
            continue
        carry_note_text = entry.get("carry_note_text")
        if isinstance(carry_note_text, str) and carry_note_text.strip():
            rewrite_brief.append(carry_note_text.strip())
    return rewrite_brief


def _resolve_human_review_event(
    session: Session, scene_id: str, state: SceneRunState
) -> HumanReviewEvent | None:
    if not state.current_human_review_event_id:
        return None
    event = session.get(HumanReviewEvent, state.current_human_review_event_id)
    if event is not None and event.scene_id == scene_id:
        return event
    return None


def _serialize_human_review_summary(event: HumanReviewEvent | None) -> dict | None:
    if event is None:
        return None
    details = dict(event.details_json or {})
    return {
        "event_id": event.event_id,
        "status": event.status,
        "event_source": event.event_source,
        "priority": event.priority,
        "trigger_reason": details.get("trigger_reason"),
        "failure_reason": details.get("failure_reason"),
        "recommended_action": details.get("recommended_action"),
        "linked_target_ref": details.get("linked_target_ref"),
        "created_at": event.created_at,
    }


def _serialize_attempt(item: AttemptTracker) -> dict:
    return {
        "attempt_id": item.attempt_id,
        "step": item.step,
        "status": item.status,
        "source_bundle_id": item.source_bundle_id,
        "details_json": item.details_json,
        "created_at": item.created_at,
    }


__all__ = [
    "INCLUDE_DIAGNOSTICS",
    "STYLE_WINDOW_ATTEMPT_STEPS",
    "SceneWorkbenchService",
    "attach_style_notices",
    "serialize_generation_summary",
    "serialize_near_final_summary",
    "serialize_qc_summary",
]

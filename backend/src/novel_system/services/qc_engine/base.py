"""质检引擎的骨架：结论类型、报告号、尝试记录（details_json 键名是检查点契约）、引擎基类（构造、提示词装配与
LLM 运行器、开生成阻断的人工复核事件、升级到人工复核的收尾）与两个引擎共用的状态记账。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any, ClassVar

from sqlalchemy.orm import Session

from novel_system.db.models import AttemptTracker, SceneCard, SceneRunState
from novel_system.services.human_review_manager import HumanReviewManager
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.prompt_builder import PromptBuilder


def _build_qc_report_id(
    scene_id: str,
    *,
    timestamp: str | None = None,
    random_hex: str | None = None,
) -> str:
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = random_hex or uuid.uuid4().hex[:12]
    return f"qc_report_{scene_id}_{stamp}_{suffix}"


@dataclass(slots=True)
class QcDecision:
    branch: str
    qc_report_id: str
    human_review_event_id: str | None
    resolution_code: str
    next_action: str
    should_continue: bool
    stop_reason: str | None = None
    llm_call_id: str | None = None
    execution_step_key: str | None = None


# 兼容别名：调用方（orchestrator/checkpoint 测试）按 hard/soft 名 import，字段契约同一。
HardQcDecision = QcDecision
SoftQcDecision = QcDecision


def _qc_primary_issue_key(issues: list[dict[str, Any]]) -> str | None:
    for issue in issues:
        issue_key = issue.get("issue_key")
        if isinstance(issue_key, str) and issue_key:
            return issue_key
    return None


def _qc_apply_issue_tracking(
    state: SceneRunState, issues: list[dict[str, Any]]
) -> None:
    issue_key = _qc_primary_issue_key(issues)
    if issue_key is None:
        state.repeat_issue_key = None
        state.repeat_issue_count = 0
        return
    if state.repeat_issue_key == issue_key:
        state.repeat_issue_count += 1
    else:
        state.repeat_issue_key = issue_key
        state.repeat_issue_count = 1


def _qc_clear_downstream_outputs(state: SceneRunState) -> None:
    state.current_style_draft_row_id = None
    state.current_final_scene_row_id = None


def _qc_record_attempt(
    session: Session,
    *,
    step: str,
    scene_id: str,
    chapter_id: str,
    source_bundle_id: str,
    branch: str,
    qc_report_id: str,
    resolution_code: str,
    next_action: str,
    human_review_event_id: str | None,
    execution_step_key: str,
    llm_call_id: str | None = None,
    error_code: str | None = None,
    retryable: bool | None = None,
    continuity_warning: dict[str, Any] | None = None,
    details_extra: dict[str, Any] | None = None,
) -> None:
    """details_json 键名是 checkpoint 契约：公共键在此固定，引擎差异键
    （soft 侧 source_draft_row_id/rewrite_brief）经 details_extra 注入。"""
    details_json = _with_run_context(
        {
            "qc_report_id": qc_report_id,
            "resolution_code": resolution_code,
            "next_action": next_action,
            "human_review_event_id": human_review_event_id,
            "execution_step_key": execution_step_key,
            **(details_extra or {}),
        },
        llm_call_id=llm_call_id,
        error_code=error_code,
        retryable=retryable,
        continuity_warning=continuity_warning,
    )
    session.add(
        AttemptTracker(
            scene_id=scene_id,
            chapter_id=chapter_id,
            step=step,
            status=branch,
            source_bundle_id=source_bundle_id,
            details_json=details_json,
        )
    )


class QcEngineBase:
    """硬 / 软质检共用的引擎骨架：构造、提示词装配与 LLM 运行器、开生成阻断的人工复核事件、升级到人工复核的收尾。

    两个引擎的分支逻辑各自保留——阻断词汇、熔断、软风险接受、重放上下文都不一样，合成一个引擎不会更简单。
    """

    # 这一道质检在尝试记录里的 step，与重放上下文里被复核那份稿子的行号键（检查点契约的键名）
    QC_STEP: ClassVar[str]
    DRAFT_ROW_KEY: ClassVar[str]

    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
        human_review_manager: HumanReviewManager | None = None,
    ) -> None:
        self.session = session
        self._llm_client = llm_client
        if llm_runner is not None:
            self._llm_runner = llm_runner
        self.human_review_manager = human_review_manager or HumanReviewManager(session)

    # 提示词装配与 LLM 运行器第一次用到时才建：只读路径（工作台摘要、最新一版读取）一次都用不到，
    # 不该为它们读提示词与运行时配置。测试照旧可以直接给实例的这两个属性赋值。
    @cached_property
    def prompt_builder(self) -> PromptBuilder:
        return PromptBuilder()

    @cached_property
    def _llm_runner(self) -> LLMNodeRunner:
        return LLMNodeRunner(self.session, llm_client=self._llm_client)

    def _open_generation_blocker(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        draft_row_id: str,
        failure_reason: str,
        trigger_reason: str,
        replay_context: dict[str, Any],
        allow_soft_risk_acceptance: bool = False,
    ) -> Any:
        """开一条生成阻断的人工复核事件，这一场挂到人工复核上。"""
        event = self.human_review_manager.create_generation_blocker_event(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            object_ref=draft_row_id,
            target_type="scene_draft",
            target_id=draft_row_id,
            target_ref=f"scene_draft:{draft_row_id}",
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            recommended_action="human_review_required",
            replay_context=replay_context,
            allow_soft_risk_acceptance=allow_soft_risk_acceptance,
        )
        state.current_human_review_event_id = event.event_id
        state.scene_status = "human_review_required"
        return event

    def _escalate(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        draft_row_id: str,
        qc_report: Any,
        failure_reason: str,
        trigger_reason: str,
        replay_extra: dict[str, Any],
        attempt_extra: dict[str, Any] | None = None,
        allow_soft_risk_acceptance: bool = False,
        llm_call_id: str | None = None,
        execution_step_key: str,
        error_code: str | None = None,
        retryable: bool | None = None,
        continuity_warning: dict[str, Any] | None = None,
    ) -> QcDecision:
        """已落库的质检报告升级到人工复核：开生成阻断事件、记一次 ``human_review_required`` 的尝试、给出停下的结论。

        重放上下文 = 场景 / 章 / bundle 与哈希、被复核的稿子（``DRAFT_ROW_KEY``）、报告、阻断前的状态，再加引擎自己的
        ``replay_extra``（硬质检的总尝试数；软质检的补丁次数与稿子哈希）和「有才写」的四个键——都在开事件、改状态之前取。
        """
        replay_context = _with_run_context(
            {
                "scene_id": scene.scene_id,
                "chapter_id": scene.chapter_id,
                "source_bundle_id": bundle["bundle_id"],
                "source_bundle_hash": bundle["bundle_snapshot_hash"],
                self.DRAFT_ROW_KEY: draft_row_id,
                "current_qc_report_id": qc_report.qc_report_id,
                "scene_status_before_block": state.scene_status,
                **replay_extra,
            },
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )
        event = self._open_generation_blocker(
            scene=scene,
            state=state,
            draft_row_id=draft_row_id,
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            replay_context=replay_context,
            allow_soft_risk_acceptance=allow_soft_risk_acceptance,
        )
        _qc_record_attempt(
            self.session,
            step=self.QC_STEP,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=event.event_id,
            execution_step_key=execution_step_key,
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
            details_extra=attempt_extra,
        )
        self.session.flush()
        return QcDecision(
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=event.event_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=False,
            stop_reason=trigger_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


def _with_run_context(
    context: dict[str, Any],
    *,
    llm_call_id: str | None,
    error_code: str | None,
    retryable: bool | None,
    continuity_warning: dict[str, Any] | None,
) -> dict[str, Any]:
    """重放上下文 / 尝试记录里「有才写」的四个键（键名是检查点契约）。"""
    if llm_call_id is not None:
        context["llm_call_id"] = llm_call_id
    if error_code is not None:
        context["error_code"] = error_code
    if retryable is not None:
        context["retryable"] = retryable
    if continuity_warning is not None:
        context["continuity_warning"] = continuity_warning
    return context

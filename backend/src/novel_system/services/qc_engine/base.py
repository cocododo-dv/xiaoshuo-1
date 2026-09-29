"""质检引擎的骨架：结论类型、报告号、尝试记录（details_json 键名是检查点契约）、引擎基类（构造、提示词装配与
LLM 运行器、开生成阻断的人工复核事件）与两个引擎共用的状态记账。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

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
    """硬 / 软质检共用的引擎骨架：构造、提示词装配与 LLM 运行器、开生成阻断的人工复核事件。

    两个引擎的分支逻辑各自保留——阻断词汇、熔断、软风险接受、重放上下文都不一样，合成一个引擎不会更简单。
    """

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

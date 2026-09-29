"""一次生成调用的落库账本：稿行、被退回的稿行、尝试记录、风格稿指针、失败记录，以及续跑时从尝试明细里读回的东西。

以前每个流程各写一遍 ``SceneDraft(...)``（10 处）与 ``AttemptTracker(...)``（9 处），「被退回的稿行」的 id 规则写了
4 遍，风格稿指针块写了 8 遍。行 id 格式、stage / step 值与明细键逐字不变——检查点、续跑与工作台据此读回。
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any, Mapping, NoReturn

from sqlalchemy.orm import Session

from novel_system.db.models import AttemptTracker, LlmCall, SceneCard, SceneDraft, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_text
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.llm_task_runner import (
    CONTINUITY_BUDGET_ERROR_CODE,
    CONTINUITY_BUDGET_MESSAGE,
    SCENE_SPLIT_RECOMMENDATION,
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    current_llm_execution_id,
)
from novel_system.services.scene_generation.contracts import SceneGenerationPostprocessError


_PRE_DISPATCH_ACCOUNTING_REJECTIONS = frozenset(
    {
        "LLM_SCENE_TOKEN_BUDGET_UNINITIALIZED",
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_SCENE_CALL_IN_FLIGHT",
        "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
    }
)


def counts_as_business_attempt(exc: Exception) -> bool:
    """A pre-dispatch accounting rejection is evidence, not a generation attempt."""
    original = getattr(exc, "original_error", None)
    code = str(getattr(exc, "error_code", None) or getattr(exc, "code", None) or "")
    original_code = str(
        getattr(original, "error_code", None) or getattr(original, "code", None) or ""
    )
    return not (
        isinstance(exc, LLMAccountingRejected)
        or isinstance(original, LLMAccountingRejected)
        or code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
        or original_code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
    )


class DraftLedger:
    """一场、一份 bundle 的落库账本：稿行（id 规则见 :func:`~.contracts.versioned_scene_artifact_id`）、被退回的稿行、
    尝试记录、风格稿指针与两种失败记录。"""

    def __init__(self, session: Session, scene: SceneCard, state: SceneRunState, bundle: Mapping[str, Any]) -> None:
        self.session = session
        self.scene = scene
        self.state = state
        self.bundle = bundle

    def add_draft(
        self,
        row_id: str,
        *,
        stage: str,
        content: str,
        llm_call_id: str | None,
        status: str | None = None,
    ) -> None:
        """记一行稿子（不 flush——调用方在自己的落库点 flush，次序与原来一致）。"""
        fields: dict[str, Any] = {} if status is None else {"status": status}
        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=self.scene.scene_id,
                chapter_id=self.scene.chapter_id,
                stage=stage,
                content=content,
                source_bundle_id=self.bundle["bundle_id"],
                source_bundle_hash=self.bundle["bundle_snapshot_hash"],
                generation_llm_call_id=llm_call_id,
                **fields,
            )
        )

    def add_rejected(self, base_row_id: str, *, stage: str, content: str, llm_call_id: str | None) -> str:
        """被退回的稿子另存一行审计证据：``{base_row_id}_rejected_{正文 sha256 前 10 位}``，status=rejected。"""
        row_id = f"{base_row_id}_rejected_{sha256_text(content)[:10]}"
        self.add_draft(row_id, stage=stage, content=content, llm_call_id=llm_call_id, status="rejected")
        return row_id

    def add_attempt(self, step: str, details: dict[str, Any], *, status: str = "completed") -> None:
        self.session.add(
            AttemptTracker(
                scene_id=self.scene.scene_id,
                chapter_id=self.scene.chapter_id,
                step=step,
                status=status,
                source_bundle_id=self.bundle["bundle_id"],
                details_json=details,
            )
        )

    def point_style(self, row_id: str) -> None:
        """风格稿指针指向这一行（latest_valid 同步；current_* 与 latest_valid 分轨见治理 §4.3），并 flush。"""
        self.state.current_style_draft_row_id = row_id
        self.state.latest_valid_draft_row_id = row_id
        self.state.current_bundle_id = self.bundle["bundle_id"]
        self.state.current_bundle_hash = self.bundle["bundle_snapshot_hash"]
        self.session.flush()

    def record_runner_failure(
        self,
        *,
        step: str,
        prompt: dict[str, Any],
        exc: LLMNodeExecutionError | SceneGenerationPostprocessError,
        source_draft_row_id: str | None = None,
    ) -> None:
        """provider 这一遍失败（或结算后正文不可用）：记一条失败尝试；派发前就被记账拒绝的不算业务尝试。"""
        state, bundle = self.state, self.bundle
        details_json: dict[str, Any] = {
            "llm_call_id": exc.llm_call_id,
            "error_code": exc.error_code,
            "message": str(getattr(exc, "message", None) or str(exc)),
            "retryable": bool(getattr(exc, "retryable", False)),
            "business_attempt_consumed": counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
            reference_runtime = runtime_audit(prompt)
            if reference_runtime is not None:
                details_json["style_reference_runtime"] = reference_runtime
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        if isinstance(exc, LLMNodeContinuityError):
            details_json["continuity_warning"] = exc.continuity_warning
        self.add_attempt(step, details_json, status="failed")
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()

    def persist_generation_failure(
        self,
        *,
        llm_call_id: str,
        step: str,
        node_id: str,
        execution_step_key: str | None = None,
        started_at: float,
        task_config: Any | None,
        prompt: dict[str, Any] | None,
        request_summary: dict[str, Any],
        exc: Exception,
        source_draft_row_id: str | None = None,
    ) -> None:
        """还没派发就失败（装配提示词出错）：补一条被拒的账本行与一条失败尝试，账目与没失败的调用对得上。"""
        scene, state, bundle = self.scene, self.state, self.bundle
        error_code = getattr(exc, "code", exc.__class__.__name__)
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                scope_type="scene",
                scope_id=scene.scene_id,
                provider=getattr(task_config, "provider", None),
                provider_id=getattr(task_config, "provider_id", None),
                account_id=getattr(task_config, "account_id", None),
                model=getattr(task_config, "model", None),
                # 这一步本该派发到的节点（soft_patch 走 style_patch 路由、作者手笔首稿走 style_draft），不是步名
                node_id=node_id,
                reasoning_level=getattr(task_config, "reasoning_level", None),
                native_reasoning_json=None,
                credential_mode=getattr(task_config, "credential_mode", None),
                prompt_hash=(
                    prompt.get("prompt_hash") if isinstance(prompt, dict) else None
                ),
                step=step,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                execution_id=current_llm_execution_id(),
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="rejected",
                request_payload_summary=sanitize_audit_summary(request_summary),
                # error_audit_summary 已做过 sanitize，勿再包一层（重复 sanitize 幂等但多余）。
                response_payload_summary=error_audit_summary(exc),
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                finish_reason=None,
                error_code=error_code,
            )
        )
        self.session.flush()
        details_json: dict[str, Any] = {
            "llm_call_id": llm_call_id,
            "error_code": error_code,
            "message": str(exc),
            "execution_step_key": execution_step_key,
            "business_attempt_consumed": counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        self.add_attempt(step, details_json, status="failed")
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()


def runtime_audit(
    prompt: Mapping[str, Any] | None,
    *,
    outcome: str | None = None,
    draft_mode: str | None = None,
    notices: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """这一遍注入审计（``prompt["_style_reference_runtime_audit"]``）的拷贝，写进尝试明细；给了 ``outcome`` 就补上
    ``generation_outcome`` / ``draft_mode`` / ``notice_codes``。没有注入审计 → ``None``。"""
    audit = prompt.get("_style_reference_runtime_audit") if isinstance(prompt, Mapping) else None
    if not isinstance(audit, dict):
        return None
    copied = deepcopy(audit)
    if outcome is not None:
        copied["generation_outcome"] = outcome
        copied["draft_mode"] = draft_mode
        copied["notice_codes"] = [item["code"] for item in notices or []]
    return copied


def raise_original_runner_error(exc: LLMNodeExecutionError) -> NoReturn:
    if isinstance(exc, LLMNodeContinuityError):
        raise DomainError(
            CONTINUITY_BUDGET_ERROR_CODE,
            CONTINUITY_BUDGET_MESSAGE,
            status_code=409,
            details={
                "continuity_warning": exc.continuity_warning,
                "recommended_action": SCENE_SPLIT_RECOMMENDATION,
            },
        ) from exc
    if exc.original_error is not None:
        raise exc.original_error
    raise exc


def accepted_draft_step_key(session: Session, llm_call_id: str | None, *, repaired: bool) -> str:
    """采用的首稿 / 中性稿出自哪一步（账本行记的 execution_step_key 为准；替身运行器没有账本行时按是否修复推断）。"""
    call = session.get(LlmCall, llm_call_id) if llm_call_id else None
    step_key = str(getattr(call, "execution_step_key", "") or "") if call is not None else ""
    return step_key or ("neutral_draft_repair" if repaired else "neutral_draft")


def first_draft_lineage(session: Session, first_row_id: str) -> tuple[str | None, str, str | None]:
    """首稿的 (llm_call_id, execution_step_key, execution_id)——「首稿即风格稿」的产品沿用首稿那次调用的谱系。"""
    row = session.get(SceneDraft, first_row_id)
    llm_call_id = str(getattr(row, "generation_llm_call_id", "") or "") or None if row is not None else None
    call = session.get(LlmCall, llm_call_id) if llm_call_id else None
    step_key = str(getattr(call, "execution_step_key", "") or "") or "neutral_draft"
    execution_id = str(getattr(call, "execution_id", "") or "") or None if call is not None else None
    return llm_call_id, step_key, execution_id


def resume_base_safety(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") == row_id and isinstance(
            details.get("base_safety"), dict
        ):
            return dict(details["base_safety"])
    return fallback


def resume_style_repair_source(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback_row_id: str,
    fallback_content: str,
) -> tuple[str, str]:
    """恢复 base checkpoint 时找回被拒绝的 provider 风格稿供一次定向修复。"""
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") != row_id:
            continue
        rejected_row_id = details.get("rejected_candidate_row_id")
        if not isinstance(rejected_row_id, str) or not rejected_row_id:
            break
        rejected = session.get(SceneDraft, rejected_row_id)
        if (
            rejected is not None
            and rejected.stage == "style_rejected"
            and rejected.status == "rejected"
            and rejected.content
        ):
            return rejected.row_id, rejected.content
        break
    return fallback_row_id, fallback_content

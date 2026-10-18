"""准定稿验收评审：一场的整稿评审（``evaluate_scene``，B03-21 从 near_final 拆出）。评审结果的归一化与房风门在
``near_final_payload``。章尾的章级评审（付费、结果没人读）已删（G4 B3-1b.2）：要整章意见用「AI 通读本章」。

评审执行失败（且已落模型台账）时降级成一条「验收执行失败」的评估行，正文照常交付；没落台账就被拒的，带真实
错误码 fail-closed。评审没通过时留一份整场重写的修订候选（``near_final_scene_rewrite``）。
"""

from __future__ import annotations

import uuid
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    RevisionCandidate,
    SceneCard,
    WriterEvaluation,
)
from novel_system.services.author_actions import author_action
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_fail_closed import is_llm_capability_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
)
from novel_system.services.near_final_payload import (
    NEAR_FINAL_REWRITE_TYPE,
    NEAR_FINAL_RUBRIC_ID,
    _apply_scene_near_final_gates,
    _apply_style_bound_rewrite_policy,
    _bundle_style_bound,
    _default_structure_revision_brief,
    _execution_failure_payload,
    _normalize_acceptance_payload,
    _promotion_blockers_from_acceptance,
    should_rewrite,
)
from novel_system.services.plan_check import (
    PLAN_CHECK_KEY,
    apply_plan_check_schema,
    normalize_plan_check,
    plan_check_findings,
    plan_check_prompt,
    plan_items_from_bundle,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import require_scene
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS


def _acceptance_user_prompt(base_prompt: str, *, source_content: str) -> str:
    return "\n".join([base_prompt, "", "## Draft Under Near-Final Review", source_content]).strip()


class NearFinalAcceptanceService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self._llm_client = llm_client
        if llm_runner is not None:
            self._llm_runner = llm_runner

    # 提示词装配与 LLM 运行器第一次用到时才建：只读路径（工作台摘要、最新一版读取）一次都用不到，
    # 不该为它们读提示词与运行时配置。测试照旧可以直接给实例的这两个属性赋值。
    @cached_property
    def prompt_builder(self) -> PromptBuilder:
        return PromptBuilder()

    @cached_property
    def _llm_runner(self) -> LLMNodeRunner:
        return LLMNodeRunner(self.session, llm_client=self._llm_client)

    def _inject_style_reference_prefix(
        self,
        prompt: dict[str, Any],
        scene: SceneCard,
        bundle: dict[str, Any],
        *,
        context_text: str,
        final_user_prompt: str,
    ) -> dict[str, Any]:
        """复用 scene_generation / soft_qc 的模块级注入器;任何异常都回退到基础 prompt。"""
        try:
            from novel_system.services.style_prompt_injection import (
                ROLE_REVIEW,
                inject_style_reference_prefix,
            )

            # 风格参考 v3（L4）：评审节点按评审口径渲染——冻结选窗的前 4 窗样例（窗数由角色决定，
            # inject.request.ROLE_K_CAPS 是唯一定义），标题用评审口径
            injected = inject_style_reference_prefix(
                self.session,
                prompt,
                scene,
                bundle,
                task_type="scene_generation",
                context_text=context_text,
                final_user_prompt=final_user_prompt,
                role=ROLE_REVIEW,
            )
            return injected if injected is not None else prompt
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            # 云策略不许把这本书派生的任何东西送给这个节点:整次评审 409(带 author_action),不降级成没有参考的提示
            raise
        except Exception:  # noqa: BLE001 — 可选增强,不阻断验收评审
            import logging

            logging.getLogger(__name__).warning(
                "near-final review style reference prefix skipped for scene %s",
                getattr(scene, "scene_id", None),
                exc_info=True,
            )
            return prompt

    def evaluate_scene(
        self,
        scene_id: str,
        *,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        source_content: str,
        actor_ref: str = "operator",
        execution_step_key: str = "near_final_acceptance:0",
    ) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id)
        prompt = self.prompt_builder.build(bundle["snapshot"], "near_final_acceptance_review")
        user_prompt = _acceptance_user_prompt(prompt["user_prompt"], source_content=source_content)
        # 2026-10-18 终稿逐条复核：这一场冻结的场景结构里写了的条目附在末尾，schema 按条目生成（plan_check.py）
        items = plan_items_from_bundle(bundle)
        if items:
            user_prompt = f"{user_prompt}\n\n{plan_check_prompt(items)}"
        prompt = {**prompt, "structured_schema": apply_plan_check_schema(prompt.get("structured_schema"), items)}
        # 2026-09-09 样例优先:验收评审拿到与 style_draft 相同的 [STYLE_REFERENCE] 前缀(同一冻结
        # 契约、同一组样例窗口),「author voice match」对照参考原文而不是评审的默认口味;
        # 注入失败只降级,不阻断评审。
        prompt = self._inject_style_reference_prefix(
            prompt, scene, bundle, context_text=source_content, final_user_prompt=user_prompt
        )
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="near_final_acceptance_review",
                step="near_final_acceptance_review",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=source_draft_row_id,
                source_draft_content=source_content,
                execution_step_key=execution_step_key,
            )
            structured = node_result.response.structured_output
            payload = _normalize_acceptance_payload(structured, schema=prompt.get("structured_schema"))
            plan_check = normalize_plan_check(
                structured.get(PLAN_CHECK_KEY) if isinstance(structured, dict) else None, items, source_content
            )
            # 不改变放行：只把逐条结果带上，偏离的几条另作发现进评审记录（诊断抽屉读它们）
            payload["plan_check"] = plan_check
            payload["findings"] = [*payload.get("findings", []), *plan_check_findings(plan_check)]
            llm_call_id = node_result.llm_call_id
        except LLMNodeExecutionError as exc:
            llm_call_id = self._ledgered_failure_llm_call_id(
                exc,
                node_id="near_final_acceptance_review",
                operation="场景准定稿验收",
                target_ref=f"scene_card:{scene.scene_id}",
            )
            payload = _execution_failure_payload(exc.message)

        style_bound = _bundle_style_bound(bundle)
        payload = _apply_scene_near_final_gates(
            payload, source_content, style_bound=style_bound
        )
        if style_bound:
            payload = _apply_style_bound_rewrite_policy(payload)
        source = {
            "content": source_content,
            "source_text_ref": f"source_draft:{source_draft_row_id}",
            "source_bundle_id": bundle["bundle_id"],
        }
        evaluation = self._persist_evaluation(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            source=source,
            payload=payload,
            llm_call_id=llm_call_id,
        )
        candidate = None
        if payload["pass_flag"]:
            self._supersede_open_scene_candidates(scene.scene_id)
        else:
            candidate = self._create_scene_candidate(
                evaluation=evaluation,
                source=source,
                payload=payload,
                actor_ref=actor_ref,
            )
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            status=payload["near_final_status"],
            details={
                "evaluation_id": evaluation.evaluation_id,
                "revision_candidate_id": candidate.revision_id if candidate is not None else None,
                "source_draft_row_id": source_draft_row_id,
                "llm_call_id": llm_call_id,
                "failure_class": payload.get("failure_class"),
                "execution_step_key": execution_step_key,
                # 阶段 D：场景三问随评审记录持久化，工作台 / 目录按最近一次评审透出
                "scene_story_check": payload.get("scene_story_check"),
                # 2026-10-18：终稿逐条复核（写前笔记与工作台读它）；没有场景结构 / 评审执行失败 → None
                "plan_check": payload.get("plan_check"),
            },
        )
        self.session.flush()
        return {
            **payload,
            "evaluation_id": evaluation.evaluation_id,
            "revision_candidate_id": candidate.revision_id if candidate is not None else None,
            "should_rewrite": self._should_rewrite(payload),
        }

    def _ledgered_failure_llm_call_id(
        self,
        exc: LLMNodeExecutionError,
        *,
        node_id: str,
        operation: str,
        target_ref: str,
    ) -> str:
        """只有已落台账的失败才允许降级成"验收执行失败"评估行（正文照常交付）。

        LLMNodeRunner.run 在台账落行之前就被拒（例如记账上下文 / 运行任务归属校验失败、
        请求构造异常）时，异常携带的 llm_call_id 从未写入 llm_calls。若照旧把它持久化成
        evaluator_llm_call_id，归档检查点随后会以 RUN_CHECKPOINT_OUTPUT_MISSING 假失败，
        真实错误码被吞掉。这里 fail-closed：抛出真实错误码，且不留下悬空的评估行。
        """

        if self.session.get(LlmCall, exc.llm_call_id) is not None:
            return exc.llm_call_id
        control_plane_failure = is_llm_capability_error(exc) or isinstance(
            exc.original_error, LLMAccountingRejected
        )
        raise DomainError(
            exc.error_code,
            exc.message,
            status_code=409 if control_plane_failure else 502,
            details={
                "llm_call_id": exc.llm_call_id,
                "node_id": node_id,
                "error_code": exc.error_code,
                "retryable": exc.retryable,
                "next_action": "retry_after_restoring_llm_execution_context",
                "response_summary": exc.response_summary,
                "author_action": author_action(
                    f"{operation}未能启动",
                    f"{operation}请求在进入模型台账之前被拒绝（{exc.error_code}），已有正文没有被撤销。"
                    "请重新运行；若持续失败，请检查系统配置里的模型路由与运行任务状态。",
                    target_view="workbench",
                    target_ref=target_ref,
                    primary_button_label="去场景工作台",
                    evidence_summary=[f"节点：{node_id}", f"错误：{exc.error_code}"],
                ),
            },
        ) from exc

    @staticmethod
    def _should_rewrite(payload: dict[str, Any]) -> bool:
        return should_rewrite(payload)

    def _persist_evaluation(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
        payload: dict[str, Any],
        llm_call_id: str | None,
    ) -> WriterEvaluation:
        evaluation = WriterEvaluation(
            evaluation_id=f"near_final_eval_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            rubric_id=NEAR_FINAL_RUBRIC_ID,
            source_text_ref=source.get("source_text_ref"),
            source_bundle_id=source.get("source_bundle_id"),
            evaluator_llm_call_id=llm_call_id,
            lens="near_final_acceptance",
            overall_score=payload.get("overall_score"),
            scores_json=payload.get("scores") or {},
            findings_json=payload.get("findings") or [],
            failure_class=payload.get("failure_class"),
            auto_rewrite_eligible=1 if NearFinalAcceptanceService._should_rewrite(payload) else 0,
            contract_field_refs_json={},
            promotion_blockers_json=_promotion_blockers_from_acceptance(payload),
            revision_brief_json=payload.get("revision_brief") or [],
            requires_human_review=1 if payload.get("requires_human_review") else 0,
            status="completed",
        )
        self.session.add(evaluation)
        self.session.flush()
        return evaluation

    def _create_scene_candidate(
        self,
        *,
        evaluation: WriterEvaluation,
        source: dict[str, Any],
        payload: dict[str, Any],
        actor_ref: str,
    ) -> RevisionCandidate:
        candidate = RevisionCandidate(
            revision_id=f"revision_near_final_{evaluation.object_id}_{uuid.uuid4().hex[:10]}",
            evaluation_id=evaluation.evaluation_id,
            object_type="scene",
            object_id=evaluation.object_id,
            chapter_id=evaluation.chapter_id,
            scene_id=evaluation.scene_id,
            revision_type=NEAR_FINAL_REWRITE_TYPE,
            source_text_ref=source.get("source_text_ref"),
            proposed_text=source.get("content") or "",
            instruction_json=payload.get("revision_brief") or _default_structure_revision_brief(),
            diff_summary_json={
                "failure_class": payload.get("failure_class"),
                "near_final_status": payload.get("near_final_status"),
                "summary": "Near-final acceptance requested a bounded full-scene literary rewrite.",
                "source_text_ref": source.get("source_text_ref"),
            },
            patches_json=[],
            apply_mode="manual_or_regenerate",
            target_text_ref=source.get("source_text_ref"),
            status="candidate",
            created_by=actor_ref or "near_final_acceptance",
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def _supersede_open_scene_candidates(self, scene_id: str) -> None:
        rows = self.session.execute(
            select(RevisionCandidate).where(
                RevisionCandidate.object_type == "scene",
                RevisionCandidate.object_id == scene_id,
                RevisionCandidate.revision_type == NEAR_FINAL_REWRITE_TYPE,
                RevisionCandidate.status == "candidate",
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"

    def _record_attempt(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        source_bundle_id: str | None,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=chapter_id,
                step="near_final_acceptance_review",
                status=status,
                source_bundle_id=source_bundle_id,
                details_json=details,
            )
        )

"""软 QC 前的自动批评（``soft_qc:auto_critique:0`` / ``soft_patch:auto_critique:0``）：产品的装配、续跑复验与被拒产品的恢复。

``Orchestrator`` 直接继承 ``AutoCritiqueCheckpointMixin``。``auto_critique.llm_auto_critique`` 经模块属性调用（测试在
那里打桩）；``_resolve_auto_critique_runner`` 是开关闸（``llm_enabled`` 且 ``llm_auto_critique_enabled``），测试在类上打桩。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
)
from novel_system.services import auto_critique as _auto_critique
from novel_system.services.llm_accounting import LLMAccountingError, LLMCallContext, validate_product_call
from novel_system.services.scene_generation import SceneGenerationPostprocessError, StepKeys
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt
from novel_system.services.story_slots import planned_chapter_goal
from novel_system.settings import get_settings


class AutoCritiqueCheckpointMixin:
    def _auto_critique_patch_context(
        self,
        scene_id: str,
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
    ) -> LLMCallContext:
        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        if scene is None:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch scene owner is missing",
            )
        return LLMCallContext(
            scope_type="scene",
            scope_id=scene_id,
            project_id=scene.project_id
            or (chapter.project_id if chapter is not None else None),
            chapter_id=scene.chapter_id,
            scene_id=scene_id,
            node_id="style_patch",
            step="soft_patch",
            execution_id=execution_id or self._execution_id,
            execution_step_key=StepKeys.soft_patch("auto_critique"),
            run_job_id=run_job_id if execution_id is not None else self._run_job_id,
        )

    def _build_auto_critique_patch_failure_product(
        self,
        scene_id: str,
        error: BaseException,
    ) -> dict[str, Any]:
        context = self._auto_critique_patch_context(scene_id)
        rows = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == self._execution_id,
                    LlmCall.execution_step_key == context.execution_step_key,
                    LlmCall.accounting_status.in_(("settled", "failed", "rejected")),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise error
        if len(rows) != 1:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch failure does not resolve to one durable parent",
                details={"llm_call_ids": [row.llm_call_id for row in rows]},
            ) from error
        parent = rows[0]
        if parent.accounting_status == "rejected":
            outcome = "rejected_before_dispatch"
            reason = "pre_dispatch_rejection"
            error_code = parent.error_code
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=error_code,
            )
        elif parent.accounting_status == "failed":
            outcome = "provider_failed"
            reason = "provider_call_failed"
            error_code = parent.error_code
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=error_code,
            )
        else:
            if (
                not isinstance(error, SceneGenerationPostprocessError)
                or error.llm_call_id != parent.llm_call_id
                or error.error_code != "SCENE_GENERATION_RESPONSE_INVALID"
            ):
                raise error
            outcome = "parse_failed"
            reason = "invalid_scene_generation_response"
            error_code = "SCENE_GENERATION_RESPONSE_INVALID"
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome="parse_failed",
            )
        if not isinstance(error_code, str) or not error_code:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch failure parent has no stable error code",
                details={"llm_call_id": parent.llm_call_id},
            ) from error
        return {
            "schema_version": 1,
            "outcome": outcome,
            "llm_call_id": parent.llm_call_id,
            "execution_id": context.execution_id,
            "execution_step_key": context.execution_step_key,
            "run_job_id": context.run_job_id,
            "provider_execution_mode": context.provider_execution_mode,
            "reason": reason,
            "error_code": error_code,
        }

    def _validate_auto_critique_product_semantics(
        self,
        product: dict[str, Any],
        *,
        source_content: str,
        patch_outcome: str,
    ) -> None:
        expected_rule = _auto_critique.auto_critique(
            source_content,
            skip_critique=(
                product.get("outcome") == "not_invoked"
                and product.get("reason") == "skip_critique"
            ),
        )
        rule_fields = {
            "rule_should_rewrite": expected_rule.rule_should_rewrite,
            "rule_directives": expected_rule.rule_directives,
            "rule_dimension_scores": expected_rule.rule_dimension_scores,
            "rule_flagged_dimensions": expected_rule.rule_flagged_dimensions,
        }
        if any(product.get(key) != value for key, value in rule_fields.items()):
            raise checkpoint_corrupt("auto-critique rule product differs from deterministic source analysis")
        if product.get("outcome") != "completed":
            merged_rule_fields = {
                "should_rewrite": expected_rule.should_rewrite,
                "directives": expected_rule.directives,
                "dimension_scores": expected_rule.dimension_scores,
                "flagged_dimensions": expected_rule.flagged_dimensions,
            }
            if any(
                product.get(key) != value for key, value in merged_rule_fields.items()
            ):
                raise checkpoint_corrupt(
                    "auto-critique degraded/no-call product is not the deterministic rule result",
                )
            if product.get("llm_contribution") is not None:
                raise checkpoint_corrupt("auto-critique non-completed product contains an LLM contribution")
        else:
            contribution = product.get("llm_contribution")
            issues = (
                contribution.get("issues") if isinstance(contribution, dict) else None
            )
            allowed_dimensions = {
                "character_consistency",
                "earned_emotion",
                "conflict_credibility",
                "information_dumping",
                "show_vs_tell",
                "pacing",
                "llm_general",
            }
            if (
                not isinstance(contribution, dict)
                or set(contribution) != {"should_rewrite", "issues"}
                or type(contribution.get("should_rewrite")) is not bool
                or not isinstance(issues, list)
                or any(
                    not isinstance(issue, dict)
                    or set(issue) != {"dimension", "directive", "evidence"}
                    or any(not isinstance(issue.get(key), str) for key in issue)
                    or issue.get("dimension") not in allowed_dimensions
                    or not issue.get("directive", "").strip()
                    or len(issue.get("evidence", "")) > 120
                    for issue in issues
                )
                or contribution.get("should_rewrite") != bool(issues)
            ):
                raise checkpoint_corrupt("auto-critique completed LLM contribution schema is invalid")
            expected_directives = list(expected_rule.directives)
            expected_flagged = list(expected_rule.flagged_dimensions)
            seen_dimensions = set(expected_flagged)
            for issue in issues:
                dimension = issue["dimension"]
                directive = issue["directive"]
                evidence = issue["evidence"]
                if dimension not in seen_dimensions and directive:
                    entry = f"[LLM路{dimension}] {directive}"
                    if evidence:
                        entry += f" (evidence: {evidence[:120]})"
                    expected_directives.append(entry)
                    expected_flagged.append(dimension)
                    seen_dimensions.add(dimension)
            completed_invariants_hold = (
                product.get("directives") == expected_directives
                and product.get("flagged_dimensions") == expected_flagged
                and product.get("dimension_scores")
                == product.get("rule_dimension_scores")
                and product.get("should_rewrite")
                == (expected_rule.should_rewrite or contribution["should_rewrite"])
            )
            if not completed_invariants_hold:
                raise checkpoint_corrupt(
                    "auto-critique completed product violates deterministic merge invariants",
                )
        should_rewrite = product.get("should_rewrite") is True
        if (not should_rewrite and patch_outcome != "unchanged") or (
            should_rewrite
            and patch_outcome not in {"patched", "patch_skipped", "patch_failed"}
        ):
            raise checkpoint_corrupt("auto-critique decision and patch outcome are semantically inconsistent")

    def _auto_critique_llm_contribution_hash(self, product: dict[str, Any]) -> str:
        contribution = product.get("llm_contribution")
        if not isinstance(contribution, dict):
            return ""
        return _auto_critique.critique_llm_contribution_hash(contribution)

    def _validate_auto_critique_patch_failure_checkpoint(
        self,
        scene_id: str,
        product: Any,
        *,
        validate_checkpoint_hash: bool = True,
    ) -> None:
        expected_fields = {
            "schema_version",
            "outcome",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "provider_execution_mode",
            "reason",
            "error_code",
        }
        if (
            not isinstance(product, dict)
            or set(product) != expected_fields
            or product.get("schema_version") != 1
            or product.get("outcome")
            not in {"provider_failed", "rejected_before_dispatch", "parse_failed"}
            or not isinstance(product.get("llm_call_id"), str)
            or not self._checkpoint_execution_owner_matches(
                product.get("execution_id"), product.get("run_job_id")
            )
            or product.get("execution_step_key") != StepKeys.soft_patch("auto_critique")
            or product.get("provider_execution_mode") != "online"
            or not isinstance(product.get("reason"), str)
            or not isinstance(product.get("error_code"), str)
        ):
            raise checkpoint_corrupt("auto-critique patch failure product schema/owner is invalid")
        if validate_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("soft_auto_critique_patch_failure"):
            raise checkpoint_corrupt("auto-critique patch failure product hash mismatch")
        context = self._auto_critique_patch_context(
            scene_id,
            execution_id=product["execution_id"],
            run_job_id=product["run_job_id"],
        )
        call_id = product["llm_call_id"]
        outcome = product["outcome"]
        expected_status = {
            "provider_failed": "failed",
            "rejected_before_dispatch": "rejected",
            "parse_failed": "settled",
        }[outcome]
        parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=call_id,
            execution_step_key=context.execution_step_key,
            execution_id=context.execution_id,
            allowed_accounting_statuses=(expected_status,),
            allow_local_rejected_output=outcome == "rejected_before_dispatch",
        )
        if not isinstance(
            parent.response_payload_summary, dict
        ) or parent.response_payload_summary.get(
            "auto_critique_patch_failure_hash"
        ) != self._json_hash(
            product
        ):
            raise checkpoint_corrupt("auto-critique patch failure product is detached from its parent")
        try:
            if outcome == "parse_failed":
                if (
                    product.get("reason") != "invalid_scene_generation_response"
                    or product.get("error_code") != "SCENE_GENERATION_RESPONSE_INVALID"
                ):
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique patch parse failure code is invalid",
                    )
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome="parse_failed",
                )
            else:
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome=outcome,
                    expected_error_code=product["error_code"],
                )
        except LLMAccountingError as exc:
            raise checkpoint_corrupt(
                "auto-critique patch failure parent/attempt ledger is invalid",
                details={"llm_call_id": call_id, "error_code": exc.code},
            ) from exc

    def _recover_auto_critique_patch_rejected_product(
        self,
        scene_id: str,
        *,
        allow_retry: bool,
    ) -> dict[str, Any] | None:
        """Recover a rejected patch tombstone before gate changes can hide it."""

        query_context = self._auto_critique_patch_context(scene_id)
        rows = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == self._execution_id,
                    LlmCall.execution_step_key == query_context.execution_step_key,
                )
            )
            .scalars()
            .all()
        )
        rejected = [row for row in rows if row.accounting_status == "rejected"]
        if not rejected:
            released = [row for row in rows if row.accounting_status == "released"]
            if released and len(released) == len(rows):
                if allow_retry:
                    return None
                raise checkpoint_corrupt(
                    "auto-critique patch no-call gate cannot replace a released tombstone",
                    details={"llm_call_ids": [row.llm_call_id for row in rows]},
                )
            return None
        if len(rows) != 1:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch rejection does not resolve to one durable parent",
                details={"llm_call_ids": [row.llm_call_id for row in rows]},
            )
        parent = rejected[0]
        historical_execution_mode = self._parent_execution_mode(parent)
        context = self._auto_critique_patch_context(scene_id)
        if not isinstance(parent.error_code, str) or not parent.error_code:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch rejected tombstone has no terminal error code",
                details={"llm_call_id": parent.llm_call_id},
            )
        validate_product_call(
            self.session,
            parent.llm_call_id,
            context,
            expected_outcome="rejected_before_dispatch",
            expected_error_code=parent.error_code,
        )
        return {
            "schema_version": 1,
            "outcome": "rejected_before_dispatch",
            "llm_call_id": parent.llm_call_id,
            "execution_id": context.execution_id,
            "execution_step_key": context.execution_step_key,
            "run_job_id": context.run_job_id,
            "provider_execution_mode": historical_execution_mode,
            "reason": "pre_dispatch_rejection",
            "error_code": parent.error_code,
        }

    def _recover_auto_critique_rejected_product(
        self,
        context: LLMCallContext,
        rule_result: Any,
        *,
        allow_retry: bool,
    ) -> Any | None:
        """Rebuild a lost no-dispatch product before a flipped gate emits no-call."""

        rows = (
            self.session.execute(
                select(LlmCall)
                .where(
                    LlmCall.scene_id == context.scene_id,
                    LlmCall.execution_id == context.execution_id,
                    LlmCall.execution_step_key == context.execution_step_key,
                )
                .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            return None

        rejected: list[LlmCall] = []
        for row in rows:
            if row.accounting_status == "rejected":
                if not isinstance(row.error_code, str) or not row.error_code:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique rejected tombstone has no terminal error code",
                        details={"llm_call_id": row.llm_call_id},
                    )
                validate_product_call(
                    self.session,
                    row.llm_call_id,
                    context,
                    expected_outcome="rejected_before_dispatch",
                    expected_error_code=row.error_code,
                )
                rejected.append(row)
                continue
            if row.accounting_status == "released":
                attempts = (
                    self.session.execute(
                        select(LlmCallAttempt).where(
                            LlmCallAttempt.llm_call_id == row.llm_call_id
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    row.request_dispatched_at is not None
                    or row.budget_charged_tokens != 0
                    or row.total_tokens != 0
                    or row.scope_type != context.scope_type
                    or row.scope_id != context.scope_id
                    or row.node_id != context.node_id
                    or row.step != context.step
                    or row.project_id != context.project_id
                    or row.chapter_id != context.chapter_id
                    or row.run_job_id != context.run_job_id
                    or any(
                        attempt.request_dispatched_at is not None
                        or attempt.budget_charged_tokens != 0
                        or attempt.total_tokens != 0
                        or attempt.accounting_status != "released"
                        for attempt in attempts
                    )
                ):
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique released tombstone is not a zero-dispatch ledger",
                        details={"llm_call_id": row.llm_call_id},
                    )
                continue
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique no-call gate conflicts with a non-retryable ledger row",
                details={
                    "llm_call_id": row.llm_call_id,
                    "accounting_status": row.accounting_status,
                },
            )

        if not rejected:
            if allow_retry:
                return None
            raise checkpoint_corrupt(
                "auto-critique no-call gate cannot replace a released accounting tombstone",
                details={"execution_step_key": context.execution_step_key},
            )
        parent = rejected[-1]
        return replace(
            rule_result,
            outcome="rejected_before_dispatch",
            llm_call_id=parent.llm_call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
            reason="pre_dispatch_rejection",
            error_code=parent.error_code,
        )

    def _validate_auto_critique_checkpoint(
        self,
        scene_id: str,
        product: dict[str, Any],
        *,
        source_content: str,
    ) -> None:
        expected_fields = {
            "schema_version",
            "outcome",
            "should_rewrite",
            "directives",
            "dimension_scores",
            "flagged_dimensions",
            "rule_should_rewrite",
            "rule_directives",
            "rule_dimension_scores",
            "rule_flagged_dimensions",
            "llm_contribution",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "reason",
            "error_code",
        }

        if set(product) != expected_fields or product.get("schema_version") != 1:
            raise checkpoint_corrupt("auto-critique checkpoint product schema is invalid")
        outcome = product.get("outcome")
        if outcome not in {
            "not_invoked",
            "completed",
            "rejected_before_dispatch",
            "provider_failed",
            "parse_failed",
        }:
            raise checkpoint_corrupt("auto-critique checkpoint outcome is invalid")
        if (
            type(product.get("should_rewrite")) is not bool
            or type(product.get("rule_should_rewrite")) is not bool
            or any(
                not isinstance(product.get(field_name), list)
                or any(not isinstance(item, str) for item in product[field_name])
                for field_name in (
                    "directives",
                    "flagged_dimensions",
                    "rule_directives",
                    "rule_flagged_dimensions",
                )
            )
            or any(
                not isinstance(product.get(field_name), dict)
                or any(
                    not isinstance(key, str)
                    or type(value) not in {int, float}
                    or not 0 <= value <= 1
                    for key, value in product[field_name].items()
                )
                for field_name in ("dimension_scores", "rule_dimension_scores")
            )
        ):
            raise checkpoint_corrupt("auto-critique checkpoint product fields are invalid")
        if outcome != "completed" and product.get("llm_contribution") is not None:
            raise checkpoint_corrupt("auto-critique non-completed contribution field is invalid")
        expected_step = "soft_qc:auto_critique:0"
        if (
            not self._checkpoint_execution_owner_matches(
                product.get("execution_id"), product.get("run_job_id")
            )
            or product.get("execution_step_key") != expected_step
        ):
            raise checkpoint_corrupt("auto-critique checkpoint execution ownership is invalid")

        call_id = product.get("llm_call_id")
        reason = product.get("reason")
        error_code = product.get("error_code")
        if outcome == "not_invoked":
            if (
                call_id is not None
                or reason
                not in {
                    "skip_critique",
                    "budget_or_candidate_cap",
                    "feature_disabled",
                    "runner_unavailable",
                }
                or error_code is not None
            ):
                raise checkpoint_corrupt("auto-critique no-call outcome field matrix is invalid")
            ledger_rows = (
                self.session.execute(
                    select(LlmCall).where(
                        LlmCall.execution_id == product.get("execution_id"),
                        LlmCall.execution_step_key == expected_step,
                    )
                )
                .scalars()
                .all()
            )
            if ledger_rows:
                raise checkpoint_corrupt(
                    "auto-critique no-call product unexpectedly has an execution ledger"
                )
            expected_rule = _auto_critique.auto_critique(
                source_content,
                skip_critique=reason == "skip_critique",
            )
            if any(
                product.get(product_key) != expected_value
                for product_key, expected_value in {
                    "should_rewrite": expected_rule.should_rewrite,
                    "directives": expected_rule.directives,
                    "dimension_scores": expected_rule.dimension_scores,
                    "flagged_dimensions": expected_rule.flagged_dimensions,
                    "rule_should_rewrite": expected_rule.rule_should_rewrite,
                    "rule_directives": expected_rule.rule_directives,
                    "rule_dimension_scores": expected_rule.rule_dimension_scores,
                    "rule_flagged_dimensions": expected_rule.rule_flagged_dimensions,
                }.items()
            ):
                raise checkpoint_corrupt(
                    "auto-critique no-call product differs from its deterministic rule result"
                )
            return
        if not isinstance(call_id, str) or not call_id:
            raise checkpoint_corrupt("auto-critique called outcome is missing its parent id")
        if outcome == "completed":
            if reason is not None or error_code is not None:
                raise checkpoint_corrupt("auto-critique completed outcome field matrix is invalid")
        elif (
            not isinstance(reason, str)
            or not reason
            or not isinstance(error_code, str)
            or not error_code
        ):
            raise checkpoint_corrupt("auto-critique degraded outcome field matrix is invalid")
        if outcome == "parse_failed" and (
            reason != "invalid_llm_response"
            or error_code != "LLM_CRITIQUE_RESPONSE_INVALID"
        ):
            raise checkpoint_corrupt("auto-critique parse-failed outcome code is invalid")

        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        if scene is None:
            raise checkpoint_corrupt("auto-critique checkpoint scene owner is missing")
        product_parent = self.session.get(LlmCall, call_id)
        context = LLMCallContext(
            scope_type="scene",
            scope_id=scene_id,
            project_id=scene.project_id
            or (chapter.project_id if chapter is not None else None),
            chapter_id=scene.chapter_id,
            scene_id=scene_id,
            node_id="soft_qc",
            step=expected_step,
            execution_id=product.get("execution_id"),
            execution_step_key=expected_step,
            run_job_id=product.get("run_job_id"),
        )
        expected_status = {
            "completed": "settled",
            "parse_failed": "settled",
            "rejected_before_dispatch": "rejected",
            "provider_failed": "failed",
        }[outcome]
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=call_id,
            execution_step_key=expected_step,
            execution_id=product.get("execution_id"),
            allowed_accounting_statuses=(expected_status,),
            allow_local_rejected_output=outcome == "rejected_before_dispatch",
        )
        parent = product_parent
        if (
            parent is None
            or not isinstance(parent.response_payload_summary, dict)
            or parent.response_payload_summary.get("auto_critique_product_hash")
            != self._json_hash(product)
        ):
            raise checkpoint_corrupt("auto-critique product hash is detached from its accounting parent")
        if outcome == "completed" and (
            parent.response_payload_summary.get("auto_critique_parsed_llm_hash")
            != self._auto_critique_llm_contribution_hash(product)
        ):
            raise checkpoint_corrupt(
                "auto-critique LLM merge payload is detached from its parsed-result hash"
            )
        try:
            validate_product_call(
                self.session,
                call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=(
                    error_code
                    if outcome in {"rejected_before_dispatch", "provider_failed"}
                    else None
                ),
            )
        except LLMAccountingError as exc:
            raise checkpoint_corrupt(
                "auto-critique checkpoint parent/physical-attempt ledger is invalid",
                details={"llm_call_id": call_id, "error_code": exc.code},
            ) from exc

    def _resolve_auto_critique_runner(self):
        """§8 gate: the independent LLM editor critic is layered on ONLY when both
        ``llm_enabled`` and ``llm_auto_critique_enabled`` are set (opt-in); otherwise the
        critic runner is ``None`` and ``llm_auto_critique`` degrades to the rule-based pass.
        Extracted from ``run_scene`` so the opt-in gate is unit-testable in isolation
        (blueprint §8 + §15 honest-bounds)."""
        settings = get_settings()
        return (
            self.llm_runner
            if (settings.llm_enabled and settings.llm_auto_critique_enabled)
            else None
        )

    def _scene_critique_context(self, scene: SceneCard, contract):
        """Build the §8 SceneContext for the LLM editor critic (best-effort; the critic
        degrades gracefully when fields are absent)."""
        payload = getattr(contract, "payload_json", None) or {}
        brief = getattr(scene, "writer_brief_json", None) or {}
        tension = brief.get("tension_target")
        # 场目标只给作者规划过的：旧物化给没写摘要的场补的本章样板目标「推进本章：<章名>」不算（S2 1）
        chapter_id = getattr(scene, "chapter_id", None)
        chapter = self.session.get(ChapterGoal, chapter_id) if chapter_id else None
        return _auto_critique.SceneContext(
            scene_goal=str(
                planned_chapter_goal(getattr(scene, "scene_goal", ""), chapter) or payload.get("scene_goal") or ""
            ),
            tension_target=tension if isinstance(tension, int) else None,
            cost_requirement=str(
                payload.get("cost_requirement") or brief.get("cost_requirement") or ""
            ),
        )

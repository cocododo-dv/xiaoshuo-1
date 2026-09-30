"""bundle、首稿与硬 QC 的检查点读回，以及生成产品的账本父调用核对（续跑时用）。

``Orchestrator`` 直接继承 ``DraftCheckpointMixin``。历史上离线确定性执行模式写下的父调用照旧拒绝（``_parent_execution_mode``）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    LlmCall,
    QcReport,
    SceneBundle,
    SceneCard,
    SceneDraft,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import verify_bundle_snapshot_hash
from novel_system.services.llm_accounting import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    LLMAccountingError,
    validate_product_call_ledger,
)
from novel_system.services.qc_engine import HardQcDecision
from novel_system.services.scene_generation import NeutralGenerationResult, StyleGenerationResult
from novel_system.services.scene_run.snapshots import qc_report_snapshot
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt


class DraftCheckpointMixin:
    def _validate_qc_attempt(
        self,
        *,
        scene_id: str,
        step: str,
        qc_report_id: str,
        source_bundle_id: str,
        source_draft_row_id: str | None,
        llm_call_id: str | None,
        execution_step_key: str | None,
    ) -> None:
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == step,
                    AttemptTracker.source_bundle_id == source_bundle_id,
                )
            )
            .scalars()
            .all()
        )
        matched = []
        for attempt in attempts:
            details = attempt.details_json or {}
            if details.get("qc_report_id") != qc_report_id:
                continue
            if (
                source_draft_row_id is not None
                and details.get("source_draft_row_id") != source_draft_row_id
            ):
                continue
            if details.get("llm_call_id") != llm_call_id:
                continue
            if details.get("execution_step_key") != execution_step_key:
                continue
            matched.append(attempt)
        if len(matched) != 1:
            raise checkpoint_corrupt(
                f"{step} checkpoint has no unique matching attempt audit row",
                details={
                    "qc_report_id": qc_report_id,
                    "matching_attempts": len(matched),
                },
            )

    def _load_checkpoint_bundle(self, scene_id: str) -> dict[str, Any]:
        bundle_id = self._checkpoint_artifact(
            "bundle_id", expected_node_at_least="bundle_ready"
        )
        expected_hash = self._checkpoint_hash("bundle")
        bundle = (
            self.session.get(SceneBundle, bundle_id)
            if isinstance(bundle_id, str)
            else None
        )
        if bundle is None:
            raise DomainError(
                "RUN_CHECKPOINT_OUTPUT_MISSING",
                "checkpoint bundle output is missing",
                status_code=409,
                details={"bundle_id": bundle_id},
            )
        if bundle.scene_id != scene_id or bundle.bundle_snapshot_hash != expected_hash:
            raise checkpoint_corrupt("checkpoint bundle identity/hash mismatch")
        bundle_integrity = verify_bundle_snapshot_hash(
            bundle.frozen_snapshot_json,
            expected_hash=bundle.bundle_snapshot_hash,
        )
        if not bundle_integrity["valid"]:
            raise checkpoint_corrupt(
                "checkpoint bundle snapshot no longer matches its recorded hash",
                details={
                    "bundle_id": bundle.bundle_id,
                    "bundle_integrity": bundle_integrity,
                },
            )
        return {
            "bundle_id": bundle.bundle_id,
            "bundle_snapshot_hash": bundle.bundle_snapshot_hash,
            "snapshot": bundle.frozen_snapshot_json,
        }

    def _load_checkpoint_draft(
        self,
        scene_id: str,
        *,
        ref_key: str,
        expected_stage: str,
        expected_node_at_least: str,
        result_type: str,
    ) -> NeutralGenerationResult | StyleGenerationResult:
        row_id = self._checkpoint_artifact(
            ref_key, expected_node_at_least=expected_node_at_least
        )
        row = self._require_checkpoint_row(SceneDraft, row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        hash_key = "draft" if result_type == "neutral" else "selected_draft"
        if result_type == "neutral":
            llm_call_id = self._checkpoint_artifact(
                "neutral_llm_call_id",
                expected_node_at_least=expected_node_at_least,
            )
            execution_step_key = self._checkpoint_artifact(
                "neutral_execution_step_key",
                expected_node_at_least=expected_node_at_least,
            )
            artifact_execution_id = self._checkpoint_artifact(
                "neutral_artifact_execution_id",
                expected_node_at_least=expected_node_at_least,
            )
        else:
            llm_call_id = self._checkpoint_artifact(
                "style_llm_call_id",
                expected_node_at_least=expected_node_at_least,
            )
            execution_step_key = self._checkpoint_artifact(
                "style_execution_step_key",
                expected_node_at_least=expected_node_at_least,
            )
            artifact_execution_id = self._checkpoint_artifact(
                "style_artifact_execution_id",
                expected_node_at_least=expected_node_at_least,
            )
        artifact_execution_id = self._validate_artifact_execution_owner(
            artifact_execution_id
        )
        generation_parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            execution_id=artifact_execution_id,
        )
        try:
            self._validate_settled_parent_ledger(generation_parent)
        except LLMAccountingError as exc:
            raise checkpoint_corrupt(
                f"{result_type} checkpoint generation attempt ledger is invalid",
                details={"llm_call_id": llm_call_id, "error_code": exc.code},
            ) from exc
        if (
            row.scene_id != scene_id
            or row.stage != expected_stage
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != llm_call_id
            or self._text_hash(row.content) != self._checkpoint_hash(hash_key)
        ):
            raise checkpoint_corrupt("checkpoint draft identity/hash mismatch")
        kwargs = {
            "row_id": row.row_id,
            "content": row.content,
            "llm_call_id": llm_call_id,
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": bundle["bundle_snapshot_hash"],
            "execution_step_key": execution_step_key,
            "artifact_execution_id": artifact_execution_id,
        }
        if result_type == "neutral":
            return NeutralGenerationResult(**kwargs)
        return StyleGenerationResult(**kwargs)

    def _load_hard_qc_checkpoint(self, scene_id: str) -> HardQcDecision:
        qc_report_id = self._checkpoint_artifact(
            "qc_report_id", expected_node_at_least="hard_qc_ready"
        )
        report = self._require_checkpoint_row(QcReport, qc_report_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        source_draft_row_id = refs.get("hard_qc_source_draft_row_id")
        hard_qc_execution_id = self._validate_artifact_execution_owner(
            refs.get("hard_qc_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=refs.get("hard_qc_llm_call_id"),
            execution_step_key=refs.get("hard_qc_execution_step_key"),
            execution_id=hard_qc_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        if (
            report.scene_id != scene_id
            or report.qc_type != "hard_qc"
            or report.source_draft_row_id != source_draft_row_id
            or source_draft_row_id != refs.get("neutral_draft_row_id")
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get("hard_qc_bundle_id") != bundle["bundle_id"]
        ):
            raise checkpoint_corrupt("hard QC checkpoint identity/source mismatch")
        decision = HardQcDecision(
            branch=str(refs.get("branch") or "continue"),
            qc_report_id=report.qc_report_id,
            human_review_event_id=refs.get("human_review_event_id"),
            resolution_code=str(
                refs.get("resolution_code") or report.resolution_code or ""
            ),
            next_action=str(refs.get("next_action") or report.next_action or ""),
            should_continue=bool(refs.get("should_continue")),
            stop_reason=refs.get("stop_reason"),
            llm_call_id=refs.get("hard_qc_llm_call_id"),
            execution_step_key=refs.get("hard_qc_execution_step_key"),
        )
        decision_summary = {
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "human_review_event_id": decision.human_review_event_id,
            "resolution_code": decision.resolution_code,
            "next_action": decision.next_action,
            "stop_reason": decision.stop_reason,
            "should_continue": decision.should_continue,
            "llm_call_id": decision.llm_call_id,
            "execution_step_key": decision.execution_step_key,
        }
        if self._json_hash(decision_summary) != self._checkpoint_hash(
            "hard_qc_decision"
        ):
            raise checkpoint_corrupt("hard QC decision hash mismatch")
        if self._json_hash(qc_report_snapshot(report)) != self._checkpoint_hash(
            "hard_qc_report"
        ):
            raise checkpoint_corrupt("hard QC report hash mismatch")
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="hard_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=None,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        return decision

    def _validate_settled_parent_ledger(self, parent: LlmCall) -> None:
        """Validate a provider-success parent without conflating later product parsing."""

        if parent.accounting_status != "settled":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "provider-success product parent is not settled",
                details={"llm_call_id": parent.llm_call_id},
            )
        if parent.provider == "offline_deterministic":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "retired non-provider execution cannot back a product",
                details={"llm_call_id": parent.llm_call_id},
            )
        validate_product_call_ledger(
            self.session,
            parent,
            expected_outcome="completed",
        )

    def _validate_generation_before_checkpoint(
        self,
        scene_id: str,
        generation: StyleGenerationResult,
    ) -> LlmCall:
        parent = self.session.get(LlmCall, generation.llm_call_id)
        owner = generation.artifact_execution_id or self._execution_id
        draft = self.session.get(SceneDraft, generation.row_id)
        if (
            parent is None
            or draft is None
            or draft.scene_id != scene_id
            or draft.generation_llm_call_id != generation.llm_call_id
            or parent.scene_id != scene_id
            or parent.execution_id != owner
            or parent.execution_step_key != generation.execution_step_key
        ):
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product is detached from its durable parent",
                details={"llm_call_id": generation.llm_call_id},
            )
        self._validate_generation_parent_identity(
            scene_id=scene_id,
            parent=parent,
            draft_stage=draft.stage,
            execution_step_key=generation.execution_step_key,
            execution_id=owner,
            draft=draft,
        )
        self._validate_settled_parent_ledger(parent)
        return parent

    def _is_accepted_first_draft(self, draft: SceneDraft | None, parent: LlmCall) -> bool:
        """风格参考 v3（P5b）：风格稿行是不是「首稿即风格稿」——它的父调用是首稿那次（style_draft 节点、
        neutral_draft / neutral_draft_repair 步），且正文与那次调用写下的首稿行逐字相同。"""
        if draft is None or draft.stage != "style_draft":
            return False
        if parent.node_id != "style_draft" or parent.step not in {"neutral_draft", "neutral_draft_repair"}:
            return False
        first = self.session.execute(
            select(SceneDraft).where(
                SceneDraft.scene_id == draft.scene_id,
                SceneDraft.stage == "neutral_draft",
                SceneDraft.generation_llm_call_id == parent.llm_call_id,
            )
        ).scalars().first()
        return first is not None and (first.content or "") == (draft.content or "")

    def _validate_generation_parent_identity(
        self,
        *,
        scene_id: str,
        parent: LlmCall,
        draft_stage: str,
        execution_step_key: str | None,
        execution_id: str | None,
        draft: SceneDraft | None = None,
    ) -> None:
        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        stage_owner = {
            "style_draft": ("style_draft", "style_draft"),
            "style_patch": ("style_patch", "soft_patch"),
            "de_template": ("style_patch", "de_template"),
        }.get(draft_stage)
        if draft_stage == "style_draft" and self._is_accepted_first_draft(draft, parent):
            stage_owner = ("style_draft", parent.step)
        if scene is None or stage_owner is None:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product stage/scene owner is invalid",
                details={"llm_call_id": parent.llm_call_id, "draft_stage": draft_stage},
            )
        node_id, step = stage_owner
        actual_execution_mode = (
            parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        # 只有在线执行记账（离线确定性执行已退役）：历史上的离线父调用过不了这里。
        if actual_execution_mode != "online":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product execution mode snapshot is invalid",
                details={"llm_call_id": parent.llm_call_id},
            )
        expected_project_id = scene.project_id or (
            chapter.project_id if chapter is not None else None
        )
        if (
            parent.scope_type != "scene"
            or parent.scope_id != scene_id
            or parent.project_id != expected_project_id
            or parent.chapter_id != scene.chapter_id
            or parent.scene_id != scene_id
            or not self._checkpoint_execution_owner_matches(
                execution_id, parent.run_job_id
            )
            or parent.execution_id != execution_id
            or parent.execution_step_key != execution_step_key
            or parent.node_id != node_id
            or parent.step != step
        ):
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product is detached from its exact durable owner",
                details={"llm_call_id": parent.llm_call_id, "draft_stage": draft_stage},
            )

    @staticmethod
    def _parent_execution_mode(parent: LlmCall) -> str:
        mode = (
            parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        if mode != "online":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "durable parent has no valid provider execution mode marker",
                details={"llm_call_id": parent.llm_call_id},
            )
        return mode

from __future__ import annotations

import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RevisionCandidate,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    WriterEvaluation,
    utcnow,
)
from novel_system.services import scene_budget
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.llm_accounting import (
    LLMAccountingRejected,
)
from novel_system.services.literary_quality import adversarial_rank_score
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.author_state import compute_author_state
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.llm_task_runner import (
    LLMNodeRunner,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.near_final import (
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.qc_engine import (
    HardQcEngine,
    SoftQcEngine,
)
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_lookup import get_scene_or_404
from novel_system.services.scene_criticality import classify_scene_with_context
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference.style_step import (
    fidelity_thresholds,
)
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_generation import (
    SceneGenerationService,
    StyleGenerationResult,
    assess_rewrite_regressions,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_run_checkpoint import (
    RUN_CHECKPOINT_ORDER,
    SceneRunCheckpointService,
    checkpoint_corrupt,
)
from novel_system.services.scene_run.critique import AutoCritiqueCheckpointMixin
from novel_system.services.scene_run.planning import PlanningCheckpointMixin
from novel_system.services.scene_run.drafts import DraftCheckpointMixin
from novel_system.services.scene_run.style_candidates import StyleCandidatesMixin
from novel_system.services.scene_run.soft_qc import SoftQcCheckpointMixin
from novel_system.services.scene_run.archive import ArchiveCheckpointMixin
from novel_system.services.scene_run.kernel import RunCheckpointKernelMixin
from novel_system.services.scene_run.constants import (
    NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON,
    NEAR_FINAL_REWRITE_GATE_STAGE,
    NEAR_FINAL_REWRITE_MOVED_AWAY_SKIP_REASON,
    NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON,
    STYLE_PATCH_REVERTED_SKIP_REASON,
    STYLE_PATCH_REVERTED_STOP_REASON,
)
from novel_system.services.scene_run.near_final_gate import (
    _near_final_rejection_skip_reason,
    _near_final_rewrite_gate_summary,
    _near_final_rewrite_gate_warnings,
)
from novel_system.services.scene_run.results import (
    apply_finality,
    merged_warnings,
    near_evaluation_payload,
    near_final_result_payload,
    qc_decision_payload,
    soft_risk_acceptance_event_id,
)
from novel_system.services.scene_run.snapshots import (
    planning_provenance,
    qc_report_snapshot,
    revision_candidate_snapshot,
    writer_evaluation_snapshot,
)
from novel_system.services.scene_archive_effects import SceneArchiveEffects

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult

_LOGGER = logging.getLogger(__name__)

# 测试与旧调用方从这里取的名字（家在 services.scene_run.*）
__all__ = [
    "NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON",
    "NEAR_FINAL_REWRITE_GATE_STAGE",
    "NEAR_FINAL_REWRITE_MOVED_AWAY_SKIP_REASON",
    "NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON",
    "STYLE_PATCH_REVERTED_SKIP_REASON",
    "STYLE_PATCH_REVERTED_STOP_REASON",
    "Orchestrator",
    "_near_final_rejection_skip_reason",
    "_near_final_rewrite_gate_summary",
    "_near_final_rewrite_gate_warnings",
]


class Orchestrator(SoftQcCheckpointMixin, StyleCandidatesMixin, DraftCheckpointMixin, PlanningCheckpointMixin, AutoCritiqueCheckpointMixin, ArchiveCheckpointMixin, RunCheckpointKernelMixin):
    def __init__(
        self,
        session: Session,
        *,
        scene_generation_service: SceneGenerationService | None = None,
        hard_qc_engine: HardQcEngine | None = None,
        soft_qc_engine: SoftQcEngine | None = None,
        planning_service: NearFinalPlanningService | None = None,
        near_final_service: NearFinalAcceptanceService | None = None,
    ) -> None:
        self.session = session
        self.bundle_builder = BundleBuilder(session)
        self.archiver = Archiver(session)
        self.aggregator = Aggregator(session)
        llm_runner = LLMNodeRunner(session)
        self.llm_runner = llm_runner
        self.scene_generation_service = (
            scene_generation_service
            or SceneGenerationService(session, llm_runner=llm_runner)
        )
        self.hard_qc_engine = hard_qc_engine or HardQcEngine(
            session, llm_runner=llm_runner
        )
        self.soft_qc_engine = soft_qc_engine or SoftQcEngine(
            session, llm_runner=llm_runner
        )
        self.scene_blueprint_service = SceneBlueprintService(
            session, llm_runner=llm_runner
        )
        self.execution_contract_service = SceneExecutionContractService(session)
        self.planning_service = planning_service or NearFinalPlanningService(
            session, llm_runner=llm_runner
        )
        self.near_final_service = near_final_service or NearFinalAcceptanceService(
            session, llm_runner=llm_runner
        )
        # 每次运行的执行归属（检查点内核 RunCheckpointKernelMixin 的四个字段）：开跑时设、收尾时清
        self._execution_id: str | None = None
        self._run_job_id: str | None = None
        self._checkpoint_service: SceneRunCheckpointService | None = None
        self._lease_renewer = None

    def run_scene(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        author_note = normalize_author_note(author_note)
        get_scene_or_404(self.session, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()

        effective_execution_id = execution_id or f"direct:{scene_id}:{uuid4().hex}"
        checkpoints = SceneRunCheckpointService(self.session)
        claim = checkpoints.acquire_execution(scene_id, effective_execution_id)
        if claim.last_node == "archived":
            self._execution_id = effective_execution_id
            self._run_job_id = run_job_id
            self._checkpoint_service = checkpoints
            try:
                self._assert_author_note_matches_bundle(
                    self._load_checkpoint_bundle(scene_id),
                    author_note,
                    scene_id=scene_id,
                )
                final_scene = self._load_archived_checkpoint(scene_id)
                if claim.status != "completed":
                    checkpoints.mark_completed(scene_id, effective_execution_id)
                    self.session.commit()
                return self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": state.current_bundle_id,
                        "current_bundle_hash": state.current_bundle_hash,
                        "current_final_scene_row_id": final_scene.row_id,
                    },
                )
            finally:
                self._execution_id = None
                self._run_job_id = None
                self._checkpoint_service = None
        self._prepare_state_for_run(state, new_execution=not claim.resumed)
        state.run_policy = run_policy
        self.session.commit()

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            self._raise_if_run_cancelled()
            result = self._run_scene_pipeline(
                scene_id,
                author_note=author_note,
                run_policy=run_policy,
            )
            if result.get("scene_status") in {
                "archived",
                "quality_warning_pending_acceptance",
            }:
                # Strict mode deliberately stops with a valid draft awaiting the
                # author's Q2/Q3 acceptance.  It is a successful execution
                # terminal, not a failure/retry checkpoint.
                checkpoints.mark_completed(scene_id, effective_execution_id)
            elif result.get("scene_status") == "awaiting_candidate_selection":
                checkpoints.mark_waiting_selection(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except DomainError as exc:
            if exc.code == "RUN_JOB_CANCELLED_BY_AUTHOR":
                checkpoints.mark_cancelled(scene_id, effective_execution_id)
                self.session.commit()
                raise
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None

    @staticmethod
    def _assert_author_note_matches_bundle(
        bundle: dict[str, Any],
        author_note: str | None,
        *,
        scene_id: str,
    ) -> None:
        expected_note = str(author_note or "").strip()
        actual_note = str(
            ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
                "author_instruction"
            )
            or ""
        )
        if actual_note != expected_note:
            raise DomainError(
                "RUN_INPUT_MISMATCH",
                "author instruction differs from the frozen run checkpoint",
                status_code=409,
                details={"scene_id": scene_id, "field": "author_note"},
            )

    def _run_scene_pipeline(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
    ) -> dict:
        # Wave 2/3（治理 §5.4/§5.5）：run_policy 现已落列（Wave 3 迁移 0062）。
        # reliable（默认）：Q2/Q3 警告随稿归档；strict：存在 Q2 时停在可归档的
        # quality_warning，由作者经 adopt-current 显式接受。Q0/Q1 阻断与模式无关。
        scene = get_scene_or_404(self.session, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            # FE 目录直接建的场景没有运行时状态行（scenes POST 才会建）：按同一约定补建
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()
        contract = self.execution_contract_service.get_or_create(
            scene_id, actor_ref="orchestrator"
        )
        if contract.status != "active":
            detail_reason = "scene execution contract is not ready for drafting"
            if contract.status == "blocked":
                missing_fields = list(contract.missing_fields_json or [])
                detail_reason = "scene execution contract is missing required fields"
                raise DomainError(
                    "SCENE_EXECUTION_CONTRACT_BLOCKED",
                    detail_reason,
                    status_code=409,
                    details={
                        "scene_id": scene_id,
                        "execution_contract_id": contract.contract_id,
                        "status": contract.status,
                        "missing_fields": missing_fields,
                    },
                )
            raise DomainError(
                "SCENE_EXECUTION_CONTRACT_BLOCKED",
                detail_reason,
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "execution_contract_id": contract.contract_id,
                    "status": contract.status,
                    "missing_fields": list(contract.missing_fields_json or []),
                },
            )
        # Wave 3（§6.1）：本次运行的生效策略落列（预算/使用量不在 _prepare 重置，§7.12）
        state.run_policy = run_policy

        # Wave 3（§4.6/§5.5）：确立场景 token 预算（N × 单发基线，N 取 NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER，
        # 默认 0 = 不设上限；已设不覆盖）
        if not self._checkpoint_reached("budget_ready"):
            state = scene_budget.ensure_scene_budget_initialized(self.session, scene_id)
            self._save_run_checkpoint(
                "budget_ready",
                artifact_refs={"scene_token_budget": state.scene_token_budget},
                artifact_hashes={
                    "budget_basis": self._json_hash(state.scene_budget_basis_json or {})
                },
            )
        else:
            self._validate_budget_checkpoint(state)

        planning_progress = self._planning_checkpoint_progress()
        if planning_progress < 3:
            resume_planning_artifacts: dict[str, GenerationPlanningArtifact] = {}
            if planning_progress >= 0:
                self._validate_planning_prefix(scene_id, through=planning_progress)
                blueprint = self._load_planning_blueprint_checkpoint(scene_id)
                if planning_progress >= 1:
                    resume_planning_artifacts["chapter_architecture"] = (
                        self._load_planning_artifact_checkpoint(
                            scene_id,
                            prefix="planning_chapter_architecture",
                            expected_step_key="planning:chapter_architecture",
                            expected_kind="chapter_architecture",
                        )
                    )
                if planning_progress >= 2:
                    resume_planning_artifacts["character_pressure"] = (
                        self._load_planning_artifact_checkpoint(
                            scene_id,
                            prefix="planning_character_pressure",
                            expected_step_key="planning:character_pressure",
                            expected_kind="character_pressure",
                        )
                    )
            else:
                # 风格参考 v3：蓝图版式要与这一场现在的风格策略相符（让位 → 事实版），否则重生成
                existing_blueprint = self.scene_blueprint_service.reusable(scene_id)
                blueprint_reused = existing_blueprint is not None
                if existing_blueprint is None:
                    self._reconcile_execution_step("scene_blueprint")
                blueprint = self.scene_blueprint_service.ensure_for_scene(
                    scene_id,
                    execution_step_key="scene_blueprint",
                )
                blueprint_payload = self.scene_blueprint_service.serialize(blueprint)
                assert blueprint_payload is not None
                blueprint_refs = self._planning_artifact_refs(
                    prefix="planning_scene_blueprint",
                    serialized=blueprint_payload,
                    execution_step_key="scene_blueprint",
                    reused=blueprint_reused,
                )
                self._save_run_checkpoint(
                    "planning_ready",
                    sub_index=0,
                    artifact_refs=blueprint_refs
                    | {"scene_blueprint": blueprint_payload},
                    artifact_hashes={
                        "planning_scene_blueprint": self._json_hash(blueprint_payload),
                        "planning_scene_blueprint_provenance": self._json_hash(
                            planning_provenance(
                                blueprint_refs, "planning_scene_blueprint"
                            )
                        ),
                        "scene_blueprint": self._json_hash(blueprint_payload),
                    },
                    strategy="planning_in_progress",
                )

            def _planning_artifact_committed(
                kind: str,
                serialized: dict[str, Any],
                reused: bool,
            ) -> None:
                substeps = {
                    "chapter_architecture": (
                        1,
                        "planning_chapter_architecture",
                        "planning:chapter_architecture",
                    ),
                    "character_pressure": (
                        2,
                        "planning_character_pressure",
                        "planning:character_pressure",
                    ),
                }
                if kind not in substeps:
                    raise checkpoint_corrupt(f"unknown planning artifact callback: {kind}")
                sub_index, prefix, step_key = substeps[kind]
                current_progress = self._planning_checkpoint_progress()
                if current_progress >= sub_index:
                    checkpoint_row = self._load_planning_artifact_checkpoint(
                        scene_id,
                        prefix=prefix,
                        expected_step_key=step_key,
                        expected_kind=kind,
                    )
                    if checkpoint_row.row_id != serialized.get("row_id"):
                        raise checkpoint_corrupt(f"{kind} callback differs from durable planning checkpoint")
                    return
                artifact_refs = self._planning_artifact_refs(
                    prefix=prefix,
                    serialized=serialized,
                    execution_step_key=step_key,
                    reused=reused,
                )
                self._save_run_checkpoint(
                    "planning_ready",
                    sub_index=sub_index,
                    artifact_refs=artifact_refs | {prefix: serialized},
                    artifact_hashes={
                        prefix: self._json_hash(serialized),
                        f"{prefix}_provenance": self._json_hash(
                            planning_provenance(artifact_refs, prefix)
                        ),
                    },
                    strategy="planning_in_progress",
                )

            planning = self.planning_service.ensure_scene_planning(
                scene_id,
                step_reconciler=self._reconcile_execution_step,
                artifact_committed=_planning_artifact_committed,
                resume_artifacts=resume_planning_artifacts,
            )
            blueprint_payload = self.scene_blueprint_service.serialize(blueprint)
            assert blueprint_payload is not None
            self._save_run_checkpoint(
                "planning_ready",
                sub_index=3,
                artifact_refs={
                    "planning": planning,
                    "scene_blueprint": blueprint_payload,
                },
                artifact_hashes={
                    "planning": self._json_hash(planning),
                    "scene_blueprint": self._json_hash(blueprint_payload),
                },
                strategy="planning_complete",
            )
        else:
            planning = self._load_planning_checkpoint(scene_id)

        if not self._checkpoint_reached("bundle_ready"):
            bundle = self.bundle_builder.build(scene_id, author_note=author_note)
            self._save_run_checkpoint(
                "bundle_ready",
                artifact_refs={"bundle_id": bundle["bundle_id"]},
                artifact_hashes={"bundle": bundle["bundle_snapshot_hash"]},
            )
        else:
            bundle = self._load_checkpoint_bundle(scene_id)
            self._assert_author_note_matches_bundle(
                bundle, author_note, scene_id=scene_id
            )

        # §6.4 / §16：chapter_seq、连续过渡计数、constraint_intensity 的上下文推导
        # 统一收敛在 classify_scene_with_context——与崩溃续跑同一入口，判定不得分叉。
        criticality = classify_scene_with_context(self.session, scene)
        _LOGGER.info(
            "scene %s criticality=%s reasons=%s best_of_n=%d",
            scene_id,
            criticality.level,
            criticality.reasons,
            criticality.best_of_n,
        )
        # §6 Defect D: persist criticality classification for API exposure
        state.criticality_level = criticality.level
        state.criticality_reasons_json = criticality.reasons

        if self._checkpoint_reached("neutral_ready"):
            neutral_generation = self._load_checkpoint_draft(
                scene_id,
                ref_key="neutral_draft_row_id",
                expected_stage="neutral_draft",
                expected_node_at_least="neutral_ready",
                result_type="neutral",
            )
        else:
            self._reconcile_execution_step("neutral_draft")
            neutral_generation = self.scene_generation_service.generate_neutral_draft(
                scene_id,
                bundle,
                author_note=author_note,
            )
            self._save_run_checkpoint(
                "neutral_ready",
                artifact_refs={
                    "neutral_draft_row_id": neutral_generation.row_id,
                    "neutral_llm_call_id": neutral_generation.llm_call_id,
                    "neutral_execution_step_key": neutral_generation.execution_step_key
                    or "neutral_draft",
                    "neutral_artifact_execution_id": self._execution_id,
                    "bundle_id": neutral_generation.bundle_id,
                },
                artifact_hashes={
                    "draft": self._text_hash(neutral_generation.content),
                    "bundle": neutral_generation.bundle_hash,
                },
            )
        neutral_content = neutral_generation.content

        if self._checkpoint_reached("hard_qc_ready"):
            hard_qc = self._load_hard_qc_checkpoint(scene_id)
        else:
            self._reconcile_execution_step("hard_qc:0")
            hard_qc = self.hard_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                neutral_draft_row_id=neutral_generation.row_id,
                neutral_content=neutral_content,
                execution_step_key="hard_qc:0",
            )
            # 前六键与 _hard_qc_result_payload 同源；哈希按排好序的键算（_json_hash），键序不影响哈希。
            hard_decision = {
                **qc_decision_payload(hard_qc),
                "should_continue": hard_qc.should_continue,
                "llm_call_id": hard_qc.llm_call_id,
                "execution_step_key": hard_qc.execution_step_key,
            }
            self.session.flush()
            hard_report = self.session.get(QcReport, hard_qc.qc_report_id)
            if hard_report is None:
                self._raise_checkpoint_output_missing(row_id=hard_qc.qc_report_id)
            self._save_run_checkpoint(
                "hard_qc_ready",
                artifact_refs={
                    **hard_decision,
                    "hard_qc_source_draft_row_id": neutral_generation.row_id,
                    "hard_qc_bundle_id": bundle["bundle_id"],
                    "hard_qc_llm_call_id": hard_qc.llm_call_id,
                    "hard_qc_execution_step_key": hard_qc.execution_step_key,
                    "hard_qc_artifact_execution_id": self._execution_id,
                },
                artifact_hashes={
                    "hard_qc_decision": self._json_hash(hard_decision),
                    "hard_qc_report": self._json_hash(
                        qc_report_snapshot(hard_report)
                    ),
                },
                strategy=hard_qc.resolution_code,
                branch=hard_qc.branch,
            )
        if not hard_qc.should_continue:
            self.session.flush()
            # Wave 2 项 5：所有早退结果都携带 author_state 契约（含 latest_valid 指针）
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": state.scene_status,
                    "current_bundle_id": bundle["bundle_id"],
                    "current_bundle_hash": bundle["bundle_snapshot_hash"],
                    "current_qc_report_id": state.current_qc_report_id,
                    "current_human_review_event_id": state.current_human_review_event_id,
                    "hard_qc": qc_decision_payload(hard_qc),
                },
            )

        # Wave 3（§5.5 成本分配）：候选数 N 由开关与关键度定（关键 3 / 标准 2 / 过渡 1）。
        # [批准#2] 只有作者手笔直起才出多稿：其余起草方式即使开关打开也只起一稿（检查点如实记 single）。
        n_candidates = self._best_of_n_count(contract, criticality=criticality)
        if n_candidates > 1 and not style_policy_for_bundle(bundle).style_first:
            n_candidates = 1
        candidate_summaries: list[dict[str, Any]] = []
        if self._checkpoint_reached("style_ready"):
            candidates = self._load_style_checkpoint_candidates(scene_id)
            style_generation = candidates[0]
        else:
            style_work_items = self._load_partial_style_work_items(
                scene_id,
                expected_initial_count=n_candidates,
            )
            resume_bases, resume_products = self._style_resume_products(
                style_work_items, scene_id=scene_id
            )

            def _style_product_checkpoint(
                slot_key: str,
                phase: str,
                product: StyleGenerationResult,
                metadata: dict[str, Any],
            ) -> None:
                self._update_style_work_item(
                    style_work_items,
                    slot_key=slot_key,
                    phase=phase,
                    product=product,
                    metadata=metadata,
                    neutral_draft_row_id=neutral_generation.row_id,
                )
                completed = [
                    item["final"]
                    for item in style_work_items
                    if item.get("final") is not None
                ]
                slot_order = metadata.get("slot_order")
                if not isinstance(slot_order, int):
                    raise checkpoint_corrupt("style product slot order is invalid")
                self._save_run_checkpoint(
                    "hard_qc_ready",
                    sub_index=slot_order * 2 + (1 if phase == "final" else 0),
                    artifact_refs={
                        "style_work_items": deepcopy(style_work_items),
                        "style_initial_candidate_count": n_candidates,
                        "style_candidate_row_ids": [
                            item["row_id"] for item in completed
                        ],
                        "style_candidate_llm_call_ids": [
                            item["llm_call_id"] for item in completed
                        ],
                        "style_candidate_step_keys": [
                            item["execution_step_key"] for item in completed
                        ],
                        "style_candidate_execution_ids": [
                            item["artifact_execution_id"] for item in completed
                        ],
                    },
                    artifact_hashes={
                        "style_work_items": self._json_hash(style_work_items)
                    },
                    strategy=(
                        "best_of_n_in_progress"
                        if n_candidates > 1
                        else "single_in_progress"
                    ),
                )

            if n_candidates > 1:
                candidates = (
                    self.scene_generation_service.generate_style_draft_candidates(
                        scene_id,
                        bundle,
                        neutral_draft_row_id=neutral_generation.row_id,
                        neutral_content=neutral_content,
                        author_note=author_note,
                        n_candidates=n_candidates,
                        step_reconciler=self._reconcile_execution_step,
                        resume_bases=resume_bases,
                        resume_products=resume_products,
                        product_callback=_style_product_checkpoint,
                    )
                )
            else:
                if "initial:0" not in resume_bases:
                    self._reconcile_execution_step("style_draft:0")
                style_generation = self.scene_generation_service.generate_style_draft(
                    scene_id,
                    bundle,
                    neutral_draft_row_id=neutral_generation.row_id,
                    neutral_content=neutral_content,
                    author_note=author_note,
                    resume_base=resume_bases.get("initial:0"),
                    product_callback=_style_product_checkpoint,
                    step_reconciler=self._reconcile_execution_step,
                )
                candidates = [style_generation]
            style_generation = candidates[0]

        # 标准场景：机器下限 + 受约束风格信号继续管线；关键场景在下方暂停终选。
        # 从 checkpoint 恢复时也重建同一摘要，避免审计信息因一次进程中断消失。
        for idx, cand in enumerate(candidates):
            ranking = cand.ranking_audit or {}
            cand_score = ranking.get("quality_score")
            if not isinstance(cand_score, (int, float)):
                cand_score = adversarial_rank_score(cand.content)
            rerank_audit = (
                ranking.get("rerank") if isinstance(ranking.get("rerank"), dict) else {}
            )
            summary = {
                "row_id": cand.row_id,
                "rank": idx,
                "adversarial_score": round(cand_score, 3),
                "style_score": ranking.get("style_score"),
                "style_confidence": ranking.get("style_confidence"),
                "style_rerank_mode": rerank_audit.get("applied_mode"),
                "plagiarism_passed": ranking.get("plagiarism_passed"),
                "selection_reason": ranking.get(
                    "selection_reason", "quality_order"
                ),
                "content_preview": (cand.content or "")[:300],
                "selected": idx == 0,
            }
            if "fidelity_distance" in ranking:
                # 风格参考 v3（P5b）：作者手笔直起时候选按读数排序（distance 越小越像）
                summary["fidelity_distance"] = ranking.get("fidelity_distance")
                summary["fidelity_percentile"] = ranking.get("fidelity_percentile")
            candidate_summaries.append(summary)
        if not self._checkpoint_reached("style_ready"):
            style_candidate_rankings = [
                candidate.ranking_audit for candidate in candidates
            ]
            self._save_run_checkpoint(
                "style_ready",
                artifact_refs={
                    "style_draft_row_id": style_generation.row_id,
                    "candidate_row_ids": [candidate.row_id for candidate in candidates],
                    "llm_call_ids": [candidate.llm_call_id for candidate in candidates],
                    "style_execution_step_keys": [
                        candidate.execution_step_key for candidate in candidates
                    ],
                    "style_artifact_execution_ids": [
                        candidate.artifact_execution_id or self._execution_id
                        for candidate in candidates
                    ],
                    "style_llm_call_id": style_generation.llm_call_id,
                    "style_execution_step_key": style_generation.execution_step_key,
                    "style_artifact_execution_id": style_generation.artifact_execution_id
                    or self._execution_id,
                    "bundle_id": style_generation.bundle_id,
                    "style_candidate_rankings": style_candidate_rankings,
                },
                artifact_hashes={
                    "selected_draft": self._text_hash(style_generation.content),
                    "bundle": style_generation.bundle_hash,
                    "style_candidate_rankings": self._json_hash(
                        style_candidate_rankings
                    ),
                    **{
                        f"style_ready_candidate_{index}": self._text_hash(
                            candidate.content
                        )
                        for index, candidate in enumerate(candidates)
                    },
                },
                strategy="best_of_n" if n_candidates > 1 else "single",
            )

        hard_qc_payload = qc_decision_payload(hard_qc)

        # Wave 3（§5.5）：关键场景在候选生成后暂停编排——确定性坏稿淘汰 →
        # 匿名终选 gate；作者选择后经 resume-after-selection 从批判修订/QC 继续。
        # 「§6.3 终选决定质量上界，归人」从推荐信号升级为强制暂停。
        # 风格参考 v3（L2）：按正文去重之后才数候选——作者手笔直起时没过门的修改槽位保留首稿原文，几个槽位可能
        # 是同一段字；只剩一份不同的正文就没有可选的，不能让作者对着一份稿子「终选」，管线照常往下走
        if criticality.human_gate and self._distinct_candidate_count(candidates) > 1:
            offered_row_ids = self._offer_candidates_for_selection(
                scene, state, bundle, candidates
            )
            if offered_row_ids is not None:
                content_by_row_id = {
                    candidate.row_id: candidate.content for candidate in candidates
                }
                self._save_run_checkpoint(
                    "selection_wait",
                    artifact_refs={
                        "selection_event_id": state.current_human_review_event_id,
                        "selection_candidate_row_ids": offered_row_ids,
                    },
                    artifact_hashes={
                        f"selection_candidate_{index}": self._text_hash(
                            content_by_row_id[row_id]
                        )
                        for index, row_id in enumerate(offered_row_ids)
                    },
                    strategy="human_selection",
                )
                return self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": bundle["bundle_id"],
                        "current_bundle_hash": bundle["bundle_snapshot_hash"],
                        "current_qc_report_id": state.current_qc_report_id,
                        "current_human_review_event_id": state.current_human_review_event_id,
                        "hard_qc": hard_qc_payload,
                        "planning": planning,
                        "run_policy": run_policy,
                        # 盲化：暂停响应只报数量，不带分数/预览（候选经盲化视图取用）
                        "candidate_count": len(offered_row_ids),
                        "candidate_selection_required": True,
                    },
                )

        return self._finalize_after_style(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            planning=planning,
            hard_qc_payload=hard_qc_payload,
            style_generation=style_generation,
            candidate_summaries=candidate_summaries if candidate_summaries else None,
            run_policy=run_policy,
        )

    def _finalize_after_style(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        contract,
        bundle: dict[str, Any],
        criticality,
        planning,
        hard_qc_payload: dict[str, Any],
        style_generation,
        candidate_summaries: list[dict[str, Any]] | None,
        run_policy: str,
    ) -> dict:
        """§5.5 顺序的后半段：批判修订 → 软 QC → near-final → 严格停点 → 归档。

        run_scene 与 resume_after_selection 共用。可选支出（LLM 批判、补丁、
        near-final 重写）过预算闸（§5.8 预算耗尽停止新调用、交付最佳稿）。
        """
        scene_id = scene.scene_id
        strict_mode = run_policy == "strict"

        def _optional_spend_allowed() -> bool:
            return scene_budget.can_spend(state, scene_budget.budget_unit(state))

        if self._near_final_checkpoint_progress() >= 3:
            return self._archive_near_final_checkpoint(
                scene=scene,
                state=state,
                contract=contract,
                bundle=bundle,
                hard_qc_payload=hard_qc_payload,
                planning=planning,
                candidate_summaries=candidate_summaries,
                run_policy=run_policy,
            )

        # §8 reflexion-style auto-critique pass (after best-of-N selection, before soft QC).
        # Default: rule-based pass only. Opt-in (NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED +
        # llm_enabled) layers the independent LLM editor critic on top — degrades to
        # rule-only on any runner error, never blocks (blueprint §8 + §15 honest-bounds).
        soft_qc, final_generation = self._ensure_soft_qc_subcheckpoints(
            scene=scene,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            selected_style_generation=style_generation,
            optional_spend_allowed=_optional_spend_allowed,
        )
        if soft_qc.branch == "human_review_required":
            # Wave 2：软 QC 只在 verified Q0/Q1 时才会走到这里（LLM-only 意见已在
            # 引擎内降级为 waive）——这是真硬阻断，正文保留、契约随行。
            self.session.flush()
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": state.scene_status,
                    "current_bundle_id": bundle["bundle_id"],
                    "current_bundle_hash": bundle["bundle_snapshot_hash"],
                    "current_qc_report_id": state.current_qc_report_id,
                    "current_human_review_event_id": state.current_human_review_event_id,
                    "hard_qc": hard_qc_payload,
                    "soft_qc": qc_decision_payload(soft_qc),
                    "planning": planning,
                },
            )

        (
            near_final,
            final_generation,
            rewrite_count,
            near_final_skip_reason,
            rewrite_gate,
        ) = self._ensure_near_final_subcheckpoints(
            scene=scene,
            bundle=bundle,
            source_generation=final_generation,
            optional_spend_allowed=_optional_spend_allowed,
        )
        near_final_payload = near_final_result_payload(
            near_final, rewrite_count=rewrite_count, rewrite_gate=rewrite_gate
        )
        # Wave 2（§5.4 / Wave 2 项 4）：near-final 是 LLM 提案层（Q2/Q3）——达自动
        # 修订上限（软补丁 ≤1 + 准终稿重写 ≤1 = 2 次）后不再断头，交付当前最好稿；
        # 其 requires_human_review 亦为提案，不得产生 human_review_required 断头。
        # 严格模式的 Q2 判据要看带 rewrite_style_gate 的 payload(重写稿被 gate 拒绝 /
        # gate 未执行的警告只在 payload 里),而不是评审器的原始返回。
        near_final_warnings = self._near_final_warning_findings(near_final_payload)

        # 严格模式停点：存在 Q2 级警告（软 QC 报告或 near-final 未过）时不自动归档，
        # 停在可归档的 quality_warning，由作者经 adopt-current 显式接受（留审计）。
        if strict_mode:
            strict_warnings = self._collect_q2_warnings(state, near_final_warnings)
            if strict_warnings:
                strict_gate = FinalTextGateService(self.session).evaluate(
                    scene_id=scene_id,
                    content=final_generation.content,
                    source_bundle_id=bundle["bundle_id"],
                )
                state.scene_status = "quality_warning_pending_acceptance"
                self.session.flush()
                result = self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": bundle["bundle_id"],
                        "current_bundle_hash": bundle["bundle_snapshot_hash"],
                        "current_qc_report_id": state.current_qc_report_id,
                        "current_human_review_event_id": state.current_human_review_event_id,
                        "hard_qc": hard_qc_payload,
                        "soft_qc": qc_decision_payload(soft_qc),
                        "planning": planning,
                        "near_final": near_final_payload,
                        "run_policy": run_policy,
                    },
                )
                result["quality_warnings"] = merged_warnings(
                    result.get("quality_warnings"), near_final_warnings
                )
                apply_finality(
                    result, gate_summary=strict_gate, warnings=strict_warnings
                )
                return result

        # Wave 3：旧的 near-final 后置 critical_scene_human_gate 被前移的候选终选
        # gate 取代（§5.5 顺序——终选在批判修订/硬检查之前，此处不再二次人工门）。

        final_row_id = versioned_scene_artifact_id("final_scene", scene_id, bundle)
        soft_risk_event_id = soft_risk_acceptance_event_id(soft_qc)
        carry_notes_json = (
            self._carry_notes_from_report(soft_qc.qc_report_id)
            if soft_qc.branch == "waive"
            else []
        )
        if soft_risk_event_id:
            carry_notes_json.append(
                {
                    "kind": "soft_risk_acceptance",
                    "human_review_event_id": soft_risk_event_id,
                    "qc_report_id": soft_qc.qc_report_id,
                }
            )
        if getattr(soft_qc, "stop_reason", None) == STYLE_PATCH_REVERTED_STOP_REASON:
            # 风格参考 v3（P5b）：补丁让稿子离作者更远被退回——评审的改稿意见随稿留痕（作者可自己改）
            carry_notes_json.append(
                {
                    "kind": "style_patch_reverted",
                    "qc_report_id": soft_qc.qc_report_id,
                    "rewrite_brief": self._rewrite_brief_from_report(soft_qc.qc_report_id)[:8],
                    "recommended_action": "author_review_optional_fix",
                }
            )
        if not near_final.get("pass_flag"):
            # Wave 2：达修订上限仍未过的 near-final 意见随稿归档留痕（作者行动建议）
            carry_notes_json.append(
                {
                    "kind": "near_final_unresolved",
                    "failure_class": near_final.get("failure_class"),
                    "rewrite_count": rewrite_count,
                    "recommended_action": "author_review_optional_fix",
                }
            )
        self.session.add(
            FinalScene(
                row_id=final_row_id,
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                content=final_generation.content,
                status="near_final_ready",
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=final_generation.llm_call_id,
            )
        )
        self.session.flush()
        state.current_final_scene_row_id = final_row_id
        finalize_details = {
            "source_style_draft_row_id": final_generation.row_id,
            "source_qc_report_id": soft_qc.qc_report_id,
            "final_generation_llm_call_id": final_generation.llm_call_id,
        }
        if soft_risk_event_id:
            finalize_details["soft_risk_acceptance_event_id"] = soft_risk_event_id
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                step="finalize",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=finalize_details,
            )
        )
        self.session.flush()

        near_final_evaluation_step_key = f"near_final_acceptance:{rewrite_count}"
        near_completion = {
            "final_evaluation_round": rewrite_count,
            "rewrite_count": rewrite_count,
            "skip_reason": near_final_skip_reason,
            "branch": near_final.get("near_final_status"),
            "evaluation_id": near_final.get("evaluation_id"),
            "source_draft_row_id": final_generation.row_id,
            "final_scene_row_id": final_row_id,
        }
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=3,
            artifact_refs={
                "final_scene_row_id": final_row_id,
                "near_final_source_draft_row_id": final_generation.row_id,
                "final_generation_llm_call_id": final_generation.llm_call_id,
                "final_generation_execution_step_key": final_generation.execution_step_key,
                "final_generation_artifact_execution_id": (
                    final_generation.artifact_execution_id or self._execution_id
                ),
                "near_final_evaluation_id": near_final.get("evaluation_id"),
                "near_final_evaluation_llm_call_id": self._writer_evaluation_llm_call_id(
                    near_final.get("evaluation_id")
                ),
                "near_final_evaluation_step_key": near_final_evaluation_step_key,
                "near_final_evaluation_execution_id": self._execution_id,
                "near_final": near_final_payload,
                "carry_notes": carry_notes_json,
                "soft_qc_report_id": soft_qc.qc_report_id,
                "near_final_branch": near_final.get("near_final_status"),
                "near_final_skip_reason": near_final_skip_reason,
                "near_final_rewrite_count": rewrite_count,
                "near_completion": near_completion,
            },
            artifact_hashes={
                "final_scene": self._text_hash(final_generation.content),
                "near_final": self._json_hash(near_final_payload),
                "carry_notes": self._json_hash(carry_notes_json),
                "near_completion": self._json_hash(near_completion),
            },
            branch=str(near_final.get("near_final_status") or "near_final_ready"),
        )

        return self._archive_near_final_checkpoint(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            hard_qc_payload=hard_qc_payload,
            planning=planning,
            candidate_summaries=candidate_summaries,
            run_policy=run_policy,
        )

    def _validate_budget_checkpoint(self, state: SceneRunState) -> None:
        try:
            state = scene_budget.ensure_scene_budget_initialized(self.session, state.scene_id)
        except ValueError as exc:
            raise checkpoint_corrupt(
                "budget state cannot be reconstructed from its immutable basis and topup audit",
            ) from exc
        expected_budget = self._checkpoint_artifact(
            "scene_token_budget",
            expected_node_at_least="budget_ready",
        )
        values = {
            "scene_token_budget": state.scene_token_budget,
            "scene_tokens_used": state.scene_tokens_used,
            "scene_tokens_reserved": state.scene_tokens_reserved,
            "attempt_budget": state.attempt_budget,
            "total_attempt_count": state.total_attempt_count,
            "provider_attempt_budget": state.provider_attempt_budget,
            "provider_attempts_used": state.provider_attempts_used,
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values.values()
        ):
            raise checkpoint_corrupt("budget checkpoint counters are invalid")
        budget_prefixes = scene_budget.audited_scene_budget_prefixes(self.session, state)
        if (
            expected_budget not in budget_prefixes
            or state.scene_tokens_used + state.scene_tokens_reserved
            > state.scene_token_budget
            or state.total_attempt_count > state.attempt_budget
            or state.provider_attempts_used > state.provider_attempt_budget
            or not isinstance(state.scene_budget_basis_json, dict)
            or self._json_hash(state.scene_budget_basis_json)
            != self._checkpoint_hash("budget_basis")
        ):
            raise checkpoint_corrupt("budget state differs from its durable checkpoint")

    def _writer_evaluation_llm_call_id(self, evaluation_id: Any) -> str | None:
        row = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        return row.evaluator_llm_call_id if row is not None else None

    def _capture_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        self.session.flush()
        calls = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == execution_id,
                )
            )
            .scalars()
            .all()
        )
        call_snapshots = [
            {
                column.name: deepcopy(getattr(call, column.name))
                for column in LlmCall.__table__.columns
            }
            for call in calls
        ]
        call_ids = {call.llm_call_id for call in calls}
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.status == "failed",
                )
            )
            .scalars()
            .all()
        )
        attempt_snapshots: list[dict[str, Any]] = []
        for attempt in attempts:
            details = dict(attempt.details_json or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in call_ids:
                continue
            attempt_snapshots.append(
                {
                    "scene_id": attempt.scene_id,
                    "chapter_id": attempt.chapter_id,
                    "step": attempt.step,
                    "status": attempt.status,
                    "source_bundle_id": attempt.source_bundle_id,
                    "details_json": deepcopy(details),
                    "created_at": attempt.created_at,
                }
            )
        return {"calls": call_snapshots, "attempts": attempt_snapshots}

    def _persist_failure_audits_or_fence(
        self,
        scene_id: str,
        execution_id: str,
        checkpoints: SceneRunCheckpointService,
    ) -> None:
        try:
            snapshots = self._capture_failure_audits(scene_id, execution_id)
        except Exception as exc:
            _LOGGER.exception(
                "failed to snapshot failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="snapshot",
                cause=exc,
            )
            return

        self.session.rollback()
        try:
            self._restore_failure_audits(scene_id, execution_id, snapshots)
            checkpoints.mark_failed(scene_id, execution_id)
            self.session.commit()
        except Exception as exc:
            _LOGGER.exception(
                "failed to restore failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="restore",
                cause=exc,
            )

    def _persist_unrecoverable_execution_fence(
        self,
        scene_id: str,
        execution_id: str,
        *,
        phase: str,
        cause: Exception,
    ) -> None:
        """Best-effort terminal fence when failure-audit durability is unknown."""
        try:
            state = self.session.get(SceneRunState, scene_id)
            if state is None:
                return
            self.session.refresh(state)
            if state.active_execution_id != execution_id:
                return
            if (
                state.run_execution_status == "cancelled"
                and state.run_checkpoint == "cancelled"
            ):
                return
            payload = (
                dict(state.run_checkpoint_json)
                if isinstance(state.run_checkpoint_json, dict)
                else {}
            )
            artifact_refs = payload.get("artifact_refs")
            artifact_hashes = payload.get("artifact_hashes")
            cancelled_payload = {
                **payload,
                "execution_id": execution_id,
                "node_key": "cancelled",
                "artifact_refs": (
                    artifact_refs if isinstance(artifact_refs, dict) else {}
                ),
                "artifact_hashes": (
                    artifact_hashes if isinstance(artifact_hashes, dict) else {}
                ),
                "cancelled_from_node": state.run_checkpoint,
                "cancelled_at": utcnow(),
                "unrecoverable_failure_audit": {
                    "phase": phase,
                    "error_type": type(cause).__name__,
                },
            }
            fenced = self.session.execute(
                update(SceneRunState)
                .where(
                    SceneRunState.scene_id == scene_id,
                    SceneRunState.active_execution_id == execution_id,
                    SceneRunState.run_execution_status.in_(("active", "failed")),
                )
                .values(
                    run_execution_status="cancelled",
                    run_checkpoint="cancelled",
                    run_checkpoint_json=cancelled_payload,
                )
                .execution_options(synchronize_session=False)
            )
            if fenced.rowcount != 1:
                self.session.rollback()
                return
            self.session.commit()
        except Exception:
            self.session.rollback()
            _LOGGER.exception(
                "failed to persist unrecoverable execution fence for scene %s execution %s",
                scene_id,
                execution_id,
            )

    def _restore_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
        snapshots: dict[str, list[dict[str, Any]]],
    ) -> None:
        state = self.session.get(SceneRunState, scene_id)
        terminal_accounting_statuses = {
            "settled",
            "failed",
            "usage_exceeds_reservation",
        }
        restored_call_ids: set[str] = set()
        for call_snapshot in snapshots.get("calls", []):
            call_data = dict(call_snapshot)
            if (
                call_data.get("execution_id") != execution_id
                or call_data.get("scene_id") != scene_id
            ):
                continue
            llm_call_id = call_data["llm_call_id"]
            existing = self.session.get(LlmCall, llm_call_id)
            existing_status = (
                existing.accounting_status if existing is not None else None
            )
            existing_reserved = (
                int(existing.reserved_tokens or 0) if existing is not None else 0
            )
            existing_total = (
                int(existing.total_tokens or 0) if existing is not None else 0
            )
            snapshot_status = call_data.get("accounting_status")
            snapshot_total = int(call_data.get("total_tokens") or 0)
            if existing is None:
                self.session.add(
                    LlmCall(
                        scope_type=call_data.pop("scope_type", None),
                        scope_id=call_data.pop("scope_id", None),
                        **call_data,
                    )
                )
            else:
                for column in LlmCall.__table__.columns:
                    if column.primary_key:
                        continue
                    setattr(existing, column.name, deepcopy(call_data[column.name]))
            restored_call_ids.add(llm_call_id)

            if state is None or state.active_execution_id != execution_id:
                continue
            if snapshot_status in terminal_accounting_statuses:
                if existing_status == "reserved":
                    state.scene_tokens_reserved = max(
                        0,
                        int(state.scene_tokens_reserved or 0) - existing_reserved,
                    )
                if existing_status not in terminal_accounting_statuses:
                    state.scene_tokens_used = (
                        int(state.scene_tokens_used or 0) + snapshot_total
                    )
                elif snapshot_total != existing_total:
                    state.scene_tokens_used = max(
                        0,
                        int(state.scene_tokens_used or 0)
                        + snapshot_total
                        - existing_total,
                    )

        restored = 0
        restored_business_attempts = 0
        for attempt_snapshot in snapshots.get("attempts", []):
            attempt_data = dict(attempt_snapshot)
            details = dict(attempt_data.get("details_json") or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in restored_call_ids:
                continue
            existing_attempts = (
                self.session.execute(
                    select(AttemptTracker).where(
                        AttemptTracker.scene_id == scene_id,
                        AttemptTracker.step == attempt_data["step"],
                        AttemptTracker.status == "failed",
                    )
                )
                .scalars()
                .all()
            )
            if any(
                (attempt.details_json or {}).get("llm_call_id") == llm_call_id
                for attempt in existing_attempts
            ):
                continue
            self.session.add(AttemptTracker(**attempt_data))
            restored += 1
            if details.get("business_attempt_consumed", True) is not False:
                restored_business_attempts += 1
        if restored_business_attempts:
            if state is not None and state.active_execution_id == execution_id:
                state.total_attempt_count = (
                    int(state.total_attempt_count or 0) + restored_business_attempts
                )
        self.session.flush()

    def _near_final_rewrite_guard(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult,
        rewrite_generation: StyleGenerationResult,
        gate: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """定稿改写稿进 eval1 之前的确定性门（见 ``NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON`` 处的说明）。

        返回要存进检查点的 gate 小结：已被抄袭门拒绝的原样返回；新添基础安全回退 → ``rejected_reason="base_safety"``；
        作者手笔直起、读数可信、且重写稿的 distance 比来源稿大出 ``patch_max_distance_increase`` 以上 →
        ``rejected_reason="moved_away"``；都没有 → 原来的 gate（重写稿更像或差不多时把读数记进 ``fidelity``）。"""
        if isinstance(gate, dict) and gate.get("rejected"):
            return gate
        regressions = assess_rewrite_regressions(
            scene,
            bundle,
            source_content=source_generation.content,
            rewritten_content=rewrite_generation.content,
        )
        if regressions["regressed"]:
            return self._rejected_rewrite_gate(
                gate,
                reason="base_safety",
                details={
                    "reasons": list(regressions["reasons"]),
                    "integrity_markers": list(regressions["rewritten_integrity_markers"]),
                },
            )
        drift = self._near_final_rewrite_drift(
            scene=scene,
            bundle=bundle,
            source_generation=source_generation,
            rewrite_generation=rewrite_generation,
        )
        if drift is None:
            return gate
        if drift["moved_away"]:
            return self._rejected_rewrite_gate(gate, reason="moved_away", details=drift)
        return {**gate, "fidelity": drift} if isinstance(gate, dict) else gate

    @staticmethod
    def _rejected_rewrite_gate(
        gate: dict[str, Any] | None, *, reason: str, details: dict[str, Any]
    ) -> dict[str, Any]:
        base = (
            dict(gate)
            if isinstance(gate, dict)
            else {
                "stage": NEAR_FINAL_REWRITE_GATE_STAGE,
                "verdict": "not_run",
                "plagiarism_hit_count": None,
                "forbidden_hit_count": None,
                "error": None,
                "profile_id": None,
                "runtime_contract_hash": None,
                "notice_codes": [],
            }
        )
        base.update(rejected=True, rejected_reason=reason, rejection=deepcopy(details))
        return base

    def _near_final_rewrite_drift(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult,
        rewrite_generation: StyleGenerationResult,
    ) -> dict[str, Any] | None:
        """作者手笔直起时重写稿相对来源稿的读数比较；不适用 / 读不出 / 读数不可信 → ``None``（不拿它拒稿）。"""
        policy = style_policy_for_bundle(bundle)
        if not policy.bound or not policy.style_first:
            return None
        thresholds = fidelity_thresholds()
        try:
            # 读数可能要先建这本书的窗口索引、写库：放在自己的保存点里，失败只回滚它
            with self.session.begin_nested():
                before = style_readings.reading_for_text(self.session, policy, source_generation.content)
                after = style_readings.reading_for_text(self.session, policy, rewrite_generation.content)
        except Exception:  # noqa: BLE001 — 读数是观察：读不出就不拿它拒稿
            _LOGGER.warning("near-final rewrite fidelity reading failed for scene %s", scene.scene_id, exc_info=True)
            return None
        if before is None or after is None or not before.reliable or not after.reliable:
            return None
        tolerance = float(thresholds.patch_max_distance_increase)
        return {
            "moved_away": bool(after.distance > before.distance + tolerance),
            "source_distance": round(float(before.distance), 4),
            "rewrite_distance": round(float(after.distance), 4),
            "source_percentile": round(float(before.percentile), 1) if before.percentile is not None else None,
            "rewrite_percentile": round(float(after.percentile), 1) if after.percentile is not None else None,
            "tolerance": tolerance,
        }

    def _ensure_near_final_subcheckpoints(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult,
        optional_spend_allowed,
    ) -> tuple[
        dict[str, Any],
        StyleGenerationResult,
        int,
        str | None,
        dict[str, Any] | None,
    ]:
        """Returns ``(near_final, final_generation, rewrite_count, skip_reason, rewrite_gate)``.

        ``rewrite_gate`` is the near_final_rewrite styled-draft gate summary (``None`` when no
        rewrite was produced or the scene has no style binding).
        """
        progress = self._near_final_checkpoint_progress()
        if progress >= 3:
            # 调用方 _finalize_after_style 在准终稿子游标 ≥3 时已直接去归档；这之间只写 soft_qc_ready，推不动这个游标。
            raise checkpoint_corrupt("near-final completion appeared while the soft QC phase was running")

        if progress < 0:
            self._reconcile_execution_step("near_final_acceptance:0")
            eval0 = self.near_final_service.evaluate_scene(
                scene.scene_id,
                bundle=bundle,
                source_draft_row_id=source_generation.row_id,
                source_content=source_generation.content,
                execution_step_key="near_final_acceptance:0",
            )
            rewrite_requested = not bool(eval0.get("pass_flag")) and bool(
                eval0.get("should_rewrite")
            )
            rewrite_allowed = rewrite_requested and optional_spend_allowed()
            if rewrite_requested and not rewrite_allowed:
                skip_reason = "budget_or_candidate_cap"
                branch = "rewrite_skipped"
                _LOGGER.warning(
                    "near-final rewrite skipped for scene %s (budget/candidate cap)",
                    scene.scene_id,
                )
            elif eval0.get("requires_human_review"):
                skip_reason = "human_review_proposal"
                branch = "human_review_proposal"
            elif eval0.get("pass_flag"):
                skip_reason = "no_rewrite_requested"
                branch = "pass"
            elif not rewrite_requested:
                skip_reason = "not_auto_rewrite_eligible"
                branch = "unresolved"
            else:
                skip_reason = None
                branch = "rewrite"
            control = {
                "branch": branch,
                "rewrite_requested": rewrite_requested,
                "rewrite_allowed": rewrite_allowed,
                "skip_reason": skip_reason,
            }
            self._save_near_evaluation_checkpoint(
                sub_index=0,
                round_index=0,
                result=eval0,
                source_generation=source_generation,
                bundle=bundle,
                control=control,
            )
            progress = 0
        else:
            eval0 = self._load_near_evaluation_checkpoint(
                scene_id=scene.scene_id,
                round_index=0,
                source_generation=source_generation,
            )
            control = self._load_near_eval0_control(eval0)

        if bool(control["rewrite_allowed"]):
            if progress < 1:
                self._reconcile_execution_step("near_final_rewrite:0")
                rewrite_generation = (
                    self.scene_generation_service.generate_near_final_rewrite(
                        scene.scene_id,
                        bundle,
                        source_draft_row_id=source_generation.row_id,
                        source_content=source_generation.content,
                        revision_brief=self._near_final_rewrite_brief(eval0),
                        source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                        execution_step_key="near_final_rewrite:0",
                    )
                )
                rewrite_gate = _near_final_rewrite_gate_summary(rewrite_generation)
                rewrite_gate = self._near_final_rewrite_guard(
                    scene=scene,
                    bundle=bundle,
                    source_generation=source_generation,
                    rewrite_generation=rewrite_generation,
                    gate=rewrite_gate,
                )
                self._save_near_rewrite_checkpoint(
                    generation=rewrite_generation,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                    bundle=bundle,
                    styled_gate=rewrite_gate,
                )
                progress = 1
            else:
                rewrite_generation = self._load_near_rewrite_checkpoint(
                    scene_id=scene.scene_id,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                )
                rewrite_gate = self._load_near_rewrite_gate_checkpoint()
            if rewrite_gate is not None and rewrite_gate.get("rejected"):
                # v2（W5）：重写稿 styled-draft gate 判定确定性抄袭（Q0，不开放软风险
                # 接受）。重写稿永远不能成为终稿——不再对它做 near-final 评审，回退到
                # 重写前、已过 soft_qc gate 的来源稿；skip_reason 与 Q2 警告让作者看见。
                if progress >= 2:
                    raise checkpoint_corrupt("near-final evaluation exists for a gate-rejected rewrite")
                rejection_skip_reason = _near_final_rejection_skip_reason(rewrite_gate)
                _LOGGER.warning(
                    "near-final rewrite for scene %s rejected (%s); falling back to the gated source draft %s",
                    scene.scene_id,
                    rejection_skip_reason,
                    source_generation.row_id,
                )
                # 生成重写稿时 scene_generation 已把当前稿指针推到被拒的重写行;回退后
                # 指针必须指回来源稿,否则 adopt-current(尚无 FinalScene 时按
                # latest_valid_draft_row_id 取稿)会把抄袭稿当成当前稿采纳。
                state = self.session.get(SceneRunState, scene.scene_id)
                if state is not None:
                    state.current_style_draft_row_id = source_generation.row_id
                    state.latest_valid_draft_row_id = source_generation.row_id
                    self.session.flush()
                return (
                    eval0,
                    source_generation,
                    0,
                    rejection_skip_reason,
                    rewrite_gate,
                )
            if progress < 2:
                self._reconcile_execution_step("near_final_acceptance:1")
                eval1 = self.near_final_service.evaluate_scene(
                    scene.scene_id,
                    bundle=bundle,
                    source_draft_row_id=rewrite_generation.row_id,
                    source_content=rewrite_generation.content,
                    execution_step_key="near_final_acceptance:1",
                )
                self._save_near_evaluation_checkpoint(
                    sub_index=2,
                    round_index=1,
                    result=eval1,
                    source_generation=rewrite_generation,
                    bundle=bundle,
                )
            else:
                eval1 = self._load_near_evaluation_checkpoint(
                    scene_id=scene.scene_id,
                    round_index=1,
                    source_generation=rewrite_generation,
                )
            return eval1, rewrite_generation, 1, None, rewrite_gate

        if progress >= 1:
            raise checkpoint_corrupt("near-final rewrite checkpoint exists for a non-rewrite eval0 branch")
        return eval0, source_generation, 0, control.get("skip_reason"), None

    def _near_candidate_refs_and_hashes(
        self,
        *,
        prefix: str,
        evaluation_id: str,
        candidate_id: Any,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        rows = (
            self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.evaluation_id == evaluation_id
                )
            )
            .scalars()
            .all()
        )
        if candidate_id is None:
            if rows:
                raise checkpoint_corrupt("passing near-final evaluation has an unexpected revision candidate")
            snapshot = None
        else:
            candidate = self._require_checkpoint_row(RevisionCandidate, candidate_id)
            if len(rows) != 1 or rows[0].revision_id != candidate_id:
                raise checkpoint_corrupt("near-final evaluation candidate reference is not unique")
            snapshot = revision_candidate_snapshot(candidate)
        return (
            {
                f"{prefix}_revision_candidate_id": candidate_id,
                f"{prefix}_candidate_snapshot": snapshot,
            },
            {f"{prefix}_candidate": self._json_hash(snapshot)},
        )

    def _save_near_evaluation_checkpoint(
        self,
        *,
        sub_index: int,
        round_index: int,
        result: dict[str, Any],
        source_generation: StyleGenerationResult,
        bundle: dict[str, Any],
        control: dict[str, Any] | None = None,
    ) -> None:
        self.session.flush()
        prefix = f"near_eval{round_index}"
        evaluation_id = result.get("evaluation_id")
        evaluation = self._require_checkpoint_row(WriterEvaluation, evaluation_id)
        normalized = near_evaluation_payload(result)
        candidate_refs, candidate_hashes = self._near_candidate_refs_and_hashes(
            prefix=prefix,
            evaluation_id=evaluation.evaluation_id,
            candidate_id=result.get("revision_candidate_id"),
        )
        refs: dict[str, Any] = {
            f"{prefix}_evaluation_id": evaluation.evaluation_id,
            f"{prefix}_payload": normalized,
            f"{prefix}_source_draft_row_id": source_generation.row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
            f"{prefix}_llm_call_id": evaluation.evaluator_llm_call_id,
            f"{prefix}_execution_step_key": f"near_final_acceptance:{round_index}",
            f"{prefix}_artifact_execution_id": self._execution_id,
            **candidate_refs,
        }
        hashes = {
            f"{prefix}_payload": self._json_hash(normalized),
            f"{prefix}_evaluation": self._json_hash(
                writer_evaluation_snapshot(evaluation)
            ),
            **candidate_hashes,
        }
        if round_index == 0 and control is not None:
            refs["near_eval0_control"] = deepcopy(control)
            hashes["near_eval0_control"] = self._json_hash(control)
        if round_index == 1:
            state_refs = (
                self._active_checkpoint_state().run_checkpoint_json or {}
            ).get("artifact_refs") or {}
            eval0_id = state_refs.get("near_eval0_evaluation_id")
            eval0_candidate_id = state_refs.get("near_eval0_revision_candidate_id")
            refreshed_refs, refreshed_hashes = self._near_candidate_refs_and_hashes(
                prefix="near_eval0",
                evaluation_id=str(eval0_id or ""),
                candidate_id=eval0_candidate_id,
            )
            refs.update(refreshed_refs)
            hashes.update(refreshed_hashes)
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=sub_index,
            artifact_refs=refs,
            artifact_hashes=hashes,
            branch=str(result.get("near_final_status") or "near_final_ready"),
        )

    def _load_near_evaluation_checkpoint(
        self,
        *,
        scene_id: str,
        round_index: int,
        source_generation: StyleGenerationResult,
    ) -> dict[str, Any]:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        prefix = f"near_eval{round_index}"
        evaluation_id = refs.get(f"{prefix}_evaluation_id")
        evaluation = self._require_checkpoint_row(WriterEvaluation, evaluation_id)
        normalized = refs.get(f"{prefix}_payload")
        if (
            not isinstance(normalized, dict)
            or self._json_hash(normalized) != self._checkpoint_hash(f"{prefix}_payload")
            or self._json_hash(writer_evaluation_snapshot(evaluation))
            != self._checkpoint_hash(f"{prefix}_evaluation")
        ):
            raise checkpoint_corrupt(f"near-final evaluation {round_index} payload/content hash mismatch")
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get(f"{prefix}_llm_call_id")
        step_key = refs.get(f"{prefix}_execution_step_key")
        owner = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
            execution_id=owner,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        if (
            evaluation.object_type != "scene"
            or evaluation.object_id != scene_id
            or evaluation.scene_id != scene_id
            or evaluation.rubric_id != "near_final_acceptance_v1"
            or evaluation.source_text_ref != f"source_draft:{source_generation.row_id}"
            or evaluation.source_bundle_id != bundle["bundle_id"]
            or evaluation.evaluator_llm_call_id != llm_call_id
            or refs.get(f"{prefix}_source_draft_row_id") != source_generation.row_id
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or step_key != f"near_final_acceptance:{round_index}"
            or normalized.get("evaluation_id") != evaluation.evaluation_id
            or normalized.get("overall_score") != evaluation.overall_score
            or normalized.get("scores") != (evaluation.scores_json or {})
            or normalized.get("findings") != (evaluation.findings_json or [])
            or normalized.get("failure_class") != evaluation.failure_class
            or normalized.get("should_rewrite")
            != bool(evaluation.auto_rewrite_eligible)
            or normalized.get("revision_brief")
            != (evaluation.revision_brief_json or [])
            or normalized.get("requires_human_review")
            != bool(evaluation.requires_human_review)
            or normalized.get("pass_flag")
            != (normalized.get("near_final_status") == "near_final_ready")
            or normalized.get("requires_human_review")
            != (normalized.get("near_final_status") == "human_review_required")
            or (
                normalized.get("pass_flag")
                and (
                    normalized.get("failure_class") is not None
                    or normalized.get("revision_candidate_id") is not None
                )
            )
            or (
                not normalized.get("pass_flag")
                and not isinstance(normalized.get("revision_candidate_id"), str)
            )
        ):
            raise checkpoint_corrupt(f"near-final evaluation {round_index} identity/source mismatch")
        candidate_id = refs.get(f"{prefix}_revision_candidate_id")
        expected_candidate_id = normalized.get("revision_candidate_id")
        candidate_snapshot = refs.get(f"{prefix}_candidate_snapshot")
        candidates = (
            self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.evaluation_id == evaluation.evaluation_id
                )
            )
            .scalars()
            .all()
        )
        if expected_candidate_id is None:
            if candidate_id is not None or candidate_snapshot is not None or candidates:
                raise checkpoint_corrupt(
                    f"near-final evaluation {round_index} has a forged candidate reference",
                )
        else:
            candidate = self._require_checkpoint_row(RevisionCandidate, candidate_id)
            if (
                candidate_id != expected_candidate_id
                or len(candidates) != 1
                or candidates[0].revision_id != candidate_id
                or candidate.evaluation_id != evaluation.evaluation_id
                or candidate.object_type != "scene"
                or candidate.object_id != scene_id
                or candidate.scene_id != scene_id
                or candidate.revision_type != "near_final_scene_rewrite"
                or candidate.source_text_ref
                != f"source_draft:{source_generation.row_id}"
                or candidate.proposed_text != source_generation.content
                or candidate.status not in {"candidate", "superseded"}
                or revision_candidate_snapshot(candidate) != candidate_snapshot
            ):
                raise checkpoint_corrupt(f"near-final evaluation {round_index} candidate is misbound")
        if self._json_hash(candidate_snapshot) != self._checkpoint_hash(
            f"{prefix}_candidate"
        ):
            raise checkpoint_corrupt(f"near-final evaluation {round_index} candidate hash mismatch")
        self._validate_near_final_attempt(
            scene_id=scene_id,
            evaluation_id=evaluation.evaluation_id,
            candidate_id=expected_candidate_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_generation.row_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
        )
        return deepcopy(normalized)

    def _validate_near_final_attempt(
        self,
        *,
        scene_id: str,
        evaluation_id: str,
        candidate_id: str | None,
        source_bundle_id: str,
        source_draft_row_id: str,
        llm_call_id: str,
        execution_step_key: str,
    ) -> None:
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == "near_final_acceptance_review",
                    AttemptTracker.source_bundle_id == source_bundle_id,
                )
            )
            .scalars()
            .all()
        )
        matched = []
        for attempt in attempts:
            details = attempt.details_json or {}
            if (
                details.get("evaluation_id") == evaluation_id
                and details.get("revision_candidate_id") == candidate_id
                and details.get("source_draft_row_id") == source_draft_row_id
                and details.get("llm_call_id") == llm_call_id
                and details.get("execution_step_key") == execution_step_key
            ):
                matched.append(attempt)
        if len(matched) != 1:
            raise checkpoint_corrupt(
                "near-final checkpoint has no unique matching attempt audit row",
                details={
                    "evaluation_id": evaluation_id,
                    "matching_attempts": len(matched),
                },
            )

    def _load_near_eval0_control(self, eval0: dict[str, Any]) -> dict[str, Any]:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs"
        ) or {}
        control = refs.get("near_eval0_control")
        rewrite_requested = not bool(eval0.get("pass_flag")) and bool(
            eval0.get("should_rewrite")
        )
        if (
            not isinstance(control, dict)
            or not isinstance(control.get("branch"), str)
            or not isinstance(control.get("rewrite_requested"), bool)
            or not isinstance(control.get("rewrite_allowed"), bool)
            or self._json_hash(control) != self._checkpoint_hash("near_eval0_control")
            or control["rewrite_requested"] != rewrite_requested
            or (control["rewrite_allowed"] and not control["rewrite_requested"])
            or (control["rewrite_allowed"] and control.get("skip_reason") is not None)
            or (
                control.get("skip_reason") is not None
                and not isinstance(control.get("skip_reason"), str)
            )
        ):
            raise checkpoint_corrupt("near-final eval0 branch control is invalid")
        expected_branch: str
        expected_skip_reason: str | None
        if eval0.get("requires_human_review"):
            expected_branch, expected_skip_reason = (
                "human_review_proposal",
                "human_review_proposal",
            )
        elif eval0.get("pass_flag"):
            expected_branch, expected_skip_reason = "pass", "no_rewrite_requested"
        elif rewrite_requested and control["rewrite_allowed"]:
            expected_branch, expected_skip_reason = "rewrite", None
        elif rewrite_requested:
            expected_branch, expected_skip_reason = (
                "rewrite_skipped",
                "budget_or_candidate_cap",
            )
        else:
            expected_branch, expected_skip_reason = (
                "unresolved",
                "not_auto_rewrite_eligible",
            )
        if (
            control.get("branch") != expected_branch
            or control.get("skip_reason") != expected_skip_reason
        ):
            raise checkpoint_corrupt("near-final eval0 branch/skip reason is inconsistent")
        return deepcopy(control)

    def _save_near_rewrite_checkpoint(
        self,
        *,
        generation: StyleGenerationResult,
        source_generation: StyleGenerationResult,
        source_evaluation_id: str,
        bundle: dict[str, Any],
        styled_gate: dict[str, Any] | None = None,
    ) -> None:
        refs: dict[str, Any] = {
            "near_rewrite_draft_row_id": generation.row_id,
            "near_rewrite_llm_call_id": generation.llm_call_id,
            "near_rewrite_execution_step_key": generation.execution_step_key,
            "near_rewrite_artifact_execution_id": generation.artifact_execution_id
            or self._execution_id,
            "near_rewrite_source_draft_row_id": source_generation.row_id,
            "near_rewrite_source_evaluation_id": source_evaluation_id,
            "near_rewrite_bundle_id": bundle["bundle_id"],
            "near_rewrite_bundle_hash": bundle["bundle_snapshot_hash"],
        }
        hashes = {"near_rewrite_draft": self._text_hash(generation.content)}
        if styled_gate is not None:
            # 重写稿 gate 小结随检查点冻结：恢复 / 回放时据此重建「重写被拒 → 来源稿成为
            # 终稿」的分支，而不是重新信任一份已判定抄袭的重写稿。
            refs["near_rewrite_styled_gate"] = deepcopy(styled_gate)
            hashes["near_rewrite_styled_gate"] = self._json_hash(styled_gate)
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=1,
            artifact_refs=refs,
            artifact_hashes=hashes,
            branch="rewrite",
        )

    def _load_near_rewrite_gate_checkpoint(self) -> dict[str, Any] | None:
        """重写稿 styled-draft gate 小结（v2 之前的检查点没有该键 → ``None``）。"""
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs"
        ) or {}
        gate = refs.get("near_rewrite_styled_gate")
        expected_hash = self._checkpoint_hash("near_rewrite_styled_gate")
        if gate is None and expected_hash is None:
            return None
        if (
            not isinstance(gate, dict)
            or not isinstance(gate.get("rejected"), bool)
            or self._json_hash(gate) != expected_hash
        ):
            raise checkpoint_corrupt("near-final rewrite styled-draft gate checkpoint hash mismatch")
        return deepcopy(gate)

    def _load_near_rewrite_checkpoint(
        self,
        *,
        scene_id: str,
        source_generation: StyleGenerationResult,
        source_evaluation_id: str,
    ) -> StyleGenerationResult:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs"
        ) or {}
        row_id = refs.get("near_rewrite_draft_row_id")
        draft = self._require_checkpoint_row(SceneDraft, row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get("near_rewrite_llm_call_id")
        step_key = refs.get("near_rewrite_execution_step_key")
        owner = self._validate_artifact_execution_owner(
            refs.get("near_rewrite_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
            execution_id=owner,
        )
        if (
            draft.scene_id != scene_id
            or draft.stage != "near_final_rewrite"
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != llm_call_id
            or refs.get("near_rewrite_source_draft_row_id") != source_generation.row_id
            or refs.get("near_rewrite_source_evaluation_id") != source_evaluation_id
            or refs.get("near_rewrite_bundle_id") != bundle["bundle_id"]
            or refs.get("near_rewrite_bundle_hash") != bundle["bundle_snapshot_hash"]
            or step_key != "near_final_rewrite:0"
            or self._text_hash(draft.content)
            != self._checkpoint_hash("near_rewrite_draft")
        ):
            raise checkpoint_corrupt("near-final rewrite checkpoint identity/source/hash mismatch")
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == "scene_literary_rewrite",
                    AttemptTracker.source_bundle_id == bundle["bundle_id"],
                )
            )
            .scalars()
            .all()
        )
        matched = [
            attempt
            for attempt in attempts
            if (attempt.details_json or {}).get("row_id") == draft.row_id
            and (attempt.details_json or {}).get("llm_call_id") == llm_call_id
            and (attempt.details_json or {}).get("source_draft_row_id")
            == source_generation.row_id
            and (attempt.details_json or {}).get("source_evaluation_id")
            == source_evaluation_id
        ]
        if len(matched) != 1:
            raise checkpoint_corrupt("near-final rewrite checkpoint has no unique matching attempt audit row")
        return StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=step_key,
            artifact_execution_id=owner,
        )

    def _load_near_final_checkpoint(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult | None = None,
    ) -> tuple[FinalScene, dict[str, Any]]:
        scene_id = scene.scene_id
        final_row_id = self._checkpoint_artifact(
            "final_scene_row_id",
            expected_node_at_least="near_final_ready",
        )
        final_scene = self._require_checkpoint_row(FinalScene, final_row_id)
        state_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = state_payload.get("artifact_refs") or {}
        generation_call_id = refs.get("final_generation_llm_call_id")
        generation_step_key = refs.get("final_generation_execution_step_key")
        generation_execution_id = self._validate_artifact_execution_owner(
            refs.get("final_generation_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=generation_call_id,
            execution_step_key=generation_step_key,
            execution_id=generation_execution_id,
        )
        if (
            final_scene.scene_id != scene_id
            or final_scene.chapter_id != scene.chapter_id
            or final_scene.source_bundle_id != bundle["bundle_id"]
            or final_scene.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or final_scene.generation_llm_call_id != generation_call_id
            or self._text_hash(final_scene.content)
            != self._checkpoint_hash("final_scene")
        ):
            raise checkpoint_corrupt("near-final checkpoint identity/hash mismatch")

        near_final_payload = refs.get("near_final")
        if not isinstance(near_final_payload, dict) or self._json_hash(
            near_final_payload
        ) != self._checkpoint_hash("near_final"):
            raise checkpoint_corrupt("near-final payload hash mismatch")
        carry_notes = refs.get("carry_notes")
        if not isinstance(carry_notes, list) or self._json_hash(
            carry_notes
        ) != self._checkpoint_hash("carry_notes"):
            raise checkpoint_corrupt("near-final carry notes hash mismatch")
        evaluation_id = refs.get("near_final_evaluation_id")
        evaluation = self._require_checkpoint_row(WriterEvaluation, evaluation_id)
        evaluation_call_id = refs.get("near_final_evaluation_llm_call_id")
        evaluation_step_key = refs.get("near_final_evaluation_step_key")
        evaluation_execution_id = self._validate_artifact_execution_owner(
            refs.get("near_final_evaluation_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=evaluation_call_id,
            execution_step_key=evaluation_step_key,
            execution_id=evaluation_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        source_draft_id = refs.get("near_final_source_draft_row_id")
        if (
            evaluation.object_type != "scene"
            or evaluation.object_id != scene_id
            or evaluation.scene_id != scene_id
            or evaluation.chapter_id != scene.chapter_id
            or evaluation.rubric_id != "near_final_acceptance_v1"
            or evaluation.source_text_ref != f"source_draft:{source_draft_id}"
            or evaluation.source_bundle_id != bundle["bundle_id"]
            or evaluation.evaluator_llm_call_id != evaluation_call_id
            or near_final_payload.get("evaluation_id") != evaluation.evaluation_id
        ):
            raise checkpoint_corrupt("near-final evaluation checkpoint is misbound")
        if refs.get("near_completion") is not None:
            if source_generation is None:
                raise checkpoint_corrupt("near-final prefix validation requires the soft-final source draft")
            eval0 = self._load_near_evaluation_checkpoint(
                scene_id=scene_id,
                round_index=0,
                source_generation=source_generation,
            )
            control = self._load_near_eval0_control(eval0)
            rewrite_count = refs.get("near_final_rewrite_count")
            rewrite_gate = self._load_near_rewrite_gate_checkpoint()
            expected_skip_reason = (
                control.get("skip_reason") if rewrite_count == 0 else None
            )
            if rewrite_count == 1:
                if (
                    not control.get("rewrite_allowed")
                    or control.get("skip_reason") is not None
                ):
                    raise checkpoint_corrupt("near-final rewrite completion is not reachable from eval0")
                if rewrite_gate is not None and rewrite_gate.get("rejected"):
                    raise checkpoint_corrupt("near-final completion promoted a gate-rejected rewrite")
                expected_generation = self._load_near_rewrite_checkpoint(
                    scene_id=scene_id,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                )
                final_evaluation = self._load_near_evaluation_checkpoint(
                    scene_id=scene_id,
                    round_index=1,
                    source_generation=expected_generation,
                )
            elif rewrite_count == 0:
                if control.get("rewrite_allowed"):
                    # v2（W5）：允许的重写被 styled-draft gate 拒绝（抄袭）才可能走到
                    # 这里——重写产物必须仍在且匹配，终稿则是重写前的来源稿。
                    if rewrite_gate is None or not rewrite_gate.get("rejected"):
                        raise checkpoint_corrupt("near-final completion skipped an allowed rewrite")
                    self._load_near_rewrite_checkpoint(
                        scene_id=scene_id,
                        source_generation=source_generation,
                        source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                    )
                    expected_skip_reason = _near_final_rejection_skip_reason(rewrite_gate)
                elif rewrite_gate is not None:
                    raise checkpoint_corrupt("near-final rewrite gate exists for a non-rewrite branch")
                expected_generation = source_generation
                final_evaluation = eval0
            else:
                raise checkpoint_corrupt("near-final rewrite count is invalid")
            expected_payload = near_final_result_payload(
                final_evaluation,
                rewrite_count=rewrite_count,
                rewrite_gate=rewrite_gate,
            )
            eval0_candidate_id = refs.get("near_eval0_revision_candidate_id")
            if isinstance(eval0_candidate_id, str):
                eval0_candidate = self.session.get(
                    RevisionCandidate, eval0_candidate_id
                )
                if eval0_candidate is None:
                    self._raise_checkpoint_output_missing(row_id=eval0_candidate_id)
                expected_eval0_candidate_status = (
                    "superseded"
                    if rewrite_count == 1 and final_evaluation.get("pass_flag")
                    else "candidate"
                )
                if eval0_candidate.status != expected_eval0_candidate_status:
                    raise checkpoint_corrupt(
                        "near-final eval0 candidate lifecycle is inconsistent with the final evaluation",
                    )
            completion = refs.get("near_completion")
            expected_completion = {
                "final_evaluation_round": rewrite_count,
                "rewrite_count": rewrite_count,
                "skip_reason": refs.get("near_final_skip_reason"),
                "branch": final_evaluation.get("near_final_status"),
                "evaluation_id": final_evaluation.get("evaluation_id"),
                "source_draft_row_id": expected_generation.row_id,
                "final_scene_row_id": final_scene.row_id,
            }
            if (
                completion != expected_completion
                or self._json_hash(completion)
                != self._checkpoint_hash("near_completion")
                or refs.get("near_final_branch")
                != final_evaluation.get("near_final_status")
                or refs.get("near_final_skip_reason") != expected_skip_reason
                or near_final_payload != expected_payload
                or refs.get("near_final_source_draft_row_id")
                != expected_generation.row_id
                or final_scene.content != expected_generation.content
                or final_scene.generation_llm_call_id != expected_generation.llm_call_id
            ):
                raise checkpoint_corrupt("near-final completion prefix/branch/hash mismatch")
            finalize_attempts = (
                self.session.execute(
                    select(AttemptTracker).where(
                        AttemptTracker.scene_id == scene_id,
                        AttemptTracker.step == "finalize",
                        AttemptTracker.source_bundle_id == bundle["bundle_id"],
                    )
                )
                .scalars()
                .all()
            )
            matched_finalize = [
                attempt
                for attempt in finalize_attempts
                if (attempt.details_json or {}).get("source_style_draft_row_id")
                == expected_generation.row_id
                and (attempt.details_json or {}).get("final_generation_llm_call_id")
                == expected_generation.llm_call_id
                and (attempt.details_json or {}).get("source_qc_report_id")
                == refs.get("soft_qc_report_id")
            ]
            if len(matched_finalize) != 1:
                raise checkpoint_corrupt("near-final checkpoint has no unique finalize attempt audit row")
        return final_scene, near_final_payload

    def _load_archived_checkpoint(self, scene_id: str) -> FinalScene:
        state = self._active_checkpoint_state()
        scene = self.session.get(SceneCard, scene_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        selected_style = self._load_selected_style_checkpoint(scene_id)
        _soft_qc, soft_generation = self._load_soft_qc_checkpoint(
            scene_id,
            selected_style_generation=selected_style,
        )
        final_scene, _near_final = self._load_near_final_checkpoint(
            scene=scene,
            bundle=bundle,
            source_generation=soft_generation,
        )
        refs = (state.run_checkpoint_json or {}).get("artifact_refs", {})
        carry_notes = list(refs.get("carry_notes") or [])
        if self._json_hash(carry_notes) != self._checkpoint_hash("carry_notes"):
            raise checkpoint_corrupt("archived carry notes hash mismatch")
        contract = self.execution_contract_service.get_or_create(
            scene_id,
            actor_ref="orchestrator",
        )
        self._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            allow_terminal=True,
        )
        self._validate_archive_rule_events_checkpoint(scene)
        self._validate_archive_prose_checkpoint(scene, contract)
        self._validate_archive_vector_product(scene, final_scene)
        self._validate_archive_chapter_product(scene)
        self._validate_archive_volume_product(scene)
        self._validate_archive_chapter_evaluation_product(scene)
        self._validate_archive_drift_product(scene)
        manifest = refs.get("archive_manifest")
        expected_manifest = self._archive_manifest()
        if manifest != expected_manifest or self._json_hash(
            manifest
        ) != self._checkpoint_hash("archive_manifest"):
            raise checkpoint_corrupt("archived product manifest is incomplete or changed")
        memory_id = self._checkpoint_artifact(
            "scene_memory_row_id", expected_node_at_least="archived"
        )
        memory = self._require_checkpoint_row(SceneMemory, memory_id)
        if (
            state.run_checkpoint != "archived"
            or state.scene_status != "archived"
            or state.current_final_scene_row_id != final_scene.row_id
            or state.current_bundle_id != bundle["bundle_id"]
            or state.current_bundle_hash != bundle["bundle_snapshot_hash"]
            or final_scene.status != "archived"
            or memory.scene_id != scene_id
            or memory.chapter_id != scene.chapter_id
            or memory.final_scene_row_id != final_scene.row_id
            or memory.source_bundle_id != bundle["bundle_id"]
            or memory.content != final_scene.content
            or memory.active_flag != 1
            or memory.runtime_eligible != 1
        ):
            raise checkpoint_corrupt("archived checkpoint product graph is inconsistent")
        return final_scene

    @staticmethod
    def _prepare_state_for_run(state: SceneRunState, *, new_execution: bool) -> None:
        if not new_execution:
            return
        state.current_bundle_id = None
        state.current_bundle_hash = None
        state.current_neutral_draft_row_id = None
        state.current_style_draft_row_id = None
        state.current_final_scene_row_id = None
        state.current_human_review_event_id = None
        state.current_qc_report_id = None
        # This counter describes the current run's soft-patch loop; lifecycle
        # provider/total attempt counters and latest_valid remain cumulative.
        state.soft_patch_count = 0

    def _carry_notes_from_report(self, qc_report_id: str) -> list[dict[str, Any]]:
        report = self.session.get(QcReport, qc_report_id)
        if report is None:
            return []
        carry_notes: list[dict[str, Any]] = []
        for entry in report.rewrite_brief_json or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "carry_forward_note":
                continue
            note_scope = entry.get("note_scope")
            carry_note_text = entry.get("carry_note_text")
            if (
                isinstance(note_scope, str)
                and note_scope.strip()
                and isinstance(carry_note_text, str)
                and carry_note_text.strip()
            ):
                carry_notes.append(
                    {
                        "kind": "carry_forward_note",
                        "note_scope": note_scope.strip(),
                        "carry_note_text": carry_note_text.strip(),
                    }
                )
        return carry_notes

    @staticmethod
    def _near_final_rewrite_brief(near_final: dict[str, Any]) -> list[str]:
        rewrite_brief: list[str] = []
        for entry in near_final.get("revision_brief") or []:
            if isinstance(entry, dict):
                action = (
                    entry.get("action")
                    or entry.get("instruction")
                    or entry.get("recommendation")
                )
                if not (isinstance(action, str) and action.strip()):
                    # 验收评审（场景 / 章级）的提示词让每条简报给 target / issue / fix_direction 三个键；只认
                    # action 类键时评审给的具体改法被整条丢掉，重写落到下面的房风默认简报上（有绑定时正是
                    # near_final._apply_style_bound_rewrite_policy 要防的那句）。
                    action = "；".join(
                        text
                        for text in (str(entry.get(key) or "").strip() for key in ("target", "issue", "fix_direction"))
                        if text
                    )
                if isinstance(action, str) and action.strip():
                    rewrite_brief.append(action.strip())
            elif isinstance(entry, str) and entry.strip():
                rewrite_brief.append(entry.strip())
        if not rewrite_brief:
            rewrite_brief.append(
                "Rewrite the full scene so forced choice, paid cost, relationship turn, and ending action are visible."
            )
        return rewrite_brief

    def _with_author_projection(
        self, scene_id: str, state: SceneRunState, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2 项 5：run 结果（含全部早退路径）统一附 §5.3 作者状态契约。"""
        projection = compute_author_state(self.session, scene_id, state)
        return {**payload, **projection}

    @staticmethod
    def _near_final_warning_findings(
        near_final: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """near-final 未过 → Q2/Q3 警告条目（LLM 提案层，不阻断）。

        v2（W5）：重写稿 styled-draft gate 的结果（抄袭被拒 / 禁用词 / gate 未执行）追加
        Q2 警告——与评审是否通过无关，严格模式据此停点。
        """
        warnings = _near_final_rewrite_gate_warnings(
            near_final.get("rewrite_style_gate")
        )
        if near_final.get("pass_flag"):
            return warnings
        failure_class = str(near_final.get("failure_class") or "near_final_unresolved")
        level = "Q3" if failure_class == "prose_model_voice" else "Q2"
        first_finding = next(
            (
                item
                for item in near_final.get("findings") or []
                if isinstance(item, dict)
            ),
            {},
        )
        return [
            {
                "issue_key": f"near_final_{failure_class}",
                "quality_level": level,
                "message": str(first_finding.get("issue") or failure_class),
                "recommended_action": "author_review_optional_fix",
                "verified_by": None,
            },
            *warnings,
        ]

    def _collect_q2_warnings(
        self, state: SceneRunState, near_final_warnings: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """严格模式停点判据：当前 QC 报告的 Q2 条目 + near-final Q2 警告（Q3 只诊断不停）。"""
        warnings = [
            item for item in near_final_warnings if item.get("quality_level") == "Q2"
        ]
        report = (
            self.session.get(QcReport, state.current_qc_report_id)
            if state.current_qc_report_id
            else None
        )
        for issue in (report.issues_json or []) if report else []:
            if isinstance(issue, dict) and issue.get("quality_level") == "Q2":
                warnings.append(issue)
        return warnings

    def _archive_effects(self) -> SceneArchiveEffects:
        """Build the archive-effects worker for the CURRENT run.

        ``_execution_id`` / ``_run_job_id`` are set per run_scene/resume call, so
        the worker is constructed at call time — never cached — and it dispatches
        cluster-internal cross-calls back through ``self`` so instance-level
        overrides (a test seam) keep intercepting sibling recorder calls.
        """
        return SceneArchiveEffects(
            self.session,
            self.llm_runner,
            execution_id=self._execution_id,
            run_job_id=self._run_job_id,
            dispatch=self,
        )

    def _record_narrative_events(
        self,
        scene: SceneCard,
        contract,
        content: str,
        *,
        include_prose: bool = True,
        degrade_errors: bool = True,
        final_scene_row_id: str | None = None,
    ) -> list[str]:
        return self._archive_effects()._record_narrative_events(
            scene,
            contract,
            content,
            include_prose=include_prose,
            degrade_errors=degrade_errors,
            final_scene_row_id=final_scene_row_id,
        )

    def _resolve_scene_project_id(self, scene: SceneCard, contract=None) -> str:
        return self._archive_effects()._resolve_scene_project_id(scene, contract)

    def _archive_event_base(self, scene: SceneCard, contract) -> dict[str, str]:
        return self._archive_effects()._archive_event_base(scene, contract)

    def _record_prose_events(
        self,
        log,
        scene: SceneCard,
        base: dict,
        content: str,
        *,
        final_scene_row_id: str | None = None,
        return_event_ids: bool = False,
    ) -> ProseExtractionResult | tuple[ProseExtractionResult, list[str]]:
        return self._archive_effects()._record_prose_events(
            log,
            scene,
            base,
            content,
            final_scene_row_id=final_scene_row_id,
            return_event_ids=return_event_ids,
        )

    def _record_archive_fidelity_reading(self, scene: SceneCard) -> dict[str, Any]:
        """编排器归档检查点的读数槽位（source=pipeline）；读数是观察，失败只降级（带错误码），不阻断归档。"""
        try:
            return self._archive_effects()._record_archive_fidelity_reading(scene, source="pipeline")
        except Exception as exc:  # noqa: BLE001 — 读数失败不影响归档
            _LOGGER.warning("archive fidelity reading degraded for scene %s", scene.scene_id, exc_info=True)
            return {"outcome": "degraded", "error_code": str(getattr(exc, "code", None) or type(exc).__name__)}


    def resume_after_selection(
        self,
        scene_id: str,
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        state = self.session.get(SceneRunState, scene_id)
        previous_execution_id = state.active_execution_id if state is not None else None
        if not previous_execution_id:
            raise DomainError(
                "RESUME_EXECUTION_NOT_FOUND",
                "candidate selection has no durable execution to resume",
                status_code=409,
                details={"scene_id": scene_id},
            )
        effective_execution_id = (
            execution_id or f"direct-selection-resume:{scene_id}:{uuid4().hex}"
        )
        checkpoints = SceneRunCheckpointService(self.session)
        checkpoints.acquire_selection_resume(scene_id, effective_execution_id)

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            # This endpoint always owns the post-selection continuation. A failed
            # retry may already be at soft/near-final sub-checkpoints; routing it
            # through the ordinary run pipeline would recreate the selection gate.
            result = self._resume_after_selection_pipeline(scene_id)
            if result.get("scene_status") == "archived":
                checkpoints.mark_completed(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except LLMAccountingRejected as exc:
            # Candidate selection resume is synchronous, unlike run/jobs. A
            # lifecycle boundary is an expected recoverable stop: return a
            # durable payload so the UI can request an audited top-up and retry
            # this same post-selection pipeline, never the ordinary prefix.
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            state = self.session.get(SceneRunState, scene_id)
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": getattr(state, "scene_status", None),
                    "lifecycle_budget_block": {
                        "code": getattr(
                            exc, "code", "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
                        ),
                        "message": str(exc),
                        "resume_mode": "selection",
                    },
                },
            )
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None

    def _resume_after_selection_pipeline(self, scene_id: str) -> dict:
        """Wave 3（§5.5/§6.3）：作者终选后从批判修订/QC 续跑到归档。

        前置：终选 gate 已 selected，且持久化 checkpoint 已进入终选或
        终选后的软 QC / near-final 阶段。作者可见 scene_status 可能已提前
        发布为可恢复的 patch/revision 状态，不能把它当作 checkpoint 真值。
        选中稿即后续批判/软 QC/near-final 的输入（§4.4 上限归人）。
        """
        scene = get_scene_or_404(self.session, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        checkpoint_payload = (
            (state.run_checkpoint_json or {}) if state is not None else {}
        )
        checkpoint = state.run_checkpoint if state is not None else None
        checkpoint_is_post_selection = (
            checkpoint in RUN_CHECKPOINT_ORDER
            and RUN_CHECKPOINT_ORDER.index(checkpoint)
            >= RUN_CHECKPOINT_ORDER.index("selection_wait")
            and bool(checkpoint_payload.get("selection_origin_execution_id"))
        )
        if state is None or not checkpoint_is_post_selection:
            raise DomainError(
                "RESUME_NOT_AVAILABLE",
                "scene has no resumable selection checkpoint",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "scene_status": getattr(state, "scene_status", None),
                    "run_checkpoint": checkpoint,
                },
            )
        # Selection handoff bypasses the ordinary run prefix, so validate the
        # durable lifecycle budget before any optional critique/QC provider work.
        self._validate_budget_checkpoint(state)
        selection_event_id = self._checkpoint_artifact(
            "selection_event_id",
            expected_node_at_least="selection_wait",
        )
        offered_row_ids = self._checkpoint_artifact(
            "selection_candidate_row_ids",
            expected_node_at_least="selection_wait",
        )
        gate = (
            self.session.get(HumanReviewEvent, selection_event_id)
            if isinstance(selection_event_id, str)
            else None
        )
        if (
            gate is None
            or gate.scene_id != scene_id
            or gate.event_source != "candidate_selection"
            or not isinstance(offered_row_ids, list)
            or not offered_row_ids
        ):
            raise checkpoint_corrupt("selection checkpoint event/candidate context is invalid")
        details = dict(gate.details_json or {}) if gate is not None else {}
        selected_row_id = details.get("selected_row_id")
        if details.get("candidate_row_ids") != offered_row_ids:
            raise checkpoint_corrupt("selection gate candidates differ from the durable checkpoint")
        if (
            gate is None
            or details.get("decision_status") != "selected"
            or not selected_row_id
        ):
            raise DomainError(
                "SELECTION_REQUIRED",
                "author terminal selection is required before resuming",
                status_code=409,
                details={"scene_id": scene_id},
            )
        if selected_row_id not in offered_row_ids:
            raise checkpoint_corrupt("selected candidate is outside the durable offered set")

        # Selection is a hand-off, not a new prefix. Validate the complete durable
        # prefix before trusting the chosen style row or entering the post-style path.
        planning = self._load_planning_checkpoint(scene_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        self._load_checkpoint_draft(
            scene_id,
            ref_key="neutral_draft_row_id",
            expected_stage="neutral_draft",
            expected_node_at_least="neutral_ready",
            result_type="neutral",
        )
        hard_qc = self._load_hard_qc_checkpoint(scene_id)
        if not hard_qc.should_continue:
            raise checkpoint_corrupt("selection checkpoint follows a terminal hard QC decision")
        self._load_style_checkpoint_candidates(scene_id)
        draft = self.session.get(SceneDraft, selected_row_id)
        if draft is None or not (draft.content or "").strip():
            self._raise_checkpoint_output_missing(row_id=selected_row_id)

        selected_index = offered_row_ids.index(selected_row_id)
        if (
            draft.scene_id != scene_id
            # [批准#2] 终选门只为作者手笔直起的多稿开：候选都是 style_draft 行（去模板谱系随先中性后润色的多稿删掉）
            or draft.stage != "style_draft"
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or self._text_hash(draft.content)
            != self._checkpoint_hash(f"selection_candidate_{selected_index}")
        ):
            raise checkpoint_corrupt(
                "selected candidate identity/source/hash differs from the durable checkpoint",
            )
        checkpoint_payload = state.run_checkpoint_json or {}
        checkpoint_refs = checkpoint_payload.get("artifact_refs") or {}
        candidate_row_ids = checkpoint_refs.get("candidate_row_ids")
        candidate_llm_call_ids = checkpoint_refs.get("llm_call_ids")
        candidate_step_keys = checkpoint_refs.get("style_execution_step_keys")
        candidate_execution_ids = checkpoint_refs.get("style_artifact_execution_ids")
        if (
            not isinstance(candidate_row_ids, list)
            or selected_row_id not in candidate_row_ids
            or not isinstance(candidate_llm_call_ids, list)
            or not isinstance(candidate_step_keys, list)
            or not isinstance(candidate_execution_ids, list)
            or not (
                len(candidate_row_ids)
                == len(candidate_llm_call_ids)
                == len(candidate_step_keys)
                == len(candidate_execution_ids)
            )
        ):
            raise checkpoint_corrupt("selected candidate ledger lineage is incomplete")
        candidate_index = candidate_row_ids.index(selected_row_id)
        selected_llm_call_id = candidate_llm_call_ids[candidate_index]
        selected_step_key = candidate_step_keys[candidate_index]
        selected_execution_id = self._validate_artifact_execution_owner(
            candidate_execution_ids[candidate_index]
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=selected_llm_call_id,
            execution_step_key=selected_step_key,
            execution_id=selected_execution_id,
        )
        selection_decision = {
            "selection_event_id": selection_event_id,
            "selected_row_id": selected_row_id,
            "offered_row_ids": offered_row_ids,
        }
        handoff_ref_keys = (
            "selected_row_id",
            "selected_llm_call_id",
            "selected_execution_step_key",
            "selected_artifact_execution_id",
        )
        checkpoint_hashes = checkpoint_payload.get("artifact_hashes") or {}
        handoff_already_committed = any(
            key in checkpoint_refs for key in handoff_ref_keys
        ) or ("selection_decision" in checkpoint_hashes)
        if handoff_already_committed:
            if (
                checkpoint_refs.get("selected_row_id") != selected_row_id
                or checkpoint_refs.get("selected_llm_call_id") != selected_llm_call_id
                or checkpoint_refs.get("selected_execution_step_key")
                != selected_step_key
                or checkpoint_refs.get("selected_artifact_execution_id")
                != selected_execution_id
                or self._json_hash(selection_decision)
                != self._checkpoint_hash("selection_decision")
                or gate.status != "resolved"
                or details.get("resumed") is not True
                or (
                    state.run_checkpoint == "selection_wait"
                    and (
                        state.current_style_draft_row_id != selected_row_id
                        or state.latest_valid_draft_row_id != selected_row_id
                    )
                )
            ):
                raise checkpoint_corrupt(
                    "selection resume sub-checkpoint differs from committed business state",
                )
        else:
            if state.run_checkpoint != "selection_wait":
                raise checkpoint_corrupt("post-selection checkpoint is missing its durable handoff decision")
            state.current_style_draft_row_id = draft.row_id
            state.latest_valid_draft_row_id = draft.row_id
            gate.status = "resolved"
            gate.details_json = {**details, "resumed": True}
            self._save_run_checkpoint(
                "selection_wait",
                sub_index=0,
                artifact_refs={
                    "selected_row_id": selected_row_id,
                    "selected_llm_call_id": selected_llm_call_id,
                    "selected_execution_step_key": selected_step_key,
                    "selected_artifact_execution_id": selected_execution_id,
                },
                artifact_hashes={
                    "selection_decision": self._json_hash(selection_decision)
                },
                strategy="human_selection_resumed",
            )
        contract = self.execution_contract_service.get_or_create(
            scene_id, actor_ref="orchestrator"
        )
        # 与首跑主管线同一入口：续跑同样喂入 §6.4 连续过渡计数，判定不降级。
        criticality = classify_scene_with_context(self.session, scene)
        style_generation = SimpleNamespace(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=selected_llm_call_id,
            execution_step_key=selected_step_key,
            artifact_execution_id=selected_execution_id,
        )
        hard_qc_payload = qc_decision_payload(hard_qc)
        return self._finalize_after_style(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            planning=planning,
            hard_qc_payload=hard_qc_payload,
            style_generation=style_generation,
            candidate_summaries=None,
            run_policy=state.run_policy or "reliable",
        )



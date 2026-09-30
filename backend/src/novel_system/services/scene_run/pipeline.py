"""一场的管线：预算 → 规划 → bundle → 首稿 → 硬 QC → 风格稿 → 终选门 → 软 QC → 准终稿 → 交给归档尾段。

``Orchestrator`` 直接继承 ``PipelineMixin``。``_run_scene_pipeline`` 名字不变（测试在类上打桩）；各阶段各是一个
``_phase_*`` 方法。
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
import logging
from typing import Any

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services import scene_budget
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.literary_quality import adversarial_rank_score
from novel_system.services.scene_criticality import classify_scene_with_context
from novel_system.services.scene_generation import (
    ProductCallback,
    ProductMetadata,
    RankingAudit,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_lookup import get_scene_or_404
from novel_system.services.scene_run.constants import STYLE_PATCH_REVERTED_STOP_REASON
from novel_system.services.scene_run.context import ArchiveInputs, SceneRunContext
from novel_system.services.scene_run.results import (
    apply_finality,
    base_result,
    merged_warnings,
    near_final_result_payload,
    qc_decision_payload,
    soft_risk_acceptance_event_id,
)
from novel_system.services.scene_run.snapshots import planning_provenance, qc_report_snapshot
from novel_system.services.scene_run_checkpoint import RUN_CHECKPOINT_ORDER, checkpoint_corrupt
from novel_system.services.style_policy import style_policy_for_bundle

_LOGGER = logging.getLogger(__name__)


class PipelineMixin:
    def _run_scene_pipeline(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
    ) -> dict:
        # Wave 2/3（治理 §5.4/§5.5）：run_policy 现已落列（Wave 3 迁移 0062）。
        # reliable（默认）：Q2/Q3 警告随稿归档；strict：存在 Q2 时停在可归档的
        # quality_warning，由作者经 adopt-current 显式接受。Q0/Q1 阻断与模式无关。
        ctx = self._phase_prelude(scene_id, author_note=author_note, run_policy=run_policy)
        self._phase_budget(ctx)
        self._phase_planning(ctx)
        self._phase_bundle(ctx)
        self._phase_criticality(ctx)
        self._phase_first_draft(ctx)
        self._phase_hard_qc(ctx)
        if not ctx.hard_qc.should_continue:
            self.session.flush()
            # Wave 2 项 5：所有早退结果都携带 author_state 契约（含 latest_valid 指针）
            return self._with_author_projection(
                scene_id,
                ctx.state,
                {
                    **base_result(ctx.state, ctx.bundle),
                    "hard_qc": qc_decision_payload(ctx.hard_qc),
                },
            )
        self._phase_style_candidates(ctx)
        paused = self._phase_selection_gate(ctx)
        if paused is not None:
            return paused
        return self._finalize_after_style(
            scene=ctx.scene,
            state=ctx.state,
            contract=ctx.contract,
            bundle=ctx.bundle,
            criticality=ctx.criticality,
            planning=ctx.planning,
            hard_qc_payload=qc_decision_payload(ctx.hard_qc),
            style_generation=ctx.style_generation,
            candidate_summaries=ctx.candidate_summaries if ctx.candidate_summaries else None,
            run_policy=ctx.run_policy,
        )

    def _phase_prelude(
        self, scene_id: str, *, author_note: str | None, run_policy: str
    ) -> SceneRunContext:
        """场景与运行状态（没有就按同一约定补建）、执行合同闸、落本次运行的生效策略。"""
        scene, state = self._ensure_scene_and_state(scene_id)
        contract = self.execution_contract_service.get_or_create(
            scene_id, actor_ref="orchestrator"
        )
        if contract.status != "active":
            detail_reason = (
                "scene execution contract is missing required fields"
                if contract.status == "blocked"
                else "scene execution contract is not ready for drafting"
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
        return SceneRunContext(
            scene=scene,
            state=state,
            contract=contract,
            author_note=author_note,
            run_policy=run_policy,
        )

    def _phase_budget(self, ctx: SceneRunContext) -> None:
        """``budget_ready``：确立场景 token 预算（N × 单发基线，N 取 NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER，
        默认 0 = 不设上限；已设不覆盖）；续跑时复验预算基线。"""
        if not self._checkpoint_reached("budget_ready"):
            ctx.state = scene_budget.ensure_scene_budget_initialized(self.session, ctx.scene_id)
            self._save_run_checkpoint(
                "budget_ready",
                artifact_refs={"scene_token_budget": ctx.state.scene_token_budget},
                artifact_hashes={
                    "budget_basis": self._json_hash(ctx.state.scene_budget_basis_json or {})
                },
            )
        else:
            self._validate_budget_checkpoint(ctx.state)

    def _phase_planning(self, ctx: SceneRunContext) -> None:
        """``planning_ready`` 子游标 0..3：场景蓝图 → 章架构 → 人物压力 → 规划完成（每件产品一落库就存子检查点）。"""
        scene_id = ctx.scene_id
        planning_progress = self._planning_checkpoint_progress()
        if planning_progress >= 3:
            ctx.planning = self._load_planning_checkpoint(scene_id)
            return
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

        planning = self.planning_service.ensure_scene_planning(
            scene_id,
            step_reconciler=self._reconcile_execution_step,
            artifact_committed=partial(self._planning_artifact_committed, scene_id),
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
        ctx.planning = planning

    def _planning_artifact_committed(
        self,
        scene_id: str,
        kind: str,
        serialized: dict[str, Any],
        reused: bool,
    ) -> None:
        """规划服务每落一件产品（章架构 / 人物压力）回调一次：存它的子检查点；续跑时已存过的只核对是同一行。"""
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

    def _phase_bundle(self, ctx: SceneRunContext) -> None:
        """``bundle_ready``：冻结这一场的 bundle；续跑时读回并核对作者附言没变。"""
        if not self._checkpoint_reached("bundle_ready"):
            ctx.bundle = self.bundle_builder.build(ctx.scene_id, author_note=ctx.author_note)
            self._save_run_checkpoint(
                "bundle_ready",
                artifact_refs={"bundle_id": ctx.bundle["bundle_id"]},
                artifact_hashes={"bundle": ctx.bundle["bundle_snapshot_hash"]},
            )
        else:
            ctx.bundle = self._load_checkpoint_bundle(ctx.scene_id)
            self._assert_author_note_matches_bundle(
                ctx.bundle, ctx.author_note, scene_id=ctx.scene_id
            )

    def _phase_criticality(self, ctx: SceneRunContext) -> None:
        """关键度（每次运行重算，不进检查点）：落到运行状态上供接口展示。"""
        # §6.4 / §16：chapter_seq、连续过渡计数、constraint_intensity 的上下文推导
        # 统一收敛在 classify_scene_with_context——与崩溃续跑同一入口，判定不得分叉。
        criticality = classify_scene_with_context(self.session, ctx.scene)
        # 记开关打开时这一场会起的候选数（_best_of_n_count 读的就是它；旧的候选上限随 [批准#2] 删了）
        _LOGGER.info(
            "scene %s criticality=%s reasons=%s initial_best_of_n=%d",
            ctx.scene_id,
            criticality.level,
            criticality.reasons,
            criticality.initial_best_of_n,
        )
        # §6 Defect D: persist criticality classification for API exposure
        ctx.state.criticality_level = criticality.level
        ctx.state.criticality_reasons_json = criticality.reasons
        ctx.criticality = criticality

    def _phase_first_draft(self, ctx: SceneRunContext) -> None:
        """``neutral_ready``：首稿（中性或作者手笔直起，由 scene_generation 按起草方式定）。"""
        scene_id = ctx.scene_id
        if self._checkpoint_reached("neutral_ready"):
            ctx.neutral_generation = self._load_checkpoint_draft(
                scene_id,
                ref_key="neutral_draft_row_id",
                expected_stage="neutral_draft",
                expected_node_at_least="neutral_ready",
                result_type="neutral",
            )
            return
        self._reconcile_execution_step("neutral_draft")
        neutral_generation = self.scene_generation_service.generate_neutral_draft(
            scene_id,
            ctx.bundle,
            author_note=ctx.author_note,
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
        ctx.neutral_generation = neutral_generation

    def _phase_hard_qc(self, ctx: SceneRunContext) -> None:
        """``hard_qc_ready``：首稿过硬 QC（没过就停在这里，``hard_qc.should_continue`` 为假）。"""
        scene_id = ctx.scene_id
        if self._checkpoint_reached("hard_qc_ready"):
            ctx.hard_qc = self._load_hard_qc_checkpoint(scene_id)
            return
        neutral_generation = ctx.neutral_generation
        self._reconcile_execution_step("hard_qc:0")
        hard_qc = self.hard_qc_engine.evaluate(
            scene_id=scene_id,
            bundle=ctx.bundle,
            neutral_draft_row_id=neutral_generation.row_id,
            neutral_content=neutral_generation.content,
            execution_step_key="hard_qc:0",
        )
        # 前六键与运行结果的 hard_qc 同源；哈希按排好序的键算（_json_hash），键序不影响哈希。
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
                "hard_qc_bundle_id": ctx.bundle["bundle_id"],
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
        ctx.hard_qc = hard_qc

    def _phase_style_candidates(self, ctx: SceneRunContext) -> None:
        """风格稿（``hard_qc_ready`` 子游标 = 每个槽位的底稿 / 成稿）→ ``style_ready``：候选与它们的排序审计。"""
        scene_id = ctx.scene_id
        bundle = ctx.bundle
        # Wave 3（§5.5 成本分配）：候选数 N 由开关与关键度定（关键 3 / 标准 2 / 过渡 1）。
        # [批准#2] 只有作者手笔直起才出多稿：其余起草方式即使开关打开也只起一稿（检查点如实记 single）。
        n_candidates = self._best_of_n_count(ctx.contract, criticality=ctx.criticality)
        if n_candidates > 1 and not style_policy_for_bundle(bundle).style_first:
            n_candidates = 1
        style_ready = self._checkpoint_reached("style_ready")
        if style_ready:
            candidates = self._load_style_checkpoint_candidates(scene_id)
        else:
            neutral_generation = ctx.neutral_generation
            style_work_items = self._load_partial_style_work_items(
                scene_id,
                expected_initial_count=n_candidates,
            )
            resume_bases, resume_products = self._style_resume_products(
                style_work_items, scene_id=scene_id
            )
            product_callback: ProductCallback = partial(
                self._save_style_product_checkpoint,
                style_work_items,
                neutral_draft_row_id=neutral_generation.row_id,
                n_candidates=n_candidates,
            )
            if n_candidates > 1:
                candidates = (
                    self.scene_generation_service.generate_style_draft_candidates(
                        scene_id,
                        bundle,
                        neutral_draft_row_id=neutral_generation.row_id,
                        neutral_content=neutral_generation.content,
                        author_note=ctx.author_note,
                        n_candidates=n_candidates,
                        step_reconciler=self._reconcile_execution_step,
                        resume_bases=resume_bases,
                        resume_products=resume_products,
                        product_callback=product_callback,
                    )
                )
            else:
                if "initial:0" not in resume_bases:
                    self._reconcile_execution_step("style_draft:0")
                candidates = [
                    self.scene_generation_service.generate_style_draft(
                        scene_id,
                        bundle,
                        neutral_draft_row_id=neutral_generation.row_id,
                        neutral_content=neutral_generation.content,
                        author_note=ctx.author_note,
                        resume_base=resume_bases.get("initial:0"),
                        product_callback=product_callback,
                        step_reconciler=self._reconcile_execution_step,
                    )
                ]
        style_generation = candidates[0]
        ctx.n_candidates = n_candidates
        ctx.candidates = candidates
        ctx.style_generation = style_generation
        # 标准场景：机器下限 + 受约束风格信号继续管线；关键场景在下一阶段暂停终选。
        # 从 checkpoint 恢复时也重建同一摘要，避免审计信息因一次进程中断消失。
        ctx.candidate_summaries = self._candidate_summaries(candidates)
        if style_ready:
            return
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

    def _save_style_product_checkpoint(
        self,
        style_work_items: list[dict[str, Any]],
        slot_key: str,
        phase: str,
        product: StyleGenerationResult,
        metadata: ProductMetadata,
        *,
        neutral_draft_row_id: str,
        n_candidates: int,
    ) -> None:
        """风格稿每落一件产品（某个槽位的底稿 / 成稿）回调一次：更新工作项，存 ``hard_qc_ready`` 的子检查点
        （子游标 = 槽位序 × 2 + 是否成稿）。"""
        self._update_style_work_item(
            style_work_items,
            slot_key=slot_key,
            phase=phase,
            product=product,
            metadata=metadata,
            neutral_draft_row_id=neutral_draft_row_id,
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

    @staticmethod
    def _candidate_summaries(
        candidates: list[StyleGenerationResult],
    ) -> list[dict[str, Any]]:
        """运行结果的 ``style_candidates``：每份候选的排名、分数与排序审计（第一份是选中的）。"""
        summaries: list[dict[str, Any]] = []
        for idx, cand in enumerate(candidates):
            ranking: RankingAudit = cand.ranking_audit or {}
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
            summaries.append(summary)
        return summaries

    def _phase_selection_gate(self, ctx: SceneRunContext) -> dict[str, Any] | None:
        """关键场景在候选之后暂停，等作者匿名终选（``selection_wait``）；暂停就返回运行结果，否则 None。"""
        # Wave 3（§5.5）：关键场景在候选生成后暂停编排——确定性坏稿淘汰 →
        # 匿名终选 gate；作者选择后经 resume-after-selection 从批判修订/QC 继续。
        # 「§6.3 终选决定质量上界，归人」从推荐信号升级为强制暂停。
        # 风格参考 v3（L2）：按正文去重之后才数候选——作者手笔直起时没过门的修改槽位保留首稿原文，几个槽位可能
        # 是同一段字；只剩一份不同的正文就没有可选的，不能让作者对着一份稿子「终选」，管线照常往下走
        candidates = ctx.candidates
        if not (ctx.criticality.human_gate and self._distinct_candidate_count(candidates) > 1):
            return None
        state = ctx.state
        offered_row_ids = self._offer_candidates_for_selection(
            ctx.scene, state, ctx.bundle, candidates
        )
        if offered_row_ids is None:
            return None
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
            ctx.scene_id,
            state,
            {
                **base_result(state, ctx.bundle),
                "hard_qc": qc_decision_payload(ctx.hard_qc),
                "planning": ctx.planning,
                "run_policy": ctx.run_policy,
                # 盲化：暂停响应只报数量，不带分数/预览（候选经盲化视图取用）
                "candidate_count": len(offered_row_ids),
                "candidate_selection_required": True,
            },
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
                    **base_result(state, bundle),
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
                        **base_result(state, bundle),
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
            # 同一进程刚做完的：归档尾段不再从检查点读回复验（只在续跑时复验，B01-11）
            in_process=ArchiveInputs(
                soft_qc=soft_qc,
                final_scene=self.session.get(FinalScene, final_row_id),
                near_final_payload=near_final_payload,
            ),
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
        # 与首跑交给 _finalize_after_style 的是同一种值（B01-18：以前是只带五个字段的 SimpleNamespace）
        style_generation = StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=selected_llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
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

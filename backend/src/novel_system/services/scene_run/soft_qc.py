"""软 QC（``soft_qc_ready`` 子游标 0..3：软 QC 的输入 → 第一轮 → 修补 → 第二轮）的驱动、修补留用 / 回退、检查点存取。

``Orchestrator`` 直接继承 ``SoftQcCheckpointMixin``；``_style_patch_keep_decision`` 测试在类上打桩。
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

from novel_system.db.models import (
    AttemptTracker,
    HumanReviewEvent,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection
from novel_system.services.qc_engine import SoftQcDecision
from novel_system.services.scene_generation import (
    STYLE_NOTICE_PATCH_REVERTED,
    STYLE_PATCH_KEEP_STEP,
    StepKeys,
    StyleGenerationResult,
    fidelity_probe,
    style_notice,
)
from novel_system.services.scene_run.branch_control import is_derivable_control, soft_qc0_control
from novel_system.services.scene_run.constants import (
    STYLE_PATCH_REVERTED_SKIP_REASON,
    STYLE_PATCH_REVERTED_STOP_REASON,
)
from novel_system.services.scene_run.snapshots import qc_report_snapshot, soft_decision_snapshot
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference.style_step import (
    PATCH_DECISION_REVERTED,
    PATCH_REASON_PAIRWISE_ORIGINAL,
    fidelity_thresholds,
    patch_keep_decision,
    reading_brief,
    report_reference_judge,
)

_LOGGER = logging.getLogger(__name__)

# 软 QC 输入（= 选定的风格稿）可能的稿子阶段：风格稿，或去模板改写后的风格稿
SOFT_INPUT_STAGES = frozenset({"style_draft", "de_template"})


class SoftQcCheckpointMixin:
    def _soft_checkpoint_progress(self) -> int:
        return self._sub_checkpoint_progress(
            "soft_qc_ready",
            last_sub_index=3,
            invalid_message="soft QC checkpoint sub-index is invalid",
        )

    def _ensure_soft_qc_subcheckpoints(
        self,
        *,
        scene: SceneCard,
        contract: Any,
        bundle: dict[str, Any],
        selected_style_generation: StyleGenerationResult,
        optional_spend_allowed,
    ) -> tuple[SoftQcDecision, StyleGenerationResult]:
        progress = self._soft_checkpoint_progress()
        if progress >= 3:
            return self._load_soft_qc_checkpoint(
                scene.scene_id,
                selected_style_generation=selected_style_generation,
            )

        scene_id = scene.scene_id
        style_generation = selected_style_generation
        if progress < 0:
            # 软 QC 的输入就是选定的风格稿（自动批评删了，G4 B3-1b.1）：第 0 步记下它与它的来源，续跑时核对是同一行
            self._validate_generation_before_checkpoint(scene_id, style_generation)
            self._save_run_checkpoint(
                "soft_qc_ready",
                sub_index=0,
                artifact_refs=self._soft_draft_refs(
                    prefix="soft_input",
                    generation=style_generation,
                    source_draft_row_id=selected_style_generation.row_id,
                    bundle=bundle,
                ),
                artifact_hashes={
                    "soft_input_draft": self._text_hash(style_generation.content)
                },
            )
            progress = 0
        else:
            style_generation = self._load_soft_draft_checkpoint(
                scene_id,
                prefix="soft_input",
                expected_source_draft_row_id=selected_style_generation.row_id,
                expected_stages=SOFT_INPUT_STAGES,
            )

        if progress < 1:
            self._reconcile_execution_step("soft_qc:0")
            soft_qc0 = self.soft_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                source_draft_row_id=style_generation.row_id,
                source_draft_content=style_generation.content,
                execution_step_key="soft_qc:0",
            )
            qc0_control = soft_qc0_control(
                soft_qc0.branch,
                spend_allowed=soft_qc0.branch == "patch" and optional_spend_allowed(),
            )
            patch_allowed = qc0_control["patch_allowed"]
            qc0_skip_reason = qc0_control["skip_reason"]
            if qc0_skip_reason == "budget_or_candidate_cap":
                _LOGGER.warning(
                    "soft patch skipped for scene %s (budget/candidate cap)", scene_id
                )
            self._save_soft_qc_round_checkpoint(
                sub_index=1,
                round_index=0,
                decision=soft_qc0,
                source_generation=style_generation,
                bundle=bundle,
                patch_allowed=patch_allowed,
                skip_reason=qc0_skip_reason,
            )
            progress = 1
        else:
            soft_qc0 = self._load_soft_qc_round_checkpoint(
                scene_id,
                round_index=0,
                source_generation=style_generation,
            )
            patch_allowed, qc0_skip_reason = self._load_soft_qc0_branch_control(
                soft_qc0
            )

        if soft_qc0.branch == "patch" and patch_allowed:
            if progress < 2:
                rewrite_brief = self._pov_desensitize_brief(
                    scene,
                    contract,
                    self._rewrite_brief_from_report(soft_qc0.qc_report_id),
                )
                self._reconcile_execution_step(StepKeys.soft_patch("soft_qc"))
                final_generation = self.scene_generation_service.generate_style_patch(
                    scene_id,
                    bundle,
                    source_style_draft_row_id=style_generation.row_id,
                    source_style_content=style_generation.content,
                    rewrite_brief=rewrite_brief,
                    source_qc_report_id=soft_qc0.qc_report_id,
                    execution_step_key=StepKeys.soft_patch("soft_qc"),
                )
                self._save_run_checkpoint(
                    "soft_qc_ready",
                    sub_index=2,
                    artifact_refs=self._soft_draft_refs(
                        prefix="soft_patch",
                        generation=final_generation,
                        source_draft_row_id=style_generation.row_id,
                        bundle=bundle,
                        source_qc_report_id=soft_qc0.qc_report_id,
                    ),
                    artifact_hashes={
                        "soft_patch_draft": self._text_hash(final_generation.content)
                    },
                    branch="patch",
                )
            else:
                final_generation = self._load_soft_draft_checkpoint(
                    scene_id,
                    prefix="soft_patch",
                    expected_source_draft_row_id=style_generation.row_id,
                    expected_stages={"style_patch"},
                    expected_source_qc_report_id=soft_qc0.qc_report_id,
                )

            # 2026-10-14 评审统一：补丁留不留先问成对比较（补丁前 vs 补丁稿，两个次序各一次）。放在补丁的检查点
            # 之后、soft_qc:1 之前：模型调用会先提交会话，这里提交的只是已经落了检查点的东西
            patch_comparison = self._compare_patch(
                scene=scene, bundle=bundle, before=style_generation, after=final_generation
            )
            self._reconcile_execution_step("soft_qc:1")
            soft_qc = self.soft_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                source_draft_row_id=final_generation.row_id,
                source_draft_content=final_generation.content,
                execution_step_key="soft_qc:1",
            )
            # 风格参考 v3（P5b，N6 / V7）：作者手笔直起时补丁「不更像就不采用」——参考评审分变差、或确定性
            # distance 明显变大而评审分没提高 → 退回补丁前的稿子（指针与当前 QC 报告随之指回，检查点记退回）。
            # L3：这一段夹在 soft_qc:1 的调用与它的检查点之间，只是观察（读数、记读数、记决定）——在保存点里做，
            # 任何失败只回滚保存点、按「留下补丁」处理，绝不能让一次已派发的调用落不下检查点。
            patch_keep = self._observe_patch_keep(
                scene=scene,
                bundle=bundle,
                before=style_generation,
                after=final_generation,
                qc0=soft_qc0,
                qc1=soft_qc,
                comparison=patch_comparison,
            )
            if patch_keep is not None and patch_keep.get("decision") == PATCH_DECISION_REVERTED:
                reverted = self._reverted_patch_decision(soft_qc0)
                state = self.session.get(SceneRunState, scene_id)
                if state is not None:
                    state.current_style_draft_row_id = style_generation.row_id
                    state.latest_valid_draft_row_id = style_generation.row_id
                    state.current_qc_report_id = soft_qc0.qc_report_id
                    state.scene_status = "soft_qc_passed_with_notes"
                    self.session.flush()
                self._save_soft_qc_round_checkpoint(
                    sub_index=3,
                    round_index=1,
                    decision=soft_qc,
                    source_generation=final_generation,
                    bundle=bundle,
                    final_generation=style_generation,
                    final_skip_reason=STYLE_PATCH_REVERTED_SKIP_REASON,
                    completion_decision=reverted,
                    completion_source_generation=style_generation,
                )
                return reverted, style_generation
            self._save_soft_qc_round_checkpoint(
                sub_index=3,
                round_index=1,
                decision=soft_qc,
                source_generation=final_generation,
                bundle=bundle,
                final_generation=final_generation,
                final_skip_reason=None,
            )
            return soft_qc, final_generation

        if progress >= 2:
            raise checkpoint_corrupt("soft patch checkpoint exists for a non-patch QC0 branch")
        self._save_soft_qc_round_checkpoint(
            sub_index=3,
            round_index=0,
            decision=soft_qc0,
            source_generation=style_generation,
            bundle=bundle,
            final_generation=style_generation,
            final_skip_reason=qc0_skip_reason,
        )
        return soft_qc0, style_generation

    def _soft_round_owner(self, round_index: int) -> str | None:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get("artifact_refs") or {}
        owner = refs.get(f"soft_qc{round_index}_artifact_execution_id")
        return str(owner) if isinstance(owner, str) and owner else self._execution_id

    @staticmethod
    def _reverted_patch_decision(qc0: SoftQcDecision) -> SoftQcDecision:
        """补丁被退回时软 QC 阶段的收尾决定：补丁前那一轮评审，放行并留痕（waive）。"""
        return SoftQcDecision(
            branch="waive",
            qc_report_id=qc0.qc_report_id,
            human_review_event_id=None,
            resolution_code=qc0.resolution_code,
            next_action=qc0.next_action,
            should_continue=True,
            stop_reason=STYLE_PATCH_REVERTED_STOP_REASON,
            llm_call_id=qc0.llm_call_id,
            execution_step_key=qc0.execution_step_key,
        )

    def _report_judge(self, qc_report_id: str | None) -> dict[str, Any] | None:
        """软 QC 报告里的参考评审分（10 分制的 ``reference_judge`` 条目，``style_step.report_reference_judge``）；没有 → None。"""
        return report_reference_judge(self.session.get(QcReport, qc_report_id) if qc_report_id else None)

    def _compare_patch(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        before: StyleGenerationResult,
        after: StyleGenerationResult,
    ) -> dict[str, Any] | None:
        """补丁前 vs 补丁稿的成对比较（``scene_generation.style_compare``）；不是作者手笔直起 → None（去留规则不适用）。
        比不成只记原因、退回读数规则；云策略的 409 原样抛（软 QC 第二轮同样会拦）。"""
        policy = style_policy_for_bundle(bundle)
        if not getattr(policy, "style_first", False):
            return None
        from novel_system.services.scene_generation.style_compare import compare_versions
        from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS

        try:
            return compare_versions(
                self.session,
                self.scene_generation_service._llm_runner,
                scene=scene,
                bundle=bundle,
                policy=policy,
                base_text=before.content,
                candidate_text=after.content,
                execution_step_key=STYLE_PATCH_KEEP_STEP,
            )
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            raise
        except Exception:  # noqa: BLE001 — 比不成不阻断起草，去留退回读数规则
            _LOGGER.warning("patch comparison failed for scene %s", scene.scene_id, exc_info=True)
            return {"verdict": "unavailable", "reason": "error"}

    def _observe_patch_keep(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        before: StyleGenerationResult,
        after: StyleGenerationResult,
        qc0: SoftQcDecision,
        qc1: SoftQcDecision,
        comparison: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """:meth:`_style_patch_keep_decision` 的安全外壳（风格参考 v3 L3）：它夹在 ``soft_qc:1`` 的调用与检查点之间，
        只做观察（读数、记读数、记决定的尝试行）。整段在保存点里跑，任何失败只回滚这个保存点、记日志、返回 ``None``
        （= 没有退回的依据，留下补丁，与旧行为一致）——会话照样可用，检查点照常落下。"""
        try:
            with self.session.begin_nested():
                return self._style_patch_keep_decision(
                    scene=scene, bundle=bundle, before=before, after=after, qc0=qc0, qc1=qc1, comparison=comparison
                )
        except Exception:  # noqa: BLE001 — 观察失败不能让已派发的调用落不下检查点
            _LOGGER.warning("style patch keep decision failed for scene %s; keeping the patch", scene.scene_id, exc_info=True)
            return None

    def _style_patch_keep_decision(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        before: StyleGenerationResult,
        after: StyleGenerationResult,
        qc0: SoftQcDecision,
        qc1: SoftQcDecision,
        comparison: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """作者手笔直起时补丁的去留（风格参考 v3 N6 / V7；2026-10-14 改成成对比较）；不适用（未让位 / 补丁后要人工
        复核）→ None。

        先看成对比较（``comparison``：两次都说补丁前更像 → 退回，都说补丁更像 → 留下）；没有明确结论再看确定性读数的
        distance。记一条 ``patched`` 读数（带补丁后那轮的评审）与一条 ``style_patch_keep`` 尝试（决定、比较、读数、两轮
        评审；退回时带 STYLE_PATCH_REVERTED 提示）。
        """
        policy = style_policy_for_bundle(bundle)
        if not policy.style_first or qc1.branch == "human_review_required":
            return None
        thresholds = fidelity_thresholds()
        judge_before = self._report_judge(qc0.qc_report_id)
        judge_after = self._report_judge(qc1.qc_report_id)
        # 读数是观察（B02-05：与生成侧同一个探针，各在自己的保存点里读，失败只回滚保存点）：读不出只看评审分
        reading_before, _ = fidelity_probe.observe(
            self.session, policy, before.content, ref=scene.scene_id, what="patch-before"
        )
        reading_after, _ = fidelity_probe.observe(
            self.session, policy, after.content, ref=scene.scene_id, what="patch-after"
        )
        comparable = (
            reading_before is not None
            and reading_after is not None
            and reading_before.reliable
            and reading_after.reliable
        )
        decision, reason = patch_keep_decision(
            comparison=(comparison or {}).get("verdict"),
            before_distance=reading_before.distance if comparable else None,
            after_distance=reading_after.distance if comparable else None,
            thresholds=thresholds,
        )
        patched_row = style_readings.record_fidelity_reading(
            self.session,
            policy=policy,
            text=after.content,
            source=style_readings.SOURCE_PIPELINE,
            stage=style_readings.STAGE_PATCHED,
            scene_id=scene.scene_id,
            project_id=style_readings.scene_project_id(self.session, scene),
            draft_ref=after.row_id,
            reading=reading_after,
            judge=judge_after,
            max_percentile=thresholds.style_step_max_percentile,
        )
        notices: list[dict[str, Any]] = []
        if decision == PATCH_DECISION_REVERTED:
            notices.append(
                style_notice(
                    STYLE_NOTICE_PATCH_REVERTED,
                    (
                        "软 QC 的补丁没有让稿子更像参考作者（成对比较两次都认为补丁前的稿子更像）"
                        if reason == PATCH_REASON_PAIRWISE_ORIGINAL
                        else "软 QC 的补丁让稿子离参考作者更远（读数变远，成对比较没有分出高下）"
                    )
                    + "，已退回补丁前的稿子；评审的改稿意见随稿留痕。",
                    severity="info",
                    reason=reason,
                    comparison=(comparison or {}).get("verdict"),
                    before_distance=reading_before.distance if reading_before is not None else None,
                    after_distance=reading_after.distance if reading_after is not None else None,
                    restored_row_id=before.row_id,
                )
            )
        details = {
            "version": "style_patch_keep_v2",
            "decision": decision,
            "reason": reason,
            "source_draft_row_id": after.row_id,
            "restored_row_id": before.row_id if decision == PATCH_DECISION_REVERTED else None,
            "before_qc_report_id": qc0.qc_report_id,
            "after_qc_report_id": qc1.qc_report_id,
            "comparison": dict(comparison) if comparison else None,
            "before_judge": judge_before,
            "after_judge": judge_after,
            "before_reading": reading_brief(reading_before),
            "after_reading": reading_brief(
                reading_after, reading_id=patched_row.reading_id if patched_row is not None else None
            ),
            "distance_compared": comparable,
            "thresholds": thresholds.audit(),
            "notices": notices,
        }
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step=STYLE_PATCH_KEEP_STEP,
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details,
            )
        )
        self.session.flush()
        return details

    def _soft_draft_refs(
        self,
        *,
        prefix: str,
        generation: StyleGenerationResult,
        source_draft_row_id: str,
        bundle: dict[str, Any],
        source_qc_report_id: str | None = None,
    ) -> dict[str, Any]:
        refs = {
            f"{prefix}_draft_row_id": generation.row_id,
            f"{prefix}_llm_call_id": generation.llm_call_id,
            f"{prefix}_execution_step_key": generation.execution_step_key,
            f"{prefix}_artifact_execution_id": generation.artifact_execution_id
            or self._execution_id,
            f"{prefix}_source_draft_row_id": source_draft_row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
        }
        if source_qc_report_id is not None:
            refs[f"{prefix}_source_qc_report_id"] = source_qc_report_id
        return refs

    def _load_soft_draft_checkpoint(
        self,
        scene_id: str,
        *,
        prefix: str,
        expected_source_draft_row_id: str,
        expected_stages: set[str],
        expected_source_qc_report_id: str | None = None,
    ) -> StyleGenerationResult:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        row_id = refs.get(f"{prefix}_draft_row_id")
        draft = self._require_checkpoint_row(SceneDraft, row_id)
        source_row_id = refs.get(f"{prefix}_source_draft_row_id")
        self._require_checkpoint_row(SceneDraft, source_row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get(f"{prefix}_llm_call_id")
        execution_step_key = refs.get(f"{prefix}_execution_step_key")
        artifact_execution_id = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        generation_parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            execution_id=artifact_execution_id,
        )
        if (
            draft.scene_id != scene_id
            or draft.stage not in expected_stages
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != llm_call_id
            or source_row_id != expected_source_draft_row_id
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or self._text_hash(draft.content)
            != self._checkpoint_hash(f"{prefix}_draft")
        ):
            raise checkpoint_corrupt(f"{prefix} checkpoint draft identity/source/hash mismatch")
        if prefix == "soft_input" and draft.row_id != source_row_id:
            # 软 QC 的输入就是选定的风格稿本身（没有自动批评的补丁夹在中间了）
            raise checkpoint_corrupt("soft input is not the selected style draft")
        if expected_source_qc_report_id is not None and (
            refs.get(f"{prefix}_source_qc_report_id") != expected_source_qc_report_id
        ):
            raise checkpoint_corrupt(f"{prefix} checkpoint QC source mismatch")
        try:
            self._validate_generation_parent_identity(
                scene_id=scene_id,
                parent=generation_parent,
                draft_stage=draft.stage,
                execution_step_key=execution_step_key,
                execution_id=artifact_execution_id,
                draft=draft,
            )
            self._validate_settled_parent_ledger(generation_parent)
        except LLMAccountingError as exc:
            raise checkpoint_corrupt(
                f"{prefix} generation parent/physical-attempt ledger is invalid",
                details={"llm_call_id": llm_call_id, "error_code": exc.code},
            ) from exc
        return StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=execution_step_key,
            artifact_execution_id=artifact_execution_id,
        )

    def _save_soft_qc_round_checkpoint(
        self,
        *,
        sub_index: int,
        round_index: int,
        decision: SoftQcDecision,
        source_generation: StyleGenerationResult,
        bundle: dict[str, Any],
        patch_allowed: bool | None = None,
        skip_reason: str | None = None,
        final_generation: StyleGenerationResult | None = None,
        final_skip_reason: str | None = None,
        completion_decision: SoftQcDecision | None = None,
        completion_source_generation: StyleGenerationResult | None = None,
    ) -> None:
        """一轮软 QC 的检查点；带 ``final_generation`` 时同时记软 QC 阶段的收尾。

        风格参考 v3（P5b）：补丁被退回时，这一轮（``soft_qc1_*``）照记补丁稿的评审，收尾（``soft_qc_*`` /
        ``soft_completion``）记 ``completion_decision``（由补丁前那一轮评审派生）与补丁前的稿子——收尾的 QC 报告
        永远评的是收尾的那份稿子。"""
        self.session.flush()
        report = self.session.get(QcReport, decision.qc_report_id)
        if report is None:
            self._raise_checkpoint_output_missing(row_id=decision.qc_report_id)
        prefix = f"soft_qc{round_index}"
        decision_snapshot = soft_decision_snapshot(
            decision, include_should_continue=True
        )
        refs: dict[str, Any] = {
            f"{prefix}_report_id": decision.qc_report_id,
            f"{prefix}_decision": decision_snapshot,
            f"{prefix}_source_draft_row_id": source_generation.row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
            f"{prefix}_llm_call_id": decision.llm_call_id,
            f"{prefix}_execution_step_key": decision.execution_step_key,
            f"{prefix}_artifact_execution_id": self._execution_id,
        }
        hashes = {
            f"{prefix}_decision": self._json_hash(decision_snapshot),
            f"{prefix}_report": self._json_hash(qc_report_snapshot(report)),
        }
        if round_index == 0 and patch_allowed is not None:
            control = {"patch_allowed": patch_allowed, "skip_reason": skip_reason}
            refs["soft_qc0_control"] = control
            hashes["soft_qc0_control"] = self._json_hash(control)
        if final_generation is not None:
            final_decision = completion_decision or decision
            final_source = completion_source_generation or source_generation
            final_report = report
            if completion_decision is not None:
                final_report = self.session.get(QcReport, completion_decision.qc_report_id)
                if final_report is None:
                    self._raise_checkpoint_output_missing(row_id=completion_decision.qc_report_id)
            final_decision_snapshot = soft_decision_snapshot(
                final_decision, include_should_continue=False
            )
            completion = {
                "final_qc_round": round_index,
                "skip_reason": final_skip_reason,
                "branch": final_decision.branch,
                "qc_report_id": final_decision.qc_report_id,
                "draft_row_id": final_generation.row_id,
            }
            refs.update(
                {
                    "soft_qc_report_id": final_decision.qc_report_id,
                    "soft_qc_human_review_event_id": final_decision.human_review_event_id,
                    "soft_qc_branch": final_decision.branch,
                    "soft_qc_resolution_code": final_decision.resolution_code,
                    "soft_qc_next_action": final_decision.next_action,
                    "soft_qc_stop_reason": final_decision.stop_reason,
                    "soft_final_draft_row_id": final_generation.row_id,
                    "soft_final_llm_call_id": final_generation.llm_call_id,
                    "soft_final_execution_step_key": final_generation.execution_step_key,
                    "soft_final_artifact_execution_id": (
                        final_generation.artifact_execution_id or self._execution_id
                    ),
                    "soft_qc_source_draft_row_id": final_source.row_id,
                    "soft_qc_bundle_id": bundle["bundle_id"],
                    "soft_qc_llm_call_id": final_decision.llm_call_id,
                    "soft_qc_execution_step_key": final_decision.execution_step_key,
                    "soft_qc_artifact_execution_id": (
                        self._soft_round_owner(0) if completion_decision is not None else self._execution_id
                    ),
                    "soft_final_qc_round": round_index,
                    "soft_completion_skip_reason": final_skip_reason,
                    "soft_completion": completion,
                }
            )
            hashes.update(
                {
                    "soft_final_draft": self._text_hash(final_generation.content),
                    "soft_qc_decision": self._json_hash(final_decision_snapshot),
                    "soft_qc_report": self._json_hash(qc_report_snapshot(final_report)),
                    "soft_completion": self._json_hash(completion),
                }
            )
        self._save_run_checkpoint(
            "soft_qc_ready",
            sub_index=sub_index,
            artifact_refs=refs,
            artifact_hashes=hashes,
            branch=decision.branch,
        )

    def _load_soft_qc_round_checkpoint(
        self,
        scene_id: str,
        *,
        round_index: int,
        source_generation: StyleGenerationResult,
    ) -> SoftQcDecision:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        prefix = f"soft_qc{round_index}"
        report_id = refs.get(f"{prefix}_report_id")
        report = self._require_checkpoint_row(QcReport, report_id)
        decision_payload = refs.get(f"{prefix}_decision")
        if not isinstance(decision_payload, dict) or self._json_hash(
            decision_payload
        ) != self._checkpoint_hash(f"{prefix}_decision"):
            raise checkpoint_corrupt(f"soft QC{round_index} decision hash mismatch")
        decision = SoftQcDecision(
            branch=str(decision_payload.get("branch") or "continue"),
            qc_report_id=str(decision_payload.get("qc_report_id") or ""),
            human_review_event_id=decision_payload.get("human_review_event_id"),
            resolution_code=str(decision_payload.get("resolution_code") or ""),
            next_action=str(decision_payload.get("next_action") or ""),
            should_continue=bool(decision_payload.get("should_continue")),
            stop_reason=decision_payload.get("stop_reason"),
            llm_call_id=decision_payload.get("llm_call_id"),
            execution_step_key=decision_payload.get("execution_step_key"),
        )
        owner = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
            execution_id=owner,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        bundle = self._load_checkpoint_bundle(scene_id)
        if (
            report.scene_id != scene_id
            or report.qc_type != "soft_qc"
            or report.qc_report_id != decision.qc_report_id
            or report.source_draft_row_id != source_generation.row_id
            or refs.get(f"{prefix}_source_draft_row_id") != source_generation.row_id
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or refs.get(f"{prefix}_llm_call_id") != decision.llm_call_id
            or refs.get(f"{prefix}_execution_step_key") != decision.execution_step_key
            or decision.execution_step_key != f"soft_qc:{round_index}"
            or self._json_hash(qc_report_snapshot(report))
            != self._checkpoint_hash(f"{prefix}_report")
        ):
            raise checkpoint_corrupt(f"soft QC{round_index} checkpoint identity/source mismatch")
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="soft_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_generation.row_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        if decision.branch == "human_review_required":
            event = self.session.get(HumanReviewEvent, decision.human_review_event_id)
            if event is None:
                self._raise_checkpoint_output_missing(
                    row_id=decision.human_review_event_id
                )
            replay_context = (event.details_json or {}).get("replay_context")
            if (
                event.scene_id != scene_id
                or event.object_ref != source_generation.row_id
                or not isinstance(replay_context, dict)
                or replay_context.get("current_qc_report_id") != report.qc_report_id
                or replay_context.get("source_draft_row_id") != source_generation.row_id
                or replay_context.get("source_bundle_id") != bundle["bundle_id"]
            ):
                raise checkpoint_corrupt(f"soft QC{round_index} human-review event is misbound")
        return decision

    def _load_soft_qc0_branch_control(
        self, decision: SoftQcDecision
    ) -> tuple[bool, str | None]:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        control = refs.get("soft_qc0_control")
        # 存的一边按同一张分支表写；唯一没记下的输入是那时还能不能花钱，所以只能是两种之一
        if (
            not isinstance(control, dict)
            or self._json_hash(control) != self._checkpoint_hash("soft_qc0_control")
            or not is_derivable_control(control, partial(soft_qc0_control, decision.branch))
        ):
            raise checkpoint_corrupt("soft QC0 branch control is invalid")
        return control["patch_allowed"], control["skip_reason"]

    def _load_soft_qc_checkpoint(
        self,
        scene_id: str,
        *,
        selected_style_generation: StyleGenerationResult | None = None,
    ) -> tuple[SoftQcDecision, StyleGenerationResult]:
        report_id = self._checkpoint_artifact(
            "soft_qc_report_id",
            expected_node_at_least="soft_qc_ready",
        )
        report = self._require_checkpoint_row(QcReport, report_id)
        if report.scene_id != scene_id or report.qc_type != "soft_qc":
            raise checkpoint_corrupt("soft QC checkpoint identity mismatch")

        row_id = self._checkpoint_artifact(
            "soft_final_draft_row_id",
            expected_node_at_least="soft_qc_ready",
        )
        draft = self._require_checkpoint_row(SceneDraft, row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        soft_qc_execution_id = self._validate_artifact_execution_owner(
            refs.get("soft_qc_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=refs.get("soft_qc_llm_call_id"),
            execution_step_key=refs.get("soft_qc_execution_step_key"),
            execution_id=soft_qc_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        final_llm_call_id = refs.get("soft_final_llm_call_id")
        final_execution_step_key = refs.get("soft_final_execution_step_key")
        final_artifact_execution_id = self._validate_artifact_execution_owner(
            refs.get("soft_final_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=final_llm_call_id,
            execution_step_key=final_execution_step_key,
            execution_id=final_artifact_execution_id,
        )
        if (
            draft.scene_id != scene_id
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != final_llm_call_id
            or self._text_hash(draft.content)
            != self._checkpoint_hash("soft_final_draft")
            or report.source_draft_row_id != refs.get("soft_qc_source_draft_row_id")
            or report.source_draft_row_id != draft.row_id
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get("soft_qc_bundle_id") != bundle["bundle_id"]
        ):
            raise checkpoint_corrupt("soft QC draft identity/hash mismatch")

        decision = SoftQcDecision(
            branch=str(
                refs.get("soft_qc_branch") or payload.get("branch") or "continue"
            ),
            qc_report_id=report.qc_report_id,
            human_review_event_id=refs.get("soft_qc_human_review_event_id"),
            resolution_code=str(
                refs.get("soft_qc_resolution_code") or report.resolution_code or ""
            ),
            next_action=str(
                refs.get("soft_qc_next_action") or report.next_action or ""
            ),
            should_continue=str(refs.get("soft_qc_branch") or payload.get("branch"))
            in {"continue", "waive"},
            stop_reason=refs.get("soft_qc_stop_reason"),
            llm_call_id=refs.get("soft_qc_llm_call_id"),
            execution_step_key=refs.get("soft_qc_execution_step_key"),
        )
        decision_summary = {
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "human_review_event_id": decision.human_review_event_id,
            "resolution_code": decision.resolution_code,
            "next_action": decision.next_action,
            "stop_reason": decision.stop_reason,
            "llm_call_id": decision.llm_call_id,
            "execution_step_key": decision.execution_step_key,
        }
        if self._json_hash(decision_summary) != self._checkpoint_hash(
            "soft_qc_decision"
        ):
            raise checkpoint_corrupt("soft QC decision hash mismatch")
        if self._json_hash(qc_report_snapshot(report)) != self._checkpoint_hash(
            "soft_qc_report"
        ):
            raise checkpoint_corrupt("soft QC report hash mismatch")
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="soft_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=draft.row_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        generation = StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=final_llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=final_execution_step_key,
            artifact_execution_id=final_artifact_execution_id,
        )
        # 收尾必有前缀链：第 0 轮（与它的分支控制）、需要时补丁与第 1 轮，逐项复验
        if selected_style_generation is None:
            raise checkpoint_corrupt(
                "soft checkpoint prefix validation requires the selected style source",
            )
        soft_input = self._load_soft_draft_checkpoint(
            scene_id,
            prefix="soft_input",
            expected_source_draft_row_id=selected_style_generation.row_id,
            expected_stages=SOFT_INPUT_STAGES,
        )
        qc0 = self._load_soft_qc_round_checkpoint(
            scene_id,
            round_index=0,
            source_generation=soft_input,
        )
        patch_allowed, skip_reason = self._load_soft_qc0_branch_control(qc0)
        final_qc_round = refs.get("soft_final_qc_round")
        expected_skip_reason = skip_reason if final_qc_round == 0 else None
        if final_qc_round == 1:
            if (
                qc0.branch != "patch"
                or not patch_allowed
                or skip_reason is not None
            ):
                raise checkpoint_corrupt("soft QC1 completion is not reachable from its QC0 branch")
            patch_generation = self._load_soft_draft_checkpoint(
                scene_id,
                prefix="soft_patch",
                expected_source_draft_row_id=soft_input.row_id,
                expected_stages={"style_patch"},
                expected_source_qc_report_id=qc0.qc_report_id,
            )
            checkpoint_decision = self._load_soft_qc_round_checkpoint(
                scene_id,
                round_index=1,
                source_generation=patch_generation,
            )
            expected_generation = patch_generation
            if refs.get("soft_completion_skip_reason") == STYLE_PATCH_REVERTED_SKIP_REASON:
                # 风格参考 v3（P5b）：补丁被退回——补丁与补丁后那一轮评审照常校验，收尾是补丁前的稿子与
                # 由补丁前那一轮评审派生的决定。
                checkpoint_decision = self._reverted_patch_decision(qc0)
                expected_generation = soft_input
                expected_skip_reason = STYLE_PATCH_REVERTED_SKIP_REASON
        elif final_qc_round == 0:
            if qc0.branch == "patch" and patch_allowed:
                raise checkpoint_corrupt("soft QC0 completion skipped an allowed patch")
            checkpoint_decision = qc0
            expected_generation = soft_input
        else:
            raise checkpoint_corrupt("soft final QC round is invalid")
        completion = refs.get("soft_completion")
        expected_completion = {
            "final_qc_round": final_qc_round,
            "skip_reason": refs.get("soft_completion_skip_reason"),
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "draft_row_id": generation.row_id,
        }
        if (
            completion != expected_completion
            or self._json_hash(completion)
            != self._checkpoint_hash("soft_completion")
            or refs.get("soft_completion_skip_reason") != expected_skip_reason
            or soft_decision_snapshot(
                checkpoint_decision, include_should_continue=False
            )
            != soft_decision_snapshot(decision, include_should_continue=False)
            or expected_generation.row_id != generation.row_id
            or expected_generation.llm_call_id != generation.llm_call_id
        ):
            raise checkpoint_corrupt("soft completion prefix/branch/hash mismatch")
        return decision, generation

    def _pov_desensitize_brief(
        self, scene: SceneCard, contract, brief: list[str]
    ) -> list[str]:
        """Wave 4（§5.6/§7.11/不变量 11）：回灌自动补丁提示词前做 POV 证据脱敏。

        引用了非 POV 已知秘密的 brief 条目不得进入自动补丁——剔除后只能走作者确认修订。
        硬 QC 自身始终读全量权威状态，不经此路径。pov 缺失或项目无秘密时无副作用；
        脱敏失败降级为原 brief（不阻断主流程），失败以 WARNING 可见。
        """
        if not brief:
            return brief
        try:
            payload = getattr(contract, "payload_json", None) or {}
            pov = scene.pov_character_id or payload.get("pov_character_id")
            if not pov:
                return brief
            project_id = self._resolve_scene_project_id(scene, contract)
            return PovKnowledgeProjection(
                self.session,
                event_log=NarrativeEventLog(self.session),
            ).redact_brief(
                brief,
                project_id,
                scene_id=scene.scene_id,
                pov_character_id=pov,
                onstage_character_ids=scene.onstage_chars_json or [],
            )
        except Exception:
            _LOGGER.warning(
                "pov brief desensitization degraded for scene %s; keeping raw brief",
                scene.scene_id,
                exc_info=True,
            )
            return brief

    def _rewrite_brief_from_report(self, qc_report_id: str) -> list[str]:
        report = self.session.get(QcReport, qc_report_id)
        if report is None:
            return []
        entries = report.rewrite_brief_json or []
        rewrite_brief: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                instruction = entry.get("instruction")
                if isinstance(instruction, str) and instruction.strip():
                    rewrite_brief.append(instruction.strip())
        return rewrite_brief

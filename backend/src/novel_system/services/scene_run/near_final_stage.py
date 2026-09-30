"""准终稿（``near_final_ready`` 子游标 0..3：评审 → 重写 → 复评 → 终稿）的驱动、重写门与检查点存取。

``Orchestrator`` 直接继承 ``NearFinalCheckpointMixin``。重写稿过门时的基础安全回退经本模块的
``assess_rewrite_regressions`` 名字取（测试在这里打桩）；``_near_final_rewrite_drift`` 测试在类上打桩。
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
import logging
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    RevisionCandidate,
    SceneCard,
    SceneDraft,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.scene_generation import StyleGenerationResult, assess_rewrite_regressions, fidelity_probe
from novel_system.services.scene_run.branch_control import is_derivable_control, near_final_eval0_control
from novel_system.services.scene_run.constants import NEAR_FINAL_REWRITE_GATE_STAGE
from novel_system.services.scene_run.near_final_gate import (
    _near_final_rejection_skip_reason,
    _near_final_rewrite_gate_summary,
    _near_final_rewrite_gate_warnings,
)
from novel_system.services.scene_run.results import near_evaluation_payload, near_final_result_payload
from novel_system.services.scene_run.snapshots import revision_candidate_snapshot, writer_evaluation_snapshot
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt
from novel_system.services.style_policy import style_policy_for_bundle

_LOGGER = logging.getLogger(__name__)


class NearFinalCheckpointMixin:
    def _writer_evaluation_llm_call_id(self, evaluation_id: Any) -> str | None:
        row = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        return row.evaluator_llm_call_id if row is not None else None

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
        # 两稿各读一次（在保存点里读，失败只回滚保存点）——与去模板改写的「越改越远」同一个探针（B02-05）
        drift = fidelity_probe.rewrite_drift(
            self.session,
            policy_or_bundle=policy,
            source_content=source_generation.content,
            rewritten_content=rewrite_generation.content,
        )
        if not drift.get("comparable"):
            return None
        before, after = drift["source"], drift["rewritten"]
        tolerance = float(drift["max_distance_increase"])
        return {
            "moved_away": bool(after["distance"] > before["distance"] + tolerance),
            "source_distance": round(before["distance"], 4),
            "rewrite_distance": round(after["distance"], 4),
            "source_percentile": round(before["percentile"], 1),
            "rewrite_percentile": round(after["percentile"], 1),
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
            control = near_final_eval0_control(
                eval0,
                spend_allowed=rewrite_requested and optional_spend_allowed(),
            )
            if control["branch"] == "rewrite_skipped":
                _LOGGER.warning(
                    "near-final rewrite skipped for scene %s (budget/candidate cap)",
                    scene.scene_id,
                )
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
        # 检查点格式 v2：修订候选的快照（带整份来源稿）只记哈希；v1 还把快照本身存在 {prefix}_candidate_snapshot
        return (
            {f"{prefix}_revision_candidate_id": candidate_id},
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
        # v1（格式 v2 上线前写下的检查点）还存着快照本身，逐字段比；v2 只有哈希
        snapshot_stored = f"{prefix}_candidate_snapshot" in refs
        candidate_snapshot = refs.get(f"{prefix}_candidate_snapshot")
        live_snapshot = None
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
                or (
                    snapshot_stored
                    and revision_candidate_snapshot(candidate) != candidate_snapshot
                )
            ):
                raise checkpoint_corrupt(f"near-final evaluation {round_index} candidate is misbound")
            live_snapshot = revision_candidate_snapshot(candidate)
        if self._json_hash(candidate_snapshot if snapshot_stored else live_snapshot) != self._checkpoint_hash(
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
        if not isinstance(control, dict) or self._json_hash(control) != self._checkpoint_hash(
            "near_eval0_control"
        ):
            raise checkpoint_corrupt("near-final eval0 branch control is invalid")
        # 存的一边按同一张分支表写；唯一没记下的输入是那时还能不能花钱，所以只能是两种之一
        if not is_derivable_control(control, partial(near_final_eval0_control, eval0)):
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

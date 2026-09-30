"""归档尾段：``near_final_ready`` 的子游标 4..11 与最后的 ``archived`` 节点。

``Orchestrator`` 直接继承 ``ArchiveCheckpointMixin``：驱动（``_archive_near_final_checkpoint``）、各步的产品、
续跑时的逐步复验与恢复、归档清单。检查点节点键、步骤键、子游标、``artifact_refs`` / ``artifact_hashes`` 键与
``RUN_CHECKPOINT_CORRUPT`` 的校验语义一字不变。

方法之间一律 ``self.X`` 互调：测试在编排器实例上覆盖某一步（``_run_archive_chapter_evaluation``、
``_archive_product``、``_archive_manifest`` …）或类上打桩，驱动与复验照样看得到。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    FinalScene,
    LlmCall,
    NarrativeEvent,
    SceneCard,
    SceneMemory,
    SceneRunState,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.services.llm_accounting import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    LLMAccountingError,
    LLMCallContext,
    validate_product_call,
)
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.scene_run.context import ArchiveInputs
from novel_system.services.scene_run.results import apply_finality, merged_warnings, qc_decision_payload
from novel_system.services.scene_run.snapshots import (
    archive_attempt_snapshot,
    archive_final_scene_snapshot,
    archive_rolling_note_snapshot,
    archive_scene_memory_snapshot,
    archive_writer_evaluation_snapshot,
    chapter_memory_snapshot,
    narrative_event_snapshot,
    volume_snapshot,
)
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveStage:
    """归档尾段的一步（``near_final_ready`` 的子游标）。归档清单、驱动与前缀复验都照 :data:`ARCHIVE_STAGES` 走。

    ``kind`` / ``hash_key`` 是产品种类与它在 ``artifact_refs`` / ``artifact_hashes`` 里的键（清单记的就是这两样，
    都已持久化，不能改）。第 7..11 步只存一份产品：``run`` 是产出它的方法名（``(scene, final_scene) -> product``，
    测试在实例或类上覆盖它来注入故障），``validate`` 是复验它的方法名。第 4..6 步连着别的行一起写，驱动里各有
    一个方法（``_archive_stage_core`` / ``_archive_stage_rule_events`` / ``_archive_stage_prose``）。
    """

    sub_index: int
    kind: str
    hash_key: str
    run: str | None = None
    validate: str | None = None
    validate_takes_final_scene: bool = False


ARCHIVE_STAGES: tuple[ArchiveStage, ...] = (
    ArchiveStage(4, "core_archive", "archive_core"),
    ArchiveStage(5, "rule_events", "archive_rule_product"),
    ArchiveStage(6, "prose_extraction", "archive_prose_product"),
    ArchiveStage(
        7,
        "vector_index",
        "archive_vector_product",
        run="_run_archive_vector_index",
        validate="_validate_archive_vector_product",
        validate_takes_final_scene=True,
    ),
    ArchiveStage(
        8,
        "chapter_aggregate",
        "archive_chapter_product",
        run="_run_archive_chapter_aggregate",
        validate="_validate_archive_chapter_product",
    ),
    ArchiveStage(
        9,
        "volume_aggregate",
        "archive_volume_product",
        run="_run_archive_volume_aggregate",
        validate="_validate_archive_volume_product",
    ),
    ArchiveStage(
        10,
        "chapter_near_final",
        "archive_chapter_evaluation_product",
        run="_run_archive_chapter_evaluation",
        validate="_validate_archive_chapter_evaluation_product",
    ),
    ArchiveStage(
        11,
        "style_drift",
        "archive_drift_product",
        run="_run_archive_style_reading",
        validate="_validate_archive_drift_product",
    ),
)
# 只存一份产品、驱动按表逐步走的那几步（7..11）
ARCHIVE_PRODUCT_STAGES: tuple[ArchiveStage, ...] = tuple(
    stage for stage in ARCHIVE_STAGES if stage.run is not None
)


class ArchiveCheckpointMixin:
    def _archive_near_final_checkpoint(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        contract,
        bundle: dict[str, Any],
        hard_qc_payload: dict[str, Any],
        planning,
        candidate_summaries: list[dict[str, Any]] | None,
        run_policy: str,
        in_process: ArchiveInputs | None = None,
    ) -> dict[str, Any]:
        """归档尾段的驱动：从子游标停下的地方接着做 4..11，每步「产出 → 自检 → 存检查点」；最后写 ``archived`` 与
        归档清单，装配运行结果。

        只在续跑时复验（B01-11、B03-07）：进来时已经存下的归档产品逐步复验一遍；同一进程里新做的产品存之前就核对
        过，不再回读。``in_process`` 是同一进程刚做完的软 QC 决定、终稿行与准终稿 payload（全新的一次运行从
        ``_finalize_after_style`` 直接交进来）；没有就从检查点读回并复验（续跑）。
        """
        scene_id = scene.scene_id
        if in_process is None:
            selected_style = self._load_selected_style_checkpoint(scene_id)
            soft_qc, soft_generation = self._load_soft_qc_checkpoint(
                scene_id,
                selected_style_generation=selected_style,
            )
            final_scene, near_final_payload = self._load_near_final_checkpoint(
                scene=scene,
                bundle=bundle,
                source_generation=soft_generation,
            )
        else:
            soft_qc = in_process.soft_qc
            final_scene = in_process.final_scene
            near_final_payload = in_process.near_final_payload
        state_payload = state.run_checkpoint_json or {}
        refs = state_payload.get("artifact_refs") or {}
        carry_notes = list(refs.get("carry_notes") or [])
        if self._json_hash(carry_notes) != self._checkpoint_hash("carry_notes"):
            raise checkpoint_corrupt("near-final carry notes hash mismatch")
        entry = self._near_final_checkpoint_progress()
        if entry >= 4:
            # 续跑：停下之前已经存下的产品逐步复验
            archive_result = self._validate_archive_prefix(
                scene=scene,
                contract=contract,
                final_scene=final_scene,
                carry_notes=carry_notes,
                through=entry,
            )
        else:
            archive_result = self._archive_stage_core(
                scene, final_scene, soft_qc=soft_qc, carry_notes=carry_notes
            )
        if entry < 5:
            self._archive_stage_rule_events(scene, contract, final_scene)
        if entry < 6:
            self._archive_stage_prose(scene, contract, final_scene)
        self._stage_archive_prose_extraction(state, final_scene)

        chapter_near_final = None
        for stage in ARCHIVE_PRODUCT_STAGES:
            if entry < stage.sub_index:
                product = getattr(self, stage.run)(scene, final_scene)
                self._validate_archive_stage_product(
                    stage, scene, final_scene, product, require_checkpoint_hash=False
                )
                self._save_run_checkpoint(
                    "near_final_ready",
                    sub_index=stage.sub_index,
                    artifact_refs={stage.hash_key: product},
                    artifact_hashes={stage.hash_key: self._json_hash(product)},
                )
            else:
                # 进来时就存下了，上面的前缀复验核对过
                product = self._archive_checkpoint_ref(stage.hash_key) or {}
            if stage.kind == "chapter_near_final" and product.get("outcome") == "evaluated":
                chapter_near_final = product.get("evaluation")

        manifest = self._archive_manifest()
        state.scene_status = "archived"
        self._save_run_checkpoint(
            "archived",
            artifact_refs={
                "final_scene_row_id": final_scene.row_id,
                "scene_memory_row_id": archive_result.get("scene_memory_row_id"),
                "archive_manifest": manifest,
            },
            artifact_hashes={
                "final_scene": self._text_hash(final_scene.content),
                "archive_manifest": self._json_hash(manifest),
            },
        )

        near_final_warnings = self._near_final_warning_findings(near_final_payload)
        result = self._with_author_projection(
            scene_id,
            state,
            {
                "scene_status": state.scene_status,
                "current_bundle_id": bundle["bundle_id"],
                "current_bundle_hash": bundle["bundle_snapshot_hash"],
                "current_final_scene_row_id": final_scene.row_id,
                "current_qc_report_id": state.current_qc_report_id,
                "current_human_review_event_id": state.current_human_review_event_id,
                "hard_qc": hard_qc_payload,
                "soft_qc": qc_decision_payload(soft_qc),
                "planning": planning,
                "near_final": near_final_payload,
                "chapter_near_final": chapter_near_final,
                "style_candidates": candidate_summaries,
                "run_policy": run_policy,
            },
        )
        result["quality_warnings"] = merged_warnings(
            result.get("quality_warnings"), near_final_warnings
        )
        archive_attempt = self.session.get(
            AttemptTracker, archive_result.get("archive_attempt_id")
        )
        gate_summary = (
            (archive_attempt.details_json or {}).get("final_text_gate")
            if archive_attempt is not None
            else {}
        )
        apply_finality(
            result, gate_summary=gate_summary, warnings=near_final_warnings
        )
        if near_final_warnings and "author_review_optional_fix" not in (
            result.get("recommended_actions") or []
        ):
            result["recommended_actions"] = [
                *(result.get("recommended_actions") or []),
                "author_review_optional_fix",
            ]
        return result

    def _archive_stage_core(
        self,
        scene: SceneCard,
        final_scene: FinalScene,
        *,
        soft_qc,
        carry_notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """归档第 4 步：终稿归档（SceneMemory / 章滚动笔记 / 归档尝试），四份行快照随产品一起存；返回核对过的
        三个行 id（与续跑时 ``_validate_archive_core_checkpoint`` 读回的一样）。"""
        archive_result = self.archiver.archive_final_scene(
            scene.scene_id,
            final_scene.row_id,
            qc_report_id=soft_qc.qc_report_id,
            carry_notes_json=carry_notes,
            execution_id=self._execution_id,
            finalize_scene_status=False,
            # 检查点在 progress < 11 时自己在 archive:style_drift:0 槽位里记读数
            record_fidelity_reading=False,
        )
        archive_core_product = self._archive_product(
            scene=scene,
            kind="core_archive",
            outcome="completed",
            step_key="archive:core:0",
            input_hash=self._text_hash(final_scene.content),
            final_scene_row_id=final_scene.row_id,
            scene_memory_row_id=archive_result["scene_memory_row_id"],
            chapter_rolling_note_row_id=archive_result[
                "chapter_rolling_note_row_id"
            ],
            archive_attempt_id=archive_result["archive_attempt_id"],
            final_scene_snapshot=archive_final_scene_snapshot(final_scene),
            scene_memory_snapshot=archive_scene_memory_snapshot(
                self.session.get(SceneMemory, archive_result["scene_memory_row_id"])
            ),
            rolling_note_snapshot=archive_rolling_note_snapshot(
                self.session.get(
                    ChapterRollingNote,
                    archive_result["chapter_rolling_note_row_id"],
                )
            ),
            archive_attempt_snapshot=archive_attempt_snapshot(
                self.session.get(
                    AttemptTracker,
                    archive_result["archive_attempt_id"],
                )
            ),
        )
        validated = self._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            product=archive_core_product,
            require_checkpoint_hash=False,
        )
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=4,
            artifact_refs={
                "scene_memory_row_id": archive_result["scene_memory_row_id"],
                "archive_core": archive_core_product,
                "archive_final_scene_snapshot": archive_core_product[
                    "final_scene_snapshot"
                ],
                "archive_scene_memory_snapshot": archive_core_product[
                    "scene_memory_snapshot"
                ],
                "archive_rolling_note_snapshot": archive_core_product[
                    "rolling_note_snapshot"
                ],
                "archive_attempt_snapshot": archive_core_product[
                    "archive_attempt_snapshot"
                ],
            },
            artifact_hashes={
                "archive_core": self._json_hash(archive_core_product),
                "archive_final_scene_snapshot": self._json_hash(
                    archive_core_product["final_scene_snapshot"]
                ),
                "archive_scene_memory_snapshot": self._json_hash(
                    archive_core_product["scene_memory_snapshot"]
                ),
                "archive_rolling_note_snapshot": self._json_hash(
                    archive_core_product["rolling_note_snapshot"]
                ),
                "archive_attempt_snapshot": self._json_hash(
                    archive_core_product["archive_attempt_snapshot"]
                ),
            },
        )
        return validated

    def _archive_stage_rule_events(
        self, scene: SceneCard, contract, final_scene: FinalScene
    ) -> None:
        """归档第 5 步：规则事件（每条事件的 payload 记上归档执行 id / 步位 / 序号）。"""
        rule_event_ids = (
            self._record_narrative_events(
                scene,
                contract,
                final_scene.content,
                include_prose=False,
                degrade_errors=False,
                final_scene_row_id=final_scene.row_id,
            )
            or []
        )
        for ordinal, event_id in enumerate(rule_event_ids):
            event = self.session.get(NarrativeEvent, event_id)
            event.payload_json = {
                **dict(event.payload_json or {}),
                "archive_execution_id": self._execution_id,
                "archive_step_key": "archive:rule_events:0",
                "archive_ordinal": ordinal,
            }
        self.session.flush()
        rule_events = self._narrative_event_snapshots(rule_event_ids)
        rule_product = self._archive_product(
            scene=scene,
            kind="rule_events",
            outcome="recorded",
            step_key="archive:rule_events:0",
            input_hash=self._text_hash(final_scene.content),
            event_ids=rule_event_ids,
            events=rule_events,
        )
        self._validate_archive_rule_events_checkpoint(
            scene,
            product=rule_product,
            event_ids=rule_event_ids,
            events=rule_events,
            require_checkpoint_hash=False,
        )
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=5,
            artifact_refs={
                "archive_rule_event_ids": rule_event_ids,
                "archive_rule_events": rule_events,
                "archive_rule_product": rule_product,
            },
            artifact_hashes={
                "archive_rule_events": self._json_hash(rule_events),
                "archive_rule_product": self._json_hash(rule_product),
            },
        )

    def _archive_stage_prose(
        self, scene: SceneCard, contract, final_scene: FinalScene
    ) -> None:
        """归档第 6 步：正文事件抽取（可选的 LLM 节点；上次被本地拒掉的抽取从账本恢复，不再调一次）。"""
        from novel_system.services.narrative_event_log import NarrativeEventLog

        self._reconcile_execution_step("archive:prose_event_extract:0")
        recovered_prose = self._recover_archive_prose_rejection()
        if recovered_prose is None:
            prose_result, prose_event_ids = self._record_prose_events(
                NarrativeEventLog(self.session),
                scene,
                self._archive_event_base(scene, contract),
                final_scene.content,
                final_scene_row_id=final_scene.row_id,
                return_event_ids=True,
            )
        else:
            prose_result, prose_event_ids = recovered_prose, []
        self.session.flush()
        prose_events = self._narrative_event_snapshots(prose_event_ids)
        extraction_snapshot = prose_result.product_snapshot()
        prose_product = self._archive_product(
            scene=scene,
            kind="prose_extraction",
            outcome=extraction_snapshot["outcome"],
            step_key="archive:prose_event_extract:0",
            input_hash=self._text_hash(final_scene.content),
            extraction=extraction_snapshot,
            event_ids=prose_event_ids,
            events=prose_events,
        )
        if prose_result.llm_call_id is not None:
            prose_parent = self.session.get(LlmCall, prose_result.llm_call_id)
            if prose_parent is None:
                raise LLMAccountingError(
                    "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                    "prose extraction product parent disappeared before archive checkpoint",
                )
            prose_parent.response_payload_summary = sanitize_audit_summary(
                {
                    **dict(prose_parent.response_payload_summary or {}),
                    "archive_prose_product_hash": self._json_hash(prose_product),
                }
            )
        self.session.flush()
        self._validate_archive_prose_checkpoint(
            scene,
            contract,
            product=prose_product,
            event_ids=prose_event_ids,
            events=prose_events,
            require_checkpoint_hash=False,
        )
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=6,
            artifact_refs={
                "archive_prose_product": prose_product,
                "archive_prose_event_ids": prose_event_ids,
                "archive_prose_events": prose_events,
            },
            artifact_hashes={
                "archive_prose_product": self._json_hash(prose_product),
                "archive_prose_events": self._json_hash(prose_events),
            },
        )

    def _stage_archive_prose_extraction(
        self, state: SceneRunState, final_scene: FinalScene
    ) -> None:
        """检查点里的抽取结果只是候选：交给正史复核台账（待定的行在重放里看不见）。每次走过第 6 步都做（幂等）。"""
        prose_checkpoint = dict(
            ((state.run_checkpoint_json or {}).get("artifact_refs") or {}).get(
                "archive_prose_product"
            )
            or {}
        )
        extraction_checkpoint = dict(prose_checkpoint.get("extraction") or {})
        from novel_system.services.canon_continuity import CanonContinuityService

        CanonContinuityService(self.session).stage_extraction(
            final_scene.row_id,
            outcome=str(extraction_checkpoint.get("outcome") or "not_invoked"),
            event_ids=list(prose_checkpoint.get("event_ids") or []),
            reason=extraction_checkpoint.get("reason"),
            error_code=extraction_checkpoint.get("error_code"),
        )

    def _validate_archive_stage_product(
        self,
        stage: ArchiveStage,
        scene: SceneCard,
        final_scene: FinalScene,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        """按表复验第 7..11 步的一份产品（``product`` 为空就读检查点里的那份）。"""
        validate = getattr(self, stage.validate)
        if stage.validate_takes_final_scene:
            return validate(
                scene,
                final_scene,
                product,
                require_checkpoint_hash=require_checkpoint_hash,
            )
        return validate(scene, product, require_checkpoint_hash=require_checkpoint_hash)

    def _run_archive_style_reading(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        """归档第 11 步：归档终稿的「像不像」读数。风格参考 v3 删了漂移驾驶；这个槽位的 kind / 步位键 / 哈希键保持原名，
        已持久化的检查点照常续跑。"""
        drift_result = self._record_archive_fidelity_reading(scene)
        return self._archive_product(
            scene=scene,
            kind="style_drift",
            outcome=drift_result["outcome"],
            step_key="archive:style_drift:0",
            input_hash=self._text_hash(final_scene.content),
            **{key: value for key, value in drift_result.items() if key != "outcome"},
        )

    def _near_final_checkpoint_progress(self) -> int:
        if self._execution_id is None or self._checkpoint_service is None:
            return -1
        return self._sub_checkpoint_progress(
            "near_final_ready",
            last_sub_index=11,
            legacy_complete=lambda refs: bool(refs.get("final_scene_row_id")),
            legacy_sub_index=3,
            invalid_message="near-final checkpoint sub-index is invalid",
        )

    def _archive_product(
        self,
        *,
        scene: SceneCard,
        kind: str,
        outcome: str,
        step_key: str,
        input_hash: str,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": kind,
            "outcome": outcome,
            "execution_id": self._execution_id,
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "step_key": step_key,
            "input_hash": input_hash,
            **details,
        }

    def _validate_archive_core_checkpoint(
        self,
        *,
        scene: SceneCard,
        final_scene: FinalScene,
        carry_notes: list[dict[str, Any]],
        allow_terminal: bool = False,
        product: dict[str, Any] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        product = product or refs.get("archive_core")
        if (
            not isinstance(product, dict)
            or set(product)
            != {
                "schema_version",
                "kind",
                "outcome",
                "execution_id",
                "scene_id",
                "chapter_id",
                "step_key",
                "input_hash",
                "final_scene_row_id",
                "scene_memory_row_id",
                "chapter_rolling_note_row_id",
                "archive_attempt_id",
                "final_scene_snapshot",
                "scene_memory_snapshot",
                "rolling_note_snapshot",
                "archive_attempt_snapshot",
            }
            or product.get("schema_version") != 1
            or product.get("kind") != "core_archive"
            or product.get("outcome") != "completed"
            or product.get("execution_id") != self._execution_id
            or product.get("scene_id") != scene.scene_id
            or product.get("chapter_id") != scene.chapter_id
            or product.get("step_key") != "archive:core:0"
            or product.get("input_hash") != self._text_hash(final_scene.content)
            or product.get("final_scene_row_id") != final_scene.row_id
            or (
                require_checkpoint_hash
                and self._json_hash(product) != self._checkpoint_hash("archive_core")
            )
        ):
            raise checkpoint_corrupt("archive core checkpoint product schema/owner/hash is invalid")
        memory = self.session.get(SceneMemory, product["scene_memory_row_id"])
        rolling = self.session.get(
            ChapterRollingNote,
            product["chapter_rolling_note_row_id"],
        )
        attempt = self.session.get(AttemptTracker, product["archive_attempt_id"])
        state = self._active_checkpoint_state()
        snapshot_refs = {
            "final_scene_snapshot": refs.get("archive_final_scene_snapshot"),
            "scene_memory_snapshot": refs.get("archive_scene_memory_snapshot"),
            "rolling_note_snapshot": refs.get("archive_rolling_note_snapshot"),
            "archive_attempt_snapshot": refs.get("archive_attempt_snapshot"),
        }
        if require_checkpoint_hash and any(
            snapshot_refs[key] != product.get(key)
            or self._json_hash(snapshot_refs[key])
            != self._checkpoint_hash(
                {
                    "final_scene_snapshot": "archive_final_scene_snapshot",
                    "scene_memory_snapshot": "archive_scene_memory_snapshot",
                    "rolling_note_snapshot": "archive_rolling_note_snapshot",
                    "archive_attempt_snapshot": "archive_attempt_snapshot",
                }[key]
            )
            for key in snapshot_refs
        ):
            raise checkpoint_corrupt("archive core independent snapshot hashes are invalid")
        if memory is None or rolling is None or attempt is None:
            self._raise_checkpoint_output_missing(
                row_id=(
                    product["scene_memory_row_id"]
                    if memory is None
                    else (
                        product["chapter_rolling_note_row_id"]
                        if rolling is None
                        else str(product["archive_attempt_id"])
                    )
                )
            )
        if (
            product.get("final_scene_snapshot")
            != archive_final_scene_snapshot(final_scene)
            or product.get("scene_memory_snapshot")
            != archive_scene_memory_snapshot(memory)
            or product.get("rolling_note_snapshot")
            != archive_rolling_note_snapshot(rolling)
            or product.get("archive_attempt_snapshot")
            != archive_attempt_snapshot(attempt)
            or final_scene.status != "archived"
            or (
                state.scene_status != "archived"
                if allow_terminal
                else state.scene_status == "archived"
            )
            or state.current_final_scene_row_id != final_scene.row_id
            or memory.scene_id != scene.scene_id
            or memory.chapter_id != scene.chapter_id
            or memory.final_scene_row_id != final_scene.row_id
            or memory.source_bundle_id != final_scene.source_bundle_id
            or memory.content != final_scene.content
            or memory.carry_notes_json != carry_notes
            or memory.active_flag != 1
            or memory.runtime_eligible != 1
            or rolling.scene_id != scene.scene_id
            or rolling.chapter_id != scene.chapter_id
            or rolling.source_scene_memory_row_id != memory.row_id
            or rolling.note_text != final_scene.content
            or attempt.scene_id != scene.scene_id
            or attempt.chapter_id != scene.chapter_id
            or attempt.step != "archive"
            or attempt.status != "completed"
            or attempt.source_bundle_id != final_scene.source_bundle_id
            or (attempt.details_json or {}).get("final_scene_row_id")
            != final_scene.row_id
            or (attempt.details_json or {}).get("execution_id") != self._execution_id
        ):
            raise checkpoint_corrupt("archive core checkpoint product graph is inconsistent")
        return {
            "scene_memory_row_id": memory.row_id,
            "chapter_rolling_note_row_id": rolling.row_id,
            "archive_attempt_id": attempt.attempt_id,
            "scene_status": state.scene_status,
        }

    def _narrative_event_snapshots(self, event_ids: list[str]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for event_id in event_ids:
            event = self.session.get(NarrativeEvent, event_id)
            if event is None:
                self._raise_checkpoint_output_missing(row_id=event_id)
            snapshots.append(narrative_event_snapshot(event))
        return snapshots

    def _validate_archive_rule_events_checkpoint(
        self,
        scene: SceneCard,
        *,
        product: dict[str, Any] | None = None,
        event_ids: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> None:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        event_ids = (
            event_ids if event_ids is not None else refs.get("archive_rule_event_ids")
        )
        events = events if events is not None else refs.get("archive_rule_events")
        product = product if product is not None else refs.get("archive_rule_product")
        final_scene = self.session.get(FinalScene, refs.get("final_scene_row_id"))
        expected_product = (
            self._archive_product(
                scene=scene,
                kind="rule_events",
                outcome="recorded",
                step_key="archive:rule_events:0",
                input_hash=self._text_hash(final_scene.content),
                event_ids=event_ids,
                events=events,
            )
            if final_scene is not None
            else None
        )
        if (
            not isinstance(event_ids, list)
            or any(
                not isinstance(event_id, str) or not event_id for event_id in event_ids
            )
            or len(event_ids) != len(set(event_ids))
            or not isinstance(events, list)
            or not isinstance(product, dict)
            or product != expected_product
            or (
                require_checkpoint_hash
                and self._json_hash(events)
                != self._checkpoint_hash("archive_rule_events")
            )
            or (
                require_checkpoint_hash
                and self._json_hash(product)
                != self._checkpoint_hash("archive_rule_product")
            )
        ):
            raise checkpoint_corrupt("archive rule-event checkpoint schema/owner/hash is invalid")
        actual = self._narrative_event_snapshots(event_ids)
        if actual != events or any(
            event.get("scene_id") != scene.scene_id
            or event.get("chapter_id") != scene.chapter_id
            or event.get("confidence") != "high"
            or (event.get("payload_json") or {}).get("source") == "prose"
            or (event.get("payload_json") or {}).get("archive_execution_id")
            != self._execution_id
            or (event.get("payload_json") or {}).get("archive_step_key")
            != "archive:rule_events:0"
            or (event.get("payload_json") or {}).get("archive_ordinal") != ordinal
            for ordinal, event in enumerate(actual)
        ):
            raise checkpoint_corrupt("archive rule-event checkpoint rows are missing, detached, or changed")

    def _validate_archive_prose_checkpoint(
        self,
        scene: SceneCard,
        contract,
        *,
        product: dict[str, Any] | None = None,
        event_ids: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> None:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        product = product if product is not None else refs.get("archive_prose_product")
        event_ids = (
            event_ids if event_ids is not None else refs.get("archive_prose_event_ids")
        )
        events = events if events is not None else refs.get("archive_prose_events")
        final_scene = self.session.get(FinalScene, refs.get("final_scene_row_id"))
        extraction = product.get("extraction") if isinstance(product, dict) else None
        if (
            final_scene is None
            or not isinstance(product, dict)
            or not isinstance(extraction, dict)
            or product
            != self._archive_product(
                scene=scene,
                kind="prose_extraction",
                outcome=extraction.get("outcome"),
                step_key="archive:prose_event_extract:0",
                input_hash=self._text_hash(final_scene.content),
                extraction=extraction,
                event_ids=event_ids,
                events=events,
            )
            or not isinstance(event_ids, list)
            or any(
                not isinstance(event_id, str) or not event_id for event_id in event_ids
            )
            or len(event_ids) != len(set(event_ids))
            or not isinstance(events, list)
            or (
                require_checkpoint_hash
                and self._json_hash(product)
                != self._checkpoint_hash("archive_prose_product")
            )
            or (
                require_checkpoint_hash
                and self._json_hash(events)
                != self._checkpoint_hash("archive_prose_events")
            )
        ):
            raise checkpoint_corrupt("archive prose-extraction checkpoint schema/owner/hash is invalid")

        expected_extraction_fields = {
            "schema_version",
            "outcome",
            "events",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "reason",
            "error_code",
        }
        outcome = extraction.get("outcome")
        if (
            set(extraction) != expected_extraction_fields
            or extraction.get("schema_version") != 1
            or outcome
            not in {
                "not_invoked",
                "rejected_before_dispatch",
                "provider_failed",
                "parse_failed",
                "completed_empty",
                "completed_events",
            }
            or not self._checkpoint_execution_owner_matches(
                extraction.get("execution_id"), extraction.get("run_job_id")
            )
            or extraction.get("execution_step_key") != "archive:prose_event_extract:0"
            or not isinstance(extraction.get("events"), list)
        ):
            raise checkpoint_corrupt("archive prose-extraction product field matrix is invalid")
        call_id = extraction.get("llm_call_id")
        if outcome == "not_invoked":
            if call_id is not None or extraction.get("error_code") is not None:
                raise checkpoint_corrupt("archive prose no-call product has a parent/error")
            ledger = (
                self.session.execute(
                    select(LlmCall).where(
                        LlmCall.execution_id == self._execution_id,
                        LlmCall.execution_step_key == "archive:prose_event_extract:0",
                    )
                )
                .scalars()
                .all()
            )
            if ledger:
                raise checkpoint_corrupt("archive prose no-call product unexpectedly has a ledger")
        else:
            if not isinstance(call_id, str) or not call_id:
                raise checkpoint_corrupt("archive prose called product has no parent id")
            parent = self.session.get(LlmCall, call_id)
            base = self._archive_event_base(scene, contract)
            context = LLMCallContext(
                scope_type="scene",
                scope_id=scene.scene_id,
                project_id=base["project_id"],
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                node_id="extraction",
                step="archive:prose_event_extract:0",
                execution_id=self._execution_id,
                execution_step_key="archive:prose_event_extract:0",
                run_job_id=self._run_job_id,
                provider_execution_mode="online",
            )
            expected_outcome = {
                "completed_empty": "completed",
                "completed_events": "completed",
                "parse_failed": "parse_failed",
                "provider_failed": "provider_failed",
                "rejected_before_dispatch": "rejected_before_dispatch",
            }[outcome]
            try:
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome=expected_outcome,
                    expected_error_code=(
                        extraction.get("error_code")
                        if expected_outcome
                        in {"provider_failed", "rejected_before_dispatch"}
                        else None
                    ),
                )
            except LLMAccountingError as exc:
                raise checkpoint_corrupt(
                    "archive prose parent/attempt ledger is invalid",
                    details={"llm_call_id": call_id, "error_code": exc.code},
                ) from exc
            if not isinstance(
                parent.response_payload_summary, dict
            ) or parent.response_payload_summary.get(
                "archive_prose_product_hash"
            ) != self._json_hash(
                product
            ):
                raise checkpoint_corrupt("archive prose product hash is detached from its parent")
            if outcome in {"completed_empty", "completed_events"}:
                from novel_system.services.prose_event_extractor import (
                    prose_extraction_parsed_hash,
                )

                if parent.response_payload_summary.get(
                    "prose_extraction_parsed_hash"
                ) != prose_extraction_parsed_hash(extraction.get("events") or []):
                    raise checkpoint_corrupt("archive prose parsed output hash is detached from its parent")
        actual = self._narrative_event_snapshots(event_ids)
        extracted_events = extraction.get("events") or []
        if (
            actual != events
            or len(actual) != len(extracted_events)
            or any(
                event.get("scene_id") != scene.scene_id
                or event.get("chapter_id") != scene.chapter_id
                or event.get("confidence") != "extracted"
                or (event.get("payload_json") or {}).get("source") != "prose"
                or (event.get("payload_json") or {}).get("archive_execution_id")
                != self._execution_id
                or (event.get("payload_json") or {}).get("archive_step_key")
                != "archive:prose_event_extract:0"
                or (event.get("payload_json") or {}).get("archive_ordinal") != ordinal
                or {
                    "event_type": event.get("event_type"),
                    "entity_id": event.get("entity_id"),
                    "fact_key": event.get("fact_key"),
                    "fact_value": event.get("fact_value"),
                    "evidence": (
                        event.get("source_text_excerpt") or ""
                        if extracted_events[ordinal].get("evidence")
                        else ""
                    ),
                }
                != extracted_events[ordinal]
                for ordinal, event in enumerate(actual)
            )
        ):
            raise checkpoint_corrupt("archive prose event rows are missing, detached, or changed")

    def _recover_archive_prose_rejection(self) -> ProseExtractionResult | None:
        """Restore a durable local rejection without creating a second parent call."""

        from novel_system.services.prose_event_extractor import ProseExtractionResult

        calls = list(
            self.session.scalars(
                select(LlmCall)
                .where(
                    LlmCall.scene_id == self._active_checkpoint_state().scene_id,
                    LlmCall.execution_id == self._execution_id,
                    LlmCall.execution_step_key == "archive:prose_event_extract:0",
                )
                .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
            ).all()
        )
        rejected = [
            call
            for call in calls
            if call.accounting_status == "rejected"
            and call.request_dispatched_at is None
        ]
        if not rejected:
            return None
        if len(rejected) != 1 or any(
            call is not rejected[0] and call.accounting_status != "released"
            for call in calls
        ):
            raise checkpoint_corrupt("archive prose rejected tombstone ledger is ambiguous")
        parent = rejected[0]
        if not isinstance(parent.error_code, str) or not parent.error_code:
            raise checkpoint_corrupt("archive prose rejected tombstone has no error code")
        return ProseExtractionResult(
            outcome="rejected_before_dispatch",
            llm_call_id=parent.llm_call_id,
            execution_id=self._execution_id,
            execution_step_key="archive:prose_event_extract:0",
            run_job_id=self._run_job_id,
            reason="pre_dispatch_rejection",
            error_code=parent.error_code,
        )

    def _archive_checkpoint_ref(self, key: str) -> Any:
        return (
            (self._active_checkpoint_state().run_checkpoint_json or {}).get(
                "artifact_refs", {}
            )
        ).get(key)

    def _validate_common_archive_product(
        self,
        *,
        scene: SceneCard,
        product: Any,
        kind: str,
        step_key: str,
        outcomes: set[str],
    ) -> dict[str, Any]:
        if (
            not isinstance(product, dict)
            or product.get("schema_version") != 1
            or product.get("kind") != kind
            or product.get("outcome") not in outcomes
            or product.get("execution_id") != self._execution_id
            or product.get("scene_id") != scene.scene_id
            or product.get("chapter_id") != scene.chapter_id
            or product.get("step_key") != step_key
            or not isinstance(product.get("input_hash"), str)
            or not product.get("input_hash")
        ):
            raise checkpoint_corrupt(f"archive {kind} product schema/owner is invalid")
        return product

    def _run_archive_vector_index(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        """归档第 7 步：向量索引已退役（[批准#1]，重评 R1）——起草提示里的「相似场景」段已删，这份索引没人读。
        槽位（sub_index 7、步位键、哈希键、清单条目）留着，记一份 ``retired`` 空产品，旧检查点照样续跑。"""
        return self._retired_vector_product(scene, final_scene)

    def _retired_vector_product(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        return self._archive_product(
            scene=scene,
            kind="vector_index",
            outcome="retired",
            step_key="archive:vector_index:0",
            input_hash=self._text_hash(final_scene.content),
            reason="vector_index_retired",
        )

    def _validate_archive_vector_product(
        self,
        scene: SceneCard,
        final_scene: FinalScene,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._archive_checkpoint_ref("archive_vector_product")
        product = self._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="vector_index",
            step_key="archive:vector_index:0",
            outcomes={"retired", "indexed", "already_present", "non_persistent", "failed"},
        )
        if require_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("archive_vector_product"):
            raise checkpoint_corrupt("archive vector product identity/hash is invalid")
        if product["outcome"] == "retired":
            if product != self._retired_vector_product(scene, final_scene):
                raise checkpoint_corrupt("archive vector product identity/hash is invalid")
            return product
        # 第 7 步退役之前写下的检查点：只核对产品自身的结构与它记的正文哈希，不再去碰向量库（向量库已删）。
        if (
            product.get("input_hash") != self._text_hash(final_scene.content)
            or product.get("vector_id") != scene.scene_id
            or product.get("text_hash")
            != self._text_hash((final_scene.content or "")[:600])
            or not isinstance(product.get("collection_name"), str)
            or product.get("backend") not in {"memory", "chroma"}
            or product.get("validation_scope")
            != ("process_local" if product.get("backend") == "memory" else "persistent")
            or product.get("write_status")
            not in {"indexed", "already_present", "failed"}
            or (
                product.get("backend") == "memory"
                and product.get("outcome") not in {"non_persistent", "failed"}
            )
            or (
                product.get("backend") != "memory"
                and product.get("outcome") == "non_persistent"
            )
        ):
            raise checkpoint_corrupt("archive vector product identity/hash is invalid")
        if product["outcome"] == "non_persistent":
            if product.get("error_code") is not None or product.get(
                "write_status"
            ) not in {
                "indexed",
                "already_present",
            }:
                raise checkpoint_corrupt("non-persistent vector product has invalid local write evidence")
            return product
        if product["outcome"] in {"indexed", "already_present"}:
            if (
                product.get("error_code") is not None
                or product.get("write_status") != product["outcome"]
            ):
                raise checkpoint_corrupt(
                    "persistent vector product outcome does not match its write evidence",
                )
        elif (
            not isinstance(product.get("error_code"), str)
            or product.get("write_status") != "failed"
        ):
            raise checkpoint_corrupt("archive vector failure has no stable error code")
        return product

    def _scene_memory_inputs(self, chapter_id: str) -> list[dict[str, str]]:
        memories = list(
            self.session.scalars(
                select(SceneMemory)
                .where(
                    SceneMemory.chapter_id == chapter_id,
                    SceneMemory.active_flag == 1,
                )
                .order_by(SceneMemory.row_id.asc())
            ).all()
        )
        return [
            {
                "row_id": memory.row_id,
                "scene_id": memory.scene_id,
                "chapter_id": memory.chapter_id,
                "content_hash": self._text_hash(memory.content),
            }
            for memory in memories
        ]

    def _run_archive_chapter_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._archive_product(
                scene=scene,
                kind="chapter_aggregate",
                outcome="not_applicable",
                step_key="archive:chapter_aggregate:0",
                input_hash=self._text_hash(final_scene.content),
                reason="not_chapter_last",
                inputs=[],
                result=None,
                chapter_memory=None,
            )
        inputs = self._scene_memory_inputs(scene.chapter_id)
        result = self.aggregator.run_final_aggregate(scene.chapter_id)
        self.session.flush()
        row_id = (
            result.get("chapter_memory_row_id") if isinstance(result, dict) else None
        )
        memory = (
            self.session.get(ChapterMemory, row_id) if isinstance(row_id, str) else None
        )
        return self._archive_product(
            scene=scene,
            kind="chapter_aggregate",
            outcome=("aggregated" if memory is not None else "no_op"),
            step_key="archive:chapter_aggregate:0",
            input_hash=self._json_hash(inputs),
            reason=(
                (result or {}).get("reason")
                if isinstance(result, dict)
                else "no_result"
            ),
            inputs=inputs,
            result=result,
            chapter_memory=(
                chapter_memory_snapshot(memory) if memory is not None else None
            ),
        )

    def _validate_archive_chapter_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._archive_checkpoint_ref("archive_chapter_product")
        product = self._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="chapter_aggregate",
            step_key="archive:chapter_aggregate:0",
            outcomes={"not_applicable", "aggregated", "no_op"},
        )
        if require_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("archive_chapter_product"):
            raise checkpoint_corrupt("chapter aggregate product hash mismatch")
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("inputs") != []
                or final_scene is None
                or product.get("input_hash") != self._text_hash(final_scene.content)
                or product.get("result") is not None
                or product.get("chapter_memory") is not None
            ):
                raise checkpoint_corrupt("non-final scene chapter product is invalid")
            return product
        inputs = product.get("inputs")
        if (
            not isinstance(inputs, list)
            or inputs != sorted(inputs, key=lambda item: item.get("row_id", ""))
            or product.get("input_hash") != self._json_hash(inputs)
        ):
            raise checkpoint_corrupt("chapter aggregate input manifest is invalid")
        for item in inputs:
            memory = self.session.get(
                SceneMemory, item.get("row_id") if isinstance(item, dict) else None
            )
            if memory is None:
                self._raise_checkpoint_output_missing(row_id=(item or {}).get("row_id"))
            if (
                memory.scene_id != item.get("scene_id")
                or memory.chapter_id != scene.chapter_id
                or item.get("chapter_id") != scene.chapter_id
                or self._text_hash(memory.content) != item.get("content_hash")
            ):
                raise checkpoint_corrupt("chapter aggregate input memory changed")
        snapshot = product.get("chapter_memory")
        if product.get("outcome") == "aggregated":
            memory = self.session.get(ChapterMemory, (snapshot or {}).get("row_id"))
            if memory is None:
                self._raise_checkpoint_output_missing(
                    row_id=(snapshot or {}).get("row_id")
                )
            actual = chapter_memory_snapshot(memory)
            for mutable_field in (
                "active_flag",
                "runtime_eligible",
                "runtime_eligibility_basis",
            ):
                actual[mutable_field] = snapshot.get(mutable_field)
            expected_content = "\n".join(
                self.session.get(SceneMemory, item["row_id"]).content for item in inputs
            )
            if actual != snapshot or memory.content != expected_content:
                raise checkpoint_corrupt("chapter aggregate output changed")
        elif snapshot is not None:
            raise checkpoint_corrupt("chapter no-op unexpectedly has output")
        if (
            product.get("outcome") == "no_op"
            and isinstance(product.get("result"), dict)
            and product["result"].get("status") == "created"
        ):
            raise checkpoint_corrupt("chapter aggregate created result lost its output")
        return product

    def _volume_input_memories(self, scene: SceneCard) -> list[dict[str, str]]:
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if (
            chapter is None
            or chapter.project_id is None
            or chapter.display_order is None
        ):
            return []
        chapters = list(
            self.session.scalars(
                select(ChapterGoal)
                .where(
                    ChapterGoal.project_id == chapter.project_id,
                    ChapterGoal.trashed_flag == 0,
                    ChapterGoal.display_order.isnot(None),
                    ChapterGoal.display_order <= chapter.display_order,
                )
                .order_by(ChapterGoal.display_order.desc())
                .limit(5)
            ).all()
        )
        chapter_ids = [item.chapter_id for item in reversed(chapters)]
        rows = list(
            self.session.scalars(
                select(ChapterMemory).where(
                    ChapterMemory.chapter_id.in_(chapter_ids),
                    ChapterMemory.aggregate_stage == "final",
                    ChapterMemory.active_flag == 1,
                )
            ).all()
        )
        order = {chapter_id: ordinal for ordinal, chapter_id in enumerate(chapter_ids)}
        rows.sort(key=lambda row: (order.get(row.chapter_id, 999), row.row_id))
        return [
            {
                "row_id": row.row_id,
                "chapter_id": row.chapter_id,
                "content_hash": self._text_hash(row.content),
            }
            for row in rows
        ]

    def _run_archive_volume_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._archive_product(
                scene=scene,
                kind="volume_aggregate",
                outcome="not_applicable",
                step_key="archive:volume_aggregate:0",
                input_hash=self._text_hash(final_scene.content),
                reason="not_chapter_last",
                inputs=[],
                result=None,
                volume_summary=None,
            )
        inputs = self._volume_input_memories(scene)
        try:
            result = self.aggregator.maybe_aggregate_volume(scene.chapter_id)
            row_id = (
                result.get("volume_summary_row_id")
                if isinstance(result, dict)
                else None
            )
            row = (
                self.session.get(VolumeSummary, row_id)
                if isinstance(row_id, str)
                else None
            )
            outcome = "aggregated" if row is not None else "no_op"
            error_code = None
        except Exception as exc:
            _LOGGER.warning(
                "volume aggregation degraded for chapter %s",
                scene.chapter_id,
                exc_info=True,
            )
            result, row, outcome, error_code = (
                None,
                None,
                "degraded",
                exc.__class__.__name__,
            )
        return self._archive_product(
            scene=scene,
            kind="volume_aggregate",
            outcome=outcome,
            step_key="archive:volume_aggregate:0",
            input_hash=self._json_hash(inputs),
            reason=(
                (result or {}).get("reason")
                if isinstance(result, dict)
                else ("aggregation_failed" if error_code else "no_result")
            ),
            error_code=error_code,
            inputs=inputs,
            result=result,
            volume_summary=(volume_snapshot(row) if row is not None else None),
        )

    def _validate_archive_volume_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._archive_checkpoint_ref("archive_volume_product")
        product = self._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="volume_aggregate",
            step_key="archive:volume_aggregate:0",
            outcomes={"not_applicable", "aggregated", "no_op", "degraded"},
        )
        if require_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("archive_volume_product"):
            raise checkpoint_corrupt("volume aggregate product hash mismatch")
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("inputs") != []
            ):
                raise checkpoint_corrupt("non-final scene volume product is invalid")
            if (
                final_scene is None
                or product.get("input_hash") != self._text_hash(final_scene.content)
                or product.get("result") is not None
                or product.get("volume_summary") is not None
            ):
                raise checkpoint_corrupt("non-final scene volume no-op payload is invalid")
            return product
        inputs = product.get("inputs")
        if not isinstance(inputs, list) or product.get("input_hash") != self._json_hash(
            inputs
        ):
            raise checkpoint_corrupt("volume aggregate input manifest is invalid")
        for item in inputs:
            row = self.session.get(
                ChapterMemory, item.get("row_id") if isinstance(item, dict) else None
            )
            if row is None:
                self._raise_checkpoint_output_missing(row_id=(item or {}).get("row_id"))
            if row.chapter_id != item.get("chapter_id") or self._text_hash(
                row.content
            ) != item.get("content_hash"):
                raise checkpoint_corrupt("volume aggregate input changed")
        snapshot = product.get("volume_summary")
        if product.get("outcome") == "aggregated":
            row = self.session.get(VolumeSummary, (snapshot or {}).get("row_id"))
            if row is None:
                self._raise_checkpoint_output_missing(
                    row_id=(snapshot or {}).get("row_id")
                )
            actual = volume_snapshot(row)
            for mutable_field in (
                "active_flag",
                "runtime_eligible",
                "runtime_eligibility_basis",
                "updated_at",
            ):
                actual[mutable_field] = snapshot.get(mutable_field)
            if actual != snapshot:
                raise checkpoint_corrupt("volume aggregate output changed")
        elif snapshot is not None:
            raise checkpoint_corrupt("volume non-output product has a row")
        if (
            product.get("outcome") == "no_op"
            and isinstance(product.get("result"), dict)
            and product["result"].get("status") == "created"
        ):
            raise checkpoint_corrupt("volume aggregate created result lost its output")
        if product.get("outcome") == "degraded" and not isinstance(
            product.get("error_code"), str
        ):
            raise checkpoint_corrupt("volume degraded product has no error code")
        return product

    def _run_archive_chapter_evaluation(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._archive_product(
                scene=scene,
                kind="chapter_near_final",
                outcome="not_applicable",
                step_key="archive:chapter_near_final:0",
                input_hash=self._text_hash(final_scene.content),
                reason="not_chapter_last",
                evaluation=None,
                evaluator_llm_call_id=None,
            )
        self._reconcile_execution_step(
            "archive:chapter_near_final:0",
            chapter_scope=True,
        )
        evaluation_result = self.near_final_service.evaluate_chapter(
            scene.chapter_id,
            execution_step_key="archive:chapter_near_final:0",
        )
        evaluation_id = evaluation_result.get("evaluation_id")
        row = self.session.get(WriterEvaluation, evaluation_id)
        if row is None:
            self._raise_checkpoint_output_missing(row_id=evaluation_id)
        snapshot = archive_writer_evaluation_snapshot(row)
        product = self._archive_product(
            scene=scene,
            kind="chapter_near_final",
            outcome="evaluated",
            step_key="archive:chapter_near_final:0",
            input_hash=self._json_hash(
                {
                    "chapter_product_hash": self._checkpoint_hash(
                        "archive_chapter_product"
                    ),
                    "source_text_ref": row.source_text_ref,
                }
            ),
            reason=None,
            evaluation=dict(evaluation_result),
            evaluation_row=snapshot,
            evaluator_llm_call_id=row.evaluator_llm_call_id,
        )
        parent = self.session.get(LlmCall, row.evaluator_llm_call_id)
        if parent is None:
            self._raise_checkpoint_output_missing(row_id=row.evaluator_llm_call_id)
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                "archive_chapter_near_final_product_hash": self._json_hash(product),
            }
        )
        self.session.flush()
        return product

    def _validate_archive_chapter_evaluation_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._archive_checkpoint_ref(
            "archive_chapter_evaluation_product"
        )
        product = self._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="chapter_near_final",
            step_key="archive:chapter_near_final:0",
            outcomes={"not_applicable", "evaluated"},
        )
        if require_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("archive_chapter_evaluation_product"):
            raise checkpoint_corrupt("chapter evaluation product hash mismatch")
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("evaluation") is not None
                or product.get("evaluator_llm_call_id") is not None
                or final_scene is None
                or product.get("input_hash") != self._text_hash(final_scene.content)
            ):
                raise checkpoint_corrupt("non-final scene chapter evaluation is invalid")
            return product
        snapshot = product.get("evaluation_row")
        if not isinstance(snapshot, dict) or not snapshot.get("evaluation_id"):
            raise checkpoint_corrupt(
                f"chapter evaluation product has no full row snapshot: {snapshot!r}; keys={sorted(product)!r}",
                details={"product_keys": sorted(product), "snapshot": snapshot},
            )
        row = self.session.get(WriterEvaluation, (snapshot or {}).get("evaluation_id"))
        if row is None:
            self._raise_checkpoint_output_missing(
                row_id=(snapshot or {}).get("evaluation_id")
            )
        if (
            archive_writer_evaluation_snapshot(row) != snapshot
            or row.object_type != "chapter"
            or row.object_id != scene.chapter_id
            or row.chapter_id != scene.chapter_id
            or row.scene_id is not None
            or row.evaluator_llm_call_id != product.get("evaluator_llm_call_id")
            or (product.get("evaluation") or {}).get("evaluation_id")
            != row.evaluation_id
        ):
            raise checkpoint_corrupt("chapter evaluation row is detached or changed")
        expected_input_hash = self._json_hash(
            {
                "chapter_product_hash": self._checkpoint_hash(
                    "archive_chapter_product"
                ),
                "source_text_ref": row.source_text_ref,
            }
        )
        if product.get("input_hash") != expected_input_hash:
            raise checkpoint_corrupt("chapter evaluation input hash mismatch")
        parent = self.session.get(LlmCall, row.evaluator_llm_call_id)
        if parent is None:
            self._raise_checkpoint_output_missing(row_id=row.evaluator_llm_call_id)
        execution_mode = (
            (parent.request_payload_summary or {}).get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        if execution_mode != "online":
            raise checkpoint_corrupt("chapter evaluation parent execution mode is missing or invalid")
        expected_outcome = (
            "completed"
            if parent.accounting_status == "settled"
            else (
                "rejected_before_dispatch"
                if parent.accounting_status == "rejected"
                else "provider_failed"
            )
        )
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        authoritative_project_id = chapter.project_id if chapter is not None else None
        if (
            not isinstance(authoritative_project_id, str)
            or not authoritative_project_id
            or (
                scene.project_id is not None
                and scene.project_id != authoritative_project_id
            )
        ):
            raise checkpoint_corrupt("chapter evaluation project ownership is inconsistent")
        context = LLMCallContext(
            scope_type="chapter",
            scope_id=scene.chapter_id,
            project_id=authoritative_project_id,
            chapter_id=scene.chapter_id,
            scene_id=None,
            node_id="chapter_near_final_review",
            step="chapter_near_final_review",
            execution_id=self._execution_id,
            execution_step_key="archive:chapter_near_final:0",
            run_job_id=self._run_job_id,
            provider_execution_mode=execution_mode,
        )
        try:
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=expected_outcome,
                expected_error_code=(
                    parent.error_code if expected_outcome != "completed" else None
                ),
            )
        except (LLMAccountingError, ValueError) as exc:
            raise checkpoint_corrupt("chapter evaluation parent ledger is invalid") from exc
        if (parent.response_payload_summary or {}).get(
            "archive_chapter_near_final_product_hash"
        ) != self._json_hash(product):
            raise checkpoint_corrupt("chapter evaluation hash is detached from parent")
        return product

    def _validate_archive_drift_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._archive_checkpoint_ref("archive_drift_product")
        product = self._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="style_drift",
            step_key="archive:style_drift:0",
            outcomes={
                "recorded",
                "not_applicable",
                "no_op",
                "observed",
                "degraded",
            },
        )
        if require_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("archive_drift_product"):
            raise checkpoint_corrupt("style drift product hash mismatch")
        # 风格参考 v3：这个槽位记归档读数（漂移驾驶已删）；旧检查点里的 observed / no_op 产品照常通过。
        if product.get("outcome") == "degraded" and not isinstance(
            product.get("error_code"), str
        ):
            raise checkpoint_corrupt("style drift degraded product has no error code")
        if product.get("outcome") == "recorded" and not isinstance(
            product.get("reading_id"), str
        ):
            raise checkpoint_corrupt("fidelity reading product has no reading id")
        return product

    def _archive_manifest(self) -> list[dict[str, Any]]:
        hashes = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_hashes", {}
        )
        manifest = [
            {
                "sub_index": stage.sub_index,
                "kind": stage.kind,
                "hash_key": stage.hash_key,
                "product_hash": hashes.get(stage.hash_key),
            }
            for stage in ARCHIVE_STAGES
        ]
        if any(not isinstance(entry["product_hash"], str) for entry in manifest):
            raise checkpoint_corrupt("archive manifest is incomplete")
        return manifest

    def _validate_archive_prefix(
        self,
        *,
        scene: SceneCard,
        contract,
        final_scene: FinalScene,
        carry_notes: list[dict[str, Any]],
        through: int,
        allow_terminal: bool = False,
    ) -> dict[str, Any] | None:
        """复验归档产品到第 ``through`` 步为止；返回第 4 步核对出的行 id（``through`` 不到 4 时为 None）。"""
        archive_result = None
        if through >= 4:
            archive_result = self._validate_archive_core_checkpoint(
                scene=scene,
                final_scene=final_scene,
                carry_notes=carry_notes,
                allow_terminal=allow_terminal,
            )
        if through >= 5:
            self._validate_archive_rule_events_checkpoint(scene)
        if through >= 6:
            self._validate_archive_prose_checkpoint(scene, contract)
        for stage in ARCHIVE_PRODUCT_STAGES:
            if through >= stage.sub_index:
                self._validate_archive_stage_product(stage, scene, final_scene)
        return archive_result

    # ---- 归档效果（叙事事件 / 正文抽取 / 读数）：SceneArchiveEffects 每次调用新建，同簇互调经 dispatch=self 回到编排器

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

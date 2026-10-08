"""归档尾段：``near_final_ready`` 的子游标 4、5 与最后的 ``archived`` 节点（检查点格式 4，G4 B3-1a.2）。

``Orchestrator`` 直接继承 ``ArchiveCheckpointMixin``：驱动（``_archive_near_final_checkpoint``）、两步的产品、续跑时的
复验与归档清单。

- 第 4 步 ``archive_effects``：确定性的收尾一次做完，与检查点在同一个事务里提交——终稿归档（``Archiver``：成稿门照要
  归档的这一份正文判；场景记忆、章滚动笔记、归档尝试；「像不像」读数，source=pipeline），章末那一场再重建章汇总、
  卷汇总。断在提交之前什么也没留下，续跑整步重做；提交之后续跑直接去第 5 步。「采纳并归档」「晋升」两条路径一向
  这样一次做完（``archive_effects_plan``）。
- 第 5 步 ``prose_extraction``：可选的正文事件抽取（LLM 节点 ``extraction``，``NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED``
  默认关，关着记一份 ``not_invoked`` 产品）。付费调用的账本行先于产品提交，所以单独一步；产品绑在它的账本行上
  （``archive_prose_product_hash``），抽出的事件只是候选，交给正史复核台账。

格式 3 及以前是 4..11 八个子步（核心归档、B11-02 起总是空的规则事件、正文抽取、退役的向量索引、章汇总、卷汇总、结果
没人读的章级准终稿评审、读数），每步单独存检查点；那样的检查点由格式关拒绝续跑（``scene_run_checkpoint.CHECKPOINT_FORMAT``）。

方法之间一律 ``self.X`` 互调：测试在编排器实例上覆盖某一步（``_archive_stage_effects``、``_record_prose_events``、
``_archive_manifest`` …）注入故障，驱动与复验照样看得到。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterRollingNote,
    FinalScene,
    LlmCall,
    NarrativeEvent,
    SceneCard,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.archive_effects_plan import aggregate_volume_after_chapter
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    stamp_product_hash,
    validate_product_call,
)
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.scene_run.context import ArchiveInputs
from novel_system.services.scene_run.results import (
    apply_finality,
    base_result,
    merged_warnings,
    qc_decision_payload,
)
from novel_system.services.scene_run.snapshots import (
    archive_attempt_snapshot,
    archive_final_scene_snapshot,
    archive_rolling_note_snapshot,
    archive_scene_memory_snapshot,
    narrative_event_snapshot,
)
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult


@dataclass(frozen=True)
class ArchiveStage:
    """归档尾段的一步（``near_final_ready`` 的子游标）：产品种类与它在 ``artifact_refs`` / ``artifact_hashes`` 里的键。
    归档清单照 :data:`ARCHIVE_STAGES` 记。"""

    sub_index: int
    kind: str
    hash_key: str


ARCHIVE_EFFECTS_STAGE = ArchiveStage(4, "archive_effects", "archive_effects")
ARCHIVE_PROSE_STAGE = ArchiveStage(5, "prose_extraction", "archive_prose_product")
ARCHIVE_STAGES: tuple[ArchiveStage, ...] = (ARCHIVE_EFFECTS_STAGE, ARCHIVE_PROSE_STAGE)

# 第 4 步产品的格式（核心归档产品 v1 → v2 只记快照哈希 → v3 一步做完全部确定性收尾）
ARCHIVE_EFFECTS_SCHEMA_VERSION = 3
ARCHIVE_EFFECTS_STEP_KEY = "archive:effects:0"
# 第 4 步产品里四份行快照的哈希（快照本身带整场正文，不进检查点）：键 → 行快照函数
_ARCHIVE_SNAPSHOTS = {
    "final_scene": archive_final_scene_snapshot,
    "scene_memory": archive_scene_memory_snapshot,
    "rolling_note": archive_rolling_note_snapshot,
    "archive_attempt": archive_attempt_snapshot,
}
_ARCHIVE_EFFECTS_PRODUCT_KEYS = frozenset(
    {
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
        "snapshot_hashes",
        "chapter_aggregate",
        "volume_aggregate",
        "fidelity_reading",
    }
)
# 第 4 步记下的三项收尾结果：可取的 outcome，与带结果行时那一行 id 的键
_ARCHIVE_EFFECT_RESULTS = {
    "chapter_aggregate": ({"not_applicable", "aggregated", "no_op"}, {"aggregated": "chapter_memory_row_id"}),
    "volume_aggregate": (
        {"not_applicable", "aggregated", "no_op", "degraded"},
        {"aggregated": "volume_summary_row_id", "degraded": "error_code"},
    ),
    "fidelity_reading": (
        {"recorded", "not_applicable", "degraded"},
        {"recorded": "reading_id", "degraded": "error_code"},
    ),
}
_NOT_CHAPTER_LAST = {"outcome": "not_applicable", "reason": "not_chapter_last"}


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
        """归档尾段的驱动：从子游标停下的地方接着做第 4、5 步，每步「产出 → 自检 → 存检查点」；最后写 ``archived`` 与
        归档清单，装配运行结果。

        只在续跑时复验（B01-11、B03-07）：进来时已经存下的产品复验一遍；同一进程里新做的产品存之前就核对过，不再
        回读。``in_process`` 是同一进程刚做完的软 QC 决定、终稿行与准终稿 payload（全新的一次运行从
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
        if entry >= ARCHIVE_EFFECTS_STAGE.sub_index:
            # 续跑：停下之前已经存下的产品复验一遍
            effects = self._validate_archive_prefix(
                scene=scene,
                contract=contract,
                final_scene=final_scene,
                carry_notes=carry_notes,
                through=entry,
            )
        else:
            effects = self._archive_stage_effects(
                scene, final_scene, soft_qc=soft_qc, carry_notes=carry_notes
            )
        if entry < ARCHIVE_PROSE_STAGE.sub_index:
            self._archive_stage_prose(scene, contract, final_scene)
        self._stage_archive_prose_extraction(state, final_scene)

        manifest = self._archive_manifest()
        state.scene_status = "archived"
        self._save_run_checkpoint(
            "archived",
            artifact_refs={"archive_manifest": manifest},
            artifact_hashes={"archive_manifest": self._json_hash(manifest)},
        )

        near_final_warnings = self._near_final_warning_findings(near_final_payload)
        result = self._with_author_projection(
            scene_id,
            state,
            {
                **base_result(state, bundle),
                "current_final_scene_row_id": final_scene.row_id,
                "hard_qc": hard_qc_payload,
                "soft_qc": qc_decision_payload(soft_qc),
                "planning": planning,
                "near_final": near_final_payload,
                "style_candidates": candidate_summaries,
                "run_policy": run_policy,
            },
        )
        result["quality_warnings"] = merged_warnings(
            result.get("quality_warnings"), near_final_warnings
        )
        archive_attempt = self.session.get(AttemptTracker, effects["archive_attempt_id"])
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

    def _archive_stage_effects(
        self,
        scene: SceneCard,
        final_scene: FinalScene,
        *,
        soft_qc,
        carry_notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """归档第 4 步：确定性的收尾一次做完，产品与检查点一起提交（同一个事务：断在提交之前什么也没留下）。

        终稿归档（成稿门照这一份正文判；读数也在这里记）→ 章末那一场重建章汇总 → 卷汇总（不看章汇总这一次的结果，
        见 ``archive_effects_plan``）。产品记三行的 id、四份行快照的哈希与三项收尾的结果；返回核对过的产品（与续跑时
        ``_validate_archive_effects`` 读回的一样）。
        """
        archive_result = self.archiver.archive_final_scene(
            scene.scene_id,
            final_scene.row_id,
            qc_report_id=soft_qc.qc_report_id,
            carry_notes_json=carry_notes,
            execution_id=self._execution_id,
            finalize_scene_status=False,
            fidelity_source="pipeline",
        )
        rows = self._archive_rows(final_scene, archive_result)
        product = self._archive_product(
            scene=scene,
            kind=ARCHIVE_EFFECTS_STAGE.kind,
            outcome="completed",
            step_key=ARCHIVE_EFFECTS_STEP_KEY,
            input_hash=self._text_hash(final_scene.content),
            schema_version=ARCHIVE_EFFECTS_SCHEMA_VERSION,
            final_scene_row_id=final_scene.row_id,
            scene_memory_row_id=rows["scene_memory"].row_id,
            chapter_rolling_note_row_id=rows["rolling_note"].row_id,
            archive_attempt_id=rows["archive_attempt"].attempt_id,
            snapshot_hashes={
                name: self._json_hash(snapshot(rows[name])) for name, snapshot in _ARCHIVE_SNAPSHOTS.items()
            },
            chapter_aggregate=self._archive_chapter_aggregate(scene),
            volume_aggregate=self._archive_volume_aggregate(scene),
            fidelity_reading=archive_result.get("fidelity_reading")
            or {"outcome": "not_applicable", "reason": "no_scene"},
        )
        validated = self._validate_archive_effects(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            product=product,
            require_checkpoint_hash=False,
        )
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=ARCHIVE_EFFECTS_STAGE.sub_index,
            artifact_refs={ARCHIVE_EFFECTS_STAGE.hash_key: product},
            artifact_hashes={ARCHIVE_EFFECTS_STAGE.hash_key: self._json_hash(product)},
        )
        return validated

    def _archive_rows(self, final_scene: FinalScene, ids: dict[str, Any]) -> dict[str, Any]:
        """终稿归档的四行（终稿、场景记忆、章滚动笔记、归档尝试）；少了哪一行 → ``RUN_CHECKPOINT_OUTPUT_MISSING``。"""
        attempt_id = ids.get("archive_attempt_id")
        attempt = (
            self.session.get(AttemptTracker, attempt_id)
            if isinstance(attempt_id, int) and not isinstance(attempt_id, bool)
            else None
        )
        if attempt is None:
            self._raise_checkpoint_output_missing(row_id=str(attempt_id))
        return {
            "final_scene": final_scene,
            "scene_memory": self._require_checkpoint_row(SceneMemory, ids.get("scene_memory_row_id")),
            "rolling_note": self._require_checkpoint_row(
                ChapterRollingNote, ids.get("chapter_rolling_note_row_id")
            ),
            "archive_attempt": attempt,
        }

    def _archive_chapter_aggregate(self, scene: SceneCard) -> dict[str, Any]:
        """章末那一场重建存下来的章汇总（派生缓存，章级读者读时现拼，R13）；拼不出来原样记下（``no_op`` + 原因），
        不挡归档。"""
        if scene.is_chapter_last != 1:
            return dict(_NOT_CHAPTER_LAST)
        result = dict(
            self.aggregator.run_final_aggregate(scene.chapter_id) or {"status": "no_op", "reason": "no_result"}
        )
        self.session.flush()
        return {"outcome": "aggregated" if result.get("status") == "created" else "no_op", **result}

    def _archive_volume_aggregate(self, scene: SceneCard) -> dict[str, Any]:
        """章末那一场接着卷汇总（到了卷边界才卷，卷的是各章存着的章汇总；在自己的保存点里做，失败只降级）。"""
        if scene.is_chapter_last != 1:
            return dict(_NOT_CHAPTER_LAST)
        result = aggregate_volume_after_chapter(self.session, scene.chapter_id)
        status = result.get("status")
        outcome = "aggregated" if status == "created" else ("degraded" if status == "degraded" else "no_op")
        return {"outcome": outcome, **result}

    def _archive_stage_prose(
        self, scene: SceneCard, contract, final_scene: FinalScene
    ) -> None:
        """归档第 5 步：正文事件抽取（可选的 LLM 节点；上次被本地拒掉的抽取从账本恢复，不再调一次）。"""
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
            kind=ARCHIVE_PROSE_STAGE.kind,
            outcome=extraction_snapshot["outcome"],
            step_key="archive:prose_event_extract:0",
            input_hash=self._text_hash(final_scene.content),
            extraction=extraction_snapshot,
            event_ids=prose_event_ids,
            events=prose_events,
        )
        if prose_result.llm_call_id is not None and not stamp_product_hash(
            self.session, prose_result.llm_call_id, "archive_prose_product_hash", self._json_hash(prose_product)
        ):
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "prose extraction product parent disappeared before archive checkpoint",
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
            sub_index=ARCHIVE_PROSE_STAGE.sub_index,
            artifact_refs={
                ARCHIVE_PROSE_STAGE.hash_key: prose_product,
                "archive_prose_event_ids": prose_event_ids,
                "archive_prose_events": prose_events,
            },
            artifact_hashes={
                ARCHIVE_PROSE_STAGE.hash_key: self._json_hash(prose_product),
                "archive_prose_events": self._json_hash(prose_events),
            },
        )

    def _stage_archive_prose_extraction(
        self, state: SceneRunState, final_scene: FinalScene
    ) -> None:
        """检查点里的抽取结果只是候选：交给正史复核台账（待定的行在重放里看不见）。每次走过第 5 步都做（幂等）。"""
        prose_checkpoint = dict(
            ((state.run_checkpoint_json or {}).get("artifact_refs") or {}).get(
                ARCHIVE_PROSE_STAGE.hash_key
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

    def _near_final_checkpoint_progress(self) -> int:
        if self._execution_id is None or self._checkpoint_service is None:
            return -1
        return self._sub_checkpoint_progress(
            "near_final_ready",
            last_sub_index=ARCHIVE_STAGES[-1].sub_index,
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
        schema_version: int = 1,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "kind": kind,
            "outcome": outcome,
            "execution_id": self._execution_id,
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "step_key": step_key,
            "input_hash": input_hash,
            **details,
        }

    def _validate_archive_effects(
        self,
        *,
        scene: SceneCard,
        final_scene: FinalScene,
        carry_notes: list[dict[str, Any]],
        allow_terminal: bool = False,
        product: dict[str, Any] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        """复验第 4 步的产品（``product`` 为空就读检查点里的那份）：产品自身（格式、归属、哈希）；终稿归档的四行还在、
        没被改过（按库里的行重算快照比哈希）、彼此对得上；三项收尾的结果形状。

        章汇总 / 卷汇总 / 读数的行不回读：它们是派生缓存与观察，别的归档路径随时重建（R13、[批准#22]），产品只记
        当时的结果。
        """
        if product is None:
            product = self._archive_checkpoint_ref(ARCHIVE_EFFECTS_STAGE.hash_key)
        snapshot_hashes = product.get("snapshot_hashes") if isinstance(product, dict) else None
        if (
            not isinstance(product, dict)
            or set(product) != _ARCHIVE_EFFECTS_PRODUCT_KEYS
            or product.get("schema_version") != ARCHIVE_EFFECTS_SCHEMA_VERSION
            or product.get("kind") != ARCHIVE_EFFECTS_STAGE.kind
            or product.get("outcome") != "completed"
            or product.get("execution_id") != self._execution_id
            or product.get("scene_id") != scene.scene_id
            or product.get("chapter_id") != scene.chapter_id
            or product.get("step_key") != ARCHIVE_EFFECTS_STEP_KEY
            or product.get("input_hash") != self._text_hash(final_scene.content)
            or product.get("final_scene_row_id") != final_scene.row_id
            or not isinstance(snapshot_hashes, dict)
            or set(snapshot_hashes) != set(_ARCHIVE_SNAPSHOTS)
            or (
                require_checkpoint_hash
                and self._json_hash(product) != self._checkpoint_hash(ARCHIVE_EFFECTS_STAGE.hash_key)
            )
        ):
            raise checkpoint_corrupt("archive effects checkpoint product schema/owner/hash is invalid")
        for key, (outcomes, row_keys) in _ARCHIVE_EFFECT_RESULTS.items():
            entry = product.get(key)
            outcome = entry.get("outcome") if isinstance(entry, dict) else None
            row_key = row_keys.get(outcome)
            if outcome not in outcomes or (row_key is not None and not isinstance(entry.get(row_key), str)):
                raise checkpoint_corrupt(f"archive effects {key} result is invalid")
        rows = self._archive_rows(final_scene, product)
        memory, rolling, attempt = rows["scene_memory"], rows["rolling_note"], rows["archive_attempt"]
        state = self._active_checkpoint_state()
        if (
            any(
                snapshot_hashes[name] != self._json_hash(snapshot(rows[name]))
                for name, snapshot in _ARCHIVE_SNAPSHOTS.items()
            )
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
            or (attempt.details_json or {}).get("final_scene_row_id") != final_scene.row_id
            or (attempt.details_json or {}).get("execution_id") != self._execution_id
        ):
            raise checkpoint_corrupt("archive effects checkpoint product graph is inconsistent")
        return product

    def _narrative_event_snapshots(self, event_ids: list[str]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for event_id in event_ids:
            event = self.session.get(NarrativeEvent, event_id)
            if event is None:
                self._raise_checkpoint_output_missing(row_id=event_id)
            snapshots.append(narrative_event_snapshot(event))
        return snapshots

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
        """复验归档产品到第 ``through`` 步为止；返回第 4 步核对过的产品（``through`` 不到 4 时为 None）。"""
        effects = None
        if through >= ARCHIVE_EFFECTS_STAGE.sub_index:
            effects = self._validate_archive_effects(
                scene=scene,
                final_scene=final_scene,
                carry_notes=carry_notes,
                allow_terminal=allow_terminal,
            )
        if through >= ARCHIVE_PROSE_STAGE.sub_index:
            self._validate_archive_prose_checkpoint(scene, contract)
        return effects

    # ---- 正文抽取：SceneArchiveEffects 每次调用新建，同簇互调经 dispatch=self 回到编排器

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

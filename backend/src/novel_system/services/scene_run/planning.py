"""规划检查点（``planning_ready`` 子游标 0..3：场景蓝图、章架构、人物压力、规划完成）的读、复验与来源核对。

``Orchestrator`` 直接继承 ``PlanningCheckpointMixin``；规划的产出在 ``pipeline`` 的规划阶段，这里只管续跑时把
检查点里的规划产品读回来并逐项核对（行快照、账本、来源版本）。
"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import (
    GenerationPlanningArtifact,
    LlmCall,
    SceneBlueprint,
    SceneCard,
)
from novel_system.services.scene_run.snapshots import planning_provenance
from novel_system.services.scene_run_checkpoint import checkpoint_corrupt


class PlanningCheckpointMixin:
    def _planning_checkpoint_progress(self) -> int:
        return self._sub_checkpoint_progress(
            "planning_ready",
            last_sub_index=3,
            legacy_complete=lambda refs: isinstance(refs.get("planning"), dict),
            legacy_sub_index=3,
            invalid_message="planning checkpoint sub-index is invalid",
        )

    def _planning_artifact_refs(
        self,
        *,
        prefix: str,
        serialized: dict[str, Any],
        execution_step_key: str,
        reused: bool,
    ) -> dict[str, Any]:
        llm_call_id = serialized.get("llm_call_id")
        call = (
            self.session.get(LlmCall, llm_call_id)
            if isinstance(llm_call_id, str)
            else None
        )
        if call is not None and isinstance(call.execution_id, str):
            artifact_execution_id = call.execution_id
        elif reused:
            artifact_execution_id = None
        else:
            artifact_execution_id = self._execution_id
        if not reused and artifact_execution_id != self._execution_id:
            raise checkpoint_corrupt("new planning artifact is not owned by the current execution")
        return {
            f"{prefix}_row_id": serialized.get("row_id"),
            f"{prefix}_llm_call_id": llm_call_id,
            f"{prefix}_execution_step_key": execution_step_key,
            f"{prefix}_artifact_execution_id": artifact_execution_id,
            f"{prefix}_reused": reused,
        }

    def _validate_planning_provenance(
        self,
        *,
        refs: dict[str, Any],
        prefix: str,
        llm_call_id: str | None,
    ) -> str | None:
        provenance = planning_provenance(refs, prefix)
        if self._json_hash(provenance) != self._checkpoint_hash(f"{prefix}_provenance"):
            raise checkpoint_corrupt(f"{prefix} provenance hash mismatch")
        reused = provenance.get("reused")
        owner_execution_id = provenance.get("artifact_execution_id")
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        if not isinstance(reused, bool):
            raise checkpoint_corrupt("planning reuse marker is invalid")
        if llm_call_id is None:
            if reused:
                valid_owner = owner_execution_id is None
            else:
                allowed = {
                    self._execution_id,
                    (
                        payload.get("selection_origin_execution_id")
                        if isinstance(payload, dict)
                        else None
                    ),
                }
                if isinstance(payload, dict):
                    allowed.update(payload.get("artifact_execution_lineage_ids") or [])
                valid_owner = owner_execution_id in allowed
            if not valid_owner:
                raise checkpoint_corrupt("local planning provenance is invalid")
            return None
        if not isinstance(owner_execution_id, str):
            raise checkpoint_corrupt("planning execution provenance is missing")
        superseded = (
            set(payload.get("superseded_execution_ids") or [])
            if isinstance(payload, dict)
            else set()
        )
        if reused:
            if (
                owner_execution_id == self._execution_id
                or owner_execution_id not in superseded
            ):
                raise checkpoint_corrupt(
                    "reused planning artifact is outside the superseded execution lineage",
                )
        elif owner_execution_id != self._execution_id:
            allowed = {
                (
                    payload.get("selection_origin_execution_id")
                    if isinstance(payload, dict)
                    else None
                ),
            }
            if isinstance(payload, dict):
                allowed.update(payload.get("artifact_execution_lineage_ids") or [])
            if owner_execution_id not in allowed:
                raise checkpoint_corrupt("current planning artifact is owned by another execution")
        return owner_execution_id

    @staticmethod
    def _planning_snapshot_matches(
        current: dict[str, Any] | None, snapshot: dict[str, Any]
    ) -> bool:
        if current == snapshot:
            return True
        if not isinstance(current, dict):
            return False
        normalized = dict(current)
        if (
            snapshot.get("status") == "active"
            and normalized.get("status") == "superseded"
        ):
            normalized["status"] = "active"
        return normalized == snapshot

    def _validate_planning_prefix(self, scene_id: str, *, through: int) -> None:
        if through >= 0:
            self._load_planning_blueprint_checkpoint(scene_id)
        if through >= 1:
            self._load_planning_artifact_checkpoint(
                scene_id,
                prefix="planning_chapter_architecture",
                expected_step_key="planning:chapter_architecture",
                expected_kind="chapter_architecture",
            )
        if through >= 2:
            self._load_planning_artifact_checkpoint(
                scene_id,
                prefix="planning_character_pressure",
                expected_step_key="planning:character_pressure",
                expected_kind="character_pressure",
            )

    def _load_planning_blueprint_checkpoint(self, scene_id: str) -> SceneBlueprint:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if not isinstance(refs, dict):
            raise checkpoint_corrupt("planning references are invalid")
        serialized = refs.get("scene_blueprint")
        row_id = refs.get("planning_scene_blueprint_row_id")
        row = self._require_checkpoint_row(SceneBlueprint, row_id)
        scene = self.session.get(SceneCard, scene_id)
        if (
            not isinstance(serialized, dict)
            or serialized.get("row_id") != row_id
            or not self._planning_snapshot_matches(
                self.scene_blueprint_service.serialize(row),
                serialized,
            )
            or self._json_hash(serialized)
            != self._checkpoint_hash("planning_scene_blueprint")
            or row.scene_id != scene_id
            or scene is None
            or row.chapter_id != scene.chapter_id
            or refs.get("planning_scene_blueprint_llm_call_id") != row.llm_call_id
            or refs.get("planning_scene_blueprint_execution_step_key")
            != "scene_blueprint"
        ):
            raise checkpoint_corrupt("planning blueprint checkpoint is invalid")
        owner_execution_id = self._validate_planning_provenance(
            refs=refs,
            prefix="planning_scene_blueprint",
            llm_call_id=row.llm_call_id,
        )
        assert owner_execution_id is not None
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=row.llm_call_id,
            execution_step_key="scene_blueprint",
            execution_id=owner_execution_id,
        )
        return row

    def _load_planning_artifact_checkpoint(
        self,
        scene_id: str,
        *,
        prefix: str,
        expected_step_key: str,
        expected_kind: str,
    ) -> GenerationPlanningArtifact:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if not isinstance(refs, dict):
            raise checkpoint_corrupt("planning references are invalid")
        serialized = refs.get(prefix)
        row_id = refs.get(f"{prefix}_row_id")
        row = self._require_checkpoint_row(GenerationPlanningArtifact, row_id)
        scene = self.session.get(SceneCard, scene_id)
        expected_shapes = {
            "chapter_architecture": (
                "chapter_story_architecture",
                "chapter",
                scene.chapter_id if scene is not None else None,
                None,
            ),
            "character_pressure": (
                "character_pressure_blueprint",
                "scene",
                scene_id,
                scene_id,
            ),
        }
        artifact_type, object_type, object_id, artifact_scene_id = expected_shapes[
            expected_kind
        ]
        if (
            scene is None
            or not isinstance(serialized, dict)
            or serialized.get("row_id") != row_id
            or not self._planning_snapshot_matches(
                self.planning_service.serialize_artifact(row),
                serialized,
            )
            or self._json_hash(serialized) != self._checkpoint_hash(prefix)
            or row.artifact_type != artifact_type
            or row.object_type != object_type
            or row.object_id != object_id
            or row.chapter_id != scene.chapter_id
            or row.scene_id != artifact_scene_id
            or refs.get(f"{prefix}_llm_call_id") != row.llm_call_id
            or refs.get(f"{prefix}_execution_step_key") != expected_step_key
        ):
            raise checkpoint_corrupt(f"{expected_kind} checkpoint is invalid")
        owner_execution_id = self._validate_planning_provenance(
            refs=refs,
            prefix=prefix,
            llm_call_id=row.llm_call_id,
        )
        if row.llm_call_id is not None:
            assert owner_execution_id is not None
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=row.llm_call_id,
                execution_step_key=expected_step_key,
                execution_id=owner_execution_id,
            )
        return row

    def _load_planning_checkpoint(self, scene_id: str) -> dict[str, Any]:
        state_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        state_refs = (
            state_payload.get("artifact_refs")
            if isinstance(state_payload, dict)
            else None
        )
        if (
            isinstance(state_refs, dict)
            and "planning_scene_blueprint_row_id" in state_refs
        ):
            self._validate_planning_prefix(scene_id, through=2)
        planning = self._checkpoint_artifact(
            "planning", expected_node_at_least="planning_ready"
        )
        blueprint_payload = self._checkpoint_artifact(
            "scene_blueprint",
            expected_node_at_least="planning_ready",
        )
        if (
            not isinstance(planning, dict)
            or self._json_hash(planning) != self._checkpoint_hash("planning")
            or not isinstance(blueprint_payload, dict)
            or self._json_hash(blueprint_payload)
            != self._checkpoint_hash("scene_blueprint")
        ):
            raise checkpoint_corrupt("planning checkpoint payload/hash is invalid")

        scene = self.session.get(SceneCard, scene_id)
        blueprint_id = blueprint_payload.get("row_id")
        blueprint = self._require_checkpoint_row(SceneBlueprint, blueprint_id)
        assert blueprint is not None and scene is not None
        if (
            blueprint.scene_id != scene_id
            or blueprint.chapter_id != scene.chapter_id
            or self.scene_blueprint_service.serialize(blueprint) != blueprint_payload
        ):
            raise checkpoint_corrupt("scene blueprint checkpoint row is misbound")
        self._validate_reused_planning_call(
            scene_id=scene_id,
            llm_call_id=blueprint.llm_call_id,
            expected_step_key="scene_blueprint",
        )

        expected_shapes = {
            "chapter_architecture": {
                "artifact_type": "chapter_story_architecture",
                "object_type": "chapter",
                "object_id": scene.chapter_id,
                "chapter_id": scene.chapter_id,
                "scene_id": None,
                "step_key": "planning:chapter_architecture",
            },
            "character_pressure": {
                "artifact_type": "character_pressure_blueprint",
                "object_type": "scene",
                "object_id": scene_id,
                "chapter_id": scene.chapter_id,
                "scene_id": scene_id,
                "step_key": "planning:character_pressure",
            },
        }
        for key, shape in expected_shapes.items():
            serialized = planning.get(key)
            row_id = serialized.get("row_id") if isinstance(serialized, dict) else None
            row = self._require_checkpoint_row(GenerationPlanningArtifact, row_id)
            if (
                not isinstance(serialized, dict)
                or self.planning_service.serialize_artifact(row) != serialized
                or row.artifact_type != shape["artifact_type"]
                or row.object_type != shape["object_type"]
                or row.object_id != shape["object_id"]
                or row.chapter_id != shape["chapter_id"]
                or row.scene_id != shape["scene_id"]
            ):
                raise checkpoint_corrupt(f"{key} planning artifact is misbound")
            self._validate_reused_planning_call(
                scene_id=scene_id,
                llm_call_id=row.llm_call_id,
                expected_step_key=str(shape["step_key"]),
                allow_absent=True,
            )
        return planning

    def _validate_reused_planning_call(
        self,
        *,
        scene_id: str,
        llm_call_id: str | None,
        expected_step_key: str,
        allow_absent: bool = False,
    ) -> None:
        if llm_call_id is None and allow_absent:
            return
        call = self._require_checkpoint_row(LlmCall, llm_call_id)
        if (
            call.scene_id != scene_id
            or call.request_dispatched_at is None
            or call.accounting_status != "settled"
            or (
                call.execution_id == self._execution_id
                and call.execution_step_key != expected_step_key
            )
        ):
            raise checkpoint_corrupt("planning artifact LLM call is misbound")
        if call.execution_id == self._execution_id:
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=llm_call_id,
                execution_step_key=expected_step_key,
            )

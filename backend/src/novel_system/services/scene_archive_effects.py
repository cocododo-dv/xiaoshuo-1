"""Archive-time side-effect recorders extracted from the scene orchestrator.

Two recorders live here: the opt-in prose event extraction (archive step 5,
``archive:prose_event_extract:0`` — prose-grounded candidates only, staged for
the author's canon review) and the archive-time style fidelity reading (风格参考
v3; every archive path records it through ``Archiver.archive_final_scene``, the
pipeline inside its atomic archive step 4). The plan-based rule events (never
read, B11-02), the retired vector index ([批准#1]) and the old style-drift
steering are gone, and so are their checkpoint slots (checkpoint format 4,
G4 B3-1a.2).

Dispatch contract: cluster-internal cross-calls go through ``self._dispatch``
(the hosting ``Orchestrator`` when constructed by its delegates, else ``self``).
This preserves the long-standing test seam where suites override individual
recorder methods as instance attributes on the orchestrator and expect the
sibling methods to observe the override.

``execution_id`` / ``run_job_id`` are per-run values captured at construction
time; hosts must build a fresh ``SceneArchiveEffects`` per call rather than
caching one across runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    SceneCard,
    SceneDraft,
)
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.hash_engine import sha256_json_plain, sha256_text

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult


class SceneArchiveEffects:
    def __init__(
        self,
        session: Session,
        llm_runner,
        *,
        execution_id: str | None,
        run_job_id: str | None,
        dispatch=None,
    ) -> None:
        self.session = session
        self.llm_runner = llm_runner
        self._execution_id = execution_id
        self._run_job_id = run_job_id
        # Cluster-internal cross-calls route through the host so instance-level
        # overrides on the orchestrator (a test seam) keep intercepting them.
        self._dispatch = dispatch if dispatch is not None else self

    @staticmethod
    def _text_hash(content: str) -> str:
        return sha256_text(content)

    @staticmethod
    def _json_hash(payload: Any) -> str:
        return sha256_json_plain(payload)

    def _resolve_scene_project_id(self, scene: SceneCard, contract=None) -> str:
        """Resolve project ownership exclusively from relational authority."""
        payload = getattr(contract, "payload_json", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        explicit_project_id = payload.get("project_id")
        return require_scene_project_id(
            self.session,
            scene,
            explicit_project_id=(
                explicit_project_id if isinstance(explicit_project_id, str) else None
            ),
        )

    def _archive_event_base(self, scene: SceneCard, contract) -> dict[str, str]:
        project_id = self._dispatch._resolve_scene_project_id(scene, contract)
        return {
            "project_id": str(project_id),
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
        }

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
        """§2 (opt-in): extract events from the ACTUAL generated prose so model drift away
        from the spec is captured. Tagged confidence="extracted" + source="prose" → advisory
        only, never a hard consistency blocker (blueprint §15 honest-bounds). Returns an
        explicit no-call/degraded/completed product; accounting and control-plane integrity
        failures propagate."""
        from novel_system.services.prose_event_extractor import (
            ProseExtractionResult,
            extract_events_from_prose,
            stage_prose_events,
        )
        from novel_system.settings import get_settings

        settings = get_settings()
        extract_step_key = "archive:prose_event_extract:0"
        if not (
            settings.llm_enabled
            and getattr(settings, "llm_event_extraction_enabled", False)
        ):
            result = ProseExtractionResult(
                outcome="not_invoked",
                execution_id=self._execution_id,
                execution_step_key=(
                    extract_step_key if self._execution_id is not None else None
                ),
                run_job_id=self._run_job_id,
                reason="feature_disabled",
            )
            return (result, []) if return_event_ids else result
        if not (content and content.strip()):
            result = ProseExtractionResult(
                outcome="not_invoked",
                execution_id=self._execution_id,
                execution_step_key=(
                    extract_step_key if self._execution_id is not None else None
                ),
                run_job_id=self._run_job_id,
                reason="empty_content",
            )
            return (result, []) if return_event_ids else result
        extract_context = LLMCallContext(
            scope_type="scene",
            scope_id=str(base.get("scene_id") or getattr(scene, "scene_id", "")),
            project_id=str(base.get("project_id") or getattr(scene, "project_id", ""))
            or None,
            chapter_id=str(base.get("chapter_id") or getattr(scene, "chapter_id", ""))
            or None,
            scene_id=str(base.get("scene_id") or getattr(scene, "scene_id", ""))
            or None,
            node_id="extraction",
            step=extract_step_key,
            execution_id=self._execution_id,
            execution_step_key=(
                extract_step_key if self._execution_id is not None else None
            ),
            run_job_id=self._run_job_id,
        )
        result = extract_events_from_prose(
            content,
            session=self.session,
            llm_runner=self.llm_runner,
            llm_context=extract_context,
        )
        event_ids = stage_prose_events(
            log,
            base,
            result.events,
            final_scene_row_id=final_scene_row_id,
            payload=lambda ordinal: {
                "source": "prose",
                "archive_execution_id": self._execution_id,
                "archive_step_key": extract_step_key,
                "archive_ordinal": ordinal,
            },
        )
        return (result, event_ids) if return_event_ids else result

    def _record_archive_fidelity_reading(
        self,
        scene: SceneCard,
        *,
        source: str = "pipeline",
    ) -> dict[str, Any]:
        """归档终稿的「像不像」读数（风格参考 v3 P5b；每一条归档路径都经这里）。

        终稿取场景当前的 ``FinalScene``；策略：终稿出自管线 bundle → 那份 bundle 冻结的策略（与这一场生成时同一把尺）；
        否则（作者稿提升、采纳作者稿、旧 bundle 没冻结契约）→ 当前活动绑定轻量现解析（作者此刻对着的那本书）。
        读数入库走唯一入口 ``readings.record_fidelity_reading``（stage=final，按场景键、不按章键，同一终稿行幂等）；
        评审过这份终稿原文的软 QC 参考评审分一并记上。未绑定 / 书没有参照分布 → ``not_applicable``。
        """
        from novel_system.db.models import FinalScene, QcReport, SceneBundle, SceneRunState
        from novel_system.services.style_policy import MODE_NONE, style_policy_for_bundle, style_policy_live
        from novel_system.services.style_reference import readings
        from novel_system.services.style_reference.style_step import report_reference_judge

        state = self.session.get(SceneRunState, scene.scene_id)
        final_row_id = getattr(state, "current_final_scene_row_id", None) if state is not None else None
        final = self.session.get(FinalScene, final_row_id) if final_row_id else None
        if final is None or final.scene_id != scene.scene_id or not (final.content or "").strip():
            return {"outcome": "not_applicable", "reason": "no_final_text"}
        bundle = self.session.get(SceneBundle, final.source_bundle_id) if final.source_bundle_id else None
        policy = None
        if bundle is not None and bundle.scene_id == scene.scene_id:
            policy = style_policy_for_bundle(bundle.frozen_snapshot_json)
        if policy is None or policy.mode == MODE_NONE:
            policy = style_policy_live(self.session, scene, freeze_contract=False)
        if not policy.bound:
            return {"outcome": "not_applicable", "reason": "unbound"}
        judge = None
        for report, content in self.session.execute(
            select(QcReport, SceneDraft.content)
            .join(SceneDraft, SceneDraft.row_id == QcReport.source_draft_row_id)
            .where(QcReport.scene_id == scene.scene_id, QcReport.qc_type == "soft_qc")
            .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        ).all():
            if (content or "") != (final.content or ""):
                continue
            judge = report_reference_judge(report)
            break
        row = readings.record_fidelity_reading(
            self.session,
            policy=policy,
            text=final.content,
            source=source,
            stage=readings.STAGE_FINAL,
            scene_id=scene.scene_id,
            project_id=readings.scene_project_id(self.session, scene),
            draft_ref=final.row_id,
            judge=judge,
        )
        if row is None:
            return {"outcome": "not_applicable", "reason": "no_reference_reading"}
        return {
            "outcome": "recorded",
            "reading_id": row.reading_id,
            "percentile": row.percentile,
            "distance": row.distance,
            "within_range": bool((row.reading_json or {}).get("within_range")),
            "reading_source": source,
        }

"""Archive-time side-effect recorders extracted from the scene orchestrator.

This module owns the post-approval archival effect cluster: narrative event
recording (prose-grounded candidates only — the plan-based rule events were never
read and are no longer written, B11-02) and the archive-time slot for the style
fidelity reading (风格参考 v3: the old style-drift steering was removed; the slot
keeps its checkpoint step key so persisted checkpoints still resume). The
archive-time vector index of the final text was retired with the similar-scene
prompt section ([批准#1], 重评 R1); its checkpoint slot records a ``retired`` product. The methods here were moved from
``Orchestrator`` — checkpoint step keys, event payload fields, and every
product/degraded return value are unchanged.

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

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    SceneCard,
    SceneDraft,
)
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    is_llm_control_plane_failure,
)
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.hash_engine import sha256_json_plain, sha256_text

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult

_LOGGER = logging.getLogger(__name__)


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
        """归档时往叙事事件账本里记的东西。

        以前先按场景卡的「计划」记一批规则事件（出场、位置、出口变化、揭示、关系转折），记成 ``planned``；可运行时
        重放只认 ``accepted``，也没有任何代码把它们升级——每次归档都写、从来没人读（B11-02）。现在不再写：归档第 5 步
        （``archive:rule_events:0``）照旧产出一份空的 ``rule_events`` 产品，写过规则事件的旧检查点照样按原样校验、续跑。
        ``include_prose`` 时只剩正文抽取（同样是暂存、等作者在正史核对里决定）。
        """
        if not include_prose:
            return []
        try:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            _result, event_ids = self._dispatch._record_prose_events(
                NarrativeEventLog(self.session),
                scene,
                self._dispatch._archive_event_base(scene, contract),
                content,
                final_scene_row_id=final_scene_row_id,
                return_event_ids=True,
            )
            self.session.flush()
            return list(event_ids)
        except Exception as exc:
            if is_llm_control_plane_failure(exc) or isinstance(exc, LLMAccountingError):
                raise
            if not degrade_errors:
                raise
            _LOGGER.warning(
                "narrative event recording degraded for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return []

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
            provider_execution_mode=getattr(
                self.llm_runner,
                "provider_execution_mode",
                "online",
            ),
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

    # 归档检查点 ``archive:style_drift:0``（sub 11，产品 kind ``style_drift``）这个槽位留着：已持久化的检查点
    # 按它续跑，校验器接受 {"recorded", "not_applicable", "no_op", "observed", "degraded"}。风格参考 v3 起槽位里记的是
    # 「像不像」读数（``outcome="recorded"`` + ``reading_id``），而不再是漂移驾驶（旧写端对作者自己的书 95–98% 报警，
    # 还会改下一场的选窗、关轮换）。

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
            judge = next(
                (
                    dict(entry)
                    for entry in report.rewrite_brief_json or []
                    if isinstance(entry, dict) and entry.get("kind") == "reference_judge"
                ),
                None,
            )
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

"""场景运行编排器的门面：``Orchestrator`` 的构造函数，以及测试与旧调用方从这里取的名字。

一场的实现分在 :mod:`novel_system.services.scene_run` 的各个 mixin 里（管线、生命周期、准终稿、软 QC、风格稿候选、
bundle / 首稿 / 硬 QC 的读回、规划、自动批评、归档尾段、检查点内核），``Orchestrator`` 直接继承它们；mixin 之间只经
``self`` 互调。

测试依赖、要保持稳定的接缝：

- 模块全局 ``LLMNodeRunner``（``__init__`` 里读，测试在 ``novel_system.services.orchestrator.LLMNodeRunner`` 打桩）；
- 构造参数 ``scene_generation_service`` / ``hard_qc_engine`` / ``soft_qc_engine`` / ``planning_service`` /
  ``near_final_service`` 与属性 ``archiver`` / ``bundle_builder`` / ``scene_blueprint_service`` /
  ``scene_generation_service`` / ``execution_contract_service`` / ``llm_runner``；
- 每次运行的四个字段 ``_execution_id`` / ``_run_job_id`` / ``_checkpoint_service`` / ``_lease_renewer``（测试直接设）；
- 在类上打桩或在实例上覆盖的方法（``_run_scene_pipeline``、``_best_of_n_count``、``_reconcile_execution_step``、
  ``_run_archive_*``、``_archive_product``、``_archive_manifest``、``_record_*_events``、``_capture_failure_audits`` /
  ``_restore_failure_audits``、``_near_final_warning_findings``、``_resolve_auto_critique_runner``、
  ``_style_patch_keep_decision``、``_near_final_rewrite_drift``、``_resume_after_selection_pipeline`` …）——
  名字都留在 ``Orchestrator`` 上（继承来的），类上的打桩按 MRO 先于 mixin；
- 在来源模块打桩、经模块属性调用的：``auto_critique.llm_auto_critique``、``scene_budget.can_spend``、
  ``idempotency.owner_lease_ttl_seconds``、``scene_run.near_final_stage.assess_rewrite_regressions``。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.near_final import (
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run.archive import ArchiveCheckpointMixin
from novel_system.services.scene_run.constants import (
    NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON,
    NEAR_FINAL_REWRITE_GATE_STAGE,
    NEAR_FINAL_REWRITE_MOVED_AWAY_SKIP_REASON,
    NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON,
    STYLE_PATCH_REVERTED_SKIP_REASON,
    STYLE_PATCH_REVERTED_STOP_REASON,
)
from novel_system.services.scene_run.critique import AutoCritiqueCheckpointMixin
from novel_system.services.scene_run.drafts import DraftCheckpointMixin
from novel_system.services.scene_run.kernel import RunCheckpointKernelMixin
from novel_system.services.scene_run.lifecycle import RunLifecycleMixin
from novel_system.services.scene_run.near_final_gate import (
    _near_final_rejection_skip_reason,
    _near_final_rewrite_gate_summary,
    _near_final_rewrite_gate_warnings,
)
from novel_system.services.scene_run.near_final_stage import NearFinalCheckpointMixin
from novel_system.services.scene_run.pipeline import PipelineMixin
from novel_system.services.scene_run.planning import PlanningCheckpointMixin
from novel_system.services.scene_run.soft_qc import SoftQcCheckpointMixin
from novel_system.services.scene_run.style_candidates import StyleCandidatesMixin
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

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


class Orchestrator(
    PipelineMixin,
    RunLifecycleMixin,
    NearFinalCheckpointMixin,
    SoftQcCheckpointMixin,
    StyleCandidatesMixin,
    DraftCheckpointMixin,
    PlanningCheckpointMixin,
    AutoCritiqueCheckpointMixin,
    ArchiveCheckpointMixin,
    RunCheckpointKernelMixin,
):
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

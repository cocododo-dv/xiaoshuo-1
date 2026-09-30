"""场景 / 章节运行：运行态、章状态、bundle、蓝图、执行合同、规划产物、草稿、质检、尝试记录、运行任务、恢复租约。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.env_config import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class SceneRunState(Base):
    __tablename__ = "scene_run_states"
    __table_args__ = (
        CheckConstraint(
            "scene_tokens_reserved >= 0",
            name="ck_scene_run_states_tokens_reserved_nonnegative",
        ),
        CheckConstraint(
            "provider_attempts_used >= 0",
            name="ck_scene_run_states_provider_attempts_used_nonnegative",
        ),
        CheckConstraint(
            "provider_attempt_budget >= 0",
            name="ck_scene_run_states_provider_attempt_budget_nonnegative",
        ),
    )

    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"), primary_key=True)
    scene_status: Mapped[str] = mapped_column(String, default="ready")
    current_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    current_neutral_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_style_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 治理 §4.3：最近有效正文指针——与 current_* 不同，失败/重写路径不清空，
    # 任何后续失败都能回退到该版本（仅项目级运行时失效才重置）
    latest_valid_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_human_review_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_qc_report_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bundle_build_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_partial_rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_full_rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    soft_patch_count: Mapped[int] = mapped_column(Integer, default=0)
    total_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_budget: Mapped[int] = mapped_column(Integer, default=4)
    repeat_issue_key: Mapped[str | None] = mapped_column(String, nullable=True)
    repeat_issue_count: Mapped[int] = mapped_column(Integer, default=0)
    # §6 dispersion signal — last Best-of-N candidate Jaccard dispersion (0.0–1.0)
    candidate_dispersion_score: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # §6 criticality classification result for this run
    criticality_level: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    criticality_reasons_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)
    # Wave 3（治理 §5.5/§6.1）：运行策略 + 场景 token 预算（与 attempt_budget
    # 次数预算双轨）。预算按场景生命周期累计，自动流程不得重置（§7.12），
    # 扩容唯一入口是作者显式 topup（留审计）。
    run_policy: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scene_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    scene_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    scene_tokens_reserved: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    scene_budget_basis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )
    provider_attempts_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    provider_attempt_budget: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_PROVIDER_ATTEMPT_BUDGET,
        server_default=str(DEFAULT_PROVIDER_ATTEMPT_BUDGET),
    )
    active_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_execution_status: Mapped[str | None] = mapped_column(String, nullable=True)
    run_checkpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    run_checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    active_run_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 作者稿提升为权威正文后，叙事事件是否已明确与当前 FinalScene 对齐。
    # v1 只允许作者显式确认 facts_unchanged；需要事件重建的稿件不得静默放行。
    narrative_sync_status: Mapped[str] = mapped_column(String, default="synced")
    narrative_sync_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ChapterState(Base):
    __tablename__ = "chapter_states"

    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"), primary_key=True)
    current_phase: Mapped[str] = mapped_column(String, default="planning")
    chapter_passed_scene_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_backfill_pending_count: Mapped[int] = mapped_column(Integer, default=0)
    mid_aggregate_enabled_effective: Mapped[int] = mapped_column(Integer, default=0)
    aggregate_block_reason: Mapped[str] = mapped_column(String, default="none")
    manual_hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_interim_memory_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_final_memory_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneBundle(Base):
    __tablename__ = "scene_bundles"
    __table_args__ = (Index("ix_scene_bundles_scene", "scene_id"),)

    bundle_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chapter_goals.chapter_id",
            name="fk_scene_bundles_chapter_id",
        )
    )
    execution_mode: Mapped[str] = mapped_column(String, default="P2")
    bundle_snapshot_hash: Mapped[str] = mapped_column(String)
    frozen_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneBlueprint(Base):
    __tablename__ = "scene_blueprints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','accepted','superseded')",
            name="ck_scene_blueprints_status",
        ),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_scene_blueprints_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_scene_blueprints_chapter_id")
    )
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    blueprint_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneExecutionContract(Base):
    __tablename__ = "scene_execution_contracts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','blocked','stale','superseded')",
            # 约束名以迁移 0027 的冻结 DDL 为准（生产库带迁移名，改 ORM 侧对齐，不新开迁移）
            name="ck_scene_execution_contract_status",
        ),
    )

    contract_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey(
            "scene_cards.scene_id",
            name="fk_scene_execution_contracts_scene_id",
        )
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chapter_goals.chapter_id",
            name="fk_scene_execution_contracts_chapter_id",
        )
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "story_projects.project_id",
            name="fk_scene_execution_contracts_project_id",
        ),
        nullable=True,
    )
    contract_version: Mapped[str] = mapped_column(String, default="scene_execution_contract_v1")
    source_snapshot_hash: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    missing_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    created_by: Mapped[str] = mapped_column(String, default="scene_execution")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class GenerationPlanningArtifact(Base):
    __tablename__ = "generation_planning_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('character_pressure_blueprint','chapter_story_architecture')",
            name="ck_generation_planning_artifacts_type",
        ),
        CheckConstraint("object_type IN ('scene','chapter')", name="ck_generation_planning_artifacts_object_type"),
        CheckConstraint("status IN ('active','superseded')", name="ck_generation_planning_artifacts_status"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    created_by: Mapped[str] = mapped_column(String, default="near_final_planning")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneDraft(Base):
    __tablename__ = "scene_drafts"
    __table_args__ = (Index("ix_scene_drafts_scene", "scene_id"),)

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_scene_drafts_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_scene_drafts_chapter_id")
    )
    stage: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    content: Mapped[str] = mapped_column(Text)
    source_bundle_id: Mapped[str] = mapped_column(String)
    source_bundle_hash: Mapped[str] = mapped_column(String)
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class QcReport(Base):
    __tablename__ = "qc_reports"
    __table_args__ = (Index("ix_qc_reports_scene", "scene_id"),)

    qc_report_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_qc_reports_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_qc_reports_chapter_id"),
        nullable=True,
    )
    qc_type: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    source_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String, nullable=True)
    pass_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    issues_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    rewrite_brief_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class AttemptTracker(Base):
    __tablename__ = "attempt_tracker"
    __table_args__ = (Index("ix_attempt_tracker_scene", "scene_id"),)

    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_attempt_tracker_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_attempt_tracker_chapter_id"),
        nullable=True,
    )
    step: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class ChapterRunJob(Base):
    __tablename__ = "chapter_run_jobs"
    __table_args__ = (Index("ix_chapter_run_jobs_scene_created", "scene_id", "created_at"),)

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_chapter_run_jobs_chapter_id"),
        nullable=True,
    )
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_chapter_run_jobs_scene_id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String)
    job_type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class BackgroundRecoveryLease(Base):
    """Short database lease that elects one startup recovery scanner."""

    __tablename__ = "background_recovery_leases"

    lease_key: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String)
    lease_expires_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "AttemptTracker",
    "BackgroundRecoveryLease",
    "ChapterRunJob",
    "ChapterState",
    "GenerationPlanningArtifact",
    "QcReport",
    "SceneBlueprint",
    "SceneBundle",
    "SceneDraft",
    "SceneExecutionContract",
    "SceneRunState",
]

"""评审与待办：写作评审、修订候选、局部补丁候选、待办卡片、派生项的稍后提醒、人工审阅事件。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Computed,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class WriterEvaluation(Base):
    __tablename__ = "writer_evaluations"
    __table_args__ = (
        # 场景诊断按（对象、评审口径）取最新一行、按父行取各镜头行（迁移 20260929_0095）
        Index("ix_writer_evaluations_object", "object_type", "object_id", "rubric_id", "created_at"),
        Index("ix_writer_evaluations_parent", "parent_evaluation_id"),
    )

    evaluation_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    rubric_id: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluator_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lens: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_spans_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    source_blueprint_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String, nullable=True)
    auto_rewrite_eligible: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_field_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    promotion_blockers_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision_brief_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    requires_human_review: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="completed")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class RevisionCandidate(Base):
    __tablename__ = "revision_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_revision_candidates_status",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_type: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_text: Mapped[str] = mapped_column(Text)
    instruction_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    diff_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    patches_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=list)
    apply_mode: Mapped[str] = mapped_column(String, default="manual_only")
    target_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="writer_engine")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class PassagePatchCandidate(Base):
    __tablename__ = "passage_patch_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_passage_patch_candidates_status",
        ),
        CheckConstraint(
            "author_decision IN ('pending','accepted','rejected','regenerate')",
            name="ck_passage_patch_candidates_author_decision",
        ),
        # 一个对象（场 / 章）的改写候选按时间取（迁移 20260929_0095）
        Index("ix_passage_patch_candidates_object", "object_type", "object_id", "created_at"),
    )

    patch_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    target_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    quality_signal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_excerpt: Mapped[str] = mapped_column(Text)
    issue_dimension: Mapped[str] = mapped_column(String)
    candidate_category: Mapped[str] = mapped_column(String, default="local_patch")
    target_range_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    revision_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    preference_tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    inserted_into_author_draft: Mapped[int] = mapped_column(Integer, default=0)
    replacement_options_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_only: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision: Mapped[str] = mapped_column(String, default="pending")
    selected_option_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="writer_deep_review")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_review_items_status"),
        # onceTask: 同一作品同一 dedupe_key 只允许一张卡（NULL 不参与唯一性）。
        # 镜像迁移 0050 的唯一索引，使测试的 create_all 与生产迁移同样强制该唯一性。
        Index("ux_review_items_project_dedupe", "project_id", "dedupe_key", unique=True),
        # 审计 P-9 热路径索引（迁移 0060）
        Index("ix_review_items_project_state", "project_id", "state"),
        Index("ix_review_items_scene", "scene_id"),
    )

    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_type: Mapped[str] = mapped_column(String)
    target_collection: Mapped[str] = mapped_column(
        String,
        Computed(
            "CASE "
            "WHEN item_type = 'style_observation' THEN 'style_observations' "
            "WHEN item_type = 'style_rule_set' THEN 'style_rules' "
            "WHEN item_type = 'banned_rule_cluster' THEN 'banned_rule_clusters' "
            "WHEN item_type = 'narrative_pattern' THEN 'narrative_patterns' "
            "WHEN item_type = 'voice_card_candidate' THEN 'voice_cards' "
            "WHEN item_type = 'relation_card_candidate' THEN 'relation_cards' "
            "WHEN item_type = 'world_rule' THEN 'world_rules' "
            "WHEN item_type = 'calibration_candidate' THEN 'calibration_lines' "
            "WHEN item_type = 'foreshadow_open' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'foreshadow_touch' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'foreshadow_resolve' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'scene_memory' THEN 'scene_memories' "
            "WHEN item_type = 'scene_summary' THEN 'scene_memories' "
            "WHEN item_type = 'chapter_summary' THEN 'chapter_memories' "
            "WHEN item_type = 'author_preference_profile' THEN 'author_preference_profiles' "
            "WHEN item_type = 'longform_structure_guidance' THEN 'longform_structure_guidance' "
            "ELSE 'review_items' END",
            persisted=True,
        ),
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    candidate_text: Mapped[str] = mapped_column(Text)
    candidate_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active_on_approve: Mapped[int] = mapped_column(Integer, default=1)
    materialize_status: Mapped[str] = mapped_column(String, default="pending")
    approved_item_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # FE-ALIGN P5: 待办收件箱卡片模型（原型 ws-review 五类卡；legacy 行这些列为 NULL，
    # 响应里把 status pending/approved/rejected 映射成统一 state open/resolved）。
    # 卡片行 item_type="fe_card"、status 恒 "pending"（CheckConstraint 兼容），生命周期走 state。
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    card_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    actions_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_action_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ReviewDerivedSnooze(Base):
    """FE-ALIGN P5: 实时派生待办的稍后记录（按内容指纹 id 存——指纹变化即重新浮现）。"""

    __tablename__ = "review_derived_snoozes"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class HumanReviewEvent(Base):
    __tablename__ = "human_review_events"
    __table_args__ = (
        Index("ix_human_review_events_scene", "scene_id"),
        Index("ix_human_review_events_status", "status"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_human_review_events_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_human_review_events_chapter_id"),
        nullable=True,
    )
    object_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    event_source: Mapped[str] = mapped_column(String, default="system")
    priority: Mapped[str] = mapped_column(String, default="normal")
    owner: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")
    allowed_actions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_status_map_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    default_action: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "HumanReviewEvent",
    "PassagePatchCandidate",
    "ReviewDerivedSnooze",
    "ReviewItem",
    "RevisionCandidate",
    "WriterEvaluation",
]

"""评审：写作评审、修订候选、局部补丁候选、人工审阅事件。

待办收件箱 2026-10-21 退役：它的卡片表 ``review_items`` 与派生项的稍后记录 ``review_derived_snoozes`` 随迁移
``20261021_0102`` 删掉（剩下的行导出到库文件旁的 JSON）；提醒改由前端从目录现算（``ws-attention.js``）。
"""

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
    "RevisionCandidate",
    "WriterEvaluation",
]

"""权威正文与正史：终稿、场景 / 章记忆、卷汇总、章滚动笔记、叙事事件、正史提交、事实候选、连续性快照。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class FinalScene(Base):
    __tablename__ = "final_scenes"
    __table_args__ = (
        Index("ix_final_scenes_scene", "scene_id"),
        Index("ix_final_scenes_chapter", "chapter_id"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_final_scenes_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_final_scenes_chapter_id")
    )
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="approved")
    source_bundle_id: Mapped[str] = mapped_column(String)
    source_bundle_hash: Mapped[str] = mapped_column(String)
    source_kind: Mapped[str] = mapped_column(String, default="generation")
    source_author_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_author_draft_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_by_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="system")
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneMemory(Base):
    __tablename__ = "scene_memories"
    __table_args__ = (
        Index("ix_scene_memories_scene", "scene_id"),
        Index("ix_scene_memories_chapter", "chapter_id"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    carry_notes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_bundle_id: Mapped[str] = mapped_column(String)
    final_scene_row_id: Mapped[str] = mapped_column(String)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="direct_read")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class ChapterMemory(Base):
    __tablename__ = "chapter_memories"
    __table_args__ = (Index("ix_chapter_memories_chapter", "chapter_id"),)

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(String)
    aggregate_stage: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    # §2 summary tower: "事实从日志查，氛围从摘要读". memory_kind labels how the
    # content may be used downstream — "mixed" (legacy, both), "factual" (state
    # cross-reference only), "atmosphere" (tone/mood far-horizon, never as facts).
    memory_kind: Mapped[str] = mapped_column(String, default="mixed")
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class VolumeSummary(Base):
    """§2 summary tower — volume/book-level far-horizon ATMOSPHERE summary.

    Blueprint §2: the summary tower is a read-only auxiliary layer supplying
    far-horizon tone/atmosphere context. It must NEVER be a fact-bearing source —
    facts are projected from the event log. This rolls up chapter memories into a
    volume-level digest used as far-horizon mood context for generation.
    """
    __tablename__ = "volume_summaries"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    volume_seq: Mapped[int] = mapped_column(Integer)
    chapter_id_start: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id_end: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    # Atmosphere-only far-horizon context (tone, mood, thematic arc). NOT facts.
    atmosphere_summary: Mapped[str] = mapped_column(Text, default="")
    # Optional structured factual digest derived from event log (state milestones).
    # 声明未实现：无生成路径，恒 NULL（蓝图 §2 的事实摘要尚未落地）。
    factual_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="direct_read")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ChapterRollingNote(Base):
    __tablename__ = "chapter_rolling_notes"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(String, unique=True)
    chapter_id: Mapped[str] = mapped_column(String)
    source_scene_memory_row_id: Mapped[str] = mapped_column(String)
    note_text: Mapped[str] = mapped_column(Text)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class NarrativeEvent(Base):
    """Append-only narrative event log — the single source of truth for story state.

    Every fact about characters, locations, relationships, and information flow
    is recorded as an event tied to a scene. Character state at any point is
    reconstructed by replaying events up to that scene.
    """
    __tablename__ = "narrative_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, index=True)
    scene_id: Mapped[str] = mapped_column(String, index=True)
    chapter_id: Mapped[str] = mapped_column(String, index=True)
    scene_seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    fact_key: Mapped[str] = mapped_column(String)
    fact_value: Mapped[str] = mapped_column(String)
    confidence: Mapped[str] = mapped_column(String, default="high")
    causal_predecessor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Blueprint §2: each event carries theme tags for theme-aware queries
    theme_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    # Blueprint §2: forward-pointing obligation IDs (foreshadow / causal obligations)
    obligation_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    source_text_excerpt: Mapped[str | None] = mapped_column(String, nullable=True)
    # Fail closed: unspecified writes are plans, never runtime canon. Extractor
    # rows explicitly use pending; only the canon service may promote to accepted.
    authority_status: Mapped[str] = mapped_column(String, default="planned")
    source_kind: Mapped[str] = mapped_column(String, default="legacy_plan")
    final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)

    __table_args__ = (
        Index("ix_narrative_events_entity_scene", "entity_id", "scene_seq"),
        Index("ix_narrative_events_project_scene", "project_id", "scene_seq"),
        Index(
            "ix_narrative_events_project_chapter_scene",
            "project_id",
            "chapter_id",
            "scene_id",
        ),
        Index(
            "ix_narrative_events_project_entity_scene",
            "project_id",
            "entity_id",
            "scene_id",
        ),
        Index(
            "ix_narrative_events_authority_project_scene",
            "authority_status",
            "project_id",
            "scene_id",
        ),
        Index("ix_narrative_events_final_scene", "final_scene_row_id"),
        Index("ix_narrative_events_canon_commit", "canon_commit_id"),
        CheckConstraint(
            "authority_status IN ('accepted','pending','rejected','planned','superseded')",
            name="ck_narrative_events_authority_status",
        ),
    )


class CanonCommit(Base):
    """正文事实经过作者/规则裁决后的不可变正史提交。"""

    __tablename__ = "canon_commits"
    __table_args__ = (
        Index(
            "ix_canon_commits_project_scene_final",
            "project_id",
            "scene_id",
            "final_scene_row_id",
        ),
        Index("ix_canon_commits_chapter", "chapter_id"),
        Index("ix_canon_commits_scene", "scene_id"),
        Index("ix_canon_commits_final_scene", "final_scene_row_id"),
        Index("ix_canon_commits_source_final_scene", "source_final_scene_row_id"),
        CheckConstraint(
            "status IN ('active','superseded')",
            name="ck_canon_commits_status",
        ),
        CheckConstraint(
            "commit_kind IN ('candidate_acceptance','author_verification','facts_unchanged')",
            name="ck_canon_commits_commit_kind",
        ),
    )

    commit_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    final_scene_row_id: Mapped[str] = mapped_column(ForeignKey("final_scenes.row_id"))
    final_content_hash: Mapped[str] = mapped_column(String)
    commit_kind: Mapped[str] = mapped_column(String, default="candidate_acceptance")
    candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="active")
    actor_ref: Mapped[str] = mapped_column(String, default="operator")
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class FactCandidate(Base):
    """从终稿抽取、尚未成为事实的可审计候选。"""

    __tablename__ = "fact_candidates"
    __table_args__ = (
        Index(
            "ix_fact_candidates_project_chapter_status",
            "project_id",
            "chapter_id",
            "status",
        ),
        Index("ix_fact_candidates_scene_status", "scene_id", "status"),
        Index("ix_fact_candidates_final_scene", "final_scene_row_id"),
        Index("ix_fact_candidates_chapter", "chapter_id"),
        Index("ix_fact_candidates_planned_timeline", "planned_timeline_event_id"),
        Index("ix_fact_candidates_canon_commit", "canon_commit_id"),
        UniqueConstraint("staged_event_id", name="ux_fact_candidates_staged_event"),
        CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded')",
            name="ck_fact_candidates_status",
        ),
        CheckConstraint(
            "entity_resolution_status IN ('exact','alias','ambiguous','unresolved','manual')",
            name="ck_fact_candidates_entity_resolution_status",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    final_scene_row_id: Mapped[str] = mapped_column(ForeignKey("final_scenes.row_id"))
    staged_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("narrative_events.event_id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String)
    raw_entity_ref: Mapped[str] = mapped_column(String)
    resolved_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_resolution_status: Mapped[str] = mapped_column(String, default="unresolved")
    entity_candidates_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    fact_key: Mapped[str] = mapped_column(String)
    fact_value: Mapped[str] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="prose_extraction")
    confidence: Mapped[str] = mapped_column(String, default="extracted")
    criticality: Mapped[str] = mapped_column(String, default="critical")
    planned_timeline_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("timeline_events.event_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ContinuitySnapshot(Base):
    """可重建的结构化连续性投影；原始正文仍由 FinalScene 保存。"""

    __tablename__ = "continuity_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "scope_type",
            "scope_id",
            name="ux_continuity_snapshots_scope",
        ),
        Index("ix_continuity_snapshots_chapter", "chapter_id", "scope_type"),
        Index("ix_continuity_snapshots_scene", "scene_id"),
        Index("ix_continuity_snapshots_final_scene", "final_scene_row_id"),
        Index("ix_continuity_snapshots_latest_commit", "latest_commit_id"),
        CheckConstraint(
            "scope_type IN ('scene','chapter')",
            name="ck_continuity_snapshots_scope_type",
        ),
        CheckConstraint(
            "status IN ('pending','complete','degraded','superseded')",
            name="ck_continuity_snapshots_status",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    scope_type: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id"), nullable=True
    )
    final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    latest_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    summary_text: Mapped[str] = mapped_column(Text, default="")
    state_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    knowledge_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    relationship_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    item_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    timeline_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    open_obligations_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_commit_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "CanonCommit",
    "ChapterMemory",
    "ChapterRollingNote",
    "ContinuitySnapshot",
    "FactCandidate",
    "FinalScene",
    "NarrativeEvent",
    "SceneMemory",
    "VolumeSummary",
]

"""权威正文与正史：终稿、场景 / 章记忆、故事记忆（章摘要、线索标记、编辑通读与意见）、章滚动笔记、叙事事件、正史提交、事实候选、
连续性快照。"""

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


class ChapterDigest(Base):
    """故事记忆的章摘要（``services/story_memory.py``）：一章写完之后由 LLM 写的事实摘要——发生了什么、章末停在哪、
    人物的持久变化、还没收的线（一章章往下带）。起草后面各章时进「前情」（Story So Far）。

    一章同时只有一条 ``active``；重写 / 作者修改都另起一行、旧行 ``superseded``。``source_hash`` 绑住生成时读到的
    整章正文（``body_hash``）与上一章那份摘要（``previous_digest_hash``）：任一变了就判旧，后台重写——作者改过的
    （``origin = author``）不自动重写，只提示作者看一眼。取代 2026-06 的卷汇总（原文拼接、只标「氛围」）。
    """

    __tablename__ = "chapter_digests"
    __table_args__ = (
        CheckConstraint("status IN ('active','superseded')", name="ck_chapter_digests_status"),
        CheckConstraint("origin IN ('llm','author')", name="ck_chapter_digests_origin"),
        Index("ix_chapter_digests_chapter_status", "chapter_id", "status"),
        Index("ix_chapter_digests_project", "project_id"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    origin: Mapped[str] = mapped_column(String, default="llm")
    # 生成 / 修改时读到的：整章正文的哈希、上一章那份摘要（行 id 与内容哈希），合起来的 source_hash
    body_hash: Mapped[str] = mapped_column(String, default="")
    previous_digest_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    previous_digest_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_hash: Mapped[str] = mapped_column(String, default="")
    # {summary, ending_state, character_changes[], threads[]}——threads 是这一章对线索台账的更新（埋下 / 推进 / 兑现），
    # 台账本身由各章的这份更新按故事顺序推出来（story_memory_digest.fold_threads），不另存
    digest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String, default="")
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StoryThreadMark(Base):
    """作者对线索台账里一条线的标记（``story_memory``）：``dropped``（放下，不再提醒、不进起草）或 ``resolved``（作者判定
    已经收了）。台账由章摘要推出来，这张表只是叠在上面的一层；``label_snapshot`` 是标记时那条线的文字——章摘要重写后
    同一个 id 换成了另一条线（文字对不上），标记就不再生效。取消标记 = 删掉这一行。
    """

    __tablename__ = "story_thread_marks"
    __table_args__ = (
        CheckConstraint("status IN ('dropped','resolved')", name="ck_story_thread_marks_status"),
        UniqueConstraint("project_id", "thread_id", name="uq_story_thread_marks_thread"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    thread_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    label_snapshot: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class EditorialReview(Base):
    """编辑通读（``story_editorial``）：每写完几章，AI 读全书设计、各章摘要、线索台账、最近几章正文与后面几章的计划，
    写一份问题清单（``review_json``）和给后面几章的意见（``EditorialGuidance``）。

    ``through_chapter_id`` 是读到的最后一章，``window_chapter_ids_json`` 是意见针对的后面几章。一部作品同时只有一份
    ``active``（最新的一份）；新的一份存下时旧的 ``superseded``，旧份里还没决定的意见随之作废，已采纳的照旧生效。
    """

    __tablename__ = "editorial_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('active','superseded')", name="ck_editorial_reviews_status"),
        CheckConstraint("origin IN ('auto','manual')", name="ck_editorial_reviews_origin"),
        Index("ix_editorial_reviews_project_status", "project_id", "status"),
    )

    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    through_chapter_id: Mapped[str] = mapped_column(String)
    window_chapter_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="active")
    origin: Mapped[str] = mapped_column(String, default="auto")
    source_hash: Mapped[str] = mapped_column(String, default="")
    # {assessment, findings[]}
    review_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class EditorialGuidance(Base):
    """编辑通读给后面某一章（``chapter_id``）或整段后面几章（``chapter_id`` 为空 = 这份通读的 window）的一条意见。

    ``proposed`` → 作者 ``accepted``（可以改字，存在 ``author_text``）/ ``dismissed``；新一份通读存下时还没决定的变成
    ``superseded``。只有 ``accepted`` 的进那一章各场的起草与场景蓝图（``editorial_guidance`` 段）。
    """

    __tablename__ = "editorial_guidance"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','accepted','dismissed','superseded')", name="ck_editorial_guidance_status"
        ),
        Index("ix_editorial_guidance_review", "review_id"),
        Index("ix_editorial_guidance_project_status", "project_id", "status"),
    )

    guidance_id: Mapped[str] = mapped_column(String, primary_key=True)
    review_id: Mapped[str] = mapped_column(String, ForeignKey("editorial_reviews.review_id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    author_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="proposed")
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
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
    "ChapterDigest",
    "ChapterMemory",
    "ChapterRollingNote",
    "ContinuitySnapshot",
    "FactCandidate",
    "FinalScene",
    "NarrativeEvent",
    "SceneMemory",
]

"""作者稿：稿、续写候选、事件、修订快照，以及旧的作者偏好画像表。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class AuthorPreferenceProfile(Base):
    __tablename__ = "author_preference_profiles"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global','genre','project','chapter')",
            name="ck_author_preference_profiles_scope_type",
        ),
        CheckConstraint(
            "status IN ('draft','approved','rejected','superseded')",
            name="ck_author_preference_profiles_status",
        ),
        CheckConstraint(
            "runtime_eligible IN (0,1)",
            name="ck_author_preference_profiles_runtime_eligible",
        ),
    )

    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, default="global")
    scope_ref_id: Mapped[str] = mapped_column(String, default="global")
    status: Mapped[str] = mapped_column(String, default="draft")
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_patch_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String, default="writer_deep_review")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraft(Base):
    __tablename__ = "author_drafts"
    __table_args__ = (
        CheckConstraint("object_type IN ('scene','chapter','project')", name="ck_author_drafts_object_type"),
        CheckConstraint("status IN ('current','superseded','archived')", name="ck_author_drafts_status"),
        # 「这一场 / 这一章的当前作者稿」：十来处按（对象、状态）取最新一份（迁移 20260929_0095）
        Index("ix_author_drafts_object", "object_type", "object_id", "status", "updated_at"),
    )

    draft_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    # 草稿保存与权威正文提升是两个独立动作；这两个字段记录最近一次成功提升，
    # 也为 promote-canonical 提供 revision + FinalScene 双重 CAS 的持久化证据。
    last_promoted_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_promoted_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="current")
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    updated_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraftProposal(Base):
    __tablename__ = "author_draft_proposals"
    __table_args__ = (
        CheckConstraint("object_type IN ('scene','chapter','project')", name="ck_author_draft_proposals_object_type"),
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_author_draft_proposals_status",
        ),
    )

    proposal_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    proposal_type: Mapped[str] = mapped_column(String, default="scene_draft")
    proposal_source: Mapped[str] = mapped_column(String, default="single_request")
    content: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_range_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    before_text_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    replacement_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_kind: Mapped[str] = mapped_column(String, default="whole_draft")
    source_evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merge_status: Mapped[str] = mapped_column(String, default="pending")
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="author_draft_proposal")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraftEvent(Base):
    __tablename__ = "author_draft_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'created','edited','candidate_inserted','candidate_saved','candidate_rejected',"
            "'proposal_applied','proposal_rejected'"
            ")",
            name="ck_author_draft_events_type",
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    patch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    option_id: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class AuthorDraftRevision(Base):
    """正文修订快照（FE-ALIGN F2）：一行一份完整内容，记着它是第几版（``revision_no``）。

    不是每推进一个修订号就一行：写作台的自动保存（``origin=edited``）按 5 分钟时段并成一行，只留这一时段最新的
    正文（批准 #8）；建稿（``created``）、采纳并归档（``adopted``）、晋升过的那一版和整段删改之前的那一版各自
    单独留着（规则见 ``services/author_drafts/revisions.py``）。"""

    __tablename__ = "author_draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision_no", name="uq_author_draft_revisions_draft_rev"),
        Index("ix_author_draft_revisions_draft", "draft_id"),
    )

    draft_revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    revision_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    words: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String, default="edited")
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


__all__ = [
    "AuthorDraft",
    "AuthorDraftEvent",
    "AuthorDraftProposal",
    "AuthorDraftRevision",
    "AuthorPreferenceProfile",
]

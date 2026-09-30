"""目录：章与场景卡（物化后的生产单元），以及声线卡 / 关系卡两张旧表。"""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class ChapterGoal(Base):
    __tablename__ = "chapter_goals"
    __table_args__ = (
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0",
            name="ck_chapter_goals_display_order_nonnegative",
        ),
        Index(
            "ix_chapter_goals_project_display_order",
            "project_id",
            "display_order",
            "chapter_id",
        ),
        Index(
            "ux_chapter_goals_active_project_display_order",
            "project_id",
            "display_order",
            unique=True,
            sqlite_where=text(
                "trashed_flag = 0 AND project_id IS NOT NULL "
                "AND display_order IS NOT NULL"
            ),
            postgresql_where=text(
                "trashed_flag = 0 AND project_id IS NOT NULL "
                "AND display_order IS NOT NULL"
            ),
        ),
    )

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("story_projects.project_id"), nullable=True)
    outline_plan_id: Mapped[str | None] = mapped_column(ForeignKey("outline_plans.plan_id"), nullable=True)
    planned_scene_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mid_aggregate_enabled: Mapped[int] = mapped_column(Integer, default=0)
    chapter_goal: Mapped[str] = mapped_column(Text)
    # FE-ALIGN P3 目录统一：叙事卡（act/tension/pov/entry/exit/promise/drama/threads/title）、
    # 章状态、目标字数、显示顺序（混合 id 格式下不能依赖 chapter_id 字典序）。
    narrative_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String, default="planned")
    words_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    main_plot_push: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotional_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    ending_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    must_not: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    writer_brief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneCard(Base):
    __tablename__ = "scene_cards"
    __table_args__ = (
        CheckConstraint(
            "scene_seq >= 1",
            name="ck_scene_cards_scene_seq_positive",
        ),
        Index(
            "ix_scene_cards_project_chapter_seq",
            "project_id",
            "chapter_id",
            "scene_seq",
            "scene_id",
        ),
        Index(
            "ux_scene_cards_active_chapter_scene_seq",
            "chapter_id",
            "scene_seq",
            unique=True,
            sqlite_where=text("trashed_flag = 0"),
            postgresql_where=text("trashed_flag = 0"),
        ),
    )

    scene_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("story_projects.project_id"), nullable=True)
    outline_plan_id: Mapped[str | None] = mapped_column(ForeignKey("outline_plans.plan_id"), nullable=True)
    scene_seq: Mapped[int] = mapped_column(Integer)
    pov_character_id: Mapped[str | None] = mapped_column(String, nullable=True)
    onstage_chars_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    resolved_relation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_goal: Mapped[str] = mapped_column(Text)
    beats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    must_include_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    forbidden_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    writer_brief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    target_length_band: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_type: Mapped[str | None] = mapped_column(String, nullable=True)
    is_chapter_last: Mapped[int] = mapped_column(Integer, default=0)
    # FE-ALIGN P3：场景写作状态（todo/writing/done）与当前正文字数
    # （正文保存时更新；排序复用既有 scene_seq，不另建 display_order）。
    state: Mapped[str] = mapped_column(String, default="todo")
    words_current: Mapped[int] = mapped_column(Integer, default=0)
    # Writer-side reminders are authoritative author data, not disposable
    # browser cache. The revision is used as a compare-and-swap fence between
    # browsers/devices.
    author_notes: Mapped[str] = mapped_column(Text, default="")
    author_notes_revision_no: Mapped[int] = mapped_column(Integer, default=0)
    deep_review_decision_log_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    deep_review_ignored_keys_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    deep_review_preferences_revision_no: Mapped[int] = mapped_column(Integer, default=0)
    # §16 "breathing gap" — author-facing slider; 0.0=free-flow, 1.0=full-rigor, NULL=auto (criticality-based)
    constraint_intensity: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    voice_profile_id: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    character_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class RelationProfile(Base):
    __tablename__ = "relation_profiles"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    relation_profile_id: Mapped[str] = mapped_column(String)
    left_character_id: Mapped[str] = mapped_column(String)
    right_character_id: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "ChapterGoal",
    "RelationProfile",
    "SceneCard",
    "VoiceProfile",
]

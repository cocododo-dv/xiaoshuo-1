"""作品与大纲：作品行、写作统计、大纲计划。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class StoryProject(Base):
    __tablename__ = "story_projects"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    target_word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_chapter_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FE-ALIGN P2: 作品档案字段（原型 WsWorks 作品对象 mark/accent/sub/今日目标）。
    mark: Mapped[str | None] = mapped_column(String, nullable=True)
    accent: Mapped[str | None] = mapped_column(String, nullable=True)
    synopsis_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    words_target_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outline_text: Mapped[str] = mapped_column(Text)
    planning_mode: Mapped[str] = mapped_column(String, default="outline_driven")
    snowflake_schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    snowflake_workflow_mode: Mapped[str] = mapped_column(String, default="strict")
    status: Mapped[str] = mapped_column(String, default="outline_draft")
    active_outline_plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_chapter_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # FE-ALIGN P4: 作品级软删（沿用章/场景 trash 的列名约定；级联只动可见性不动数据）
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ProjectWritingStats(Base):
    """FE-ALIGN P2: 每个项目一行的写作统计（D2：服务端计算，Asia/Shanghai）。

    today/streak 规则照抄原型 ws-catalog.jsx 的 catAddToday/catBumpStreak/
    catEffectiveStreak：当天首次正向增量记账；昨天也写过 +1 否则重记 1；
    断更超一天展示为 0（展示态在服务层算，不落库）。
    """

    __tablename__ = "project_writing_stats"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("story_projects.project_id"), primary_key=True
    )
    words_total: Mapped[int] = mapped_column(Integer, default=0)
    day: Mapped[str | None] = mapped_column(String, nullable=True)
    words_today: Mapped[int] = mapped_column(Integer, default=0)
    streak_last_day: Mapped[str | None] = mapped_column(String, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class OutlinePlan(Base):
    __tablename__ = "outline_plans"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "OutlinePlan",
    "ProjectWritingStats",
    "StoryProject",
]

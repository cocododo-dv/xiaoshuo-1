"""资料库：人物、实体、关系、时间线。"""

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
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class StoryCharacter(Base):
    __tablename__ = "story_characters"

    character_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    synopsis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    bible_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class LibraryEntity(Base):
    """资料库实体(地点/物品/阵营/设定等非人物对象)。

    人物的权威实体是 StoryCharacter,不在此表重复;资料库聚合接口
    会把两者合并输出。kind 用字符串常量(不新增 Enum):
    location / item / faction / concept。
    """

    __tablename__ = "library_entities"
    __table_args__ = (Index("ix_library_entities_project", "project_id"),)

    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    kind: Mapped[str] = mapped_column(String, default="concept")
    name: Mapped[str] = mapped_column(String)
    aliases_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    tags_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class LibraryRelation(Base):
    """资料库关系边。端点用带前缀的 ref:"character:<id>" 或 "entity:<id>"。"""

    __tablename__ = "library_relations"
    __table_args__ = (Index("ix_library_relations_project", "project_id"),)

    relation_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    from_ref: Mapped[str] = mapped_column(String)
    to_ref: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="related")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class TimelineEvent(Base):
    """FE-ALIGN P6: 资料库时间线事件（原型 ws-library 大事记 cat=events）。

    entity_refs_json 元素用带前缀 ref（"character:<id>" / "entity:<id>"），
    chapter_ref 是展示用章标记（如 "CH02" / "贯穿"），不强约束外键。
    """

    __tablename__ = "timeline_events"
    __table_args__ = (
        Index("ix_timeline_events_project", "project_id"),
        Index("ix_timeline_events_realized_canon_commit_id", "realized_canon_commit_id"),
        Index("ix_timeline_events_realized_scene_id", "realized_scene_id"),
        CheckConstraint(
            "event_mode IN ('planned','recorded')",
            name="ck_timeline_events_event_mode",
        ),
        CheckConstraint(
            "realization_status IN ('planned','realized')",
            name="ck_timeline_events_realization_status",
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    label: Mapped[str] = mapped_column(String)
    time_label: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_refs_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # planned = 作者意图；recorded = 仅作历史展示。正文真正兑现后通过
    # realized_canon_commit_id 指向不可变正史提交，不再靠双表内容猜测。
    event_mode: Mapped[str] = mapped_column(String, default="planned")
    realization_status: Mapped[str] = mapped_column(String, default="planned")
    realized_canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    realized_scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "LibraryEntity",
    "LibraryRelation",
    "StoryCharacter",
    "TimelineEvent",
]

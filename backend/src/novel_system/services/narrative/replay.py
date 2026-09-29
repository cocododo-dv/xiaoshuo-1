"""叙事事件账本的存取与重放：追加写入、按全书位置截到某一场之前、逐实体折叠成状态。

「事件溯源为唯一真相源」（蓝图 §2）：某一场之前的角色 / 地点 / 物品状态，就是把那之前的已认可事件按位置重放一遍。
重放只认两种事件：不归正史管理的已认可事件，和归正史管理、且带着与终稿哈希绑定的有效提交的事件
（runtime_authority_clause）——抽取出来还没核对的候选永远进不了重放。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    CanonCommit,
    FinalScene,
    NarrativeEvent,
)
from novel_system.services.errors import DomainError
from novel_system.services.narrative.taxonomy import (
    CANON_MANAGED_SOURCE_KINDS,
    SCENE_COMPLETION_COMMIT_KINDS,
)
from novel_system.services.narrative_position import NarrativePositionService


@dataclass(slots=True)
class ProjectedFact:
    entity_type: str
    entity_id: str
    fact_key: str
    fact_value: str
    scene_id: str
    event_id: str
    confidence: str = "high"

    @classmethod
    def from_event(cls, event: NarrativeEvent) -> ProjectedFact:
        return cls(
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            fact_key=event.fact_key,
            fact_value=event.fact_value,
            scene_id=event.scene_id,
            event_id=event.event_id,
            confidence=event.confidence,
        )


# 置信档：spec/规则事件=high(权威)，prose 抽取的 advisory 事件=extracted(顾问)。
# 重放在**同一场**内必须让高置信优先——advisory(LLM)事件不得反超 spec 事实，
# 否则 LLM 幻觉会覆盖「单一真相源」。跨场仍按最新事件演进（latest-wins）。
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0, "extracted": 0}


def _confidence_rank(confidence: str | None) -> int:
    return _CONFIDENCE_RANK.get((confidence or "high").strip().lower(), 1)


def fold_fact(facts: dict[str, ProjectedFact], event: NarrativeEvent) -> None:
    """把一条事件叠进某个实体的事实表：同一事实键取最新的一条，只是同一场里低置信的不盖过高置信的。"""
    existing = facts.get(event.fact_key)
    if (
        existing is not None
        and existing.scene_id == event.scene_id
        and _confidence_rank(existing.confidence) > _confidence_rank(event.confidence)
    ):
        return
    facts[event.fact_key] = ProjectedFact.from_event(event)


@dataclass(slots=True)
class EntityState:
    entity_type: str
    entity_id: str
    facts: dict[str, ProjectedFact] = field(default_factory=dict)

    def get(self, fact_key: str) -> str | None:
        f = self.facts.get(fact_key)
        return f.fact_value if f else None

    def as_dict(self) -> dict[str, str]:
        return {k: v.fact_value for k, v in sorted(self.facts.items())}


class CharacterState(EntityState):
    """角色的投影：按 entity_id 叠加这个角色名下的全部事件（不分 entity_type）。"""

    __slots__ = ()

    def __init__(
        self,
        character_id: str,
        facts: dict[str, ProjectedFact] | None = None,
    ) -> None:
        EntityState.__init__(
            self,
            entity_type="character",
            entity_id=character_id,
            facts=dict(facts or {}),
        )

    @property
    def character_id(self) -> str:
        return self.entity_id


def fold_events(events, state: EntityState) -> EntityState:
    """按给定次序把事件叠进 ``state``（调用方负责只给属于这个实体的事件）。"""
    for event in events:
        fold_fact(state.facts, event)
    return state


def require_scene_boundary(scene_seq: None, scene_id: str | None) -> str:
    """摘要只认「这一场之前」的场景边界。

    按章内 scene_seq 截断的旧游标已经删掉（B11-07：它是章内序号，多章作品里拿它当全书边界是错的，
    产品调用方早就只传 scene_id）；位置参数 ``scene_seq`` 留着只是为了照旧传 ``None`` 的调用方。
    """
    if scene_seq is not None:
        raise TypeError("the chapter-local scene_seq cursor was removed; pass scene_id")
    if not scene_id:
        raise TypeError("a narrative digest needs the scene_id it is written for")
    return scene_id


def runtime_authority_clause():
    """Accept canon-managed events only with a complete, hash-bound scene commit."""

    managed = NarrativeEvent.source_kind.in_(CANON_MANAGED_SOURCE_KINDS)
    event_commit_valid = exists(
        select(CanonCommit.commit_id).where(
            CanonCommit.commit_id == NarrativeEvent.canon_commit_id,
            CanonCommit.final_scene_row_id == NarrativeEvent.final_scene_row_id,
            CanonCommit.status == "active",
            CanonCommit.final_content_hash == FinalScene.content_hash,
            FinalScene.row_id == NarrativeEvent.final_scene_row_id,
        )
    )
    completion_commit_valid = exists(
        select(CanonCommit.commit_id).where(
            CanonCommit.final_scene_row_id == NarrativeEvent.final_scene_row_id,
            CanonCommit.project_id == NarrativeEvent.project_id,
            CanonCommit.scene_id == NarrativeEvent.scene_id,
            CanonCommit.status == "active",
            CanonCommit.commit_kind.in_(SCENE_COMPLETION_COMMIT_KINDS),
            CanonCommit.final_content_hash == FinalScene.content_hash,
            FinalScene.row_id == NarrativeEvent.final_scene_row_id,
        )
    )
    return and_(
        NarrativeEvent.authority_status == "accepted",
        or_(~managed, and_(event_commit_valid, completion_commit_valid)),
    )


class NarrativeEventStore:
    """事件账本的存取与按位置重放（追加写入、截到某场之前的已认可事件、逐实体折叠）。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.positions = NarrativePositionService(session)

    def _event_statement(
        self,
        project_id: str,
        *,
        before_scene_id: str | None = None,
        up_to_scene_id: str | None = None,
        descending: bool = False,
    ):
        if before_scene_id is not None and up_to_scene_id is not None:
            raise DomainError(
                "NARRATIVE_CURSOR_CONFLICT",
                "use exactly one narrative boundary",
                status_code=400,
            )
        statement = self.positions.event_statement(project_id).where(
            self._runtime_authority_clause()
        )
        if before_scene_id is not None:
            cursor = self.positions.cursor_for_scene(project_id, before_scene_id)
            statement = self.positions.before(statement, cursor)
        elif up_to_scene_id is not None:
            cursor = self.positions.cursor_for_scene(project_id, up_to_scene_id)
            statement = self.positions.before(statement, cursor, inclusive=True)
        return self.positions.ordered_events(statement, descending=descending)

    def events(
        self,
        project_id: str,
        *,
        before_scene_id: str | None = None,
        up_to_scene_id: str | None = None,
        event_type: str | None = None,
        entity_id: str | None = None,
        fact_key: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[NarrativeEvent]:
        statement = self._event_statement(
            project_id,
            before_scene_id=before_scene_id,
            up_to_scene_id=up_to_scene_id,
            descending=descending,
        )
        if event_type is not None:
            statement = statement.where(NarrativeEvent.event_type == event_type)
        if entity_id is not None:
            statement = statement.where(NarrativeEvent.entity_id == entity_id)
        if fact_key is not None:
            statement = statement.where(NarrativeEvent.fact_key == fact_key)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.execute(statement).scalars().all())

    def log_event(
        self,
        *,
        project_id: str,
        scene_id: str,
        chapter_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        fact_key: str,
        fact_value: str,
        confidence: str = "high",
        source_text_excerpt: str | None = None,
        payload: dict[str, Any] | None = None,
        authority_status: str = "planned",
        source_kind: str = "legacy_plan",
        final_scene_row_id: str | None = None,
        canon_commit_id: str | None = None,
    ) -> NarrativeEvent:
        cursor = self.positions.cursor_for_scene(project_id, scene_id)
        if cursor.chapter_id != chapter_id:
            raise DomainError(
                "NARRATIVE_EVENT_CHAPTER_MISMATCH",
                f"scene '{scene_id}' belongs to chapter '{cursor.chapter_id}', not '{chapter_id}'",
                status_code=409,
            )
        event = NarrativeEvent(
            event_id=f"nevt_{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            # 写入时的章内位置，只作记录：重放按场景卡的当前位置排序（narrative_position），
            # 场景挪动后这一列不跟着改。
            scene_seq=cursor.scene_seq,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            fact_key=fact_key,
            fact_value=fact_value,
            confidence=confidence,
            source_text_excerpt=source_text_excerpt,
            authority_status=authority_status,
            source_kind=source_kind,
            final_scene_row_id=final_scene_row_id,
            canon_commit_id=canon_commit_id,
            payload_json=payload or {},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def project_character_state(
        self,
        character_id: str,
        project_id: str,
        *,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> CharacterState:
        """Replay events to reconstruct character state. Latest fact per key wins."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                )
            .where(
                NarrativeEvent.entity_id == character_id,
            )
        )

        events = self.session.execute(query).scalars().all()
        return fold_events(events, CharacterState(character_id))

    def project_entity_state(
        self,
        entity_type: str,
        entity_id: str,
        project_id: str,
        *,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> EntityState:
        """Replay events to reconstruct any entity's state. Latest fact per key wins."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                )
            .where(
                NarrativeEvent.entity_type == entity_type,
                NarrativeEvent.entity_id == entity_id,
            )
        )

        events = self.session.execute(query).scalars().all()
        return fold_events(events, EntityState(entity_type=entity_type, entity_id=entity_id))

    def known_facts_for_character(
        self,
        character_id: str,
        project_id: str,
        *,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> list[ProjectedFact]:
        """All facts an entity has accumulated up to a scene — for POV filtering."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                )
            .where(
                NarrativeEvent.entity_id == character_id,
                NarrativeEvent.event_type == "character_learns",
            )
        )

        events = self.session.execute(query).scalars().all()
        return [ProjectedFact.from_event(evt) for evt in events]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _characters_in_project(self, project_id: str) -> list[str]:
        return self._entities_of_type_in_project(project_id, "character")

    def _entities_of_type_in_project(self, project_id: str, entity_type: str) -> list[str]:
        rows = self.session.execute(
            select(NarrativeEvent.entity_id)
            .where(
                NarrativeEvent.project_id == project_id,
                NarrativeEvent.entity_type == entity_type,
                self._runtime_authority_clause(),
            )
            .distinct()
        ).scalars().all()
        return list(rows)

    @staticmethod
    def _runtime_authority_clause():
        return runtime_authority_clause()

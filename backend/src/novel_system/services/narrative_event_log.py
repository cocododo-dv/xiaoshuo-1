"""Narrative event sourcing — append-only event log + deterministic state projection.

Blueprint §2: "事件溯源为唯一真相源"
Blueprint §17 Action B: minimal verifiable kernel

Event types:
  character_state   — physical/emotional state change
  character_learns  — character acquires information
  location_change   — character moves
  relation_change   — relationship shift
  item_change       — item gained/lost
  foreshadow_plant  — foreshadow set up
  foreshadow_resolve — foreshadow paid off

Entity types: character, location, item, relation, foreshadow
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    CanonCommit,
    ChapterGoal,
    FinalScene,
    NarrativeEvent,
    SceneCard,
)
from novel_system.services.llm_accounting import LLMAccountingRejected, LLMCallContext
from novel_system.services.narrative_contracts import INFORMATION_ASYMMETRY_FACT_KEYS
from novel_system.services.narrative_position import NarrativePositionService
from novel_system.services.errors import DomainError


EVENT_TYPES = (
    "character_state",
    "character_learns",
    "location_change",
    "relation_change",
    "item_change",
    "foreshadow_plant",
    "foreshadow_reinforce",
    "foreshadow_resolve",
)

ENTITY_TYPES = ("character", "location", "item", "relation", "foreshadow")


@dataclass(slots=True)
class ProjectedFact:
    entity_type: str
    entity_id: str
    fact_key: str
    fact_value: str
    scene_id: str
    scene_seq: int
    event_id: str
    confidence: str = "high"


# 置信档：spec/规则事件=high(权威)，prose 抽取的 advisory 事件=extracted(顾问)。
# 重放在**同一 scene_seq**内必须让高置信优先——advisory(LLM)事件不得反超 spec 事实，
# 否则 LLM 幻觉会覆盖「单一真相源」。跨 scene_seq 仍按最新事件演进（latest-wins）。
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0, "extracted": 0}


def _confidence_rank(confidence: str | None) -> int:
    return _CONFIDENCE_RANK.get((confidence or "high").strip().lower(), 1)


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


@dataclass(slots=True)
class CharacterState:
    character_id: str
    facts: dict[str, ProjectedFact] = field(default_factory=dict)

    def get(self, fact_key: str) -> str | None:
        f = self.facts.get(fact_key)
        return f.fact_value if f else None

    def as_dict(self) -> dict[str, str]:
        return {k: v.fact_value for k, v in sorted(self.facts.items())}


@dataclass(slots=True)
class ConsistencyViolation:
    fact_key: str
    expected: str
    actual: str
    entity_id: str
    evidence: str
    # §15: "keyword" = high-confidence deterministic match (blocking) — the only producer
    # since the advisory LLM flag layer (check_consistency_llm) was removed.
    source: str = "keyword"


@dataclass(slots=True)
class ConsistencyReport:
    passed: bool
    violations: list[ConsistencyViolation] = field(default_factory=list)
    facts_checked: int = 0


class NarrativeEventLog:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.positions = NarrativePositionService(session)

    def _event_statement(
        self,
        project_id: str,
        *,
        before_scene_id: str | None = None,
        up_to_scene_id: str | None = None,
        up_to_scene_seq: int | None = None,
        descending: bool = False,
    ):
        boundaries = sum(
            value is not None
            for value in (before_scene_id, up_to_scene_id, up_to_scene_seq)
        )
        if boundaries > 1:
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
        elif up_to_scene_seq is not None:
            self._require_unambiguous_legacy_cursor(project_id)
            # Backward compatibility for callers that operate on a one-chapter
            # project.  New runtime paths always pass a scene id.
            statement = statement.where(SceneCard.scene_seq <= up_to_scene_seq)
        return self.positions.ordered_events(statement, descending=descending)

    def _require_unambiguous_legacy_cursor(self, project_id: str) -> None:
        chapter_ids = set(
            self.session.execute(
                select(SceneCard.chapter_id)
                .join(ChapterGoal, ChapterGoal.chapter_id == SceneCard.chapter_id)
                .where(
                    SceneCard.trashed_flag == 0,
                    ChapterGoal.trashed_flag == 0,
                    or_(
                        SceneCard.project_id == project_id,
                        ChapterGoal.project_id == project_id,
                    ),
                )
                .distinct()
            ).scalars().all()
        )
        if not chapter_ids:
            chapter_ids = set(
                self.session.execute(
                    select(NarrativeEvent.chapter_id)
                    .where(
                        NarrativeEvent.project_id == project_id,
                        self._runtime_authority_clause(),
                    )
                    .distinct()
                ).scalars().all()
            )
        if len(chapter_ids) > 1:
            raise DomainError(
                "NARRATIVE_CURSOR_AMBIGUOUS",
                "scene_seq is chapter-local; use a scene_id boundary for multi-chapter replay",
                status_code=400,
            )

    def events(
        self,
        project_id: str,
        *,
        before_scene_id: str | None = None,
        up_to_scene_id: str | None = None,
        up_to_scene_seq: int | None = None,
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
            up_to_scene_seq=up_to_scene_seq,
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
        causal_predecessor_id: str | None = None,
        theme_tags: list[str] | None = None,
        obligation_ids: list[str] | None = None,
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
            scene_seq=cursor.scene_seq,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            fact_key=fact_key,
            fact_value=fact_value,
            confidence=confidence,
            causal_predecessor_id=causal_predecessor_id,
            theme_tags=theme_tags or [],
            obligation_ids=obligation_ids or [],
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

    def log_events_batch(self, events: list[dict[str, Any]]) -> list[NarrativeEvent]:
        result = []
        for evt in events:
            result.append(self.log_event(**evt))
        return result

    def project_character_state(
        self,
        character_id: str,
        project_id: str,
        *,
        up_to_scene_seq: int | None = None,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> CharacterState:
        """Replay events to reconstruct character state. Latest fact per key wins."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                up_to_scene_seq=up_to_scene_seq,
            )
            .where(
                NarrativeEvent.entity_id == character_id,
            )
        )

        events = self.session.execute(query).scalars().all()
        state = CharacterState(character_id=character_id)
        for evt in events:
            existing = state.facts.get(evt.fact_key)
            if (
                existing is not None
                and existing.scene_id == evt.scene_id
                and _confidence_rank(existing.confidence) > _confidence_rank(evt.confidence)
            ):
                continue  # 同场景内 advisory 不得反超高置信 spec 事实
            state.facts[evt.fact_key] = ProjectedFact(
                entity_type=evt.entity_type,
                entity_id=evt.entity_id,
                fact_key=evt.fact_key,
                fact_value=evt.fact_value,
                scene_id=evt.scene_id,
                scene_seq=evt.scene_seq,
                event_id=evt.event_id,
                confidence=evt.confidence,
            )
        return state

    def project_entity_state(
        self,
        entity_type: str,
        entity_id: str,
        project_id: str,
        *,
        up_to_scene_seq: int | None = None,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> EntityState:
        """Replay events to reconstruct any entity's state. Latest fact per key wins."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                up_to_scene_seq=up_to_scene_seq,
            )
            .where(
                NarrativeEvent.entity_type == entity_type,
                NarrativeEvent.entity_id == entity_id,
            )
        )

        events = self.session.execute(query).scalars().all()
        state = EntityState(entity_type=entity_type, entity_id=entity_id)
        for evt in events:
            existing = state.facts.get(evt.fact_key)
            if (
                existing is not None
                and existing.scene_id == evt.scene_id
                and _confidence_rank(existing.confidence) > _confidence_rank(evt.confidence)
            ):
                continue  # 同场景内 advisory 不得反超高置信 spec 事实
            state.facts[evt.fact_key] = ProjectedFact(
                entity_type=evt.entity_type,
                entity_id=evt.entity_id,
                fact_key=evt.fact_key,
                fact_value=evt.fact_value,
                scene_id=evt.scene_id,
                scene_seq=evt.scene_seq,
                event_id=evt.event_id,
                confidence=evt.confidence,
            )
        return state

    def known_facts_for_character(
        self,
        character_id: str,
        project_id: str,
        *,
        up_to_scene_seq: int | None = None,
        up_to_scene_id: str | None = None,
        before_scene_id: str | None = None,
    ) -> list[ProjectedFact]:
        """All facts an entity has accumulated up to a scene — for POV filtering."""
        query = (
            self._event_statement(
                project_id,
                before_scene_id=before_scene_id,
                up_to_scene_id=up_to_scene_id,
                up_to_scene_seq=up_to_scene_seq,
            )
            .where(
                NarrativeEvent.entity_id == character_id,
                NarrativeEvent.event_type == "character_learns",
            )
        )

        events = self.session.execute(query).scalars().all()
        return [
            ProjectedFact(
                entity_type=evt.entity_type,
                entity_id=evt.entity_id,
                fact_key=evt.fact_key,
                fact_value=evt.fact_value,
                scene_id=evt.scene_id,
                scene_seq=evt.scene_seq,
                event_id=evt.event_id,
                confidence=evt.confidence,
            )
            for evt in events
        ]

    def all_facts_at_scene(
        self,
        project_id: str,
        scene_seq: int | None = None,
        *,
        scene_id: str | None = None,
    ) -> dict[str, CharacterState]:
        """Project all character states at a given scene. Returns {character_id: CharacterState}."""
        query = self._event_statement(
            project_id,
            up_to_scene_id=scene_id,
            up_to_scene_seq=scene_seq if scene_id is None else None,
        ).where(NarrativeEvent.entity_type == "character")
        events = self.session.execute(query).scalars().all()

        states: dict[str, CharacterState] = {}
        for evt in events:
            state = states.setdefault(evt.entity_id, CharacterState(character_id=evt.entity_id))
            existing = state.facts.get(evt.fact_key)
            if (
                existing is not None
                and existing.scene_id == evt.scene_id
                and _confidence_rank(existing.confidence) > _confidence_rank(evt.confidence)
            ):
                continue  # 同场景内 advisory 不得反超高置信 spec 事实
            state.facts[evt.fact_key] = ProjectedFact(
                entity_type=evt.entity_type,
                entity_id=evt.entity_id,
                fact_key=evt.fact_key,
                fact_value=evt.fact_value,
                scene_id=evt.scene_id,
                scene_seq=evt.scene_seq,
                event_id=evt.event_id,
                confidence=evt.confidence,
            )
        return states

    def check_consistency(
        self,
        generated_text: str,
        project_id: str,
        scene_id: str,
        *,
        character_ids: list[str] | None = None,
    ) -> ConsistencyReport:
        """Check generated text against projected character state for contradictions.

        Checks hard facts (location, physical_state, alive) against text content.
        This is the "one incremental consistency check" from blueprint §17 Action B.
        """
        self.positions.cursor_for_scene(project_id, scene_id)
        if character_ids:
            chars = character_ids
        else:
            chars = self._characters_in_project(project_id)

        violations: list[ConsistencyViolation] = []
        facts_checked = 0
        text_lower = generated_text.lower()
        # Known location names in this project — used by the location detector to
        # distinguish "character is at the WRONG named place" from generic prose.
        # Locations live both as location entities AND as `location` facts asserted
        # on characters (location_change events), so gather both.
        known_locations = {
            loc.lower()
            for loc in self._entities_of_type_in_project(project_id, "location")
            if loc
        }
        loc_values = self.session.execute(
            select(NarrativeEvent.fact_value).where(
                NarrativeEvent.project_id == project_id,
                NarrativeEvent.fact_key == "location",
                self._runtime_authority_clause(),
            ).distinct()
        ).scalars().all()
        known_locations |= {v.lower() for v in loc_values if v}

        for char_id in chars:
            state = self.project_character_state(
                char_id, project_id, before_scene_id=scene_id,
            )
            for fact_key, projected in state.facts.items():
                if fact_key not in _CHECKABLE_FACT_KEYS:
                    continue
                facts_checked += 1
                violation = _check_fact_against_text(
                    text_lower, char_id, fact_key, projected.fact_value,
                    known_locations=known_locations,
                )
                if violation is not None:
                    violations.append(violation)

        return ConsistencyReport(
            passed=len(violations) == 0,
            violations=violations,
            facts_checked=facts_checked,
        )

    def format_state_for_prompt(
        self,
        project_id: str,
        scene_seq: int | None = None,
        *,
        scene_id: str | None = None,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> str:
        """Format projected entity states as a prompt section for injection.

        Wave 4（§5.6）：这是**写作提示词**槽位。当指定 ``pov_character_id`` 时，委派
        `PovKnowledgeProjection` 做 POV 减法投影，隐藏非 POV 秘密内容；``pov=None``
        保持全知视角全量注入（逐字节不变）。**硬 QC 不走此方法**——它读
        `project_character_state` / `all_facts_at_scene` 的全量权威状态，不受投影影响。
        """
        if pov_character_id:
            from novel_system.services.pov_knowledge_projection import (
                PovKnowledgeProjection,
            )
            return PovKnowledgeProjection(self.session, event_log=self).format_state_for_prompt(
                project_id, scene_seq,
                scene_id=scene_id,
                pov_character_id=pov_character_id,
                onstage_character_ids=onstage_character_ids,
            )
        boundary = (
            {"before_scene_id": scene_id}
            if scene_id is not None
            else {"up_to_scene_seq": int(scene_seq or 0) - 1}
        )
        chars = onstage_character_ids or self._characters_in_project(project_id)
        lines: list[str] = []
        lines.append("## Authoritative Character State (from event log, do NOT contradict)")
        for char_id in chars:
            state = self.project_character_state(char_id, project_id, **boundary)
            if not state.facts:
                continue
            lines.append(f"\n### {char_id}")
            for key, value in sorted(state.as_dict().items()):
                lines.append(f"- {key}: {value}")

        if pov_character_id:
            known = self.known_facts_for_character(
                pov_character_id, project_id, **boundary,
            )
            if known:
                lines.append(f"\n### POV知识边界 ({pov_character_id} 已知信息)")
                for fact in known:
                    lines.append(f"- {fact.fact_key}: {fact.fact_value}")

        location_ids = self._entities_of_type_in_project(project_id, "location")
        if location_ids:
            loc_lines: list[str] = []
            for loc_id in location_ids:
                state = self.project_entity_state(
                    "location", loc_id, project_id, **boundary,
                )
                if state.facts:
                    loc_lines.append(f"\n### {loc_id}")
                    for key, value in sorted(state.as_dict().items()):
                        loc_lines.append(f"- {key}: {value}")
            if loc_lines:
                lines.append("\n## Authoritative Location State (from event log, do NOT contradict)")
                lines.extend(loc_lines)

        item_ids = self._entities_of_type_in_project(project_id, "item")
        if item_ids:
            item_lines: list[str] = []
            for item_id in item_ids:
                state = self.project_entity_state(
                    "item", item_id, project_id, **boundary,
                )
                if state.facts:
                    item_lines.append(f"\n### {item_id}")
                    for key, value in sorted(state.as_dict().items()):
                        item_lines.append(f"- {key}: {value}")
            if item_lines:
                lines.append("\n## Authoritative Item State (from event log, do NOT contradict)")
                lines.extend(item_lines)

        return "\n".join(lines) if len(lines) > 1 else ""

    def information_asymmetry_digest(
        self,
        project_id: str,
        scene_seq: int | None,
        onstage_character_ids: list[str],
        *,
        scene_id: str | None = None,
        pov_character_id: str | None = None,
    ) -> str:
        """Blueprint §2/§11: format information gaps between onstage characters for prompt injection.

        For each pair of onstage characters, identify what one knows that the other doesn't.
        Also surface active secrets and false beliefs.

        Wave 4（§5.6）：写作提示词槽位。指定 ``pov_character_id`` 时委派
        `PovKnowledgeProjection`——只展示 POV 独有认知，他人独有内容/秘密只给
        内容无关的盲区提示，绝不打印 "Secrets held by X" 正文。``pov=None`` 保持全量。
        """
        if pov_character_id:
            from novel_system.services.pov_knowledge_projection import (
                PovKnowledgeProjection,
            )
            return PovKnowledgeProjection(self.session, event_log=self).information_asymmetry_digest(
                project_id, scene_seq, onstage_character_ids,
                scene_id=scene_id,
                pov_character_id=pov_character_id,
            )
        boundary = (
            {"before_scene_id": scene_id}
            if scene_id is not None
            else {"up_to_scene_seq": int(scene_seq or 0) - 1}
        )
        if len(onstage_character_ids) < 2:
            return ""

        lines: list[str] = []
        lines.append("## Information Asymmetry (who knows what the other doesn't)")

        knowledge: dict[str, set[str]] = {}
        secrets: dict[str, list[str]] = {}
        false_beliefs: dict[str, list[str]] = {}

        for char_id in onstage_character_ids:
            facts = self.known_facts_for_character(char_id, project_id, **boundary)
            knowledge[char_id] = {f"{f.fact_key}:{f.fact_value}" for f in facts}

            state = self.project_character_state(char_id, project_id, **boundary)
            for fk, pf in state.facts.items():
                if fk == "secret_held_by":
                    secrets.setdefault(char_id, []).append(pf.fact_value)
                elif fk == "believes_false":
                    false_beliefs.setdefault(char_id, []).append(pf.fact_value)

        for i, char_a in enumerate(onstage_character_ids):
            for char_b in onstage_character_ids[i + 1:]:
                a_knows = knowledge.get(char_a, set())
                b_knows = knowledge.get(char_b, set())
                a_exclusive = a_knows - b_knows
                b_exclusive = b_knows - a_knows
                if a_exclusive or b_exclusive:
                    lines.append(f"\n### {char_a} ↔ {char_b}")
                    if a_exclusive:
                        lines.append(f"  {char_a} knows but {char_b} doesn't:")
                        for fact in list(a_exclusive)[:5]:
                            lines.append(f"    - {fact}")
                    if b_exclusive:
                        lines.append(f"  {char_b} knows but {char_a} doesn't:")
                        for fact in list(b_exclusive)[:5]:
                            lines.append(f"    - {fact}")

        for char_id in onstage_character_ids:
            if char_id in secrets:
                lines.append(f"\n### Secrets held by {char_id}")
                for s in secrets[char_id][:3]:
                    lines.append(f"  - {s}")
            if char_id in false_beliefs:
                lines.append(f"\n### False beliefs of {char_id}")
                for b in false_beliefs[char_id][:3]:
                    lines.append(f"  - {b}")

        return "\n".join(lines) if len(lines) > 1 else ""

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
        """Accept canon-managed events only with a complete, hash-bound scene commit."""

        managed = NarrativeEvent.source_kind.in_(
            ("canon_candidate_accepted", "canon_acceptance", "facts_unchanged")
        )
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
                CanonCommit.commit_kind.in_(("author_verification", "facts_unchanged")),
                CanonCommit.final_content_hash == FinalScene.content_hash,
                FinalScene.row_id == NarrativeEvent.final_scene_row_id,
            )
        )
        return and_(
            NarrativeEvent.authority_status == "accepted",
            or_(~managed, and_(event_commit_valid, completion_commit_valid)),
        )


_CHECKABLE_FACT_KEYS = {
    "alive",
    "location",
    "physical_state",
    "has_item",
    "missing_limb",
    "appearance",
    "ability",
}


# ---------------------------------------------------------------------------
# Hard-fact contradiction detection (blueprint §13 Step 6 / §17 Action B)
#
# Works at *clause* granularity with proximity + synonym matching instead of
# brittle adjacent-substring matching. This is what makes recall survive
# realistic prose ("他抬起右手攥紧剑柄") rather than only textbook-exact phrasings
# ("右手握"). Precision is held up by negative guards (memory / loss framing)
# and by requiring an action signal near the entity reference.
# ---------------------------------------------------------------------------

_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;：:\n\r，,、（）()「」『』“”\"'…—　]+")


def _split_clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]


def _clause_refers_to_entity(clauses: list[str], index: int, entity_lower: str) -> bool:
    clause = clauses[index]
    if entity_lower in clause:
        return True
    # Permit a tightly adjacent pronoun continuation ("林远……。他右手……")
    # without attributing a different named character's action to this entity.
    starts_with_pronoun = re.match(
        r"^\s*[‘’“”\"']*(?:他|她|其|he\b|she\b|his\b|her\b)", clause
    ) is not None
    if not starts_with_pronoun or index <= 0:
        return False
    if entity_lower in clauses[index - 1]:
        return True
    if index >= 2 and entity_lower in clauses[index - 2]:
        bridge = clauses[index - 1]
        return any(pronoun in bridge for pronoun in ("他", "她", "其", " he ", " she ", " his ", " her "))
    return False


def _all_idx(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = haystack.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _min_gap(clause: str, tokens_a: tuple[str, ...], tokens_b: tuple[str, ...]) -> int | None:
    """Smallest char distance between any A-token and any B-token within a clause."""
    pa = [i for t in tokens_a for i in _all_idx(clause, t)]
    pb = [i for t in tokens_b for i in _all_idx(clause, t)]
    if not pa or not pb:
        return None
    return min(abs(a - b) for a in pa for b in pb)


def _verb_after(clause: str, anchor: str, verbs: tuple[str, ...], window: int) -> str | None:
    """First verb appearing within *window* chars AFTER *anchor* (i.e. anchor is subject)."""
    for start in _all_idx(clause, anchor):
        seg = clause[start + len(anchor): start + len(anchor) + window]
        for v in verbs:
            if v in seg:
                return v
    return None


# alive==dead: a dead character performing a living action, unless memory/flashback framing
_DEATH_MEMORY_GUARDS = (
    "想起", "记得", "回忆", "忆起", "曾经", "生前", "死前", "临终", "遗言", "遗体",
    "遗物", "遗像", "尸", "已故", "亡", "坟", "墓", "灵位", "牌位", "画像", "祭", "悼",
    "梦", "若还", "如果还", "仿佛还", "好像还", "似乎还", "当年", "那时", "过去",
)
_ALIVE_ACTION_VERBS = (
    "说道", "说", "开口", "喊道", "喊", "叫道", "问道", "问", "答道", "回答", "点头",
    "摇头", "起身", "站起", "坐起", "转身", "抬手", "抬起", "伸手", "迈步", "走来",
    "走向", "走进", "走到", "拔出", "握住", "握紧", "睁开", "看向", "望向", "盯着",
    "拿起", "举起", "笑了", "招手", "摆手", "开门",
    "said", "spoke", "walked", "nodded", "smiled",
)

# missing_limb synonym groups (normalise right_arm / 右臂 / 右手 → one group)
_LIMB_GROUPS: dict[str, tuple[str, ...]] = {
    "right_arm": ("右臂", "右手", "右胳膊", "右手臂", "右臂膀", "right arm", "right hand"),
    "left_arm": ("左臂", "左手", "左胳膊", "左手臂", "左臂膀", "left arm", "left hand"),
    "right_leg": ("右腿", "右脚", "右膝", "right leg", "right foot"),
    "left_leg": ("左腿", "左脚", "左膝", "left leg", "left foot"),
}
_LIMB_ACTION_VERBS = (
    "握", "攥", "抓", "举", "抬", "挥", "拔", "持", "拿", "伸", "搭", "按", "拍", "捏",
    "扯", "拽", "推", "抱", "捧", "接", "指", "划", "端", "提", "掂", "甩", "挡",
    "gripped", "raised", "grabbed", "clenched", "held", "swung",
)
_BOTH_HANDS = ("双手", "两手", "两只手", "双臂", "两臂", "十指", "both hands")
_LIMB_MISSING_GUARDS = (
    "断", "残", "失去", "没有了", "空荡荡", "仅存", "唯一", "缺", "截", "空袖", "断口",
    "残肢", "伤口", "本应", "本该", "曾经的", "想起", "记得",
)

# location: broadened "still at the wrong place" phrasings
_STILL_AT_PHRASES = (
    "还在", "仍在", "仍旧在", "依旧在", "依然在", "还停留", "仍停留", "还留在", "仍留在",
    "还待在", "仍待在", "依旧待在", "依然待在", "还守在", "仍守在", "还困在", "仍困在",
    "迟迟没有离开", "迟迟未离开", "没有离开", "未曾离开", "尚未离开",
    "still at", "still in", "remained at", "remained in", "was still at",
)

# has_item lost: a character using an item they no longer possess
_ITEM_POSSESS_VERBS = (
    "拿出", "掏出", "取出", "抽出", "拔出", "拿起", "握住", "握紧", "握", "举起", "举",
    "挥舞", "挥", "抓起", "攥", "佩", "掂", "提着", "捧着", "抚摸", "擦拭",
    "pulled out", "drew", "raised", "gripped",
)
_ITEM_LOSS_GUARDS = (
    "失去", "丢了", "丢失", "没了", "不见了", "遗失", "早已不在", "曾经的", "已经没有",
    "不在手中", "空空", "已断", "碎了", "毁了", "想起", "记得", "回忆", "怀念",
)

# Deterministic coverage for structured physical-state facts.  Free-form values
# such as "tired" or "wounded" are intentionally not inferred as contradictions:
# they can legitimately change inside the scene and remain advisory-only.
_STATE_RECOVERY_GUARDS = (
    "醒来", "苏醒", "恢复意识", "康复", "痊愈", "重新能够", "不再", "曾经",
    "回忆", "梦见", "如果", "假如", "试图", "尝试", "却没能", "但失败",
    "woke", "awoke", "recovered", "used to", "remembered", "dreamed", "tried",
)
_STATE_NEGATION_GUARDS = (
    "不能", "无法", "没法", "未能", "没有", "不再", "差点", "险些", "试图", "尝试",
    "cannot", "can't", "could not", "couldn't", "unable", "failed to", "tried to",
)
_VISUAL_ACTIONS = (
    "看见", "看到", "望见", "瞥见", "瞧见", "目睹", "注视", "端详", "读到", "阅读",
    "saw", "looked", "watched", "read", "glimpsed",
)
_HEARING_ACTIONS = ("听见", "听到", "听清", "听出", "heard", "listened")
_SPEAKING_ACTIONS = (
    "说道", "说", "开口", "回答", "喊道", "叫道", "低语", "耳语",
    "said", "spoke", "answered", "whispered", "shouted",
)
_WALKING_ACTIONS = (
    "站起", "起身", "走向", "走到", "迈步", "奔跑", "跑向", "跳起",
    "stood", "walked", "ran", "jumped",
)
_ASSISTIVE_PERCEPTION_GUARDS = (
    "盲杖", "摸索", "听声辨位", "读屏", "屏幕阅读器", "借助", "助听器", "读唇",
    "braille", "screen reader", "hearing aid", "lip-read",
)

_COLOR_ALIASES: dict[str, tuple[str, ...]] = {
    "black": ("黑色", "乌黑", "墨黑", "黑发", "black"),
    "brown": ("棕色", "褐色", "栗色", "棕发", "brown", "chestnut"),
    "blond": ("金色", "金发", "浅金", "blond", "blonde", "golden"),
    "red": ("红色", "赤红", "红发", "red", "auburn"),
    "white": ("白色", "雪白", "银白", "白发", "银发", "white", "silver"),
    "gray": ("灰色", "灰白", "灰发", "gray", "grey"),
    "blue": ("蓝色", "湛蓝", "蓝眸", "blue"),
    "green": ("绿色", "碧绿", "绿眸", "green"),
}
_HAIR_ANCHORS = ("头发", "发丝", "发色", "长发", "短发", "hair")
_EYE_ANCHORS = ("眼睛", "眼眸", "瞳孔", "眸子", "eye", "eyes")
_APPEARANCE_MEMORY_GUARDS = ("曾经", "从前", "旧照", "照片", "回忆", "梦里", "假如", "伪装", "染成")

_ABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "swim": ("游泳", "泅水", "游水", "swim"),
    "read": ("阅读", "读书", "读到", "read"),
    "write": ("写字", "书写", "write"),
    "speak": _SPEAKING_ACTIONS,
    "see": _VISUAL_ACTIONS,
    "hear": _HEARING_ACTIONS,
    "walk": _WALKING_ACTIONS,
    "fly": ("飞行", "飞起", "腾空", "fly", "flew"),
    "magic": ("施法", "释放魔法", "念动咒语", "施展法术", "cast a spell", "used magic"),
}


def _clause_has_negated_or_nonactual_action(clause: str) -> bool:
    return any(guard in clause for guard in (*_STATE_NEGATION_GUARDS, *_STATE_RECOVERY_GUARDS))


def _physical_state_kind(value_lower: str) -> str | None:
    normalized = value_lower.replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("unconscious", "coma", "昏迷", "失去意识")):
        return "unconscious"
    if any(token in normalized for token in ("blind", "失明", "看不见")):
        return "blind"
    if any(token in normalized for token in ("deaf", "失聪", "听不见")):
        return "deaf"
    if any(token in normalized for token in ("mute", "失语", "不能说话")):
        return "mute"
    if any(token in normalized for token in ("paralyzed", "paralysed", "瘫痪", "无法行走")):
        return "paralyzed"
    return None


def _physical_state_missing_limb(value_lower: str) -> str | None:
    normalized = value_lower.replace("-", "_").replace(" ", "_")
    for group, words in _LIMB_GROUPS.items():
        group_tokens = (group, *words)
        if any(token in normalized for token in group_tokens) and any(
            marker in normalized
            for marker in ("severed", "amputated", "missing", "断", "截肢", "失去")
        ):
            return group
    return None


def _canonical_color(value_lower: str) -> str | None:
    for canonical, aliases in _COLOR_ALIASES.items():
        if any(alias in value_lower for alias in aliases):
            return canonical
    return None


def _appearance_contract(value_lower: str) -> tuple[str, str] | None:
    normalized = value_lower.replace("=", ":")
    if any(prefix in normalized for prefix in ("hair:", "hair_color:", "头发:", "发色:")):
        color = _canonical_color(normalized)
        return ("hair", color) if color else None
    if any(prefix in normalized for prefix in ("eye:", "eyes:", "eye_color:", "眼睛:", "瞳色:")):
        color = _canonical_color(normalized)
        return ("eyes", color) if color else None
    if normalized in {"bald", "光头", "秃头", "头发全无"}:
        return ("bald", "bald")
    return None


def _negative_ability_contract(value_lower: str) -> tuple[str, tuple[str, ...]] | None:
    normalized = value_lower.strip().replace("=", ":")
    prefixes = ("cannot:", "unable:", "lost:", "no:", "不能:", "无法:", "失去能力:")
    payload = next((normalized[len(prefix):].strip() for prefix in prefixes if normalized.startswith(prefix)), None)
    if payload is None:
        for suffix in ("_unavailable", "_lost", "_disabled"):
            if normalized.endswith(suffix):
                payload = normalized[: -len(suffix)]
                break
    if not payload:
        return None
    for canonical, aliases in _ABILITY_ALIASES.items():
        if payload == canonical or any(alias == payload for alias in aliases):
            return canonical, aliases
    # A structured negative contract may use a project-specific concrete action.
    # Match it literally, but never infer synonyms for arbitrary prose.
    if len(payload) >= 2:
        return payload, (payload,)
    return None


def _norm_limb_group(value_lower: str) -> str | None:
    """Map a missing_limb fact value (right_arm / 右臂 / 右手 …) to a canonical group key."""
    for key, words in _LIMB_GROUPS.items():
        if value_lower == key or value_lower in key or key in value_lower:
            return key
        for w in words:
            if w in value_lower or value_lower in w:
                return key
    return None


def _check_fact_against_text(
    text_lower: str,
    char_id: str,
    fact_key: str,
    fact_value: str,
    *,
    known_locations: set[str] | None = None,
) -> ConsistencyViolation | None:
    """Contradiction detection for hard facts (blueprint §15: hard facts only).

    Clause-level proximity + synonym matching so the detector survives realistic
    prose, not just textbook-exact phrasings. Returns at most one violation.
    Soft facts (tone, relationship nuance) are deliberately out of scope.
    """
    value_lower = fact_value.lower()
    char_lower = char_id.lower()
    clauses = _split_clauses(text_lower)
    known_locations = known_locations or set()

    # --- alive / dead: dead character performing a living action ---
    if fact_key == "alive" and value_lower == "dead":
        for clause in clauses:
            if char_lower not in clause:
                continue
            if any(g in clause for g in _DEATH_MEMORY_GUARDS):
                continue  # memory / flashback / corpse framing → not a contradiction
            verb = _verb_after(clause, char_lower, _ALIVE_ACTION_VERBS, window=10)
            if verb:
                return ConsistencyViolation(
                    fact_key=fact_key, expected="dead",
                    actual="appears alive in text",
                    entity_id=char_id, evidence=f"{char_id}…{verb}",
                )

    # --- location: character is still at the WRONG named place ---
    if fact_key == "location":
        wrong_locs = {loc for loc in known_locations if loc and loc != value_lower}
        for clause in clauses:
            if char_lower not in clause:
                continue
            if value_lower and value_lower in clause:
                continue  # correct location mentioned → assume consistent
            if not any(p in clause for p in _STILL_AT_PHRASES):
                continue
            present_wrong = next((w for w in wrong_locs if w in clause), None)
            if present_wrong:
                return ConsistencyViolation(
                    fact_key=fact_key, expected=fact_value,
                    actual=f"text places {char_id} at {present_wrong}",
                    entity_id=char_id, evidence=clause[:80],
                )

    # --- missing_limb: character uses a limb they no longer have ---
    if fact_key == "missing_limb" and value_lower:
        group = _norm_limb_group(value_lower)
        if group:
            limb_words = _LIMB_GROUPS[group]
            for clause_index, clause in enumerate(clauses):
                if not _clause_refers_to_entity(clauses, clause_index, char_lower):
                    continue
                if any(g in clause for g in _LIMB_MISSING_GUARDS):
                    continue  # clause describes the loss, not a use of the limb
                gap = _min_gap(clause, limb_words, _LIMB_ACTION_VERBS)
                if gap is not None and gap <= 6:
                    return ConsistencyViolation(
                        fact_key=fact_key, expected=f"missing: {fact_value}",
                        actual="text uses the missing limb",
                        entity_id=char_id, evidence=clause[:80],
                    )
                # using BOTH hands when one arm is gone (blueprint's 双手握剑 example)
                if group in ("right_arm", "left_arm"):
                    gap2 = _min_gap(clause, _BOTH_HANDS, _LIMB_ACTION_VERBS)
                    if gap2 is not None and gap2 <= 6:
                        return ConsistencyViolation(
                            fact_key=fact_key, expected=f"missing: {fact_value}",
                            actual="text uses both hands despite a missing arm",
                            entity_id=char_id, evidence=clause[:80],
                        )

    # --- has_item: character uses an item they lost ---
    if fact_key == "has_item" and value_lower.startswith("lost:"):
        lost_item = value_lower.replace("lost:", "").strip()
        if lost_item:
            for clause_index, clause in enumerate(clauses):
                if not _clause_refers_to_entity(clauses, clause_index, char_lower):
                    continue
                if lost_item not in clause:
                    continue
                if any(g in clause for g in _ITEM_LOSS_GUARDS):
                    continue
                gap = _min_gap(clause, (lost_item,), _ITEM_POSSESS_VERBS)
                if gap is not None and gap <= 6:
                    return ConsistencyViolation(
                        fact_key=fact_key, expected=f"item lost: {lost_item}",
                        actual="text shows character using the lost item",
                        entity_id=char_id, evidence=clause[:80],
                    )

    # --- physical_state: only explicit, stable incapacity contracts ---
    if fact_key == "physical_state":
        missing_limb = _physical_state_missing_limb(value_lower)
        if missing_limb:
            limb_violation = _check_fact_against_text(
                text_lower,
                char_id,
                "missing_limb",
                missing_limb,
                known_locations=known_locations,
            )
            if limb_violation is not None:
                return ConsistencyViolation(
                    fact_key=fact_key,
                    expected=fact_value,
                    actual=limb_violation.actual,
                    entity_id=char_id,
                    evidence=limb_violation.evidence,
                )

        state_kind = _physical_state_kind(value_lower)
        action_map: dict[str, tuple[str, ...]] = {
            "unconscious": tuple(dict.fromkeys((*_ALIVE_ACTION_VERBS, *_SPEAKING_ACTIONS, *_WALKING_ACTIONS))),
            "blind": _VISUAL_ACTIONS,
            "deaf": _HEARING_ACTIONS,
            "mute": _SPEAKING_ACTIONS,
            "paralyzed": _WALKING_ACTIONS,
        }
        if state_kind is not None:
            for clause in clauses:
                if char_lower not in clause or _clause_has_negated_or_nonactual_action(clause):
                    continue
                if state_kind in {"blind", "deaf"} and any(
                    guard in clause for guard in _ASSISTIVE_PERCEPTION_GUARDS
                ):
                    continue
                action = _verb_after(clause, char_lower, action_map[state_kind], window=14)
                if action:
                    return ConsistencyViolation(
                        fact_key=fact_key,
                        expected=fact_value,
                        actual=f"text shows an action incompatible with {state_kind}: {action}",
                        entity_id=char_id,
                        evidence=clause[:120],
                    )

    # --- appearance: structured hair/eye colour or baldness only ---
    if fact_key == "appearance":
        contract = _appearance_contract(value_lower)
        if contract is not None:
            feature, expected_color = contract
            anchors = _HAIR_ANCHORS if feature in {"hair", "bald"} else _EYE_ANCHORS
            for clause in clauses:
                if char_lower not in clause or any(g in clause for g in _APPEARANCE_MEMORY_GUARDS):
                    continue
                if feature == "bald":
                    if any(marker in clause for marker in ("一头长发", "满头长发", "浓密头发", "thick hair", "long hair")):
                        return ConsistencyViolation(
                            fact_key=fact_key,
                            expected=fact_value,
                            actual="text gives the character a full head of hair",
                            entity_id=char_id,
                            evidence=clause[:120],
                        )
                    continue
                expected_aliases = _COLOR_ALIASES.get(expected_color, ())
                if expected_aliases and _min_gap(clause, anchors, expected_aliases) is not None:
                    continue
                for actual_color, aliases in _COLOR_ALIASES.items():
                    if actual_color == expected_color:
                        continue
                    gap = _min_gap(clause, anchors, aliases)
                    if gap is not None and gap <= 5:
                        return ConsistencyViolation(
                            fact_key=fact_key,
                            expected=fact_value,
                            actual=f"text describes {feature} colour as {actual_color}",
                            entity_id=char_id,
                            evidence=clause[:120],
                        )

    # --- ability: explicit negative contracts (cannot:/unable:/lost:/no:) ---
    if fact_key == "ability":
        contract = _negative_ability_contract(value_lower)
        if contract is not None:
            ability_name, action_tokens = contract
            for clause in clauses:
                if char_lower not in clause or _clause_has_negated_or_nonactual_action(clause):
                    continue
                action = _verb_after(clause, char_lower, action_tokens, window=16)
                if action:
                    return ConsistencyViolation(
                        fact_key=fact_key,
                        expected=fact_value,
                        actual=f"text shows forbidden ability '{ability_name}': {action}",
                        entity_id=char_id,
                        evidence=clause[:120],
                    )

    return None

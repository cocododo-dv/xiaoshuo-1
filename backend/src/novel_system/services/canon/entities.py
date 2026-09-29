"""正史候选指的是谁：按人物 / 资料库实体的 id、显示名、别名解析，作者选定时核对归属。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from novel_system.db.models import FactCandidate, LibraryEntity, StoryCharacter
from novel_system.services.character_names import character_alias_values, normalized_name
from novel_system.services.errors import DomainError


class CanonEntitiesMixin:
    def _resolve_entity(self, project_id: str, entity_type: str, raw_ref: str) -> dict[str, Any]:
        normalized = self._normalized_name(raw_ref)
        exact_ids: list[str] = []
        alias_ids: list[str] = []
        if entity_type in {"character", "relation"}:
            rows = list(
                self.session.execute(
                    select(StoryCharacter).where(StoryCharacter.project_id == project_id)
                ).scalars().all()
            )
            for row in rows:
                if normalized in {
                    self._normalized_name(row.character_id),
                    self._normalized_name(row.display_name),
                    self._normalized_name(f"character:{row.character_id}"),
                }:
                    exact_ids.append(row.character_id)
                elif normalized in self._character_aliases(row):
                    alias_ids.append(row.character_id)
        else:
            rows = list(
                self.session.execute(
                    select(LibraryEntity).where(LibraryEntity.project_id == project_id)
                ).scalars().all()
            )
            for row in rows:
                if entity_type in {"location", "item"} and row.kind != entity_type:
                    continue
                if normalized in {
                    self._normalized_name(row.entity_id),
                    self._normalized_name(row.name),
                    self._normalized_name(f"entity:{row.entity_id}"),
                }:
                    exact_ids.append(row.entity_id)
                elif normalized in {
                    self._normalized_name(value) for value in (row.aliases_json or [])
                }:
                    alias_ids.append(row.entity_id)

        candidate_ids = list(dict.fromkeys(exact_ids or alias_ids))
        if len(candidate_ids) == 1:
            return {
                "status": "exact" if exact_ids else "alias",
                "resolved_entity_id": candidate_ids[0],
                "candidate_ids": candidate_ids,
            }
        if len(candidate_ids) > 1:
            return {
                "status": "ambiguous",
                "resolved_entity_id": None,
                "candidate_ids": candidate_ids,
            }
        return {"status": "unresolved", "resolved_entity_id": None, "candidate_ids": []}

    def _resolved_entity_for_accept(
        self,
        candidate: FactCandidate,
        *,
        selected_entity_id: str | None,
    ) -> str:
        if selected_entity_id:
            if not self._entity_belongs_to_project(
                candidate.project_id,
                candidate.entity_type,
                selected_entity_id,
            ):
                raise DomainError(
                    "CANON_ENTITY_SELECTION_INVALID",
                    "选的人物 / 实体不属于这部作品",
                    status_code=409,
                )
            if (
                candidate.entity_candidates_json
                and selected_entity_id not in candidate.entity_candidates_json
            ):
                raise DomainError(
                    "CANON_ENTITY_SELECTION_INVALID",
                    "选的人物 / 实体不在这条候选的匹配里",
                    status_code=409,
                )
            return selected_entity_id
        if (
            candidate.resolved_entity_id
            and candidate.entity_resolution_status in {"exact", "alias", "manual"}
        ):
            return candidate.resolved_entity_id
        raise DomainError(
            "CANON_ENTITY_RESOLUTION_REQUIRED",
            "这条候选指的是谁还不确定，请先选定人物 / 实体",
            status_code=409,
            details={"entity_candidates": list(candidate.entity_candidates_json or [])},
        )

    def _entity_belongs_to_project(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
    ) -> bool:
        if entity_type in {"character", "relation"}:
            row = self.session.get(StoryCharacter, entity_id)
            return bool(row is not None and row.project_id == project_id)
        row = self.session.get(LibraryEntity, entity_id)
        return bool(row is not None and row.project_id == project_id)

    @staticmethod
    def _character_aliases(row: StoryCharacter) -> set[str]:
        return {normalized_name(value) for value in character_alias_values(row) if normalized_name(value)}

    @staticmethod
    def _normalized_name(value: Any) -> str:
        return normalized_name(value)

    def _entity_labels(self, project_id: str, entity_ids: set[str]) -> dict[str, str]:
        """候选实体的显示名（人物优先，其次资料库实体），只认本作品的。"""
        if not entity_ids:
            return {}
        labels: dict[str, str] = {}
        for row in self.session.execute(
            select(LibraryEntity).where(LibraryEntity.entity_id.in_(entity_ids))
        ).scalars().all():
            if row.project_id == project_id:
                labels[row.entity_id] = row.name
        for row in self.session.execute(
            select(StoryCharacter).where(StoryCharacter.character_id.in_(entity_ids))
        ).scalars().all():
            if row.project_id == project_id:
                labels[row.character_id] = row.display_name
        return labels

    def _entity_options(
        self,
        row: FactCandidate,
        entity_labels: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        entity_ids = list(row.entity_candidates_json or [])
        if entity_labels is None:
            entity_labels = self._entity_labels(row.project_id, set(entity_ids))
        return [
            {"entity_id": entity_id, "label": entity_labels[entity_id]}
            for entity_id in entity_ids
            if entity_id in entity_labels
        ]

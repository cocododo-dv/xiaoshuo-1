from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.scene_lookup import require_scene


class SceneNotesService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, scene_id: str) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True)
        return self._payload(scene)

    def save(
        self,
        scene_id: str,
        notes: str,
        *,
        base_revision_no: int,
    ) -> dict[str, Any]:
        require_scene(self.session, scene_id, trashed_as_conflict=True)
        next_revision_no = int(base_revision_no) + 1
        changed = self.session.execute(
            update(SceneCard)
            .where(
                SceneCard.scene_id == scene_id,
                SceneCard.trashed_flag == 0,
                SceneCard.author_notes_revision_no == int(base_revision_no),
            )
            .values(
                author_notes=notes,
                author_notes_revision_no=next_revision_no,
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            current_revision = self.session.scalar(
                select(SceneCard.author_notes_revision_no).where(SceneCard.scene_id == scene_id)
            )
            raise DomainError(
                "SCENE_AUTHOR_NOTES_CONFLICT",
                "scene author notes changed; refresh before saving",
                status_code=409,
                details={"current_revision_no": current_revision},
            )
        self.session.expire_all()
        return self._payload(require_scene(self.session, scene_id, trashed_as_conflict=True))

    @staticmethod
    def _payload(scene: SceneCard) -> dict[str, Any]:
        return {
            "scene_id": scene.scene_id,
            "notes": scene.author_notes or "",
            "revision_no": int(scene.author_notes_revision_no or 0),
            "updated_at": scene.updated_at,
        }

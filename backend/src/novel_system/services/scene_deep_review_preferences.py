from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.scene_lookup import require_scene


class SceneDeepReviewPreferencesService:
    """Durable writer decisions used by the local deep-review heuristics."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, scene_id: str) -> dict[str, Any]:
        return self._payload(self._require_scene(scene_id))

    def save(
        self,
        scene_id: str,
        *,
        decision_log: list[dict[str, Any]],
        ignored_issue_keys: list[str],
        base_revision_no: int,
    ) -> dict[str, Any]:
        self._require_scene(scene_id)
        next_revision_no = int(base_revision_no) + 1
        changed = self.session.execute(
            update(SceneCard)
            .where(
                SceneCard.scene_id == scene_id,
                SceneCard.trashed_flag == 0,
                SceneCard.deep_review_preferences_revision_no == int(base_revision_no),
            )
            .values(
                deep_review_decision_log_json=decision_log,
                deep_review_ignored_keys_json=list(dict.fromkeys(ignored_issue_keys)),
                deep_review_preferences_revision_no=next_revision_no,
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            current_revision = self.session.scalar(
                select(SceneCard.deep_review_preferences_revision_no).where(SceneCard.scene_id == scene_id)
            )
            raise DomainError(
                "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT",
                "scene deep-review preferences changed; refresh before saving",
                status_code=409,
                details={"current_revision_no": current_revision},
            )
        self.session.expire_all()
        scene = self._require_scene(scene_id)
        payload = self._payload(scene)
        # 2026-09-22 场景诊断第三轮：忽略 / 恢复之后这一场 / 这一章开着的发现数随响应回传（角标不是闸门）
        try:
            from novel_system.services.scene_diagnosis import SceneDiagnosisService

            payload["diagnosis_rollup"] = SceneDiagnosisService(self.session).scene_rollup(scene)
        except Exception:  # noqa: BLE001
            pass
        return payload

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id, trashed_as_conflict=True)

    @staticmethod
    def _payload(scene: SceneCard) -> dict[str, Any]:
        return {
            "scene_id": scene.scene_id,
            "decision_log": list(scene.deep_review_decision_log_json or []),
            "ignored_issue_keys": list(scene.deep_review_ignored_keys_json or []),
            "revision_no": int(scene.deep_review_preferences_revision_no or 0),
            "updated_at": scene.updated_at,
        }

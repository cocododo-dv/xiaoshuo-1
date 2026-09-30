from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_lookup import require_scene

_LOGGER = logging.getLogger(__name__)


class SceneDeepReviewPreferencesService:
    """写作台深改面板里作者的决定：忽略了哪些发现（按 ``signal_id``，场景诊断、文学质量视图与成稿门都认）与
    决定记录；带修订号，并发保存按修订号比较、冲突 409。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, scene_id: str) -> dict[str, Any]:
        return self._payload(require_scene(self.session, scene_id, trashed_as_conflict=True))

    def save(
        self,
        scene_id: str,
        *,
        decision_log: list[dict[str, Any]],
        ignored_issue_keys: list[str],
        base_revision_no: int,
    ) -> dict[str, Any]:
        require_scene(self.session, scene_id, trashed_as_conflict=True)
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
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True)
        payload = self._payload(scene)
        # 2026-09-22 场景诊断第三轮：忽略 / 恢复之后这一场 / 这一章开着的发现数随响应回传（角标不是闸门：
        # 算不出来只少这一个键，忽略照样保存）
        try:
            payload["diagnosis_rollup"] = SceneDiagnosisService(self.session).scene_rollup(scene)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("diagnosis rollup after saving deep-review preferences failed for %s", scene_id, exc_info=True)
        return payload

    @staticmethod
    def _payload(scene: SceneCard) -> dict[str, Any]:
        return {
            "scene_id": scene.scene_id,
            "decision_log": list(scene.deep_review_decision_log_json or []),
            "ignored_issue_keys": list(scene.deep_review_ignored_keys_json or []),
            "revision_no": int(scene.deep_review_preferences_revision_no or 0),
            "updated_at": scene.updated_at,
        }

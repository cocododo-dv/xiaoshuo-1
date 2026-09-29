"""正史核对服务的门面：``CanonContinuityService`` 这个名字与它的全部方法照旧从这里用。

实现拆在 ``services/canon/`` 的几个混入类里（B11-10），按职责各管一段：归档与暂存抽取、作者的决定、版本更替、
读模型与快照、实体解析、起草提示。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.services.canon.common import CanonBase
from novel_system.services.canon.decisions import CanonDecisionsMixin
from novel_system.services.canon.entities import CanonEntitiesMixin
from novel_system.services.canon.lifecycle import CanonLifecycleMixin
from novel_system.services.canon.prompt import CanonPromptMixin
from novel_system.services.canon.revisions import CanonRevisionsMixin
from novel_system.services.canon.snapshots import CanonSnapshotsMixin

__all__ = ["CanonContinuityService"]


class CanonContinuityService(
    CanonLifecycleMixin,
    CanonDecisionsMixin,
    CanonPromptMixin,
    CanonSnapshotsMixin,
    CanonRevisionsMixin,
    CanonEntitiesMixin,
    CanonBase,
):
    """Turn prose-grounded candidates into accepted, replayable story canon.

    Extraction is deliberately separated from authority: staged NarrativeEvent rows
    remain ``pending`` and runtime replay filters them out. Candidate acceptance binds
    the fact and records an audit commit, but the event enters runtime canon only when
    the whole scene receives a completion commit.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

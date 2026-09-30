"""叙事事件账本的门面：旧的导入路径与 ``NarrativeEventLog`` 这个名字照旧可用。

实现住在 ``services/narrative/``：``taxonomy``（事件 / 实体分类表）、``replay``（存取、按位置重放、单趟快照）、
``hard_facts``（硬事实矛盾检查）、``digests``（全知摘要）、``pov``（POV 减法投影）。``NarrativeEventLog`` 在重放
之上加三个入口：提示词的状态摘要、信息差摘要和连续性检查，每个入口只取一份快照。质检与成稿门经这个类的
``check_consistency`` 调用（测试会在类上替换它）。
"""
from __future__ import annotations

from novel_system.services.narrative import digests, hard_facts, pov
from novel_system.services.narrative.hard_facts import ConsistencyReport, ConsistencyViolation
from novel_system.services.narrative.replay import (
    CharacterState,
    EntityState,
    NarrativeEventStore,
    ProjectedFact,
    fold_events,
    fold_fact,
    require_scene_boundary,
    snapshot_before,
)
from novel_system.services.narrative.taxonomy import ENTITY_TYPES, EVENT_TYPES

__all__ = [
    "ENTITY_TYPES",
    "EVENT_TYPES",
    "CharacterState",
    "ConsistencyReport",
    "ConsistencyViolation",
    "EntityState",
    "NarrativeEventLog",
    "ProjectedFact",
    "fold_events",
    "fold_fact",
]


class NarrativeEventLog(NarrativeEventStore):
    def check_consistency(
        self,
        generated_text: str,
        project_id: str,
        scene_id: str,
        *,
        character_ids: list[str] | None = None,
    ) -> ConsistencyReport:
        """正文与这一场之前的权威状态做硬事实矛盾检查（蓝图 §17 Action B）。"""
        return hard_facts.check_consistency(
            self,
            generated_text,
            project_id,
            scene_id,
            character_ids=character_ids,
        )

    def format_state_for_prompt(
        self,
        project_id: str,
        *,
        scene_id: str,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> str:
        """写作提示词里的权威状态摘要（截到这一场之前）。

        Wave 4（§5.6）：指定 ``pov_character_id`` 时做 POV 减法投影，隐藏非 POV 秘密内容；``pov=None``
        保持全知视角全量注入。**硬 QC 不走此方法**——它读 ``check_consistency`` 的全量权威状态。
        """
        snapshot = snapshot_before(self, project_id, require_scene_boundary(scene_id))
        if pov_character_id:
            return pov.format_pov_state(snapshot, pov_character_id, onstage_character_ids)
        return digests.format_state(snapshot, onstage_character_ids=onstage_character_ids)

    def information_asymmetry_digest(
        self,
        project_id: str,
        *,
        scene_id: str,
        onstage_character_ids: list[str] | None = None,
        pov_character_id: str | None = None,
    ) -> str:
        """蓝图 §2 / §11：在场角色之间的信息差摘要（截到这一场之前）。

        Wave 4（§5.6）：指定 ``pov_character_id`` 时只展示 POV 独有认知，他人独有内容 / 秘密只给
        内容无关的盲区提示，绝不打印 "Secrets held by X" 正文；``pov=None`` 保持全量。
        """
        scene_id = require_scene_boundary(scene_id)
        onstage = list(onstage_character_ids or [])
        if len(onstage) < 2:
            return ""
        snapshot = snapshot_before(self, project_id, scene_id)
        if pov_character_id:
            return pov.format_pov_asymmetry(snapshot, pov_character_id, onstage)
        return digests.format_asymmetry(snapshot, onstage)

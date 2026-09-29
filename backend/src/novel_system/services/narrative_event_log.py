"""叙事事件账本的门面：旧的导入路径与 ``NarrativeEventLog`` 这个名字照旧可用。

实现住在 ``services/narrative/``：``taxonomy``（事件 / 实体分类表）、``replay``（存取与按位置重放）、
``hard_facts``（硬事实矛盾检查）。``NarrativeEventLog`` 在重放之上加三个入口：提示词的状态摘要、信息差摘要
和连续性检查。质检与成稿门经这个类的 ``check_consistency`` 调用（测试会在类上替换它）。
"""
from __future__ import annotations

from novel_system.services.narrative import hard_facts
from novel_system.services.narrative.hard_facts import ConsistencyReport, ConsistencyViolation
from novel_system.services.narrative.replay import (
    CharacterState,
    EntityState,
    NarrativeEventStore,
    ProjectedFact,
    fold_events,
    fold_fact,
    require_scene_boundary,
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
        scene_seq: None = None,
        *,
        scene_id: str,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> str:
        """Format projected entity states as a prompt section for injection.

        Wave 4（§5.6）：这是**写作提示词**槽位。当指定 ``pov_character_id`` 时，委派
        `PovKnowledgeProjection` 做 POV 减法投影，隐藏非 POV 秘密内容；``pov=None``
        保持全知视角全量注入（逐字节不变）。**硬 QC 不走此方法**——它读
        `project_character_state` / `check_consistency` 的全量权威状态，不受投影影响。
        """
        if pov_character_id:
            from novel_system.services.pov_knowledge_projection import (
                PovKnowledgeProjection,
            )
            return PovKnowledgeProjection(self.session, event_log=self).format_state_for_prompt(
                project_id,
                scene_id=require_scene_boundary(scene_seq, scene_id),
                pov_character_id=pov_character_id,
                onstage_character_ids=onstage_character_ids,
            )
        boundary = {"before_scene_id": require_scene_boundary(scene_seq, scene_id)}
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
        scene_seq: None = None,
        onstage_character_ids: list[str] | None = None,
        *,
        scene_id: str,
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
                project_id,
                onstage_character_ids=onstage_character_ids,
                scene_id=require_scene_boundary(scene_seq, scene_id),
                pov_character_id=pov_character_id,
            )
        boundary = {"before_scene_id": require_scene_boundary(scene_seq, scene_id)}
        onstage_character_ids = list(onstage_character_ids or [])
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
                    # 集合按排序取前 5 条：以前按集合的迭代次序取，字符串哈希每个进程随机，
                    # 同一份库在不同进程里拼出的提示词（和 bundle 哈希）不一样。
                    if a_exclusive:
                        lines.append(f"  {char_a} knows but {char_b} doesn't:")
                        for fact in sorted(a_exclusive)[:5]:
                            lines.append(f"    - {fact}")
                    if b_exclusive:
                        lines.append(f"  {char_b} knows but {char_a} doesn't:")
                        for fact in sorted(b_exclusive)[:5]:
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

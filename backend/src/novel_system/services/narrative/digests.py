"""写作提示词里的叙事摘要（全知视角）：权威状态、信息差。

都从同一份 ``ProjectionSnapshot`` 拼出来，不再自己查库。POV 视角的减法投影在 ``narrative/pov.py``，
地点 / 物品两段两边共用。硬 QC 不读这些摘要——它走 ``hard_facts`` 的全量权威状态。
"""
from __future__ import annotations

from novel_system.services.character_names import display_name_of, labelled_name_of
from novel_system.services.narrative.replay import ProjectionSnapshot

CHARACTER_STATE_HEADER = "## Authoritative Character State (from event log, do NOT contradict)"
_ENTITY_STATE_HEADERS = (
    ("location", "## Authoritative Location State (from event log, do NOT contradict)"),
    ("item", "## Authoritative Item State (from event log, do NOT contradict)"),
)


def entity_state_lines(snapshot: ProjectionSnapshot) -> list[str]:
    """地点 / 物品的公共状态段（全知与 POV 摘要共用，逐字节相同）。"""
    out: list[str] = []
    for entity_type, header in _ENTITY_STATE_HEADERS:
        block: list[str] = []
        for entity_id in snapshot.entities_of_type(entity_type):
            state = snapshot.entity_state(entity_type, entity_id)
            if state.facts:
                block.append(f"\n### {labelled_name_of(snapshot.names(), entity_id)}")
                for key, value in sorted(state.as_dict().items()):
                    block.append(f"- {key}: {value}")
        if block:
            out.append("\n" + header)
            out.extend(block)
    return out


def format_state(
    snapshot: ProjectionSnapshot,
    *,
    onstage_character_ids: list[str] | None = None,
) -> str:
    """全知视角的权威状态摘要：在场角色（没给名单就是全作品的角色）+ 地点 / 物品。"""
    chars = onstage_character_ids or snapshot.characters()
    lines: list[str] = [CHARACTER_STATE_HEADER]
    for char_id in chars:
        state = snapshot.character_state(char_id)
        if not state.facts:
            continue
        lines.append(f"\n### {labelled_name_of(snapshot.names(), char_id)}")
        for key, value in sorted(state.as_dict().items()):
            lines.append(f"- {key}: {value}")
    lines.extend(entity_state_lines(snapshot))
    return "\n".join(lines) if len(lines) > 1 else ""


def format_asymmetry(snapshot: ProjectionSnapshot, onstage_character_ids: list[str]) -> str:
    """全知视角的信息差摘要（蓝图 §2 / §11）：两两之间谁知道对方不知道的事，外加秘密与错误信念。"""
    if len(onstage_character_ids) < 2:
        return ""

    names = snapshot.names()
    lines: list[str] = ["## Information Asymmetry (who knows what the other doesn't)"]
    knowledge: dict[str, set[str]] = {}
    secrets: dict[str, list[str]] = {}
    false_beliefs: dict[str, list[str]] = {}

    for char_id in onstage_character_ids:
        knowledge[char_id] = {f"{f.fact_key}:{f.fact_value}" for f in snapshot.known_facts(char_id)}
        for fact_key, projected in snapshot.character_state(char_id).facts.items():
            if fact_key == "secret_held_by":
                secrets.setdefault(char_id, []).append(projected.fact_value)
            elif fact_key == "believes_false":
                false_beliefs.setdefault(char_id, []).append(projected.fact_value)

    for i, char_a in enumerate(onstage_character_ids):
        for char_b in onstage_character_ids[i + 1:]:
            a_knows = knowledge.get(char_a, set())
            b_knows = knowledge.get(char_b, set())
            a_exclusive = a_knows - b_knows
            b_exclusive = b_knows - a_knows
            if a_exclusive or b_exclusive:
                name_a, name_b = display_name_of(names, char_a), display_name_of(names, char_b)
                lines.append(f"\n### {name_a} ↔ {name_b}")
                # 集合按排序取前 5 条：以前按集合的迭代次序取，字符串哈希每个进程随机，
                # 同一份库在不同进程里拼出的提示词（和 bundle 哈希）不一样。
                if a_exclusive:
                    lines.append(f"  {name_a} knows but {name_b} doesn't:")
                    for fact in sorted(a_exclusive)[:5]:
                        lines.append(f"    - {fact}")
                if b_exclusive:
                    lines.append(f"  {name_b} knows but {name_a} doesn't:")
                    for fact in sorted(b_exclusive)[:5]:
                        lines.append(f"    - {fact}")

    for char_id in onstage_character_ids:
        if char_id in secrets:
            lines.append(f"\n### Secrets held by {display_name_of(names, char_id)}")
            for secret in secrets[char_id][:3]:
                lines.append(f"  - {secret}")
        if char_id in false_beliefs:
            lines.append(f"\n### False beliefs of {display_name_of(names, char_id)}")
            for belief in false_beliefs[char_id][:3]:
                lines.append(f"  - {belief}")

    return "\n".join(lines) if len(lines) > 1 else ""

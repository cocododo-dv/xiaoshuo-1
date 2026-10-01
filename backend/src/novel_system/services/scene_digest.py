from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.qc_constraints import strip_reference_policy
from novel_system.services.story_slots import planned_beats, planned_chapter_goal


def scene_card_digest(scene: SceneCard, chapter: ChapterGoal | None = None) -> str:
    # 场目标 / 节拍只印作者规划过的：物化曾给没写摘要的场补上本章的样板目标「推进本章：<章名>」（也当唯一一拍），
    # 它不是这一场的目标（story_slots.planned_chapter_goal，按 ``chapter`` 的章名认）
    goal = planned_chapter_goal(scene.scene_goal, chapter)
    lines = [f"Goal: {goal}"] if goal else []
    if scene.location:
        lines.append(f"Location: {scene.location}")
    beats = planned_beats(scene.beats_json, chapter)
    if beats:
        lines.append(f"Beats: {'; '.join(str(beat) for beat in beats)}")
    if scene.must_include_text:
        lines.append(f"Required beats to weave naturally: {scene.must_include_text}")
    # 只有作者真写的禁用词才算「Forbidden text」；旧卡上的防抄袭政策句不是禁用词表（见 qc_constraints）
    forbidden_text = strip_reference_policy(scene.forbidden_text)
    if forbidden_text:
        lines.append(f"Forbidden text: {forbidden_text}")
    if scene.exit_change:
        lines.append(f"Exit change: {scene.exit_change}")
    if scene.hook:
        lines.append(f"Hook: {scene.hook}")
    if scene.target_length_band:
        lines.append(f"Target length: {scene.target_length_band}")
    if scene.scene_type:
        lines.append(f"Scene type: {scene.scene_type}")
    return "\n".join(lines)

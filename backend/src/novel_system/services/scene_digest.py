from __future__ import annotations

from novel_system.db.models import SceneCard
from novel_system.services.qc_constraints import strip_reference_policy


def scene_card_digest(scene: SceneCard) -> str:
    lines = [f"Goal: {scene.scene_goal}"] if scene.scene_goal else []
    if scene.location:
        lines.append(f"Location: {scene.location}")
    if scene.beats_json:
        lines.append(f"Beats: {'; '.join(str(beat) for beat in scene.beats_json)}")
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

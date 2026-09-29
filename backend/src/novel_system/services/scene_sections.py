"""场景的两段「作者设计」挂进来源快照：结构简报（事实，永不压缩）与设计上下文（背景，预算紧时先压后省）。

同一段「渲染 → ``source_version_refs`` 记来源 → ``ordered_injections`` 排位 → ``inline_digests`` 放正文」以前在
bundle 构建、蓝图来源快照、近终稿快照里各写一遍；这里是唯一的一份。两段的键与标签见
:mod:`.scene_structure_brief` / :mod:`.scene_design_context`。挂的次序（结构在前、设计在后）进
``ordered_injections``，来源快照的哈希依赖它——改次序就是改哈希。
"""

from __future__ import annotations

from collections.abc import MutableMapping, MutableSequence
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard
from novel_system.services.scene_design_context import SCENE_DESIGN_SECTION_KEY, build_scene_design_context
from novel_system.services.scene_structure_brief import SCENE_STRUCTURE_SECTION_KEY, render_scene_structure_brief


def attach_scene_sections(
    scene: SceneCard,
    session: Session | None,
    *,
    refs: MutableMapping[str, Any],
    injections: MutableSequence[dict[str, Any]],
    digests: MutableMapping[str, Any],
    structure: bool = True,
    design: bool = True,
) -> None:
    """按需渲染两段并挂上；没有结构 / 没有已确认的设计（或各自的开关关着）的那段不挂，什么都不写。"""
    if structure:
        brief = render_scene_structure_brief(scene, session)
        if brief:
            refs[SCENE_STRUCTURE_SECTION_KEY] = scene.scene_id
            injections.append(
                {"slot": SCENE_STRUCTURE_SECTION_KEY, "ref_id": scene.scene_id, "digest_key": SCENE_STRUCTURE_SECTION_KEY}
            )
            digests[SCENE_STRUCTURE_SECTION_KEY] = brief
    if design:
        context = build_scene_design_context(scene, session)
        if context is not None:
            # 引用的步骤版本进来源：设计一改，快照哈希就变
            refs[SCENE_DESIGN_SECTION_KEY] = list(context.step_run_ids)
            injections.append(
                {"slot": SCENE_DESIGN_SECTION_KEY, "ref_id": scene.scene_id, "digest_key": SCENE_DESIGN_SECTION_KEY}
            )
            digests[SCENE_DESIGN_SECTION_KEY] = context.text

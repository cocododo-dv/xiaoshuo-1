"""雪花场景计划行（``snowflake_scene_plans``）的形状：交给前端 / 提示词的两种行载荷、内容签名、可改字段、身份铸造。

工作台、结构化同步、分诊、回流共用；2026-09-30 从 ``snowflake_workspace.py`` 收拢到这里（B06-07）。叶子模块。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from novel_system.db.models import SnowflakeScenePlan
from novel_system.services.snowflake_staleness import stable_json


SCENE_PATCH_FIELDS = {
    # P1-1: scene_id / chapter_id are system-minted identity, never author-editable.
    # chapter_title / chapter_goal / chapter_role stay editable (content, not identity).
    "chapter_title",
    "chapter_goal",
    "chapter_role",
    # 灾一/灾二/灾三：作者标在场上的结构铰链，脊柱锚点分章要用（P2）。
    # 它是内容标注，不是身份，所以可编辑。
    "spine",
    "scene_seq",
    "pov_character_id",
    "onstage_chars_json",
    "title",
    "summary",
    "primary_form",
    "scene_type",
    "location",
    "scene_crucible",
    "crucible",
    "goal",
    "conflict",
    "setback",
    "reaction",
    "dilemma",
    "decision",
    "cost_requirement",
    "beats_json",
    "must_include_text",
    "exit_change",
    "hook",
    "target_length_band",
    # 阶段 C / N：呈现方式——summary 对两种形态都合法，skip 只给反应场（见 snowflake_steps.effective_rendering_mode）。
    "rendering_mode",
    # 阶段 J：原著的几栏——读者应感到什么、故事时间（在场人物已在上面）。
    "expected_reader_emotion",
    "story_time",
    # 阶段 N：作者的破例理由（原著：不过关也可以放行，但要知道理由）。
    "exception_reason",
}
#: 已有场景计划行上只归 09 场景列表（章的包装归分章面板）改的字段：交给前端的 09 行（:func:`scene_list_payload`）上的
#: 内容，坩埚的两个写法都算。第 10 步的草稿不改它们——它只管自己那几栏（三拍、代价、在场、篇幅、呈现……）。以前只护着
#: 形态与视角：恢复一版旧的 10、模型交回来的 10 都会把旧的事件 / 地点 / 坩埚 / 脊柱标记盖回现在的场上（复核 Q2b 新发现）
SCENE_LIST_OWNED_FIELDS = (
    "primary_form", "scene_type", "pov_character_id", "summary", "location", "crucible", "scene_crucible", "spine",
    "chapter_role", "chapter_title", "chapter_goal", "scene_seq",
)

#: 产出场景计划的两步（09 场景列表 / 10 场景规划）：它们的「已复核」连同过期的场景计划一起复核
SCENE_PLAN_STEPS = frozenset({"scene_list", "scene_details"})
#: 确认这两步时（工作台带 ``sync_catalog``）已物化的场景卡自动跟上构思
CATALOG_SYNC_STEPS = SCENE_PLAN_STEPS

SCENE_PLAN_STATE_KEYS: frozenset[str] = frozenset(
    {"scene_plan_id", "status", "stale_reason", "stale_accepted_at", "stale_accepted_by", "stale_accepted_note", "diagnosis"}
)


# 位置不是内容：章内序号按 09 的行序重算（``renumber_scene_seq``），在前面插进 / 删掉一场，后面几场都挪一格
SCENE_PLAN_POSITION_KEYS: frozenset[str] = frozenset({"scene_seq"})


def scene_plan_content_signature(scene: SnowflakeScenePlan) -> str:
    """场景计划行的**内容**签名（去掉状态 / 失效留痕 / 诊断 / 身份与章内位置）；同步时据此判断这一行有没有真的改。

    以前签名里带着章内序号：草稿里在一章前面插进 / 删掉一场，同章后面每一场都被当成「改了」，打回 draft、
    复核留痕清零（S1 18）。"""
    payload = {
        key: value
        for key, value in scene_plan_payload(scene).items()
        if key not in SCENE_PLAN_STATE_KEYS and key not in SCENE_PLAN_POSITION_KEYS
    }
    return stable_json(payload)


def scene_plan_payload(scene: SnowflakeScenePlan) -> dict[str, Any]:
    return {
        "scene_plan_id": scene.scene_plan_id,
        "row_uid": scene.row_uid or "",
        "scene_id": scene.scene_id,
        "chapter_plan_id": scene.chapter_plan_id or "",
        "chapter_id": scene.chapter_id,
        "chapter_title": scene.chapter_title or "",
        "chapter_goal": scene.chapter_goal or "",
        "chapter_role": scene.chapter_role or "",
        "spine": scene.spine or "",
        "scene_seq": scene.scene_seq,
        "pov_character_id": scene.pov_character_id or "",
        "onstage_chars_json": list(scene.onstage_chars_json or []),
        "title": scene.title or "",
        "summary": scene.summary or "",
        "primary_form": scene.scene_type or "proactive",
        "scene_type": scene.scene_type or "proactive",
        "location": scene.location or "",
        "scene_crucible": scene.scene_crucible or "",
        "crucible": scene.scene_crucible or "",
        "goal": scene.goal or "",
        "conflict": scene.conflict or "",
        "setback": scene.setback or "",
        "reaction": scene.reaction or "",
        "dilemma": scene.dilemma or "",
        "decision": scene.decision or "",
        "cost_requirement": scene.cost_requirement or "",
        "beats_json": list(scene.beats_json or []),
        "must_include_text": scene.must_include_text or "",
        "exit_change": scene.exit_change or "",
        "hook": scene.hook or "",
        "target_length_band": scene.target_length_band or "",
        "rendering_mode": scene.rendering_mode or "full",
        "expected_reader_emotion": scene.expected_reader_emotion or "",
        "story_time": scene.story_time or "",
        "exception_reason": scene.exception_reason or "",
        "status": scene.status,
        "stale_reason": scene.stale_reason or "",
        "stale_accepted_at": scene.stale_accepted_at,
        "stale_accepted_by": scene.stale_accepted_by or "",
        "stale_accepted_note": scene.stale_accepted_note or "",
        "diagnosis": deepcopy(scene.diagnosis_json or {}),
    }


def scene_list_payload(scene: SnowflakeScenePlan) -> dict[str, Any]:
    return {
        "scene_plan_id": scene.scene_plan_id,
        "row_uid": scene.row_uid or "",
        "scene_id": scene.scene_id,
        "chapter_plan_id": scene.chapter_plan_id or "",
        "chapter_id": scene.chapter_id,
        "chapter_title": scene.chapter_title or scene.chapter_id,
        "chapter_goal": scene.chapter_goal or "",
        "spine": scene.spine or "",
        "scene_seq": scene.scene_seq,
        "pov_character_id": scene.pov_character_id or "",
        "summary": scene.summary or scene.title or "",
        "primary_form": scene.scene_type or "proactive",
        "scene_type": scene.scene_type or "proactive",
        "chapter_role": scene.chapter_role or "",
        "location": scene.location or "",
        "crucible": scene.scene_crucible or "",
    }


def sanitize_scene_patch(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    patch: dict[str, Any] = {}
    for key in SCENE_PATCH_FIELDS:
        if key in payload:
            patch[key] = deepcopy(payload[key])
    if "primary_form" in patch:
        patch["scene_type"] = patch["primary_form"]
    return patch


def mint_row_uid() -> str:
    """Mint an immutable, system-owned scene-row identity (P1-1).

    Scene identity no longer derives from the author-editable ``scene_id``; this
    uuid is minted once when a row is first seen and then never changes, so a
    reorder or an ID re-mint can never orphan a plan or break the diff chain.
    """
    return f"row_{uuid.uuid4().hex}"


def mint_scene_id(project_id: str, row_uid: str) -> str:
    """Mint the scene's materialization identity from its immutable row anchor (P1-2).

    The old rule was ``f"{chapter_id}_SC{scene_seq:02d}"``, frozen at creation while
    ``scene_seq`` was recomputed on every save — so deleting a scene and adding another
    reliably produced two rows with the same ``scene_id``, and materialization then lost
    one of them without a word. Deriving it from ``row_uid`` instead makes it unique by
    construction and independent of both chapter membership and ordering.
    """
    return f"{project_id}_SC_{row_uid}"

"""雪花各步草稿的形状：默认稿、与存档合并、按步归一化（五段按位置、角色全档案嵌套、场景形态）、第 10 步的场景种子。

2026-09-30 从 ``snowflake_steps.py`` 拆出（B06-09），``snowflake_steps`` 仍然转出这里的每一个名字。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from novel_system.services.value_coercion import coerce_string_list
from novel_system.services.snowflake_step_catalog import LONG_SYNOPSIS_PARAGRAPHS, step_definition_view


def derive_three_act(draft: dict[str, Any] | None) -> dict[str, str]:
    """三幕灾难是五句脊的视图，不是独立事实（P1-2）。

    句 2/3/4 本就是三次灾难、句 5 是结局方向。统一从 ``sentences`` 单向派生，
    任何持久化 / 导出想暴露三幕都「读时派生」，不再入库——杜绝双写漂移。
    """
    payload = draft if isinstance(draft, dict) else {}
    sentences = [str(item or "") for item in (payload.get("sentences") or [])] + [""] * 5
    return {
        "first_disaster": sentences[1],
        "second_disaster": sentences[2],
        "third_disaster": sentences[3],
        "ending": sentences[4],
    }


def merge_step_draft(
    step_key: str,
    stored_draft: dict[str, Any] | None,
    *,
    latest_by_step: dict[str, Any] | None = None,
) -> dict[str, Any]:
    draft = default_step_draft(step_key, latest_by_step=latest_by_step)
    payload = stored_draft or {}
    if not isinstance(payload, dict):
        return draft
    return _normalize_step_draft(step_key, _merge_dicts(draft, payload))


def default_step_draft(step_key: str, *, latest_by_step: dict[str, Any] | None = None) -> dict[str, Any]:
    step = step_definition_view(step_key)
    draft = deepcopy(step.get("default_draft") or {})
    if step_key == "scene_details":
        scene_list_artifact = (latest_by_step or {}).get("scene_list")
        scenes = []
        if scene_list_artifact is not None:
            scenes = list((scene_list_artifact.draft_json or {}).get("scenes") or [])
        if scenes:
            draft["scenes"] = [_scene_detail_seed(scene, index) for index, scene in enumerate(scenes, start=1)]
    return _normalize_step_draft(step_key, draft)


def _normalize_step_draft(step_key: str, draft: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(draft if isinstance(draft, dict) else {})
    if step_key in {"short_synopsis", "long_synopsis"}:
        # 阶段 D / H：五段是**按位置**的槽（第 n 段扩第 n 句）——补齐到五槽，保留中间的空槽，
        # 永不截断（多出来的段由生成侧的数量契约拒绝；作者手写的第六段绝不静默丢失，
        # 空着的第二段也不能让第三段顶上去）。
        paragraphs = _coerce_positional_list(payload.get("paragraphs"))
        while len(paragraphs) < LONG_SYNOPSIS_PARAGRAPHS:
            paragraphs.append("")
        payload["paragraphs"] = paragraphs
    elif step_key == "character_bibles":
        payload["characters"] = [_normalize_character_bible(item) for item in payload.get("characters") or [] if isinstance(item, dict)]
    elif step_key in {"scene_list", "scene_details"}:
        payload["scenes"] = [_normalize_scene_item(item, index=index) for index, item in enumerate(payload.get("scenes") or [], start=1) if isinstance(item, dict)]
    return payload


def _normalize_character_bible(item: dict[str, Any]) -> dict[str, Any]:
    template_field = next(
        field
        for field in step_definition_view("character_bibles")["editor"]["fields"]
        if field.get("key") == "characters"
    )
    template = deepcopy(template_field.get("template") or {})
    normalized = _merge_dicts(template, item)

    physical = normalized.setdefault("physical_profile", {})
    personality = normalized.setdefault("personality_profile", {})
    environment = normalized.setdefault("environment_profile", {})
    psychological = normalized.setdefault("psychological_profile", {})
    if isinstance(physical, dict):
        physical["age"] = str(item.get("age") or physical.get("age") or "").strip()
    if isinstance(personality, dict):
        personality["strongest_trait"] = str(item.get("strongest_trait") or personality.get("strongest_trait") or "").strip()
        personality["weakest_trait"] = str(item.get("weakest_trait") or personality.get("weakest_trait") or "").strip()
    if isinstance(environment, dict):
        environment["home"] = str(item.get("home") or environment.get("home") or "").strip()
    if isinstance(psychological, dict):
        psychological["deepest_fear"] = str(item.get("deepest_fear") or psychological.get("deepest_fear") or "").strip()
        psychological["character_arc"] = str(item.get("how_character_changes") or psychological.get("character_arc") or "").strip()
    return normalized


def _normalize_scene_item(item: dict[str, Any], *, index: int) -> dict[str, Any]:
    del index  # 2026-09-13 阶段 B：默认形态不再按行号奇偶交替——Ingermanson 说的是「反应场是少数」，不是一主一反
    normalized = deepcopy(item)
    primary_form = str(
        normalized.get("primary_form")
        or normalized.get("scene_type")
        or "proactive"
    ).strip().lower()
    if primary_form not in {"proactive", "reactive"}:
        primary_form = "proactive"
    normalized["primary_form"] = primary_form
    normalized["scene_type"] = primary_form
    normalized.setdefault("crucible", normalized.get("scene_crucible") or "")
    normalized.setdefault("scene_crucible", normalized.get("crucible") or "")
    if "beats_json" in normalized:
        normalized["beats_json"] = coerce_string_list(normalized.get("beats_json"))
    return normalized


def _scene_detail_seed(scene: dict[str, Any], index: int) -> dict[str, Any]:
    # 形态跟随第 9 步的标注；没标就是主动场（阶段 B：不再按奇偶交替播种反应场）。
    scene_type = str(scene.get("primary_form") or scene.get("scene_type") or "proactive").strip().lower() or "proactive"
    if scene_type not in {"proactive", "reactive"}:
        scene_type = "proactive"
    base = {
        "scene_id": scene.get("scene_id") or "",
        "chapter_id": scene.get("chapter_id") or "",
        "title": scene.get("summary") or f"场景 {index:02d}",
        "summary": scene.get("summary") or "",
        "primary_form": scene_type,
        "scene_type": scene_type,
        "location": scene.get("location") or "",
        "crucible": scene.get("crucible") or scene.get("scene_crucible") or "",
        "scene_crucible": "",
        "goal": "",
        "conflict": "",
        "setback": "",
        "reaction": "",
        "dilemma": "",
        "decision": "",
        "cost_requirement": "",
        "rendering_mode": "full",
        "onstage_chars_json": list(scene.get("onstage_chars_json") or []),
        "story_time": scene.get("story_time") or "",
        "expected_reader_emotion": "",
        "exception_reason": "",
        "triage_status": "",
        "triage_notes": "",
        "triage_missing_fields": [],
        "triage_fix_steps": [],
    }
    return base


def _coerce_positional_list(value: Any) -> list[str]:
    """按位置的字符串槽：列表原样保留空槽；纯文本（旧数据）按行拆、丢空行。"""
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value]


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged

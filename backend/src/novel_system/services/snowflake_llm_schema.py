"""把模板的 structured_schema 按步骤编辑器模板补全成员 properties（下发给 provider 的 json_schema）。

2026-09-16 真实故障：集合步的成员对象只写了 ``additionalProperties: true``，按 schema 约束解码的中转只能吐 ``{}``。
纯函数。2026-09-30 从 ``snowflake_workspace_llm.py`` 拆出（B06-08），那边原样转出这里的每一个名字。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from novel_system.services.snowflake_step_catalog import get_step_definition
from novel_system.services.snowflake_llm_sanitize import _SCENE_REPAIR_PATCH_KEYS


def enrich_structured_schema(
    schema: dict[str, Any] | None,
    *,
    step_key: str | None = None,
    template_name: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把模板 structured_schema 里没有 properties 的对象按编辑器模板补全；返回 (schema, 补全了的路径)。

    为什么：两条 OpenAI 线路都把 structured_schema 作为 json_schema 下发（``openai_common.
    openai_text_format``）。集合步的成员对象在 prompts.yaml 里只写了 ``additionalProperties: true``——
    对 OpenAI 的非 strict 模式这等于「随便写」，但对按 schema 约束解码的后端（经中转的 Gemini）它是
    「一个键都不许写」：每个角色 / 场景都被解码成 ``{}``（2026-09-16 真实故障：角色摘要表「采纳并
    结构化」连续三次「过于稀疏」，审计里的输出正是 54 / 57 字节的 ``[{},{}]`` / ``[{},{},{}]``）。
    编辑器模板本来就是这些成员的规范键（提示词也明说「服务端丢弃其它键」），从它派生 properties，
    yaml 与步骤目录不会漂移。``additionalProperties`` 保持 yaml 原样、不加 ``required``：留白规则允许
    成员省略字段，只是不能一个键都没有。

    覆盖：整步生成的集合字段（characters / scenes / chapters）、教练的 ``candidate_patch``（本步
    编辑器全部字段）、场景分诊的 ``items[].repair_patch``（修补器接受的场景键）。
    """
    enriched = deepcopy(schema) if isinstance(schema, dict) else {}
    properties = enriched.get("properties")
    if not isinstance(properties, dict):
        return enriched, []
    applied: list[str] = []
    editor_properties = _editor_schema_properties(step_key) if step_key else {}
    for key, node in properties.items():
        if not isinstance(node, dict):
            continue
        if key == "candidate_patch" and _is_open_object(node) and editor_properties:
            node["properties"] = deepcopy(editor_properties)
            applied.append(key)
            continue
        items = node.get("items") if node.get("type") == "array" else None
        if not isinstance(items, dict):
            continue
        if _is_open_object(items):
            derived = editor_properties.get(key)
            derived_items = derived.get("items") if isinstance(derived, dict) else None
            if isinstance(derived_items, dict) and isinstance(derived_items.get("properties"), dict):
                items["properties"] = deepcopy(derived_items["properties"])
                if step_key == "scene_list" and key == "scenes":
                    # spine 故意不在 09 的编辑器模板里（见 _apply_scene_list_spine），于是从模板派生的
                    # properties 里也没有它——按 schema 约束解码的后端因此**写不出**提示词点名要的
                    # 灾一 / 灾二 / 灾三，三个灾难只能挤进 chapter_role 的自由文本。wire schema 单独补上。
                    items["properties"]["spine"] = {"type": "string"}
                # 标签用审计能原样保留的标识符形（字母 / 下划线），"characters[]" 一类会被指纹化
                applied.append(f"{key}_items")
        item_properties = items.get("properties")
        if template_name == "snowflake_scene_triage_suggest" and isinstance(item_properties, dict):
            patch = item_properties.get("repair_patch")
            if _is_open_object(patch):
                patch["properties"] = {name: {"type": "string"} for name in sorted(_SCENE_REPAIR_PATCH_KEYS)}
                applied.append(f"{key}_items_repair_patch")
    return enriched, applied


def _is_open_object(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "object" and not node.get("properties")


def _editor_schema_properties(step_key: str) -> dict[str, Any]:
    """本步编辑器字段 → JSON-schema properties（文本 → string；列表 / 段 / 句 → string 数组；
    object 字段 → 子键皆 string；带 template 的集合 → 成员对象按模板递归）。"""
    try:
        definition = get_step_definition(step_key)
    except KeyError:
        return {}
    out: dict[str, Any] = {}
    for field in (definition.get("editor") or {}).get("fields") or []:
        key = field.get("key")
        kind = str(field.get("kind") or "")
        if not isinstance(key, str):
            continue
        if kind in {"text", "textarea"}:
            out[key] = {"type": "string"}
        elif kind in {"list", "paragraphs", "sentences"}:
            out[key] = {"type": "array", "items": {"type": "string"}}
        elif kind == "object":
            out[key] = {
                "type": "object",
                "properties": {
                    str(nested.get("key")): {"type": "string"}
                    for nested in field.get("fields") or []
                    if isinstance(nested.get("key"), str)
                },
            }
        elif isinstance(field.get("template"), dict):
            out[key] = {"type": "array", "items": _schema_from_template(field["template"])}
    return out


def _schema_from_template(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"type": "object", "properties": {str(k): _schema_from_template(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"type": "array", "items": {"type": "string"}}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    return {"type": "string"}

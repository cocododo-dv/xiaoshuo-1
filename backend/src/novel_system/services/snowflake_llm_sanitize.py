"""模型产出的清洗：按编辑器模板只留规范键、数量契约（五句 / 五段）、身份键继承、空成员不落库、补丁合并。

纯函数（不碰会话）。2026-09-30 从 ``snowflake_workspace_llm.py`` 拆出（B06-08），那边原样转出这里的每一个名字。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.snowflake_character_ids import canonical_character_id, mint_character_id
from novel_system.services.snowflake_draft_merge import apply_member_patch, patch_id_key
from novel_system.services.snowflake_step_catalog import LONG_SYNOPSIS_PARAGRAPHS, RENDERING_MODES, get_step_definition
from novel_system.services.snowflake_step_diagnosis import is_lead_role, step_completeness
from novel_system.services.value_coercion import coerce_string_list, has_value, int_or_default


class SparseGenerationOutput(ValueError):
    """整步生成清洗后只剩身份键 / 空字段（模型回传了 `{}` 一类的空成员）。

    2026-09-16 真实故障：Responses API 把模板 structured_schema 作为 json_schema 下发，而集合步的
    成员对象只写了 ``additionalProperties: true``、没有 ``properties``——按 schema 约束解码的后端
    （经中转的 Gemini）对这种对象只能吐 ``{}``，每个角色都成了空对象。schema 现已按编辑器模板
    补全 properties（见 ``enrich_structured_schema``）；这个异常是它之后的兜底：带原因重试一次，
    再空就如实报错。"""


class StructuredCountMismatch(ValueError):
    """模型违反了数量契约（一段话概括恰好五句、一页梗概恰好五段）。

    阶段 B（2026-09-13 雪花评估）：旧归一化把多出来的句 / 段静默截掉——一页梗概的第 6–9 段在编辑器
    与长篇大纲的上游上下文里无声消失。现在整步生成拒绝这份输出并带着理由重试一次，教练补丁则
    丢弃该键；绝不静默截断。
    """


# 一页梗概 = 五句各扩一段（Ingermanson 第 4 步）。前端 05 也只有五个槽。
SHORT_SYNOPSIS_PARAGRAPHS = 5


# 场景规划里承载「这一场被深化过」的内容键——身份/序号/章归属不算内容。
_SCENE_CONTENT_KEYS = (
    "title", "summary", "location", "scene_crucible", "crucible",
    "goal", "conflict", "setback", "reaction", "dilemma", "decision",
    "cost_requirement", "must_include_text", "exit_change", "hook", "beats_json",
)


def _assert_scene_details_advanced(
    step_key: str,
    *,
    base: dict[str, Any],
    merged: dict[str, Any],
    targeted_ids: set[str] | None,
) -> None:
    """场景规划的空转防线：模型必须真的动过被点名的场，否则报错而不是「生成成功」。

    真实故障：模型自造场景编号（SC001 → S1）或整段复述底稿，清洗器按 scene_id
    合并后一个字都没变——旧行为是抛出一份与原稿逐字相同的草稿、盖上 llm 来源、
    弹「已生成」，作者白等一轮、白付一次 token，还以为模型认可了现状。
    """
    if step_key != "scene_details":
        return
    base_scenes = [scene for scene in (base or {}).get("scenes") or [] if isinstance(scene, dict)]
    merged_scenes = [scene for scene in (merged or {}).get("scenes") or [] if isinstance(scene, dict)]
    # 场景规划的名册来自底稿，生成只深化、从不删场。合并后场数变少 = 有场在清洗/合并
    # 里掉了（例如没有 scene_id 的场对不上号，被整表丢弃）。这是最极端的空转：
    # 作者点一次生成，换回一份更空的草稿，旧代码还报「已生成」。
    if len(merged_scenes) < len(base_scenes):
        raise DomainError(
            "SNOWFLAKE_SCENE_DETAILS_LOST",
            f"生成后场景数从 {len(base_scenes)} 掉到 {len(merged_scenes)}——本次结果已丢弃，原草稿保持不变。"
            "通常是有场缺少系统指派的场景编号；请回「场景列表」重新保存一次以补齐编号后重试。",
            status_code=409,
            details={
                "node_id": "snowflake_step_generate",
                "step_key": step_key,
                "scenes_before": len(base_scenes),
                "scenes_after": len(merged_scenes),
                "next_action": "resave_scene_list_to_assign_ids",
            },
        )
    base_by_id = {
        str(scene.get("scene_id") or scene.get("row_uid") or ""): scene
        for scene in base_scenes
    }
    advanced = False
    inspected = 0
    for scene in (merged or {}).get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        identity = {str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")}
        if targeted_ids is not None and not (identity & targeted_ids):
            continue
        inspected += 1
        before = base_by_id.get(str(scene.get("scene_id") or scene.get("row_uid") or "")) or {}
        for key in _SCENE_CONTENT_KEYS:
            if normalize(scene.get(key)) != normalize(before.get(key)):
                advanced = True
                break
        if advanced:
            break
    if inspected and not advanced:
        raise DomainError(
            "SNOWFLAKE_LLM_EMPTY_GENERATION",
            "模型这次没有对指定场景产出任何新内容（常见原因是它回传了草稿里不存在的场景编号）。"
            "原草稿已原样保留，请重试；若反复如此，请检查该节点绑定的模型是否支持 JSON 结构化输出。",
            status_code=409,
            details={
                "node_id": "snowflake_step_generate",
                "step_key": step_key,
                "scenes_inspected": inspected,
                "next_action": "retry_or_check_node_model",
            },
        )


_SERVER_ASSIGNED_ITEM_KEYS = {"row_uid", "scene_id", "chapter_id", "chapter_title", "chapter_goal", "scene_seq", "character_id", "display_name"}


_SCENE_LIST_CONTENT_KEYS = ("summary", "pov_character_id", "location", "crucible", "chapter_role")


_CHARACTER_COLLECTION_STEPS = {"character_sheets", "character_synopses", "character_bibles"}


def _collect_generation_gaps(step_key: str, draft: dict[str, Any] | None) -> list[str]:
    """清洗后空字段盘点（修复重试的靶子）。step_completeness 对集合步只看顶层键
    是否非空，而残缺的主形态恰是「characters/scenes 数组在、字段全空」——这里按
    编辑器模板对集合项逐字段下钻（嵌套档案维度以整块全空计），scene_details 的
    逐场缺字段 step_completeness 本身已下钻。"""
    payload = draft if isinstance(draft, dict) else {}
    gaps = [str(field) for field in step_completeness(step_key, payload).get("missing_fields") or []]
    if step_key in _CHARACTER_COLLECTION_STEPS:
        template = _collection_template(step_key, "characters")
        checked = [field_key for field_key in template if field_key not in _SERVER_ASSIGNED_ITEM_KEYS]
        for index, item in enumerate(payload.get("characters") or [], start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("display_name") or item.get("character_id") or index)
            empty: list[str] = []
            for field_key in checked:
                template_value = template[field_key]
                value = item.get(field_key)
                if isinstance(template_value, dict):
                    nested = value if isinstance(value, dict) else {}
                    if not any(has_value(nested_value) for nested_value in nested.values()):
                        empty.append(field_key)
                elif not has_value(value):
                    empty.append(field_key)
            # 2026-09-30（B06-03，作者批准 #16b）：留白即合法——原著只要求主角 / 对手的表完整，配角可以只有
            # 一行定位。配角只在整个成员一个字都没写（连定位都没有）时才算缺口，不再逐字段逼模型替作者编。
            if is_lead_role(item.get("role")):
                gaps.extend(f"characters[{label}].{field_key}" for field_key in empty)
            elif checked and len(empty) == len(checked):
                gaps.append(f"characters[{label}]")
    elif step_key == "scene_list":
        for index, item in enumerate(payload.get("scenes") or [], start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("scene_id") or index)
            gaps.extend(
                f"scenes[{label}].{field_key}"
                for field_key in _SCENE_LIST_CONTENT_KEYS
                if not has_value(item.get(field_key))
            )
    return gaps


def _collection_template(step_key: str, field_key: str) -> dict[str, Any]:
    for field in get_step_definition(step_key).get("editor", {}).get("fields") or []:
        if field.get("key") == field_key:
            template = field.get("template")
            return template if isinstance(template, dict) else {}
    return {}


def _sanitize_step_patch(
    step_key: str,
    patch: dict[str, Any],
    *,
    latest_by_step: dict[str, Any],
    project_id: str,
    base: dict[str, Any],
    count_policy: str = "raise",
) -> dict[str, Any]:
    if not isinstance(patch, dict):
        return {}
    step_definition = get_step_definition(step_key)
    result: dict[str, Any] = {}
    for field in step_definition.get("editor", {}).get("fields") or []:
        key = field.get("key")
        if not isinstance(key, str) or key not in patch:
            continue
        value = patch.get(key)
        normalized = _sanitize_field_value(
            field,
            value,
            project_id=project_id,
            latest_by_step=latest_by_step,
            base=base,
            step_key=step_key,
            count_policy=count_policy,
        )
        if has_value(normalized):
            result[key] = normalized
    return result


def _assert_meaningful_generation_patch(step_key: str, patch: dict[str, Any]) -> None:
    if step_key != "character_sheets":
        return
    characters = patch.get("characters") if isinstance(patch, dict) else None
    if not isinstance(characters, list) or not characters:
        raise SparseGenerationOutput(
            "角色摘要表生成结果为空：模型没有回传任何有内容的角色。每个角色至少要有定位（role），"
            "再加上具体的目标、野心、价值观、阻碍、顿悟或故事线。"
        )
    if any(_has_meaningful_character_sheet_content(item) for item in characters if isinstance(item, dict)):
        return
    raise SparseGenerationOutput(
        "角色摘要表生成结果过于稀疏：模型只回传了 id / 姓名或空字段。每个角色至少要有定位（role），"
        "再加上具体的目标、野心、价值观、阻碍、顿悟或故事线。"
    )


def _has_meaningful_character_sheet_content(item: dict[str, Any]) -> bool:
    identity_values = {
        str(item.get("character_id") or "").strip(),
        str(item.get("display_name") or "").strip(),
        str(item.get("name") or "").strip(),
    }
    filled_fields = [
        field
        for field in (
            "role",
            "goal",
            "ambition",
            "values",
            "conflict",
            "epiphany",
            "one_sentence_summary",
            "one_paragraph_summary",
        )
        if _has_meaningful_non_identity_value(item.get(field), identity_values)
    ]
    pressure_fields = {
        "goal",
        "ambition",
        "values",
        "conflict",
        "epiphany",
        "one_sentence_summary",
        "one_paragraph_summary",
    }
    return len(filled_fields) >= 2 and any(field in pressure_fields for field in filled_fields)


def _has_meaningful_non_identity_value(value: Any, identity_values: set[str]) -> bool:
    if isinstance(value, list):
        return any(_has_meaningful_non_identity_value(item, identity_values) for item in value)
    if isinstance(value, dict):
        return any(_has_meaningful_non_identity_value(item, identity_values) for item in value.values())
    text = str(value or "").strip()
    return bool(text and text not in identity_values)


def _sanitize_field_value(
    field: dict[str, Any],
    value: Any,
    *,
    project_id: str,
    latest_by_step: dict[str, Any],
    base: dict[str, Any],
    step_key: str = "",
    count_policy: str = "raise",
) -> Any:
    kind = str(field.get("kind") or "")
    key = str(field.get("key") or "")
    if kind in {"text", "textarea"}:
        return str(value or "").strip()
    if kind == "paragraphs" and step_key in {"short_synopsis", "long_synopsis"}:
        # 阶段 B：一页梗概 = 五句各扩一段，恰好五段。多出来的段不再静默截掉。
        # 阶段 D：长篇大纲的五段展开同理——一页梗概的五段各扩成约一页，恰好五段。
        items = coerce_string_list(value)
        expected = SHORT_SYNOPSIS_PARAGRAPHS if step_key == "short_synopsis" else LONG_SYNOPSIS_PARAGRAPHS
        if len(items) > expected:
            if step_key == "short_synopsis":
                message = (
                    f"一页梗概要求恰好 {expected} 段（每段对应五句之一），"
                    f"模型返回了 {len(items)} 段；本次结果已丢弃。"
                )
            else:
                message = (
                    f"长篇大纲的五段展开要求恰好 {expected} 段（每段扩自一页梗概的一段，章表另列），"
                    f"模型返回了 {len(items)} 段；本次结果已丢弃。"
                )
            if count_policy == "raise":
                raise StructuredCountMismatch(message)
            return None
        return items
    if kind in {"list", "paragraphs"}:
        return coerce_string_list(value)
    if kind == "sentences":
        seed = base.get(key) if isinstance(base.get(key), list) else []
        items = coerce_string_list(value)
        if seed and len(items) > len(seed):
            message = (
                f"一段话概括要求恰好 {len(seed)} 句（开局、三次灾难、结局），"
                f"模型返回了 {len(items)} 句；本次结果已丢弃。"
            )
            if count_policy == "raise":
                raise StructuredCountMismatch(message)
            return None
        if seed and len(items) < len(seed):
            items.extend([""] * (len(seed) - len(items)))
        return items
    if kind == "object":
        nested = {}
        payload = value if isinstance(value, dict) else {}
        for nested_field in field.get("fields") or []:
            nested_key = nested_field.get("key")
            if not isinstance(nested_key, str):
                continue
            if nested_key not in payload:
                continue
            nested[nested_key] = str(payload.get(nested_key) or "").strip()
        return nested
    if kind in {"characters", "character_synopses", "character_bibles"}:
        template = field.get("template") if isinstance(field.get("template"), dict) else {}
        return _sanitize_character_items(
            value,
            template=template,
            project_id=project_id,
            latest_by_step=latest_by_step,
            base_items=base.get(key) if isinstance(base.get(key), list) else [],
        )
    if kind == "chapters":
        template = field.get("template") if isinstance(field.get("template"), dict) else {}
        return _sanitize_chapter_items(
            value,
            template=template,
            base_items=base.get(key) if isinstance(base.get(key), list) else [],
        )
    if kind == "scene_list":
        template = field.get("template") if isinstance(field.get("template"), dict) else {}
        return _sanitize_scene_list_items(
            value,
            template=template,
            project_id=project_id,
            base_items=base.get(key) if isinstance(base.get(key), list) else [],
        )
    if kind == "scene_details":
        return _sanitize_scene_detail_items(
            value,
            project_id=project_id,
            base_items=base.get(key) if isinstance(base.get(key), list) else [],
        )
    return None


_SPINE_MARKS = ("灾一", "灾二", "灾三")


def _sanitize_chapter_items(
    value: Any,
    *,
    template: dict[str, Any],
    base_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """07 长篇大纲的结构化章表（P2）。

    章序由服务端按数组顺序重排（模型给的编号常和顺序对不上）。``row_uid`` 是系统铸造的
    身份锚，**绝不收模型编的**——提示词也明说「Leave row_uid as ""」，模型自己编一个出来
    会把两章绑成同一行。

    但也不能一律清空。清空等于每次「AI 生成」都把整张章表判成**全新的**一批章：
    ``_sync_chapter_plans`` 于是给每一章铸新 uid、软删全部旧章行，并把分在旧章里的场
    ``chapter_plan_id`` 统统置空——作者只是想润一下章标题，已经分好的全书归属整片消失，
    物化闸门无声退回 blocked。所以这里按**最终章序对位**从底稿继承身份：第 N 章还是第
    N 章。模型多出来的章留空 uid，交给同步层铸新的（那才是真的新章）。

    脊柱只认三个合法标记。
    """
    if not isinstance(value, list):
        return []
    # 对位用最终章序（作者在分章面板看到的就是这个序），而不是模型数组下标——
    # 空壳章会被下面跳过，用下标对位会让身份整体错位一格。
    base_uids = [
        str(item.get("row_uid") or "").strip()
        for item in (base_items or [])
        if isinstance(item, dict)
    ]
    chapters: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title and not summary:
            continue  # 空壳章不进草稿：宁可少一章，也不要在分章面板里摆一行空的
        spine = str(item.get("spine") or "").strip()
        try:
            act = int(item.get("act") or 1)
        except (TypeError, ValueError):
            act = 1
        index = len(chapters) + 1
        chapters.append(
            {
                **deepcopy(template),
                "row_uid": base_uids[index - 1] if index <= len(base_uids) else "",
                "chapter_seq": index,
                "act": act if act in (1, 2, 3) else 1,
                "title": title,
                "summary": summary,
                "spine": spine if spine in _SPINE_MARKS else "",
                "chapter_goal": str(item.get("chapter_goal") or "").strip(),
            }
        )
    return chapters


def _sanitize_character_items(
    value: Any,
    *,
    template: dict[str, Any],
    project_id: str,
    latest_by_step: dict[str, Any],
    base_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    existing_by_name = {
        str(item.get("display_name") or "").strip(): str(item.get("character_id") or "").strip()
        for item in base_items
        if isinstance(item, dict) and str(item.get("display_name") or "").strip()
    }
    for step_key in ("character_sheets", "character_bibles", "character_synopses"):
        artifact = latest_by_step.get(step_key)
        if artifact is None:
            continue
        for item in (artifact.draft_json or {}).get("characters") or []:
            if not isinstance(item, dict):
                continue
            display_name = str(item.get("display_name") or "").strip()
            character_id = str(item.get("character_id") or "").strip()
            if display_name and character_id and display_name not in existing_by_name:
                existing_by_name[display_name] = character_id

    result = []
    for index, raw_item in enumerate(value, start=1):
        if not isinstance(raw_item, dict):
            continue
        item = {}
        display_name = str(raw_item.get("display_name") or raw_item.get("name") or "").strip()
        character_id = str(raw_item.get("character_id") or "").strip()
        if not display_name and not character_id and not any(
            has_value(raw_item.get(field_key)) for field_key in template if field_key != "character_id"
        ):
            # 完全空的成员（按 schema 约束解码的后端会回 `{}`）：不铸 id、不进名册——否则每个
            # 空对象都变成一个只有 id 的幽灵角色。
            continue
        if not character_id and display_name:
            character_id = existing_by_name.get(display_name, "")
        if not character_id:
            # 不再按位置编号（删掉中间一个角色，按位置编的号会落到下一个人身上）：铸一个规范 id（B06-01）
            character_id = mint_character_id(project_id)
        # 模型回的可能是前端口径的 id（c1）：规范成库里的口径，按 id 合并回底稿才对得上同一个人
        item["character_id"] = canonical_character_id(project_id, character_id)
        for field_key, template_value in template.items():
            if field_key == "character_id":
                continue
            sanitized = _prune_empty_values(_sanitize_template_value(template_value, raw_item.get(field_key)))
            # 生成/补丁是「补全」语义：空值不落键，_merge_patch 时不清空该成员的既有
            # 内容（与 FE 咨询式补丁一致）——模型部分回传不再抹掉手工填过的字段。
            if has_value(sanitized):
                item[field_key] = sanitized
        if not item.get("display_name"):
            item["display_name"] = display_name or character_id
        result.append(item)
    return result


def _prune_empty_values(value: Any) -> Any:
    """递归剥掉空叶子（"" / [] / {} / 全空子树），让补丁只携带有内容的键。"""
    if isinstance(value, dict):
        pruned = {key: _prune_empty_values(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if has_value(item)}
    return value


def _apply_scene_list_spine(items: list[dict[str, Any]]) -> None:
    """把模型标的灾一/灾二/灾三 收进场景表——原地改写 ``items``。

    ``spine`` **故意**不在 scene_list 的编辑器模板里，所以上面那圈模板遍历碰不到它。
    直接加进模板不行：模型没回 spine 时会落一个 ``spine: ""``，``_sanitize_scene_patch``
    照写进库，作者亲手标的三个灾难被无声抹掉。完全不收也不行：提示词明写要模型标 spine、
    还说「服务端丢弃其它键」，而 spine 恰恰就是被丢的那个 —— 脊柱锚点分章找不到锚，
    退化成按场数平均切章，三个灾难随机落在幕中间。

    规则按「模型这一轮到底有没有在用这个字段」分岔：

    - 一个合法标记都没有 → 模型压根没碰 spine，作者的标记原样留着（不落键）。
    - 标了至少一个 → 整表生成里模型给的脊柱就是新真相，未标的场显式清成 ""，
      否则新旧标记会并存（实测：旧的灾一在第 3 场、新的在第 5 场，两个灾一同时生效）。
    """
    if not any(item.get("spine") in _SPINE_MARKS for item in items):
        return
    for item in items:
        if item.get("spine") not in _SPINE_MARKS:
            item["spine"] = ""


def _sanitize_scene_list_items(
    value: Any,
    *,
    template: dict[str, Any],
    project_id: str,
    base_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    scene_seq_by_chapter: dict[str, int] = {}
    current_chapter_id = ""
    for index, raw_item in enumerate(value, start=1):
        if not isinstance(raw_item, dict):
            continue
        if not any(has_value(raw_item.get(field_key)) for field_key in (*template, "title", "mode", "spine")):
            # 完全空的成员（`{}`）不进场表：否则它会被铸成一场没有概要的幽灵场景。
            continue
        chapter_id = str(raw_item.get("chapter_id") or current_chapter_id or f"{project_id}_CH01").strip()
        current_chapter_id = chapter_id
        next_seq = scene_seq_by_chapter.get(chapter_id, 0) + 1
        scene_seq = int_or_default(raw_item.get("scene_seq"), next_seq)
        scene_seq_by_chapter[chapter_id] = scene_seq
        scene_id = str(raw_item.get("scene_id") or f"{chapter_id}_SC{scene_seq:02d}").strip()
        item = {
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "scene_seq": scene_seq,
        }
        for field_key, template_value in template.items():
            if field_key in {"scene_id", "chapter_id", "scene_seq"}:
                continue
            item[field_key] = _sanitize_template_value(template_value, raw_item.get(field_key))
        if not item.get("chapter_title"):
            item["chapter_title"] = chapter_id
        if not item.get("summary"):
            item["summary"] = str(raw_item.get("title") or "").strip()
        scene_type = str(
            raw_item.get("primary_form")
            or raw_item.get("scene_type")
            or raw_item.get("mode")
            or item.get("primary_form")
            or item.get("scene_type")
            or "proactive"
        ).strip().lower()
        scene_type = scene_type if scene_type in {"proactive", "reactive"} else "proactive"
        item["primary_form"] = scene_type
        item["scene_type"] = scene_type
        spine = str(raw_item.get("spine") or "").strip()
        if spine in _SPINE_MARKS:
            item["spine"] = spine
        result.append(item)
    _apply_scene_list_spine(result)
    if not result and base_items:
        return normalize(base_items)
    return result


def _sanitize_scene_detail_items(
    value: Any,
    *,
    project_id: str,
    base_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    del project_id
    items = value if isinstance(value, list) else []
    base_by_id = {
        str(item.get("scene_id") or ""): normalize(item)
        for item in base_items
        if isinstance(item, dict) and str(item.get("scene_id") or "")
    }
    overlay_by_id = {
        str(item.get("scene_id") or ""): item
        for item in items
        if isinstance(item, dict) and str(item.get("scene_id") or "")
    }
    allowed_keys = {
        "title",
        "summary",
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
        "target_length_band",
        "rendering_mode",
        "must_include_text",
        "exit_change",
        "hook",
        "triage_status",
        "triage_notes",
        "beats_json",
        # 阶段 J：在场人物 / 故事时间 / 读者应感到
        "onstage_chars_json",
        "story_time",
        "expected_reader_emotion",
        # 阶段 N：破例理由——作者写的，模型只能原样回显；提示词要求它不编。
        "exception_reason",
    }
    result = []
    for base_item in base_items:
        if not isinstance(base_item, dict):
            continue
        scene_id = str(base_item.get("scene_id") or "")
        merged = dict(base_by_id.get(scene_id) or normalize(base_item))
        overlay = overlay_by_id.get(scene_id, {})
        for key in allowed_keys:
            if key not in overlay:
                continue
            if key == "rendering_mode":
                # 阶段 C / N：summary 对两种形态都合法，skip 只给反应场；非法值不落键（保持 full）。
                mode = str(overlay.get(key) or "").strip().lower()
                form = str(merged.get("primary_form") or merged.get("scene_type") or "proactive").strip().lower()
                if mode in RENDERING_MODES and (mode != "skip" or form == "reactive"):
                    merged[key] = mode
                continue
            if key in {"primary_form", "scene_type"}:
                scene_type = str(
                    overlay.get(key)
                    or merged.get("primary_form")
                    or merged.get("scene_type")
                    or "proactive"
                ).strip().lower()
                scene_type = scene_type if scene_type in {"proactive", "reactive"} else str(merged.get("primary_form") or merged.get("scene_type") or "proactive")
                merged["primary_form"] = scene_type
                merged["scene_type"] = scene_type
            elif key in {"beats_json", "onstage_chars_json"}:
                beats = coerce_string_list(overlay.get(key))
                if beats:
                    merged[key] = beats
            else:
                text = str(overlay.get(key) or "").strip()
                # 补全语义：模型对某键回传空串不清空底稿既有内容
                if text:
                    merged[key] = text
        result.append(merged)
    return result


def _sanitize_template_value(template_value: Any, value: Any) -> Any:
    if isinstance(template_value, dict):
        payload = value if isinstance(value, dict) else {}
        return {
            str(field_key): _sanitize_template_value(nested_template, payload.get(field_key))
            for field_key, nested_template in template_value.items()
        }
    if isinstance(template_value, list):
        return coerce_string_list(value)
    return str(value or "").strip()


#: 旧名（``snowflake_workspace_llm`` 照旧转出）；实现在 ``snowflake_draft_merge``
_merge_patch = apply_member_patch
_collection_id_key = patch_id_key


# 场景分诊修补器接受的场景键——也是分诊 wire schema 里 repair_patch 的 properties（enrich_structured_schema）。
_SCENE_REPAIR_PATCH_KEYS = frozenset(
    {
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
        "exit_change",
        "hook",
        "target_length_band",
        "must_include_text",
    }
)


def _sanitize_scene_repair_patch(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = _SCENE_REPAIR_PATCH_KEYS
    return {key: str(value.get(key) or "").strip() for key in allowed if key in value and str(value.get(key) or "").strip()}

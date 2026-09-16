"""雪花工作台提示词的输入预算：超预算时分级降载，并如实上报降了什么。

**为什么不复用 `context_budget.apply_context_budget`**：那套 `SECTION_SPECS` 是
场景写作专用的（chapter_goal / scene_card / style_profile / foreshadow…），与雪花
工作台的载荷形状（step 契约 + upstream_steps + current_draft + focus_*）没有交集。

**为什么不按「深层取代浅层」去重角色三步**：角色摘要表（目标/野心/价值观/顿悟）、
角色背景故事（synopsis）、角色全档案（四类 profile）三步的字段**互不重叠**，只共享
身份三元组。丢掉任何一层都是在销毁故事事实，不是省预算。

真正可降的是**与本次生成无关的材料**，判据只有一条：这一批要深化的是哪些成员。
焦点成员与本步契约永不降载；其余材料按下面的阶梯逐级降到参照级，每级之后重新估算，
达标即停——能少降就少降。跑完全部阶梯仍超预算时不静默，交回报告让调用方告诉作者。
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable

from novel_system.services.context_budget import estimate_tokens

# 本步契约与作者显式意图：任何情况下都不降载。降了它们模型就不知道要产出什么，
# 省下的预算换来一次废调用。
AUTHOR_DIRECTION_BRIEF_KEY = "author_direction_brief"

PROTECTED_KEYS = frozenset(
    {
        "step_key",
        "step_label",
        "step_english_label",
        "step_description",
        "step_instruction",
        "step_guidance",
        "step_editor",
        "scene_rules",
        "pressure_rubric",
        "current_pressure_diagnosis",
        "project",
        "focus_scenes",
        "focus_characters",
        "adopted_direction",
        "completeness_repair",
        "upstream_steps_how_to_use",
        "current_draft_how_to_use",
        # 2026-09-16 阶段 T：作者意图要点（教练对话蒸馏、作者核过）。条目数与单条长度在
        # snowflake_direction_brief 里有硬上限（16 条 × 160 字 + 继承 24 条），受保护也不会失控。
        AUTHOR_DIRECTION_BRIEF_KEY,
    }
)

# 场景规划的底稿种子已经从场景列表带走了这些字段（见 snowflake_steps._scene_detail_seed），
# 上游那份再发一遍纯属重复。留下的是种子没带、模型确实要用的。
_SCENE_LIST_DELTA_KEYS = (
    "scene_id",
    "scene_seq",
    "chapter_id",
    "chapter_title",
    "chapter_goal",
    "pov_character_id",
    "chapter_role",
)

# 参照级角色：留身份、定位和一句话，模型知道「这个人存在、是谁」，不会写出与之矛盾
# 的内容，也不会因为名册缺人而现编一个。
_CHARACTER_REFERENCE_KEYS = ("character_id", "display_name", "role", "one_sentence_summary")

# 参照级场景：留身份、位置与标题，模型知道「这一场在哪、叫什么」。
_SCENE_REFERENCE_KEYS = (
    "scene_id",
    "row_uid",
    "chapter_id",
    "scene_seq",
    "title",
    "primary_form",
    "scene_type",
)

_CHARACTER_STEP_KEYS = ("character_sheets", "character_synopses", "character_bibles")

# 同一份材料在不同节点上叫不同的键：整步生成用 upstream_steps / current_draft，
# 驻场教练用 approved_context，候选生成用 current_canonical_draft。形状完全一致
# （前者是 [{step_key,label,status,confirmed,draft}]，后者是本步规范草稿），
# 所以阶梯按**角色**取键，不认字面名——否则顾问型节点整条阶梯全空转，
# 报告说超预算却把原样的超大载荷发出去。
_UPSTREAM_ROLE_KEYS = ("upstream_steps", "approved_context")
_DRAFT_ROLE_KEYS = ("current_draft", "current_canonical_draft")

# 2026-09-12 结构跟随：场景清单 / 场景规划的载荷成员——参考作者的结构画像（带数字）、
# 章首 / 章尾样例（封装原文）、场景手法。它是外部参考而非本书事实：降载时先于上游长散文
# 与底稿参照条目让路（先卸样例，再卸整张画像），但排在纯重复的三级之后。
STYLE_REFERENCE_STRUCTURE_KEY = "style_reference_structure"
_STYLE_REFERENCE_SAMPLES_KEY = "structure_samples"
_STYLE_REFERENCE_SAMPLES_SHED_NOTE = "章首 / 章尾样例因输入预算省略；仍按结构画像的尺度与开合方式规划。"


def _role_key(payload: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    return next((key for key in candidates if isinstance(payload.get(key), (list, dict))), None)


_TRUNCATION_MARK = "…（上下文预算已截断）"
_LONG_TEXT_TOKEN_CAP = 160


def estimate_payload_tokens(payload: dict[str, Any]) -> int:
    """载荷的输入 token 估算（与 `_render_user_prompt` 的紧凑 JSON 渲染一致）。"""
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def apply_snowflake_prompt_budget(
    payload: dict[str, Any],
    *,
    budget_tokens: int,
    step_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把载荷压到预算内，返回 (载荷, 报告)。

    `budget_tokens <= 0` 表示不设预算（原样返回）。报告里 `applied` 是实际生效的
    降载级别，`within_budget` 为假表示阶梯跑完仍超——调用方要让作者看见这件事。
    """
    before = estimate_payload_tokens(payload)
    report: dict[str, Any] = {
        "budget_tokens": int(budget_tokens),
        "estimated_before": before,
        "estimated_after": before,
        "applied": [],
        "within_budget": True,
    }
    if budget_tokens <= 0 or before <= budget_tokens:
        return payload, report

    current = deepcopy(payload)
    focus = _focus_context(current, step_key=step_key)
    for name, rung in _ladder(step_key):
        reduced = _restore_protected(rung(current, focus), payload, report)
        if reduced is None:
            continue
        current = reduced
        report["applied"].append(name)
        after = estimate_payload_tokens(current)
        report["estimated_after"] = after
        if after <= budget_tokens:
            return current, report

    report["within_budget"] = False
    return current, report


def _restore_protected(
    reduced: dict[str, Any] | None, original: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any] | None:
    """把受保护的键按原样钉回去——让 PROTECTED_KEYS 成为生产代码里真正生效的
    不变量，而不是一句只有测试读的注释。

    削掉本步契约或焦点成员，省下的预算换来的是一次不知道要产出什么的废调用；
    这种错误只会在新增降载级别时误伤，所以恢复动作要记进报告，让它可见而非静默。
    """
    if reduced is None:
        return None
    violated = [key for key in PROTECTED_KEYS if key in original and reduced.get(key) != original[key]]
    if not violated:
        return reduced
    restored = {**reduced, **{key: original[key] for key in violated}}
    report.setdefault("protected_restored", [])
    report["protected_restored"].extend(key for key in violated if key not in report["protected_restored"])
    return restored


def budget_audit_fields(report: dict[str, Any]) -> dict[str, Any]:
    """把报告摊平成 LLM 审计摘要认的形状。

    `llm_audit.sanitize_audit_summary` 是一条信任边界：只有有界的协议/配置原子能以
    字面量存活，超限时更只留白名单里的键。嵌套 dict 会在压缩时被整块换成指纹——
    所以这里摊平成 `_status` / `_tokens` 后缀的键（白名单已覆盖），
    `prompt_budget_applied` 则登记在 `_SAFE_LITERAL_LIST_KEYS`。
    """
    return {
        "prompt_budget_status": "within_budget" if report.get("within_budget", True) else "exceeded",
        "prompt_budget_tokens": int(report.get("budget_tokens") or 0),
        "prompt_input_tokens": int(report.get("estimated_after") or 0),
        "prompt_pre_shed_input_tokens": int(report.get("estimated_before") or 0),
        "prompt_budget_applied": list(report.get("applied") or []),
    }


def _ladder(step_key: str) -> tuple[tuple[str, Callable[..., dict[str, Any] | None]], ...]:
    """降载阶梯：越靠前越接近「纯重复」，越靠后越接近「真材料」。"""
    return (
        ("upstream_scene_list_delta", _rung_upstream_scene_list_delta),
        ("reference_characters_to_identity", _rung_reference_characters),
        ("reference_scenes_to_identity", _rung_reference_scenes),
        ("drop_style_reference_samples", _rung_drop_style_reference_samples),
        ("drop_style_reference_structure", _rung_drop_style_reference_structure),
        ("truncate_long_prose", _rung_truncate_long_prose),
        ("drop_reference_scenes", _rung_drop_reference_scenes),
    )


def _focus_context(payload: dict[str, Any], *, step_key: str) -> dict[str, Any]:
    """本次生成到底在深化谁：判不出来就留空，后面对应的降载级别一律跳过——
    宁可多花预算，也不能靠猜把有用的材料降掉。"""
    focus_scenes = [
        scene
        for scene in ((payload.get("focus_scenes") or {}).get("scenes") or [])
        if isinstance(scene, dict)
    ]
    focus_scene_ids = {
        str(scene.get("scene_id") or "").strip()
        for scene in focus_scenes
        if str(scene.get("scene_id") or "").strip()
    } | {
        str(scene.get("row_uid") or "").strip()
        for scene in focus_scenes
        if str(scene.get("row_uid") or "").strip()
    }
    focus_characters = [
        member
        for member in ((payload.get("focus_characters") or {}).get("characters") or [])
        if isinstance(member, dict)
    ]
    relevant_characters = {
        str(member.get("character_id") or "").strip()
        for member in focus_characters
        if str(member.get("character_id") or "").strip()
    }
    # 场景批次的相关角色 = 焦点场的 POV + 在焦点场文本里被点名的人
    if focus_scenes:
        relevant_characters |= {
            str(scene.get("pov_character_id") or "").strip()
            for scene in focus_scenes
            if str(scene.get("pov_character_id") or "").strip()
        }
        relevant_characters |= _characters_named_in(focus_scenes, payload)
    return {
        "step_key": step_key,
        "scene_ids": focus_scene_ids,
        "character_ids": relevant_characters,
        "neighbor_ids": _neighbor_scene_ids(payload, focus_scene_ids),
    }


def _characters_named_in(focus_scenes: list[dict[str, Any]], payload: dict[str, Any]) -> set[str]:
    text = json.dumps(focus_scenes, ensure_ascii=False)
    named: set[str] = set()
    upstream_key = _role_key(payload, _UPSTREAM_ROLE_KEYS)
    for item in (payload.get(upstream_key) if upstream_key else None) or []:
        if not isinstance(item, dict):
            continue
        for member in (item.get("draft") or {}).get("characters") or []:
            if not isinstance(member, dict):
                continue
            name = str(member.get("display_name") or "").strip()
            character_id = str(member.get("character_id") or "").strip()
            if name and name in text and character_id:
                named.add(character_id)
    return named


def _neighbor_scene_ids(payload: dict[str, Any], focus_scene_ids: set[str]) -> set[str]:
    """焦点场的紧邻前后场：衔接要靠它们（上一场的挫败接出本场目标）。"""
    draft_key = _role_key(payload, _DRAFT_ROLE_KEYS)
    scenes = [
        scene
        for scene in (((payload.get(draft_key) if draft_key else None) or {}).get("scenes") or [])
        if isinstance(scene, dict)
    ]
    positions = [
        index
        for index, scene in enumerate(scenes)
        if {str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")} & focus_scene_ids
    ]
    neighbors: set[str] = set()
    for position in positions:
        for offset in (-1, 1):
            if 0 <= position + offset < len(scenes):
                scene = scenes[position + offset]
                neighbors |= {
                    str(scene.get("scene_id") or ""),
                    str(scene.get("row_uid") or ""),
                }
    return {value for value in neighbors if value}


def _rung_upstream_scene_list_delta(
    payload: dict[str, Any], focus: dict[str, Any]
) -> dict[str, Any] | None:
    """场景规划：上游那份场景列表与底稿高度重复，只留底稿种子没带走的字段。"""
    if focus.get("step_key") != "scene_details":
        return None
    upstream_key = _role_key(payload, _UPSTREAM_ROLE_KEYS)
    if upstream_key is None:
        return None
    changed = False
    steps = []
    for item in payload.get(upstream_key) or []:
        if not isinstance(item, dict) or item.get("step_key") != "scene_list":
            steps.append(item)
            continue
        scenes = [scene for scene in (item.get("draft") or {}).get("scenes") or [] if isinstance(scene, dict)]
        trimmed = [
            {key: scene[key] for key in _SCENE_LIST_DELTA_KEYS if key in scene and scene[key] not in ("", None, [])}
            for scene in scenes
        ]
        # 已经削过就别再声称削了一次：阶梯靠 applied 判断「这一级有没有腾出空间」，
        # 谎报会让上层以为还有余地可降。
        if not trimmed or trimmed == scenes:
            steps.append(item)
            continue
        steps.append({**item, "draft": {**(item.get("draft") or {}), "scenes": trimmed}})
        changed = True
    if not changed:
        return None
    return {**payload, upstream_key: steps}


def _rung_reference_characters(
    payload: dict[str, Any], focus: dict[str, Any]
) -> dict[str, Any] | None:
    """与本批无关的角色降到参照级：留身份/定位/一句话，细节让位给焦点材料。

    判不出相关角色（没有 POV、也没在焦点文本里点名）就整级跳过——那种情况下
    「无关」是猜的，猜错就是把主角的档案降掉。
    """
    relevant = focus.get("character_ids") or set()
    if not relevant:
        return None
    upstream_key = _role_key(payload, _UPSTREAM_ROLE_KEYS)
    if upstream_key is None:
        return None
    changed = False
    steps = []
    for item in payload.get(upstream_key) or []:
        if not isinstance(item, dict) or item.get("step_key") not in _CHARACTER_STEP_KEYS:
            steps.append(item)
            continue
        item_changed = False
        trimmed = []
        for member in (item.get("draft") or {}).get("characters") or []:
            if not isinstance(member, dict):
                continue
            if str(member.get("character_id") or "").strip() in relevant:
                trimmed.append(member)
                continue
            reference = {
                key: member[key] for key in _CHARACTER_REFERENCE_KEYS if key in member and member[key]
            }
            item_changed = item_changed or reference != member
            trimmed.append(reference)
        if not item_changed:
            steps.append(item)
            continue
        changed = True
        steps.append({**item, "draft": {**(item.get("draft") or {}), "characters": trimmed}})
    if not changed:
        return None
    return {**payload, upstream_key: steps}


def _rung_reference_scenes(payload: dict[str, Any], focus: dict[str, Any]) -> dict[str, Any] | None:
    """底稿里既非焦点、也非紧邻的场降到参照级（连概要一起让出）。"""
    keep = (focus.get("scene_ids") or set()) | (focus.get("neighbor_ids") or set())
    if not keep:
        return None
    draft_key = _role_key(payload, _DRAFT_ROLE_KEYS)
    if draft_key is None:
        return None
    draft = payload.get(draft_key) or {}
    scenes = draft.get("scenes") or []
    changed = False
    trimmed = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if {str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")} & keep:
            trimmed.append(scene)
            continue
        reference = {key: scene[key] for key in _SCENE_REFERENCE_KEYS if key in scene and scene[key] not in ("", None)}
        if reference != scene:
            changed = True
        trimmed.append(reference)
    if not changed:
        return None
    return {**payload, draft_key: {**draft, "scenes": trimmed}}


def _rung_drop_style_reference_samples(
    payload: dict[str, Any], focus: dict[str, Any]
) -> dict[str, Any] | None:
    """参考作者的章首 / 章尾样例最占体量、也最不影响尺度判断：先卸它，留下带数字的画像。
    留一句说明——模型要知道样例是被预算省掉的，不是这位作者没有开合方式。"""
    del focus
    member = payload.get(STYLE_REFERENCE_STRUCTURE_KEY)
    if not isinstance(member, dict) or not member.get(_STYLE_REFERENCE_SAMPLES_KEY):
        return None
    trimmed = {key: value for key, value in member.items() if key != _STYLE_REFERENCE_SAMPLES_KEY}
    trimmed["structure_samples_note"] = _STYLE_REFERENCE_SAMPLES_SHED_NOTE
    return {**payload, STYLE_REFERENCE_STRUCTURE_KEY: trimmed}


def _rung_drop_style_reference_structure(
    payload: dict[str, Any], focus: dict[str, Any]
) -> dict[str, Any] | None:
    """整张参考画像让位：它是外部参考，本书自己的上游散文与场表条目比它重要。"""
    del focus
    if STYLE_REFERENCE_STRUCTURE_KEY not in payload:
        return None
    return {key: value for key, value in payload.items() if key != STYLE_REFERENCE_STRUCTURE_KEY}


def _rung_truncate_long_prose(payload: dict[str, Any], focus: dict[str, Any]) -> dict[str, Any] | None:
    """上游长散文按上限截断，并留下显式截断标记——模型要知道这里被砍过，
    不能把截断当成作者写完了。"""
    del focus
    upstream_key = _role_key(payload, _UPSTREAM_ROLE_KEYS)
    if upstream_key is None:
        return None
    steps = []
    changed = False
    for item in payload.get(upstream_key) or []:
        if not isinstance(item, dict):
            steps.append(item)
            continue
        truncated, hit = _truncate_tree(item.get("draft"))
        changed = changed or hit
        steps.append({**item, "draft": truncated} if hit else item)
    if not changed:
        return None
    return {**payload, upstream_key: steps}


def _truncate_tree(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        # 幂等：已截断的文本不再截一次，否则标记会一层层叠加，
        # 而且每次都谎报「本级又腾出了空间」。
        if value.endswith(_TRUNCATION_MARK) or estimate_tokens(value) <= _LONG_TEXT_TOKEN_CAP:
            return value, False
        return _truncate_text(value), True
    if isinstance(value, list):
        results = [_truncate_tree(item) for item in value]
        return [item for item, _ in results], any(hit for _, hit in results)
    if isinstance(value, dict):
        results = {key: _truncate_tree(item) for key, item in value.items()}
        return (
            {key: item for key, (item, _) in results.items()},
            any(hit for _, hit in results.values()),
        )
    return value, False


def _truncate_text(text: str) -> str:
    # estimate_tokens 对中日韩宽字符按 1 token/字计，其余按 4 字符/token；
    # 这里按宽字符上界切，并把截断标记自身的开销先扣出去，保证结果一定在上限内。
    keep = max(1, _LONG_TEXT_TOKEN_CAP - estimate_tokens(_TRUNCATION_MARK))
    return text[:keep].rstrip() + _TRUNCATION_MARK


def _rung_drop_reference_scenes(
    payload: dict[str, Any], focus: dict[str, Any]
) -> dict[str, Any] | None:
    """兜底：底稿只留焦点场与紧邻前后场。到这一级说明这本书的上游材料本身
    已经超出预算，再留参照条目也只是把预算耗在名单上。"""
    keep = (focus.get("scene_ids") or set()) | (focus.get("neighbor_ids") or set())
    if not keep:
        return None
    draft_key = _role_key(payload, _DRAFT_ROLE_KEYS)
    if draft_key is None:
        return None
    draft = payload.get(draft_key) or {}
    scenes = [
        scene
        for scene in draft.get("scenes") or []
        if isinstance(scene, dict)
        and {str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")} & keep
    ]
    if not scenes or len(scenes) == len(draft.get("scenes") or []):
        return None
    return {**payload, draft_key: {**draft, "scenes": scenes}}

"""雪花 LLM 节点的上下文载荷：上游各步、本步底稿、焦点场 / 焦点角色、采纳的方向、分批深化的批次。

纯函数（只读草稿与步骤目录，不碰会话）。2026-09-30 从 ``snowflake_workspace_llm.py`` 拆出（B06-08），
那边原样转出这里的每一个名字。
"""

from __future__ import annotations

from typing import Any, Mapping

from novel_system.db.models import StoryProject
from novel_system.services.hash_engine import normalize
from novel_system.services.snowflake_step_catalog import CONFIRMED_STEP_STATUSES, STEP_ORDER, list_step_definitions
from novel_system.services.snowflake_step_drafts import merge_step_draft
from novel_system.services.snowflake_step_diagnosis import diagnose_scene_detail
from novel_system.services.value_coercion import has_value


# 场景规划分批深化的每批场数。整表一次生成对任何真实体量的书都不可行：
# 30 场的完整 Scene/Sequel 明细就已逼近 max_output_tokens=8192（客户端降级阶梯的
# 上限，见 llm_client.MAX_OUTPUT_TOKENS_CEILING），60-150 场的长篇必然被砍断——
# 要么硬失败，要么模型自行「只深化前几场」交回一份半新半旧的割裂草稿。
# 6 场/批留足余量：reasoning 模型的思考 token 同样吃 max_output_tokens。
SCENE_DETAIL_BATCH_SIZE = 6


# 单次请求最多跑几批。分批把一次调用换成若干次串行调用，长篇（100+ 场）不设上限
# 就会让一次点击变成半小时的同步请求（幂等租约只有 600s，见 config/models.yaml
# job_runtime）。封顶后剩余场次由 notice 告诉作者，再点一次从第一场未完成处续深。
SCENE_DETAIL_MAX_BATCHES_PER_RUN = 6


def _adopted_direction_payload(text: str, *, kind: str | None, focused: bool) -> dict[str, Any]:
    """「采纳并结构化」的方向蓝本。来源不同用法不同：候选正文是可直接展开的基调；教练回复是判断与建议，
    要照它点名的缺口 / 走向 / 禁忌去展开，而不是把回复当成正文意象来保留。"""
    if kind == "coach_reply":
        how = (
            "作者已选定驻场教练的这段回复作为定向方向：只把它落实到 focus 指定的成员上——"
            "它点名的缺口必须补上、它建议的走向必须落实、它否定的写法不得出现；焦点外的成员一律不动、不复述。"
            if focused
            else "作者已选定驻场教练的这段回复作为本步的方向：按它的判断与建议展开本步全部字段——"
            "它点名的缺口必须补上、它建议的走向必须落实、它否定的写法不得出现，不要另起新方向；"
            "它没有覆盖到的字段按上游材料补全。"
        )
    else:
        how = (
            # 定向采纳（候选 × 焦点成员）与整步采纳的蓝本用法不同：前者只落到焦点成员
            "作者已选定这段文字作为定向蓝本：只把它落实到 focus 指定的成员上，"
            "保留它的核心意象、人物立场与转折；焦点外的成员一律不动、不复述。"
            if focused
            else "作者已选定这段文字作为本步的方向蓝本：以它为基调把本步全部字段结构化展开，"
            "保留它的核心意象、人物立场与转折，不要另起新方向；它没有覆盖到的字段按上游材料补全。"
        )
    return {"text": text, "source": kind or "candidate", "how_to_use": how}


def _project_prompt_payload(project: StoryProject) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "title": project.title,
        "genre": project.genre,
        "target_word_count": project.target_word_count,
        "target_chapter_count": project.target_chapter_count,
        "outline_text": project.outline_text,
    }


def _sanitize_canonical_draft(draft: dict[str, Any] | None) -> dict[str, Any]:
    """提示上下文净化：剥掉前端写穿缓存的 fe_* 键（脚手架 JSON / 状态 / 历史账本），
    它们与规范字段内容重复且占提示预算；作者自由草稿（fe_text）是真实创作意图，
    以显式键 author_free_draft 保留。"""
    payload = {k: v for k, v in (draft or {}).items() if not str(k).startswith("fe_")}
    free_text = str((draft or {}).get("fe_text") or "").strip()
    if free_text:
        payload["author_free_draft"] = free_text
    return payload


# 上游上下文的用法说明：状态是「这份材料有多稳」的提示，不是「要不要遵守」的开关。
UPSTREAM_STEPS_HOW_TO_USE = (
    "这是本步之前每一步的规范草稿，按雪花顺序排列，是本作品故事事实的唯一来源："
    "人物姓名与 id、地点、时间线、已埋的冲突与灾难链，全部以它们为准，不得另起炉灶。"
    "confirmed=true 是作者确认过的事实。confirmed=false 是作者当前的工作稿：同样不得忽略或改写，"
    "但它里面空着或写着「未定」的槽位是作者有意留白，不要替作者从想象里补满——"
    "留白照样留白，只在本步的产出里写本步该写的东西。"
)


# 本步自己的草稿与上游留白规则的分界：上游空槽是作者的留白，本步空槽是这次要写的目标。
# 2026-09-16 真实故障的另一半：只有「留白照样留白」而没有这句，模型面对一张只有 role 的空角色表
# 有理由把空槽当成作者意图原样交回。
CURRENT_DRAFT_HOW_TO_USE = (
    "current_draft 是本步现有的规范草稿，可能只是一张空脚手架：其中已有内容的字段是作者已经写下的事实，"
    "保留并深化；空着的字段正是本次要生成的目标——它们不是作者的留白（上游步骤里的留白才是，见 "
    "upstream_steps_how_to_use）。current_pressure_diagnosis 只评价已写下的内容，不会把空槽记为缺口；"
    "有 adopted_direction 时以它为蓝本把这些字段全部展开。"
)


def _upstream_step_context(
    latest_by_step: Mapping[str, Any],
    *,
    step_key: str | None = None,
) -> list[dict[str, Any]]:
    """本步之前所有步骤的规范草稿（按雪花顺序），带确认状态。

    这里**不能**只收 approved/skipped。explore 模式（雪花工作台建的作品默认就是它）
    允许作者一路生成不确认，而改动上游又会把下游整片打成 stale——只收已确认就等于
    把整条故事线从提示词里删掉，模型只剩书名可用，于是凭空另编一本书。这不是假想：
    真实故障：一部作品的场景列表里冒出一个与前八步毫无关系的主角，就是这么来的。

    未确认的草稿同样是作者此刻认定的故事事实，必须进上下文，只是要如实标注状态。
    """
    limit = STEP_ORDER.get(str(step_key or ""), len(STEP_ORDER))
    items: list[dict[str, Any]] = []
    for definition in list_step_definitions():
        key = str(definition["step_key"])
        if STEP_ORDER[key] >= limit:
            continue
        artifact = latest_by_step.get(key)
        if artifact is None:
            continue
        draft = _sanitize_canonical_draft(
            merge_step_draft(key, getattr(artifact, "draft_json", None), latest_by_step=dict(latest_by_step))
        )
        # 空骨架（还没写的步骤）不占提示预算，也别让模型误以为作者已经交代过什么。
        if not has_value(draft):
            continue
        status = str(getattr(artifact, "status", "") or "")
        items.append(
            {
                "step_key": key,
                "label": definition.get("label"),
                "status": status,
                "confirmed": status in CONFIRMED_STEP_STATUSES,
                "draft": draft,
            }
        )
    return items


# 焦外场景在提示词里保留的参照键（它是什么、接在哪），以及紧邻前后场额外保留的
# 衔接键（上一场的挫败/决定要能接出本场的目标）。
_SCENE_REFERENCE_KEYS = (
    "scene_id", "row_uid", "chapter_id", "chapter_title", "scene_seq",
    "title", "summary", "primary_form", "scene_type", "location",
    "pov_character_id", "chapter_role",
)


_SCENE_NEIGHBOR_KEYS = _SCENE_REFERENCE_KEYS + (
    "goal", "setback", "decision", "exit_change", "hook",
)


def _scene_ref(scene: dict[str, Any]) -> str:
    """定向指认一场的统一口径：优先系统指派的 scene_id，退到前端铸的 row_uid。

    分批、焦点解析、空转防线三处必须用同一口径——口径不一致时，缺 scene_id 的场
    会被「分批指得着、防线看不见」，一次白跑还报成功。
    """
    return str(scene.get("scene_id") or "").strip() or str(scene.get("row_uid") or "").strip()


def _scene_detail_batches(draft: dict[str, Any]) -> tuple[list[list[str]], int]:
    """把场景规划草稿切成分批深化的场景 ref 列表，返回 (批次, 本次未覆盖的场数)。

    返回空批次列表 = 不需要分批，调用方走原来的整表单次通道。两种情况会这样：
    场表本身不超过一批；或者有场既没有 scene_id 也没有 row_uid（定向指不着它）——
    分批的代价绝不能是「指不着的场永远轮不到」，宁可慢，不可漏。

    起点取「第一场还缺必填项的场」：整表全空时就是第 0 场（首次整表生成），
    深化到一半时就是上次断掉的地方（作者点的按钮本来就叫「全部补全」）。
    注意剩余不足一批时**仍然分批**——那正是续深场景，退回整表通道会把已经
    深化好的场连带重做一遍，既烧 token 又可能改写作者已认可的内容。

    单次封顶剩下的场数原样返回，由调用方告诉作者还剩多少、再点一次继续。
    """
    scenes = [scene for scene in (draft or {}).get("scenes") or [] if isinstance(scene, dict)]
    refs: list[str] = []
    for scene in scenes:
        ref = _scene_ref(scene)
        if not ref:
            return [], 0
        refs.append(ref)
    if len(refs) <= SCENE_DETAIL_BATCH_SIZE:
        return [], 0

    start = next(
        (
            index
            for index, scene in enumerate(scenes)
            if diagnose_scene_detail(scene, index=index + 1).get("missing_fields")
        ),
        0,
    )
    todo = refs[start:]
    batches = [todo[i : i + SCENE_DETAIL_BATCH_SIZE] for i in range(0, len(todo), SCENE_DETAIL_BATCH_SIZE)]
    pending = sum(len(batch) for batch in batches[SCENE_DETAIL_MAX_BATCHES_PER_RUN:])
    return batches[:SCENE_DETAIL_MAX_BATCHES_PER_RUN], pending


def _compact_scene_context(draft: dict[str, Any], focus_refs: set[str]) -> dict[str, Any]:
    """定向深化的提示上下文：焦点场给全量明细，焦外场压成参照条目。"""
    payload = _sanitize_canonical_draft(draft)
    scenes = [scene for scene in payload.get("scenes") or [] if isinstance(scene, dict)]

    def _is_focus(scene: dict[str, Any]) -> bool:
        return bool({str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")} & focus_refs)

    focus_positions = [index for index, scene in enumerate(scenes) if _is_focus(scene)]
    neighbor_positions = {pos + offset for pos in focus_positions for offset in (-1, 1)}
    compacted = []
    for index, scene in enumerate(scenes):
        if _is_focus(scene):
            compacted.append(scene)
            continue
        keys = _SCENE_NEIGHBOR_KEYS if index in neighbor_positions else _SCENE_REFERENCE_KEYS
        compacted.append({key: scene[key] for key in keys if key in scene and has_value(scene[key])})
    payload["scenes"] = compacted
    return payload


def _scene_rules(step_key: str) -> dict[str, Any] | None:
    if step_key != "scene_details":
        return None
    return {
        "primary_form_field": "primary_form",
        "proactive": ["goal", "conflict", "setback"],
        "reactive": ["reaction", "dilemma", "decision"],
        "follow_up_fields_are_allowed": True,
    }


def _pressure_rubric(step_key: str) -> dict[str, Any]:
    scene_rules = _scene_rules(step_key)
    rubric = {
        "goal": "让每一层雪花都更容易扩展成带目标、阻力、代价和变化的具体场景。",
        "dimensions": [
            "读者承诺：目标读者和类型爽点足够具体，能指导取舍",
            "因果升级：每一层都让下一事件更难、更贵或更不可逆",
            "角色压力：目标、价值、阻力和变化都能看见",
            "场景开放循环：场景层保留主动场景的目标/冲突/挫折，或反应场景的反应/困境/决定",
            "连续性：保留用户项目中已确认的事实、ID、顺序和语言",
        ],
        "repair_priority": [
            "先补缺失的必需结构",
            "把泛泛压力替换成具体阻力和代价",
            "让结尾或决定制造下一层继续展开的需要",
        ],
    }
    if scene_rules is not None:
        rubric["scene_rules"] = scene_rules
    return rubric


def _focus_scene_payload(step: dict[str, Any], focus_scene_id: str | None) -> dict[str, Any] | None:
    scene_id = str(focus_scene_id or "").strip()
    if not scene_id:
        return None
    draft = step.get("draft") if isinstance(step.get("draft"), dict) else {}
    for scene in draft.get("scenes") or []:
        # FE 场景规划以 row_uid 为键，教练单场聚焦允许 row_uid 或 scene_id 指场
        if isinstance(scene, dict) and scene_id in {str(scene.get("scene_id") or "").strip(), str(scene.get("row_uid") or "").strip()}:
            return normalize(scene)
    return {"scene_id": scene_id}


def _project_id_from_steps(latest_by_step: Mapping[str, Any]) -> str:
    for artifact in latest_by_step.values():
        project_id = str(getattr(artifact, "project_id", "") or "").strip()
        if project_id:
            return project_id
    return ""


def draft_has_content(draft: Any) -> bool:
    """草稿里是否有任何非空内容——空骨架代表作者还没写，不该占提示上下文。"""
    return has_value(draft)

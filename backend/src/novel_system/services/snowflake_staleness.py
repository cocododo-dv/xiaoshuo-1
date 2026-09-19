"""依赖 / diff 感知的雪花失效判定（P0-3 · 收口-3）。

历史上 planner（``SnowflakeArtifact``）与 workspace（``SnowflakeStepRun``）各有一份
``_mark_downstream_stale``，把下标更大的步骤**无差别**全标 stale——改一次第 3 步，
第 4–9 步连同场景计划集体亮红，与「鼓励回修」直接对立。

本模块把两份全量循环收敛为**一个**纯函数判定：一步只在它**真正消费过的上游**发生
**它会读到的字段**改动时才变 stale。语义上对齐原型 ``ws-snow.jsx`` 的
``s2SnapAncestors`` —— 审批时拍下「我消费的上游长这样」的快照，之后按内容签名 + 字段
diff 比对。

设计要点：
- 快照粒度是「每个上游的逐字段签名」（``consumed_input_sigs_json``），既能做依赖边级
  判定（上游整体是否变），也能做字段级判定（变的字段本步是否读）。
- ``FIELDS_CONSUMED`` 是「哪步读上游哪些字段」的小表；没有条目的 (step, up) 退化为
  依赖边级精度——已远胜全量。正确性优先：宁可多列字段（偏多亮），也不漏标。
- 缺快照的老 run（``consumed_input_sigs_json`` 为空）：第一次回修时**保守按依赖边全标**
  （等价旧行为），下次 approve 补齐快照后转入精细模式。无破坏性迁移。

判定本身是无副作用的：调用方拿到 ``StaleHit`` 列表后，各自往自己的模型上落地
（workspace 还要写 ``stale_reason`` / ``RevisionLink`` / 场景计划；planner 只置 status）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

# 「哪步读上游哪些（顶层）字段」——每一步**直接展开**的上游（Ingermanson：一句扩一段、一段扩一页）。
#
# 2026-09-13 阶段 G（让回溯便宜）改了缺省语义：审批快照拍的是全部祖先，而旧表只列直接上游，
# 表里没有的 (step, up) 组合一律「上游任何改动都算」——于是改一句道德前提会让 06 到 10 全部需复核，
# 改读者定位的安全规则会让九步全失效，恰与「鼓励早回溯」相反。现在：
# - 本步在表里、上游也在本步的表里 → 字段级判定；
# - 本步在表里、上游不在 → 本步不消费它，**不失效**（上游变化经它真正的下游一层层再批准来传递）；
# - 本步不在表里（未知步骤）→ 退化为依赖边级，宁可多亮。
# 正确性优先：字段宁可多列（偏多亮），也不漏标。
FIELDS_CONSUMED: dict[str, dict[str, set[str]]] = {
    # 一句话是写给这类读者的营销句：类型 / 读者 / 故事种类 / 承诺变了它要重看；安全规则是起草侧的禁令，不是设计。
    "one_sentence_summary": {
        "book_brief": {"category", "target_reader", "story_kind", "genre_promise", "delight_reason", "expected_reader_emotion"},
    },
    "one_paragraph_summary": {"one_sentence_summary": {"summary"}},
    # 角色表读五句脊柱（三次灾难落在谁身上）；道德前提是故事的论证，改措辞不必重看角色表。
    "character_sheets": {"one_paragraph_summary": {"sentences"}},
    "short_synopsis": {"one_paragraph_summary": {"sentences"}},
    # 06 的「视角故事」是把一页梗概从这个角色的眼睛再讲一遍。
    "character_synopses": {"character_sheets": {"characters"}, "short_synopsis": {"paragraphs"}},
    "long_synopsis": {"short_synopsis": {"paragraphs"}},
    "character_bibles": {"character_sheets": {"characters"}, "character_synopses": {"characters"}},
    # 09 只消费 07 的五段展开（拆场素材）。章表**不是** 09 的输入：章是列完场之后的包装决定（阶段 K），
    # 07 的 chapters 现在由分章面板按场景列表回填（阶段 V 的镜像）——把它登记成 09 的上游就是一个环：
    # 确认一次分章、或只改一个章名，再确认 07，09 就被判「需复核」，而场景一个字都没变。
    # （阶段 D 曾把 chapters 登记进来，那时章表还在场景之前。）章表改动对已物化场景的影响另有其人：
    # project_runtime_invalidation 按章行定位到场，起草时 Scene Design Context 读的也是现行章表。
    "scene_list": {"long_synopsis": {"paragraphs"}},
    "scene_details": {"scene_list": {"scenes"}},
}

# 场景行的**内容**投影：作者在 09 / 10 真正编辑的键。服务端在建行时派生的键（title 缺省等于 summary、
# chapter_title 缺省等于章号、身份 / 状态 / 诊断）不算内容——否则生成器写的第一版与前端回传的版本
# 会因为这些派生键在每一行都不同。同义键归一（scene_type/primary_form、scene_crucible/crucible）。
_SCENE_ROW_CONTENT_KEYS: tuple[str, ...] = (
    "summary",
    "pov_character_id",
    "location",
    "chapter_role",
    "spine",
    # onstage_chars_json 故意不比：09 / 10 的表单没有这一栏，生成器写进草稿的名单不会回到计划行，
    # 一比每一行都「变了」。
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
    "rendering_mode",
    "expected_reader_emotion",
    "story_time",
    "exception_reason",
)


def changed_scene_row_uids(previous_payload: dict[str, Any] | None, current_payload: dict[str, Any] | None) -> set[str] | None:
    """09 重新批准时，按行身份（``row_uid``，旧数据退回 ``scene_id``）找出内容真的变了（或新加）的场。

    任一侧有行两种身份都缺 → 返回 ``None``，调用方退回「全部场景计划置 stale」。
    被删掉的场不在返回集合里——它们已经软删，没有计划行可标。
    """
    previous = _scene_rows_by_uid(previous_payload)
    current = _scene_rows_by_uid(current_payload)
    if previous is None or current is None:
        return None
    changed: set[str] = set()
    for row_uid, row in current.items():
        before = previous.get(row_uid)
        if before is None or scene_row_content(before) != scene_row_content(row):
            changed.add(row_uid)
    return changed


def _scene_rows_by_uid(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    scenes = semantic_payload(payload).get("scenes")
    if not isinstance(scenes, list):
        return None
    rows: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(scenes, start=1):
        if not isinstance(item, dict):
            return None
        key = str(item.get("row_uid") or "").strip() or str(item.get("scene_id") or "").strip()
        if not key:
            return None
        rows[key] = {**item, "_ordinal": index}
    return rows


def scene_row_content(row: dict[str, Any]) -> str:
    # 空值与缺席同义：生成器写的行没有 hook / beats 这些键，前端上行的行带着空串，
    # 不能因此把每一场都判成「改了」。
    content: dict[str, Any] = {}
    for key in _SCENE_ROW_CONTENT_KEYS:
        value = row.get(key)
        if value not in ("", None, [], {}):
            content[key] = value
    scene_type = str(row.get("scene_type") or row.get("primary_form") or "").strip().lower()
    if scene_type:
        content["scene_type"] = scene_type
    crucible = str(row.get("scene_crucible") or row.get("crucible") or "").strip()
    if crucible:
        content["crucible"] = crucible
    content["_ordinal"] = row.get("_ordinal")
    return stable_json(content)


class _StaleRow(Protocol):
    """planner 的 ``SnowflakeArtifact`` 与 workspace 的 ``SnowflakeStepRun`` 的公共形态。

    两者都暴露 ``step_key`` / ``status`` / ``artifact_json``（StepRun 上是返回
    ``draft_json`` 的 property）/ ``consumed_input_sigs_json``。
    """

    step_key: str
    status: str
    consumed_input_sigs_json: dict[str, Any] | None

    @property
    def artifact_json(self) -> dict[str, Any]: ...


@dataclass
class StaleHit:
    """一条「应置 stale」的判定结果。``row`` 是命中的下游行，调用方据此落地。"""

    row: Any
    step_key: str
    reason: str


def stable_json(value: Any) -> str:
    """与运行时失效分析器一致的稳定序列化（排序键、保留中文）。"""
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _sig(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()[:16]


def semantic_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """剥掉前端写穿缓存字段，只保留会改变故事含义的步骤内容。

    ``fe_text`` / ``fe_scaffold`` 是规范字段的前端镜像，``fe_state`` / ``fe_t`` /
    ``fe_meta`` 是本地状态与跨会话 UI 账本。它们需要随草稿保存，但不能制造故事版本、
    也不能触发下游雪花步骤失效。
    """
    data = payload if isinstance(payload, dict) else {}
    # 顶层的空值与「缺席」同义：阶段 J 给 01 加了可选的 narrative_stance，default_draft 会把 "" 合并进
    # 每一次 re-PATCH——若把「有这个键但为空」当成内容，升级前确认过的 01 会在第一次原样保存时被打回
    # 待审（阶段 C 的 rendering_mode=full 是同一类问题）。真正的清空（有值 → 空）仍然算改动：旧签名有键、
    # 新签名没有。
    result = {
        key: value
        for key, value in data.items()
        if not str(key).startswith("fe_") and not _is_empty_value(value)
    }
    scenes = result.get("scenes")
    if isinstance(scenes, list):
        # 阶段 C：rendering_mode 的默认值 full 与「缺席」同义——阶段 C 之前存的草稿没有这个键，
        # 前端水合后总会把 full 发回来；不剥掉默认值，已确认的场景规划会在升级当刻被打回待审。
        result["scenes"] = [_strip_packaging_keys(_strip_default_rendering_mode(item)) for item in scenes]
    return result


# 场景行上**不属于 09 / 10 故事内容**的键：由分章决定的章归属，以及计划行的服务端状态。工作台交给
# 前端的场景行是从计划行现算的，这些键跟着分章结果 / 批准状态变；前端的保真合并会把它们原样带回
# 上行的草稿——不剥掉的话，作者在分章面板里点一次确认（或只是确认了 09），已批准的 09 / 10 就会在下一次
# 自动保存时被判成「故事改了」、打回待重新确认，物化闸门随即拦下刚确认的分章（2026-09-18 真实故障：
# 第 10 步在十几分钟里被这样造出四个版本，差异只有行上的 status 与章字段）。章归属是分章那一步的决定，
# 场的先后由列表顺序本身表达，状态 / 失效留痕 / 诊断是服务端算出来的。
_SCENE_ROW_PACKAGING_KEYS: frozenset[str] = frozenset(
    {
        "scene_plan_id", "chapter_plan_id", "chapter_id", "chapter_title", "chapter_goal", "scene_seq",
        "status", "stale_reason", "stale_accepted_at", "stale_accepted_by", "stale_accepted_note", "diagnosis",
    }
)


def _strip_packaging_keys(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {key: value for key, value in item.items() if key not in _SCENE_ROW_PACKAGING_KEYS}


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def _strip_default_rendering_mode(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    mode = str(item.get("rendering_mode") or "").strip().lower()
    if "rendering_mode" in item and mode in {"", "full"}:
        return {key: value for key, value in item.items() if key != "rendering_mode"}
    return item


def field_sigs(payload: dict[str, Any] | None) -> dict[str, str]:
    """逐顶层故事字段的内容签名——支持字段级 diff，又不必存整份旧 payload。"""
    data = semantic_payload(payload)
    return {key: _sig(value) for key, value in data.items()}


def changed_fields(old_sigs: dict[str, str] | None, new_sigs: dict[str, str] | None) -> set[str]:
    """两份逐字段签名的差集（含新增 / 删除的字段）。"""
    old = old_sigs or {}
    new = new_sigs or {}
    return {key for key in (set(old) | set(new)) if old.get(key) != new.get(key)}


def snapshot_consumed_sigs(
    latest_by_step: dict[str, Any],
    consumed_step_keys: Iterable[str],
) -> dict[str, dict[str, str]]:
    """审批某步时，拍下它消费的每个上游的逐字段签名。

    ``consumed_step_keys`` 直接复用调用方已有的 ``_input_refs`` 依赖定义，确保「消费了谁」
    与现有依赖边完全一致。
    """
    snapshot: dict[str, dict[str, str]] = {}
    for key in consumed_step_keys:
        run = latest_by_step.get(key)
        if run is not None:
            snapshot[key] = field_sigs(run.artifact_json)
    return snapshot


def recompute_stale(
    *,
    changed_step_key: str,
    current_field_sigs: dict[str, str],
    candidate_rows: Iterable[_StaleRow],
    step_order: dict[str, int],
) -> list[StaleHit]:
    """给定刚被（重新）审批的步骤，算出真正受影响的下游步骤。

    单一判定取代两份全量循环。只标：在 ``changed_step_key`` **之后**、且本步**直接消费**该上游
    （``FIELDS_CONSUMED`` 有登记）、且其审批快照里被消费的字段确实变了的步骤。
    本步不消费该上游 → 不标（变化经真正的下游一层层再批准来传递）；本步不在表里 → 依赖边级；
    消费但缺快照 → 保守置 stale。
    """
    changed_index = step_order.get(changed_step_key)
    if changed_index is None:
        return []

    hits: list[StaleHit] = []
    for row in candidate_rows:
        row_index = step_order.get(row.step_key)
        if row_index is None or row_index <= changed_index:
            continue  # 只有严格下游才可能受影响

        table = FIELDS_CONSUMED.get(row.step_key)
        consumed = table.get(changed_step_key) if table is not None else None
        if table is not None and consumed is None:
            continue  # 本步不读这个上游：连 stale 都不必亮。

        snaps = row.consumed_input_sigs_json or {}
        snap_for_changed = snaps.get(changed_step_key)
        if snap_for_changed is None:
            # 缺快照（老数据 / 审批早于该上游）→ 保守按依赖边全标，等价旧行为。
            hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 已修订；缺输入快照，保守置 stale"))
            continue

        if snap_for_changed == current_field_sigs:
            continue  # 上游没动 → 跳过（回修不再被惩罚）

        changed = changed_fields(snap_for_changed, current_field_sigs)
        if consumed is None:
            # 未知步骤，无字段表 → 依赖边级：上游变了就标。
            hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 改了 {sorted(changed)}"))
        else:
            hit_fields = changed & consumed
            if hit_fields:
                hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 改了被消费字段 {sorted(hit_fields)}"))
            # 改的字段本步根本不读 → 连 stale 都不必亮。
    return hits

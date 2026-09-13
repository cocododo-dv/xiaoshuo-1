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

# 「哪步读上游哪些（顶层）字段」。键缺失即退化为依赖边级（上游任何改动都算）。
# 这里只登记「整字段单向消费」这类绝对安全的边，未覆盖的关系自动走更保守的边级判定。
FIELDS_CONSUMED: dict[str, dict[str, set[str]]] = {
    "one_paragraph_summary": {"one_sentence_summary": {"summary"}},
    "character_sheets": {"one_paragraph_summary": {"sentences"}},
    "short_synopsis": {"one_paragraph_summary": {"sentences"}},
    "character_synopses": {"character_sheets": {"characters"}},
    "long_synopsis": {"short_synopsis": {"paragraphs"}},
    "character_bibles": {"character_sheets": {"characters"}},
    # 阶段 D：07 的 paragraphs 是五段展开（拆场素材），chapters 是章表——以前章表镜像在 paragraphs 里，
    # 改章表自然让 09 失效；镜像去掉后把 chapters 明确登记进来，语义不变。
    "scene_list": {"long_synopsis": {"paragraphs", "chapters"}},
    "scene_details": {"scene_list": {"scenes"}},
}


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
    result = {key: value for key, value in data.items() if not str(key).startswith("fe_")}
    scenes = result.get("scenes")
    if isinstance(scenes, list):
        # 阶段 C：rendering_mode 的默认值 full 与「缺席」同义——阶段 C 之前存的草稿没有这个键，
        # 前端水合后总会把 full 发回来；不剥掉默认值，已确认的场景规划会在升级当刻被打回待审。
        result["scenes"] = [_strip_default_rendering_mode(item) for item in scenes]
    return result


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

    单一判定取代两份全量循环。只标：在 ``changed_step_key`` **之后**、且其审批快照里
    该上游的内容确实变了、且变的字段本步会读（或无字段表时退化为依赖边级）的步骤。
    缺快照的下游保守按依赖边全标。
    """
    changed_index = step_order.get(changed_step_key)
    if changed_index is None:
        return []

    hits: list[StaleHit] = []
    for row in candidate_rows:
        row_index = step_order.get(row.step_key)
        if row_index is None or row_index <= changed_index:
            continue  # 只有严格下游才可能受影响

        snaps = row.consumed_input_sigs_json or {}
        snap_for_changed = snaps.get(changed_step_key)
        if snap_for_changed is None:
            # 缺快照（老数据 / 审批早于该上游）→ 保守按依赖边全标，等价旧行为。
            hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 已修订；缺输入快照，保守置 stale"))
            continue

        if snap_for_changed == current_field_sigs:
            continue  # 上游没动 → 跳过（回修不再被惩罚）

        changed = changed_fields(snap_for_changed, current_field_sigs)
        consumed = FIELDS_CONSUMED.get(row.step_key, {}).get(changed_step_key)
        if consumed is None:
            # 无字段表 → 依赖边级：上游变了就标。
            hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 改了 {sorted(changed)}"))
        else:
            hit_fields = changed & consumed
            if hit_fields:
                hits.append(StaleHit(row=row, step_key=row.step_key, reason=f"{changed_step_key} 改了被消费字段 {sorted(hit_fields)}"))
            # 改的字段本步根本不读 → 连 stale 都不必亮。
    return hits

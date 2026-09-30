from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from sqlalchemy import func, select

from novel_system.db.models import LlmCall, SnowflakeScenePlan, StoryProject
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.llm_client import LLMConfigurationError, build_llm_request
from novel_system.services.llm_service_base import RuntimeLLMAccess, structured_prompt_hash
from novel_system.services.llm_accounting import (
    LLMCallContext,
    execute_accounted_call,
    mark_postprocess_failure,
)
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.prompt_builder import PromptConfigurationError
from novel_system.services.snowflake_prompt_budget import (
    AUTHOR_DIRECTION_BRIEF_KEY,
    STYLE_REFERENCE_STRUCTURE_KEY,
    apply_snowflake_prompt_budget,
    budget_audit_fields,
)
from novel_system.services.snowflake_steps import (
    diagnose_step_pressure,
    get_step_definition,
    merge_step_draft,
    step_guidance,
)
from novel_system.services.style_reference.planning_context import (
    STRUCTURE_REFERENCE_HOW_TO_USE,
    resolve_project_style_reference,
)

# 纯函数分在四个叶子模块里（B06-08）；这里原样转出——调用方与测试照旧从本模块 import。
from novel_system.services.snowflake_llm_sanitize import (  # noqa: F401
    SHORT_SYNOPSIS_PARAGRAPHS,
    SparseGenerationOutput,
    StructuredCountMismatch,
    _CHARACTER_COLLECTION_STEPS,
    _SCENE_CONTENT_KEYS,
    _SCENE_LIST_CONTENT_KEYS,
    _SCENE_REPAIR_PATCH_KEYS,
    _SERVER_ASSIGNED_ITEM_KEYS,
    _SPINE_MARKS,
    _apply_scene_list_spine,
    _assert_meaningful_generation_patch,
    _assert_scene_details_advanced,
    _collect_generation_gaps,
    _collection_id_key,
    _collection_template,
    _has_meaningful_character_sheet_content,
    _has_meaningful_non_identity_value,
    _merge_patch,
    _prune_empty_values,
    _sanitize_chapter_items,
    _sanitize_character_items,
    _sanitize_field_value,
    _sanitize_scene_detail_items,
    _sanitize_scene_list_items,
    _sanitize_scene_repair_patch,
    _sanitize_step_patch,
    _sanitize_template_value,
)
from novel_system.services.snowflake_llm_context import (  # noqa: F401
    CURRENT_DRAFT_HOW_TO_USE,
    SCENE_DETAIL_BATCH_SIZE,
    SCENE_DETAIL_MAX_BATCHES_PER_RUN,
    UPSTREAM_STEPS_HOW_TO_USE,
    GenerationFocus,
    _SCENE_NEIGHBOR_KEYS,
    _SCENE_REFERENCE_KEYS,
    _adopted_direction_payload,
    _compact_scene_context,
    _focus_scene_payload,
    _pressure_rubric,
    _project_id_from_steps,
    _project_prompt_payload,
    _sanitize_canonical_draft,
    _scene_detail_batches,
    _scene_ref,
    _scene_rules,
    _upstream_step_context,
    approved_context_from_steps,
    context_step_item,
    resolve_generation_focus,
    step_confirmed,
)
from novel_system.services.snowflake_llm_schema import (  # noqa: F401
    _editor_schema_properties,
    _is_open_object,
    _schema_from_template,
    enrich_structured_schema,
)
from novel_system.services.snowflake_llm_normalize import (  # noqa: F401
    _filter_output_to_focus,
    _normalize_assistant_output,
    _normalize_candidates_output,
    _normalize_full_step_output,
    _normalize_triage_output,
    _triage_skeleton_items,
)


@dataclass(slots=True)
class WorkspaceLLMResult:
    source: str
    llm_call_id: str | None
    payload: dict[str, Any]
    # 生成过程里作者必须知道、但不属于草稿内容的事实（分批深化的进度 / 中途失败）。
    # 落到 health_json.generation_notice，FE 据此把「已生成」提示降级为警告。
    notice: dict[str, Any] | None = None


# 2026-09-12 结构跟随：只有这两步在「排场」——参考作者的章 / 场尺度、开合方式、对白比重
# 才有用武之地；其余八步（定位 / 摘要 / 角色）看不到它。
_STRUCTURE_REFERENCE_STEPS = frozenset({"scene_list", "scene_details"})


class SnowflakeWorkspaceLLMService(RuntimeLLMAccess):
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        routing_config: Any | None = None,
        prompt_templates: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self._init_runtime_llm_access(
            llm_client=llm_client,
            routing_config=routing_config,
            prompt_templates=prompt_templates,
        )
        # 场景规划分批深化会多次调用 _generate_step_once：同一次请求内的绑定解析只做一遍。
        self._style_reference_cache: dict[str, dict[str, Any] | None] = {}

    def generate_step(
        self,
        *,
        project: StoryProject,
        step_key: str,
        latest_by_step: Mapping[str, Any],
        adopted_direction: str | None = None,
        focus_scene_refs: list[str] | None = None,
        focus_character_refs: list[str] | None = None,
        draft_override: dict[str, Any] | None = None,
        author_direction_brief: dict[str, Any] | None = None,
        direction_kind: str | None = None,
    ) -> WorkspaceLLMResult:
        """整步生成入口。场景规划的「整表生成 / 全部补全」在这里分批派发。

        ``author_direction_brief``（阶段 T）是工作台层解析好的作者意图要点（本步活动条目 + 继承的全书级
        条目 + how_to_use），原样进受保护键；``direction_kind`` 说明 ``adopted_direction`` 的来源
        （候选正文 / 教练回复），决定它的用法说明。"""
        if step_key == "scene_details" and not focus_scene_refs:
            current_draft = merge_step_draft(
                step_key,
                draft_override
                or (
                    latest_by_step.get(step_key).draft_json
                    if latest_by_step.get(step_key) is not None
                    else None
                ),
                latest_by_step=dict(latest_by_step),
            )
            # 没有场景可深化时立刻报错：场景规划的名册来自场景列表，模型无权凭空加场
            # （提示词里就写死了「服务端会丢弃草稿里没有的 scene_id」）。不拦就是稳赔一次
            # 调用换一份空草稿，还报「已生成」。
            if not (current_draft.get("scenes") or []):
                raise DomainError(
                    "SNOWFLAKE_SCENE_LIST_EMPTY",
                    "场景规划还没有可深化的场景：请先在「场景列表」里生成或手工列出场景，再回来补全每一场的规划。",
                    status_code=409,
                    details={
                        "node_id": "snowflake_step_generate",
                        "step_key": step_key,
                        "author_action": {
                            "kind": "navigate_step",
                            "step_key": "scene_list",
                            "label": "去场景列表",
                        },
                        "next_action": "generate_scene_list_first",
                    },
                )
            batches, pending = _scene_detail_batches(current_draft)
            if batches:
                return self._generate_scene_details_batched(
                    project=project,
                    latest_by_step=latest_by_step,
                    adopted_direction=adopted_direction,
                    base_draft=current_draft,
                    batches=batches,
                    pending=pending,
                    author_direction_brief=author_direction_brief,
                    direction_kind=direction_kind,
                )
        return self._generate_step_once(
            project=project,
            step_key=step_key,
            latest_by_step=latest_by_step,
            adopted_direction=adopted_direction,
            focus_scene_refs=focus_scene_refs,
            focus_character_refs=focus_character_refs,
            draft_override=draft_override,
            author_direction_brief=author_direction_brief,
            direction_kind=direction_kind,
        )

    def _generate_scene_details_batched(
        self,
        *,
        project: StoryProject,
        latest_by_step: Mapping[str, Any],
        adopted_direction: str | None,
        base_draft: dict[str, Any],
        batches: list[list[str]],
        pending: int,
        author_direction_brief: dict[str, Any] | None = None,
        direction_kind: str | None = None,
    ) -> WorkspaceLLMResult:
        """把整表深化拆成若干次定向生成，逐批把结果并回底稿。

        每批复用既有的单场定向通道，因此白拿三条既有防线：服务端焦点过滤
        （模型改写焦外场景一律丢弃）、按 scene_id 合并（其余场保持原样）、
        以及作用域收窄到本批的完备性修复重试。

        中途失败不回滚已完成的批次——那些场是真花了 token 深化出来的。但也绝不
        静默：失败与本次未覆盖的剩余场次都进 notice，由 health_json 交给作者。
        第一批就失败则直接抛出（什么都没完成，报错才是诚实的）。
        """
        accumulated = base_draft
        last_call_id: str | None = None
        completed = 0
        failure: DomainError | None = None
        budget_notice: dict[str, Any] | None = None
        for index, batch in enumerate(batches):
            try:
                result = self._generate_step_once(
                    project=project,
                    step_key="scene_details",
                    latest_by_step=latest_by_step,
                    adopted_direction=adopted_direction,
                    focus_scene_refs=batch,
                    focus_character_refs=None,
                    draft_override=accumulated,
                    author_direction_brief=author_direction_brief,
                    direction_kind=direction_kind,
                )
            except DomainError as exc:
                if index == 0:
                    raise
                failure = exc
                break
            accumulated = result.payload
            last_call_id = result.llm_call_id or last_call_id
            # 提示词预算警告对每一批都成立（同一份上游材料），留第一条即可
            budget_notice = budget_notice or result.notice
            completed += 1

        planned = sum(len(batch) for batch in batches)
        done = sum(len(batch) for batch in batches[:completed])
        # 本次没轮到的场 = 中断后剩下的 + 一开始就被单次上限挡在外面的
        left = planned - done + pending
        notice: dict[str, Any] | None = None
        if failure is not None:
            notice = {
                "code": "SCENE_DETAILS_PARTIAL",
                "message": (
                    f"分批深化在第 {completed + 1}/{len(batches)} 批中断：{failure.message} "
                    f"本次已深化 {done} 场，还剩 {left} 场——再次点击生成会从第一场未完成的场继续。"
                ),
                "error_code": failure.code,
            }
        elif pending:
            notice = {
                "code": "SCENE_DETAILS_MORE_TO_GO",
                "message": (
                    f"本次已深化 {done} 场（单次上限 {SCENE_DETAIL_MAX_BATCHES_PER_RUN} 批），"
                    f"还剩 {left} 场——再次点击生成继续深化剩余场景。"
                ),
            }
        if notice is not None:
            notice.update(
                {
                    "severity": "warning",
                    "batches_total": len(batches),
                    "batches_completed": completed,
                    "scenes_deepened": done,
                    "scenes_remaining": left,
                }
            )
            # 进度类提示优先（作者当下要做的决定是「还要不要再点一次」），
            # 但预算超限这件事不能因此丢掉——并进同一条提示。
            if budget_notice is not None:
                notice["message"] = f"{notice['message']} 另：{budget_notice['message']}"
                notice["prompt_budget"] = {
                    key: budget_notice.get(key)
                    for key in ("budget_tokens", "estimated_before", "estimated_after", "applied")
                }
        return WorkspaceLLMResult(
            source="llm",
            llm_call_id=last_call_id,
            payload=accumulated,
            notice=notice if notice is not None else budget_notice,
        )

    def _generate_step_once(
        self,
        *,
        project: StoryProject,
        step_key: str,
        latest_by_step: Mapping[str, Any],
        adopted_direction: str | None = None,
        focus_scene_refs: list[str] | None = None,
        focus_character_refs: list[str] | None = None,
        draft_override: dict[str, Any] | None = None,
        author_direction_brief: dict[str, Any] | None = None,
        direction_kind: str | None = None,
    ) -> WorkspaceLLMResult:
        """一次整步（或定向）生成：解析焦点 → 组提示载荷 → 按契约重试（B06-08 拆成三段）。"""
        # draft_override：FE 与上行 PATCH 同源的本地最新规范草稿（service 层已并入
        # 存档、剥 fe_*）——消除「刚编辑还没自动保存上行，模型看不到」的竞态。
        current_source = (
            draft_override
            if draft_override
            else (latest_by_step.get(step_key).draft_json if latest_by_step.get(step_key) is not None else None)
        )
        current_draft = merge_step_draft(
            step_key,
            current_source,
            latest_by_step=dict(latest_by_step),
        )
        focus = resolve_generation_focus(
            step_key,
            current_draft,
            latest_by_step,
            focus_scene_refs=focus_scene_refs,
            focus_character_refs=focus_character_refs,
        )
        prompt_payload = self._build_generation_payload(
            project=project,
            step_key=step_key,
            latest_by_step=latest_by_step,
            current_draft=current_draft,
            focus=focus,
            adopted_direction=adopted_direction,
            direction_kind=direction_kind,
            author_direction_brief=author_direction_brief,
        )
        # 集合步（角色×3 / 场景规划）的合并底稿一律用「当前最新草稿」（剥 fe_* 写穿键）：
        # 默认底稿是空骨架 / 从 scene_list 重新播种的骨架，模型没回传的成员会被整体
        # 丢掉——作者手工加的角色、焦点外场景的既有深化都要在这里幸存。
        # scene_list 的「AI 生成整表」保持替换语义：重排整表时不让旧场景残留混排。
        # long_synopsis 也要拿真底稿，但目的不同：章表**仍然整表替换**（模型重排/增删章是
        # 合法的重生成），只是清洗器要能看见既有章的 row_uid 才能按章序把身份传下去。
        # 底稿给空骨架 = 每次生成都判成全新的章 → 全书场景归属整片解绑（见 #1）。
        keep_members = (
            step_key in _CHARACTER_COLLECTION_STEPS
            or step_key in {"scene_details", "long_synopsis"}
        )
        merge_base = (
            {key: value for key, value in current_draft.items() if not str(key).startswith("fe_")}
            if keep_members
            else None
        )
        # 焦点定向的服务端硬约束：不管模型是否守约，输出里焦点外的成员一律丢弃，
        # 合并时保持原样——「其余成员不动」不能只靠提示词。
        focus_filter = focus.output_filter()
        run_kwargs: dict[str, Any] = dict(
            task_key="snowflake_step_generate",
            template_name=f"snowflake_generate_{step_key}",
            project_id=project.project_id,
            step_ref=step_key,
            schema_step_key=step_key,
            normalize_output=lambda output: _normalize_full_step_output(
                step_key,
                output,
                latest_by_step=dict(latest_by_step),
                project_id=project.project_id,
                base_override=merge_base,
                focus_filter=focus_filter,
            ),
        )
        return self._run_with_contract_retries(
            step_key,
            prompt_payload,
            run_kwargs,
            current_draft=current_draft,
            focus=focus,
        )

    def _build_generation_payload(
        self,
        *,
        project: StoryProject,
        step_key: str,
        latest_by_step: Mapping[str, Any],
        current_draft: dict[str, Any],
        focus: GenerationFocus,
        adopted_direction: str | None,
        direction_kind: str | None,
        author_direction_brief: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """整步生成的提示载荷（上游各步、本步底稿、压力量表、焦点、方向、作者意图要点、参考书结构画像）。"""
        step_definition = get_step_definition(step_key)
        compact_scenes = bool(focus.scenes) and step_key == "scene_details"
        upstream_steps = _upstream_step_context(latest_by_step, step_key=step_key)
        if compact_scenes:
            # 上游的场景列表是同一份场表的另一副本，整表随每批重发就是按批数翻倍。
            # 焦点场保留全量（pov/章内职能只在这里有），其余压成参照条目。
            upstream_steps = [
                dict(item, draft=_compact_scene_context(item["draft"], focus.scene_allowed))
                if item.get("step_key") == "scene_list"
                else item
                for item in upstream_steps
            ]
        guidance = step_guidance(step_key)
        prompt_payload = {
            "project": _project_prompt_payload(project),
            "step_key": step_key,
            "step_label": step_definition.get("label"),
            "step_english_label": step_definition.get("english_label"),
            "step_description": step_definition.get("description"),
            "step_instruction": guidance.get("instruction"),
            "step_guidance": guidance,
            "step_editor": step_definition.get("editor") or {},
            "upstream_steps": upstream_steps,
            "upstream_steps_how_to_use": UPSTREAM_STEPS_HOW_TO_USE,
            # 定向深化时焦外场景压成参照条目：全表明细 × 每批一次 = 输入成本按批数翻倍，
            # 而模型对焦外场只需要「它是什么、接在哪」。紧邻前后场保留衔接字段。
            "current_draft": (
                _compact_scene_context(current_draft, focus.scene_allowed)
                if compact_scenes
                else _sanitize_canonical_draft(current_draft)
            ),
            "current_draft_how_to_use": CURRENT_DRAFT_HOW_TO_USE,
            "pressure_rubric": _pressure_rubric(step_key),
            "current_pressure_diagnosis": diagnose_step_pressure(step_key, current_draft),
            "scene_rules": _scene_rules(step_key),
        }
        # 2026-09-12 结构跟随：场景清单 / 场景规划看参考作者的结构画像与场景手法
        # （project + global 绑定）。超预算时由 snowflake_prompt_budget 先卸样例、再卸整张画像。
        if step_key in _STRUCTURE_REFERENCE_STEPS:
            style_reference = self._project_style_reference(project.project_id)
            if style_reference:
                prompt_payload[STYLE_REFERENCE_STRUCTURE_KEY] = style_reference
        if adopted_direction:
            prompt_payload["adopted_direction"] = _adopted_direction_payload(
                adopted_direction,
                kind=direction_kind,
                focused=bool(focus.scenes or focus.characters),
            )
        # 2026-09-16 阶段 T：作者意图要点——教练对话蒸馏、作者核过的决定 / 否决 / 约束 / 待定
        # （加上游各步的全书级条目）。受保护键：降载阶梯永不削它。
        if author_direction_brief:
            prompt_payload[AUTHOR_DIRECTION_BRIEF_KEY] = author_direction_brief
        if focus.scenes:
            prompt_payload["focus_scenes"] = {
                "scenes": normalize(focus.scenes),
                "how_to_use": (
                    "本次只深化 focus_scenes 里列出的场景：输出的 scenes 数组只包含这些场景，"
                    "scene_id 必须原样回传（服务端按 scene_id 合并，其余场景保持不动）；"
                    "结合前后场景的挫败/决定衔接（见 current_draft），不要改动场景的类型与顺序。"
                ),
            }
        if focus.characters:
            prompt_payload["focus_characters"] = {
                "characters": normalize(focus.characters),
                "how_to_use": (
                    "本次只深化 focus_characters 里列出的角色：输出的 characters 数组只包含这些角色，"
                    "character_id 必须原样回传（服务端按 character_id 合并，其余角色保持不动）。"
                    "current_draft 里焦点外的角色是已确立的既成事实：只作为一致性参照"
                    "（人物关系、立场、时间线必须与他们吻合），不要改写他们，也不要在输出里复述他们。"
                ),
            }
        return prompt_payload

    def _run_with_contract_retries(
        self,
        step_key: str,
        prompt_payload: dict[str, Any],
        run_kwargs: dict[str, Any],
        *,
        current_draft: dict[str, Any],
        focus: GenerationFocus,
    ) -> WorkspaceLLMResult:
        """跑一次生成，契约没守住时带着理由各再给模型一次机会：数量契约 / 空成员（拒绝即重试，再错如实报错）、
        完备性（首版有空字段 → 带空字段清单重试，只有更完整才采用）。"""
        try:
            result = self._run_structured_task(prompt_payload=prompt_payload, **run_kwargs)
        except DomainError as exc:
            details = getattr(exc, "details", None) or {}
            if not (details.get("count_mismatch") or details.get("sparse_output")):
                raise
            # 数量契约（五句 / 五段）被违反，或集合步只回了空成员：带着拒绝理由再给模型一次机会；
            # 再错就如实报错，绝不静默截断 / 落一版空表。completeness_repair 是受预算保护的键，
            # 降载时不会被削掉。
            if details.get("count_mismatch"):
                follow_up = "Return exactly the required number of items, in order, and nothing else."
            else:
                follow_up = (
                    "The blank slots in current_draft are what this step must write, not the author's deliberate "
                    "blanks; expand the adopted direction and upstream material into full sheets — at minimum the "
                    "protagonist and the antagonist carry role, goal, ambition, values, conflict and storyline. "
                    "Use only the canonical keys named in the task; an item that is an empty object is discarded."
                )
            repair_payload = dict(prompt_payload)
            repair_payload["completeness_repair"] = {
                "empty_fields": [],
                "instruction": f"The previous attempt was rejected: {exc.message} {follow_up}",
            }
            result = self._run_structured_task(prompt_payload=repair_payload, **run_kwargs)
        if result.source != "llm":
            return result
        _assert_scene_details_advanced(
            step_key,
            base=current_draft,
            merged=result.payload,
            targeted_ids=set(focus.scene_ids) if focus.scene_ids else None,
        )

        # 完备性修复重试（残缺兜底）：模型输出经清洗（丢契约外键）后仍有空字段时，
        # 带着空字段清单再给模型一次机会；只有重试确实更完整才采用，失败保留首版。
        # 单场定向时只盯焦点场景的缺口——焦外场景本来就没让模型动。
        def collect_gaps(payload: dict[str, Any]) -> list[str]:
            return focus.relevant_gaps(_collect_generation_gaps(step_key, payload))

        gaps = collect_gaps(result.payload)
        if not gaps:
            return result
        repair_payload = dict(prompt_payload)
        repair_payload["completeness_repair"] = {
            "empty_fields": gaps[:40],
            "instruction": (
                "The previous attempt left every field listed in empty_fields blank after server-side "
                "sanitization (unknown keys are discarded — use only the canonical keys named in the task). "
                "Regenerate the complete step and make sure each listed field carries substantive, "
                "story-specific content; empty strings and placeholders are defects. An entry that names a whole "
                "character (characters[name]) means that character carries nothing but a name: give it at least "
                "its role. Blanks that are not listed may stay blank — minor characters may stay sparse."
            ),
        }
        try:
            retry = self._run_structured_task(prompt_payload=repair_payload, **run_kwargs)
        except DomainError:
            return result
        if retry.source == "llm" and len(collect_gaps(retry.payload)) < len(gaps):
            return retry
        return result

    # 「先看 3 个方向」（阶段 U 起是教练日志里的一种回合）——提示词由模板组装。
    # 上下文以后端权威材料为主（各步规范草稿 + 当前步压力诊断），draft_override 与整步生成同源。
    # fail-closed：LLM 未启用即 409。
    def step_candidates(
        self,
        *,
        project: StoryProject,
        step_key: str,
        target_chars: int,
        latest_by_step: Mapping[str, Any] | None = None,
        draft_override: dict[str, Any] | None = None,
        author_ask: str | None = None,
        focus_scene_id: str | None = None,
        author_direction_brief: dict[str, Any] | None = None,
    ) -> WorkspaceLLMResult:
        step_definition = get_step_definition(step_key)
        guidance = step_guidance(step_key)
        prompt_payload: dict[str, Any] = {
            "project": _project_prompt_payload(project),
            "step_key": step_key,
            "step_label": step_definition.get("label"),
            "step_english_label": step_definition.get("english_label"),
            "step_description": step_definition.get("description"),
            "step_instruction": guidance.get("instruction"),
            "target_chars": target_chars,
        }
        # 阶段 U：作者对这一组方向的要求（教练输入框里的那句话）——三条方向都要满足它，在它划定的范围内分岔
        if str(author_ask or "").strip():
            prompt_payload["author_ask"] = {
                "text": str(author_ask).strip(),
                "how_to_use": (
                    "这是作者对这一组方向的要求：三条方向都必须满足它，在它划定的范围内分岔；"
                    "它与 author_direction_brief 冲突时以它为准（它更新）。"
                ),
            }
        if latest_by_step is not None:
            latest = latest_by_step.get(step_key)
            current_source = (
                draft_override
                if draft_override
                else (latest.draft_json if latest is not None else None)
            )
            current_canonical = merge_step_draft(step_key, current_source, latest_by_step=dict(latest_by_step))
            prompt_payload.update(
                {
                    "upstream_steps": _upstream_step_context(latest_by_step, step_key=step_key),
                    "upstream_steps_how_to_use": UPSTREAM_STEPS_HOW_TO_USE,
                    "current_canonical_draft": _sanitize_canonical_draft(current_canonical),
                    "pressure_rubric": _pressure_rubric(step_key),
                    "current_pressure_diagnosis": diagnose_step_pressure(step_key, current_canonical),
                }
            )
            # 第 10 步：方向只针对选中的那一场（与教练的聚焦场同一口径，row_uid / scene_id 皆可指）
            if focus_scene_id and step_key == "scene_details":
                prompt_payload["focus_scene_id"] = focus_scene_id
                prompt_payload["focus_scene"] = _focus_scene_payload({"draft": current_canonical}, focus_scene_id)
        # 阶段 T：三条方向在作者要的范围内分岔，而不是在作者否决过的方向上抽卡
        if author_direction_brief:
            prompt_payload[AUTHOR_DIRECTION_BRIEF_KEY] = author_direction_brief
        return self._run_structured_task(
            task_key="snowflake_step_candidates",
            template_name="snowflake_step_candidates",
            project_id=project.project_id,
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=_normalize_candidates_output,
        )

    def assistant_reply(
        self,
        *,
        project: dict[str, Any],
        step: dict[str, Any],
        message: str,
        approved_context: list[dict[str, Any]],
        latest_by_step: Mapping[str, Any],
        conversation: dict[str, Any] | None = None,
        focus_scene_id: str | None = None,
    ) -> WorkspaceLLMResult:
        """驻场教练。2026-09-16 阶段 T 起 **fail-closed**：LLM 未启用即 409（与整步生成同一条路），
        不再回规则罐头——罐头回合既不是辅导，也不能进作者意图要点。``conversation`` 是工作台层
        组好的既有对话（当前要点 + 作者撤下的条目 + 继承的全书级要点 + 本步最近几轮）。"""
        step_key = str(step.get("step_key") or "book_brief").strip() or "book_brief"
        guidance_payload = step.get("guidance") if isinstance(step.get("guidance"), dict) else {}
        prompt_payload = {
            "project": project,
            "step_key": step_key,
            "step_label": step.get("label"),
            "step_english_label": step.get("english_label"),
            "step_description": step.get("description"),
            "step_instruction": guidance_payload.get("instruction"),
            "step_guidance": guidance_payload,
            "step_editor": step.get("editor") or {},
            "draft": _sanitize_canonical_draft(step.get("draft") if isinstance(step.get("draft"), dict) else {}),
            "message": str(message or "").strip(),
            "approved_context": approved_context,
            "focus_scene_id": focus_scene_id or "",
            "focus_scene": _focus_scene_payload(step, focus_scene_id),
            "pressure_rubric": _pressure_rubric(step_key),
            "current_pressure_diagnosis": diagnose_step_pressure(step_key, step.get("draft") if isinstance(step.get("draft"), dict) else {}),
            "scene_rules": _scene_rules(step_key),
        }
        if conversation:
            prompt_payload["conversation"] = conversation
        return self._run_structured_task(
            task_key="snowflake_workspace_assistant",
            template_name="snowflake_workspace_assistant",
            schema_step_key=step_key,
            project_id=str(project.get("project_id") or ""),
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_assistant_output(
                step_key,
                output,
                latest_by_step=dict(latest_by_step),
                base_draft=step.get("draft") if isinstance(step.get("draft"), dict) else {},
                project_id=str(project.get("project_id") or ""),
            ),
        )

    def scene_triage_suggestions(
        self,
        *,
        project: dict[str, Any],
        step: dict[str, Any],
        approved_context: list[dict[str, Any]],
        author_direction_brief: dict[str, Any] | None = None,
    ) -> WorkspaceLLMResult:
        step_key = "scene_details"
        draft = step.get("draft") if isinstance(step.get("draft"), dict) else {}
        guidance_payload = step.get("guidance") if isinstance(step.get("guidance"), dict) else {}
        prompt_payload = {
            "project": project,
            "step_key": step_key,
            "step_label": step.get("label"),
            "step_english_label": step.get("english_label"),
            "step_instruction": guidance_payload.get("instruction"),
            "draft": _sanitize_canonical_draft(draft),
            "approved_context": approved_context,
            "pressure_rubric": _pressure_rubric(step_key),
            "current_pressure_diagnosis": diagnose_step_pressure(step_key, draft),
            # 阶段 Q：按原著分诊的口径——Yes 的两条、No 的四条、Maybe 的七步救治（与 snowflake_scene_triage_suggest v3 同一句话）。
            "triage_rules": {
                "pass": (
                    "Yes: the plan is a miniature story on its own (a POV character inside a scene crucible, with a goal/conflict/setback "
                    "or a reaction/dilemma/decision that would give the reader one powerful emotional experience) AND the scene crucible "
                    "can be named. A summary or skipped rendering, follow-up beats, a mixed victory, an empty cost, and a scene that carries "
                    "an author's exception_reason that holds are all still Yes."
                ),
                "maybe": (
                    "Maybe: the elements are present but weak, generic, or under-specified in a way a targeted patch fixes; or the "
                    "exception_reason does not justify the missing beats. fix_steps follow the method's rescue: confirm the form, write the "
                    "weak beat and the crucible, decide summary / skip / full for a reactive scene, state the reader emotion, rewrite the plan."
                ),
                "rewrite": (
                    "No: the scene no longer fits the big story, cannot deliver an emotional experience and never will, has no crucible and "
                    "none can be welded on, or is not a story and cannot become one (it only sets the stage, explains, or shows motivation). "
                    "Rebuild it from its plan; only the author may mark it cut."
                ),
            },
            "scene_rules": _scene_rules(step_key),
        }
        if author_direction_brief:
            prompt_payload[AUTHOR_DIRECTION_BRIEF_KEY] = author_direction_brief
        return self._run_structured_task(
            task_key="snowflake_scene_triage",
            template_name="snowflake_scene_triage_suggest",
            schema_step_key="scene_details",
            project_id=str(project.get("project_id") or ""),
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_triage_output(output, draft),
        )

    def chapter_plan_suggestions(
        self,
        *,
        project: dict[str, Any],
        chapters: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
        current_assignment: list[dict[str, Any]],
    ) -> WorkspaceLLMResult:
        """分章建议（P3，顾问通道）。

        **不给 fallback_payload —— 这个端点 fail-closed。** 作者点的是「让 AI 建议分章」，
        LLM 没配好时返回一份规则算出来的东西并称之为建议，就是撒谎；而规则分章本来就
        以 `spine_anchor` 策略明明白白摆在面板上，作者随时能用，不需要伪装成 AI 建议。
        """
        prompt_payload = {
            "project": project,
            "chapters": chapters,
            "scenes": scenes,
            "current_assignment": current_assignment,
        }
        allowed_scene_ids = {str(item.get("scene_plan_id") or "") for item in scenes}
        allowed_chapter_uids = {str(item.get("row_uid") or "") for item in chapters}
        return self._run_structured_task(
            task_key="snowflake_chapter_plan",
            template_name="snowflake_chapter_plan_suggest",
            project_id=str(project.get("project_id") or ""),
            step_ref="long_synopsis",
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_chapter_plan_output(
                output, allowed_scene_ids, allowed_chapter_uids
            ),
        )

    def chapter_title_suggestions(
        self,
        *,
        project: dict[str, Any],
        book: dict[str, Any],
        chapters: list[dict[str, Any]],
        named_chapters: list[dict[str, Any]],
        reference_titles: dict[str, Any] | None = None,
    ) -> WorkspaceLLMResult:
        """AI 起章名（阶段 W，顾问通道）：给 ``chapters`` 里每一章一个章名和一句章摘要。

        2026-09-22 结构跟随参考书：``reference_titles``（参考作家的题名样例与形态）进载荷；样例本身
        当作已占用的名字，模型照抄一条就当重复丢掉。

        和分章建议一样 **fail-closed**（不给 ``fallback_payload``）：作者点的是「AI 起章名」，模型没配好
        就如实报 409，不拿规则拼出来的名字冒充。走 ``snowflake_chapter_plan`` 节点的路由——同一个面板、
        同一类顾问调用；另立节点的话，已保存模型快照的安装还得先点一次「一键补齐」才用得上。
        """
        prompt_payload = {
            "project": project,
            "book": book,
            "named_chapters": named_chapters,
            "chapters": chapters,
        }
        if reference_titles:
            prompt_payload["reference_titles"] = dict(reference_titles)
        allowed_row_uids = {str(item.get("row_uid") or "") for item in chapters}
        taken_titles = {str(item.get("title") or "").strip() for item in named_chapters}
        taken_titles |= {
            str(item or "").strip()
            for item in ((reference_titles or {}).get("samples") or [])
            if str(item or "").strip()
        }
        return self._run_structured_task(
            task_key="snowflake_chapter_plan",
            template_name="snowflake_chapter_titles_suggest",
            project_id=str(project.get("project_id") or ""),
            step_ref="long_synopsis",
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_chapter_titles_output(output, allowed_row_uids, taken_titles),
        )

    def _run_structured_task(
        self,
        *,
        task_key: str,
        template_name: str,
        project_id: str,
        step_ref: str,
        prompt_payload: dict[str, Any],
        normalize_output: Callable[[dict[str, Any]], dict[str, Any]],
        schema_step_key: str | None = None,
    ) -> WorkspaceLLMResult:
        """``schema_step_key``：按这一步的编辑器模板把模板 structured_schema 里没有 properties 的
        成员对象补全（见 ``enrich_structured_schema``）——下发给 provider 的 json_schema 与提示词
        点名的规范键一致，按 schema 约束解码的后端才写得出内容。"""
        if not self._llm_enabled():
            # 所有雪花 LLM 节点 fail-closed（作者 2026-09-15「没有模型就不兜底」）：整步生成、方向、教练、
            # AI 分诊（B06-20）、分章建议与起章名都不拿规则结果冒充 AI 产出，引导作者去配置。
            raise DomainError(
                "SNOWFLAKE_LLM_NOT_CONFIGURED",
                "雪花工作台的 AI 生成需要先启用真实模型。请到系统配置里配置 provider 与密钥并测试通过后重试。",
                status_code=409,
                details={
                    "node_id": task_key,
                    "template_name": template_name,
                    "author_action": llm_setup_action(
                        llm_enabled=False,
                        generation_mode="offline_disabled",
                    ),
                },
            )

        try:
            task_config = self._task_config(task_key)
            template = self._template(template_name)
        except (KeyError, LLMConfigurationError, PromptConfigurationError) as exc:
            missing_route = isinstance(exc, KeyError)
            if missing_route:
                message = (
                    f"模型已接入，但 LLM 节点路由未配置：{task_key}。"
                    "请到配置环境点击“一键补齐”，或在节点路由中为该节点绑定 provider/model 后重试。"
                )
                next_action = "sync_missing_llm_node_routes"
            else:
                message = (
                    f"snowflake LLM prompt or route is not ready: {task_key}。"
                    "请检查节点路由、提示词模板和模型配置后重试。"
                )
                next_action = "configure_snowflake_node_route_and_prompt_then_retry"
            raise DomainError(
                "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING",
                message,
                status_code=409,
                details={
                    "node_id": task_key,
                    "template_name": template_name,
                    "error_code": getattr(exc, "code", exc.__class__.__name__),
                    "reason": "missing_node_route" if missing_route else "route_or_prompt_invalid",
                    "next_action": next_action,
                },
            ) from exc

        # 输入预算在这里统一施加：所有雪花 LLM 节点共用这一条渲染路径，
        # 载荷（上游十步 + 全表底稿）会随作品体量无界增长，不设闸就是把
        # 「一次点击」变成几十万 token，甚至撑爆模型上下文窗口。
        prompt_payload, budget_report = apply_snowflake_prompt_budget(
            prompt_payload,
            budget_tokens=self._input_token_budget(template),
            step_key=step_ref,
        )
        user_prompt = _render_user_prompt(template, prompt_payload)
        structured_schema, schema_enriched = enrich_structured_schema(
            template.structured_schema,
            step_key=schema_step_key,
            template_name=template_name,
        )
        prompt_hash = structured_prompt_hash(template_name, template.version, template.system_prompt, user_prompt, structured_schema)
        llm_call_id = f"llm_call_project_{task_key}_{uuid.uuid4().hex[:12]}"
        request = build_llm_request(
            task_config,
            node_id=task_key,
            messages=[
                {"role": "system", "content": template.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_schema={"name": template.name, "schema": structured_schema},
        )
        request_summary = sanitize_audit_summary(
            {
                "task_key": task_key,
                "template_name": template.name,
                "template_version": template.version,
                "step_key": step_ref,
                # 哪些成员对象的 schema 是按编辑器模板补全的——审计里留痕，
                # 「模型为什么只回了空对象」才查得出是 schema 的锅还是模型的锅。
                "response_schema_enriched": schema_enriched,
                # 降载过的提示词必须在审计里留痕：否则「模型怎么把这个角色写丢了」
                # 将永远查不出是预算削的还是模型的锅。摊平是为了在摘要超限压缩时存活。
                **budget_audit_fields(budget_report),
                **normalize(prompt_payload),
            }
        )
        try:
            response = execute_accounted_call(
                self.session,
                self._client(),
                request,
                LLMCallContext(
                    scope_type="project",
                    scope_id=project_id,
                    project_id=project_id,
                    node_id=task_key,
                    step=step_ref,
                ),
                llm_call_id=llm_call_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._supplement_accounted_call(
                llm_call_id=llm_call_id,
                request_summary=request_summary,
                prompt_hash=prompt_hash,
                response_summary=error_audit_summary(exc),
            )
            raise DomainError(
                "SNOWFLAKE_LLM_CALL_FAILED",
                _llm_failure_message(exc, task_key),
                status_code=409,
                details={
                    "llm_call_id": llm_call_id,
                    "node_id": task_key,
                    "error_code": getattr(exc, "code", exc.__class__.__name__),
                    "next_action": "check_provider_route_model_and_retry",
                    "response_summary": error_audit_summary(exc),
                },
            ) from exc

        try:
            raw_output = response.structured_output or {}
            if not isinstance(raw_output, dict):
                raise ValueError("structured output must be an object")
            normalized_output = normalize_output(raw_output)
        except Exception as exc:  # noqa: BLE001
            mark_postprocess_failure(
                self.session,
                llm_call_id,
                error_code="LLM_RESPONSE_INVALID_SCHEMA",
                error_text=str(exc),
            )
            self._supplement_accounted_call(
                llm_call_id=llm_call_id,
                request_summary=request_summary,
                prompt_hash=prompt_hash,
                response_summary={
                    "message": str(exc),
                    "structured_output": response.structured_output,
                    "request_id": response.request_id,
                },
            )
            count_mismatch = isinstance(exc, StructuredCountMismatch)
            sparse_output = isinstance(exc, SparseGenerationOutput)
            if count_mismatch:
                next_action = "regenerate_with_exact_count"
            elif sparse_output:
                next_action = "regenerate_with_substantive_content"
            else:
                next_action = "retry_or_adjust_prompt_schema"
            raise DomainError(
                "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA",
                str(exc),
                status_code=409,
                details={
                    "llm_call_id": llm_call_id,
                    "node_id": task_key,
                    "error_code": "LLM_RESPONSE_INVALID_SCHEMA",
                    "next_action": next_action,
                    "count_mismatch": count_mismatch,
                    "sparse_output": sparse_output,
                    "structured_output": response.structured_output,
                },
            ) from exc

        self._supplement_accounted_call(
            llm_call_id=llm_call_id,
            request_summary=request_summary,
            prompt_hash=prompt_hash,
            response_summary={
                "request_id": response.request_id,
                "response_format": response.response_format,
                "structured_output": response.structured_output,
            },
        )
        return WorkspaceLLMResult(
            source="llm",
            llm_call_id=response.llm_call_id or llm_call_id,
            payload=normalized_output,
            notice=_budget_notice(budget_report),
        )

    def _project_style_reference(self, project_id: str) -> dict[str, Any] | None:
        """参考作者结构画像的载荷成员（缓存于本次请求）；无绑定 / 旧画像 / 解析失败 → None。"""
        if project_id not in self._style_reference_cache:
            # 这份载荷只进雪花步骤生成（09 / 10 的场景表）：按 snowflake_step_generate 的实际路由判云策略（H1）
            reference = resolve_project_style_reference(
                self.session, project_id, node_ids=("snowflake_step_generate",)
            )
            member: dict[str, Any] | None = None
            if reference:
                member = {
                    "profile_id": reference["profile_id"],
                    "how_to_use": STRUCTURE_REFERENCE_HOW_TO_USE,
                }
                for key in ("structure_card", "structure_samples", "planning_guidance"):
                    if reference.get(key):
                        member[key] = reference[key]
                # 2026-09-22 结构跟随参考书:按参考章长与本书每章场数推算的每场字数(本书已分章时)
                scale = self._project_reference_scale(project_id, reference.get("card"))
                if scale:
                    member["project_scale"] = scale
            self._style_reference_cache[project_id] = member
        cached = self._style_reference_cache[project_id]
        return dict(cached) if cached else None

    def _project_reference_scale(self, project_id: str, card: Any) -> dict[str, Any] | None:
        """本书每章几场 × 参考作者章长 → 每场约几字(``structure.reference_scene_scale``)。

        场数取当前分章里各章场数的中位(已分章的活跃场景计划);还没分章 → 只给参考章长,
        由模型把每章场数与场长一起定。任何异常 → None(可选增强)。
        """
        try:
            from novel_system.services.style_reference.structure import reference_scene_scale

            if not isinstance(card, dict) or int(card.get("chapter_count") or 0) <= 1:
                return None
            rows = self.session.execute(
                select(SnowflakeScenePlan.chapter_plan_id, func.count())
                .where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.removed_at.is_(None),
                    SnowflakeScenePlan.chapter_plan_id.is_not(None),
                )
                .group_by(SnowflakeScenePlan.chapter_plan_id)
            ).all()
            counts = sorted(int(count or 0) for _chapter, count in rows if int(count or 0) > 0)
            chapter_chars = card.get("chapter_chars") if isinstance(card.get("chapter_chars"), dict) else {}
            if not counts:
                return {
                    "scenes_per_chapter": None,
                    "derived_scene_chars": None,
                    "chapter_chars_median": int(chapter_chars.get("median") or 0) or None,
                    "note": "本书还没有分章：先按参考章长定每章场数，再让每场字数 × 每章场数落在参考章长附近。",
                }
            scenes_per_chapter = counts[len(counts) // 2]
            scale = reference_scene_scale(card, scenes_in_chapter=scenes_per_chapter)
            if not scale:
                return None
            return {
                "scenes_per_chapter": scenes_per_chapter,
                "derived_scene_chars": scale["derived_scene_chars"],
                "chapter_chars_median": scale["chapter_chars"]["median"],
                "basis": scale["basis"],
                "note": (
                    f"本书当前每章约 {scenes_per_chapter} 场；参考作者单章中位 {scale['chapter_chars']['median']} 字，"
                    f"推算每场约 {scale['derived_scene_chars']} 字——target_length_band 取它附近的数字区间。"
                ),
            }
        except Exception:  # noqa: BLE001 — 可选增强
            return None

    def _input_token_budget(self, template: Any) -> int:
        """本次渲染的输入预算：环境变量优先（小上下文的本地模型要能收紧），
        否则用模板声明值；两者皆无 → 0 = 不设预算。"""
        override = int(getattr(self._settings_payload(), "snowflake_input_token_budget", 0) or 0)
        if override > 0:
            return override
        return int(getattr(template, "input_token_budget", 0) or 0)

    def llm_enabled(self) -> bool:
        """公开可用性探针：FE 的「采纳并结构化」用它决定报错而不是落 fallback 版本。"""
        return self._llm_enabled()

    def _supplement_accounted_call(
        self,
        *,
        llm_call_id: str,
        request_summary: dict[str, Any],
        prompt_hash: str,
        response_summary: dict[str, Any],
    ) -> None:
        parent = self.session.get(LlmCall, llm_call_id)
        if parent is None:
            raise RuntimeError(f"accounted snowflake call {llm_call_id} is missing")
        parent.prompt_hash = prompt_hash
        parent.request_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.request_payload_summary or {}),
                **request_summary,
            }
        )
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                **response_summary,
            }
        )
        self.session.commit()


def _budget_notice(report: dict[str, Any]) -> dict[str, Any] | None:
    """降载阶梯跑完仍超预算 → 作者可见的警告。

    只在**超出**时提示。正常范围内的降载是设计动作（本来就只发本次要用的材料），
    每次都弹提示只会训练作者忽略它；真正需要作者知道的是「这本书的上游材料已经
    超出提示词预算，模型这次看到的是删减版」。
    """
    if report.get("within_budget", True):
        return None
    return {
        "code": "PROMPT_BUDGET_EXCEEDED",
        "severity": "warning",
        "message": (
            f"上游材料已超出提示词输入预算（约 {report.get('estimated_after')} / "
            f"{report.get('budget_tokens')} token）：本次已按无关材料优先的顺序删减"
            f"（{'、'.join(report.get('applied') or []) or '无可删减项'}），模型看到的是删减版。"
            "可精简上游步骤，或调高该节点的输入预算。"
        ),
        **{key: report.get(key) for key in ("budget_tokens", "estimated_before", "estimated_after", "applied")},
    }


def _render_user_prompt(template: Any, prompt_payload: dict[str, Any]) -> str:
    required = template.structured_schema.get("required") or []
    required_text = ", ".join(str(item) for item in required if isinstance(item, str))
    # 紧凑 JSON：缩进对模型没有价值，却给这份嵌套载荷凭空加了约六成体积——
    # 场景规划分批后同一份上下文要重发 N 次，这笔浪费按批数翻倍。
    prompt_json = json.dumps(normalize(prompt_payload), ensure_ascii=False, separators=(",", ":"))
    return (
        f"{template.task_prompt.strip()}\n\n"
        f"Working payload:\n{prompt_json}\n\n"
        f"Required top-level JSON keys: {required_text or 'follow the provided schema'}.\n"
        "Return only valid JSON. Do not wrap it in markdown fences."
    )


def _normalize_chapter_plan_output(
    output: dict[str, Any],
    allowed_scene_ids: set[str],
    allowed_chapter_uids: set[str],
) -> dict[str, Any]:
    """把模型给的分章建议约束回一份可安全展示的提案。

    服务端硬约束（模型违约时是过滤掉，不是报错——建议本来就允许不完美，但绝不能
    因为它编了一个不存在的 id 就把作者的场丢掉或绑到不存在的章上）：
    - 只保留 id 在白名单里的条目；
    - 一个场只认第一次出现（重复分配会让场在两章里各出现一次）；
    - 没被提到的场不在这里补——调用方按「缺哪些」如实展示。
    """
    raw = output.get("assignments") if isinstance(output, dict) else None
    assignments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        scene_plan_id = str(item.get("scene_plan_id") or "").strip()
        chapter_row_uid = str(item.get("chapter_row_uid") or "").strip()
        if scene_plan_id not in allowed_scene_ids or chapter_row_uid not in allowed_chapter_uids:
            continue
        if scene_plan_id in seen:
            continue
        seen.add(scene_plan_id)
        assignments.append({"scene_plan_id": scene_plan_id, "chapter_row_uid": chapter_row_uid})
    rationale = str((output or {}).get("rationale") or "").strip()[:600]
    return {
        "assignments": assignments,
        "rationale": rationale,
        "missing_scene_plan_ids": sorted(allowed_scene_ids - seen),
    }


#: 章名的硬上限（字符）。提示词要的是 2–10 个字；超过这个数的不是章名，是一句话。
CHAPTER_TITLE_MAX_CHARS = 24
CHAPTER_SUMMARY_MAX_CHARS = 120
_TITLE_WRAPPERS = "《》〈〉「」『』“”\"'‘’【】[]（）()"
_TITLE_NUMBER_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千零〇两]+\s*[章回节幕卷]|chapter\s*[0-9ivxlc]+)\s*[:：·\-—.、\s]*",
    re.IGNORECASE,
)
_GENERIC_TITLES = frozenset({"序幕", "开端", "发展", "高潮", "转折", "结局", "风波", "真相", "危机", "尾声", "开始", "结束"})


def clean_chapter_title(value: Any) -> str:
    """模型给的章名 → 可以直接落进章表的章名；不合格返回空串（那一章就留着等作者起名）。

    去掉包裹的引号 / 书名号、模型自己加的「第三章：」前缀（章号由系统编）、句末标点；
    空的、超长的（一句话不是章名）、光秃秃的结构标签（高潮 / 结局…）一律不要。
    """
    title = str(value or "").strip()
    title = _TITLE_NUMBER_PREFIX.sub("", title).strip()
    while title and title[0] in _TITLE_WRAPPERS:
        title = title[1:].strip()
    while title and title[-1] in _TITLE_WRAPPERS + "。．.！!？?，,；;：:、":
        title = title[:-1].strip()
    if not title or len(title) > CHAPTER_TITLE_MAX_CHARS or title in _GENERIC_TITLES:
        return ""
    return title


def _normalize_chapter_titles_output(
    output: dict[str, Any],
    allowed_row_uids: set[str],
    taken_titles: set[str],
) -> dict[str, Any]:
    """把模型起的章名约束回一份可以安全展示的提案（违约的条目过滤掉，不报错——建议允许不完美）。

    - 只认白名单里的 ``row_uid``，一章只认第一次出现；
    - 章名过 ``clean_chapter_title``；与已有章名、与本批前面的章名重复的不要（全书章名互不相同）；
    - 章摘要截到上限；章名不合格时整条不要（只有摘要的条目没有意义）。
    """
    raw = output.get("titles") if isinstance(output, dict) else None
    titles: list[dict[str, str]] = []
    seen_uids: set[str] = set()
    used = {title for title in taken_titles if title}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        row_uid = str(item.get("row_uid") or "").strip()
        if row_uid not in allowed_row_uids or row_uid in seen_uids:
            continue
        title = clean_chapter_title(item.get("title"))
        if not title or title in used:
            continue
        seen_uids.add(row_uid)
        used.add(title)
        summary = " ".join(str(item.get("summary") or "").split())[:CHAPTER_SUMMARY_MAX_CHARS]
        titles.append({"row_uid": row_uid, "title": title, "summary": summary})
    return {"titles": titles, "missing_row_uids": sorted(allowed_row_uids - seen_uids)}


def _llm_failure_message(exc: Exception, task_key: str) -> str:
    details = getattr(exc, "details", None)
    details = details if isinstance(details, dict) else {}
    if details.get("next_action") == "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider":
        provider_id = str(details.get("provider_id") or "current provider")
        model = str(details.get("model") or "current model")
        endpoint = str(details.get("endpoint") or "/responses")
        return (
            f"LLM 请求失败：节点 {task_key} 正在用 {provider_id}/{model} 调 Responses API（{endpoint}），"
            "但服务返回 404。这个中转服务大概率只支持 Chat Completions；"
            "请到配置环境把该提供方“调用协议”切换为 chat，保存后点击“一键补齐”，"
            "或改用支持 Responses API 的提供方。"
        )
    if getattr(exc, "code", "") == "LLM_RESPONSE_TRUNCATED":
        # 抬预算已在客户端自动试过（最高 8192）还是装不下，只能由作者缩小这一次的范围。
        return (
            f"LLM 输出被长度上限截断：节点 {task_key} 这一步要生成的内容超出了模型单次输出上限，"
            "自动提高输出预算后仍然装不下。请分批生成——例如先用「AI 补全这一场 / 这个角色」"
            "逐个深化，或先减少本步的成员数量；也可以到配置环境把该节点的输出预算调得更高。"
        )
    return str(exc)



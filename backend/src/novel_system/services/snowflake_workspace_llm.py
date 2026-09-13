from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, StoryProject
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json, normalize
from novel_system.services.llm_client import (
    LLMClient,
    LLMConfigurationError,
    build_llm_request,
    load_model_routing_config,
    resolve_node_route,
)
from novel_system.services.llm_accounting import (
    LLMCallContext,
    execute_accounted_call,
    mark_postprocess_failure,
)
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.prompt_builder import PromptConfigurationError, load_prompt_templates
from novel_system.services.snowflake_prompt_budget import (
    STYLE_REFERENCE_STRUCTURE_KEY,
    apply_snowflake_prompt_budget,
    budget_audit_fields,
)
from novel_system.services.snowflake_steps import (
    SCENE_FIELD_EXAMPLES,
    STEP_ORDER,
    diagnose_scene_detail,
    diagnose_step_pressure,
    get_step_definition,
    list_step_definitions,
    merge_step_draft,
    step_completeness,
    step_guidance,
)
from novel_system.services.style_reference.planning_context import (
    STRUCTURE_REFERENCE_HOW_TO_USE,
    resolve_project_style_reference,
)
from novel_system.services.system_config import load_llm_provider_runtime_configs
from novel_system.settings import get_settings


class StructuredCountMismatch(ValueError):
    """模型违反了数量契约（一段话概括恰好五句、一页梗概恰好五段）。

    阶段 B（2026-09-13 雪花评估）：旧归一化把多出来的句 / 段静默截掉——一页梗概的第 6–9 段在编辑器
    与长篇大纲的上游上下文里无声消失。现在整步生成拒绝这份输出并带着理由重试一次，教练补丁则
    丢弃该键；绝不静默截断。
    """


# 一页梗概 = 五句各扩一段（Ingermanson 第 4 步）。前端 05 也只有五个槽。
SHORT_SYNOPSIS_PARAGRAPHS = 5


@dataclass(slots=True)
class WorkspaceLLMResult:
    source: str
    llm_call_id: str | None
    payload: dict[str, Any]
    # 生成过程里作者必须知道、但不属于草稿内容的事实（分批深化的进度 / 中途失败）。
    # 落到 health_json.generation_notice，FE 据此把「已生成」提示降级为警告。
    notice: dict[str, Any] | None = None


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
# 2026-09-12 结构跟随：只有这两步在「排场」——参考作者的章 / 场尺度、开合方式、对白比重
# 才有用武之地；其余八步（定位 / 摘要 / 角色）看不到它。
_STRUCTURE_REFERENCE_STEPS = frozenset({"scene_list", "scene_details"})


class SnowflakeWorkspaceLLMService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        routing_config: Any | None = None,
        prompt_templates: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self._llm_client = llm_client
        self._routing_config = routing_config
        self._prompt_templates = prompt_templates
        self._provider_configs: dict[str, Any] | None = None
        self._settings = None
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
    ) -> WorkspaceLLMResult:
        """整步生成入口。场景规划的「整表生成 / 全部补全」在这里分批派发。"""
        if step_key == "scene_details" and not focus_scene_refs:
            current_draft = merge_step_draft(
                step_key,
                draft_override
                or (
                    latest_by_step.get(step_key).artifact_json
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
                )
        return self._generate_step_once(
            project=project,
            step_key=step_key,
            latest_by_step=latest_by_step,
            adopted_direction=adopted_direction,
            focus_scene_refs=focus_scene_refs,
            focus_character_refs=focus_character_refs,
            draft_override=draft_override,
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
    ) -> WorkspaceLLMResult:
        step_definition = get_step_definition(step_key)
        # draft_override：FE 与上行 PATCH 同源的本地最新规范草稿（service 层已并入
        # 存档、剥 fe_*）——消除「刚编辑还没自动保存上行，模型看不到」的竞态。
        current_source = (
            draft_override
            if draft_override
            else (latest_by_step.get(step_key).artifact_json if latest_by_step.get(step_key) is not None else None)
        )
        current_draft = merge_step_draft(
            step_key,
            current_source,
            latest_by_step=dict(latest_by_step),
        )
        # 单场定向：refs 可以是 row_uid 或 scene_id；解析失败要报错而不是悄悄全量重写
        focus_scene_ids: list[str] = []
        focus_scenes: list[dict[str, Any]] = []
        focus_scene_allowed: set[str] = set()
        if focus_scene_refs:
            refs = set(focus_scene_refs)
            for scene in current_draft.get("scenes") or []:
                if not isinstance(scene, dict):
                    continue
                if {str(scene.get("row_uid") or ""), str(scene.get("scene_id") or "")} & refs:
                    focus_scenes.append(scene)
                    # 身份口径必须与 _scene_detail_batches 一致：分批按 scene_id or row_uid
                    # 指场，这里若只认 scene_id，缺编号的场就会让空转防线与修复重试
                    # 双双对着空串比对，等于失效。
                    focus_scene_ids.append(_scene_ref(scene))
                    focus_scene_allowed |= {str(scene.get("scene_id") or ""), str(scene.get("row_uid") or "")}
            focus_scene_allowed = {ref for ref in (focus_scene_allowed | refs) if ref}
            if not focus_scenes:
                raise DomainError(
                    "SNOWFLAKE_FOCUS_SCENE_NOT_FOUND",
                    "指定的场景不在当前场景规划草稿里（可能刚被删除或还未保存）——刷新后重试。",
                    status_code=409,
                    details={"focus_scene_refs": focus_scene_refs},
                )
        # 单角色定向（04/06/08 三个角色集合步）：refs 可以是 character_id 或姓名。
        # 06/08 的成员可能还没在本步草稿里立档——用 04 角色摘要表的名册兜底解析，
        # 解析出的种子成员（id/姓名/定位）进焦点上下文，生成后按 character_id 合并。
        focus_character_ids: list[str] = []
        focus_characters: list[dict[str, Any]] = []
        focus_character_allowed: set[str] = set()
        if focus_character_refs:
            refs = {str(ref or "").strip() for ref in focus_character_refs if str(ref or "").strip()}
            matched_refs: set[str] = set()

            def _match_member(member: dict[str, Any]) -> set[str]:
                return {str(member.get("character_id") or ""), str(member.get("display_name") or "").strip()} & refs

            for member in current_draft.get("characters") or []:
                if not isinstance(member, dict):
                    continue
                hit = _match_member(member)
                if hit:
                    focus_characters.append(member)
                    focus_character_ids.append(str(member.get("character_id") or ""))
                    focus_character_allowed |= {str(member.get("character_id") or ""), str(member.get("display_name") or "").strip()}
                    matched_refs |= hit
            if refs - matched_refs and step_key != "character_sheets":
                roster_artifact = latest_by_step.get("character_sheets")
                roster = (getattr(roster_artifact, "artifact_json", None) or {}).get("characters") if roster_artifact is not None else None
                for member in roster or []:
                    if not isinstance(member, dict):
                        continue
                    hit = _match_member(member) - matched_refs
                    if hit:
                        seed = {
                            "character_id": str(member.get("character_id") or ""),
                            "display_name": str(member.get("display_name") or "").strip(),
                            "role": str(member.get("role") or "").strip(),
                        }
                        focus_characters.append(seed)
                        focus_character_ids.append(seed["character_id"])
                        focus_character_allowed |= {seed["character_id"], seed["display_name"]}
                        matched_refs |= hit
            focus_character_allowed = {ref for ref in (focus_character_allowed | refs) if ref}
            if not focus_characters:
                raise DomainError(
                    "SNOWFLAKE_FOCUS_CHARACTER_NOT_FOUND",
                    "指定的角色不在当前名册里（可能刚被删除或还未保存）——刷新后重试。",
                    status_code=409,
                    details={"focus_character_refs": focus_character_refs},
                )
        upstream_steps = _upstream_step_context(latest_by_step, step_key=step_key)
        if focus_scenes and step_key == "scene_details":
            # 上游的场景列表是同一份场表的另一副本，整表随每批重发就是按批数翻倍。
            # 焦点场保留全量（pov/章内职能只在这里有），其余压成参照条目。
            upstream_steps = [
                dict(item, draft=_compact_scene_context(item["draft"], focus_scene_allowed))
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
                _compact_scene_context(current_draft, focus_scene_allowed)
                if (focus_scenes and step_key == "scene_details")
                else _sanitize_canonical_draft(current_draft)
            ),
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
            prompt_payload["adopted_direction"] = {
                "text": adopted_direction,
                "how_to_use": (
                    # 定向采纳（候选 × 焦点成员）与整步采纳的蓝本用法不同：前者只落到焦点成员
                    "作者已选定这段文字作为定向蓝本：只把它落实到 focus 指定的成员上，"
                    "保留它的核心意象、人物立场与转折；焦点外的成员一律不动、不复述。"
                    if (focus_scenes or focus_characters)
                    else "作者已选定这段文字作为本步的方向蓝本：以它为基调把本步全部字段结构化展开，"
                    "保留它的核心意象、人物立场与转折，不要另起新方向；它没有覆盖到的字段按上游材料补全。"
                ),
            }
        if focus_scenes:
            prompt_payload["focus_scenes"] = {
                "scenes": normalize(focus_scenes),
                "how_to_use": (
                    "本次只深化 focus_scenes 里列出的场景：输出的 scenes 数组只包含这些场景，"
                    "scene_id 必须原样回传（服务端按 scene_id 合并，其余场景保持不动）；"
                    "结合前后场景的挫败/决定衔接（见 current_draft），不要改动场景的类型与顺序。"
                ),
            }
        if focus_characters:
            prompt_payload["focus_characters"] = {
                "characters": normalize(focus_characters),
                "how_to_use": (
                    "本次只深化 focus_characters 里列出的角色：输出的 characters 数组只包含这些角色，"
                    "character_id 必须原样回传（服务端按 character_id 合并，其余角色保持不动）。"
                    "current_draft 里焦点外的角色是已确立的既成事实：只作为一致性参照"
                    "（人物关系、立场、时间线必须与他们吻合），不要改写他们，也不要在输出里复述他们。"
                ),
            }
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
        focus_filter: dict[str, Any] | None = None
        if focus_scenes:
            focus_filter = {"field": "scenes", "id_keys": ("scene_id", "row_uid"), "allowed": focus_scene_allowed}
        elif focus_characters:
            focus_filter = {"field": "characters", "id_keys": ("character_id", "display_name", "name"), "allowed": focus_character_allowed}
        run_kwargs: dict[str, Any] = dict(
            task_key="snowflake_step_generate",
            template_name=f"snowflake_generate_{step_key}",
            project_id=project.project_id,
            step_ref=step_key,
            normalize_output=lambda output: _normalize_full_step_output(
                step_key,
                output,
                latest_by_step=dict(latest_by_step),
                project_id=project.project_id,
                base_override=merge_base,
                focus_filter=focus_filter,
            ),
        )
        try:
            result = self._run_structured_task(prompt_payload=prompt_payload, **run_kwargs)
        except DomainError as exc:
            if not (getattr(exc, "details", None) or {}).get("count_mismatch"):
                raise
            # 数量契约（五句 / 五段）被违反：带着拒绝理由再给模型一次机会；再错就如实报错，
            # 绝不静默截断。completeness_repair 是受预算保护的键，降载时不会被削掉。
            repair_payload = dict(prompt_payload)
            repair_payload["completeness_repair"] = {
                "empty_fields": [],
                "instruction": (
                    f"The previous attempt was rejected: {exc.message} "
                    "Return exactly the required number of items, in order, and nothing else."
                ),
            }
            result = self._run_structured_task(prompt_payload=repair_payload, **run_kwargs)
        if result.source != "llm":
            return result
        _assert_scene_details_advanced(
            step_key,
            base=current_draft,
            merged=result.payload,
            targeted_ids=set(focus_scene_ids) if focus_scene_ids else None,
        )

        # 完备性修复重试（残缺兜底）：模型输出经清洗（丢契约外键）后仍有空字段时，
        # 带着空字段清单再给模型一次机会；只有重试确实更完整才采用，失败保留首版。
        # 单场定向时只盯焦点场景的缺口——焦外场景本来就没让模型动。
        def collect_gaps(payload: dict[str, Any]) -> list[str]:
            gaps = _collect_generation_gaps(step_key, payload)
            if focus_scene_ids:
                focus_set = set(focus_scene_ids)
                gaps = [gap for gap in gaps if gap.split(".", 1)[0] in focus_set]
            elif focus_characters:
                # 角色缺口标签形如 characters[姓名或id].field——只盯焦点角色的缺口
                allowed_labels = {f"characters[{ref}]" for ref in focus_character_allowed}
                gaps = [gap for gap in gaps if gap.split(".", 1)[0] in allowed_labels]
            return gaps

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
                "story-specific content; empty strings and placeholders are defects."
            ),
        }
        try:
            retry = self._run_structured_task(prompt_payload=repair_payload, **run_kwargs)
        except DomainError:
            return result
        if retry.source == "llm" and len(collect_gaps(retry.payload)) < len(gaps):
            return retry
        return result

    # FE-ALIGN G5：构思视图的「生成 3 条候选」——提示词由模板组装。
    # 上下文以后端权威材料为主（approved 各步规范草稿 + 当前步压力诊断），
    # 前端折叠文本降级为「本地未上行编辑」的补充信号。
    def step_candidates(
        self,
        *,
        project: StoryProject,
        step_key: str,
        context_text: str,
        current_draft: str,
        target_chars: int,
        latest_by_step: Mapping[str, Any] | None = None,
    ) -> WorkspaceLLMResult:
        step_definition = get_step_definition(step_key)
        guidance = step_guidance(step_key)
        prompt_payload = {
            "project": _project_prompt_payload(project),
            "step_key": step_key,
            "step_label": step_definition.get("label"),
            "step_english_label": step_definition.get("english_label"),
            "step_description": step_definition.get("description"),
            "step_instruction": guidance.get("instruction"),
            "fe_local_context": context_text,
            "current_draft_text": current_draft,
            "target_chars": target_chars,
        }
        if latest_by_step is not None:
            current_canonical = merge_step_draft(
                step_key,
                latest_by_step.get(step_key).artifact_json if latest_by_step.get(step_key) is not None else None,
                latest_by_step=dict(latest_by_step),
            )
            prompt_payload.update(
                {
                    "upstream_steps": _upstream_step_context(latest_by_step, step_key=step_key),
                    "upstream_steps_how_to_use": UPSTREAM_STEPS_HOW_TO_USE,
                    "current_canonical_draft": _sanitize_canonical_draft(current_canonical),
                    "pressure_rubric": _pressure_rubric(step_key),
                    "current_pressure_diagnosis": diagnose_step_pressure(step_key, current_canonical),
                }
            )
        return self._run_structured_task(
            task_key="snowflake_step_candidates",
            template_name="snowflake_step_candidates",
            project_id=project.project_id,
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=_normalize_candidates_output,
            fallback_payload={"candidates": []},
        )

    def assistant_reply(
        self,
        *,
        project: dict[str, Any],
        step: dict[str, Any],
        message: str,
        approved_context: list[dict[str, Any]],
        latest_by_step: Mapping[str, Any],
        fallback_factory: Callable[[], dict[str, Any]],
        focus_scene_id: str | None = None,
    ) -> WorkspaceLLMResult:
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
        return self._run_structured_task(
            task_key="snowflake_workspace_assistant",
            template_name="snowflake_workspace_assistant",
            project_id=str(project.get("project_id") or ""),
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_assistant_output(
                step_key,
                output,
                latest_by_step=dict(latest_by_step),
                base_draft=step.get("draft") if isinstance(step.get("draft"), dict) else {},
            ),
            fallback_payload=fallback_factory(),
        )

    def scene_triage_suggestions(
        self,
        *,
        project: dict[str, Any],
        step: dict[str, Any],
        approved_context: list[dict[str, Any]],
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
            "triage_rules": {
                "pass": "The scene has enough pressure and the required trio is present.",
                "maybe": "The scene is salvageable but its pressure, clarity, or required trio is incomplete.",
                "rewrite": "The scene core is too hollow and should be reworked from premise level.",
            },
            "scene_rules": _scene_rules(step_key),
        }
        return self._run_structured_task(
            task_key="snowflake_scene_triage",
            template_name="snowflake_scene_triage_suggest",
            project_id=str(project.get("project_id") or ""),
            step_ref=step_key,
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_triage_output(output, draft),
            fallback_payload={"items": _fallback_triage_items(draft)},
        )

    def chapter_plan_suggestions(
        self,
        *,
        project: dict[str, Any],
        chapters: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
        current_assignment: list[dict[str, Any]],
        approved_context: list[dict[str, Any]],
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
            "approved_context": approved_context,
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

    def _run_structured_task(
        self,
        *,
        task_key: str,
        template_name: str,
        project_id: str,
        step_ref: str,
        prompt_payload: dict[str, Any],
        normalize_output: Callable[[dict[str, Any]], dict[str, Any]],
        fallback_payload: dict[str, Any] | None = None,
    ) -> WorkspaceLLMResult:
        if not self._llm_enabled():
            # 主生成路径（generate_step）fail-closed：不再静默返回罐头稿，引导作者去配置。
            # 顾问型端点（候选建议/驻场教练/场景急救）传入 fallback_payload → 诚实规则回退
            # （source="fallback"，绝非伪造生成）。
            if fallback_payload is not None:
                return WorkspaceLLMResult(
                    source="fallback", llm_call_id=None, payload=normalize(fallback_payload)
                )
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
        prompt_hash = _prompt_hash(template_name, template.version, template.system_prompt, user_prompt, template.structured_schema)
        llm_call_id = f"llm_call_project_{task_key}_{uuid.uuid4().hex[:12]}"
        request = build_llm_request(
            task_config,
            node_id=task_key,
            messages=[
                {"role": "system", "content": template.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_schema={"name": template.name, "schema": template.structured_schema},
        )
        request_summary = sanitize_audit_summary(
            {
                "task_key": task_key,
                "template_name": template.name,
                "template_version": template.version,
                "step_key": step_ref,
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
            raise DomainError(
                "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA",
                str(exc),
                status_code=409,
                details={
                    "llm_call_id": llm_call_id,
                    "node_id": task_key,
                    "error_code": "LLM_RESPONSE_INVALID_SCHEMA",
                    "next_action": "regenerate_with_exact_count" if count_mismatch else "retry_or_adjust_prompt_schema",
                    "count_mismatch": count_mismatch,
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
            reference = resolve_project_style_reference(self.session, project_id)
            member: dict[str, Any] | None = None
            if reference:
                member = {
                    "profile_id": reference["profile_id"],
                    "how_to_use": STRUCTURE_REFERENCE_HOW_TO_USE,
                }
                for key in ("structure_card", "structure_samples", "planning_guidance"):
                    if reference.get(key):
                        member[key] = reference[key]
            self._style_reference_cache[project_id] = member
        cached = self._style_reference_cache[project_id]
        return dict(cached) if cached else None

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

    def _llm_enabled(self) -> bool:
        return bool(self._settings_payload().llm_enabled)

    def _settings_payload(self):
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def _client(self) -> Any:
        if self._llm_client is not None:
            return self._llm_client
        settings = self._settings_payload()
        return LLMClient(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            provider_configs=self._runtime_provider_configs(),
        )

    def _runtime_provider_configs(self) -> dict[str, Any]:
        if self._provider_configs is None:
            self._provider_configs = load_llm_provider_runtime_configs()
        return self._provider_configs

    def _routing(self) -> Any:
        if self._routing_config is None:
            self._routing_config = load_model_routing_config()
        return self._routing_config

    def _task_config(self, task_key: str) -> Any:
        return resolve_node_route(self._routing(), task_key)

    def _template(self, template_name: str) -> Any:
        if self._prompt_templates is None:
            self._prompt_templates = load_prompt_templates()
        return self._prompt_templates[template_name]

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


CONFIRMED_STEP_STATUSES = {"approved", "skipped"}

# 上游上下文的用法说明：状态是「这份材料有多稳」的提示，不是「要不要遵守」的开关。
UPSTREAM_STEPS_HOW_TO_USE = (
    "这是本步之前每一步的规范草稿，按雪花顺序排列，是本作品故事事实的唯一来源："
    "人物姓名与 id、地点、时间线、已埋的冲突与灾难链，全部以它们为准，不得另起炉灶。"
    "confirmed=false 表示作者还没确认这一步（草稿仍可能变动），"
    "但它依然是作者当前认定的故事，必须照样遵守——不要因为未确认就忽略它、也不要去改写它。"
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
    作品《何有》的场景列表主角叫「林一鸣」，与前八步的「何有」毫无关系，就是这么来的。

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
            merge_step_draft(key, getattr(artifact, "artifact_json", None), latest_by_step=dict(latest_by_step))
        )
        # 空骨架（还没写的步骤）不占提示预算，也别让模型误以为作者已经交代过什么。
        if not _has_value(draft):
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
        compacted.append({key: scene[key] for key in keys if key in scene and _has_value(scene[key])})
    payload["scenes"] = compacted
    return payload


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


def _prompt_hash(
    template_name: str,
    template_version: str,
    system_prompt: str,
    user_prompt: str,
    structured_schema: dict[str, Any],
) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        canonical_json(
            {
                "template_name": template_name,
                "template_version": template_version,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "structured_schema": structured_schema,
            }
        ),
    ).hex


def _normalize_candidates_output(output: dict[str, Any]) -> dict[str, Any]:
    """FE-ALIGN G5：候选数组裁剪到原型契约形状（≤4 条；label/tag/notes 限长）。"""
    items = output.get("candidates") if isinstance(output, dict) else None
    normalized: list[dict[str, Any]] = []
    for item in (items or [])[:4]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        notes = item.get("notes") if isinstance(item.get("notes"), list) else []
        normalized.append(
            {
                "label": str(item.get("label") or f"方向 {len(normalized) + 1}").strip()[:8],
                "tag": str(item.get("tag") or "AI 候选").strip()[:16],
                "text": text,
                "notes": [str(n).strip()[:10] for n in notes[:3] if str(n).strip()],
            }
        )
    return {"candidates": normalized}


def _normalize_full_step_output(
    step_key: str,
    output: dict[str, Any],
    *,
    latest_by_step: dict[str, Any],
    project_id: str,
    base_override: dict[str, Any] | None = None,
    focus_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if focus_filter and isinstance(output, dict):
        output = _filter_output_to_focus(output, focus_filter)
    base = base_override if base_override is not None else merge_step_draft(step_key, None, latest_by_step=latest_by_step)
    patch = _sanitize_step_patch(step_key, output, latest_by_step=latest_by_step, project_id=project_id, base=base)
    _assert_meaningful_generation_patch(step_key, patch)
    return _merge_patch(base, patch)


def _filter_output_to_focus(output: dict[str, Any], focus_filter: dict[str, Any]) -> dict[str, Any]:
    """焦点定向的服务端硬约束：清洗前先把模型输出过滤到焦点成员——模型即使
    违约复述/改写了焦点外成员，也不让它进合并；全部被过滤掉则明确报错，
    绝不悄悄把定向请求变成全量重写。"""
    field_key = str(focus_filter.get("field") or "")
    id_keys = tuple(focus_filter.get("id_keys") or ())
    allowed = focus_filter.get("allowed") or set()
    members = output.get(field_key)
    if not isinstance(members, list):
        return output
    kept = [
        item
        for item in members
        if isinstance(item, dict) and ({str(item.get(key) or "").strip() for key in id_keys} & allowed)
    ]
    if members and not kept:
        raise ValueError(
            f"focused generation returned no {field_key} matching the requested focus ids; "
            "the model must echo the focus member ids verbatim — retry the request."
        )
    filtered = dict(output)
    filtered[field_key] = kept
    return filtered


def _normalize_assistant_output(
    step_key: str,
    output: dict[str, Any],
    *,
    latest_by_step: dict[str, Any],
    base_draft: dict[str, Any],
) -> dict[str, Any]:
    reply = str(output.get("reply") or "").strip()
    suggestions = _coerce_string_list(output.get("suggestions"))
    candidate_label = str(output.get("candidate_label") or "").strip()
    patch = {}
    if isinstance(output.get("candidate_patch"), dict):
        patch = _sanitize_step_patch(
            step_key,
            output.get("candidate_patch") or {},
            latest_by_step=latest_by_step,
            project_id=_project_id_from_steps(latest_by_step),
            base=base_draft,
            # 教练回复不能因为补丁多了一段就整条报废：违反数量契约的键丢弃，回复与建议照常送达。
            count_policy="drop",
        )
    return {
        "step_key": step_key,
        "reply": reply,
        "suggestions": suggestions,
        "candidate_label": candidate_label or None,
        "candidate_patch": patch or None,
    }


def _normalize_triage_output(output: dict[str, Any], base_draft: dict[str, Any]) -> dict[str, Any]:
    base_items = _fallback_triage_items(base_draft)
    updates = {}
    for item in output.get("items") or []:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("scene_id") or "").strip()
        if not scene_id:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"pass", "maybe", "rewrite"}:
            continue
        updates[scene_id] = {
            "status": status,
            "notes": str(item.get("notes") or "").strip(),
            "missing_fields": _coerce_string_list(item.get("missing_fields")),
            "fix_steps": _coerce_string_list(item.get("fix_steps")),
            "repair_patch": _sanitize_scene_repair_patch(item.get("repair_patch") or {}),
        }
    items = []
    for item in base_items:
        scene_id = item["scene_id"]
        update = updates.get(scene_id, {})
        items.append(
            {
                **item,
                "status": update.get("status", item.get("status") or ""),
                "notes": update.get("notes", item.get("notes") or ""),
                "missing_fields": update.get("missing_fields", item.get("missing_fields") or []),
                "fix_steps": update.get("fix_steps", item.get("fix_steps") or []),
                "repair_patch": update.get("repair_patch", item.get("repair_patch") or {}),
            }
        )
    return {"items": items}


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
        for index, item in enumerate(payload.get("characters") or [], start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("display_name") or item.get("character_id") or index)
            for field_key, template_value in template.items():
                if field_key in _SERVER_ASSIGNED_ITEM_KEYS:
                    continue
                value = item.get(field_key)
                if isinstance(template_value, dict):
                    nested = value if isinstance(value, dict) else {}
                    if not any(_has_value(nested_value) for nested_value in nested.values()):
                        gaps.append(f"characters[{label}].{field_key}")
                elif not _has_value(value):
                    gaps.append(f"characters[{label}].{field_key}")
    elif step_key == "scene_list":
        for index, item in enumerate(payload.get("scenes") or [], start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("scene_id") or index)
            gaps.extend(
                f"scenes[{label}].{field_key}"
                for field_key in _SCENE_LIST_CONTENT_KEYS
                if not _has_value(item.get(field_key))
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
        if _has_value(normalized):
            result[key] = normalized
    return result


def _assert_meaningful_generation_patch(step_key: str, patch: dict[str, Any]) -> None:
    if step_key != "character_sheets":
        return
    characters = patch.get("characters") if isinstance(patch, dict) else None
    if not isinstance(characters, list) or not characters:
        raise ValueError(
            "character_sheets LLM output is empty: return at least one character with role plus concrete "
            "goal, ambition, values, conflict, epiphany, or summary content."
        )
    if any(_has_meaningful_character_sheet_content(item) for item in characters if isinstance(item, dict)):
        return
    raise ValueError(
        "character_sheets LLM output is too sparse: model returned only IDs/names or blank fields. "
        "Each generated character must include role plus concrete goal, ambition, values, conflict, "
        "epiphany, or summary content."
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
    if kind == "paragraphs" and step_key == "short_synopsis":
        # 阶段 B：一页梗概 = 五句各扩一段，恰好五段。多出来的段不再静默截掉。
        items = _coerce_string_list(value)
        if len(items) > SHORT_SYNOPSIS_PARAGRAPHS:
            message = (
                f"一页梗概要求恰好 {SHORT_SYNOPSIS_PARAGRAPHS} 段（每段对应五句之一），"
                f"模型返回了 {len(items)} 段；本次结果已丢弃。"
            )
            if count_policy == "raise":
                raise StructuredCountMismatch(message)
            return None
        return items
    if kind in {"list", "paragraphs"}:
        return _coerce_string_list(value)
    if kind == "sentences":
        seed = base.get(key) if isinstance(base.get(key), list) else []
        items = _coerce_string_list(value)
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
        for item in (artifact.artifact_json or {}).get("characters") or []:
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
        if not character_id and display_name:
            character_id = existing_by_name.get(display_name, "")
        if not character_id:
            character_id = f"{project_id}_CHAR{index:02d}"
        item["character_id"] = character_id
        for field_key, template_value in template.items():
            if field_key == "character_id":
                continue
            sanitized = _prune_empty_values(_sanitize_template_value(template_value, raw_item.get(field_key)))
            # 生成/补丁是「补全」语义：空值不落键，_merge_patch 时不清空该成员的既有
            # 内容（与 FE 咨询式补丁一致）——模型部分回传不再抹掉手工填过的字段。
            if _has_value(sanitized):
                item[field_key] = sanitized
        if not item.get("display_name"):
            item["display_name"] = display_name or character_id
        result.append(item)
    return result


def _prune_empty_values(value: Any) -> Any:
    """递归剥掉空叶子（"" / [] / {} / 全空子树），让补丁只携带有内容的键。"""
    if isinstance(value, dict):
        pruned = {key: _prune_empty_values(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if _has_value(item)}
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
        chapter_id = str(raw_item.get("chapter_id") or current_chapter_id or f"{project_id}_CH01").strip()
        current_chapter_id = chapter_id
        next_seq = scene_seq_by_chapter.get(chapter_id, 0) + 1
        scene_seq = _coerce_int(raw_item.get("scene_seq"), default=next_seq)
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
        "must_include_text",
        "exit_change",
        "hook",
        "triage_status",
        "triage_notes",
        "beats_json",
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
            elif key in {"beats_json"}:
                beats = _coerce_string_list(overlay.get(key))
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
        return _coerce_string_list(value)
    return str(value or "").strip()


def _merge_patch(base: Any, patch: Any, *, collection_key: str | None = None) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(normalize(base))
        for key, value in patch.items():
            merged[key] = _merge_patch(merged.get(key), value, collection_key=key)
        return merged
    if isinstance(base, list) and isinstance(patch, list):
        id_key = _collection_id_key(collection_key)
        if not id_key:
            return normalize(patch)
        base_items = [normalize(item) for item in base if isinstance(item, dict)]
        patch_items = [normalize(item) for item in patch if isinstance(item, dict)]
        patch_by_id = {
            str(item.get(id_key) or ""): item
            for item in patch_items
            if str(item.get(id_key) or "")
        }
        # 保持底稿成员顺序（名册/场景序即作者语序）：patch 命中的按 id 就地合并，
        # 模型新增的成员追加在尾部——定向补全不打乱其余成员的排列。
        merged_items = []
        seen_ids: set[str] = set()
        for item in base_items:
            item_id = str(item.get(id_key) or "")
            if not item_id:
                continue
            merged_items.append(_merge_patch(item, patch_by_id[item_id]) if item_id in patch_by_id else item)
            seen_ids.add(item_id)
        for item in patch_items:
            item_id = str(item.get(id_key) or "")
            if item_id and item_id not in seen_ids:
                merged_items.append(_merge_patch({}, item))
                seen_ids.add(item_id)
        return merged_items
    return normalize(patch)


def _collection_id_key(collection_key: str | None) -> str | None:
    if collection_key == "characters":
        return "character_id"
    if collection_key == "scenes":
        return "scene_id"
    return None


_FIELD_LABELS = {
    "target_reader": "目标读者",
    "summary": "概括",
    "scenes": "场景",
    "crucible": "坩埚",
    "scene_crucible": "坩埚",
    "goal": "目标",
    "conflict": "冲突",
    "setback": "挫折",
    "reaction": "反应",
    "dilemma": "困境",
    "decision": "决定",
    "exit_change": "离场变化",
    "hook": "钩子",
}

_DIAGNOSTIC_LABELS = {
    "scene_core_empty": "场景核心为空",
    "weak_crucible_pressure": "坩埚压力不足",
    "weak_goal_specificity": "目标不够具体",
    "weak_conflict_escalation": "冲突升级不足",
    "weak_setback_cost": "挫折代价不足",
    "weak_reaction_specificity": "反应不够具体",
    "fake_dilemma": "困境不是真两难",
    "weak_decision_next_goal": "决定没有引出下一目标",
}


def _field_label(value: str) -> str:
    return _FIELD_LABELS.get(str(value or ""), str(value or "字段"))


def _diagnostic_label(value: str) -> str:
    key = str(value or "")
    if key in _DIAGNOSTIC_LABELS:
        return _DIAGNOSTIC_LABELS[key]
    if key.startswith("missing_"):
        return f"缺少{_field_label(key.removeprefix('missing_'))}"
    if key.startswith("placeholder_"):
        return f"{_field_label(key.removeprefix('placeholder_'))}仍是占位"
    return key


def _fallback_triage_items(draft: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for index, raw_scene in enumerate(draft.get("scenes") or [], start=1):
        if not isinstance(raw_scene, dict):
            continue
        diagnosis = diagnose_scene_detail(raw_scene, index=index)
        scene_type = str(diagnosis.get("primary_form") or diagnosis.get("scene_type") or "proactive").strip().lower() or "proactive"
        status = str(diagnosis.get("recommended_status") or "maybe")
        missing_fields = [str(field) for field in diagnosis.get("missing_fields") or []]
        if status == "rewrite":
            notes = "修复场景压力：场景核心太薄，需要重建前提和坩埚。"
        elif status == "maybe":
            readable_flags = "、".join(_diagnostic_label(flag) for flag in diagnosis.get("pressure_flags") or missing_fields)
            notes = f"修复场景压力：{readable_flags}。"
        else:
            notes = "核心压力清楚，可以继续。"
        items.append(
            {
                "scene_id": str(raw_scene.get("scene_id") or f"scene_{index:02d}"),
                "title": str(raw_scene.get("title") or raw_scene.get("summary") or f"Scene {index:02d}"),
                "primary_form": scene_type,
                "scene_type": scene_type,
                "status": status,
                "notes": notes,
                "missing_fields": missing_fields,
                "fix_steps": diagnosis.get("fix_steps") or _fallback_fix_steps(scene_type, missing_fields, status),
                "repair_patch": _fallback_repair_patch(scene_type, missing_fields, diagnosis.get("pressure_flags")),
            }
        )
    return items


def _fallback_fix_steps(scene_type: str, missing_fields: list[str], status: str) -> list[str]:
    if status == "pass":
        return []
    if status == "rewrite":
        return ["围绕具体坩埚和可见压力转折重建这一场。"]
    label = "反应/困境/决定" if scene_type == "reactive" else "目标/冲突/挫折"
    return [f"补齐缺失的{label}字段：{'、'.join(_field_label(field) for field in missing_fields)}。"]


def _fallback_repair_patch(
    scene_type: str,
    missing_fields: list[str],
    pressure_flags: list[str] | None = None,
) -> dict[str, str]:
    # 例句与 snowflake_steps.SCENE_FIELD_EXAMPLES 同源：应用后留在字段里会被规则层认作占位。
    examples = SCENE_FIELD_EXAMPLES
    required = ["reaction", "dilemma", "decision"] if scene_type == "reactive" else ["goal", "conflict", "setback"]
    keys = ["crucible" if field == "crucible" else field for field in missing_fields if field in {"crucible", *required}]
    if pressure_flags and "missing_cost_requirement" in pressure_flags:
        keys.append("cost_requirement")
    return {key: examples[key] for key in keys if key in examples}


def _sanitize_scene_repair_patch(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {
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
    return {key: str(value.get(key) or "").strip() for key in allowed if key in value and str(value.get(key) or "").strip()}


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _project_id_from_steps(latest_by_step: Mapping[str, Any]) -> str:
    for artifact in latest_by_step.values():
        project_id = str(getattr(artifact, "project_id", "") or "").strip()
        if project_id:
            return project_id
    return ""


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


def draft_has_content(draft: Any) -> bool:
    """草稿里是否有任何非空内容——空骨架代表作者还没写，不该占提示上下文。"""
    return _has_value(draft)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    return True

"""章节编排的章节蓝图服务（chapter plan）：读 / 作者改写 / 让 AI 重新生成。

蓝图行的一处定义在 ``chapter_architecture``（作者版 ``llm_call_id=None``）；场景运行的 ``ensure_scene_planning``
有生效的蓝图就用它，没有才现做一份——起草读的就是这一份。作者改写的蓝图不因设计 / 绑定变化作废（B07-03）。
「AI 生成蓝图」的上下文由 ``chapter_planning_context`` 汇编（邻章交接、章结构邻域、人物站位、戏剧卡、雪花 canon），
LLM 调用走 ``structured_llm_call`` 的计量 / 审计骨架；锁章由 ``chapter_approval.require_chapter_mutation_allowed`` 统一 409。

2026-09-30（B07-12，作者「没有模型就不兜底」）：没有可用模型时生成回 409 ``CHAPTER_PLAN_LLM_NOT_CONFIGURED`` +
author_action，不落占位蓝图（占位会被场景运行当真注入）。给作者看的话一律中文、不带异常原文。

2026-10（round 2 · B2-5）：「AI 编排」的方向（candidates）、补全（fill / apply，连同待补清单 gaps）与 AI 体检（review）
三条通道删了——一次都没用过；出编排方向、补场景卡归构思 09 / 10 步，整章挑毛病归成稿中心的「AI 通读本章」。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, GenerationPlanningArtifact
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.chapter_architecture import (
    CHAPTER_ARCHITECTURE_ARTIFACT,
    latest_chapter_architecture,
    normalize_chapter_architecture,
    persist_chapter_architecture,
)
from novel_system.services.chapter_planning_context import (
    STYLE_REFERENCE_SLOT,
    ChapterPlanningContext,
    ChapterPlanningContextBuilder,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.llm_client import LLMConfigurationError
from novel_system.services.llm_service_base import RuntimeLLMAccess
from novel_system.services.prompt_builder import PromptConfigurationError
from novel_system.services.scene_lookup import require_project_chapter
from novel_system.services.scene_planning_staleness import design_changed_since
from novel_system.services.structured_llm_call import run_structured_call

# 节点 id（= 模板名）：与场景运行现做蓝图同一个节点
_ARCHITECTURE_NODE = CHAPTER_ARCHITECTURE_ARTIFACT


class ChapterPlanService(RuntimeLLMAccess):
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
        self._context_builder = ChapterPlanningContextBuilder(session)

    # ---------- 章节蓝图（一等公民） ----------

    def get_architecture(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        require_project_chapter(self.session, project_id, chapter_id)
        artifact = latest_chapter_architecture(self.session, chapter_id)
        return {"architecture": self._architecture_view(artifact)}

    def generate_architecture(
        self, project_id: str, chapter_id: str, *, actor_ref: str = "operator"
    ) -> dict[str, Any]:
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["chapter_story_architecture"],
            operation="chapter_plan.generate_architecture",
        )
        # 没有模型就不生成：不落占位蓝图（占位会被场景 run 当真注入），只引导去配置。
        self._require_llm(_ARCHITECTURE_NODE)
        context = self._context_builder.build(project_id, chapter_id)
        payload = self._run_structured_task(
            task_key=_ARCHITECTURE_NODE,
            template_name=_ARCHITECTURE_NODE,
            project_id=project_id,
            step_ref=f"chapter_plan:architecture:{chapter_id}",
            prompt_payload=context.prompt_payload,
            normalize_output=lambda output: normalize_chapter_architecture(output, strict=False),
        )
        artifact = self._persist_architecture(
            chapter,
            payload=payload["output"],
            llm_call_id=payload["llm_call_id"],
            actor_ref=actor_ref,
            context=context,
        )
        return {
            "source": "llm",
            "architecture": self._architecture_view(artifact),
            "degraded_slots": context.degraded_slots,
            "context_fingerprint": context.context_fingerprint,
        }

    def put_architecture(
        self,
        project_id: str,
        chapter_id: str,
        payload: dict[str, Any],
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["chapter_story_architecture"],
            operation="chapter_plan.put_architecture",
        )
        body = normalize_chapter_architecture(dict(payload or {}), strict=False)
        if not str(body.get("chapter_promise") or "").strip():
            raise DomainError(
                "CHAPTER_ARCHITECTURE_PROMISE_REQUIRED",
                "章节蓝图至少要写「本章承诺」。",
                status_code=400,
            )
        artifact = self._persist_architecture(
            chapter,
            payload=body,
            llm_call_id=None,
            actor_ref=actor_ref or "author",
            context=None,
        )
        return {"architecture": self._architecture_view(artifact)}

    def _architecture_view(self, artifact: GenerationPlanningArtifact | None) -> dict[str, Any] | None:
        """蓝图回包：作者写的蓝图留下来之后设计 / 绑定又变过时带 ``design_changed``（B07-03），否则为 None。"""
        view = _serialize_architecture(artifact)
        if view is not None:
            view["design_changed"] = design_changed_since(self.session, artifact)
        return view

    def _persist_architecture(
        self,
        chapter: ChapterGoal,
        *,
        payload: dict[str, Any],
        llm_call_id: str | None,
        actor_ref: str,
        context: ChapterPlanningContext | None,
    ) -> GenerationPlanningArtifact:
        return persist_chapter_architecture(
            self.session,
            chapter.chapter_id,
            payload,
            llm_call_id=llm_call_id,
            created_by=actor_ref or "chapter_plan",
            source_bundle_hash=context.context_fingerprint if context else None,
        )

    # ---------- LLM plumbing ----------

    def _require_llm(self, node_id: str) -> None:
        """没有可用模型就 409：作者点的是 AI，拿规则算的东西冒充 AI 结果就是撒谎（作者 2026-09-15「没有模型就不兜底」）。"""
        if self._llm_enabled():
            return
        raise DomainError(
            "CHAPTER_PLAN_LLM_NOT_CONFIGURED",
            "章节编排的 AI 需要先启用真实模型。请到系统配置里配置 provider 与密钥并测试通过后重试。",
            status_code=409,
            details={
                "node_id": node_id,
                "author_action": llm_setup_action(llm_enabled=False, generation_mode="chapter_plan"),
            },
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
    ) -> dict[str, Any]:
        try:
            task_config = self._task_config(task_key)
            template = self._template(template_name)
        except (KeyError, LLMConfigurationError, PromptConfigurationError) as exc:
            missing_route = isinstance(exc, KeyError)
            raise DomainError(
                "CHAPTER_PLAN_LLM_ROUTE_OR_PROMPT_MISSING",
                (
                    f"模型已接入，但 LLM 节点路由未配置：{task_key}。"
                    "请到配置环境点击“一键补齐”，或在节点路由中为该节点绑定 provider/model 后重试。"
                    if missing_route
                    else f"章节编排的 AI 节点还没配好：{task_key} 的路由或提示词模板不可用。"
                    "请检查节点路由、提示词模板和模型配置后重试。"
                ),
                status_code=409,
                details={
                    "node_id": task_key,
                    "template_name": template_name,
                    "error_code": getattr(exc, "code", exc.__class__.__name__),
                    "reason": "missing_node_route" if missing_route else "route_or_prompt_invalid",
                    "next_action": (
                        "sync_missing_llm_node_routes"
                        if missing_route
                        else "configure_chapter_plan_node_route_and_prompt_then_retry"
                    ),
                },
            ) from exc
        result = run_structured_call(
            self.session,
            self._client(),
            task_config=task_config,
            template=template,
            node_id=task_key,
            project_id=project_id,
            step_ref=step_ref,
            user_prompt=_render_user_prompt(template, prompt_payload),
            prompt_payload=prompt_payload,
            normalize_output=normalize_output,
            error_prefix="CHAPTER_PLAN",
            failure_message="章节编排的 AI 调用没有成功：模型没有回话或出错了。请检查模型接入与节点路由后重试。",
            invalid_message="章节编排的 AI 这一次没有给出可用的结果（格式不对或内容为空）。可以再试一次。",
        )
        return {"llm_call_id": result.llm_call_id, "output": result.output}


def _serialize_architecture(artifact: GenerationPlanningArtifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    return {
        "row_id": artifact.row_id,
        "payload": artifact.payload_json or {},
        "created_by": artifact.created_by,
        "llm_call_id": artifact.llm_call_id,
        "created_at": artifact.created_at,
        "status": artifact.status,
    }


# ---------- prompt helpers（与雪花工作区同构） ----------


def _render_style_reference_block(slot: Any) -> str:
    """2026-09-12 结构跟随：结构画像 / 样例 / 场景手法按原样多行渲染，不塞进紧凑 JSON。

    画像是带换行的中文块，样例块还带 UNTRUSTED_REFERENCE_DATA 边界——在 JSON 字符串里
    全部被转义成 ``\\n``，模型读到的是一行长串，边界标记也失去了可读性。所以从载荷里
    摘出来放在 Working payload 之后、Required keys 之前，按段落呈现。
    """
    if not isinstance(slot, dict):
        return ""
    parts = [
        str(slot.get(key) or "").strip()
        for key in ("how_to_use", "structure_card", "structure_samples", "planning_guidance")
    ]
    body = "\n".join(part for part in parts if part)
    if not body:
        return ""
    return "Reference author structure (style_reference — planning scale and craft only; never content to reuse):\n" + body


def _render_user_prompt(template: Any, prompt_payload: dict[str, Any]) -> str:
    required = template.structured_schema.get("required") or []
    required_text = ", ".join(str(item) for item in required if isinstance(item, str))
    payload = dict(prompt_payload)
    style_reference_block = _render_style_reference_block(payload.pop(STYLE_REFERENCE_SLOT, None))
    # 紧凑 JSON：缩进对模型没有价值，却给嵌套载荷凭空加约六成体积（与雪花工作区同款）。
    prompt_json = json.dumps(normalize(payload), ensure_ascii=False, separators=(",", ":"))
    return (
        f"{template.task_prompt.strip()}\n\n"
        f"Working payload:\n{prompt_json}\n\n"
        + (f"{style_reference_block}\n\n" if style_reference_block else "")
        + f"Required top-level JSON keys: {required_text or 'follow the provided schema'}.\n"
        "Return only valid JSON. Do not wrap it in markdown fences."
    )

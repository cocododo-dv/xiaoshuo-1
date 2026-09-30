"""章节编排的 LLM 规划服务（chapter plan）。

四条能力（设计文档 docs/chapter-arrangement-llm-design-2026-07-16.md §4/§5）：
- 章节蓝图显式化：读 / 作者改写 / 显式重生成（蓝图行的一处定义见 ``chapter_architecture``；作者版
  llm_call_id=None，场景 run 的 ensure_scene_planning 会自动复用最新 active 行）。
- candidates：3 个结构策略互斥的整章编排候选（无状态咨询，不落库）。
- fill：保真补全 —— 只产出「填空」补丁；覆盖型意见降级为 notes。
- review：编排体检 findings（带 evidence 与可选单条填空建议）。
- apply：补丁经服务端 sanitize 后在单事务内经 CatalogService 原子回写目录。
- gaps：**不是 AI**——按空槽列出的「待补清单」（规则算的，名字上就说清楚）。

铁律（服务端强制，不信任模型自律）：只填空、按 scene_id 对位、新卡只追加、
不删除、不覆盖作者非空文本。锁章由 chapter_approval.require_chapter_mutation_allowed
统一 409。LLM 调用走 ``structured_llm_call`` 的计量 / 审计骨架。

2026-09-30（B07-12，作者「没有模型就不兜底」）：蓝图生成 / 候选 / 补全 / 体检在没有可用模型时回 409
``CHAPTER_PLAN_LLM_NOT_CONFIGURED`` + author_action，不再回 200 + ``source: fallback``（体检以前还拿规则凑一份
findings 冒充 AI 体检）；待补清单另成一个只读接口。给作者看的话一律中文、不带异常原文。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, GenerationPlanningArtifact, SceneCard
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.catalog import (
    CatalogService,
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    scene_kind,
    scene_title,
)
from novel_system.services.catalog_labels import DRAMA_SLOT_LABELS, SCENE_SLOT_LABELS, scene_display_title
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.chapter_architecture import (
    ARCHITECTURE_FIELDS,  # noqa: F401 — 旧名：从本模块 import 的调用方
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
from novel_system.services.scene_design_ownership import plan_owned_scene_ids
from novel_system.services.scene_lookup import require_project, require_project_chapter
from novel_system.services.scene_planning_staleness import design_changed_since
from novel_system.services.structured_llm_call import run_structured_call

_PATCH_DRAMA_FIELDS = (
    "promise",
    "spine",
    "arc",
    "problem",
    "aftertaste",
    "ending",
)
# 视为「空槽」的占位文本（目录/物化两侧的历史占位词）。
_PLACEHOLDER_VALUES = {"", "—", "待定", "（待规划）", "(待规划)", "待补"}
_PLACEHOLDER_TITLE_PREFIXES = ("未命名", "新场景", "开场")
_MAX_FIELD_CHARS = 400
_MAX_TITLE_CHARS = 60
_MAX_APPEND_ABS = 6

# 2026-09-30（批准 #17a，重评 R10）：FORESHADOW_OVERDUE 去掉——伏笔账本早已删除，这个码没有任何数据来源；
# 张力 / 视角疲劳 / 交接三项改读场上的真实数据（见 chapter_plan_review v4）。
REVIEW_FINDING_CODES = (
    "PROMISE_UNGROUNDED",
    "SCENE_FUNCTION_DUPLICATE",
    "REACTIVE_MISSING",
    "TENSION_FLAT",
    "POV_FATIGUE",
    "HANDOFF_MISMATCH",
    "EXIT_NO_CHANGE",
    "BRIEF_INCOMPLETE",
    "OTHER",
)

# 各节点 id（= 模板名）
_ARCHITECTURE_NODE = CHAPTER_ARCHITECTURE_ARTIFACT
_CANDIDATES_NODE = "chapter_scene_plan_candidates"
_FILL_NODE = "chapter_scene_plan_fill"
_REVIEW_NODE = "chapter_plan_review"


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
        self._catalog = CatalogService(session)
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

    # ---------- candidates（发散通道） ----------

    def candidates(
        self, project_id: str, chapter_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        require_project_chapter(self.session, project_id, chapter_id)
        self._require_llm(_CANDIDATES_NODE)
        context = self._context_builder.build(project_id, chapter_id)
        prompt_payload = dict(context.prompt_payload)
        hint = str((body or {}).get("direction_hint") or "").strip()
        if hint:
            prompt_payload["direction_hint"] = hint[:300]
        result = self._run_structured_task(
            task_key=_CANDIDATES_NODE,
            template_name=_CANDIDATES_NODE,
            project_id=project_id,
            step_ref=f"chapter_plan:candidates:{chapter_id}",
            prompt_payload=prompt_payload,
            normalize_output=lambda output: _normalize_candidates_output(output, context.scenes),
        )
        return {
            "source": "llm",
            "llm_call_id": result["llm_call_id"],
            "degraded_slots": context.degraded_slots,
            "context_fingerprint": context.context_fingerprint,
            **result["output"],
        }

    # ---------- fill（收敛通道） ----------

    def fill(self, project_id: str, chapter_id: str, body: dict[str, Any]) -> dict[str, Any]:
        require_project_chapter(self.session, project_id, chapter_id)
        body = body or {}
        mode = str(body.get("mode") or "fill").strip().lower()
        if mode not in {"fill", "adopt"}:
            raise DomainError(
                "CHAPTER_PLAN_MODE_INVALID",
                "补全只有两种：fill（把空槽填上）或 adopt（采纳一份编排方向）。",
                status_code=400,
            )
        self._require_llm(_FILL_NODE)
        context = self._context_builder.build(project_id, chapter_id)
        prompt_payload = dict(context.prompt_payload)
        prompt_payload["mode"] = mode
        if mode == "adopt":
            candidate = body.get("candidate")
            if not isinstance(candidate, dict) or not candidate:
                raise DomainError(
                    "CHAPTER_PLAN_CANDIDATE_REQUIRED",
                    "采纳方向时要带上选中的那一份方向。",
                    status_code=400,
                )
            prompt_payload["adopted_candidate"] = normalize(candidate)
        result = self._run_structured_task(
            task_key=_FILL_NODE,
            template_name=_FILL_NODE,
            project_id=project_id,
            step_ref=f"chapter_plan:fill:{chapter_id}",
            prompt_payload=prompt_payload,
            normalize_output=lambda output: output if isinstance(output, dict) else {},
        )
        raw = result["output"]
        patch, dropped = sanitize_plan_patch(
            context.scenes,
            raw.get("patch"),
            chapter=context.chapter,
            plan_owned_scene_ids=self._plan_owned_scene_ids(project_id, context.scenes),
        )
        return {
            "source": "llm",
            "llm_call_id": result["llm_call_id"],
            "patch": patch,
            "notes": _coerce_notes(raw.get("notes")),
            "gaps": [str(item)[:200] for item in raw.get("gaps") or [] if str(item).strip()][:20],
            "dropped": dropped,
            "degraded_slots": context.degraded_slots,
            "context_fingerprint": context.context_fingerprint,
        }

    # ---------- review（体检通道） ----------

    def review(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        require_project_chapter(self.session, project_id, chapter_id)
        self._require_llm(_REVIEW_NODE)
        context = self._context_builder.build(project_id, chapter_id)
        result = self._run_structured_task(
            task_key=_REVIEW_NODE,
            template_name=_REVIEW_NODE,
            project_id=project_id,
            step_ref=f"chapter_plan:review:{chapter_id}",
            prompt_payload=context.prompt_payload,
            normalize_output=lambda output: _normalize_review_output(
                output, context.scenes, plan_owned_scene_ids=self._plan_owned_scene_ids(project_id, context.scenes),
            ),
        )
        return {
            "source": "llm",
            "llm_call_id": result["llm_call_id"],
            "degraded_slots": context.degraded_slots,
            "context_fingerprint": context.context_fingerprint,
            **result["output"],
        }

    # ---------- gaps（待补清单：不是 AI） ----------

    def gaps(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        """这一章的戏剧卡与各场三拍 / 视角还空着哪些——按空槽算出来的清单，不调模型、不冒充 AI 结果。

        ``items`` 是结构化的一份（中文字段名、第几场，不带内部 id，前端照它渲染）；``gaps`` 是旧的一行一条的写法
        （英文槽名 + 场景 id），前端换到 ``items`` 之前照旧给。"""
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        scenes = self._catalog.scene_rows(chapter_id)
        owned = self._plan_owned_scene_ids(project_id, scenes)
        return {
            "source": "rules",
            "gaps": empty_slot_gaps(scenes, chapter, plan_owned_scene_ids=owned),
            "items": empty_slot_gap_items(scenes, chapter, plan_owned_scene_ids=owned),
        }

    # ---------- apply（原子回写） ----------

    def apply(
        self,
        project_id: str,
        chapter_id: str,
        body: dict[str, Any],
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        scenes = self._catalog.scene_rows(chapter_id)
        patch, dropped = sanitize_plan_patch(
            scenes,
            (body or {}).get("patch"),
            chapter=chapter,
            plan_owned_scene_ids=self._plan_owned_scene_ids(project_id, scenes),
        )
        drama_updates = patch.get("drama") or {}
        scene_items = patch["scenes"]
        append_items = patch["append_scenes"]
        changed_fields: list[str] = [f"chapter:drama:{key}" for key in drama_updates]
        for item in scene_items:
            changed_fields.extend(f"scene:{item['scene_id']}:{key}" for key in item["set"])
        if append_items:
            changed_fields.append("scenes.append")
        # 锁章统一裁决：真实写入前先过 approved-chapter 闸（no-op 补丁不触发）。
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=changed_fields,
            operation="chapter_plan.apply",
        )
        by_id = {scene.scene_id: scene for scene in scenes}
        applied_scenes = 0
        skipped = list(dropped)
        if drama_updates:
            narrative = dict(chapter.narrative_json or {})
            drama = {**dict(narrative.get("drama") or {}), **drama_updates}
            chapter_body: dict[str, Any] = {"drama": drama}
            # 目录历史上同时保留了 chapter.promise 与 drama.promise；写核心承诺时保持两者一致。
            if "promise" in drama_updates:
                chapter_body["promise"] = drama_updates["promise"]
            self._catalog.update_chapter(project_id, chapter_id, chapter_body)
        for item in scene_items:
            scene = by_id[item["scene_id"]]
            catalog_body: dict[str, Any] = {}
            direct_updates: dict[str, str] = {}
            for key, value in item["set"].items():
                if key in ("exit_change", "hook"):
                    direct_updates[key] = value
                elif key == "pov_character_name":
                    catalog_body["pov_character_name"] = value
                else:
                    catalog_body[key] = value
            if catalog_body:
                self._catalog.update_scene(project_id, scene.scene_id, catalog_body)
            for key, value in direct_updates.items():
                setattr(scene, key, value)
            applied_scenes += 1
        appended = 0
        for item in append_items:
            created = self._catalog.create_scene(project_id, chapter_id, item)
            scene_id = created["scene"]["scene_id"]
            row = self.session.get(SceneCard, scene_id)
            if row is not None:
                if item.get("exit_change"):
                    row.exit_change = item["exit_change"]
                if item.get("hook"):
                    row.hook = item["hook"]
            appended += 1
        self.session.flush()
        # 只回这一章：按章序找出它是第几章（章号 / slug 与整本目录里的一样），只为它查表（B07-19：以前为了这一章
        # 把整本目录重建一遍）
        chapter_payload = None
        for index, row in enumerate(self._catalog.chapter_rows(project_id)):
            if row.chapter_id == chapter_id:
                chapter_payload = self._catalog.chapter_payload(require_project(self.session, project_id), row, index)
                break
        return {
            "applied": {
                "drama": len(drama_updates),
                "scenes": applied_scenes,
                "appended": appended,
            },
            "skipped": skipped,
            "chapter": chapter_payload,
        }

    # ---------- LLM plumbing ----------

    def _plan_owned_scene_ids(self, project_id: str, scenes: list[SceneCard]) -> set[str]:
        """设计归构思侧所有的场景卡（雪花物化 / 回流出来的，且构思里那一行还在）——章节规划 AI 不往它们的设计里填。"""
        return plan_owned_scene_ids(self.session, project_id, scenes)

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


# ---------- 纯函数：补丁 sanitize 与输出归一 ----------


def _is_empty_slot(value: Any) -> bool:
    text = str(value or "").strip()
    return text in _PLACEHOLDER_VALUES


def _is_placeholder_title(value: Any) -> bool:
    text = str(value or "").strip()
    if text in _PLACEHOLDER_VALUES:
        return True
    return any(text.startswith(prefix) for prefix in _PLACEHOLDER_TITLE_PREFIXES)


def _clean_text(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def sanitize_plan_patch(
    scenes: list[SceneCard],
    patch: Any,
    *,
    chapter: ChapterGoal | None = None,
    plan_owned_scene_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把 LLM 补丁裁剪成「只填空」的安全子集。

    返回 (clean_patch, dropped)；dropped 逐条记录被拒写入及原因，供 UI 展示。
    性质：不覆盖非空、不删除、不重排、新卡只追加且有上限、未知 scene_id 丢弃。
    阶段 Y：``plan_owned_scene_ids`` 里的场景卡，设计（三拍 / POV / 离场变化 / 钩子）归构思第 10 步所有——
    在这里填了，下一次回流就会按构思里的空值盖回去；一律丢弃（``design_owned_by_plan``），题名照旧规则。
    """
    owned = plan_owned_scene_ids or set()
    dropped: list[dict[str, str]] = []
    clean_scenes: list[dict[str, Any]] = []
    clean_appends: list[dict[str, Any]] = []
    if not isinstance(patch, dict):
        return {"scenes": [], "append_scenes": []}, dropped
    clean_drama: dict[str, str] = {}
    raw_drama = patch.get("drama")
    if isinstance(raw_drama, dict):
        current_drama = dict(dict(chapter.narrative_json or {}).get("drama") or {}) if chapter else {}
        for raw_key, raw_value in raw_drama.items():
            key = str(raw_key)
            field = f"drama.{key}"
            value = _clean_text(raw_value)
            if not value:
                dropped.append({"scene_id": "", "field": field, "reason": "empty_value"})
                continue
            if key not in _PATCH_DRAMA_FIELDS or chapter is None:
                dropped.append({"scene_id": "", "field": field, "reason": "field_not_allowed"})
                continue
            if not _is_empty_slot(current_drama.get(key)):
                dropped.append({"scene_id": "", "field": field, "reason": "field_not_empty"})
                continue
            clean_drama[key] = value
    by_id = {scene.scene_id: scene for scene in scenes}

    for item in patch.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("scene_id") or "").strip()
        scene = by_id.get(scene_id)
        if scene is None:
            dropped.append({"scene_id": scene_id, "field": "*", "reason": "unknown_scene"})
            continue
        raw_set = item.get("set")
        if not isinstance(raw_set, dict):
            continue
        kind = scene_kind(scene)
        brief_keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        brief = dict(scene.writer_brief_json or {})
        clean_set: dict[str, str] = {}
        for key, raw_value in raw_set.items():
            key = str(key)
            value = _clean_text(raw_value, _MAX_TITLE_CHARS if key == "title" else _MAX_FIELD_CHARS)
            if not value:
                dropped.append({"scene_id": scene_id, "field": key, "reason": "empty_value"})
                continue
            if scene_id in owned and key != "title" and (
                key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD) or key in ("pov_character_name", "exit_change", "hook")
            ):
                dropped.append({"scene_id": scene_id, "field": key, "reason": "design_owned_by_plan"})
                continue
            if key in brief_keys:
                if not _is_empty_slot(brief.get(key)):
                    dropped.append({"scene_id": scene_id, "field": key, "reason": "field_not_empty"})
                    continue
                clean_set[key] = value
            elif key == "title":
                if not _is_placeholder_title(scene_title(scene)):
                    dropped.append({"scene_id": scene_id, "field": key, "reason": "field_not_empty"})
                    continue
                clean_set[key] = value
            elif key == "pov_character_name":
                if scene.pov_character_id:
                    dropped.append({"scene_id": scene_id, "field": key, "reason": "field_not_empty"})
                    continue
                clean_set[key] = value
            elif key in ("exit_change", "hook"):
                if not _is_empty_slot(getattr(scene, key)):
                    dropped.append({"scene_id": scene_id, "field": key, "reason": "field_not_empty"})
                    continue
                clean_set[key] = value
            else:
                # kind/state/删除/重排等覆盖型或危险意图一律不进补丁。
                dropped.append({"scene_id": scene_id, "field": key, "reason": "field_not_allowed"})
        if clean_set:
            clean_scenes.append({"scene_id": scene_id, "set": clean_set})

    append_cap = min(_MAX_APPEND_ABS, len(scenes) + 4)
    for item in patch.get("append_scenes") or []:
        if not isinstance(item, dict):
            continue
        if len(clean_appends) >= append_cap:
            dropped.append({"scene_id": "", "field": "append_scenes", "reason": "append_cap_reached"})
            break
        title = _clean_text(item.get("title"), _MAX_TITLE_CHARS)
        if not title:
            dropped.append({"scene_id": "", "field": "append_scenes", "reason": "title_required"})
            continue
        kind = "reactive" if str(item.get("kind") or "").strip().lower() in {"reactive", "反应"} else "proactive"
        brief_keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        raw_brief = item.get("brief") if isinstance(item.get("brief"), dict) else {}
        clean_append: dict[str, Any] = {
            "title": title,
            "kind": kind,
            "brief": {
                key: _clean_text(raw_brief.get(key))
                for key in brief_keys
                if _clean_text(raw_brief.get(key))
            },
        }
        pov = _clean_text(item.get("pov_character_name"), _MAX_TITLE_CHARS)
        if pov:
            clean_append["pov_character_name"] = pov
        for key in ("exit_change", "hook"):
            value = _clean_text(item.get(key))
            if value:
                clean_append[key] = value
        clean_appends.append(clean_append)

    clean_patch = {"scenes": clean_scenes, "append_scenes": clean_appends}
    if isinstance(raw_drama, dict):
        clean_patch["drama"] = clean_drama
    return clean_patch, dropped


def empty_slot_gap_items(
    scenes: list[SceneCard], chapter: ChapterGoal | None = None, *, plan_owned_scene_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """待补清单的结构化写法（与 :func:`empty_slot_gaps` 同一份判定）：每条 ``{scope, scene_id, scene_label, fields,
    fill_in}``——``fields`` 是 ``[{key, label}]``（中文字段名与前端章节规划同一套叫法），``scene_label`` 是
    「第 N 场 · 题名」（章内第几场），``fill_in`` 说去哪里补：构思侧拥有设计的场是 ``snowflake_step_10``，其余为空。"""
    owned = plan_owned_scene_ids or set()
    items: list[dict[str, Any]] = []
    if chapter is not None:
        drama = dict(dict(chapter.narrative_json or {}).get("drama") or {})
        missing_drama = [key for key in _PATCH_DRAMA_FIELDS if _is_empty_slot(drama.get(key))]
        if missing_drama:
            items.append(
                {
                    "scope": "chapter",
                    "scene_id": None,
                    "scene_label": "章节戏剧卡",
                    "fields": [{"key": key, "label": DRAMA_SLOT_LABELS.get(key, key)} for key in missing_drama],
                    "fill_in": None,
                }
            )
    for position, scene in enumerate(scenes, start=1):
        missing = _missing_scene_slots(scene)
        if missing:
            items.append(
                {
                    "scope": "scene",
                    "scene_id": scene.scene_id,
                    "scene_label": f"第 {position} 场 · {scene_display_title(scene)}",
                    "fields": [{"key": key, "label": SCENE_SLOT_LABELS.get(key, key)} for key in missing],
                    "fill_in": "snowflake_step_10" if scene.scene_id in owned else None,
                }
            )
    return items


def _missing_scene_slots(scene: SceneCard) -> list[str]:
    """一场还空着的三拍槽（按主动 / 反应）与视角。"""
    brief = dict(scene.writer_brief_json or {})
    keys = SCENE_BRIEF_GCS if scene_kind(scene) == "proactive" else SCENE_BRIEF_RDD
    missing = [key for key in keys if _is_empty_slot(brief.get(key))]
    if not scene.pov_character_id:
        missing.append("pov")
    return missing


def empty_slot_gaps(
    scenes: list[SceneCard], chapter: ChapterGoal | None = None, *, plan_owned_scene_ids: set[str] | None = None
) -> list[str]:
    """待补清单（不是 AI）：列出戏剧卡与每张卡还空着的槽，给作者一份可执行的清单。"""
    owned = plan_owned_scene_ids or set()
    gaps: list[str] = []
    if chapter is not None:
        drama = dict(dict(chapter.narrative_json or {}).get("drama") or {})
        missing_drama = [key for key in _PATCH_DRAMA_FIELDS if _is_empty_slot(drama.get(key))]
        if missing_drama:
            gaps.append(f"章节戏剧卡：待补 {', '.join(missing_drama)}")
    for scene in scenes:
        missing = _missing_scene_slots(scene)
        if missing:
            where = "——在构思第 10 步补" if scene.scene_id in owned else ""
            gaps.append(f"{scene_title(scene)}（{scene.scene_id}）：待补 {', '.join(missing)}{where}")
    return gaps


# 旧名：测试从本模块 import
_empty_slot_gaps = empty_slot_gaps


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


def _normalize_candidates_output(
    output: dict[str, Any], scenes: list[SceneCard]
) -> dict[str, Any]:
    known_ids = {scene.scene_id for scene in scenes}
    candidates: list[dict[str, Any]] = []
    for raw in (output.get("candidates") or [])[:3]:
        if not isinstance(raw, dict):
            continue
        plan_items: list[dict[str, Any]] = []
        for item in (raw.get("scene_plan") or [])[:24]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref_scene_id") or "").strip() or None
            if ref is not None and ref not in known_ids:
                ref = None
            kind = (
                "reactive"
                if str(item.get("kind") or "").strip().lower() in {"reactive", "反应"}
                else "proactive"
            )
            brief_keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
            raw_brief = item.get("brief") if isinstance(item.get("brief"), dict) else {}
            plan_items.append(
                {
                    "ref_scene_id": ref,
                    "title": _clean_text(item.get("title"), _MAX_TITLE_CHARS),
                    "kind": kind,
                    "brief": {key: _clean_text(raw_brief.get(key)) for key in brief_keys},
                    "pov_character_name": _clean_text(item.get("pov_character_name"), _MAX_TITLE_CHARS),
                    "exit_change": _clean_text(item.get("exit_change")),
                    "hook": _clean_text(item.get("hook")),
                    "tension_note": _clean_text(item.get("tension_note")),
                }
            )
        if not plan_items:
            continue
        candidates.append(
            {
                "label": _clean_text(raw.get("label"), 24) or f"方向 {len(candidates) + 1}",
                "rationale": _clean_text(raw.get("rationale"), 600),
                "risk": _clean_text(raw.get("risk"), 300),
                "scene_plan": plan_items,
            }
        )
    if not candidates:
        raise ValueError("candidates output must contain at least one usable candidate")
    return {"candidates": candidates}


def _normalize_review_output(
    output: dict[str, Any], scenes: list[SceneCard], *, plan_owned_scene_ids: set[str] | None = None
) -> dict[str, Any]:
    known_ids = {scene.scene_id for scene in scenes}
    findings: list[dict[str, Any]] = []
    for raw in (output.get("findings") or [])[:20]:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        if code not in REVIEW_FINDING_CODES:
            code = "OTHER"
        severity = str(raw.get("severity") or "info").strip().lower()
        if severity not in {"warn", "info"}:
            severity = "info"
        scene_id = str(raw.get("scene_id") or "").strip() or None
        if scene_id is not None and scene_id not in known_ids:
            scene_id = None
        evidence = _clean_text(raw.get("evidence"), 600)
        if not evidence:
            # 无据断言直接丢弃：finding 必须引用注入上下文里的事实。
            continue
        finding: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "scene_id": scene_id,
            "field": _clean_text(raw.get("field"), 60) or None,
            "evidence": evidence,
            "summary": _clean_text(raw.get("summary"), 300),
        }
        suggestion = raw.get("suggestion_patch")
        if isinstance(suggestion, dict) and suggestion:
            clean_patch, _ = sanitize_plan_patch(scenes, suggestion, plan_owned_scene_ids=plan_owned_scene_ids)
            if clean_patch["scenes"] or clean_patch["append_scenes"]:
                finding["suggestion_patch"] = clean_patch
        findings.append(finding)
    return {"findings": findings}


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


def _coerce_notes(value: Any) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    for item in (value or [])[:20]:
        if isinstance(item, dict):
            suggestion = _clean_text(item.get("suggestion"), 300)
            if not suggestion:
                continue
            notes.append(
                {
                    "scene_id": str(item.get("scene_id") or "").strip(),
                    "field": _clean_text(item.get("field"), 60),
                    "suggestion": suggestion,
                    "reason": _clean_text(item.get("reason"), 300),
                }
            )
        elif isinstance(item, str) and item.strip():
            notes.append({"scene_id": "", "field": "", "suggestion": _clean_text(item, 300), "reason": ""})
    return notes

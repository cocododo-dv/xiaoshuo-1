"""章节编排的 LLM 规划服务（chapter plan）。

四条能力（设计文档 docs/chapter-arrangement-llm-design-2026-07-16.md §4/§5）：
- 章节蓝图显式化：读 / 作者改写 / 显式重生成（复用 chapter_story_architecture 节点与
  GenerationPlanningArtifact 表；作者版 llm_call_id=None，场景 run 的
  ensure_scene_planning 会自动复用最新 active 行）。
- candidates：3 个结构策略互斥的整章编排候选（无状态咨询，不落库）。
- fill：保真补全 —— 只产出「填空」补丁；覆盖型意见降级为 notes。
- review：编排体检 findings（带 evidence 与可选单条填空建议）。
- apply：补丁经服务端 sanitize 后在单事务内经 CatalogService 原子回写目录。

铁律（服务端强制，不信任模型自律）：只填空、按 scene_id 对位、新卡只追加、
不删除、不覆盖作者非空文本。锁章由 chapter_approval.require_chapter_mutation_allowed
统一 409。LLM 调用走与雪花工作区同款的 execute_accounted_call 计量/审计路径。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    GenerationPlanningArtifact,
    LlmCall,
    SceneCard,
)
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.catalog import (
    CatalogService,
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    scene_kind,
    scene_title,
)
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.chapter_planning_context import (
    CHAPTER_ARCHITECTURE_ARTIFACT,
    STYLE_REFERENCE_SLOT,
    ChapterPlanningContext,
    ChapterPlanningContextBuilder,
    latest_chapter_architecture,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json, normalize
from novel_system.services.llm_accounting import (
    LLMCallContext,
    execute_accounted_call,
    mark_postprocess_failure,
)
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.llm_client import (
    LLMClient,
    LLMConfigurationError,
    build_llm_request,
    load_model_routing_config,
    resolve_node_route,
)
from novel_system.services.prompt_builder import (
    PromptConfigurationError,
    load_prompt_templates,
)
from novel_system.services.system_config import load_llm_provider_runtime_configs
from novel_system.settings import get_settings

ARCHITECTURE_FIELDS = (
    "chapter_promise",
    "escalation_path",
    "reveal_plan",
    "payoff_target",
    "character_shift",
    "ending_question",
)
_ARCHITECTURE_LIST_FIELDS = {"escalation_path", "reveal_plan"}

# 填空补丁允许触碰的场景字段（brief 键按 kind 另行校验）。
_PATCH_SCENE_FIELDS = ("title", "pov_character_name", "exit_change", "hook")
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

REVIEW_FINDING_CODES = (
    "PROMISE_UNGROUNDED",
    "SCENE_FUNCTION_DUPLICATE",
    "REACTIVE_MISSING",
    "TENSION_FLAT",
    "FORESHADOW_OVERDUE",
    "POV_FATIGUE",
    "HANDOFF_MISMATCH",
    "EXIT_NO_CHANGE",
    "BRIEF_INCOMPLETE",
    "OTHER",
)


class ChapterPlanService:
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
        self._catalog = CatalogService(session)
        self._context_builder = ChapterPlanningContextBuilder(session)

    # ---------- 章节蓝图（一等公民） ----------

    def get_architecture(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        self._require_chapter(project_id, chapter_id)
        artifact = latest_chapter_architecture(self.session, chapter_id)
        return {"architecture": _serialize_architecture(artifact)}

    def generate_architecture(
        self, project_id: str, chapter_id: str, *, actor_ref: str = "operator"
    ) -> dict[str, Any]:
        chapter = self._require_chapter(project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["chapter_story_architecture"],
            operation="chapter_plan.generate_architecture",
        )
        context = self._context_builder.build(project_id, chapter_id)
        if not self._llm_enabled():
            # 显式生成不落占位蓝图（占位会被场景 run 当真注入），只引导去配置。
            return {
                "source": "fallback",
                "architecture": _serialize_architecture(
                    latest_chapter_architecture(self.session, chapter_id)
                ),
                "author_action": self._llm_action(),
                "degraded_slots": context.degraded_slots,
            }
        payload = self._run_structured_task(
            task_key=CHAPTER_ARCHITECTURE_ARTIFACT,
            template_name=CHAPTER_ARCHITECTURE_ARTIFACT,
            project_id=project_id,
            step_ref=f"chapter_plan:architecture:{chapter_id}",
            prompt_payload=context.prompt_payload,
            normalize_output=_normalize_architecture_payload,
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
            "architecture": _serialize_architecture(artifact),
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
        chapter = self._require_chapter(project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["chapter_story_architecture"],
            operation="chapter_plan.put_architecture",
        )
        body = _normalize_architecture_payload(dict(payload or {}))
        if not str(body.get("chapter_promise") or "").strip():
            raise DomainError(
                "CHAPTER_ARCHITECTURE_PROMISE_REQUIRED",
                "chapter_promise is required",
                status_code=400,
            )
        artifact = self._persist_architecture(
            chapter,
            payload=body,
            llm_call_id=None,
            actor_ref=actor_ref or "author",
            context=None,
        )
        return {"architecture": _serialize_architecture(artifact)}

    def _persist_architecture(
        self,
        chapter: ChapterGoal,
        *,
        payload: dict[str, Any],
        llm_call_id: str | None,
        actor_ref: str,
        context: ChapterPlanningContext | None,
    ) -> GenerationPlanningArtifact:
        for row in self.session.query(GenerationPlanningArtifact).filter(
            GenerationPlanningArtifact.artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT,
            GenerationPlanningArtifact.object_type == "chapter",
            GenerationPlanningArtifact.object_id == chapter.chapter_id,
            GenerationPlanningArtifact.status == "active",
        ):
            row.status = "superseded"
        artifact = GenerationPlanningArtifact(
            row_id=f"planning_{CHAPTER_ARCHITECTURE_ARTIFACT}_{chapter.chapter_id}_{uuid.uuid4().hex[:10]}",
            artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            payload_json=payload,
            llm_call_id=llm_call_id,
            source_bundle_id=None,
            source_bundle_hash=context.context_fingerprint if context else None,
            status="active",
            created_by=actor_ref or "chapter_plan",
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    # ---------- candidates（发散通道） ----------

    def candidates(
        self, project_id: str, chapter_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_chapter(project_id, chapter_id)
        context = self._context_builder.build(project_id, chapter_id)
        if not self._llm_enabled():
            return {
                "source": "fallback",
                "candidates": [],
                "author_action": self._llm_action(),
                "degraded_slots": context.degraded_slots,
            }
        prompt_payload = dict(context.prompt_payload)
        hint = str((body or {}).get("direction_hint") or "").strip()
        if hint:
            prompt_payload["direction_hint"] = hint[:300]
        result = self._run_structured_task(
            task_key="chapter_scene_plan_candidates",
            template_name="chapter_scene_plan_candidates",
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
        self._require_chapter(project_id, chapter_id)
        context = self._context_builder.build(project_id, chapter_id)
        body = body or {}
        mode = str(body.get("mode") or "fill").strip().lower()
        if mode not in {"fill", "adopt"}:
            raise DomainError("CHAPTER_PLAN_MODE_INVALID", "mode must be fill or adopt", status_code=400)
        if not self._llm_enabled():
            return {
                "source": "fallback",
                "patch": {"drama": {}, "scenes": [], "append_scenes": []},
                "notes": [],
                "gaps": _empty_slot_gaps(context.scenes, context.chapter),
                "dropped": [],
                "author_action": self._llm_action(),
                "degraded_slots": context.degraded_slots,
            }
        prompt_payload = dict(context.prompt_payload)
        prompt_payload["mode"] = mode
        if mode == "adopt":
            candidate = body.get("candidate")
            if not isinstance(candidate, dict) or not candidate:
                raise DomainError(
                    "CHAPTER_PLAN_CANDIDATE_REQUIRED",
                    "adopt mode requires the chosen candidate object",
                    status_code=400,
                )
            prompt_payload["adopted_candidate"] = normalize(candidate)
        result = self._run_structured_task(
            task_key="chapter_scene_plan_fill",
            template_name="chapter_scene_plan_fill",
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
        self._require_chapter(project_id, chapter_id)
        context = self._context_builder.build(project_id, chapter_id)
        if not self._llm_enabled():
            return {
                "source": "fallback",
                "findings": _rule_based_findings(context),
                "author_action": self._llm_action(),
                "degraded_slots": context.degraded_slots,
            }
        result = self._run_structured_task(
            task_key="chapter_plan_review",
            template_name="chapter_plan_review",
            project_id=project_id,
            step_ref=f"chapter_plan:review:{chapter_id}",
            prompt_payload=context.prompt_payload,
            normalize_output=lambda output: _normalize_review_output(output, context.scenes),
        )
        return {
            "source": "llm",
            "llm_call_id": result["llm_call_id"],
            "degraded_slots": context.degraded_slots,
            "context_fingerprint": context.context_fingerprint,
            **result["output"],
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
        chapter = self._require_chapter(project_id, chapter_id)
        scenes = self._catalog.scene_rows(chapter_id)
        patch, dropped = sanitize_plan_patch(
            scenes,
            (body or {}).get("patch"),
            chapter=chapter,
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
        tree = self._catalog.catalog(project_id)
        chapter_payload = next(
            (item for item in tree["chapters"] if item["chapter_id"] == chapter_id),
            None,
        )
        return {
            "applied": {
                "drama": len(drama_updates),
                "scenes": applied_scenes,
                "appended": appended,
            },
            "skipped": skipped,
            "chapter": chapter_payload,
        }

    # ---------- LLM plumbing（与雪花工作区同款计量/审计路径） ----------

    def llm_enabled(self) -> bool:
        return self._llm_enabled()

    def _llm_enabled(self) -> bool:
        return bool(self._settings_payload().llm_enabled)

    def _llm_action(self) -> dict[str, Any]:
        settings = self._settings_payload()
        return llm_setup_action(
            llm_enabled=bool(settings.llm_enabled),
            generation_mode="chapter_plan",
        )

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
                    else f"chapter plan LLM prompt or route is not ready: {task_key}。"
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

        user_prompt = _render_user_prompt(template, prompt_payload)
        prompt_hash = _prompt_hash(
            template_name,
            template.version,
            template.system_prompt,
            user_prompt,
            template.structured_schema,
        )
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
                "CHAPTER_PLAN_LLM_CALL_FAILED",
                f"chapter plan LLM call failed for {task_key}: {exc}",
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
            raise DomainError(
                "CHAPTER_PLAN_LLM_RESPONSE_INVALID_SCHEMA",
                str(exc),
                status_code=409,
                details={
                    "llm_call_id": llm_call_id,
                    "node_id": task_key,
                    "error_code": "LLM_RESPONSE_INVALID_SCHEMA",
                    "next_action": "retry_or_adjust_prompt_schema",
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
        return {
            "llm_call_id": response.llm_call_id or llm_call_id,
            "output": normalized_output,
        }

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
            raise RuntimeError(f"accounted chapter plan call {llm_call_id} is missing")
        parent.prompt_hash = prompt_hash
        parent.request_payload_summary = sanitize_audit_summary(
            {**dict(parent.request_payload_summary or {}), **request_summary}
        )
        parent.response_payload_summary = sanitize_audit_summary(
            {**dict(parent.response_payload_summary or {}), **response_summary}
        )
        self.session.commit()

    # ---------- helpers ----------

    def _require_chapter(self, project_id: str, chapter_id: str) -> ChapterGoal:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id != project_id or chapter.trashed_flag:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)
        return chapter


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
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把 LLM 补丁裁剪成「只填空」的安全子集。

    返回 (clean_patch, dropped)；dropped 逐条记录被拒写入及原因，供 UI 展示。
    性质：不覆盖非空、不删除、不重排、新卡只追加且有上限、未知 scene_id 丢弃。
    """
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


def _empty_slot_gaps(
    scenes: list[SceneCard], chapter: ChapterGoal | None = None
) -> list[str]:
    """离线降级：列出每张卡待补的空槽，让 UI 依然给出可执行清单。"""
    gaps: list[str] = []
    if chapter is not None:
        drama = dict(dict(chapter.narrative_json or {}).get("drama") or {})
        missing_drama = [key for key in _PATCH_DRAMA_FIELDS if _is_empty_slot(drama.get(key))]
        if missing_drama:
            gaps.append(f"章节戏剧卡：待补 {', '.join(missing_drama)}")
    for scene in scenes:
        kind = scene_kind(scene)
        brief = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        missing = [key for key in keys if _is_empty_slot(brief.get(key))]
        if not scene.pov_character_id:
            missing.append("pov")
        if missing:
            gaps.append(f"{scene_title(scene)}（{scene.scene_id}）：待补 {', '.join(missing)}")
    return gaps


def _normalize_architecture_payload(output: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ARCHITECTURE_FIELDS:
        value = output.get(key)
        if key in _ARCHITECTURE_LIST_FIELDS:
            items = value if isinstance(value, list) else ([value] if value else [])
            payload[key] = [
                _clean_text(item, _MAX_FIELD_CHARS) for item in items if _clean_text(item)
            ][:8]
        else:
            payload[key] = _clean_text(value, _MAX_FIELD_CHARS)
    return payload


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
    output: dict[str, Any], scenes: list[SceneCard]
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
            clean_patch, _ = sanitize_plan_patch(scenes, suggestion)
            if clean_patch["scenes"] or clean_patch["append_scenes"]:
                finding["suggestion_patch"] = clean_patch
        findings.append(finding)
    return {"findings": findings}


def _rule_based_findings(context: ChapterPlanningContext) -> list[dict[str, Any]]:
    """离线降级体检：不调用 LLM 也给出可执行的结构性提示。"""
    findings: list[dict[str, Any]] = []
    chapter_card = context.prompt_payload.get("chapter_card") or {}
    drama = dict(chapter_card.get("drama") or {})
    missing_drama = [
        key
        for key in ("promise", "spine", "arc", "problem", "aftertaste", "ending")
        if _is_empty_slot(drama.get(key))
    ]
    if missing_drama:
        findings.append(
            {
                "code": "PROMISE_UNGROUNDED",
                "severity": "warn",
                "scene_id": None,
                "field": "drama",
                "evidence": f"戏剧卡缺 {len(missing_drama)} 项：{', '.join(missing_drama)}",
                "summary": "戏剧卡未填完整，场景规划缺少章级承诺锚点。",
            }
        )
    reactive_count = 0
    for scene in context.scenes:
        kind = scene_kind(scene)
        if kind == "reactive":
            reactive_count += 1
        brief = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        missing = [key for key in keys if _is_empty_slot(brief.get(key))]
        if missing:
            findings.append(
                {
                    "code": "BRIEF_INCOMPLETE",
                    "severity": "warn",
                    "scene_id": scene.scene_id,
                    "field": ",".join(missing),
                    "evidence": f"「{scene_title(scene)}」三拍缺：{', '.join(missing)}",
                    "summary": "场景三拍不完整，起草契约会被阻断或退化。",
                }
            )
        if not scene.pov_character_id:
            findings.append(
                {
                    "code": "BRIEF_INCOMPLETE",
                    "severity": "warn",
                    "scene_id": scene.scene_id,
                    "field": "pov",
                    "evidence": f"「{scene_title(scene)}」未设 POV 角色。",
                    "summary": "缺 POV 会阻断场景执行契约。",
                }
            )
    if context.scenes and reactive_count == 0 and len(context.scenes) >= 3:
        findings.append(
            {
                "code": "REACTIVE_MISSING",
                "severity": "info",
                "scene_id": None,
                "field": None,
                "evidence": f"本章 {len(context.scenes)} 场全部为主动场。",
                "summary": "连续主动场没有喘息拍，考虑安排一场反应场消化代价。",
            }
        )
    return findings


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

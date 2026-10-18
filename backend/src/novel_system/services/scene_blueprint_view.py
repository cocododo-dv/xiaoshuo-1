"""AI 写前笔记：作者在 AI 起草台看到、能改的场景蓝图与人物压力（2026-10-16 规划层收拢 · 第 1 步）。

起草前，场景运行先做两份「这一场怎么演」的规划——场景蓝图（``scene_blueprint.SceneBlueprintService``）与人物压力
（``near_final_planning`` 的 ``character_pressure_blueprint``）——起草提示把它们和第 10 步的场景结构一起读。以前作者
看不见它们，而蓝图会在第 10 步之外添东西（给主动场加两难、加关系转折与意象）。这里把它们摆出来：

- 读（:meth:`SceneBlueprintViewService.view`）：蓝图逐格标来源——``plan``（落地第 10 步写下的那一格）、``ai``（第 10 步里
  没有，AI 加的）或 ``author``（作者的版本）。按格子与这一场第 10 步实际写了什么判，不调模型
  （:func:`blueprint_field_origins`）。人物压力整份是 AI 从人物档案推的，只读。
- 改成我的版本（:meth:`save_author_version`）：``llm_call_id`` 为空、来源记成作者的一行
  （``planning_queries.is_author_scene_blueprint``）。起草优先用它（``SceneBlueprintService.reusable``），设计 / 绑定再变
  也不作废，只提示「设计改过」（``scene_planning_staleness``）。
- 恢复 AI 版（:meth:`restore_ai_version`）：我的版本让位；保存时被它顶下去的那份 AI 蓝图如果之后设计没再变过，就回来，
  否则这一场暂时没有笔记，下一次起草（或「重做」）现做。
- 重做（:meth:`regenerate`）：蓝图与人物压力都按现在的设计重做（两次模型调用；本章还没有章节蓝图时起草本来也要先做
  一份，这里同样会做）。我的版本也随之让位。
- 写后对照（``plan_check``，2026-10-18）：最近一次准终稿评审对这一场场景结构的逐条复核（``services/plan_check.py``），
  评的是 AI 交出的准终稿；作者之后在写作台改过的地方不在里面。没有评审 / 没有场景结构 → ``None``。

起草任务在跑时不许改、不许重做（409 ``RUN_JOB_IN_PROGRESS``）；批准过的章锁着（``chapter_approval``）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    GenerationPlanningArtifact,
    OperationLog,
    SceneBlueprint,
    SceneCard,
    SceneRunState,
)
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.near_final_planning import (
    CHARACTER_PRESSURE_ARTIFACT,
    CHARACTER_PRESSURE_FIELDS,
    NearFinalPlanningService,
)
from novel_system.services.planning_queries import (
    is_author_scene_blueprint,
    latest_active_planning_artifact,
)
from novel_system.services.scene_blueprint import (
    SCENE_BLUEPRINT_FACT_FIELDS,
    SCENE_BLUEPRINT_FIELDS,
    SceneBlueprintService,
    blueprint_situation_tags,
    is_facts_blueprint,
)
from novel_system.services.scene_form import scene_form, text, writer_brief
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.scene_planning_staleness import scene_blueprint_design_changed_since

#: 保存我的版本时记一条：被它顶下去的是哪一份 AI 蓝图（「恢复 AI 版」据此找回）
AUTHOR_BLUEPRINT_SAVED_EVENT = "scene_blueprint_author_version_saved"
#: 我的版本每一格的字数上限
MAX_FIELD_CHARS = 2_000


def blueprint_field_origins(scene: SceneCard, blueprint_json: dict[str, Any]) -> dict[str, str]:
    """蓝图每一格的来源：``plan`` = 第 10 步写下的那一格落地而来；``ai`` = 第 10 步里没有，AI 加的。

    对照（主动场 / 反应场）：可见欲望 ← 目标 / 决定（或反应）；被迫选择 ← 两难（主动场只有接着反应三拍时才有）；
    代价 ← 代价；结尾动作 / 结尾完成什么 ← 挫折 / 决定（或接着那组三拍的收尾拍）；下一场牵引 ← 钩子。
    信息释放、关系转折、意象、反总结规则第 10 步没有对应的格子，一律算 AI 加的。
    """
    brief = writer_brief(scene)

    def filled(*keys: str) -> bool:
        return any(text(brief.get(key)) for key in keys)

    reactive = scene_form(scene) == "reactive"
    closing = filled("decision", "setback")
    grounded = {
        "visible_desire": filled("decision", "reaction") if reactive else filled("goal"),
        "forced_choice": filled("dilemma"),
        "price_paid": filled("cost_requirement"),
        "ending_action": closing,
        "ending_function": closing,
        "next_scene_pull": filled("next_scene_pull", "hook") or bool(text(getattr(scene, "hook", None))),
    }
    return {key: ("plan" if grounded.get(key) else "ai") for key in blueprint_json if key != "situation_tags"}


def _variant_fields(blueprint_json: dict[str, Any]) -> tuple[str, ...]:
    return SCENE_BLUEPRINT_FACT_FIELDS if is_facts_blueprint(blueprint_json) else SCENE_BLUEPRINT_FIELDS


class SceneBlueprintViewService:
    def __init__(self, session: Session, *, llm_client: Any | None = None) -> None:
        self.session = session
        self._llm_client = llm_client

    # ---------- 读 ----------

    def view(self, scene_id: str) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True, with_parents=True)
        return self._view(scene)

    def _view(self, scene: SceneCard) -> dict[str, Any]:
        row = SceneBlueprintService(self.session).latest(scene.scene_id)
        pressure = latest_active_planning_artifact(
            self.session,
            artifact_type=CHARACTER_PRESSURE_ARTIFACT,
            object_type="scene",
            object_id=scene.scene_id,
        )
        return {
            "scene_id": scene.scene_id,
            "scene_form": scene_form(scene) or "proactive",
            "running": self._running(scene.scene_id),
            "blueprint": self._blueprint_view(scene, row),
            "character_pressure": _pressure_view(pressure),
            "plan_check": self._plan_check(scene.scene_id),
        }

    def _plan_check(self, scene_id: str) -> dict[str, Any] | None:
        """最近一次准终稿评审的逐条复核（随评审的尝试记录存）；那一次没有逐条结果 → None。"""
        attempt = self.session.execute(
            select(AttemptTracker)
            .where(AttemptTracker.scene_id == scene_id, AttemptTracker.step == "near_final_acceptance_review")
            .order_by(AttemptTracker.attempt_id.desc())
        ).scalars().first()
        details = attempt.details_json if attempt is not None and isinstance(attempt.details_json, dict) else {}
        check = details.get("plan_check")
        if not isinstance(check, dict):
            return None
        return {**check, "reviewed_at": attempt.created_at}

    def _blueprint_view(self, scene: SceneCard, row: SceneBlueprint | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row.blueprint_json or {})
        author = is_author_scene_blueprint(row)
        origins = {} if author else blueprint_field_origins(scene, payload)
        fields = [
            {"key": key, "value": text(payload.get(key)), "origin": "author" if author else origins.get(key, "ai")}
            for key in _variant_fields(payload)
            if key in payload or author
        ]
        return {
            "row_id": row.row_id,
            "created_at": row.created_at,
            "source": "author" if author else "ai",
            "variant": "facts" if is_facts_blueprint(payload) else "craft",
            "fields": fields,
            "situation_tags": blueprint_situation_tags(payload),
            "design_changed": scene_blueprint_design_changed_since(self.session, row),
        }

    def _running(self, scene_id: str) -> bool:
        state = self.session.get(SceneRunState, scene_id)
        return bool(state is not None and state.active_run_job_id)

    # ---------- 写 ----------

    def save_author_version(self, scene_id: str, fields: dict[str, str], *, actor_ref: str = "author") -> dict[str, Any]:
        scene = self._writable_scene(scene_id, operation="scene_blueprint.save_author_version")
        blueprints = SceneBlueprintService(self.session)
        current = blueprints.latest(scene.scene_id)
        base = dict(current.blueprint_json or {}) if current is not None else {}
        allowed = _variant_fields(base)
        unknown = sorted(key for key in fields if key not in allowed)
        if unknown:
            raise DomainError(
                "SCENE_BLUEPRINT_FIELD_UNKNOWN",
                "写前笔记里没有这几格：" + "、".join(unknown),
                status_code=400,
                details={"fields": unknown, "allowed": list(allowed)},
            )
        payload = {key: text(fields.get(key, base.get(key)))[:MAX_FIELD_CHARS] for key in allowed}
        if not any(payload.values()):
            raise DomainError(
                "SCENE_BLUEPRINT_EMPTY",
                "我的版本至少要写一格。",
                status_code=400,
            )
        if "situation_tags" in base:
            # 场面标签不归作者改（风格参考按它挑原文），照抄当前那一份
            payload["situation_tags"] = base["situation_tags"]
        replaced = current.row_id if current is not None and not is_author_scene_blueprint(current) else None
        if replaced is None and is_author_scene_blueprint(current):
            # 改的是我的版本：被它顶下去的 AI 蓝图还是最初那一份——除非那之后设计又变过（那份 AI 蓝图已经过时）
            if scene_blueprint_design_changed_since(self.session, current) is None:
                replaced = self._replaced_ai_row_id(current)
        row = blueprints.save_author_version(scene, payload)
        self.session.add(
            OperationLog(
                event_type=AUTHOR_BLUEPRINT_SAVED_EVENT,
                object_type="scene_blueprint",
                object_ref=row.row_id,
                payload_json={"scene_id": scene.scene_id, "replaced_row_id": replaced, "actor_ref": actor_ref},
            )
        )
        self.session.flush()
        return self._view(scene)

    def restore_ai_version(self, scene_id: str, *, actor_ref: str = "author") -> dict[str, Any]:
        scene = self._writable_scene(scene_id, operation="scene_blueprint.restore_ai_version")
        blueprints = SceneBlueprintService(self.session)
        current = blueprints.latest(scene.scene_id)
        if not is_author_scene_blueprint(current):
            return self._view(scene)
        replaced_id = self._replaced_ai_row_id(current)
        design_changed = scene_blueprint_design_changed_since(self.session, current) is not None
        blueprints.retire_live(scene.scene_id)
        replaced = self.session.get(SceneBlueprint, replaced_id) if replaced_id else None
        if replaced is not None and not design_changed and replaced.status == "superseded":
            replaced.status = "accepted"
            self.session.flush()
        return self._view(scene)

    def regenerate(self, scene_id: str, *, actor_ref: str = "author") -> dict[str, Any]:
        scene = self._writable_scene(scene_id, operation="scene_blueprint.regenerate")
        SceneBlueprintService(self.session, llm_client=self._llm_client).generate(scene.scene_id, actor_ref=actor_ref)
        # 人物压力跟着重做：它的来源快照读这一场的蓝图
        for row in self.session.execute(
            select(GenerationPlanningArtifact).where(
                GenerationPlanningArtifact.artifact_type == CHARACTER_PRESSURE_ARTIFACT,
                GenerationPlanningArtifact.object_type == "scene",
                GenerationPlanningArtifact.object_id == scene.scene_id,
                GenerationPlanningArtifact.status == "active",
            )
        ).scalars().all():
            row.status = "superseded"
        self.session.flush()
        NearFinalPlanningService(self.session, llm_client=self._llm_client).ensure_scene_planning(
            scene.scene_id, actor_ref=actor_ref
        )
        return self._view(scene)

    # ---------- 守卫与查找 ----------

    def _writable_scene(self, scene_id: str, *, operation: str) -> SceneCard:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True, with_parents=True)
        chapter = require_chapter(self.session, scene.chapter_id)
        require_chapter_mutation_allowed(
            self.session, chapter, changed_fields=["scene_blueprint"], operation=operation
        )
        if self._running(scene.scene_id):
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "这一场已经有一个起草任务在运行，请等它结束。",
                status_code=409,
                details={"scene_id": scene.scene_id},
            )
        return scene

    def _replaced_ai_row_id(self, author_row: SceneBlueprint) -> str | None:
        event = self.session.execute(
            select(OperationLog)
            .where(
                OperationLog.event_type == AUTHOR_BLUEPRINT_SAVED_EVENT,
                OperationLog.object_ref == author_row.row_id,
            )
            .order_by(OperationLog.operation_id.desc())
        ).scalars().first()
        replaced = (event.payload_json or {}).get("replaced_row_id") if event is not None else None
        return str(replaced) if replaced else None


def _pressure_view(artifact: GenerationPlanningArtifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    payload = dict(artifact.payload_json or {})
    return {
        "row_id": artifact.row_id,
        "created_at": artifact.created_at,
        "fields": [{"key": key, "value": text(payload.get(key))} for key in CHARACTER_PRESSURE_FIELDS if key in payload],
    }


__all__ = [
    "AUTHOR_BLUEPRINT_SAVED_EVENT",
    "MAX_FIELD_CHARS",
    "SceneBlueprintViewService",
    "blueprint_field_origins",
]

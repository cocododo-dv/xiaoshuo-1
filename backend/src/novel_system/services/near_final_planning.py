"""场景运行的规划产物：章架构（每章一份）与人物压力（每场一份）（B03-21 从 near_final 拆出）。

编排器的规划步骤调用 ``ensure_scene_planning``：已有生效的产物就复用（续跑时由检查点交回），没有才生成——
两份产物的生成只差产物类型、对象、错误码与归一化函数，由 ``_PLANNING_ARTIFACT_SPECS`` 一张表驱动。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property, partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneCard,
)
from novel_system.services.chapter_architecture import (
    ARCHITECTURE_FIELDS,
    CHAPTER_ARCHITECTURE_ARTIFACT,
    latest_chapter_architecture,
    normalize_chapter_architecture,
    persist_chapter_architecture,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_normalized
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.planning_queries import latest_active_planning_artifact, latest_scene_blueprint
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import active_chapter_scenes, require_chapter, require_scene
from novel_system.services.scene_sections import attach_scene_sections
from novel_system.services.style_reference.planning_context import (
    build_planning_style_reference,
    register_planning_style_reference,
    style_reference_prompt_blocks,
)
from novel_system.services.writer_briefs import normalize_chapter_writer_brief, normalize_scene_writer_brief

CHARACTER_PRESSURE_ARTIFACT = "character_pressure_blueprint"

CHARACTER_PRESSURE_FIELDS = (
    "surface_goal",
    "hidden_fear",
    "wrong_belief",
    "shame_point",
    "avoidance_strategy",
    "relationship_debt",
    "current_mask",
)
# 章架构的常量、字段表、读法、落库与归一只有一处（``services/chapter_architecture``，B07-11）；旧名照旧可以从这里 import。
CHAPTER_ARCHITECTURE_FIELDS = ARCHITECTURE_FIELDS

def _planning_user_prompt(
    base_prompt: str,
    *,
    scene: SceneCard,
    chapter: ChapterGoal,
    source: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        [
            base_prompt,
            *style_reference_prompt_blocks(source),
            "",
            "## Planning Target",
            f"Scene ID: {scene.scene_id}",
            f"Chapter ID: {chapter.chapter_id}",
            f"POV Character ID: {scene.pov_character_id or ''}",
        ]
    ).strip()


def _normalize_character_pressure_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    # 只要求规范字段齐全且非空；多余字段忽略——response_format 是 json_object
    # （非严格 schema），真实模型可能多返解释性键，不应因此判整份产物失败。
    missing = [field for field in CHARACTER_PRESSURE_FIELDS if field not in payload]
    if missing:
        raise ValueError(
            "character pressure payload is missing required fields: " + ", ".join(missing)
        )
    normalized: dict[str, str] = {}
    for field in CHARACTER_PRESSURE_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        normalized[field] = value.strip()
    return normalized


@dataclass(frozen=True)
class _PlanningArtifactSpec:
    """一份规划产物的生成口径（节点 id = 产物类型 = 提示词模板名）。"""

    key: str  # ensure_scene_planning 的结果键 / 检查点里的产物键
    artifact_type: str
    object_type: str  # "chapter"：每章一份；"scene"：每场一份
    include_chapter_architecture: bool  # 来源快照带不带本章的章架构
    capability_code: str
    failure_code: str
    invalid_code: str
    operation: str
    next_action: str
    normalizer: Callable[[Any], dict[str, Any]]

    def object_id(self, scene: SceneCard, chapter: ChapterGoal) -> str:
        return chapter.chapter_id if self.object_type == "chapter" else scene.scene_id


_PLANNING_ARTIFACT_SPECS: tuple[_PlanningArtifactSpec, ...] = (
    _PlanningArtifactSpec(
        key="chapter_architecture",
        artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
        object_type="chapter",
        include_chapter_architecture=False,
        capability_code="CHAPTER_STORY_ARCHITECTURE_LLM_REQUIRED",
        failure_code="CHAPTER_STORY_ARCHITECTURE_FAILED",
        invalid_code="CHAPTER_STORY_ARCHITECTURE_OUTPUT_INVALID",
        operation="chapter architecture generation",
        next_action="configure_chapter_story_architecture_route_and_retry",
        # 场景运行现做的章架构：严格归一（六个字段缺一个、有空值都报 ValueError；多余字段忽略——json_object 非严格 schema）
        normalizer=partial(normalize_chapter_architecture, strict=True),
    ),
    _PlanningArtifactSpec(
        key="character_pressure",
        artifact_type=CHARACTER_PRESSURE_ARTIFACT,
        object_type="scene",
        include_chapter_architecture=True,
        capability_code="CHARACTER_PRESSURE_BLUEPRINT_LLM_REQUIRED",
        failure_code="CHARACTER_PRESSURE_BLUEPRINT_FAILED",
        invalid_code="CHARACTER_PRESSURE_BLUEPRINT_OUTPUT_INVALID",
        operation="character pressure generation",
        next_action="configure_character_pressure_blueprint_route_and_retry",
        normalizer=_normalize_character_pressure_payload,
    ),
)


class NearFinalPlanningService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self._llm_client = llm_client
        if llm_runner is not None:
            self._llm_runner = llm_runner

    # 提示词装配与 LLM 运行器第一次用到时才建：只读路径（工作台摘要、最新一版读取）一次都用不到，
    # 不该为它们读提示词与运行时配置。测试照旧可以直接给实例的这两个属性赋值。
    @cached_property
    def prompt_builder(self) -> PromptBuilder:
        return PromptBuilder()

    @cached_property
    def _llm_runner(self) -> LLMNodeRunner:
        return LLMNodeRunner(self.session, llm_client=self._llm_client)

    def ensure_scene_planning(
        self,
        scene_id: str,
        actor_ref: str = "operator",
        *,
        step_reconciler: Callable[[str], None] | None = None,
        artifact_committed: Callable[[str, dict[str, Any], bool], None] | None = None,
        resume_artifacts: dict[str, GenerationPlanningArtifact] | None = None,
    ) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id)
        chapter = require_chapter(self.session, scene.chapter_id)
        resume_artifacts = resume_artifacts or {}
        planned: dict[str, dict[str, Any]] = {}
        # 章架构在前：人物压力的来源快照要带上本章的章架构
        for spec in _PLANNING_ARTIFACT_SPECS:
            artifact = resume_artifacts.get(spec.key)
            if artifact is None:
                artifact = self._latest_artifact(
                    artifact_type=spec.artifact_type,
                    object_type=spec.object_type,
                    object_id=spec.object_id(scene, chapter),
                )
            reused = artifact is not None
            if artifact is None:
                step_key = f"planning:{spec.key}"
                if step_reconciler is not None:
                    step_reconciler(step_key)
                artifact = self._generate_artifact(
                    spec,
                    scene=scene,
                    chapter=chapter,
                    actor_ref=actor_ref,
                    execution_step_key=step_key,
                )
            if artifact_committed is not None:
                artifact_committed(spec.key, self.serialize_artifact(artifact), reused)
            planned[spec.key] = self.serialize_artifact(artifact)
        return planned

    @staticmethod
    def serialize_artifact(artifact: GenerationPlanningArtifact) -> dict[str, Any]:
        return {
            "row_id": artifact.row_id,
            "artifact_type": artifact.artifact_type,
            "object_type": artifact.object_type,
            "object_id": artifact.object_id,
            "chapter_id": artifact.chapter_id,
            "scene_id": artifact.scene_id,
            "payload": artifact.payload_json or {},
            "llm_call_id": artifact.llm_call_id,
            "source_bundle_id": artifact.source_bundle_id,
            "source_bundle_hash": artifact.source_bundle_hash,
            "status": artifact.status,
            "created_at": artifact.created_at,
        }

    def _generate_artifact(
        self,
        spec: _PlanningArtifactSpec,
        *,
        scene: SceneCard,
        chapter: ChapterGoal,
        actor_ref: str,
        execution_step_key: str | None = None,
    ) -> GenerationPlanningArtifact:
        source = self._source_snapshot(
            scene=scene, chapter=chapter, include_chapter_architecture=spec.include_chapter_architecture
        )
        prompt = self.prompt_builder.build(source["snapshot"], spec.artifact_type)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=chapter.chapter_id,
                bundle_id=source["source_bundle_id"],
                bundle_hash=source["source_bundle_hash"],
                node_id=spec.artifact_type,
                step=spec.artifact_type,
                prompt=prompt,
                user_prompt=_planning_user_prompt(prompt["user_prompt"], scene=scene, chapter=chapter, source=source),
                execution_step_key=execution_step_key,
            )
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code=spec.capability_code,
                failure_code=spec.failure_code,
                operation=spec.operation,
                node_id=spec.artifact_type,
                next_action=spec.next_action,
            )
        try:
            payload = spec.normalizer(node_result.response.structured_output)
        except ValueError as exc:
            raise DomainError(
                spec.invalid_code,
                f"{spec.operation} returned an invalid payload: {exc}",
                status_code=502,
                details={"llm_call_id": node_result.llm_call_id, "node_id": spec.artifact_type},
            ) from exc
        return self._persist_artifact(
            artifact_type=spec.artifact_type,
            object_type=spec.object_type,
            object_id=spec.object_id(scene, chapter),
            chapter_id=chapter.chapter_id,
            scene_id=scene.scene_id if spec.object_type == "scene" else None,
            payload=payload,
            llm_call_id=node_result.llm_call_id,
            source_bundle_id=source["source_bundle_id"],
            source_bundle_hash=source["source_bundle_hash"],
            actor_ref=actor_ref,
        )

    def _persist_artifact(
        self,
        *,
        artifact_type: str,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        payload: dict[str, Any],
        llm_call_id: str | None,
        source_bundle_id: str,
        source_bundle_hash: str,
        actor_ref: str,
    ) -> GenerationPlanningArtifact:
        if artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT:
            # 章架构与章节编排写的是同一种行：落库（旧 active 行让位）走同一处
            return persist_chapter_architecture(
                self.session,
                object_id,
                payload,
                llm_call_id=llm_call_id,
                created_by=actor_ref or "near_final_planning",
                source_bundle_id=source_bundle_id,
                source_bundle_hash=source_bundle_hash,
            )
        for row in self.session.execute(
            select(GenerationPlanningArtifact).where(
                GenerationPlanningArtifact.artifact_type == artifact_type,
                GenerationPlanningArtifact.object_type == object_type,
                GenerationPlanningArtifact.object_id == object_id,
                GenerationPlanningArtifact.status == "active",
            )
        ).scalars().all():
            row.status = "superseded"
        artifact = GenerationPlanningArtifact(
            row_id=f"planning_{artifact_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            artifact_type=artifact_type,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            payload_json=payload,
            llm_call_id=llm_call_id,
            source_bundle_id=source_bundle_id,
            source_bundle_hash=source_bundle_hash,
            status="active",
            created_by=actor_ref or "near_final_planning",
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def _source_snapshot(
        self,
        *,
        scene: SceneCard,
        chapter: ChapterGoal,
        include_chapter_architecture: bool,
    ) -> dict[str, Any]:
        scene_blueprint = self._latest_scene_blueprint(scene.scene_id)
        source_refs: dict[str, Any] = {
            "chapter_goal": chapter.chapter_id,
            "scene_card": scene.scene_id,
            "chapter_writer_brief": chapter.chapter_id,
            "scene_writer_brief": scene.scene_id,
        }
        injections = [
            {"slot": "chapter_goal", "ref_id": chapter.chapter_id, "digest_key": "chapter_goal"},
            {"slot": "scene_card", "ref_id": scene.scene_id, "digest_key": "scene_card"},
            {"slot": "chapter_writer_brief", "ref_id": chapter.chapter_id, "digest_key": "chapter_writer_brief"},
            {"slot": "scene_writer_brief", "ref_id": scene.scene_id, "digest_key": "scene_writer_brief"},
        ]
        inline_digests: dict[str, str] = {
            "chapter_goal": chapter.chapter_goal or "",
            "scene_card": json.dumps(
                {
                    "scene_goal": scene.scene_goal or "",
                    "location": scene.location or "",
                    "beats": scene.beats_json or [],
                    "must_include_text": scene.must_include_text or "",
                    "exit_change": scene.exit_change or "",
                    "hook": scene.hook or "",
                    "all_chapter_scene_cards": self._chapter_scene_digest(chapter.chapter_id),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "chapter_writer_brief": json.dumps(
                normalize_chapter_writer_brief(chapter.writer_brief_json),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "scene_writer_brief": json.dumps(
                normalize_scene_writer_brief(scene.writer_brief_json),
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        # 2026-09-13 阶段 A：近终稿评审按作者写下的场景结构核验「三拍是否兑现、坩埚是否可辨认」。
        attach_scene_sections(
            scene, self.session, refs=source_refs, injections=injections, digests=inline_digests, design=False
        )
        if scene_blueprint is not None:
            source_refs["scene_blueprint_row_id"] = scene_blueprint.row_id
            injections.append({"slot": "scene_blueprint", "ref_id": scene_blueprint.row_id, "digest_key": "scene_blueprint"})
            inline_digests["scene_blueprint"] = json.dumps(
                scene_blueprint.blueprint_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )
        if include_chapter_architecture:
            architecture = latest_chapter_architecture(self.session, chapter.chapter_id)
            if architecture is not None:
                source_refs["chapter_story_architecture_artifact_row_id"] = architecture.row_id
                injections.append(
                    {
                        "slot": "chapter_story_architecture",
                        "ref_id": architecture.row_id,
                        "digest_key": "chapter_story_architecture",
                    }
                )
                inline_digests["chapter_story_architecture"] = json.dumps(
                    architecture.payload_json or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
        snapshot = {
            "contract_version": "NEAR_FINAL_PLANNING_SOURCE_v1",
            "stage_allowlist_name": "near_final_planning",
            "scene_id": scene.scene_id,
            "chapter_id": chapter.chapter_id,
            "source_version_refs": source_refs,
            "resolved_ref_ids": {},
            "ordered_injections": injections,
            "inline_digests": inline_digests,
        }
        # 2026-09-14 保真修补（WP6.1）：章架构 / 人物压力与场景蓝图看同一套参考——叙事机制
        # （SECTION_SPECS 里的 Narrative Mechanisms section）、结构画像与场景手法（由
        # ``_planning_user_prompt`` 直接渲染）。chapter_story_architecture 模板早有 [结构画像]
        # 条款却从未收到过这一块；character_pressure_blueprint v3 起「without explanatory
        # summary」只在没有这些块时成立。无绑定 / 旧画像 / 解析失败 → 快照逐字不变。
        contract = self._style_reference_contract(scene)
        if contract is not None:
            # 这份规划快照进章架构与人物压力两个节点：按它们的实际路由判云策略（H1）
            reference = build_planning_style_reference(
                contract,
                session=self.session,
                node_ids=(CHAPTER_ARCHITECTURE_ARTIFACT, CHARACTER_PRESSURE_ARTIFACT),
            )
            if reference is not None:
                register_planning_style_reference(snapshot, reference)
        source_hash = sha256_json_normalized(snapshot)
        return {
            "source_bundle_id": f"near_final_planning_source_{scene.scene_id}",
            "source_bundle_hash": source_hash,
            "snapshot": snapshot,
        }

    def _style_reference_contract(self, scene: SceneCard) -> dict[str, Any] | None:
        """规划快照用的契约：这一场的 StylePolicy（没有 bundle，按当前活动绑定现解析，与场景蓝图同一条路径）；
        无绑定 / 解析失败 → None（style_policy_live 记错误码，不阻断规划）。"""
        from novel_system.services.style_policy import style_policy_live

        policy = style_policy_live(self.session, scene)
        return dict(policy.contract) if policy.bound and policy.contract is not None else None

    def _chapter_scene_digest(self, chapter_id: str) -> list[dict[str, Any]]:
        rows = active_chapter_scenes(self.session, chapter_id)
        return [
            {
                "scene_id": row.scene_id,
                "scene_seq": row.scene_seq,
                "scene_goal": row.scene_goal,
                "exit_change": row.exit_change,
                "hook": row.hook,
            }
            for row in rows
        ]

    def _latest_artifact(self, *, artifact_type: str, object_type: str, object_id: str) -> GenerationPlanningArtifact | None:
        if artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT:
            return latest_chapter_architecture(self.session, object_id)
        return latest_active_planning_artifact(
            self.session, artifact_type=artifact_type, object_type=object_type, object_id=object_id
        )

    def _latest_scene_blueprint(self, scene_id: str) -> SceneBlueprint | None:
        return latest_scene_blueprint(self.session, scene_id)

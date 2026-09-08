from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneBlueprint, SceneCard, SceneRunState
from novel_system.services.bundle_builder import resolve_scene_style_runtime_contract
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    collect_narrative_guidance,
    render_narrative_section,
)
from novel_system.services.writer_briefs import normalize_chapter_writer_brief, normalize_scene_writer_brief

_LOGGER = logging.getLogger(__name__)


SCENE_BLUEPRINT_FIELDS: tuple[str, ...] = (
    "visible_desire",
    "forced_choice",
    "price_paid",
    "information_release",
    "relationship_turn",
    "image_anchor",
    "ending_action",
    "next_scene_pull",
    "anti_summary_rule",
)


class SceneBlueprintService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)

    def latest(self, scene_id: str) -> SceneBlueprint | None:
        return self.session.execute(
            select(SceneBlueprint)
            .where(SceneBlueprint.scene_id == scene_id, SceneBlueprint.status.in_(("accepted", "draft")))
            .order_by(SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc())
        ).scalars().first()

    def latest_payload(self, scene_id: str) -> dict[str, Any] | None:
        return self.serialize(self.latest(scene_id))

    def ensure_for_scene(
        self,
        scene_id: str,
        actor_ref: str = "operator",
        *,
        execution_step_key: str | None = None,
    ) -> SceneBlueprint:
        latest = self.latest(scene_id)
        if latest is not None:
            return latest
        return self.generate(
            scene_id,
            actor_ref=actor_ref,
            execution_step_key=execution_step_key,
        )

    def generate(
        self,
        scene_id: str,
        actor_ref: str = "operator",
        *,
        execution_step_key: str | None = None,
    ) -> SceneBlueprint:
        scene = self._require_scene(scene_id)
        chapter = self._require_chapter(scene.chapter_id)
        source = self._source_snapshot(scene, chapter)
        prompt = self.prompt_builder.build(source["snapshot"], "scene_blueprint")
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=source["source_bundle_id"] or f"scene_blueprint_source_{scene.scene_id}",
                bundle_hash=source["source_bundle_hash"],
                node_id="scene_blueprint",
                step="scene_blueprint",
                prompt=prompt,
                user_prompt=_blueprint_user_prompt(
                    prompt["user_prompt"],
                    scene=scene,
                    chapter=chapter,
                    source=source,
                ),
                execution_step_key=execution_step_key,
            )
            payload = _validate_blueprint_payload(node_result.response.structured_output)
            llm_call_id = node_result.llm_call_id
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code="SCENE_BLUEPRINT_LLM_REQUIRED",
                failure_code="SCENE_BLUEPRINT_FAILED",
                operation="scene blueprint generation",
                node_id="scene_blueprint",
                next_action="configure_scene_blueprint_route_and_retry",
            )

        for row in self.session.execute(
            select(SceneBlueprint).where(
                SceneBlueprint.scene_id == scene.scene_id,
                SceneBlueprint.status.in_(("draft", "accepted")),
            )
        ).scalars().all():
            row.status = "superseded"

        blueprint = SceneBlueprint(
            row_id=f"scene_blueprint_{scene.scene_id}_{uuid.uuid4().hex[:10]}",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=source["source_bundle_id"],
            source_bundle_hash=source["source_bundle_hash"],
            blueprint_json=payload,
            llm_call_id=llm_call_id,
            status="accepted",
        )
        self.session.add(blueprint)
        self.session.flush()
        return blueprint

    @staticmethod
    def serialize(row: SceneBlueprint | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "row_id": row.row_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "source_bundle_id": row.source_bundle_id,
            "source_bundle_hash": row.source_bundle_hash,
            "blueprint_json": row.blueprint_json or {},
            "llm_call_id": row.llm_call_id,
            "status": row.status,
            "created_at": row.created_at,
        }

    def _source_snapshot(self, scene: SceneCard, chapter: ChapterGoal) -> dict[str, Any]:
        state = self.session.get(SceneRunState, scene.scene_id)
        source_bundle_id = state.current_bundle_id if state and state.current_bundle_id else None
        snapshot = {
            "contract_version": "SCENE_BLUEPRINT_SOURCE_v1",
            "stage_allowlist_name": "scene_blueprint",
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "source_version_refs": {
                "chapter_goal": chapter.chapter_id,
                "scene_card": scene.scene_id,
                "chapter_writer_brief": chapter.chapter_id,
                "scene_writer_brief": scene.scene_id,
            },
            "resolved_ref_ids": {},
            "ordered_injections": [
                {"slot": "chapter_goal", "ref_id": chapter.chapter_id, "digest_key": "chapter_goal"},
                {"slot": "scene_card", "ref_id": scene.scene_id, "digest_key": "scene_card"},
                {"slot": "chapter_writer_brief", "ref_id": chapter.chapter_id, "digest_key": "chapter_writer_brief"},
                {"slot": "scene_writer_brief", "ref_id": scene.scene_id, "digest_key": "scene_writer_brief"},
            ],
            "inline_digests": {
                "chapter_goal": chapter.chapter_goal or "",
                "scene_card": json.dumps(
                    {
                        "scene_goal": scene.scene_goal or "",
                        "beats": scene.beats_json or [],
                        "exit_change": scene.exit_change or "",
                        "hook": scene.hook or "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "chapter_writer_brief": json.dumps(normalize_chapter_writer_brief(chapter.writer_brief_json), ensure_ascii=False, sort_keys=True),
                "scene_writer_brief": json.dumps(normalize_scene_writer_brief(scene.writer_brief_json), ensure_ascii=False, sort_keys=True),
            },
        }
        # 2026-09 风格模仿 v2（规格 §2.W5.4）：规划层也看参考作品的叙事取舍机制——
        # 只注入 narrative_guidance（无语言层特征、无原文样例），让 information_release /
        # pacing / ending_action 受其牵引。无绑定 / 旧画像 / 解析失败 → 不注入。
        narrative_guidance = self._style_narrative_guidance(scene)
        if narrative_guidance is not None:
            lines, contract_hash = narrative_guidance
            snapshot["source_version_refs"]["style_reference_runtime_contract_hash"] = contract_hash
            snapshot["source_version_refs"]["style_narrative_guidance_line_count"] = len(lines)
            snapshot["ordered_injections"].append(
                {
                    "slot": NARRATIVE_GUIDANCE_SECTION_KEY,
                    "ref_id": contract_hash,
                    "digest_key": NARRATIVE_GUIDANCE_SECTION_KEY,
                }
            )
            snapshot["inline_digests"][NARRATIVE_GUIDANCE_SECTION_KEY] = render_narrative_section(lines)
        source_hash = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        return {
            "source_bundle_id": source_bundle_id,
            "source_bundle_hash": state.current_bundle_hash if state and state.current_bundle_hash else source_hash,
            "snapshot": snapshot,
        }

    def _style_narrative_guidance(self, scene: SceneCard) -> tuple[list[str], str] | None:
        try:
            contract = resolve_scene_style_runtime_contract(self.session, scene)
        except Exception:  # noqa: BLE001 — 可选增强：解析失败只记日志，不阻断规划
            _LOGGER.warning(
                "scene_blueprint style narrative guidance skipped for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return None
        if contract is None:
            return None
        lines = collect_narrative_guidance(contract)
        if not lines:
            return None
        return lines, str(contract["contract_hash"])

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)


def _blueprint_user_prompt(base_prompt: str, *, scene: SceneCard, chapter: ChapterGoal, source: dict[str, Any]) -> str:
    return "\n".join(
        [
            base_prompt,
            "",
            "## Scene Blueprint Target",
            f"Scene ID: {scene.scene_id}",
            f"Chapter ID: {scene.chapter_id}",
            f"Source Bundle ID: {source.get('source_bundle_id') or ''}",
            f"Source Bundle Hash: {source.get('source_bundle_hash') or ''}",
            "",
            "## Required Function",
            "Produce a scene readability proposal v2: desire, forced choice, paid price, information release, relationship turn, image anchor, ending action, next-scene pull, and one anti-summary rule.",
        ]
    )


def _validate_blueprint_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise DomainError("SCENE_BLUEPRINT_INVALID", "scene blueprint payload must be an object", status_code=502)
    # 只要求规范字段齐全；多余字段忽略——response_format 是 json_object（非严格
    # schema），真实模型可能多返解释性键，不应因此把整份蓝图判失败。
    missing_fields = sorted(set(SCENE_BLUEPRINT_FIELDS) - set(payload))
    if missing_fields:
        raise DomainError(
            "SCENE_BLUEPRINT_INVALID",
            "scene blueprint payload is missing required canonical fields",
            status_code=502,
            details={"missing_fields": missing_fields},
        )
    normalized: dict[str, str] = {}
    for field in SCENE_BLUEPRINT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DomainError(
                "SCENE_BLUEPRINT_INVALID",
                f"scene blueprint field {field} must be a non-empty string",
                status_code=502,
                details={"field": field},
            )
        normalized[field] = value.strip()
    return normalized

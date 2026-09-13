from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import RelationProfile, SceneBlueprint, SceneCard, VoiceProfile
from novel_system.services.qc_constraints import (
    constraint_alternatives,
    constraint_terms,
    named_scene_card_sources,
)
from novel_system.services.resolver import Resolver
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_structure_brief import missing_structure_fields, scene_has_structure
from novel_system.services.writer_briefs import normalize_scene_writer_brief


class SceneRunPreflightService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.resolver = Resolver()
        self.contracts = SceneExecutionContractService(session)

    def build(self, scene: SceneCard, chapter_state: dict[str, Any]) -> dict[str, Any]:
        execution_contract = self.contracts.latest(scene.scene_id)
        blocking_items = self._blocking_items(scene, execution_contract)
        warning_items = self._warning_items(scene)
        context_items = self._context_items(chapter_state)
        missing_dependencies = self._missing_dependencies(scene)
        create_actions = self._create_actions(scene.scene_id, missing_dependencies)
        constraint_conflicts = self._constraint_conflicts(scene)

        if blocking_items or constraint_conflicts:
            overall_status = "blocked"
            can_run = False
        elif warning_items or context_items:
            overall_status = "warning"
            can_run = True
        else:
            overall_status = "ready"
            can_run = True

        return {
            "can_run": can_run,
            "overall_status": overall_status,
            "blocking_items": blocking_items,
            "warning_items": warning_items,
            "context_items": context_items,
            "missing_dependencies": missing_dependencies,
            "create_actions": create_actions,
            "constraint_conflicts": constraint_conflicts,
        }

    def _missing_dependencies(self, scene: SceneCard) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        voice_profile_id = self.resolver.resolve_voice_profile_id(scene)
        if voice_profile_id and self.resolver.resolve_active_voice_profile(self.session, scene) is None:
            items.append(
                {
                    "dependency_type": "voice_card",
                    "lineage_key": voice_profile_id,
                    "character_id": scene.pov_character_id,
                    "blocking_code": "VOICE_PROFILE_MISSING",
                }
            )

        relation_profile_id = self.resolver.resolve_relation_profile_id(scene)
        if relation_profile_id and self.resolver.resolve_active_relation_profile(self.session, scene) is None:
            character_ids = list(dict.fromkeys(scene.onstage_chars_json or []))
            items.append(
                {
                    "dependency_type": "relation_card",
                    "lineage_key": relation_profile_id,
                    "character_ids": character_ids[:2],
                    "blocking_code": "RELATION_PROFILE_MISSING",
                }
            )

        return items

    def _create_actions(self, scene_id: str, missing_dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 可执行落点：所有缺失卡都可经此端点确定性建出最小 active 卡解阻（见 create_missing_cards）。
        # 历史上该动作只带 review.item_type="voice_profile"，但该 item_type 无 effect/物化落点（死胡同）；
        # 现补 endpoint/method/executable 让前端真正点得动，review 块保留向后兼容。
        endpoint = f"/api/v1/scenes/{scene_id}/preflight/create-cards"
        actions: list[dict[str, Any]] = []
        for dependency in missing_dependencies:
            dependency_type = dependency.get("dependency_type")
            lineage_key = str(dependency.get("lineage_key") or "")
            if dependency_type == "voice_card":
                character_id = str(dependency.get("character_id") or "")
                actions.append(
                    {
                        "action": "create_minimal_voice_card",
                        "lineage_key": lineage_key,
                        "label": f"Create voice card for {character_id}",
                        "executable": True,
                        "endpoint": endpoint,
                        "method": "POST",
                        "review": {
                            "item_type": "voice_profile",
                            "candidate_payload_json": {
                                "lineage_key": lineage_key,
                                "character_id": character_id,
                                "content": f"{character_id}: keep speech and interiority consistent.",
                                "source": "scene_run_preflight",
                            },
                        },
                    }
                )
            elif dependency_type == "relation_card":
                character_ids = [str(item) for item in dependency.get("character_ids", [])]
                actions.append(
                    {
                        "action": "create_minimal_relation_card",
                        "lineage_key": lineage_key,
                        "label": f"Create relation card for {' / '.join(character_ids)}",
                        "executable": True,
                        "endpoint": endpoint,
                        "method": "POST",
                        "review": {
                            "item_type": "relation_profile",
                            "candidate_payload_json": {
                                "lineage_key": lineage_key,
                                "character_ids": character_ids,
                                "content": "Define the current emotional pressure, information asymmetry, and trust boundary.",
                                "source": "scene_run_preflight",
                            },
                        },
                    }
                )
        return actions

    def create_missing_cards(self, scene: SceneCard) -> dict[str, Any]:
        """确定性地为当前场景缺失的 voice/relation 依赖建出最小可用(active)卡，解阻 run 预检。

        幂等：已有 active 卡则跳过。这是 create_minimal_voice_card / create_minimal_relation_card
        预检动作的真实执行落点（此前该动作无 effect/物化路径，是死胡同）。
        """
        created: list[dict[str, Any]] = []
        for dependency in self._missing_dependencies(scene):
            dependency_type = dependency.get("dependency_type")
            lineage_key = str(dependency.get("lineage_key") or "")
            if not lineage_key:
                continue
            if dependency_type == "voice_card":
                if self.resolver.resolve_active_voice_profile(self.session, scene) is not None:
                    continue
                character_id = str(dependency.get("character_id") or scene.pov_character_id or "")
                version = self._next_profile_version(VoiceProfile, VoiceProfile.voice_profile_id, lineage_key)
                self.session.add(
                    VoiceProfile(
                        row_id=f"voice_card__{lineage_key}__v{version}__{uuid4().hex[:8]}",
                        voice_profile_id=lineage_key,
                        version=version,
                        character_id=character_id,
                        content=f"{character_id}：保持其说话方式、用词与内心独白的一致性（最小占位声线，建议后续在声线工作台细化）。",
                        active_flag=1,
                        runtime_eligible=1,
                        runtime_eligibility_basis="manual_minimal",
                        source_note="scene_run_preflight.create_missing_cards",
                    )
                )
                created.append({"dependency_type": "voice_card", "lineage_key": lineage_key, "character_id": character_id})
            elif dependency_type == "relation_card":
                if self.resolver.resolve_active_relation_profile(self.session, scene) is not None:
                    continue
                character_ids = [str(item) for item in dependency.get("character_ids") or []]
                left = character_ids[0] if character_ids else (scene.pov_character_id or "")
                right = character_ids[1] if len(character_ids) > 1 else (scene.pov_character_id or "")
                version = self._next_profile_version(RelationProfile, RelationProfile.relation_profile_id, lineage_key)
                self.session.add(
                    RelationProfile(
                        row_id=f"relation_card__{lineage_key}__v{version}__{uuid4().hex[:8]}",
                        relation_profile_id=lineage_key,
                        left_character_id=left,
                        right_character_id=right,
                        version=version,
                        content="定义当前的情感压力、信息不对称与信任边界（最小占位关系卡，建议后续细化）。",
                        active_flag=1,
                        runtime_eligible=1,
                        runtime_eligibility_basis="manual_minimal",
                        source_note="scene_run_preflight.create_missing_cards",
                    )
                )
                created.append({"dependency_type": "relation_card", "lineage_key": lineage_key})
        self.session.flush()
        return {"created": created, "run_preflight": self.build(scene, {})}

    def _next_profile_version(self, model: Any, id_column: Any, lineage_key: str) -> int:
        versions = self.session.execute(select(model.version).where(id_column == lineage_key)).scalars().all()
        return (max(versions) + 1) if versions else 1

    def _constraint_conflicts(self, scene: SceneCard) -> list[dict[str, Any]]:
        forbidden_text = scene.forbidden_text
        if not isinstance(forbidden_text, str) or not forbidden_text.strip():
            return []
        forbidden_terms = constraint_terms(forbidden_text)
        if not forbidden_terms:
            return []
        positive_sources = self._positive_constraint_sources(scene)
        conflicts: list[dict[str, Any]] = []
        for term in forbidden_terms:
            # 与运行期 QC 闸门链同一契约：'A|B' 表示任一备选命中即触禁，
            # 预检据此判冲突（裸 term 匹配会漏掉带备选写法的约束）。
            alternatives = constraint_alternatives(term) or [term]
            for source_name, source_text in positive_sources:
                if any(alternative in source_text for alternative in alternatives):
                    conflicts.append(
                        {
                            "term": term,
                            "required_source": source_name,
                            "forbidden_source": "scene_card.forbidden_text",
                            "severity": "blocking",
                            "human_readable_reason": "场景要求使用该词，但禁用规则又禁止该词；请先选择保留或替换。",
                        }
                    )
                    break
        return conflicts

    def _blocking_items(self, scene: SceneCard, execution_contract) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        if execution_contract is not None and execution_contract.status == "blocked":
            missing_fields_list = list(execution_contract.missing_fields_json or [])
            missing_fields = ", ".join(missing_fields_list)
            items.append(
                {
                    "code": "SCENE_EXECUTION_CONTRACT_BLOCKED",
                    "title": "Scene execution contract is incomplete",
                    "detail": f"Fill the missing execution contract fields before drafting: {missing_fields}",
                    # 结构化缺失字段（供异步 run-jobs / 前端精确引导，区别于人读的 detail 串）
                    "missing_fields": missing_fields_list,
                    "technical_hint": execution_contract.contract_id,
                }
            )
        elif execution_contract is not None and execution_contract.status == "stale":
            items.append(
                {
                    "code": "SCENE_EXECUTION_CONTRACT_STALE",
                    "title": "Scene execution contract is stale",
                    "detail": "Upstream planning changed. Regenerate the contract and replan this scene before drafting.",
                    "technical_hint": execution_contract.contract_id,
                }
            )

        voice_profile_id = self.resolver.resolve_voice_profile_id(scene)
        if voice_profile_id and self.resolver.resolve_active_voice_profile(self.session, scene) is None:
            items.append(
                {
                    "code": "VOICE_PROFILE_MISSING",
                    "title": "缺少 POV 声线档案，当前不宜运行场景",
                    "detail": "请先补齐当前 POV 角色的可用声线档案，再执行完整场景运行。",
                    "technical_hint": f"expected active voice profile: {voice_profile_id}",
                }
            )

        relation_profile_id = self.resolver.resolve_relation_profile_id(scene)
        if relation_profile_id and self.resolver.resolve_active_relation_profile(self.session, scene) is None:
            items.append(
                {
                    "code": "RELATION_PROFILE_MISSING",
                    "title": "缺少同场角色关系档案，当前不宜运行场景",
                    "detail": "请先补齐当前同场角色组合的可用关系档案，再执行完整场景运行。",
                    "technical_hint": f"expected active relation profile: {relation_profile_id}",
                }
            )

        return items


    @staticmethod
    def _positive_constraint_sources(scene: SceneCard) -> list[tuple[str, str]]:
        # 字段顺序即归因优先级（与 QC 侧不同：不含 location 且 must_include 优先）。
        return named_scene_card_sources(
            scene, ("must_include_text", "hook", "exit_change", "scene_goal")
        )

    def _warning_items(self, scene: SceneCard) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        if not (scene.scene_goal or "").strip():
            items.append(
                {
                    "code": "SCENE_GOAL_MISSING",
                    "title": "场景目标为空",
                    "detail": "建议先补充这场戏要推进的目标，再执行完整场景运行。",
                    "technical_hint": "scene_card.scene_goal is blank",
                }
            )
        if not (scene.location or "").strip():
            items.append(
                {
                    "code": "SCENE_LOCATION_MISSING",
                    "title": "场景地点为空",
                    "detail": "建议先补充场景地点，让运行结果有更稳定的空间锚点。",
                    "technical_hint": "scene_card.location is blank",
                }
            )
        if not (scene.pov_character_id or "").strip():
            items.append(
                {
                    "code": "SCENE_POV_MISSING",
                    "title": "POV 角色为空",
                    "detail": "建议先明确这场戏的 POV 角色，避免运行结果失去主视角。",
                    "technical_hint": "scene_card.pov_character_id is blank",
                }
            )
        if not list(scene.onstage_chars_json or []):
            items.append(
                {
                    "code": "SCENE_ONSTAGE_CHARACTERS_MISSING",
                    "title": "同场角色列表为空",
                    "detail": "建议先补齐同场角色，让运行时关系与互动上下文更完整。",
                    "technical_hint": "scene_card.onstage_chars_json is empty",
                }
            )
        if not list(scene.beats_json or []):
            items.append(
                {
                    "code": "SCENE_BEATS_MISSING",
                    "title": "场景节拍为空",
                    "detail": "建议先补齐场景 beats，让运行结果更容易贴合预期推进。",
                    "technical_hint": "scene_card.beats_json is empty",
                }
            )
        latest_blueprint = self.session.execute(
            select(SceneBlueprint)
            .where(SceneBlueprint.scene_id == scene.scene_id, SceneBlueprint.status.in_(("accepted", "draft")))
            .order_by(SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc())
        ).scalars().first()
        if latest_blueprint is None:
            items.append(
                {
                    "code": "SCENE_BLUEPRINT_MISSING",
                    "title": "Scene literary blueprint is missing",
                    "detail": "The run can auto-generate it, but the author should review the scene intent before drafting.",
                    "technical_hint": "POST /api/v1/scenes/{scene_id}/literary-blueprint",
                }
            )
        # 2026-09-13 阶段 A：带场景结构（雪花 / 章节编排的三拍与坩埚）的场按结构本身体检，
        # 不再拿 v2 简报的字段清单去要求一个已经走完十步的作者「再填一套」。
        if scene_has_structure(scene):
            missing_beats = missing_structure_fields(scene)
            if missing_beats:
                items.append(
                    {
                        "code": "SCENE_STRUCTURE_INCOMPLETE",
                        "title": "场景结构三拍不完整",
                        "detail": (
                            "这一场带着场景结构，但还缺：" + ", ".join(missing_beats)
                            + "。回到构思的场景规划或章节编排补齐，起草模型拿到的结构简报才完整。"
                        ),
                        "technical_hint": "scene_card.writer_brief_json: scene_crucible + goal/conflict/setback or reaction/dilemma/decision",
                    }
                )
            return items

        brief = normalize_scene_writer_brief(scene.writer_brief_json)
        missing_intent = [
            key
            for key in ("choice_under_pressure", "power_shift", "new_information", "emotional_turn", "image_anchor", "reader_aftertaste")
            if not brief.get(key)
        ]
        if missing_intent:
            items.append(
                {
                    "code": "SCENE_LITERARY_INTENT_INCOMPLETE",
                    "title": "Scene literary intent is incomplete",
                    "detail": "Consider filling the v2 writer brief fields before generation: " + ", ".join(missing_intent),
                    "technical_hint": "scene_card.writer_brief_json",
                }
            )

        return items

    def _context_items(self, chapter_state: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        manual_hold_reason = (chapter_state.get("manual_hold_reason") or "").strip()
        if manual_hold_reason:
            items.append(
                {
                    "code": "CHAPTER_MANUAL_HOLD_ACTIVE",
                    "title": "本章已设置人工挂起",
                    "detail": "这不会阻止当前场景运行，但会继续阻止章节级 final aggregate。",
                    "technical_hint": f"manual hold reason: {manual_hold_reason}",
                }
            )

        pending_backfill_count = int(chapter_state.get("chapter_backfill_pending_count") or 0)
        if pending_backfill_count > 0:
            items.append(
                {
                    "code": "CHAPTER_BACKFILL_PENDING",
                    "title": "本章仍有待处理的 staged backfill",
                    "detail": "这不会阻止当前场景运行，但会继续阻止章节级 final aggregate。",
                    "technical_hint": f"pending staged backfill count: {pending_backfill_count}",
                }
            )

        return items

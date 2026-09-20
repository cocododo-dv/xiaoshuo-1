from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneBlueprint, SceneCard
from novel_system.services.qc_constraints import (
    constraint_alternatives,
    forbidden_terms as literal_forbidden_terms,
    named_scene_card_sources,
)
from novel_system.services.scene_design_ownership import design_owned_by_plan
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_structure_brief import missing_structure_fields, scene_has_structure
from novel_system.services.writer_briefs import normalize_scene_writer_brief


class SceneRunPreflightService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.contracts = SceneExecutionContractService(session)

    def build(self, scene: SceneCard, chapter_state: dict[str, Any]) -> dict[str, Any]:
        execution_contract = self.contracts.latest(scene.scene_id)
        blocking_items = self._blocking_items(scene, execution_contract)
        warning_items = self._warning_items(scene)
        context_items = self._context_items(chapter_state)
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
            "constraint_conflicts": constraint_conflicts,
        }

    def _constraint_conflicts(self, scene: SceneCard) -> list[dict[str, Any]]:
        forbidden_text = scene.forbidden_text
        if not isinstance(forbidden_text, str) or not forbidden_text.strip():
            return []
        forbidden_terms = literal_forbidden_terms(forbidden_text)
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

        # 2026-09-20：POV 声线卡 / 同场关系卡不再是起草的前提。这两类卡是 2026-09 减法删掉的知识卡体系留下的，
        # 产品里已经没有任何地方能写它们——唯一的来路是预检自己铸的一句占位套话（还带着裸角色 id），
        # 于是每一部真实作品的每一场都在这里被拦下（作者报「任务总是提示被阻断」），解阻的办法是把套话当
        # 事实喂给起草模型。角色的声音与关系来自构思：Scene Design Context 带着 POV 角色摘要、价值观、
        # 视角故事与同场角色一句话。库里真有声线 / 关系卡时 bundle 照旧注入，没有就是没有这一节。
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
                            + (
                                "。这一场是雪花整理出来的：回构思第 10 步「场景规划」补齐并确认，场景卡会自动跟上"
                                if design_owned_by_plan(self.session, scene)
                                else "。到章节编排补齐这张场景卡"
                            )
                            + "，起草模型拿到的结构简报才完整。"
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

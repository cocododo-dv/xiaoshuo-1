"""章节编排 LLM 的上下文底座（chapter planning context）。

与 bundle_builder 同族但面向「规划一章」而非「起草一场」：把雪花 canon、章节蓝图、叙事事件账本、
邻章交接、章结构邻域、人物站位、作者约束等既有资产汇编成一份确定性的 prompt payload，
带 source_version_refs（可审计）与 degraded_slots（缺料降级，冷启动不阻断）。

2026-09-30（批准 #17a，重评 R10）：章级的张力 / 线索 / 章级视角·入口·出口字段没有任何地方能填、也没有程序会写，
它们不再进提示词；邻章交接、章结构邻域、近几章的视角分布都从**各场**的真实数据现算（上一章最后一场、下一章第一场、
各章场数与灾难位置、前几章各场的视角）。伏笔账本早在 2026-09 减法里删除，伏笔槽随之去掉。
作者偏好档案那一槽也去掉了（批准 #6，重评 R5：写作偏好学习整条链退役）。

设计文档：docs/chapter-arrangement-llm-design-2026-07-16.md §3（其中的伏笔 / 张力 / 作者偏好输入已退役）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SnowflakeStepRun,
    StoryCharacter,
)
from novel_system.services.catalog import (
    CatalogService,
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    chapter_title,
    scene_kind,
    scene_title,
)
from novel_system.services.chapter_architecture import (  # noqa: F401 — 旧名：从本模块 import 的调用方
    CHAPTER_ARCHITECTURE_ARTIFACT,
    latest_chapter_architecture,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json, normalize
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.scene_design_ownership import plan_owned_scene_ids
from novel_system.services.scene_lookup import require_project
from novel_system.services.snowflake_queries import latest_by_step
from novel_system.services.snowflake_step_drafts import merge_step_draft
from novel_system.services.style_reference.planning_context import (
    STRUCTURE_REFERENCE_HOW_TO_USE,
    resolve_project_style_reference,
)

logger = logging.getLogger(__name__)

# 2026-09-12 结构跟随：参考作者结构画像 / 场景手法的 slot 名。体量由渲染器封顶（画像 ≤1,500 字
# + ≤6 条 ≤150 字样例 + ≤10 行手法），不走 _truncate_value——那是 canon 摘要的按槽预算，
# 画像里的数字（章长 / 段数 / 比重）截半行就失真。
STYLE_REFERENCE_SLOT = "style_reference"

# 雪花 canon 摘要只取世界观/主线约束层；场景清单/场景细节体量大且已物化进目录，不重复注入。
_CANON_STEP_KEYS = (
    "one_sentence_summary",
    "one_paragraph_summary",
    "short_synopsis",
    "long_synopsis",
    "character_synopses",
)
# 单步 canon 摘要的字符预算（长大纲最大）；超出截断并标注。
# 2026-09-13 阶段 D：长篇大纲 = 五段各约一页（≤600 字）的展开 + 章表，3200 才装得下整份；
# _truncate_value 对 list 值不扣预算，所以章表不会被散文挤掉。
_CANON_CHAR_BUDGET = {"long_synopsis": 5200, "character_synopses": 1200}
_CANON_DEFAULT_BUDGET = 600

# 章结构邻域窗口：前 3 章 + 本章 + 后 2 章。
_NEIGHBORHOOD_BEFORE = 3
_NEIGHBORHOOD_AFTER = 2
# 视角近期分布回看的章数。
_POV_LOOKBACK = 6


@dataclass(slots=True)
class ChapterPlanningContext:
    project_id: str
    chapter_id: str
    prompt_payload: dict[str, Any]
    source_version_refs: dict[str, Any]
    degraded_slots: list[str]
    context_fingerprint: str
    chapter: ChapterGoal = field(repr=False, default=None)  # type: ignore[assignment]
    scenes: list[SceneCard] = field(repr=False, default_factory=list)



# 章规划的 style_reference 槽进哪些节点的提示（chapter_plan_llm 的四个节点）
CHAPTER_PLANNING_REFERENCE_NODE_IDS: tuple[str, ...] = (
    "chapter_story_architecture",
    "chapter_scene_plan_candidates",
    "chapter_scene_plan_fill",
    "chapter_plan_review",
)

class ChapterPlanningContextBuilder:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._catalog = CatalogService(session)
        self._degraded: list[str] = []

    def build(self, project_id: str, chapter_id: str) -> ChapterPlanningContext:
        self._degraded = []
        project = require_project(self.session, project_id)
        chapters = self._catalog.chapter_rows(project_id)
        index = next((i for i, row in enumerate(chapters) if row.chapter_id == chapter_id), None)
        if index is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)
        chapter = chapters[index]
        scenes = self._catalog.scene_rows(chapter_id)
        # 阶段 Y：雪花整理出来、构思里那一行还在的场，设计归构思第 10 步所有——告诉模型别往里填
        self._plan_owned = plan_owned_scene_ids(self.session, project_id, scenes)

        refs: dict[str, Any] = {
            "project_id": project_id,
            "chapter_goal": chapter.chapter_id,
            "scene_card_ids": [scene.scene_id for scene in scenes],
        }
        payload: dict[str, Any] = {
            "project": {
                "project_id": project_id,
                "title": project.title,
                "genre": project.genre,
            },
            "chapter_card": self._chapter_card_slot(chapter, index),
            "scene_cards_current": [self._scene_slot(scene) for scene in scenes],
            "neighbor_handoff": self._neighbor_slot(chapters, index),
        }

        architecture = latest_chapter_architecture(self.session, chapter_id)
        if architecture is not None:
            payload["chapter_architecture"] = architecture.payload_json or {}
            refs["chapter_story_architecture_artifact_row_id"] = architecture.row_id
        else:
            self._slot_degraded("chapter_architecture")

        canon = self._snowflake_canon_slot(project_id, refs)
        if canon:
            payload["snowflake_canon"] = canon
        else:
            self._slot_degraded("snowflake_canon")

        first_scene = scenes[0] if scenes else None
        narrative_state = self._narrative_state_slot(project_id, first_scene)
        if narrative_state:
            payload["narrative_state"] = narrative_state
        else:
            self._slot_degraded("narrative_state")

        payload["structure_neighborhood"] = self._structure_slot(chapters, index)
        payload["character_positions"] = self._character_slot(chapters, index, scenes)
        payload["author_constraints"] = self._constraints_slot(chapter)
        # 2026-09-12 结构跟随：project + global 作用域的风格绑定 → 结构画像与场景手法进规划
        # 提示。无绑定是常态而非降级（不进 degraded_slots）；有绑定时契约哈希进 refs 可审计。
        style_reference = self._style_reference_slot(project_id, refs)
        if style_reference:
            payload[STYLE_REFERENCE_SLOT] = style_reference

        fingerprint = uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(normalize(payload))).hex
        return ChapterPlanningContext(
            project_id=project_id,
            chapter_id=chapter_id,
            prompt_payload=payload,
            source_version_refs=refs,
            degraded_slots=list(self._degraded),
            context_fingerprint=fingerprint,
            chapter=chapter,
            scenes=scenes,
        )

    # ---------- slots ----------

    def _slot_degraded(self, slot: str) -> None:
        if slot not in self._degraded:
            self._degraded.append(slot)

    def _chapter_card_slot(self, chapter: ChapterGoal, index: int) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        return {
            "chapter_id": chapter.chapter_id,
            "no": index + 1,
            "title": chapter_title(chapter),
            "state": str(chapter.state or "planned"),
            "words_target": chapter.words_target,
            "act": narrative.get("act"),
            "spine": narrative.get("spine"),
            "promise": narrative.get("promise"),
            "drama": dict(narrative.get("drama") or {}),
        }

    def _scene_slot(self, scene: SceneCard) -> dict[str, Any]:
        kind = scene_kind(scene)
        brief_json = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        pov_name = ""
        if scene.pov_character_id:
            character = self.session.get(StoryCharacter, scene.pov_character_id)
            pov_name = character.display_name if character is not None else ""
        return {
            "scene_id": scene.scene_id,
            "seq": scene.scene_seq,
            "title": scene_title(scene),
            "kind": kind,
            "state": str(scene.state or "todo"),
            "brief": {key: str(brief_json.get(key) or "") for key in keys},
            "pov_character_name": pov_name,
            "exit_change": str(scene.exit_change or ""),
            "hook": str(scene.hook or ""),
            "words_current": int(scene.words_current or 0),
            "design_owner": "plan" if scene.scene_id in getattr(self, "_plan_owned", set()) else "desk",
        }

    def _neighbor_slot(self, chapters: list[ChapterGoal], index: int) -> dict[str, Any]:
        """上一章怎么收的（最后一场的离场变化 / 钩子）、下一章怎么开的（第一场的形态与第一拍）——都读场上的数据。"""
        prev_row = chapters[index - 1] if index > 0 else None
        next_row = chapters[index + 1] if index + 1 < len(chapters) else None
        prev_payload: dict[str, Any] | None = None
        if prev_row is not None:
            prev_payload = {"chapter_id": prev_row.chapter_id, "title": chapter_title(prev_row)}
            last_scene = self._catalog.scene_rows(prev_row.chapter_id)[-1:]
            if last_scene:
                prev_payload["last_scene"] = {
                    "title": scene_title(last_scene[0]),
                    "exit_change": str(last_scene[0].exit_change or ""),
                    "hook": str(last_scene[0].hook or ""),
                }
        next_payload: dict[str, Any] | None = None
        if next_row is not None:
            next_payload = {"chapter_id": next_row.chapter_id, "title": chapter_title(next_row)}
            first_scene = self._catalog.scene_rows(next_row.chapter_id)[:1]
            if first_scene:
                kind = scene_kind(first_scene[0])
                brief = dict(first_scene[0].writer_brief_json or {})
                opening_key = SCENE_BRIEF_GCS[0] if kind == "proactive" else SCENE_BRIEF_RDD[0]
                next_payload["first_scene"] = {
                    "title": scene_title(first_scene[0]),
                    "kind": kind,
                    "first_beat": str(brief.get(opening_key) or ""),
                }
        return {"prev": prev_payload, "next": next_payload}

    def _snowflake_canon_slot(self, project_id: str, refs: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            latest = latest_by_step(self.session, SnowflakeStepRun, project_id)
        except SQLAlchemyError:
            logger.warning("snowflake canon slot failed for %s", project_id, exc_info=True)
            self._slot_degraded("snowflake_canon")
            return []
        items: list[dict[str, Any]] = []
        run_ids: list[str] = []
        for step_key in _CANON_STEP_KEYS:
            run = latest.get(step_key)
            if run is None or str(run.status or "") not in {"approved", "skipped"}:
                continue
            draft = merge_step_draft(step_key, run.draft_json, latest_by_step=latest)
            budget = _CANON_CHAR_BUDGET.get(step_key, _CANON_DEFAULT_BUDGET)
            items.append(
                {
                    "step_key": step_key,
                    "draft": _truncate_value(draft, budget),
                }
            )
            run_ids.append(run.step_run_id)
        if run_ids:
            refs["snowflake_step_run_ids"] = run_ids
        return items

    def _style_reference_slot(self, project_id: str, refs: dict[str, Any]) -> dict[str, Any] | None:
        # 这个槽只进章规划的四个节点的提示：按它们的实际路由判云策略（H1）
        reference = resolve_project_style_reference(
            self.session, project_id, node_ids=CHAPTER_PLANNING_REFERENCE_NODE_IDS
        )
        if not reference:
            return None
        refs["style_reference_runtime_contract_hash"] = reference["contract_hash"]
        refs["style_reference_profile_id"] = reference["profile_id"]
        slot: dict[str, Any] = {
            "profile_id": reference["profile_id"],
            "how_to_use": STRUCTURE_REFERENCE_HOW_TO_USE,
        }
        for key in ("structure_card", "structure_samples", "planning_guidance"):
            if reference.get(key):
                slot[key] = reference[key]
        return slot

    def _narrative_state_slot(self, project_id: str, first_scene: SceneCard | None) -> str | None:
        if first_scene is None:
            return None
        try:
            text = NarrativeEventLog(self.session).format_state_for_prompt(
                project_id,
                None,
                scene_id=first_scene.scene_id,
                pov_character_id=None,
                onstage_character_ids=None,
            )
        except (DomainError, SQLAlchemyError):
            # 冷启动 / 事件账本读不出：降级，不阻断规划（预期内，不记日志）
            return None
        return text or None

    def _structure_slot(self, chapters: list[ChapterGoal], index: int) -> dict[str, Any]:
        """本章前后几章的结构位置：第几章、章名、幕、灾难标记、场数（取代从来没人填的「张力曲线」）。"""
        lo = max(0, index - _NEIGHBORHOOD_BEFORE)
        hi = min(len(chapters), index + _NEIGHBORHOOD_AFTER + 1)
        window = chapters[lo:hi]
        counts = dict(
            self.session.execute(
                select(SceneCard.chapter_id, func.count())
                .where(SceneCard.chapter_id.in_([row.chapter_id for row in window]), SceneCard.trashed_flag == 0)
                .group_by(SceneCard.chapter_id)
            ).all()
        )
        items = []
        for offset, row in enumerate(window):
            narrative = dict(row.narrative_json or {})
            items.append(
                {
                    "no": lo + offset + 1,
                    "title": chapter_title(row),
                    "act": narrative.get("act"),
                    "spine": narrative.get("spine") or "",
                    "scene_count": int(counts.get(row.chapter_id) or 0),
                    "is_current": lo + offset == index,
                }
            )
        return {"window": items, "total_chapters": len(chapters)}

    def _character_slot(
        self,
        chapters: list[ChapterGoal],
        index: int,
        scenes: list[SceneCard],
    ) -> dict[str, Any]:
        # 近几章的视角分布：数前几章里每一场的视角人物（场数），不读从来没人填的章级视角字段
        previous_ids = [row.chapter_id for row in chapters[max(0, index - _POV_LOOKBACK) : index]]
        previous_povs = (
            [
                pov
                for pov in self.session.execute(
                    select(SceneCard.pov_character_id).where(
                        SceneCard.chapter_id.in_(previous_ids), SceneCard.trashed_flag == 0
                    )
                ).scalars()
                if pov
            ]
            if previous_ids
            else []
        )
        character_ids: list[str] = []
        for scene in scenes:
            for cid in [scene.pov_character_id, *(scene.onstage_chars_json or [])]:
                if cid and cid not in character_ids:
                    character_ids.append(cid)
        names = self._character_names([*previous_povs, *character_ids])
        pov_counts: dict[str, int] = {}
        for pov in previous_povs:
            label = names.get(pov) or pov
            pov_counts[label] = pov_counts.get(label, 0) + 1
        return {
            "recent_pov_distribution": pov_counts,
            "onstage_characters": [names[cid] for cid in character_ids if names.get(cid)],
        }

    def _character_names(self, character_ids: list[str]) -> dict[str, str]:
        wanted = sorted({cid for cid in character_ids if cid})
        if not wanted:
            return {}
        return {
            row.character_id: row.display_name
            for row in self.session.execute(
                select(StoryCharacter).where(StoryCharacter.character_id.in_(wanted))
            ).scalars()
            if row.display_name
        }

    def _constraints_slot(self, chapter: ChapterGoal) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        drama = dict(narrative.get("drama") or {})
        return {
            "forbidden": str(drama.get("forbidden") or ""),
            "must_not": str(chapter.must_not or ""),
            "notes": str(drama.get("notes") or ""),
        }


def _truncate_value(value: Any, budget: int) -> Any:
    """按字符预算截断 canon 摘要；结构保持 JSON 可序列化。"""
    if isinstance(value, str):
        return value if len(value) <= budget else value[:budget] + "…（截断）"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        remaining = budget
        for key, item in value.items():
            out[key] = _truncate_value(item, max(80, remaining))
            if isinstance(out[key], str):
                remaining -= len(out[key])
            if remaining <= 0:
                break
        return out
    if isinstance(value, list):
        out_list = []
        remaining = budget
        for item in value:
            trimmed = _truncate_value(item, max(80, remaining))
            out_list.append(trimmed)
            remaining -= len(canonical_json(normalize(trimmed)))
            if remaining <= 0:
                break
        return out_list
    return value

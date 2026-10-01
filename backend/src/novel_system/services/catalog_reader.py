"""目录的读取面（B08-24 从 ``catalog.py`` 拆出）：整本目录 / 单章 / 单场的载荷、场景三问、字数汇总。

``CatalogService``（``catalog.py``）继承它、再加上写入口；外面照旧 ``CatalogService(session).catalog(...)`` /
``.read_context(...)`` / ``.chapter_payload(...)``。载荷形状与键的说明见 ``catalog.py`` 模块说明。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.catalog_labels import (
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    chapter_title,
    normalize_act,
    scene_display_title,
    scene_kind,
)
from novel_system.services.chapter_structure_ownership import (
    live_chapter_plans_by_catalog_id,
    story_scene_numbers,
    structure_owned_by_plan,
)
from novel_system.services.chapter_title_sync import is_auto_chapter_title
from novel_system.services.scene_design_ownership import design_owned_by_plan, is_snowflake_origin, live_plan_scene_ids
from novel_system.services.scene_lookup import active_chapter_scenes, require_project
from novel_system.services.story_slots import planned_chapter_goal


class CatalogReader:
    def __init__(self, session: Session) -> None:
        self.session = session

    def catalog(self, project_id: str) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        chapters = self.chapter_rows(project_id)
        context = self.read_context(project_id, [chapter.chapter_id for chapter in chapters])
        return {
            "project_id": project_id,
            "chapters": [
                self.chapter_payload(project, chapter, index, context=context)
                for index, chapter in enumerate(chapters)
            ],
        }

    def read_context(self, project_id: str, chapter_ids: list[str]) -> dict[str, Any]:
        """整本目录一次读完要用的查表（各章场景卡、场景三问、角色名、场景管线状态……）——逐章 / 逐场去查是 N+1
        （80 章的目录过去要 173 条查询，B08-12）。查询条数与章数、场数无关。

        场景卡与管线状态按章号取（不按 ``SceneCard.project_id``）：v1 建的旧场景卡没有 project_id，
        归属是从章上推出来的。
        """
        names = {
            row.character_id: row.display_name or ""
            for row in self.session.execute(
                select(StoryCharacter).where(StoryCharacter.project_id == project_id)
            ).scalars()
        }
        scenes_by_chapter: dict[str, list[SceneCard]] = {chapter_id: [] for chapter_id in chapter_ids}
        run_states: dict[str, SceneRunState] = {}
        story_checks: dict[str, dict[str, Any]] = {}
        if chapter_ids:
            for scene in self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id.in_(chapter_ids), SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars():
                scenes_by_chapter.setdefault(str(scene.chapter_id), []).append(scene)
            run_states = {
                row.scene_id: row
                for row in self.session.execute(
                    select(SceneRunState)
                    .join(SceneCard, SceneCard.scene_id == SceneRunState.scene_id)
                    .where(SceneCard.chapter_id.in_(chapter_ids), SceneCard.trashed_flag == 0)
                ).scalars()
            }
            story_checks = self._story_checks_where(
                SceneCard.chapter_id.in_(chapter_ids), SceneCard.trashed_flag == 0
            )
        return {
            "character_names": names,
            "run_states": run_states,
            "scenes_by_chapter": scenes_by_chapter,
            "story_checks": story_checks,
            "plan_scene_ids": live_plan_scene_ids(self.session, project_id),
            # 阶段 Z：哪些目录章被构思的分章钉着、每一场在故事序上是第几场
            "chapter_plans": live_chapter_plans_by_catalog_id(self.session, project_id),
            "story_numbers": story_scene_numbers(self.session, project_id),
        }

    def chapter_rows(self, project_id: str) -> list[ChapterGoal]:
        """作品的活跃章按章序排好（没有章序的排最后，同序按 chapter_id）。纯读——章序由写入口压实
        （``catalog_ordering.compact_chapter_orders``），读取不再顺手写库（B08-14，迁移 0097）。"""
        rows = list(
            self.session.execute(
                select(ChapterGoal).where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
            ).scalars().all()
        )
        rows.sort(key=lambda c: (c.display_order is None, c.display_order or 0, c.chapter_id))
        return rows

    def scene_rows(self, chapter_id: str) -> list[SceneCard]:
        return active_chapter_scenes(self.session, chapter_id)

    def chapter_payload(
        self,
        project: StoryProject,
        chapter: ChapterGoal,
        index: int,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        brief = dict(chapter.writer_brief_json or {})
        slug = f"ch{index + 1:02d}"
        if context is None:
            # 单章回包（建章 / 改章）与整本目录说同一套话：结构归属、故事序场次、设计归属都要查表。
            # 逐章循环的调用方应当自己先取一次 read_context 传进来（review_derived / project_overview 都是）。
            context = self.read_context(project.project_id, [chapter.chapter_id])
        scenes = (context.get("scenes_by_chapter") or {}).get(chapter.chapter_id)
        if scenes is None:
            scenes = self.scene_rows(chapter.chapter_id)
        words_cur = sum(int(s.words_current or 0) for s in scenes)
        story_checks = context.get("story_checks")
        if story_checks is None:
            story_checks = self.story_checks([s.scene_id for s in scenes])
        title = chapter_title(chapter)
        origin = "snowflake" if is_snowflake_origin(brief) else "manual"
        # 没规划（空串、旧物化补的「推进本章：<章名>」）就是空：章节编排显示平常的空状态（S2 1）
        goal = planned_chapter_goal(chapter.chapter_goal, chapter).strip()
        summary = planned_chapter_goal(chapter.main_plot_push, chapter).strip()
        return {
            "chapter_id": chapter.chapter_id,
            "slug": slug,
            "no": f"{index + 1:02d}",
            "title": title,
            "state": str(chapter.state or "planned"),
            "current": chapter.chapter_id == project.current_chapter_id,
            "words": {"cur": words_cur, "target": chapter.words_target},
            "act": normalize_act(narrative.get("act")),
            "promise": narrative.get("promise"),
            "drama": dict(narrative.get("drama") or {}),
            # 阶段 X：章从哪来、构思里给它写了什么——台子上不用再开雪花工作台去对
            "origin": origin,
            "summary": summary if summary != title else "",
            "goal": goal if goal != title else "",
            "spine": str(narrative.get("spine") or "").strip(),
            # 阶段 Z：这一章的结构归谁改、它是章表里的哪一行、装着故事序上第几到第几场
            "structure": self._chapter_structure(chapter, title=title, context=context),
            "scenes": [
                self.scene_payload(
                    scene,
                    chapter_slug=slug,
                    story_check=story_checks.get(scene.scene_id),
                    context=context,
                    chapter=chapter,
                )
                for scene in scenes
            ],
        }

    def _chapter_structure(
        self,
        chapter: ChapterGoal,
        *,
        title: str,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """这一章的结构在哪里改（见 ``chapter_structure_ownership``）。

        ``title_auto``：章名还是系统起的占位（「第 N 章」）——章节编排据此提醒「还没起名」，与分章面板的
        「AI 起章名」同一条判定。
        """
        pinned = (context or {}).get("chapter_plans")
        if pinned is None:
            pinned = live_chapter_plans_by_catalog_id(self.session, chapter.project_id)
        if not structure_owned_by_plan(chapter, pinned):
            return {"owner": "desk", "row_uid": "", "scene_range": None, "planned_scene_count": 0, "title_auto": False}
        plan = pinned[chapter.chapter_id]
        numbers = (context or {}).get("story_numbers")
        if numbers is None:
            numbers = story_scene_numbers(self.session, chapter.project_id)
        span = (numbers.get("by_chapter_plan_id") or {}).get(plan.chapter_plan_id)
        return {
            "owner": "plan",
            "row_uid": plan.row_uid,
            "scene_range": {"first": span["first"], "last": span["last"]} if span else None,
            "planned_scene_count": int(span["count"]) if span else 0,
            "title_auto": is_auto_chapter_title(title),
        }

    def story_checks(self, scene_ids: list[str]) -> dict[str, dict[str, Any]]:
        """阶段 D：每场最近一次准定稿评审的场景三问（无评审或那次评审没有三问 → 不在结果里）。"""
        if not scene_ids:
            return {}
        return self._story_checks_where(SceneCard.scene_id.in_(scene_ids))

    def _story_checks_where(self, *scene_filters: Any) -> dict[str, dict[str, Any]]:
        """一条查询：按场取最近一次（attempt_id 最大）准定稿评审，只读它的 details_json——不再把每场的评审历史全拉回来。"""
        latest = (
            select(
                AttemptTracker.scene_id.label("scene_id"),
                func.max(AttemptTracker.attempt_id).label("attempt_id"),
            )
            .join(SceneCard, SceneCard.scene_id == AttemptTracker.scene_id)
            .where(AttemptTracker.step == "near_final_acceptance_review", *scene_filters)
            .group_by(AttemptTracker.scene_id)
            .subquery()
        )
        result: dict[str, dict[str, Any]] = {}
        for scene_id, details in self.session.execute(
            select(AttemptTracker.scene_id, AttemptTracker.details_json).join(
                latest, AttemptTracker.attempt_id == latest.c.attempt_id
            )
        ).all():
            check = (details or {}).get("scene_story_check") if isinstance(details, dict) else None
            if isinstance(check, dict):
                result[scene_id] = check
        return result

    def scene_payload(
        self,
        scene: SceneCard,
        *,
        chapter_slug: str,
        story_check: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        chapter: ChapterGoal | None = None,
    ) -> dict[str, Any]:
        kind = scene_kind(scene)
        brief_json = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        names = (context or {}).get("character_names")
        pov_id = str(scene.pov_character_id or "")
        # 场目标（整句摘要）只给作者规划过的：旧物化给没写摘要的场补的本章样板目标不算（按 ``chapter`` 的章名认）
        goal = planned_chapter_goal(scene.scene_goal, chapter)
        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            # 场景 slug = scene_id（稳定身份，见模块说明）；位置式旧 slug 只供前端迁移本机旧键
            "slug": scene.scene_id,
            "legacy_slug": f"{chapter_slug}s{scene.scene_seq}",
            "seq": scene.scene_seq,
            "title": scene_display_title(scene, goal=goal),
            "summary": goal.strip(),
            "kind": kind,
            "state": str(scene.state or "todo"),
            "words": int(scene.words_current or 0),
            "brief": {"kind": kind, **{key: str(brief_json.get(key) or "") for key in keys}},
            "pov_character_id": pov_id,
            "pov_character_name": self._character_name(pov_id, names),
            # 章节编排 LLM 规划（2026-07-16）可填的两个交接槽；可加性扩展，旧前端忽略即可。
            "exit_change": str(scene.exit_change or ""),
            "hook": str(scene.hook or ""),
            # 阶段 D：最近一次准定稿评审的场景三问（无评审则 null），成稿中心按场展示，非阻断
            "story_check": dict(story_check) if isinstance(story_check, dict) else None,
            # 阶段 X：整张设计卡 + 真实工作状态
            "design": self._scene_design(
                scene,
                kind=kind,
                names=names,
                plan_scene_ids=(context or {}).get("plan_scene_ids"),
                story_numbers=(context or {}).get("story_numbers"),
            ),
            "work": self._scene_work(scene, context=context),
        }

    def _character_name(self, character_id: str, names: dict[str, str] | None) -> str:
        if not character_id:
            return ""
        if names is not None and character_id in names:
            return names[character_id]
        # 查表里没有（旧角色行没带 project_id）：退回单行读取
        character = self.session.get(StoryCharacter, character_id)
        return (character.display_name or "") if character is not None else ""

    def _scene_design(
        self,
        scene: SceneCard,
        *,
        kind: str,
        names: dict[str, str] | None,
        plan_scene_ids: set[str] | None = None,
        story_numbers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """场景卡上的设计（雪花物化 / 回流写进去的，或作者在章节编排里填的）——只读地摊给台子。"""
        brief = dict(scene.writer_brief_json or {})

        def text(key: str) -> str:
            return str(brief.get(key) or "").strip()

        other = SCENE_BRIEF_RDD if kind == "proactive" else SCENE_BRIEF_GCS
        cast_ids = [str(item).strip() for item in (scene.onstage_chars_json or []) if str(item or "").strip()]
        rendering_mode = text("rendering_mode").lower() or "full"
        return {
            "origin": "snowflake" if is_snowflake_origin(brief) else "manual",
            # 阶段 Y：这张卡的设计在哪里改——``plan`` = 构思第 10 步（台子上只读），``desk`` = 就在台子上
            "owner": "plan" if design_owned_by_plan(self.session, scene, plan_scene_ids=plan_scene_ids) else "desk",
            # 阶段 Z：它在构思里是第几场（故事序，1 起；不在构思里的场为 0）——与分章面板、09 场景列表同一套编号
            "story_index": int(((story_numbers or {}).get("by_scene_id") or {}).get(scene.scene_id) or 0),
            "crucible": text("scene_crucible"),
            "location": str(scene.location or "").strip(),
            "story_time": text("story_time"),
            "cast": [{"character_id": cid, "name": self._character_name(cid, names) or cid} for cid in cast_ids],
            "reader_emotion": text("expected_reader_emotion"),
            "must_include": str(scene.must_include_text or "").strip(),
            "must_withhold": text("must_withhold"),
            "cost": text("cost_requirement"),
            "length_band": str(scene.target_length_band or "").strip(),
            "rendering_mode": rendering_mode if rendering_mode in {"full", "summary", "skip"} else "full",
            # 一场可以接着另一组三拍（阶段 I / N）：主形态在 brief，另一组在这里
            "followup": {key: text(key) for key in other if text(key)},
            "exception_reason": text("exception_reason"),
            "protagonist": text("protagonist_hint"),
            "is_chapter_last": bool(scene.is_chapter_last),
        }

    def _scene_work(self, scene: SceneCard, *, context: dict[str, Any] | None) -> dict[str, Any]:
        """这一场真实干到哪了：目录的 ``state`` 只是作者手打的标签，管线状态和定稿才是事实。"""
        run_states = (context or {}).get("run_states")
        state = run_states.get(scene.scene_id) if run_states is not None else self.session.get(SceneRunState, scene.scene_id)
        return {
            "run_status": str(state.scene_status or "ready") if state is not None else "",
            "has_final": bool(state is not None and state.current_final_scene_row_id),
            "has_words": int(scene.words_current or 0) > 0,
        }

    def words_rollup(self, scene: SceneCard) -> dict[str, Any]:
        chapter_words = sum(
            int(s.words_current or 0) for s in self.scene_rows(scene.chapter_id)
        )
        return {
            "scene_id": scene.scene_id,
            "scene_words": int(scene.words_current or 0),
            "chapter_id": scene.chapter_id,
            "chapter_words": chapter_words,
        }

    def _scene_payload_with_slug(self, scene: SceneCard) -> dict[str, Any]:
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        require_project(self.session, chapter.project_id)
        chapters = self.chapter_rows(chapter.project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter.chapter_id)
        return self.scene_payload(
            scene,
            chapter_slug=f"ch{index + 1:02d}",
            story_check=self.story_checks([scene.scene_id]).get(scene.scene_id),
            chapter=chapter,
        )

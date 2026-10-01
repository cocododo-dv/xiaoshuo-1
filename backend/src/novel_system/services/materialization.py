"""大纲计划 → 目录的物化引擎：批准一版大纲计划（雪花「确认写入」、v1 大纲批准）时把章与场景卡落进目录。

- ``_CatalogPlacement``：章序与场序的两阶段落位（不撞唯一索引，也不挤掉目录里已有的东西，手加的场跟着锚点走）；
- ``trash_emptied_snowflake_chapters``：重新分章后变空的雪花旧章移入回收站（雪花回流也调它）；
- ``materialize_outline_plan``：批准一版计划的整个过程（原来是 ``ProjectService.approve_outline_plan`` 里
  278 行的一个函数，B08-10），拆成准备 / 逐章 / 逐场 / 收尾几步。

从 ``projects.py`` 拆出（B08-09）；``projects`` 照旧再导出这里的名字。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    OutlinePlan,
    SceneCard,
    SceneRunState,
    StoryProject,
    utcnow,
)
from novel_system.services.catalog_ordering import compact_chapter_orders, park
from novel_system.services.catalog_placeholders import trash_pristine_placeholder_chapters
from novel_system.services.catalog_trash_cascade import revive_cascade_trashed_scene_cards
from novel_system.services.chapter_approval import is_chapter_approved
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.errors import DomainError
from novel_system.services.project_payloads import optional_text, outline_plan_payload, project_payload, string_list
from novel_system.services.project_status import (
    PLAN_STATUS_APPROVED,
    PROJECT_STATUS_CHAPTER_READY,
)
from novel_system.services.qc_constraints import strip_reference_policy
from novel_system.services.scene_design_ownership import is_snowflake_origin
from novel_system.services.scene_rehome import rehome_scenes


def planned_scene_id(chapter_id: str, index: int, scene_plan: dict[str, Any]) -> str:
    """计划里一场的场景 id：计划给了就用它，没给就按「章 id + 第几场」补（1 起）。"""
    return str(scene_plan.get("scene_id") or f"{chapter_id}_SC{index:02d}").strip()


def planned_scene_ids(chapter_plans: list[dict[str, Any]]) -> list[str]:
    """一版计划里全部场景的 id，按章表、章内顺序。"""
    return [
        planned_scene_id(str(item.get("chapter_id") or "").strip(), index, scene_plan)
        for item in chapter_plans
        for index, scene_plan in enumerate(item.get("scenes") or [], start=1)
    ]


#: 「重新分章后变空、被系统送进回收站」的标记（``trashed_by``）。带这个标记的章是空着进回收站的，
#: 以后的分章又用到它的章号时可以无损取回。
AUTO_TRASHED_EMPTY_CHAPTER = "snowflake_rechapter"


def trash_emptied_snowflake_chapters(
    session: Session,
    project_id: str,
    *,
    keep_chapter_ids: set[str],
    actor_ref: str = AUTO_TRASHED_EMPTY_CHAPTER,
    compact_orders: bool = True,
) -> list[dict[str, Any]]:
    """重新分章之后，把**雪花整理出来、现在已经空了**的旧章移入回收站（可恢复），返回被移走的章。

    6 章改成 4 章，被并掉的那两章的场景卡搬走之后就是两个空壳，挂在目录里还占着章序。
    以前只在面板上提醒作者自己去删。

    只动同时满足这几条的章：雪花物化 / 回流建的（不碰作者在章节编排里手建的章）、不在这一版分章里、
    一张场景卡都没有（活跃的、回收站里的都算——里面还有作者手加的场就原样保留）、没有终审通过。
    走回收站而不是物理删除：作者随时可以在回收站里取回。

    移走了章就顺手压实剩下的章序（``compact_orders``）；物化自己最后统一落位、压实，传 ``False``。
    """
    trashed: list[dict[str, Any]] = []
    now = utcnow()
    chapters = session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
    ).scalars().all()
    for chapter in chapters:
        # 是不是雪花建的章看来源（下面），不看 id 长什么样——阶段 Y 起章号只是序列号，没有位置含义
        if chapter.chapter_id in keep_chapter_ids:
            continue
        if not is_snowflake_origin(chapter.writer_brief_json):
            continue
        has_cards = session.execute(
            select(SceneCard.scene_id).where(SceneCard.chapter_id == chapter.chapter_id).limit(1)
        ).first()
        if has_cards is not None or is_chapter_approved(session, chapter):
            continue
        chapter.trashed_flag = 1
        chapter.trashed_at = now
        chapter.trashed_by = actor_ref
        title = str(
            (chapter.narrative_json or {}).get("title")
            or (chapter.writer_brief_json or {}).get("chapter_title")
            or chapter.chapter_id
        )
        trashed.append({"chapter_id": chapter.chapter_id, "title": title})
        session.add(
            OperationLog(
                event_type="snowflake_empty_chapter_trashed",
                object_type="chapter_goal",
                object_ref=chapter.chapter_id,
                payload_json={"project_id": project_id, "title": title, "trashed_at": now, "reason": "emptied_by_rechaptering"},
            )
        )
    if trashed:
        session.flush()
        if compact_orders:
            compact_chapter_orders(session, project_id)
    return trashed


class _CatalogPlacement:
    """批准大纲计划时章序与场序的落位——不撞唯一索引，也不挤掉目录里已经在的东西。

    两条唯一索引管着目录：``(project_id, display_order)``（活跃章）与 ``(chapter_id, scene_seq)``
    （活跃场景卡）。批准逻辑过去直接写计划里的序号、一张卡一张卡地搬，于是：

    - 目录里已有别的章时（作者手建的「第 1 章」、上一版物化留下的章），新章的 ``display_order``
      撞上去 → 整次批准 500「database operation failed」（2026-09-18 真实故障：作者点了「确认写入」，
      章节从没落进目录）；
    - 重新分章后再物化，场景卡跨章搬动：第一张卡要占的 ``(章, 序)`` 还被下一张没搬走的卡占着
      → 同样的 500。

    做法是两阶段：先把会受影响的活跃场景卡停到一段不会冲突的高位序号上，再按最终顺序落位。
    计划之外的卡（作者在章节编排里手加的场）不删不丢，**跟着它的锚点场走**：原来紧跟在哪张计划内的卡后面，
    现在还紧跟在它后面——哪怕那张卡换了章（阶段 Y：章 id 钉住之后，一章被大改时是另起新行而不是沿用位置，
    手加的场不跟过去就会被孤零零留在旧章里）；排在本章第一张计划内的卡之前的，跟着那张卡、排在它前面；
    整章都没有计划内的卡时原地不动。
    """

    def __init__(self, session: Session, project_id: str, chapter_plans: list[dict[str, Any]]) -> None:
        self.session = session
        self.project_id = project_id
        self._target_ids = [
            str(item.get("chapter_id") or "").strip() for item in chapter_plans if str(item.get("chapter_id") or "").strip()
        ]
        targets = set(self._target_ids)
        self._planned_scene_ids: set[str] = set(planned_scene_ids(chapter_plans))

        planned_cards = (
            list(
                session.execute(
                    select(SceneCard).where(
                        SceneCard.trashed_flag == 0, SceneCard.scene_id.in_(sorted(self._planned_scene_ids))
                    )
                ).scalars()
            )
            if self._planned_scene_ids
            else []
        )
        # 计划内的卡现在住着的章：它们要搬走，章里手加的卡得跟着锚点走
        self._source_chapter_ids = {str(card.chapter_id) for card in planned_cards}
        involved = sorted(targets | self._source_chapter_ids)
        cards = (
            [
                card
                for card in session.execute(
                    select(SceneCard).where(SceneCard.trashed_flag == 0, SceneCard.chapter_id.in_(involved))
                ).scalars()
                if card.chapter_id in targets or card.project_id == project_id or card.scene_id in self._planned_scene_ids
            ]
            if involved
            else []
        )
        cards.sort(key=lambda card: (str(card.chapter_id), int(card.scene_seq or 0), str(card.scene_id)))
        self._cards = {card.scene_id: card for card in cards}
        self._follow_after: dict[str, list[str]] = {}
        self._follow_before: dict[str, list[str]] = {}
        self._unanchored: dict[str, list[str]] = {}
        by_chapter: dict[str, list[SceneCard]] = {}
        for card in cards:
            by_chapter.setdefault(str(card.chapter_id), []).append(card)
        for chapter_id, members in by_chapter.items():
            anchor: str | None = None
            leading: list[str] = []
            for card in members:
                if card.scene_id in self._planned_scene_ids:
                    if leading:
                        self._follow_before.setdefault(card.scene_id, []).extend(leading)
                        leading = []
                    anchor = card.scene_id
                elif anchor is not None:
                    self._follow_after.setdefault(anchor, []).append(card.scene_id)
                else:
                    leading.append(card.scene_id)
            if leading:
                self._unanchored[chapter_id] = leading
        #: 手加的卡跟着锚点换了章：``{scene_id: (旧章, 新章)}``——运行时行要跟着走（scene_rehome）
        self.moved_followers: dict[str, tuple[str, str]] = {}
        self._sizes: dict[str, int] = {}
        # 最终序号由 final_scene_seq 一章一章写下，中途不再停车：停车位必须高过任何一章最终的场数
        park(session, cards, "scene_seq")

    def settle_chapter_order(self) -> str:
        """计划内的章在目录里按这一版章表的顺序排。返回 ``settled`` / ``unchanged`` / ``held_by_approved``。

        阶段 Y：章 id 钉在章计划上之后，目录里的章不再「天然」按位置排好——拆一章多出来的新章要插在它
        前半截的后面，而不是接到全书最后。规则和场景卡的落位同一个口径：

        - 计划内的章：严格按章表的顺序；
        - 计划之外的章（作者手建的章、里面还有手加场而留下来的旧章）：原来跟在哪一章后面，现在还跟在它后面；
          排在所有计划内的章之前的，仍然排在最前面；
        - 已终审的章不挪——和章节编排的拖动排序同一条规矩（``CatalogService.reorder_chapters``：终审章的相对
          顺序与目录位置都锁着）：算出来的顺序会让某个终审章换位置，就整个不动，新章照旧接在最后
          （``held_by_approved``，回执里告诉作者），由作者先到成稿中心重新打开再整理一次。

        不补空号——物化最后统一压实（``catalog_ordering.compact_chapter_orders``）。
        """
        active = list(
            self.session.execute(
                select(ChapterGoal).where(ChapterGoal.project_id == self.project_id, ChapterGoal.trashed_flag == 0)
            ).scalars()
        )
        active.sort(key=lambda item: (item.display_order is None, int(item.display_order or 0), item.chapter_id))
        by_id = {item.chapter_id: item for item in active}
        targets = [chapter_id for chapter_id in self._target_ids if chapter_id in by_id]
        target_set = set(targets)
        followers: dict[str | None, list[str]] = {}
        anchor: str | None = None
        for item in active:
            if item.chapter_id in target_set:
                # 刚建出来的章还没有章序（排在最后），不能当别的章的锚
                if item.display_order is not None:
                    anchor = item.chapter_id
            else:
                followers.setdefault(anchor, []).append(item.chapter_id)
        desired = list(followers.get(None, []))
        for chapter_id in targets:
            desired.append(chapter_id)
            desired.extend(followers.get(chapter_id, []))
        current = [item.chapter_id for item in active]
        if desired == current and all(item.display_order is not None for item in active):
            return "unchanged"
        moved_approved = [
            chapter_id
            for index, chapter_id in enumerate(desired)
            if current.index(chapter_id) != index and is_chapter_approved(self.session, by_id[chapter_id])
        ]
        if moved_approved:
            # 不动已终审的章：只给还没有章序的新章在最后补号（旧行为）
            tail = max((int(item.display_order or 0) for item in active), default=0)
            for item in active:
                if item.display_order is None:
                    tail += 1
                    item.display_order = tail
            self.session.flush()
            return "held_by_approved"
        # (project_id, display_order) 在活跃章上唯一：两阶段落位，先挪到不会撞的高位再写最终值
        park(self.session, [by_id[chapter_id] for chapter_id in desired], "display_order")
        for index, chapter_id in enumerate(desired, start=1):
            by_id[chapter_id].display_order = index
        self.session.flush()
        return "settled"

    def final_scene_seq(self, chapter_id: str, scene_plans: list[dict[str, Any]]) -> dict[str, int]:
        """这一章里每张计划内场景卡的最终序号；跟着它们走的计划外的卡在这里一并落到新位置（必要时换章）。"""
        planned = [
            planned_scene_id(chapter_id, index, scene_plan) for index, scene_plan in enumerate(scene_plans, start=1)
        ]
        staying = set(planned)
        merged = [scene_id for scene_id in self._unanchored.get(chapter_id, []) if scene_id not in staying]
        for scene_id in planned:
            merged.extend(self._follow_before.get(scene_id, []))
            merged.append(scene_id)
            merged.extend(self._follow_after.get(scene_id, []))
        self._sizes[chapter_id] = len(merged)
        final = {scene_id: index for index, scene_id in enumerate(merged, start=1)}
        for scene_id, seq in final.items():
            if scene_id in staying:
                continue
            card = self._cards[scene_id]
            if str(card.chapter_id) != chapter_id:
                self.moved_followers[scene_id] = (str(card.chapter_id), chapter_id)
                card.chapter_id = chapter_id
            card.scene_seq = seq
            card.is_chapter_last = 1 if seq == len(merged) else 0
        return final

    def chapter_size(self, chapter_id: str) -> int:
        return self._sizes.get(chapter_id, 0)

    def finish(self) -> None:
        """场景卡被搬走的源章（不在这次计划里的章）：剩下的卡重算「章末」标记。"""
        self.session.flush()
        for chapter_id in self._source_chapter_ids - set(self._target_ids):
            remaining = list(
                self.session.execute(
                    select(SceneCard).where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                ).scalars()
            )
            if not remaining:
                continue
            last = max(remaining, key=lambda card: (int(card.scene_seq or 0), str(card.scene_id)))
            for card in remaining:
                card.is_chapter_last = 1 if card is last else 0


def materialize_outline_plan(session: Session, project: StoryProject, plan: OutlinePlan) -> dict[str, Any]:
    """批准一版待审的大纲计划：章与场景卡落进目录，计划标为已批准，作品回到「可以运行本章」。"""
    return _MaterializationRun(session, project, plan).run()


class _MaterializationRun:
    """一次物化：``_prepare`` → 逐章 ``_upsert_chapter`` / 逐场 ``_upsert_scene`` → ``_finish``。同一个事务，步骤顺序不变。"""

    def __init__(self, session: Session, project: StoryProject, plan: OutlinePlan) -> None:
        self.session = session
        self.project = project
        self.plan = plan
        plan_json = plan.plan_json or {}
        self.chapters: list[dict[str, Any]] = list(plan_json.get("chapters") or [])
        self.source = plan_json.get("source") or "project_outline_plan"
        self.target_chapter_ids = {str(item.get("chapter_id") or "").strip() for item in self.chapters}
        self.created_chapter_count = 0
        self.created_scene_count = 0
        self.restored_chapter_ids: list[str] = []
        self.restored_scene_ids: list[str] = []
        self.trashed_placeholder_chapters: list[dict[str, Any]] = []
        self.moved_scenes: dict[str, tuple[str, str]] = {}  # 阶段 Y：换了章的场，运行时行要跟着走

    def run(self) -> dict[str, Any]:
        if not self.chapters:
            raise DomainError(
                "OUTLINE_PLAN_EMPTY", "outline plan has no chapters", status_code=422
            )
        self._prepare()
        placement = _CatalogPlacement(self.session, self.project.project_id, self.chapters)
        for chapter_plan in self.chapters:
            chapter = self._upsert_chapter(chapter_plan)
            scenes = list(chapter_plan.get("scenes") or [])
            final_seq = placement.final_scene_seq(chapter.chapter_id, scenes)
            for index, scene_plan in enumerate(scenes, start=1):
                self._upsert_scene(chapter, scene_plan, index, final_seq, placement)
        return self._finish(placement)

    def _prepare(self) -> None:
        """落位之前：移走手建的空白占位章，取回随旧章一起进了回收站的计划内场景卡。"""
        # 阶段 X：雪花的章进目录之前，先把手建的空白占位章（「第 1 章 / 开场」，一个字没写）移入回收站——
        # 必须在落位之前：章序（settle_chapter_order）是按那一刻还活跃的章排的，占位章留着就会排在雪花的章前面。
        # 只对雪花计划做；作者写过东西的章不动。
        if str(self.source) == "snowflake_method":
            self.trashed_placeholder_chapters = trash_pristine_placeholder_chapters(
                self.session,
                self.project.project_id,
                keep_chapter_ids=self.target_chapter_ids,
            )
        # 这一版章表里的场，场景卡却是随着旧章一起进回收站的（作者先删了旧章、再回来重新分章）：落位之前取回——
        # 不取回的话卡会被搬进新章、自己却还躺在回收站里，目录里看不见。作者单独删掉的卡不动
        # （见 catalog_trash_cascade）。必须在 _CatalogPlacement 之前：落位只认活跃的卡。
        self.restored_scene_ids = revive_cascade_trashed_scene_cards(
            self.session,
            self.project.project_id,
            planned_scene_ids(self.chapters),
            target_chapter_ids=[str(item.get("chapter_id") or "").strip() for item in self.chapters],
        )
        if self.restored_scene_ids:
            self.session.add(
                OperationLog(
                    event_type="snowflake_scene_cards_revived",
                    object_type="story_project",
                    object_ref=self.project.project_id,
                    payload_json={
                        "project_id": self.project.project_id,
                        "outline_plan_id": self.plan.plan_id,
                        "scene_ids": self.restored_scene_ids,
                        "reason": "trashed_with_previous_chapter",
                    },
                )
            )

    def _upsert_chapter(self, chapter_plan: dict[str, Any]) -> ChapterGoal:
        project, plan = self.project, self.plan
        chapter_id = str(chapter_plan.get("chapter_id") or "").strip()
        if not chapter_id:
            raise DomainError(
                "OUTLINE_PLAN_INVALID", "chapter_id is required", status_code=422
            )
        chapter = self.session.get(ChapterGoal, chapter_id)
        is_new_chapter = chapter is None
        if chapter is None:
            chapter = ChapterGoal(chapter_id=chapter_id, chapter_goal="")
            self.session.add(chapter)
            self.created_chapter_count += 1
        elif chapter.project_id and chapter.project_id != project.project_id:
            raise DomainError(
                "CHAPTER_ALREADY_OWNED", "chapter belongs to another project"
            )
        elif int(chapter.trashed_flag or 0) == 1:
            # 章计划钉着的目录章此刻躺在回收站里：这一章留在章表里、场被挪空（目录那一行空了 → 进回收站），
            # 之后场又挪回这一章；或者作者手动删过这一章。不取回的话场景卡会被搬进一个目录里看不见的章。
            # 取回时当新章对待（章名 / 章序按这一版重新播种）；和它一起进回收站的计划内场景卡已经在上面
            # 取回了（revive_cascade_trashed_scene_cards），作者更早单独删掉的卡不动。
            chapter.trashed_flag = 0
            chapter.trashed_at = None
            chapter.trashed_by = None
            chapter.display_order = None
            is_new_chapter = True
            self.restored_chapter_ids.append(chapter_id)

        chapter.project_id = project.project_id
        chapter.outline_plan_id = plan.plan_id
        chapter.planned_scene_count = len(chapter_plan.get("scenes") or [])
        chapter.mid_aggregate_enabled = 0
        # 计划里没有章目标就存空串（列是 NOT NULL）：不拿章名 / 章 id 冒充章目标——起草提示会把它当作者定的目标印出来，
        # 章节编排会把它当章目标给作者看（S2 1）。读的地方把空当「没规划」。
        chapter.chapter_goal = str(chapter_plan.get("chapter_goal") or "")
        # 目录侧读章名的首选字段是 narrative_json["title"]（catalog_labels.chapter_title）。
        # 雪花物化以前不写它，于是作者在 07 里起的章名到不了目录，用户看到的是章 id
        # 字符串。这里补上 —— 但**只在新建章时播种**：narrative_json / display_order
        # 是目录侧的权威字段（章节编排里能改名、能重排），重新物化不得把作者在那边
        # 的改动冲掉。
        if is_new_chapter:
            narrative = dict(chapter_plan.get("narrative_json") or {})
            if narrative:
                chapter.narrative_json = {
                    **dict(chapter.narrative_json or {}),
                    **narrative,
                }
            # 章序不在这里逐章写：所有章处理完之后由 placement.settle_chapter_order() 按这一版章表统一落位
            # （直接写计划里的序号会撞 (project_id, display_order) 唯一索引；阶段 Y 起已有的章也可能要换位）。
        else:
            # 重新物化：章名 / 幕 / 脊柱跟着这一版计划走——**只要目录里的值还是上一次物化播下去的那个**
            # （上一次播的值留在 writer_brief_json 里）。不更新的话，作者在分章面板里改过再确认的章名
            # （含 AI 起的）到不了目录，目录挂着上一版的名字。
            # 作者在章节编排里亲手改过的（目录值 ≠ 上次播的值）照旧不碰。
            seeded = dict(chapter.writer_brief_json or {})
            current = dict(chapter.narrative_json or {})
            incoming = dict(chapter_plan.get("narrative_json") or {})
            followed = {
                key: incoming[key]
                for key, seeded_key in (("title", "chapter_title"), ("act", "chapter_act"), ("spine", "chapter_spine"))
                if key in incoming and (key not in current or current.get(key) == seeded.get(seeded_key))
            }
            if followed:
                chapter.narrative_json = {**current, **followed}
        chapter.main_plot_push = optional_text(chapter_plan.get("main_plot_push"))
        chapter.emotional_target = optional_text(
            chapter_plan.get("emotional_target")
        )
        chapter.ending_effect = optional_text(chapter_plan.get("ending_effect"))
        chapter.must_not = optional_text(chapter_plan.get("must_not"))
        chapter.notes = optional_text(chapter_plan.get("notes"))
        # 简报里不再抄那份固定的「参考书安全规则」清单（S2 2）：没有人写过，也没有哪一处读它；旧行上的清单原样留着，
        # 下一次确认写入整张换掉简报时随之消失。
        chapter.writer_brief_json = {
            "source": self.source,
            "project_id": project.project_id,
            "outline_plan_id": plan.plan_id,
            "chapter_title": chapter_plan.get("title"),
            **dict(chapter_plan.get("writer_brief_json") or {}),
        }
        # ChapterState/SceneCard both carry immediate SQLite FKs to this row.
        self.session.flush()
        ensure_chapter_state(self.session, chapter.chapter_id)
        return chapter

    def _upsert_scene(
        self,
        chapter: ChapterGoal,
        scene_plan: dict[str, Any],
        index: int,
        final_seq: dict[str, int],
        placement: "_CatalogPlacement",
    ) -> None:
        project, plan = self.project, self.plan
        chapter_id = chapter.chapter_id
        scene_id = planned_scene_id(chapter_id, index, scene_plan)
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            scene = SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                scene_seq=final_seq[scene_id],
                scene_goal="",
            )
            self.session.add(scene)
            self.created_scene_count += 1
        elif scene.project_id and scene.project_id != project.project_id:
            raise DomainError(
                "SCENE_ALREADY_OWNED", "scene belongs to another project"
            )

        if scene.chapter_id and scene.chapter_id != chapter_id:
            self.moved_scenes[scene_id] = (str(scene.chapter_id), chapter_id)
        scene.chapter_id = chapter_id
        scene.project_id = project.project_id
        scene.outline_plan_id = plan.plan_id
        scene.scene_seq = final_seq[scene_id]
        scene.pov_character_id = optional_text(
            scene_plan.get("pov_character_id")
        )
        scene.onstage_chars_json = string_list(
            scene_plan.get("onstage_chars_json")
        )
        scene.location = optional_text(scene_plan.get("location"))
        scene.scene_goal = str(
            scene_plan.get("scene_goal") or chapter.chapter_goal
        )
        # 没有节拍就拿场目标当唯一一拍；场目标也没规划（空串）就没有节拍——不写一个空拍
        scene.beats_json = string_list(scene_plan.get("beats_json")) or (
            [scene.scene_goal] if scene.scene_goal.strip() else []
        )
        scene.must_include_text = optional_text(
            scene_plan.get("must_include_text")
        )
        # 计划没给禁用词就留空：防抄袭政策句不是「按字面查的禁用词」（见 qc_constraints）；
        # 重新物化顺手把旧卡上那一句清掉。
        scene.forbidden_text = optional_text(strip_reference_policy(scene_plan.get("forbidden_text")))
        scene.exit_change = optional_text(scene_plan.get("exit_change"))
        scene.hook = optional_text(scene_plan.get("hook"))
        scene.target_length_band = (
            optional_text(scene_plan.get("target_length_band")) or "medium"
        )
        scene.scene_type = (
            optional_text(scene_plan.get("scene_type")) or "outline_driven"
        )
        scene.is_chapter_last = 1 if final_seq[scene_id] == placement.chapter_size(chapter_id) else 0
        previous_brief = dict(scene.writer_brief_json or {})
        incoming_brief = {
            "source": self.source,
            "project_id": project.project_id,
            "outline_plan_id": plan.plan_id,
            **dict(scene_plan.get("writer_brief_json") or {}),
        }
        # 阶段 X：作者在台子上给这一场改过的题名（≠ 上次物化播下去的）跨重新物化保留；
        # 其余设计键整张按这一版计划重写，「台面改动」记号随之清掉（简报是整张替换的）。
        kept_title = str(previous_brief.get("title") or "").strip()
        if kept_title and kept_title != str(previous_brief.get("seeded_title") or "").strip():
            incoming_brief["title"] = kept_title
        scene.writer_brief_json = incoming_brief
        # SceneRunState has an immediate FK to SceneCard and the ORM models
        # intentionally do not declare relationships for dependency ordering.
        self.session.flush()

        if self.session.get(SceneRunState, scene.scene_id) is None:
            self.session.add(
                SceneRunState(scene_id=scene.scene_id, scene_status="ready")
            )

    def _finish(self, placement: "_CatalogPlacement") -> dict[str, Any]:
        """搬完之后：运行时行跟着换了章的场走、移走空章、按章表落位章序、计划标为已批准、放好书签。"""
        project, plan = self.project, self.plan
        placement.finish()
        self.session.flush()
        self.moved_scenes.update(placement.moved_followers)
        if self.moved_scenes:
            rehome_scenes(self.session, project.project_id, self.moved_scenes)
        trashed_empty_chapters = trash_emptied_snowflake_chapters(
            self.session,
            project.project_id,
            keep_chapter_ids=self.target_chapter_ids,
            compact_orders=False,
        )
        chapter_order = placement.settle_chapter_order()
        plan.status = PLAN_STATUS_APPROVED
        plan.approved_at = utcnow()
        project.active_outline_plan_id = plan.plan_id
        # 「当前章」是作者的书签。第一次物化（或书签指着的章已经不在目录里——占位章刚被移走、
        # 旧章重新分章后空了）才把它放到这一版的第一章；重新物化不得把写到第 8 章的作者拽回第 1 章。
        bookmark = self.session.get(ChapterGoal, project.current_chapter_id) if project.current_chapter_id else None
        if (
            bookmark is None
            or bookmark.project_id != project.project_id
            or int(bookmark.trashed_flag or 0) == 1
        ):
            project.current_chapter_id = str(self.chapters[0]["chapter_id"])
        project.status = PROJECT_STATUS_CHAPTER_READY
        # 移走占位章 / 空章、取回回收站里的章之后，按章表落位没有排到的空号在这里压实
        # （「未变」「被终审章挡住」两种落位结果不补空号；过去这一步由下一次目录读取顺手做）
        compact_chapter_orders(self.session, project.project_id)
        self.session.flush()
        return {
            "project": project_payload(project),
            "plan": outline_plan_payload(plan),
            "created_chapter_count": self.created_chapter_count,
            "created_scene_count": self.created_scene_count,
            "restored_chapter_ids": self.restored_chapter_ids,
            # 随旧章一起进了回收站、这一版章表里又有的场：场景卡已取回
            "restored_scene_ids": self.restored_scene_ids,
            "trashed_empty_chapters": trashed_empty_chapters,
            "trashed_placeholder_chapters": self.trashed_placeholder_chapters,
            # 阶段 Y：目录里有已终审的章、按章表排会挪动它 → 这次没排，新章接在最后
            "chapter_order_held": chapter_order == "held_by_approved",
        }

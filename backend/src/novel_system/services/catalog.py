"""FE-ALIGN Phase 3: 目录统一 —— ChapterGoal/SceneCard 之上的章节/场景树服务。

`GET/PATCH /api/v2/projects/{id}/catalog…` 的服务层；雪花物化（approve_outline_plan）
创建的行与本服务读写的是同一批行（护栏测试 test_catalog_single_source.py）。

约定：
- 章顺序 = display_order（混合 chapter_id 格式下不能依赖字典序）。读取不写库：改动章集合的写入口自己压实章序
  （``catalog_ordering.compact_chapter_orders``；库里原有的漂移由迁移 0097 一次补齐）。
- 场景顺序 = 既有 scene_seq（与 v1 scene-order 端点同一套逻辑，不另建列）。
- 章标题写 narrative_json["title"]；读取回退 writer_brief_json.chapter_title →
  writer_brief_json.title → chapter_goal 首行。
- slug 不入库。章 slug = "ch"+序号两位（位置式，只当显示序号与界面键用）；**场景 slug = scene_id**
  （阶段 X，2026-09-19）。它过去是位置式的 ``ch08s3``，而前端所有按场景落地的本机状态
  （写作台的草稿绑定与读缓存、AI 起草台的运行记录与队列、场景笔记）都拿它当身份：目录里任何一次
  结构变动——雪花重新分章 / 回流搬场、手动增删章、场景重排——都会让同一个 slug 指向另一场，
  于是正文存进别的场的草稿、A 场的 AI 稿被采用到 B 场。场景的身份必须跟着行走，不跟着位置走。
  旧的位置式 slug 仍以 ``legacy_slug`` 给出，只供前端把旧键一次性迁到新键。
- C4 裁决：scene brief 按 kind 返回 GCS（proactive）或 RDD（reactive），
  前端 store 适配层负责映射到视图槽位。
- 阶段 X「一条书脊」：目录是构思（雪花）与三张台子（章节编排 / 写作台 / AI 起草台）之间唯一的交接面，
  所以场景载荷带上整张设计卡（``design``：坩埚、地点、时间、出场、读者情绪、必须包含 / 隐瞒、
  代价、篇幅带、呈现方式、后续三拍、破例理由）与真实的工作状态（``work``：管线状态、有无定稿），
  章载荷带上来源（``origin``）、章摘要、章目标与脊柱标记——台子不再各自去猜，也不必再开雪花工作台。
- 阶段 Z「一张章表、两扇门」：章载荷的 ``structure`` 说这一章的结构归谁（``plan`` = 构思的分章钉着它：
  先后与幕只在「整理章节结构」里改，目录 API 409；章名两边改的是同一个，写穿到章计划）、它在章表里是哪一行、
  装着故事序上第几到第几场；场景载荷的 ``design.story_index`` 是它在构思里的场次——章节编排、分章面板、
  09 场景列表说的是同一套编号。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryCharacter,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.catalog_import import import_catalog
from novel_system.services.catalog_labels import (  # noqa: F401  (re-exported: 目录的读者从这里拿)
    CATALOG_ACTS,
    CHAPTER_STATES,
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    SCENE_STATES,
    SCENE_TITLE_MAX_CHARS,
    chapter_title,
    focus_scene_payload,
    normalize_act,
    parse_scene_kind,
    scene_display_title,
    scene_kind,
    scene_title,
    short_scene_title,
)
from novel_system.services.catalog_ordering import compact_chapter_orders, reseat_display_orders, reseat_scene_seqs
from novel_system.services.catalog_reader import CatalogReader
from novel_system.services.chapter_approval import (
    is_chapter_approved,
    require_chapter_mutation_allowed,
)
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.chapter_structure_ownership import (
    chapter_structure_owned_by_plan_action,
    live_chapter_plans_by_catalog_id,
    structure_owned_by_plan,
)
from novel_system.services.chapter_title_sync import adopt_catalog_title
from novel_system.services.errors import DomainError
from novel_system.services.project_status import PROJECT_STATUS_CHAPTER_FINAL_REVIEW
from novel_system.services.scene_design_ownership import (  # noqa: F401  (re-exported: 目录的读者从这里拿)
    PLAN_OWNED_SCENE_FIELDS,
    SNOWFLAKE_SOURCES,
    design_owned_by_plan,
    design_owned_by_plan_action,
    is_snowflake_origin,
    live_plan_scene_ids,
    plan_owned_scene_ids,
    scene_order_owned_by_plan_action,
)
from novel_system.services.scene_lookup import require_project, require_project_chapter, require_scene

# narrative_json 里由目录 API 维护的字段。章级的张力 / 视角 / 时间 / 地点 / 入口 / 出口 / 衔接 / 线索（批准 #17a）
# 没有任何地方能填、也没有程序写它们，不再读、不再写、不再下发——库里已有的旧值原样留着；
# 章节编排里看到的视角、时间、地点、入口出口从各场读出来。
NARRATIVE_FIELDS = (
    "title",
    "act",
    "promise",
    "drama",
    "notes",
)

class CatalogService(CatalogReader):
    """目录服务：读取面在 ``CatalogReader``，这里是写入口（建 / 改章与场、调章序与场序、旧版整批导入）。"""

    # ---------- 写 ----------

    def update_chapter(
        self,
        project_id: str,
        chapter_id: str,
        payload: dict[str, Any],
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        body = payload or {}
        updates: dict[str, Any] = {}
        if "state" in body:
            state = str(body["state"] or "").strip()
            if state not in CHAPTER_STATES:
                raise DomainError("CATALOG_STATE_INVALID", f"chapter state must be one of {CHAPTER_STATES}", status_code=400)
            if state == "approved" and not is_chapter_approved(self.session, chapter):
                raise DomainError(
                    "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                    "chapter approval must use the project final-approval flow",
                    status_code=409,
                )
            if (
                state != "approved"
                and state != chapter.state
                and is_chapter_approved(self.session, chapter)
            ):
                raise DomainError(
                    "CATALOG_APPROVED_CHAPTER_REOPEN_REQUIRED",
                    "approved chapter state can change only after reopening its final",
                    status_code=409,
                )
            # ``review`` is also used by the catalog as an editorial/structural
            # label while an approved outline is being maintained.  Canonical
            # FinalScene coverage is required only when this PATCH is the
            # project's real final-review submission; outline materialization
            # and re-materialization happen in ``chapter_ready`` before prose
            # exists and must remain valid structural operations.
            if (
                state == "review"
                and project.status == PROJECT_STATUS_CHAPTER_FINAL_REVIEW
                and project.current_chapter_id == chapter.chapter_id
            ):
                from novel_system.services.chapter_manuscripts import (
                    ChapterManuscriptService,
                )

                ChapterManuscriptService(self.session).require_publishable(chapter_id)
            updates["state"] = state
        if "words_target" in body:
            value = body["words_target"]
            updates["words_target"] = int(value) if value not in (None, "") else None
        narrative = dict(chapter.narrative_json or {})
        # 阶段 Z：构思的分章钉着的章——幕由分章决定（三个灾难各自收束一幕），目录里单独改只会各说各话
        plan_owned = structure_owned_by_plan(
            chapter, live_chapter_plans_by_catalog_id(self.session, project_id)
        )
        if plan_owned and "act" in body and normalize_act(body["act"]) != normalize_act(narrative.get("act")):
            raise DomainError(
                "CATALOG_CHAPTER_STRUCTURE_OWNED_BY_PLAN",
                "这一章是构思里分出来的：它在第几幕由「整理章节结构」决定，确认写入后目录跟着走。",
                status_code=409,
                details={
                    "chapter_id": chapter.chapter_id,
                    "fields": ["act"],
                    "author_action": chapter_structure_owned_by_plan_action(),
                },
            )
        previous_title = chapter_title(chapter)
        for key in NARRATIVE_FIELDS:
            if key in body:
                narrative[key] = body[key]
        if any(key in body for key in NARRATIVE_FIELDS):
            updates["narrative_json"] = narrative
        set_current = bool(body.get("current"))
        changed_fields = [
            key for key, value in updates.items() if getattr(chapter, key) != value
        ]
        if set_current and project.current_chapter_id != chapter.chapter_id:
            changed_fields.append("project.current_chapter_id")
        changed = require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=changed_fields,
            operation="catalog.update_chapter",
        )
        if changed:
            for key, value in updates.items():
                setattr(chapter, key, value)
            if set_current:
                project.current_chapter_id = chapter.chapter_id
            self.session.flush()
        # 阶段 Z「章名只有一个」：这一章是构思分出来的，作者在这里改了名 → 章计划、09 的章头、07 的章节表
        # 在同一事务里接过同一个名字（否则分章面板还挂着旧名，「AI 起章名」会给起过名的章再起一遍）。
        plan_title = None
        if changed and plan_owned and "title" in body and chapter_title(chapter) != previous_title:
            plan_title = adopt_catalog_title(
                self.session, project_id, chapter.chapter_id, chapter_title(chapter), actor_ref=actor_ref
            )
        chapters = self.chapter_rows(project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter_id)
        return {
            "chapter": self.chapter_payload(project, chapter, index),
            "changed": changed,
            # 章名写穿到了构思的章表：前端据此让本机的雪花缓存（07 章节表 / 09 章头）接过服务端这一版
            "plan_title_synced": bool(plan_title and plan_title.get("changed")),
        }

    def create_chapter(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        body = payload or {}
        compact_chapter_orders(self.session, project_id)
        existing = self.chapter_rows(project_id)
        title = str(body.get("title") or "").strip() or f"第 {len(existing) + 1} 章"
        # 空目录首章立为在写章（抄 WsCatalog.addChapter 语义）；调用方也可显式传 state/current
        is_first = not existing
        state = str(body.get("state") or ("writing" if is_first else "planned"))
        if state not in CHAPTER_STATES:
            raise DomainError("CATALOG_STATE_INVALID", f"chapter state must be one of {CHAPTER_STATES}", status_code=400)
        if state == "approved":
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        chapter = ChapterGoal(
            chapter_id=f"{project_id}_CH_{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            planned_scene_count=0,
            chapter_goal=title,
            state=state,
            words_target=int(body["words_target"]) if body.get("words_target") else None,
            display_order=len(existing) + 1,
            narrative_json={"title": title, **{k: body[k] for k in NARRATIVE_FIELDS if k in body and k != "title"}},
            writer_brief_json={"source": "catalog_api", "title": title},
        )
        self.session.add(chapter)
        self.session.flush()
        # 审计 P-1：冷启动章与雪花物化/章 API 同约定补建运行时状态行，
        # 否则场景 run 通过全部 QC 后在归档/聚合段对缺行章 None 解引用 500。
        ensure_chapter_state(self.session, chapter.chapter_id)
        if is_first or body.get("current", True):
            project.current_chapter_id = chapter.chapter_id
        # 默认带一个开场场景（抄 addChapter：scenes=[开场]）；传 with_scene=False 可跳过
        if body.get("with_scene", True):
            self._insert_scene(
                chapter,
                position=0,
                title=str(body.get("scene_title") or "开场"),
                kind="proactive",
                state="writing" if is_first else "todo",
                brief={"goal": title},
            )
        self.session.flush()
        chapters = self.chapter_rows(project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter.chapter_id)
        return {"chapter": self.chapter_payload(project, chapter, index)}

    def update_scene(self, project_id: str, scene_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, project_id=project_id)
        chapter = require_project_chapter(self.session, project_id, scene.chapter_id)
        body = payload or {}
        brief = dict(scene.writer_brief_json or {})
        updates: dict[str, Any] = {}
        if "title" in body:
            brief["title"] = str(body["title"] or "").strip()
        if "kind" in body:
            kind = parse_scene_kind(body["kind"])
            updates["scene_type"] = kind
            brief["primary_form"] = kind
        if "state" in body:
            state = str(body["state"] or "").strip()
            if state not in SCENE_STATES:
                raise DomainError("CATALOG_STATE_INVALID", f"scene state must be one of {SCENE_STATES}", status_code=400)
            updates["state"] = state
        if "exit_change" in body:
            updates["exit_change"] = str(body.get("exit_change") or "")
        if "hook" in body:
            updates["hook"] = str(body.get("hook") or "")
        for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
            if key in body:
                brief[key] = str(body[key] or "")
        nested = body.get("brief")
        if isinstance(nested, dict):
            for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
                if key in nested:
                    brief[key] = str(nested[key] or "")
        # 阶段 Y：构思里那一行还在的雪花场景卡，设计只能在构思里改（见 design_owned_by_plan）。
        # 只拦**真的改了**的字段——前端整卡回写、值没变的请求照常通过。
        before = dict(scene.writer_brief_json or {})
        design_changes = [
            key for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD) if str(before.get(key) or "") != str(brief.get(key) or "")
        ]
        if "scene_type" in updates and updates["scene_type"] != scene_kind(scene):
            design_changes.append("kind")
        design_changes.extend(
            key for key in ("exit_change", "hook")
            if key in updates and str(updates[key] or "") != str(getattr(scene, key) or "")
        )
        # POV 角色：按 id 选既有角色，或按名找（找不到的名字等确认这一场可写之后再建）；空串显式清空。
        pov_given, pov_id, character_name = self._resolve_pov(project_id, body)
        needs_character_create = bool(character_name)
        if pov_given and not needs_character_create:
            updates["pov_character_id"] = pov_id
        if needs_character_create or (
            "pov_character_id" in updates and (updates["pov_character_id"] or None) != (scene.pov_character_id or None)
        ):
            design_changes.append("pov")
        if design_changes and design_owned_by_plan(self.session, scene):
            raise DomainError(
                "CATALOG_SCENE_DESIGN_OWNED_BY_PLAN",
                "这一场是雪花整理出来的，它的设计（形态、三拍、POV、离场变化、钩子）在构思第 10 步改，确认后自动同步到目录。",
                status_code=409,
                details={
                    "scene_id": scene.scene_id,
                    "fields": design_changes,
                    "author_action": design_owned_by_plan_action(scene.scene_id),
                },
            )
        if brief != dict(scene.writer_brief_json or {}):
            updates["writer_brief_json"] = brief
        changed_fields = [
            key for key, value in updates.items() if getattr(scene, key) != value
        ]
        if needs_character_create:
            changed_fields.extend(
                ["story_character.create", "pov_character_id"]
            )
        changed = require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=changed_fields,
            operation="catalog.update_scene",
        )
        if changed:
            if needs_character_create:
                updates["pov_character_id"] = self._find_or_create_character(
                    project_id,
                    character_name,
                ).character_id
            for key, value in updates.items():
                setattr(scene, key, value)
            self.session.flush()
        return {
            "scene": self._scene_payload_with_slug(scene),
            "changed": changed,
        }

    def _resolve_pov(self, project_id: str, body: dict[str, Any]) -> tuple[bool, str | None, str]:
        """请求里的 POV → ``(给了没有, 角色 id, 还得新建的角色名)``。

        按 id 选这部作品里既有的角色（不是这部作品的 → 400）；只给名字就按名找，找不到把名字交回调用方
        （让冷启动作品不必走完整雪花物化就能设 POV，解执行契约的 pov_character_id 硬阻断）；两样都空 = 显式清空。
        """
        if "pov_character_id" not in body and "pov_character_name" not in body:
            return False, None, ""
        pov_id = str(body.get("pov_character_id") or "").strip()
        pov_name = str(body.get("pov_character_name") or "").strip()
        if pov_id:
            character = self.session.get(StoryCharacter, pov_id)
            if character is None or character.project_id != project_id:
                raise DomainError("CATALOG_POV_CHARACTER_NOT_FOUND", "pov character not found in project", status_code=400)
            return True, pov_id, ""
        if pov_name:
            existing = self.session.execute(
                select(StoryCharacter).where(
                    StoryCharacter.project_id == project_id,
                    StoryCharacter.display_name == pov_name,
                )
            ).scalars().first()
            return (True, existing.character_id, "") if existing is not None else (True, None, pov_name)
        return True, None, ""

    def _find_or_create_character(self, project_id: str, display_name: str) -> StoryCharacter:
        existing = self.session.execute(
            select(StoryCharacter).where(
                StoryCharacter.project_id == project_id,
                StoryCharacter.display_name == display_name,
            )
        ).scalars().first()
        if existing is not None:
            return existing
        character = StoryCharacter(
            character_id=f"CHAR_{uuid.uuid4().hex[:10].upper()}",
            project_id=project_id,
            display_name=display_name,
        )
        self.session.add(character)
        self.session.flush()
        return character

    def create_scene(self, project_id: str, chapter_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        chapter = require_project_chapter(self.session, project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["scenes.create"],
            operation="catalog.create_scene",
        )
        body = payload or {}
        scenes = self.scene_rows(chapter_id)
        at = body.get("at")
        position = int(at) if at is not None else len(scenes)
        position = max(0, min(position, len(scenes)))
        kind = parse_scene_kind(body.get("kind"))
        state = str(body.get("state") or "todo")
        if state not in SCENE_STATES:
            raise DomainError("CATALOG_STATE_INVALID", f"scene state must be one of {SCENE_STATES}", status_code=400)
        brief_keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        brief = {key: str(body.get(key) or "") for key in brief_keys if body.get(key)}
        nested = body.get("brief")
        if isinstance(nested, dict):
            for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
                if key in nested:
                    brief[key] = str(nested[key] or "")
        scene = self._insert_scene(
            chapter,
            position=position,
            title=str(body.get("title") or "").strip() or "新场景",
            kind=kind,
            state=state,
            brief=brief,
        )
        if "exit_change" in body:
            scene.exit_change = str(body.get("exit_change") or "")
        if "hook" in body:
            scene.hook = str(body.get("hook") or "")
        pov_given, pov_id, character_name = self._resolve_pov(project_id, body)
        if pov_given:
            scene.pov_character_id = (
                self._find_or_create_character(project_id, character_name).character_id if character_name else pov_id
            )
        self.session.flush()
        return {"scene": self._scene_payload_with_slug(scene), "changed": True}

    def reorder_chapters(
        self,
        project_id: str,
        chapter_ids: list[str],
    ) -> dict[str, Any]:
        """Persist the complete active chapter order without moving finals.

        The endpoint accepts a full-set replacement so stale clients cannot
        accidentally drop a newly-created chapter.  Approved chapters retain
        both their relative sequence and their absolute catalog position.
        """

        project = require_project(self.session, project_id)
        requested = list(chapter_ids)
        if len(requested) != len(set(requested)):
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_DUPLICATE",
                "chapter_ids must not contain duplicates",
                status_code=400,
            )

        compact_chapter_orders(self.session, project.project_id)
        current_rows = self.chapter_rows(project_id)
        current_ids = [chapter.chapter_id for chapter in current_rows]
        requested_set = set(requested)
        current_set = set(current_ids)
        foreign_ids = [
            row.chapter_id
            for row in self.session.execute(
                select(ChapterGoal).where(ChapterGoal.chapter_id.in_(requested or [""]))
            ).scalars().all()
            if row.project_id != project.project_id or row.trashed_flag == 1
        ]
        if foreign_ids:
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_PROJECT_MISMATCH",
                "chapter_ids must contain only active chapters from this project",
                status_code=409,
                details={"chapter_ids": sorted(foreign_ids)},
            )
        if requested_set != current_set or len(requested) != len(current_ids):
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_INCOMPLETE",
                "chapter_ids must contain every active chapter in the project exactly once",
                status_code=409,
                details={
                    "missing_chapter_ids": sorted(current_set - requested_set),
                    "unknown_chapter_ids": sorted(requested_set - current_set),
                },
            )

        if requested == current_ids:
            return {
                "project_id": project.project_id,
                "chapter_ids": current_ids,
                "chapters": [
                    {
                        "chapter_id": chapter.chapter_id,
                        "display_order": chapter.display_order,
                    }
                    for chapter in current_rows
                ],
                "changed": False,
            }

        by_id = {chapter.chapter_id: chapter for chapter in current_rows}
        # 阶段 Z：构思分出来的章彼此的先后 = 章表的顺序（章是故事序上连续的一段）。在目录里把它们互相挪开，
        # 目录的章序就不再是故事的顺序，下一次「确认写入」又会悄悄排回去。手建的章照常可以挪到任何两章之间。
        pinned = live_chapter_plans_by_catalog_id(self.session, project_id)
        plan_owned_ids = {
            chapter.chapter_id for chapter in current_rows if structure_owned_by_plan(chapter, pinned)
        }
        if [cid for cid in requested if cid in plan_owned_ids] != [cid for cid in current_ids if cid in plan_owned_ids]:
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_OWNED_BY_PLAN",
                "构思里分出来的章，彼此的先后由「整理章节结构」的章表决定（章是场景列表上连续的一段），确认写入后目录跟着走。",
                status_code=409,
                details={
                    "fields": ["order"],
                    "plan_owned_chapter_ids": [cid for cid in current_ids if cid in plan_owned_ids],
                    "author_action": chapter_structure_owned_by_plan_action(),
                },
            )
        approved_ids = {
            chapter.chapter_id
            for chapter in current_rows
            if is_chapter_approved(self.session, chapter)
        }
        old_approved_order = [chapter_id for chapter_id in current_ids if chapter_id in approved_ids]
        new_approved_order = [chapter_id for chapter_id in requested if chapter_id in approved_ids]
        old_positions = {
            chapter_id: index for index, chapter_id in enumerate(current_ids) if chapter_id in approved_ids
        }
        new_positions = {
            chapter_id: index for index, chapter_id in enumerate(requested) if chapter_id in approved_ids
        }
        moved_approved_ids = [
            chapter_id
            for chapter_id in old_approved_order
            if new_positions.get(chapter_id) != old_positions[chapter_id]
        ]
        if new_approved_order != old_approved_order or moved_approved_ids:
            raise DomainError(
                "CATALOG_APPROVED_CHAPTER_ORDER_LOCKED",
                "approved chapters cannot change relative order or catalog position",
                status_code=409,
                details={
                    "approved_chapter_ids": old_approved_order,
                    "moved_chapter_ids": moved_approved_ids,
                    "reopen_required": True,
                },
            )

        # Do not rewrite even an equivalent display_order value on approved
        # rows.  If historical data is already inconsistent, fail closed.
        inconsistent_approved_ids = [
            chapter_id
            for chapter_id, position in old_positions.items()
            if by_id[chapter_id].display_order != position + 1
        ]
        if inconsistent_approved_ids:
            raise DomainError(
                "CATALOG_APPROVED_CHAPTER_ORDER_INCONSISTENT",
                "approved chapter order is inconsistent and must be reopened before repair",
                status_code=409,
                details={
                    "chapter_ids": inconsistent_approved_ids,
                    "reopen_required": True,
                },
            )

        self._assign_chapter_orders(
            [
                (by_id[chapter_id], position)
                for position, chapter_id in enumerate(requested, start=1)
                if chapter_id not in approved_ids
            ]
        )
        return {
            "project_id": project.project_id,
            "chapter_ids": requested,
            "chapters": [
                {
                    "chapter_id": chapter_id,
                    "display_order": by_id[chapter_id].display_order,
                }
                for chapter_id in requested
            ],
            "changed": True,
        }

    def reorder_scenes(self, chapter_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """v1 场景重排（``POST /api/v1/chapters/{chapter_id}/scene-order``，章节编排拖场用）：整章活跃场景的新顺序 + 章末那一场。"""
        session = self.session
        chapter = AuthorLifecycleService(session).require_active_chapter(chapter_id)

        scene_ids = payload.get("scene_ids")
        if not isinstance(scene_ids, list) or not scene_ids or not all(isinstance(scene_id, str) and scene_id for scene_id in scene_ids):
            raise DomainError("SCENE_ORDER_INVALID", "scene_ids must be a non-empty list", status_code=400)
        if len(scene_ids) != len(set(scene_ids)):
            raise DomainError("SCENE_ORDER_DUPLICATE", "scene_ids must not contain duplicates", status_code=400)

        last_scene_id = payload.get("last_scene_id")
        if not isinstance(last_scene_id, str) or last_scene_id not in scene_ids:
            raise DomainError("SCENE_ORDER_LAST_SCENE_INVALID", "last_scene_id must be present in scene_ids", status_code=400)

        chapter_scenes = session.execute(
            select(SceneCard).where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
        ).scalars().all()
        chapter_scene_map = {scene.scene_id: scene for scene in chapter_scenes}
        other_chapter_scenes = {
            scene.scene_id
            for scene in session.execute(select(SceneCard).where(SceneCard.scene_id.in_(scene_ids), SceneCard.trashed_flag == 0)).scalars().all()
            if scene.chapter_id != chapter_id
        }
        if other_chapter_scenes:
            raise DomainError("SCENE_ORDER_CHAPTER_MISMATCH", "scene_ids must belong to the same chapter")

        if set(scene_ids) != set(chapter_scene_map):
            raise DomainError("SCENE_ORDER_INCOMPLETE", "scene_ids must include every scene in the chapter", status_code=409)

        ordered_scenes = [chapter_scene_map[scene_id] for scene_id in scene_ids]
        # 阶段 Y「设计只有一处可改」：雪花整理出来的场，彼此的先后 = 故事序（构思第 9 步的行序）。台子上挪了，
        # 下一次同步就会按故事序摆回去——所以这里不收；手加的场照常可以挪到任何两场之间。
        owned = plan_owned_scene_ids(session, chapter.project_id, list(chapter_scenes))
        if owned:
            current_owned = [
                scene.scene_id
                for scene in sorted(chapter_scenes, key=lambda item: (int(item.scene_seq or 0), item.scene_id))
                if scene.scene_id in owned
            ]
            if [scene_id for scene_id in scene_ids if scene_id in owned] != current_owned:
                raise DomainError(
                    "CATALOG_SCENE_ORDER_OWNED_BY_PLAN",
                    "这几场是雪花整理出来的，它们的先后在构思第 9 步「场景列表」里拖动，确认后自动同步到目录；手加的场可以在这里挪。",
                    status_code=409,
                    details={"chapter_id": chapter_id, "author_action": scene_order_owned_by_plan_action()},
                )
        changed_fields = [
            f"scene:{scene.scene_id}.order"
            for index, scene in enumerate(ordered_scenes, start=1)
            if scene.scene_seq != index
            or scene.is_chapter_last != (1 if scene.scene_id == last_scene_id else 0)
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="chapters.reorder_scenes",
        )
        if changed:
            reseat_scene_seqs(session, ordered_scenes, last_scene_id=last_scene_id)
        return {
            "chapter_id": chapter_id,
            "changed": changed,
            "scenes": [
                {"scene_id": scene.scene_id, "scene_seq": scene.scene_seq, "is_chapter_last": scene.is_chapter_last}
                for scene in ordered_scenes
            ],
        }

    def import_catalog(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """一次性迁移入口的旧形状：现在只给测试夹具播种（见 ``catalog_import``）。"""
        return import_catalog(self.session, project_id, payload)

    # ---------- internals ----------

    def _insert_scene(
        self,
        chapter: ChapterGoal,
        *,
        position: int,
        title: str,
        kind: str,
        state: str,
        brief: dict[str, Any],
    ) -> SceneCard:
        scenes = self.scene_rows(chapter.chapter_id)
        seq_base = max((int(s.scene_seq or 0) for s in self._all_chapter_scenes(chapter.chapter_id)), default=0)
        scene = SceneCard(
            scene_id=f"{chapter.chapter_id}_SC_{uuid.uuid4().hex[:8]}",
            chapter_id=chapter.chapter_id,
            project_id=chapter.project_id,
            scene_seq=seq_base + 1,
            scene_goal=title,
            scene_type=kind,
            state=state,
            words_current=0,
            writer_brief_json={"source": "catalog_api", "title": title, "primary_form": kind, **brief},
        )
        self.session.add(scene)
        # 与 v1 scenes POST 同约定补建运行时状态行：章节运行（运行本章）按 scene_id
        # 取 SceneRunState，缺行会让整章一起步就 SCENE_NOT_FOUND。
        self.session.add(SceneRunState(scene_id=scene.scene_id, scene_status="ready"))
        self.session.flush()
        ordered = list(scenes)
        ordered.insert(max(0, min(position, len(ordered))), scene)
        self._renumber(ordered)
        chapter.planned_scene_count = len(ordered)
        return scene

    def _all_chapter_scenes(self, chapter_id: str) -> list[SceneCard]:
        return list(
            self.session.execute(
                select(SceneCard).where(SceneCard.chapter_id == chapter_id)
            ).scalars().all()
        )

    def _renumber(self, ordered: list[SceneCard]) -> None:
        reseat_scene_seqs(self.session, ordered)

    def _assign_chapter_orders(
        self,
        assignments: list[tuple[ChapterGoal, int]],
    ) -> None:
        project_ids = {chapter.project_id for chapter, _ in assignments}
        active_rows = (
            list(
                self.session.execute(
                    select(ChapterGoal).where(
                        ChapterGoal.project_id.in_(project_ids),
                        ChapterGoal.trashed_flag == 0,
                    )
                ).scalars().all()
            )
            if any(chapter.display_order != int(order) for chapter, order in assignments)
            else []
        )
        reseat_display_orders(self.session, assignments, scope=active_rows)

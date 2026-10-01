"""FE-ALIGN Phase 4: 回收站 —— 作品级软删 + 三级（作品/章/场景）统一列表。

章/场景级沿用既有 AuthorLifecycleService（trashed_flag 软删，同一机制）；
本服务补：作品级软删/恢复（级联只动可见性——子数据不动，列表查询过滤）、
统一回收站列表、永久清除（D3：仅手动，不做自动清理）。

条目 id 约定："work:{project_id}" / "chapter:{chapter_id}" / "scene:{scene_id}"。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    StoryProject,
    utcnow,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.catalog_labels import chapter_title, scene_title
from novel_system.services.errors import DomainError
from novel_system.services.project_purge import build_project_purge_plan, purge_project_rows
from novel_system.services.scene_lookup import require_project, scene_project_id
from novel_system.services.story_slots import planned_chapter_goal


class TrashService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._lifecycle = AuthorLifecycleService(session)

    # ---- 作品级 ----

    def trash_project(self, project_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        project = self._require_project(project_id, allow_trashed=True)
        if project.trashed_flag != 1:
            project.trashed_flag = 1
            project.trashed_at = utcnow()
            project.trashed_by = actor_ref
            self.session.flush()
        return {"project_id": project_id, "trashed": True}

    def restore_project(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id, allow_trashed=True)
        if project.trashed_flag == 1:
            project.trashed_flag = 0
            project.trashed_at = None
            project.trashed_by = None
            self.session.flush()
        return {"project_id": project_id, "trashed": False}

    # ---- 统一列表 ----

    def list_trash(self, project_id: str | None = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        # 全局桶：被删除的整部作品（不随作品命名空间）
        works = self.session.execute(
            select(StoryProject).where(StoryProject.trashed_flag == 1)
        ).scalars().all()
        for project in works:
            items.append(
                {
                    "id": f"work:{project.project_id}",
                    "kind": "work",
                    "title": project.title,
                    "removed_at": project.trashed_at,
                    "restorable": True,
                }
            )
        if project_id:
            chapters = self.session.execute(
                select(ChapterGoal).where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 1
                )
            ).scalars().all()
            for chapter in chapters:
                items.append(
                    {
                        "id": f"chapter:{chapter.chapter_id}",
                        "kind": "chapter",
                        "title": chapter_title(chapter),
                        "removed_at": chapter.trashed_at,
                        "restorable": True,
                    }
                )
            scenes = self.session.execute(
                select(SceneCard).where(
                    SceneCard.project_id == project_id, SceneCard.trashed_flag == 1
                )
            ).scalars().all()
            trashed_chapter_ids = {c.chapter_id for c in chapters}
            # 场题名同目录：没起题名拿场目标，旧物化给没写摘要的场补的本章样板目标不算（按所在章的章名认，S2 1）
            scene_chapters = (
                {
                    row.chapter_id: row
                    for row in self.session.execute(
                        select(ChapterGoal).where(ChapterGoal.chapter_id.in_({scene.chapter_id for scene in scenes}))
                    ).scalars()
                }
                if scenes
                else {}
            )
            for scene in scenes:
                goal = planned_chapter_goal(scene.scene_goal, scene_chapters.get(scene.chapter_id))
                items.append(
                    {
                        "id": f"scene:{scene.scene_id}",
                        "kind": "scene",
                        "title": scene_title(scene, goal=goal),
                        "removed_at": scene.trashed_at,
                        # 所在章也被删时，恢复场景会被既有服务阻止（先恢复章）
                        "restorable": scene.chapter_id not in trashed_chapter_ids,
                        "chapter_id": scene.chapter_id,
                    }
                )
        items.sort(key=lambda item: item.get("removed_at") or "", reverse=True)
        return {"items": items}

    # ---- 场景级软删（校验归属后走既有 lifecycle 服务；分章面板「丢弃孤儿场景」用它） ----

    def trash_scene_in_project(self, project_id: str, scene_id: str, *, actor_ref: str) -> dict[str, Any]:
        scene = self.session.get(SceneCard, scene_id)
        if scene is None or scene_project_id(self.session, scene) != project_id:
            raise DomainError("SCENE_NOT_FOUND", "scene not found in project", status_code=404)
        result = self._lifecycle.trash_scenes([scene_id], actor_ref)
        return self._lifecycle_result(f"scene:{scene_id}", result)

    # ---- 恢复 / 永久清除（按条目 id 分发） ----

    def restore_entry(self, entry_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        kind, target = self._parse_entry(entry_id)
        if kind == "work":
            return self.restore_project(target)
        if kind == "chapter":
            result = self._lifecycle.restore_chapters([target])
            return self._lifecycle_result(entry_id, result)
        result = self._lifecycle.restore_scenes([target])
        return self._lifecycle_result(entry_id, result)

    def purge_entry(self, entry_id: str) -> dict[str, Any]:
        kind, target = self._parse_entry(entry_id)
        if kind == "work":
            return self.purge_project(target)
        if kind == "chapter":
            result = self._lifecycle.purge_chapters([target])
            return self._lifecycle_result(entry_id, result)
        result = self._lifecycle.purge_scenes([target])
        return self._lifecycle_result(entry_id, result)

    def purge_project(self, project_id: str) -> dict[str, Any]:
        """整部作品永久清除（D3 手动）。删除项目本体与全部派生数据。

        审计 P-2：此前只删 FE 域 + 雪花域 15 张表，正文全文仍以草稿 / 成稿 / 场景记忆 / 修订快照 /
        LLM 审计载荷等形式留库。现在按元数据推出作品名下的每一行（``project_purge``：作品 / 章 / 场景 /
        作者稿 / 人物各维度 + 几种间接引用），新表不必再来这里登记。刻意保留 OperationLog（纯操作审计，
        无正文，与 style_reference 保留 MetricEvent 同一取舍）。
        """
        project = self._require_project(project_id, allow_trashed=True)
        if project.trashed_flag != 1:
            raise DomainError(
                "PROJECT_NOT_TRASHED", "project must be trashed before purge", status_code=409
            )
        # 所有权先冻结再删：删到一半时章 / 场景行已经没了，就再也推不出它们名下的行
        purge_project_rows(self.session, build_project_purge_plan(self.session, project_id))
        self.session.delete(project)
        self.session.flush()
        return {"project_id": project_id, "purged": True}

    # ---- internals ----

    def _require_project(self, project_id: str, *, allow_trashed: bool = False) -> StoryProject:
        return require_project(self.session, project_id, reject_trashed=not allow_trashed)

    @staticmethod
    def _parse_entry(entry_id: str) -> tuple[str, str]:
        kind, _, target = str(entry_id or "").partition(":")
        if kind not in {"work", "chapter", "scene"} or not target:
            raise DomainError("TRASH_ENTRY_INVALID", "invalid trash entry id", status_code=400)
        return kind, target

    @staticmethod
    def _lifecycle_result(entry_id: str, result: dict[str, Any]) -> dict[str, Any]:
        blocked = list(result.get("blocked") or [])
        if blocked:
            raise DomainError(
                "TRASH_OPERATION_BLOCKED",
                str(blocked[0].get("message") or "operation blocked"),
                status_code=409,
                details={"entry_id": entry_id, "blocked": blocked},
            )
        return {"entry_id": entry_id, "processed": result.get("processed") or []}

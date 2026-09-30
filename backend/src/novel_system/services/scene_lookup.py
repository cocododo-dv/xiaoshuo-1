"""场景/章节存在性校验的共享实现。

「这一场 / 这一章得在（不在回收站）」只有 ``require_scene`` / ``require_chapter`` 这一处实现（B08-18）；
各调用方的错误码、状态与文案不可变，用参数保留：

- 默认（多数调用方）：不存在或已入回收站（trashed_flag == 1）一律 404 SCENE_NOT_FOUND / CHAPTER_NOT_FOUND。
- ``trashed_as_conflict``：入回收站区分为 409 SCENE_TRASHED / CHAPTER_TRASHED（场景备注、深评偏好、作者生命周期）。
- ``project_id``：限定作品（目录）——场景不属于它 → 404 "scene not found in project"；章不存在、入回收站或
  不属于它一律 404 "chapter not found in project"（``require_project_chapter`` 就是这个组合）。
- ``with_parents``：作者生命周期的口径（``AuthorLifecycleService.require_active_*``）——场景所在的章入了回收站
  也算场景入了回收站；所属作品不在或入了回收站 → 404 PROJECT_TRASHED（没有作品归属的旧行照旧放行）。

只查存在、不看回收站的变体（``get_scene_or_404`` / ``get_chapter_or_404`` / ``require_project``）也在这里。
场景归哪部作品（场景卡的 project_id，旧卡看所在的章）只算一处：``scene_project_id``（作者稿、目录、回收站共用）。
条件不同的仍留在原处：trash 的项目内章 / 场（不看回收站）、canon_continuity.chapter_status（"chapter not found"
且带 project_id）、chapter_final_flow._require_project_chapter（PROJECT_CHAPTER_NOT_FOUND，不看回收站）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.errors import DomainError


def require_scene(
    session: Session,
    scene_id: str,
    *,
    project_id: str | None = None,
    trashed_as_conflict: bool = False,
    with_parents: bool = False,
) -> SceneCard:
    """不在回收站的场景卡（参数见模块说明）。"""
    scene = session.get(SceneCard, scene_id)
    if scene is None:
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    chapter = session.get(ChapterGoal, scene.chapter_id) if with_parents else None
    if scene.trashed_flag == 1 or (chapter is not None and chapter.trashed_flag == 1):
        if trashed_as_conflict:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash", status_code=409)
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    if project_id is not None and scene_project_id(session, scene) != project_id:
        raise DomainError("SCENE_NOT_FOUND", "scene not found in project", status_code=404)
    if with_parents:
        _require_available_project(session, chapter.project_id if chapter is not None else scene.project_id)
    return scene


def require_chapter(
    session: Session,
    chapter_id: str,
    *,
    project_id: str | None = None,
    trashed_as_conflict: bool = False,
    with_parents: bool = False,
) -> ChapterGoal:
    """不在回收站的章（参数见模块说明）。"""
    chapter = session.get(ChapterGoal, chapter_id)
    not_found = "chapter not found" if project_id is None else "chapter not found in project"
    if chapter is None:
        raise DomainError("CHAPTER_NOT_FOUND", not_found, status_code=404)
    if chapter.trashed_flag == 1:
        if trashed_as_conflict:
            raise DomainError("CHAPTER_TRASHED", "chapter is currently in author trash", status_code=409)
        raise DomainError("CHAPTER_NOT_FOUND", not_found, status_code=404)
    if project_id is not None and chapter.project_id != project_id:
        raise DomainError("CHAPTER_NOT_FOUND", not_found, status_code=404)
    if with_parents:
        _require_available_project(session, chapter.project_id)
    return chapter


def _require_available_project(session: Session, project_id: str | None) -> None:
    # 没有作品归属的旧章 / 旧场景照旧可读
    if not project_id:
        return
    project = session.get(StoryProject, project_id)
    if project is None or project.trashed_flag == 1:
        raise DomainError(
            "PROJECT_TRASHED",
            "chapter or scene belongs to an unavailable project",
            status_code=404,
        )


def get_scene_or_404(session: Session, scene_id: str) -> SceneCard:
    """存在即返回（回收站里的也算）；不存在 404 SCENE_NOT_FOUND。"""
    scene = session.get(SceneCard, scene_id)
    if scene is None:
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    return scene


def get_chapter_or_404(session: Session, chapter_id: str) -> ChapterGoal:
    """存在即返回（回收站里的也算）；不存在 404 CHAPTER_NOT_FOUND。"""
    chapter = session.get(ChapterGoal, chapter_id)
    if chapter is None:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
    return chapter


def require_project_chapter(session: Session, project_id: str, chapter_id: str) -> ChapterGoal:
    """属于该项目且不在回收站的章；否则 404 CHAPTER_NOT_FOUND "chapter not found in project"。"""
    return require_chapter(session, chapter_id, project_id=project_id)


def require_project(session: Session, project_id: str, *, reject_trashed: bool = False) -> StoryProject:
    """项目存在（``reject_trashed`` 时还须不在回收站）；否则 404 PROJECT_NOT_FOUND。"""
    project = session.get(StoryProject, project_id)
    if project is None or (reject_trashed and project.trashed_flag == 1):
        raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
    return project


def scene_project_id(session: Session, scene: SceneCard) -> str | None:
    """场景归哪部作品：场景卡自己的 project_id；v1 建的旧卡没有，就看它所在的章（都没有给 None）。

    只读、不报错的那一种——写路径要「两边对不上就拒」时用 ``scene_ownership.require_scene_project_id``。
    """
    if scene.project_id:
        return scene.project_id
    chapter = session.get(ChapterGoal, scene.chapter_id)
    return chapter.project_id if chapter is not None else None


def active_chapter_scenes(session: Session, chapter_id: str) -> list[SceneCard]:
    """章里不在回收站的场景卡，按 (scene_seq, scene_id)。"""
    return list(
        session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
    )

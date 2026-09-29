"""场景/章节存在性校验的共享实现。

约束：
- 多数调用方契约：记录不存在或已入回收站（trashed_flag == 1）一律 404，
  错误码/文案（SCENE_NOT_FOUND / CHAPTER_NOT_FOUND）不可变。
- scene_notes / scene_deep_review_preferences 契约：入回收站需区分为
  409 SCENE_TRASHED（传 trashed_as_conflict=True），文案同样不可变。
- 只查存在、不看回收站的变体（``get_scene_or_404`` / ``get_chapter_or_404`` / ``require_project``）
  与带 project_id 的 ``require_project_chapter``（文案 "chapter not found in project"）也在这里。
- 文案或条件不同的仍留在原处：catalog._require_scene（归属按章回推）、trash 的项目内章 / 场
  （不看回收站）、canon_continuity.chapter_status（"chapter not found" 且带 project_id）、
  projects._require_project_chapter（PROJECT_CHAPTER_NOT_FOUND）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.errors import DomainError


def require_scene(session: Session, scene_id: str, *, trashed_as_conflict: bool = False) -> SceneCard:
    scene = session.get(SceneCard, scene_id)
    if scene is None:
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    if scene.trashed_flag == 1:
        if trashed_as_conflict:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash", status_code=409)
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    return scene


def require_chapter(session: Session, chapter_id: str) -> ChapterGoal:
    chapter = session.get(ChapterGoal, chapter_id)
    if chapter is None or chapter.trashed_flag == 1:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
    return chapter


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
    chapter = session.get(ChapterGoal, chapter_id)
    if chapter is None or chapter.trashed_flag == 1 or chapter.project_id != project_id:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)
    return chapter


def require_project(session: Session, project_id: str, *, reject_trashed: bool = False) -> StoryProject:
    """项目存在（``reject_trashed`` 时还须不在回收站）；否则 404 PROJECT_NOT_FOUND。"""
    project = session.get(StoryProject, project_id)
    if project is None or (reject_trashed and project.trashed_flag == 1):
        raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
    return project


def active_chapter_scenes(session: Session, chapter_id: str) -> list[SceneCard]:
    """章里不在回收站的场景卡，按 (scene_seq, scene_id)。"""
    return list(
        session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
    )

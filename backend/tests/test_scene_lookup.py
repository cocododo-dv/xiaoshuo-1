"""「这一场 / 这一章得在」只有一处实现（``scene_lookup.require_scene`` / ``require_chapter``，B08-18）。

过去有四份各写各的：scene_lookup 的 require_*、作者生命周期的 require_active_*（另查所在的章与所属作品）、
目录的 _require_scene（按作品限定）和 require_project_chapter。现在它们都是同一个实现的参数组合；各调用方
看到的错误码、状态与文案一个不变——这里逐一钉住（整理之前与之后跑同一张表）。
"""
from __future__ import annotations

import pytest

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.catalog import CatalogService
from novel_system.services.errors import DomainError
from novel_system.services.scene_lookup import require_chapter, require_project_chapter, require_scene

NOT_FOUND = ("SCENE_NOT_FOUND", 404, "scene not found")
NOT_IN_PROJECT = ("SCENE_NOT_FOUND", 404, "scene not found in project")
SCENE_TRASHED = ("SCENE_TRASHED", 409, "scene is currently in author trash")
CHAPTER_NOT_FOUND = ("CHAPTER_NOT_FOUND", 404, "chapter not found")
CHAPTER_NOT_IN_PROJECT = ("CHAPTER_NOT_FOUND", 404, "chapter not found in project")
CHAPTER_TRASHED = ("CHAPTER_TRASHED", 409, "chapter is currently in author trash")
PROJECT_UNAVAILABLE = ("PROJECT_TRASHED", 404, "chapter or scene belongs to an unavailable project")
OK = "ok"


@pytest.fixture
def world(session):
    """雨城（在用）与旧卷（进了回收站）两部作品；章与场覆盖每一种「在 / 不在」。"""
    session.add_all(
        [
            StoryProject(project_id="LOOK_P", title="雨城", outline_text="", planning_mode="snowflake"),
            StoryProject(project_id="LOOK_T", title="旧卷", outline_text="", planning_mode="snowflake", trashed_flag=1),
        ]
    )
    session.flush()
    session.add_all(
        [
            ChapterGoal(chapter_id="LOOK_C", project_id="LOOK_P", chapter_goal="旧信", display_order=1),
            ChapterGoal(chapter_id="LOOK_CT", project_id="LOOK_P", chapter_goal="案卷", display_order=2, trashed_flag=1),
            ChapterGoal(chapter_id="LOOK_CX", project_id="LOOK_T", chapter_goal="旧卷一章", display_order=1),
            ChapterGoal(chapter_id="LOOK_CL", project_id=None, chapter_goal="没有作品归属的旧章"),
        ]
    )
    session.flush()
    session.add_all(
        [
            SceneCard(scene_id="LOOK_S", chapter_id="LOOK_C", project_id="LOOK_P", scene_seq=1, scene_goal="拆信"),
            SceneCard(scene_id="LOOK_ST", chapter_id="LOOK_C", project_id="LOOK_P", scene_seq=2, scene_goal="撕页", trashed_flag=1),
            SceneCard(scene_id="LOOK_SCT", chapter_id="LOOK_CT", project_id="LOOK_P", scene_seq=1, scene_goal="随章进回收站"),
            SceneCard(scene_id="LOOK_SX", chapter_id="LOOK_CX", project_id="LOOK_T", scene_seq=1, scene_goal="旧卷的场"),
            SceneCard(scene_id="LOOK_SL", chapter_id="LOOK_CL", project_id=None, scene_seq=1, scene_goal="旧章的场"),
        ]
    )
    session.flush()
    return session


def _outcome(call) -> object:
    try:
        call()
    except DomainError as exc:
        return (exc.code, exc.status_code, exc.message)
    return OK


@pytest.mark.parametrize(
    ("scene_id", "plain", "conflict", "lifecycle"),
    [
        ("LOOK_S", OK, OK, OK),
        ("LOOK_MISSING", NOT_FOUND, NOT_FOUND, NOT_FOUND),
        ("LOOK_ST", NOT_FOUND, SCENE_TRASHED, SCENE_TRASHED),
        # 场景本身不在回收站、所在的章在：只有作者生命周期的口径把它算作进了回收站
        ("LOOK_SCT", OK, OK, SCENE_TRASHED),
        ("LOOK_SX", OK, OK, PROJECT_UNAVAILABLE),
        ("LOOK_SL", OK, OK, OK),
    ],
)
def test_scene_presets_keep_their_codes(world, scene_id, plain, conflict, lifecycle) -> None:
    assert _outcome(lambda: require_scene(world, scene_id)) == plain
    assert _outcome(lambda: require_scene(world, scene_id, trashed_as_conflict=True)) == conflict
    assert _outcome(lambda: AuthorLifecycleService(world).require_active_scene(scene_id)) == lifecycle


@pytest.mark.parametrize(
    ("scene_id", "expected"),
    [
        ("LOOK_MISSING", NOT_FOUND),
        ("LOOK_ST", NOT_FOUND),
        ("LOOK_SX", NOT_IN_PROJECT),
        ("LOOK_SL", NOT_IN_PROJECT),
    ],
)
def test_catalog_scene_lookup_is_scoped_to_the_project(world, scene_id, expected) -> None:
    assert _outcome(lambda: CatalogService(world).update_scene("LOOK_P", scene_id, {})) == expected


@pytest.mark.parametrize(
    ("chapter_id", "plain", "in_project", "lifecycle"),
    [
        ("LOOK_C", OK, OK, OK),
        ("LOOK_MISSING", CHAPTER_NOT_FOUND, CHAPTER_NOT_IN_PROJECT, CHAPTER_NOT_FOUND),
        ("LOOK_CT", CHAPTER_NOT_FOUND, CHAPTER_NOT_IN_PROJECT, CHAPTER_TRASHED),
        ("LOOK_CX", OK, CHAPTER_NOT_IN_PROJECT, PROJECT_UNAVAILABLE),
        ("LOOK_CL", OK, CHAPTER_NOT_IN_PROJECT, OK),
    ],
)
def test_chapter_presets_keep_their_codes(world, chapter_id, plain, in_project, lifecycle) -> None:
    assert _outcome(lambda: require_chapter(world, chapter_id)) == plain
    assert _outcome(lambda: require_project_chapter(world, "LOOK_P", chapter_id)) == in_project
    assert _outcome(lambda: AuthorLifecycleService(world).require_active_chapter(chapter_id)) == lifecycle

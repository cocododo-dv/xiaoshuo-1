"""B06-18：作品切换器 / 主页的作品列表（``GET /api/v2/projects``）里每部作品的「已动笔章数」。

以前每部作品每一章一条查询（章数越多列表越慢）；现在两条查询算完所有作品。口径不变：
已动笔 = 章里没进回收站的场景卡有正文字数，或章的状态不是 planned / todo；进了回收站的章不算。
"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.support.sql import count_statements as _count_statements


def _project(session, project_id: str) -> None:
    session.add(
        StoryProject(
            project_id=project_id, title=project_id, outline_text="林昭带着旧信回到雨城。", planning_mode="snowflake",
            snowflake_workflow_mode="explore", target_word_count=100000,
        )
    )
    session.flush()


def _chapter(session, project_id: str, seq: int, *, state: str = "planned", words: int = 0, trashed: bool = False) -> None:
    chapter_id = f"{project_id}_CH{seq:02d}"
    session.add(
        ChapterGoal(
            chapter_id=chapter_id, project_id=project_id, chapter_goal="案卷", state=state, trashed_flag=1 if trashed else 0,
        )
    )
    session.flush()
    session.add(
        SceneCard(
            scene_id=f"{chapter_id}_SC01", project_id=project_id, chapter_id=chapter_id, scene_seq=1, scene_goal="找案卷",
            words_current=words, trashed_flag=1 if trashed else 0,
        )
    )
    session.flush()


def _written(service: SnowflakeWorkspaceService, project_id: str) -> int:
    return next(item for item in service.list_projects()["items"] if item["project_id"] == project_id)["chapters_written"]


def test_chapters_written_counts_words_or_a_started_state_and_skips_the_trash(session) -> None:
    _project(session, "prj-list-a")
    _chapter(session, "prj-list-a", 1, words=120)
    _chapter(session, "prj-list-a", 2, state="drafting")
    _chapter(session, "prj-list-a", 3)
    _chapter(session, "prj-list-a", 4, state="todo")
    _chapter(session, "prj-list-a", 5, words=500, trashed=True)
    _project(session, "prj-list-b")
    _chapter(session, "prj-list-b", 1, words=30)

    service = SnowflakeWorkspaceService(session)
    assert _written(service, "prj-list-a") == 2
    assert _written(service, "prj-list-b") == 1


def test_the_list_does_not_query_once_per_chapter(session) -> None:
    _project(session, "prj-list-few")
    for seq in range(1, 3):
        _chapter(session, "prj-list-few", seq, words=10)
    service = SnowflakeWorkspaceService(session)
    session.expire_all()
    with _count_statements() as few:
        service.list_projects()

    for seq in range(3, 11):
        _chapter(session, "prj-list-few", seq, words=10)
    session.expire_all()
    with _count_statements() as many:
        listed = service.list_projects()
    assert listed["items"][0]["chapters_written"] == 10
    assert many["n"] == few["n"], f"2 章 {few['n']} 条语句、10 章 {many['n']} 条：每一章又是一条查询"

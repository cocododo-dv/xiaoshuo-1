"""B06-04 / 05 / 06：工作台与自动保存的 SQL 语句数有上限，而且不随场景数增长（性能守卫）。

每一次变更（包括 700 ms 防抖的自动保存 PATCH）都会建一遍工作台。修之前量得（每场只有计划、还没物化）：
20 场 workspace() 42 条、PATCH 74 条；40 场 62 / 114；60 场 82 / 154——每一场一条 ``session.get(SceneCard)``，
自动保存再加每行一次按 row_uid 对位；故事序为排序把 09 草稿重读四五遍，各步最新版本把每一版历史草稿都读出来。
修好之后 20 / 40 / 60 场都是 workspace() 15 条、PATCH 28 条、``include_workspace=false`` 的自动保存 17 条。
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import event

from novel_system.db.models import SnowflakeStepRun, StoryProject
from novel_system.db.session import engine
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

#: 上限（量得的值留了几条余量，给分章现状等别处的小改动）；关键是下面「40 场与 20 场一样多」
WORKSPACE_BUDGET = 20
PATCH_BUDGET = 36
LEAN_PATCH_BUDGET = 24


@contextmanager
def _count_statements():
    counter = {"n": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["n"] += 1

    target = engine()
    event.listen(target, "before_cursor_execute", _count)
    try:
        yield counter
    finally:
        event.remove(target, "before_cursor_execute", _count)


def _seed(session, project_id: str, scene_count: int, versions: int = 3) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=project_id,
            title="语句数",
            outline_text="语句数大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    text = "测" * 40
    scenes = [
        {
            "row_uid": f"row-{index:03d}",
            "summary": f"场景{index} {text}",
            "pov_character_id": "c1",
            "location": "码头",
            "crucible": text,
            "chapter_role": "推进",
        }
        for index in range(scene_count)
    ]
    for version in range(versions):
        listed = service.update_step(
            project_id, "scene_list", {"draft": {"scenes": [dict(scene, summary=f"{scene['summary']}{version}") for scene in scenes]}}
        )
        rows = next(step for step in listed["workspace"]["steps"] if step["step_key"] == "scene_list")["draft"]["scenes"]
        service.update_step(
            project_id,
            "scene_details",
            {"draft": {"scenes": [dict(row, goal=text, conflict=text, setback=text) for row in rows]}},
        )
        # 每一轮都留一版确认过的历史（各步最新版本以前会把每一版历史草稿都读出来）
        for run in session.query(SnowflakeStepRun).filter_by(project_id=project_id, status="pending_review"):
            run.status = "approved"
        session.flush()
    return service


def _measure(session, project_id: str, scene_count: int) -> tuple[int, int, int]:
    service = _seed(session, project_id, scene_count)
    session.expire_all()
    with _count_statements() as workspace_counter:
        workspace = service.workspace(project_id)
    rows = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")["draft"]["scenes"]
    session.expire_all()
    with _count_statements() as patch_counter:
        service.update_step(project_id, "scene_details", {"draft": {"scenes": rows}})
    edited = [dict(row, goal=f"{row['goal']}改") for row in rows]
    session.expire_all()
    with _count_statements() as lean_counter:
        saved = service.update_step(project_id, "scene_details", {"draft": {"scenes": edited}}, include_workspace=False)
    assert set(saved) == {"step", "step_run"}
    return workspace_counter["n"], patch_counter["n"], lean_counter["n"]


def test_workspace_and_autosave_statement_counts_stay_flat_as_the_book_grows(session) -> None:
    small = _measure(session, "prj-budget-20", 20)
    large = _measure(session, "prj-budget-40", 40)
    assert large == small, f"20 场 {small}，40 场 {large}：语句数跟着场景数涨了（逐场查询回来了？）"
    workspace, patch, lean_patch = small
    assert workspace <= WORKSPACE_BUDGET, workspace
    assert patch <= PATCH_BUDGET, patch
    assert lean_patch <= LEAN_PATCH_BUDGET, lean_patch

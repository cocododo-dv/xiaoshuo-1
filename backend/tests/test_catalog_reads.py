"""目录与主页的读取：查询条数不随章数增长（B08-12 / B08-13），读取不写库、章序由写入口压实（B08-14）。"""
from __future__ import annotations

from sqlalchemy import event, select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.catalog import CatalogService
from novel_system.services.project_overview import ProjectOverviewService


def _statements(session, action) -> list[str]:
    engine = session.get_bind()
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        # 连接级 PRAGMA（外键开关）只看连接新旧，与读取无关
        if not statement.lstrip().upper().startswith("PRAGMA"):
            statements.append(statement)

    session.expire_all()
    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


def _seed_book(session, project_id: str, chapter_count: int, *, scenes_per_chapter: int = 3) -> None:
    session.add(
        StoryProject(
            project_id=project_id,
            title="雨城旧案",
            outline_text="林昭翻开旧案卷。",
            approved_chapter_ids_json=[],
            current_chapter_id=f"{project_id}_CH01",
        )
    )
    session.flush()
    for chapter_no in range(1, chapter_count + 1):
        chapter_id = f"{project_id}_CH{chapter_no:02d}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                chapter_goal=f"第{chapter_no}章",
                display_order=chapter_no,
                words_target=3000,
                narrative_json={"title": f"旧信 {chapter_no}"},
                writer_brief_json={"source": "catalog_api"},
            )
        )
        session.flush()
        for seq in range(1, scenes_per_chapter + 1):
            scene_id = f"{chapter_id}_SC{seq:02d}"
            session.add(
                SceneCard(
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    project_id=project_id,
                    scene_seq=seq,
                    scene_goal=f"林昭在雨里读第{seq}封信",
                    is_chapter_last=1 if seq == scenes_per_chapter else 0,
                    words_current=100 * seq,
                    writer_brief_json={"source": "catalog_api", "title": f"第{seq}封信"},
                )
            )
            session.flush()
            session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
            session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    step="near_final_acceptance_review",
                    status="completed",
                    details_json={"scene_story_check": {"verdict": "yes", "note": "旧"}},
                )
            )
            session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    step="near_final_acceptance_review",
                    status="completed",
                    details_json={"scene_story_check": {"verdict": "maybe", "note": "新"}},
                )
            )
    session.commit()


def test_catalog_read_query_count_does_not_grow_with_the_book(session) -> None:
    _seed_book(session, "PRJ_READ_SMALL", 2)
    _seed_book(session, "PRJ_READ_LARGE", 8)
    small = _statements(session, lambda: CatalogService(session).catalog("PRJ_READ_SMALL"))
    large = _statements(session, lambda: CatalogService(session).catalog("PRJ_READ_LARGE"))
    assert len(large) == len(small), (len(small), len(large))

    payload = CatalogService(session).catalog("PRJ_READ_LARGE")
    assert [chapter["no"] for chapter in payload["chapters"]] == [f"{n:02d}" for n in range(1, 9)]
    first = payload["chapters"][0]
    assert first["words"] == {"cur": 600, "target": 3000}
    # 每场取最近一次准定稿评审的三问
    assert {scene["story_check"]["note"] for scene in first["scenes"]} == {"新"}
    assert [scene["seq"] for scene in first["scenes"]] == [1, 2, 3]


def test_the_latest_review_without_a_story_check_hides_an_older_one(session) -> None:
    _seed_book(session, "PRJ_READ_CHECK", 1, scenes_per_chapter=1)
    session.add(
        AttemptTracker(
            scene_id="PRJ_READ_CHECK_CH01_SC01",
            chapter_id="PRJ_READ_CHECK_CH01",
            step="near_final_acceptance_review",
            status="completed",
            details_json={"verdict": "pass"},
        )
    )
    session.commit()
    scene = CatalogService(session).catalog("PRJ_READ_CHECK")["chapters"][0]["scenes"][0]
    assert scene["story_check"] is None


def test_dashboard_query_count_does_not_grow_with_the_book(session) -> None:
    _seed_book(session, "PRJ_HOME_SMALL", 2)
    _seed_book(session, "PRJ_HOME_LARGE", 8)
    small = _statements(session, lambda: ProjectOverviewService(session).dashboard("PRJ_HOME_SMALL"))
    large = _statements(session, lambda: ProjectOverviewService(session).dashboard("PRJ_HOME_LARGE"))
    assert len(large) == len(small), (len(small), len(large))

    dashboard = ProjectOverviewService(session).dashboard("PRJ_HOME_LARGE")
    assert [row["no"] for row in dashboard["chapters_recent"]] == ["04", "05", "06", "07", "08"]
    assert dashboard["chapters_recent"][0] == {
        "chapter_id": "PRJ_HOME_LARGE_CH04",
        "no": "04",
        "title": "旧信 4",
        "state": "planned",
        "pct": 20,
        "active": False,
    }
    assert dashboard["resume"]["chapter_no"] == "01"
    assert dashboard["resume"]["scene_slug"] == "PRJ_HOME_LARGE_CH01_SC01"


def test_catalog_read_never_writes_even_over_drifted_chapter_orders(client, session) -> None:
    _seed_book(session, "PRJ_READ_DRIFT", 3, scenes_per_chapter=1)
    orders = {"PRJ_READ_DRIFT_CH01": 4, "PRJ_READ_DRIFT_CH02": None, "PRJ_READ_DRIFT_CH03": 2}
    for chapter_id, order in orders.items():
        session.get(ChapterGoal, chapter_id).display_order = order
    session.commit()

    statements = _statements(session, lambda: client.get("/api/v2/projects/PRJ_READ_DRIFT/catalog"))
    writes = [statement for statement in statements if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
    assert writes == []
    payload = client.get("/api/v2/projects/PRJ_READ_DRIFT/catalog").json()["data"]
    assert [chapter["chapter_id"] for chapter in payload["chapters"]] == [
        "PRJ_READ_DRIFT_CH03",
        "PRJ_READ_DRIFT_CH01",
        "PRJ_READ_DRIFT_CH02",
    ]
    session.expire_all()
    assert {chapter_id: session.get(ChapterGoal, chapter_id).display_order for chapter_id in orders} == orders


def test_trashing_and_restoring_a_chapter_keeps_the_chapter_orders_dense(client, session) -> None:
    project_id = client.post(
        "/api/v2/projects",
        json={"title": "雨城旧案", "outline_text": "旧信。"},
        headers={"X-Idempotency-Key": "reads-dense-project"},
    ).json()["data"]["project"]["project_id"]
    chapter_ids = []
    for index in range(3):
        response = client.post(
            f"/api/v2/projects/{project_id}/catalog/chapters",
            json={"title": f"第{index + 1}章", "with_scene": False},
            headers={"X-Idempotency-Key": f"reads-dense-chapter-{index}"},
        )
        assert response.status_code == 200, response.text
        chapter_ids.append(response.json()["data"]["chapter"]["chapter_id"])

    def orders() -> list[tuple[str, int]]:
        session.expire_all()
        return [
            (row.chapter_id, row.display_order)
            for row in session.execute(
                select(ChapterGoal)
                .where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
                .order_by(ChapterGoal.display_order.asc())
            ).scalars()
        ]

    trashed = client.post(
        "/api/v1/chapters/trash",
        json={"chapter_ids": [chapter_ids[1]]},
        headers={"X-Idempotency-Key": "reads-dense-trash"},
    )
    assert trashed.status_code == 200, trashed.text
    assert orders() == [(chapter_ids[0], 1), (chapter_ids[2], 2)]

    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters",
        json={"title": "第4章", "with_scene": False},
        headers={"X-Idempotency-Key": "reads-dense-chapter-after-trash"},
    )
    assert created.status_code == 200, created.text
    fourth = created.json()["data"]["chapter"]["chapter_id"]
    assert orders() == [(chapter_ids[0], 1), (chapter_ids[2], 2), (fourth, 3)]

    restored = client.post(
        f"/api/v2/trash/chapter:{chapter_ids[1]}/restore",
        json={},
        headers={"X-Idempotency-Key": "reads-dense-restore"},
    )
    assert restored.status_code == 200, restored.text
    assert orders() == [(chapter_ids[0], 1), (chapter_ids[1], 2), (chapter_ids[2], 3), (fourth, 4)]

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.canon_continuity import CanonContinuityService


_sequence = 0


def _post(client, path: str, payload: dict | None = None) -> dict:
    global _sequence
    _sequence += 1
    response = client.post(
        path,
        json=payload or {},
        headers={"X-Idempotency-Key": f"approved-guard-{_sequence}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_project(client, *, title: str = "Approved guard") -> str:
    payload = _post(
        client,
        "/api/v2/projects",
        {"title": title, "outline_text": "A guarded outline."},
    )
    return payload["project"]["project_id"]


def _create_chapter(
    client,
    project_id: str,
    title: str,
    *,
    current: bool = False,
) -> dict:
    return _post(
        client,
        f"/api/v2/projects/{project_id}/catalog/chapters",
        {"title": title, "current": current},
    )["chapter"]


def _approve_chapter(session, project_id: str, chapter_id: str) -> None:
    project = session.get(StoryProject, project_id)
    chapter = session.get(ChapterGoal, chapter_id)
    assert project is not None and chapter is not None
    approved = list(project.approved_chapter_ids_json or [])
    if chapter_id not in approved:
        approved.append(chapter_id)
    project.approved_chapter_ids_json = approved
    chapter.state = "approved"
    session.commit()


def _archive_scene(session, scene: SceneCard, *, content: str) -> None:
    row_id = f"guard_final_{scene.scene_id}"
    session.add(
        FinalScene(
            row_id=row_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content=content,
            source_bundle_id=f"guard_bundle_{scene.scene_id}",
            source_bundle_hash=f"guard_hash_{scene.scene_id}",
        )
    )
    state = session.get(SceneRunState, scene.scene_id)
    if state is None:
        state = SceneRunState(scene_id=scene.scene_id, scene_status="archived")
        session.add(state)
    state.current_final_scene_row_id = row_id
    state.scene_status = "archived"
    session.flush()


def _chapter_order_request(
    client,
    path: str,
    payload: dict,
    *,
    key: str,
):
    return client.post(
        path,
        json=payload,
        headers={"X-Idempotency-Key": key},
    )


def test_approved_chapter_catalog_and_lifecycle_writes_are_locked_but_noops_work(
    client,
    session,
) -> None:
    project_id = _create_project(client)
    chapter = _create_chapter(client, project_id, "Locked chapter", current=True)
    chapter_id = chapter["chapter_id"]
    first_scene = chapter["scenes"][0]
    second_scene = _post(
        client,
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        {"title": "Second scene"},
    )["scene"]
    draft = _post(
        client,
        f"/api/v1/author-drafts/scene/{first_scene['scene_id']}/ensure",
    )["draft"]
    _approve_chapter(session, project_id, chapter_id)

    chapter_noop = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"title": "Locked chapter", "state": "approved"},
    )
    assert chapter_noop.status_code == 200, chapter_noop.text
    assert chapter_noop.json()["data"]["changed"] is False

    chapter_change = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"title": "Bypass"},
    )
    assert chapter_change.status_code == 409
    assert chapter_change.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    scene_noop = client.patch(
        f"/api/v2/projects/{project_id}/catalog/scenes/{first_scene['scene_id']}",
        json={"title": first_scene["title"]},
    )
    assert scene_noop.status_code == 200, scene_noop.text
    assert scene_noop.json()["data"]["changed"] is False

    scene_change = client.patch(
        f"/api/v2/projects/{project_id}/catalog/scenes/{first_scene['scene_id']}",
        json={"title": "Bypass"},
    )
    assert scene_change.status_code == 409
    assert scene_change.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    create_scene = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        json={"title": "Bypass"},
        headers={"X-Idempotency-Key": "approved-create-scene"},
    )
    assert create_scene.status_code == 409
    assert create_scene.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    scene_order_noop = _post(
        client,
        f"/api/v1/chapters/{chapter_id}/scene-order",
        {
            "scene_ids": [first_scene["scene_id"], second_scene["scene_id"]],
            "last_scene_id": second_scene["scene_id"],
        },
    )
    assert scene_order_noop["changed"] is False
    scene_order_change = client.post(
        f"/api/v1/chapters/{chapter_id}/scene-order",
        json={
            "scene_ids": [second_scene["scene_id"], first_scene["scene_id"]],
            "last_scene_id": first_scene["scene_id"],
        },
        headers={"X-Idempotency-Key": "approved-reorder-scenes"},
    )
    assert scene_order_change.status_code == 409
    assert scene_order_change.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    v1_scene_update = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": first_scene["scene_id"],
            "chapter_id": chapter_id,
            "scene_goal": "Changed through legacy upsert",
        },
        headers={"X-Idempotency-Key": "approved-v1-scene-update"},
    )
    assert v1_scene_update.status_code == 409
    assert v1_scene_update.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    v1_chapter_update = client.post(
        "/api/v1/chapters",
        json={"chapter_id": chapter_id, "chapter_goal": "Changed legacy chapter"},
        headers={"X-Idempotency-Key": "approved-v1-chapter-update"},
    )
    assert v1_chapter_update.status_code == 409
    assert v1_chapter_update.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    same_draft = client.patch(
        f"/api/v1/author-drafts/{draft['draft_id']}",
        json={"content": draft["content"], "base_revision_no": draft["revision_no"]},
    )
    assert same_draft.status_code == 200, same_draft.text
    assert same_draft.json()["data"]["changed"] is False
    assert same_draft.json()["data"]["draft"]["revision_no"] == draft["revision_no"]

    changed_draft = client.patch(
        f"/api/v1/author-drafts/{draft['draft_id']}",
        json={"content": "Cannot edit an approved final.", "base_revision_no": draft["revision_no"]},
    )
    assert changed_draft.status_code == 409
    assert changed_draft.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    batch_trash = client.post(
        "/api/v1/scenes/trash",
        json={"scene_ids": [first_scene["scene_id"]]},
        headers={"X-Idempotency-Key": "approved-v1-trash-scene"},
    )
    assert batch_trash.status_code == 200
    assert batch_trash.json()["data"]["processed"] == []
    assert batch_trash.json()["data"]["blocked"][0]["code"] == "CHAPTER_APPROVED_LOCKED"

    chapter_trash = client.post(
        "/api/v1/chapters/trash",
        json={"chapter_ids": [chapter_id]},
        headers={"X-Idempotency-Key": "approved-v1-trash-chapter"},
    )
    assert chapter_trash.status_code == 200
    assert chapter_trash.json()["data"]["processed"] == []
    assert chapter_trash.json()["data"]["blocked"][0]["code"] == "CHAPTER_APPROVED_LOCKED"


def test_chapter_order_requires_strict_complete_project_set_and_keeps_approved_position(
    client,
    session,
) -> None:
    project_id = _create_project(client, title="Order guard")
    chapters = [
        _create_chapter(client, project_id, f"Chapter {index}", current=index == 1)
        for index in range(1, 5)
    ]
    chapter_ids = [chapter["chapter_id"] for chapter in chapters]
    _approve_chapter(session, project_id, chapter_ids[1])
    path = f"/api/v2/projects/{project_id}/catalog/chapter-order"

    noop = _chapter_order_request(
        client,
        path,
        {"chapter_ids": chapter_ids},
        key="chapter-order-noop",
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["data"]["changed"] is False
    replay = _chapter_order_request(
        client,
        path,
        {"chapter_ids": chapter_ids},
        key="chapter-order-noop",
    )
    assert replay.status_code == 200
    assert replay.headers.get("X-Idempotency-Status") == "replayed"

    allowed_order = [chapter_ids[0], chapter_ids[1], chapter_ids[3], chapter_ids[2]]
    allowed = _chapter_order_request(
        client,
        path,
        {"chapter_ids": allowed_order},
        key="chapter-order-allowed",
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data"]["changed"] is True
    session.expire_all()
    assert [session.get(ChapterGoal, chapter_id).display_order for chapter_id in allowed_order] == [1, 2, 3, 4]

    moved_final = _chapter_order_request(
        client,
        path,
        {"chapter_ids": [chapter_ids[1], chapter_ids[0], chapter_ids[3], chapter_ids[2]]},
        key="chapter-order-move-final",
    )
    assert moved_final.status_code == 409
    assert moved_final.json()["error"]["code"] == "CATALOG_APPROVED_CHAPTER_ORDER_LOCKED"

    duplicate = _chapter_order_request(
        client,
        path,
        {"chapter_ids": [chapter_ids[0], chapter_ids[1], chapter_ids[1], chapter_ids[2]]},
        key="chapter-order-duplicate",
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "CATALOG_CHAPTER_ORDER_DUPLICATE"

    incomplete = _chapter_order_request(
        client,
        path,
        {"chapter_ids": allowed_order[:-1]},
        key="chapter-order-incomplete",
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["code"] == "CATALOG_CHAPTER_ORDER_INCOMPLETE"

    other_project_id = _create_project(client, title="Foreign order")
    foreign_chapter = _create_chapter(client, other_project_id, "Foreign", current=True)
    cross_project = _chapter_order_request(
        client,
        path,
        {"chapter_ids": [*allowed_order[:-1], foreign_chapter["chapter_id"]]},
        key="chapter-order-cross-project",
    )
    assert cross_project.status_code == 409
    assert cross_project.json()["error"]["code"] == "CATALOG_CHAPTER_ORDER_PROJECT_MISMATCH"

    extra = _chapter_order_request(
        client,
        path,
        {"chapter_ids": allowed_order, "unexpected": True},
        key="chapter-order-extra",
    )
    assert extra.status_code == 422

    empty = _chapter_order_request(
        client,
        path,
        {"chapter_ids": []},
        key="chapter-order-empty",
    )
    assert empty.status_code == 422


def test_review_read_confirmation_and_approval_require_complete_canonical_manuscript(
    client,
    session,
) -> None:
    project_id = _create_project(client, title="Completion guard")
    chapter = _create_chapter(client, project_id, "Complete me", current=True)
    chapter_id = chapter["chapter_id"]
    _post(
        client,
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        {"title": "Second scene"},
    )
    noncurrent_chapter = _create_chapter(
        client,
        project_id,
        "Structural review only",
        current=False,
    )
    scenes = list(
        session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc())
        ).scalars().all()
    )
    assert len(scenes) == 2
    project = session.get(StoryProject, project_id)
    assert project is not None
    project.status = "chapter_final_review"
    session.commit()

    # ``review`` remains a structural/editorial catalog label for chapters
    # other than the one actually submitted through the project final-review
    # flow.  A project-wide status must not make every unfinished chapter look
    # like the current chapter's terminal submission.
    noncurrent_review = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{noncurrent_chapter['chapter_id']}",
        json={"state": "review"},
    )
    assert noncurrent_review.status_code == 200, noncurrent_review.text

    submit_incomplete = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"state": "review"},
    )
    assert submit_incomplete.status_code == 409
    assert submit_incomplete.json()["error"]["code"] == "CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE"
    assert submit_incomplete.json()["error"]["details"]["missing_scene_ids"] == [
        scene.scene_id for scene in scenes
    ]

    read_incomplete = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm",
        json={"note": "I should not be able to confirm this."},
    )
    assert read_incomplete.status_code == 409
    assert read_incomplete.json()["error"]["code"] == "CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE"

    approve_incomplete = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final",
        json={},
        headers={"X-Idempotency-Key": "approve-incomplete-canonical"},
    )
    assert approve_incomplete.status_code == 409
    assert approve_incomplete.json()["error"]["code"] == "CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE"

    _archive_scene(session, scenes[0], content="First canonical scene.")
    session.commit()
    partial = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"state": "review"},
    )
    assert partial.status_code == 409
    assert partial.json()["error"]["details"]["completion_status"] == "partial"
    assert partial.json()["error"]["details"]["missing_scene_ids"] == [scenes[1].scene_id]

    _archive_scene(session, scenes[1], content="Second canonical scene.")
    session.commit()
    canon_pending = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"state": "review"},
    )
    assert canon_pending.status_code == 409
    assert canon_pending.json()["error"]["code"] == "CHAPTER_CANON_NOT_COMMITTED"

    canon = CanonContinuityService(session)
    for scene in scenes:
        canon.verify_scene_complete(
            project_id,
            scene.scene_id,
            actor_ref="test-author",
            note="测试已通读当前终稿，确认本场正史事实完整。",
        )
    session.commit()
    submitted = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"state": "review"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["changed"] is True

    read = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm",
        json={"note": "Read the complete canonical chapter."},
    )
    assert read.status_code == 200, read.text

    approved = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final",
        json={"revision_notes": "Complete."},
        headers={"X-Idempotency-Key": "approve-complete-canonical"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["approved_chapter_id"] == chapter_id

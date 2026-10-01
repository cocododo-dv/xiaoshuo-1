from __future__ import annotations

from tests.support.seed import seed_chapter, seed_project, seed_scene


def _create_scene(suffix: str = "NOTES") -> str:
    project_id = f"PRJ_NOTES_{suffix}"
    seed_project(project_id, title="Notes project", outline_text="outline")
    chapter_id = f"CH_{suffix}"
    seed_chapter(chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="goal")
    scene_id = f"{chapter_id}_SC01"
    seed_scene(scene_id, chapter_id=chapter_id, project_id=project_id, scene_seq=1, scene_goal="goal")
    return scene_id


def test_scene_author_notes_are_durable_and_revision_fenced(client) -> None:
    scene_id = _create_scene()

    initial = client.get(f"/api/v1/scenes/{scene_id}/author-notes")
    assert initial.status_code == 200
    assert initial.json()["data"]["notes"] == ""
    assert initial.json()["data"]["revision_no"] == 0

    saved = client.patch(
        f"/api/v1/scenes/{scene_id}/author-notes",
        json={"notes": "提醒：盐钟在本场回收。", "base_revision_no": 0},
        headers={"X-Idempotency-Key": "save-scene-notes-v1"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["notes"] == "提醒：盐钟在本场回收。"
    assert saved.json()["data"]["revision_no"] == 1

    conflict = client.patch(
        f"/api/v1/scenes/{scene_id}/author-notes",
        json={"notes": "过时设备覆盖", "base_revision_no": 0},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SCENE_AUTHOR_NOTES_CONFLICT"

    current = client.get(f"/api/v1/scenes/{scene_id}/author-notes")
    assert current.json()["data"]["notes"] == "提醒：盐钟在本场回收。"
    assert current.json()["data"]["revision_no"] == 1

from __future__ import annotations

from tests.support.seed import seed_chapter, seed_project, seed_scene


def _create_scene() -> str:
    project_id = "PRJ_DEEP_PREFS"
    seed_project(project_id, title="Deep-review preferences", outline_text="outline")
    chapter_id = "CH_DEEP_PREFS"
    seed_chapter(chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="goal")
    scene_id = f"{chapter_id}_SC01"
    seed_scene(scene_id, chapter_id=chapter_id, project_id=project_id, scene_seq=1, scene_goal="goal")
    return scene_id


def test_scene_deep_review_preferences_are_durable_deduplicated_and_revision_fenced(client) -> None:
    scene_id = _create_scene()
    path = f"/api/v1/scenes/{scene_id}/deep-review/preferences"

    initial = client.get(path)
    assert initial.status_code == 200
    assert initial.json()["data"]["decision_log"] == []
    assert initial.json()["data"]["ignored_issue_keys"] == []
    assert initial.json()["data"]["revision_no"] == 0

    saved = client.patch(
        path,
        json={
            "decision_log": [{"at": 1_700_000_000_000, "text": "忽略 · 第三段重复"}],
            "ignored_issue_keys": ["echo:2:潮声", "echo:2:潮声"],
            "base_revision_no": 0,
        },
        headers={"X-Idempotency-Key": "deep-prefs-save-v1"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["ignored_issue_keys"] == ["echo:2:潮声"]
    assert saved.json()["data"]["revision_no"] == 1

    conflict = client.patch(
        path,
        json={"decision_log": [], "ignored_issue_keys": [], "base_revision_no": 0},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT"

    current = client.get(path)
    assert current.json()["data"]["decision_log"][0]["text"] == "忽略 · 第三段重复"
    assert current.json()["data"]["revision_no"] == 1


def test_scene_deep_review_preferences_reject_oversized_client_state(client) -> None:
    scene_id = _create_scene()
    response = client.patch(
        f"/api/v1/scenes/{scene_id}/deep-review/preferences",
        json={
            "decision_log": [{"at": index, "text": "x"} for index in range(31)],
            "ignored_issue_keys": [],
            "base_revision_no": 0,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

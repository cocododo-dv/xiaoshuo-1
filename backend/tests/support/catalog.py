"""章 / 场景骨架：几个测试文件共用的建章、建场景助手。"""

from __future__ import annotations


# ---------------------------------------------------------------- 经 v1 章 / 场景接口建章与场景（test_chapter_manuscripts）


def create_v1_chapter(client, chapter_id: str, *, goal: str = "Draft a chapter") -> None:
    response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": chapter_id,
            "planned_scene_count": 3,
            "chapter_goal": goal,
            "main_plot_push": f"push {chapter_id}",
            "emotional_target": f"emotion {chapter_id}",
            "ending_effect": f"ending {chapter_id}",
            "must_not": f"avoid {chapter_id}",
            "notes": f"notes {chapter_id}",
        },
        headers={"X-Idempotency-Key": f"create-{chapter_id}"},
    )
    assert response.status_code == 200


def create_v1_scene(
    client,
    scene_id: str,
    *,
    chapter_id: str,
    scene_seq: int,
    is_chapter_last: int = 0,
) -> None:
    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "scene_seq": scene_seq,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A"],
            "location": f"Location {scene_id}",
            "scene_goal": f"goal for {scene_id}",
            "beats_json": [f"beat {scene_id}"],
            "must_include_text": "",
            "forbidden_text": "",
            "exit_change": "",
            "hook": "",
            "target_length_band": "medium",
            "scene_type": "reunion",
            "is_chapter_last": is_chapter_last,
        },
        headers={"X-Idempotency-Key": f"create-{scene_id}"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------- 经 v1 接口建一章一场、供后台场景作业跑（test_scene_run_jobs）


def create_job_chapter_and_scene(client) -> None:
    chapter_response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "CHJOB",
            "planned_scene_count": 1,
            "chapter_goal": "Run scene through background job",
            "main_plot_push": "Exercise job API",
            "emotional_target": "Keep operator unblocked",
            "ending_effect": "Pollable status",
        },
        headers={"X-Idempotency-Key": "chapter-job-create"},
    )
    assert chapter_response.status_code == 200
    scene_response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "CHJOB_SC01",
            "chapter_id": "CHJOB",
            "scene_seq": 1,
            "pov_character_id": "",
            "onstage_chars_json": [],
            "location": "Control room",
            "scene_goal": "Start a pollable run",
            "beats_json": ["start", "poll"],
            "target_length_band": "short",
            "scene_type": "test",
            "is_chapter_last": 1,
        },
        headers={"X-Idempotency-Key": "scene-job-create"},
    )
    assert scene_response.status_code == 200

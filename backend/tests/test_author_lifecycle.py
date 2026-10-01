from __future__ import annotations


from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from tests.support.seed import seed_chapter, seed_scene


def _create_chapter(chapter_id: str, *, goal: str = "Author a chapter") -> None:
    seed_chapter(
        chapter_id,
        planned_scene_count=3,
        chapter_goal=goal,
        main_plot_push=f"push {chapter_id}",
        emotional_target=f"emotion {chapter_id}",
        ending_effect=f"ending {chapter_id}",
        must_not=f"avoid {chapter_id}",
        notes=f"notes {chapter_id}",
    )


def _create_scene(
    scene_id: str,
    *,
    chapter_id: str,
    scene_seq: int,
    is_chapter_last: int = 0,
    location: str = "Archive room",
) -> None:
    seed_scene(
        scene_id,
        chapter_id=chapter_id,
        scene_seq=scene_seq,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A", "CHAR_B"],
        location=location,
        scene_goal=f"goal for {scene_id}",
        beats_json=[f"beat-{scene_id}-1", f"beat-{scene_id}-2"],
        must_include_text=f"must include {scene_id}",
        forbidden_text=f"forbidden {scene_id}",
        exit_change=f"exit {scene_id}",
        hook=f"hook {scene_id}",
        target_length_band="medium",
        scene_type="reunion",
        is_chapter_last=is_chapter_last,
    )


def test_scene_purge_removes_only_the_trashed_scene_and_its_run_state(session) -> None:
    project_id = "project_scene_purge"
    chapter_id = "chapter_scene_purge"
    scene_id = "scene_scene_purge"
    session.add(StoryProject(
        project_id=project_id,
        title="Scene purge",
        outline_text="",
        planning_mode="snowflake",
    ))
    session.flush()
    session.add(ChapterGoal(
        chapter_id=chapter_id,
        project_id=project_id,
        chapter_goal="Purge one scene",
    ))
    session.flush()
    session.add_all([
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="Purge",
            trashed_flag=1,
        ),
        SceneCard(
            scene_id="scene_kept",
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=2,
            scene_goal="Keep",
        ),
    ])
    session.flush()
    session.add_all([SceneRunState(scene_id=scene_id), SceneRunState(scene_id="scene_kept")])
    session.flush()

    result = AuthorLifecycleService(session).purge_scenes([scene_id])

    assert result == {"processed": [{"scene_id": scene_id}], "blocked": []}
    assert session.get(SceneCard, scene_id) is None
    assert session.get(SceneRunState, scene_id) is None
    assert session.get(SceneCard, "scene_kept") is not None
    assert session.get(SceneRunState, "scene_kept") is not None


def test_chapter_trash_is_blocked_when_it_has_previously_trashed_child_scenes(client) -> None:
    _create_chapter("CH610", goal="Block ambiguous chapter trash")
    _create_scene("CH610_SC01", chapter_id="CH610", scene_seq=1)
    _create_scene("CH610_SC02", chapter_id="CH610", scene_seq=2, is_chapter_last=1)

    response = client.post(
        "/api/v1/scenes/trash",
        json={"scene_ids": ["CH610_SC02"]},
        headers={"X-Idempotency-Key": "trash-ch610-sc02"},
    )
    assert response.status_code == 200

    chapter_trash_response = client.post(
        "/api/v1/chapters/trash",
        json={"chapter_ids": ["CH610"]},
        headers={"X-Idempotency-Key": "trash-ch610"},
    )

    assert chapter_trash_response.status_code == 200
    assert chapter_trash_response.json()["data"] == {
        "processed": [],
        "blocked": [
            {
                "chapter_id": "CH610",
                "code": "CHAPTER_TRASH_BLOCKED_HAS_TRASHED_SCENES",
                "message": "章节下已有单独移入回收站的场景",
            }
        ],
        "actor_ref": "operator",
    }



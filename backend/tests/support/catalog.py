"""章 / 场景骨架：几个测试文件共用的建章、建场景助手（经 ``tests/support/seed.py`` 直接落库，不走 v1 建章接口）。"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, StoryProject
from tests.support.seed import seed_chapter, seed_scene


# ---------------------------------------------------------------- 一章三场的成稿骨架（test_chapter_manuscripts）


def manuscript_chapter(chapter_id: str, *, goal: str = "Draft a chapter") -> None:
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


def manuscript_scene(
    scene_id: str,
    *,
    chapter_id: str,
    scene_seq: int,
    is_chapter_last: int = 0,
) -> None:
    seed_scene(
        scene_id,
        chapter_id=chapter_id,
        scene_seq=scene_seq,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location=f"Location {scene_id}",
        scene_goal=f"goal for {scene_id}",
        beats_json=[f"beat {scene_id}"],
        must_include_text="",
        forbidden_text="",
        exit_change="",
        hook="",
        target_length_band="medium",
        scene_type="reunion",
        is_chapter_last=is_chapter_last,
    )


# ---------------------------------------------------------------- 一章一场、供后台场景作业跑（test_scene_run_jobs）


def job_chapter_and_scene() -> None:
    seed_chapter(
        "CHJOB",
        planned_scene_count=1,
        chapter_goal="Run scene through background job",
        main_plot_push="Exercise job API",
        emotional_target="Keep operator unblocked",
        ending_effect="Pollable status",
    )
    seed_scene(
        "CHJOB_SC01",
        chapter_id="CHJOB",
        scene_seq=1,
        pov_character_id="",
        onstage_chars_json=[],
        location="Control room",
        scene_goal="Start a pollable run",
        beats_json=["start", "poll"],
        target_length_band="short",
        scene_type="test",
        is_chapter_last=1,
    )


# ---------------------------------------------------------------- 直接把一章记成已终审（test_approved_chapter_guards / test_chapter_plan）


def mark_chapter_approved(session, project_id: str, chapter_id: str) -> None:
    """不走定稿流程，直接把章写成已终审：作品的已终审章表加上它、章状态 approved（测试终审锁用）。"""
    project = session.get(StoryProject, project_id)
    chapter = session.get(ChapterGoal, chapter_id)
    assert project is not None and chapter is not None
    approved = list(project.approved_chapter_ids_json or [])
    if chapter_id not in approved:
        approved.append(chapter_id)
    project.approved_chapter_ids_json = approved
    chapter.state = "approved"
    session.commit()

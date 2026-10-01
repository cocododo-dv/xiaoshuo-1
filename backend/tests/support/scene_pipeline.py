"""场景管线测试共用的作品 / 章 / 场景骨架。"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard, SceneRunState, StoryProject
from tests.support.seed import seed_chapter, seed_project, seed_scene


# ---------------------------------------------------------------- 一部作品、一章一场（test_orchestrator_flow）


def seed_story() -> None:
    seed_project("PRJ_ORCHESTRATOR_FLOW", title="orchestrator flow", outline_text="A reunion opens an old-letter mystery.")
    seed_chapter(
        "CH001",
        project_id="PRJ_ORCHESTRATOR_FLOW",
        planned_scene_count=3,
        chapter_goal="重逢与试探成立",
        main_plot_push="旧信线索被正式打开",
        emotional_target="由迟疑转为警觉",
        ending_effect="留有余波",
    )
    seed_scene(
        "CH001_SC01",
        chapter_id="CH001",
        project_id="PRJ_ORCHESTRATOR_FLOW",
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A", "CHAR_B"],
        location="旧城门廊",
        scene_goal="让两人重新见面并建立张力",
        beats_json=["重逢", "试探", "留钩子"],
        # 这组用例测的是归档 / 出处机制（在线记账替身起草）；硬性文本约束另有专门的质检与成稿门用例
        must_include_text="",
        target_length_band="short",
        scene_type="reunion",
        is_chapter_last=0,
    )


# ---------------------------------------------------------------- 一场写好章级与场景级简报、待起草蓝图的场景（test_scene_blueprint）


BLUEPRINT_CHAPTER_ID = "BP100"
BLUEPRINT_SCENE_ID = "BP100_SC01"
BLUEPRINT_PROJECT_ID = "P_BP100"


def seed_blueprint_scene(session) -> None:
    session.add(
        StoryProject(
            project_id=BLUEPRINT_PROJECT_ID,
            title="Blueprint Fixture",
            outline_text="Blueprint fixture outline.",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id=BLUEPRINT_CHAPTER_ID,
            project_id=BLUEPRINT_PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A quiet reunion must turn into a choice.",
            main_plot_push="move from suspicion to action",
            emotional_target="trust becomes costly",
            ending_effect="leave the reader asking what was hidden",
            writer_brief_json={
                "chapter_promise": "a reunion reveals a dangerous silence",
                "escalation_path": "warmth, evasion, decision",
                "ending_question": "why does the friend hide the name",
            },
        )
    )
    session.add(
        SceneCard(
            scene_id=BLUEPRINT_SCENE_ID,
            chapter_id=BLUEPRINT_CHAPTER_ID,
            project_id=BLUEPRINT_PROJECT_ID,
            scene_seq=1,
            scene_goal="The protagonist asks for the missing name and must decide whether to trust an old friend.",
            beats_json=["ask for the name", "old friend deflects", "protagonist chooses to investigate"],
            exit_change="The old friend becomes a suspect.",
            hook="The teacup stills when the name is spoken.",
            writer_brief_json={
                "character_desire": "get the truth",
                "obstacle": "the friend answers with charm instead of facts",
                "choice_under_pressure": "trust the friend or investigate alone",
                "power_shift": "the protagonist stops asking permission",
                "new_information": "the friend recognizes the missing name",
                "emotional_turn": "warmth becomes suspicion",
                "image_anchor": "the still teacup",
                "reader_aftertaste": "affection now feels dangerous",
            },
        )
    )
    session.add(SceneRunState(scene_id=BLUEPRINT_SCENE_ID, scene_status="ready"))
    session.commit()

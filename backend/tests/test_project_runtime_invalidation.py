"""构思重新确认后的运行时失效范围（``project_runtime_invalidation``）。

B07-02：09 加 / 删一场、04–08 加 / 删一个角色，曾让影响分析整个退回「全书」——id 集合一变就当成「说不清」，
于是每一场有稿的场景都被置 stale、运行态打回 needs_replan、作品进 chapter_blocked。现在 id 集合的变化按
「新增 + 删掉 + 改过」逐个定位：新增的场 / 角色还没有任何运行时产物，删掉的正好是受影响的那几张卡。
只有载荷本身按 id 定不了位（不是列表、成员缺 id）时才退回全书。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.services.project_runtime_invalidation import (
    ProjectRuntimeInvalidationService,
    SnowflakeImpactAnalyzer,
)

PROJECT_ID = "prj-impact"
CHAPTER_ID = f"{PROJECT_ID}_CH01"


def _scene_id(index: int) -> str:
    return f"{PROJECT_ID}_S{index}"


def _seed(session, *, cards: tuple[int, ...] = (1, 2), pov: dict[int, str] | None = None) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="雨城旧信", outline_text="大纲。", status="chapter_ready"))
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, chapter_goal="打开案卷", planned_scene_count=len(cards)))
    for seq, index in enumerate(cards, start=1):
        session.add(
            SceneCard(
                scene_id=_scene_id(index),
                chapter_id=CHAPTER_ID,
                project_id=PROJECT_ID,
                scene_seq=seq,
                scene_goal=f"第 {index} 场",
                pov_character_id=(pov or {}).get(index),
            )
        )
    session.commit()


def _scenes(*indexes: int, edited: int | None = None) -> dict:
    return {
        "scenes": [
            {
                "scene_id": _scene_id(index),
                "row_uid": f"u{index}",
                "summary": f"林昭翻开第 {index} 份案卷" + ("（改）" if index == edited else ""),
            }
            for index in indexes
        ]
    }


def _characters(*ids: str, edited: str | None = None) -> dict:
    return {
        "characters": [
            {"character_id": character_id, "display_name": character_id, "goal": "查清旧信" + ("（改）" if character_id == edited else "")}
            for character_id in ids
        ]
    }


def test_adding_a_scene_to_the_scene_list_affects_no_drafted_scene(session) -> None:
    _seed(session)
    impact = SnowflakeImpactAnalyzer(session).analyze(
        PROJECT_ID, "scene_list", previous_payload=_scenes(1, 2), current_payload=_scenes(1, 2, 3)
    )
    assert impact["scope"] == "scene" and impact["broad"] is False
    assert impact["affected_scene_ids"] == []  # 新增的第 3 场还没有卡


def test_adding_and_editing_scenes_affects_only_the_edited_one(session) -> None:
    _seed(session)
    impact = SnowflakeImpactAnalyzer(session).analyze(
        PROJECT_ID, "scene_details", previous_payload=_scenes(1, 2), current_payload=_scenes(1, 2, 3, edited=2)
    )
    assert impact["scope"] == "scene"
    assert impact["affected_scene_ids"] == [_scene_id(2)]


def test_removing_a_scene_affects_exactly_its_card(session) -> None:
    _seed(session)
    impact = SnowflakeImpactAnalyzer(session).analyze(
        PROJECT_ID, "scene_list", previous_payload=_scenes(1, 2), current_payload=_scenes(1)
    )
    assert impact["scope"] == "scene"
    assert impact["affected_scene_ids"] == [_scene_id(2)]


def test_a_payload_that_cannot_be_addressed_by_id_still_falls_back_to_the_whole_book(session) -> None:
    _seed(session)
    unaddressable = {"scenes": [{"summary": "没有 scene_id 的一行"}]}
    impact = SnowflakeImpactAnalyzer(session).analyze(
        PROJECT_ID, "scene_list", previous_payload=_scenes(1, 2), current_payload=unaddressable
    )
    assert impact["scope"] == "project" and impact["broad"] is True
    assert impact["affected_scene_ids"] == [_scene_id(1), _scene_id(2)]


def test_a_pure_reorder_is_still_a_change_of_the_moved_scenes(session) -> None:
    """只看两边都在的场之间的先后：挪动先后仍然算改（场的上下文变了），插进一场不算别人的改。"""
    _seed(session, cards=(1, 2, 3))
    impact = SnowflakeImpactAnalyzer(session).analyze(
        PROJECT_ID, "scene_list", previous_payload=_scenes(1, 2, 3), current_payload=_scenes(2, 1, 3)
    )
    assert impact["scope"] == "scene"
    assert impact["affected_scene_ids"] == [_scene_id(1), _scene_id(2)]


def test_adding_a_character_affects_no_scene_and_removing_one_affects_its_scenes(session) -> None:
    lin, shen, gu = f"{PROJECT_ID}_c1", f"{PROJECT_ID}_c2", f"{PROJECT_ID}_c3"
    _seed(session, pov={1: lin, 2: shen})
    analyzer = SnowflakeImpactAnalyzer(session)
    added = analyzer.analyze(
        PROJECT_ID, "character_sheets", previous_payload=_characters(lin, shen), current_payload=_characters(lin, shen, gu)
    )
    assert added["scope"] == "scene" and added["affected_scene_ids"] == []
    removed = analyzer.analyze(
        PROJECT_ID, "character_bibles", previous_payload=_characters(lin, shen), current_payload=_characters(lin)
    )
    assert removed["scope"] == "scene" and removed["affected_scene_ids"] == [_scene_id(2)]


def test_inserting_a_scene_row_keeps_every_drafted_scene_valid(session) -> None:
    """真实场景：作者写了几场，在 09 看板里插进一行新场并确认——已有的稿一张都不该变成失败稿。"""
    _seed(session)
    session.add(
        SceneDraft(
            row_id="draft-s1",
            scene_id=_scene_id(1),
            chapter_id=CHAPTER_ID,
            stage="neutral",
            content="林昭在雨里拆开旧信。",
            source_bundle_id="bundle-s1",
            source_bundle_hash="hash-s1",
        )
    )
    session.add(SceneRunState(scene_id=_scene_id(1), scene_status="archived", current_neutral_draft_row_id="draft-s1"))
    session.commit()

    impact = ProjectRuntimeInvalidationService(session).invalidate_for_snowflake_step(
        PROJECT_ID, "scene_list", previous_payload=_scenes(1, 2), current_payload=_scenes(1, 3, 2)
    )
    session.commit()
    assert impact["scope"] == "scene" and impact["affected_scene_ids"] == []
    assert session.get(SceneDraft, "draft-s1").status == "active"
    assert session.get(SceneRunState, _scene_id(1)).scene_status == "archived"
    assert session.get(StoryProject, PROJECT_ID).status == "chapter_ready"

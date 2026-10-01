"""雪花分章物化曾给每一章写一套固定的句子（情绪目标 / 结尾效果 / 禁写 / 备注，S1 9）。

物化不再写它们；已经带着它们的章（下一次「确认写入」之前），各处读的时候都当「没规划」——不当作者写的事实，
也不因为少了它们就让哪一道检查开始失败。
"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneBlueprint, SceneCard, StoryProject

EMOTION = "让人物目标、阻碍和代价在行动中显形。"
ENDING = "用新的选择、代价或信息推动下一章。"
MUST_NOT = "不得复制参考书原文表达、人物、设定或桥段。"
NOTES = "由雪花法分章物化，需确认后进入逐章运行。"
PROJECT_ID = "PRJ_CANNED"


def _legacy_chapter(session, chapter_id: str, *, goal: str = "林昭把旧信交给案卷室。") -> ChapterGoal:
    if session.get(StoryProject, PROJECT_ID) is None:
        session.add(StoryProject(project_id=PROJECT_ID, title="样板句", outline_text=""))
        session.flush()
    chapter = ChapterGoal(
        chapter_id=chapter_id,
        project_id=PROJECT_ID,
        planned_scene_count=1,
        chapter_goal=goal,
        main_plot_push=goal,
        emotional_target=EMOTION,
        ending_effect=ENDING,
        must_not=MUST_NOT,
        notes=NOTES,
        writer_brief_json={"source": "snowflake_method"},
    )
    session.add(chapter)
    session.flush()
    return chapter


def test_the_retired_sentences_are_exactly_the_ones_the_builder_wrote() -> None:
    from novel_system.services.story_slots import RETIRED_CHAPTER_BOILERPLATE

    assert RETIRED_CHAPTER_BOILERPLATE == {EMOTION, ENDING, MUST_NOT, NOTES}


def test_the_execution_contract_does_not_take_the_canned_emotion_and_stays_active(session) -> None:
    from novel_system.services.scene_execution import SceneExecutionContractService

    _legacy_chapter(session, "CANNED_CH01")
    session.add(
        SceneCard(
            scene_id="CANNED_CH01_SC01",
            chapter_id="CANNED_CH01",
            project_id=PROJECT_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            scene_goal="林昭在雨城码头等送信人",
            writer_brief_json={"goal": "拿到旧信", "conflict": "送信人不肯交", "setback_or_victory": "信被雨水泡烂"},
        )
    )
    session.add(
        SceneBlueprint(
            row_id="bp_canned",
            scene_id="CANNED_CH01_SC01",
            chapter_id="CANNED_CH01",
            status="accepted",
            blueprint_json={"scene_crucible": "天亮前拿不到信，案卷就要归档"},
        )
    )
    session.commit()

    contract = SceneExecutionContractService(session).generate("CANNED_CH01_SC01", actor_ref="test")

    # 以前每一场没写读者情绪的，都拿到这句样板当「读者该有的感受」
    assert contract.status == "active"
    assert contract.payload_json["expected_reader_emotion"] in (None, "")
    assert EMOTION not in str(contract.payload_json)


def test_chapter_set_checks_do_not_count_the_canned_sentences_as_evidence(session) -> None:
    from novel_system.services.literary_quality.chapter_set import (
        _chapter_set_payoff_reveal_checks,
        _theme_variety_score,
    )

    chapters = [_legacy_chapter(session, f"CANNED_SET_{index}") for index in range(3)]
    # 没有正文的章：以前样板句里的「代价」「选择」替它们冒充了证据
    checks = _chapter_set_payoff_reveal_checks(chapters, [])
    assert checks["forced_choice_chapter_ids"] == [] and checks["cost_chapter_ids"] == []
    # 三章同一句样板情绪：以前算成「全书只有一种情绪」（0.0）；没规划就是数据不够（0.5）
    assert _theme_variety_score(chapters) == 0.5
    # 作者写了的照旧算
    chapters[0].emotional_target = "她不得不选择，代价是失去证人"
    assert _chapter_set_payoff_reveal_checks(chapters[:1], [])["cost_chapter_ids"] == [chapters[0].chapter_id]


def test_chapter_payloads_and_the_planning_context_say_not_planned(session) -> None:
    from novel_system.services.author_lifecycle import AuthorLifecycleService
    from novel_system.services.chapter_planning_context import ChapterPlanningContextBuilder
    from novel_system.services.project_payloads import chapter_payload

    chapter = _legacy_chapter(session, "CANNED_CH09")
    session.commit()

    lifecycle = AuthorLifecycleService(session)
    for payload in (lifecycle.serialize_chapter(chapter), lifecycle.serialize_chapter_summary(chapter)):
        assert (payload["emotional_target"], payload["ending_effect"], payload["must_not"], payload["notes"]) == (
            None,
            None,
            None,
            None,
        )
    v1 = chapter_payload(session, chapter)
    assert (v1["emotional_target"], v1["ending_effect"], v1["must_not"]) == (None, None, None)
    assert ChapterPlanningContextBuilder(session)._constraints_slot(chapter)["must_not"] == ""

    # 作者写的原样给
    chapter.emotional_target = "  由迟疑转入警觉  "
    chapter.must_not = "不写梦境"
    assert lifecycle.serialize_chapter(chapter)["emotional_target"] == "  由迟疑转入警觉  "
    assert ChapterPlanningContextBuilder(session)._constraints_slot(chapter)["must_not"] == "不写梦境"


def test_the_workbench_diagnostics_read_the_canned_chapter_text_as_not_planned(session) -> None:
    """起草台工作台的诊断部分（``?include=diagnostics``）印章目标块：旧的样板情绪目标 / 结尾效果算「没规划」（None），
    作者写的照给（P09b 把工作台搬进 services/scene_workbench.py，S1 9 退役样板句；I7 合并胶水 G3）。"""
    from novel_system.services.scene_workbench import SceneWorkbenchService

    chapter = _legacy_chapter(session, "CANNED_CH07")
    session.add(
        SceneCard(
            scene_id="CANNED_CH07_SC01",
            chapter_id="CANNED_CH07",
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="林昭在雨城码头等送信人",
        )
    )
    session.commit()

    block = SceneWorkbenchService(session).payload("CANNED_CH07_SC01", diagnostics=True)["chapter_goal"]
    assert block == {
        "chapter_id": "CANNED_CH07",
        "chapter_goal": "林昭把旧信交给案卷室。",
        "main_plot_push": "林昭把旧信交给案卷室。",
        "emotional_target": None,
        "ending_effect": None,
    }

    # 作者写的原样给
    chapter.emotional_target = "由迟疑转入警觉"
    chapter.ending_effect = "旧信落进案卷室的那一刻，门外有人敲了三下"
    session.commit()
    block = SceneWorkbenchService(session).payload("CANNED_CH07_SC01", diagnostics=True)["chapter_goal"]
    assert (block["emotional_target"], block["ending_effect"]) == (
        "由迟疑转入警觉",
        "旧信落进案卷室的那一刻，门外有人敲了三下",
    )

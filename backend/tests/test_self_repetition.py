from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.self_repetition import (
    LifetimeExpressionRegistry,
    SelfRepetitionDetector,
    check_semantic_repetition,
    format_semantic_repetition_guidance,
)


def _seed_two_scenes(session, *, shared_phrase: str = "") -> None:
    session.add(ChapterGoal(chapter_id="SR100", planned_scene_count=3, chapter_goal="test"))
    session.add(ChapterState(chapter_id="SR100", current_phase="drafting"))
    for idx in (1, 2):
        scene_id = f"SR100_SC0{idx}"
        final_row_id = f"final_{scene_id}"
        session.add(SceneCard(scene_id=scene_id, chapter_id="SR100", scene_seq=idx, scene_goal="test"))
        session.add(SceneRunState(scene_id=scene_id, scene_status="archived", current_final_scene_row_id=final_row_id))
        content = f"Unique content for scene {idx}. " + (shared_phrase if shared_phrase else f"Only in scene {idx}.")
        session.add(FinalScene(
            row_id=final_row_id, scene_id=scene_id, chapter_id="SR100",
            content=content, status="approved",
            source_bundle_id=f"b_{scene_id}", source_bundle_hash=f"h_{scene_id}",
        ))
    session.commit()


def test_self_repetition_top_repeated_ngrams(session) -> None:
    shared = "The glass rain hammered the abandoned station roof while she counted"
    _seed_two_scenes(session, shared_phrase=shared)

    detector = SelfRepetitionDetector(session)
    ngrams = detector.top_repeated_ngrams("SR100")

    assert len(ngrams) >= 1


def _seed_final_scene(session, *, project_id: str, chapter_id: str, display_order: int, scene_id: str, text: str) -> None:
    if session.get(StoryProject, project_id) is None:
        session.add(StoryProject(project_id=project_id, title=project_id, outline_text="test"))
    if session.get(ChapterGoal, chapter_id) is None:
        session.add(ChapterGoal(
            chapter_id=chapter_id, project_id=project_id, planned_scene_count=1,
            chapter_goal="test", display_order=display_order,
        ))
    session.flush()
    session.add(SceneCard(scene_id=scene_id, chapter_id=chapter_id, project_id=project_id, scene_seq=1, scene_goal="test"))
    session.add(SceneRunState(scene_id=scene_id, scene_status="archived", current_final_scene_row_id=f"final_{scene_id}"))
    session.add(FinalScene(
        row_id=f"final_{scene_id}", scene_id=scene_id, chapter_id=chapter_id, content=text,
        status="approved", source_bundle_id=f"b_{scene_id}", source_bundle_hash=f"h_{scene_id}",
    ))
    session.commit()


def test_corpus_previous_chapter_is_the_one_before_it_in_the_same_book(session) -> None:
    """B04-10：「上一章」按本书目录次序取，不按章号字典序在所有作品里取。

    章号是钉住的流水号：P_TWO 的 CH07 在目录里排第一、CH05 排第二；按字典序 CH05 前面是别的作品的 CH04。"""
    _seed_final_scene(session, project_id="P_ONE", chapter_id="CH04", display_order=1, scene_id="P_ONE_S1",
                      text="别的作品的终稿：雨城码头的旧信。")
    _seed_final_scene(session, project_id="P_TWO", chapter_id="CH07", display_order=1, scene_id="P_TWO_S1",
                      text="本书第一章的终稿：林昭在案卷里夹了一张车票。")
    _seed_final_scene(session, project_id="P_TWO", chapter_id="CH05", display_order=2, scene_id="P_TWO_S2",
                      text="本书第二章的终稿。")

    detector = SelfRepetitionDetector(session)
    _texts, second_chapter_ids = detector._load_corpus("P_TWO_S2", "CH05", lookback_scenes=6)
    _texts, first_chapter_ids = detector._load_corpus("P_TWO_S1", "CH07", lookback_scenes=6)

    assert second_chapter_ids == ["P_TWO_S1"]
    assert first_chapter_ids == [], "第一章前面没有章，不能借别的作品的章"


_BOOK_TEXTS = (
    "雨城的码头上起了雾。林昭像一只受惊的鸟，攥紧了拳头，心如刀割。她叹了口气，把旧信塞回案卷。",
    "雨城的钟楼敲了三下。老周像一只受惊的鸟，攥紧了拳头，五味杂陈。他叹了口气，没有回头。",
    "天还没亮，雨停了。林昭微微一笑，眼眶微红，把车票夹进案卷，像一片湿透的落叶。",
)


def _seed_book_finals(session) -> None:
    session.add(StoryProject(project_id="P_BOOK", title="book", outline_text="test"))
    session.add(ChapterGoal(chapter_id="P_BOOK_CH01", project_id="P_BOOK", planned_scene_count=3,
                            chapter_goal="test", display_order=1))
    session.flush()
    for index, text in enumerate(_BOOK_TEXTS, start=1):
        scene_id = f"P_BOOK_SC{index:02d}"
        session.add(SceneCard(scene_id=scene_id, chapter_id="P_BOOK_CH01", project_id="P_BOOK",
                              scene_seq=index, scene_goal="test"))
        session.add(SceneRunState(scene_id=scene_id, scene_status="archived",
                                  current_final_scene_row_id=f"final_{scene_id}"))
        session.add(FinalScene(row_id=f"final_{scene_id}", scene_id=scene_id, chapter_id="P_BOOK_CH01",
                               content=text, status="approved", source_bundle_id=f"b_{scene_id}",
                               source_bundle_hash=f"h_{scene_id}"))
    # 一场还没有终稿：不进语料，也不进全书已用表达
    session.add(SceneCard(scene_id="P_BOOK_SC04", chapter_id="P_BOOK_CH01", project_id="P_BOOK",
                          scene_seq=4, scene_goal="test"))
    session.add(SceneRunState(scene_id="P_BOOK_SC04", scene_status="ready"))
    session.commit()


def test_lifetime_avoidance_guidance_lists_the_books_expressions_by_frequency(session) -> None:
    _seed_book_finals(session)

    guidance = LifetimeExpressionRegistry(session).get_lifetime_avoidance_guidance("P_BOOK")

    assert guidance == "\n".join(
        [
            "【全书已用表达禁用清单 -- 请勿在新场景中重复使用】",
            "已用过的比喻/意象：",
            "  - 像一只受惊的鸟，攥紧了 (x2)",
            "  - 像一片湿透的落叶。 (x1)",
            "已用过的场景开头方式：",
            "  - 雨城的码头上起了雾 (x1)",
            "  - 雨城的钟楼敲了三下 (x1)",
            "  - 天还没亮，雨停了 (x1)",
            "已用过的角色动作口癖：",
            "  - 叹了口气 (x2)",
            "  - 攥紧了拳头 (x2)",
            "  - 微微一笑 (x1)",
            "  - 眼眶微红 (x1)",
            "已用过的情绪惯用语：",
            "  - 心如刀割 (x1)",
            "  - 五味杂陈 (x1)",
        ]
    )
    assert LifetimeExpressionRegistry(session).get_lifetime_avoidance_guidance("P_NONE") == ""


def test_recent_corpus_feeds_semantic_repetition_guidance(session) -> None:
    _seed_book_finals(session)
    detector = SelfRepetitionDetector(session)

    texts, scene_ids = detector._load_corpus("P_BOOK_SC04", "P_BOOK_CH01", lookback_scenes=6)
    assert scene_ids == ["P_BOOK_SC03", "P_BOOK_SC02", "P_BOOK_SC01"]
    assert texts == [_BOOK_TEXTS[2], _BOOK_TEXTS[1], _BOOK_TEXTS[0]]

    hits = check_semantic_repetition("雨城的码头又下起雨。他攥紧了拳头，叹了口气，心如刀割。", texts, scene_ids)
    assert [(hit.pattern_type, hit.current_text, hit.source_scene_id) for hit in hits] == [
        ("scene_opener", "雨城的码头又下起雨", "P_BOOK_SC01"),
        ("action_habit", "叹了口气", "P_BOOK_SC02"),
        ("action_habit", "攥紧了拳头", "P_BOOK_SC02"),
        ("emotional_expression", "心如刀割", "P_BOOK_SC01"),
    ]
    assert format_semantic_repetition_guidance(hits).startswith("## Semantic Repetition Alert")
    assert detector.top_repeated_ngrams("P_BOOK_CH01", lookback_scenes=6, top_n=8) == [
        "像一只受惊的鸟攥",
        "一只受惊的鸟攥紧",
        "只受惊的鸟攥紧了",
        "受惊的鸟攥紧了拳",
        "惊的鸟攥紧了拳头",
    ]

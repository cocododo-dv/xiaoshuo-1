"""跨章检查（2026-10-20）：全书反复出现的写法进写作台深改（来源「跨场重复」），成稿中心「跨章」页签列全书的那一份，
外加章与章的形状（连着几章同一种收尾 / 开头、篇幅异常）与线索台账的提醒。不用模型，按各场现在的正文现算。"""

from __future__ import annotations

from novel_system.db.models import StoryCharacter
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.cross_chapter import CrossChapterService, paragraph_edge_kind
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_diagnosis.repeats import (
    SceneText,
    find_repeats,
    name_forms,
    repeat_signal_id,
)
from tests.support.seed import seed_chapter, seed_project, seed_scene

PROJECT = "XCH_PROJECT"


def _scene(chapter: int, scene: int, *paragraphs: str) -> SceneText:
    return SceneText(
        chapter_id=f"XCH_CH{chapter:02d}",
        chapter_number=chapter,
        scene_id=f"XCH_CH{chapter:02d}_SC{scene:02d}",
        scene_number=scene,
        paragraphs=tuple(paragraphs),
    )


# ---------------------------------------------------------------------------
# 找重复（纯函数）
# ---------------------------------------------------------------------------


def test_repeated_writing_is_found_across_scenes_and_names_are_cut_out() -> None:
    scenes = [
        _scene(1, 1, "林昭手指慢慢攥得发白，把旧信塞回案卷。", "雨城的夜里，许望眼眶慢慢红了。"),
        _scene(1, 2, "许望手指慢慢攥得发白，没有说话。"),
        _scene(2, 1, "她眼眶慢慢红了，抓起案卷就走。", "档案室里，林昭眼眶慢慢红了起来。"),
    ]
    report = find_repeats(scenes, names=["林昭（档案修复师）", "许望/老许"])
    phrases = {item.phrase: item for item in report.phrases}
    assert set(phrases) == {"手指慢慢攥得发白", "眼眶慢慢红了"}, "名字处断开：两个人的同一个写法算一句"
    assert phrases["手指慢慢攥得发白"].count == 2 and phrases["手指慢慢攥得发白"].scene_ids == ["XCH_CH01_SC01", "XCH_CH01_SC02"]
    assert phrases["眼眶慢慢红了"].count == 3 and phrases["眼眶慢慢红了"].chapter_ids == ["XCH_CH01", "XCH_CH02"]
    first = phrases["手指慢慢攥得发白"].occurrences[0]
    assert (first.paragraph_index, first.start) == (0, 2), "偏移按段里的可见文字算（名字之后）"
    assert report.by_scene["XCH_CH02_SC01"][0].phrase == "眼眶慢慢红了"
    assert name_forms(["林昭（档案修复师）", "许望/老许"]) == ["林昭", "老许", "许望"]
    assert phrases["眼眶慢慢红了"].signal_id == repeat_signal_id("眼眶慢慢红了")


def test_plot_nouns_dates_short_twos_and_one_scene_repeats_are_not_writing() -> None:
    scenes = [
        _scene(1, 1, "他把铝制档案牌放进抽屉。八月十六日那一夜很长。", "林昭小心翼翼地翻开旧信。", "他反复念着那句话，那句话，那句话。"),
        _scene(1, 2, "铝制档案牌还在抽屉里。八月十六日那一夜很长。", "许望小心翼翼地合上案卷。"),
    ]
    report = find_repeats(scenes, names=["林昭", "许望"])
    assert report.phrases == (), "纯名词、带日期的、只出现两次的短片段、同一场里的重复都不算"


def test_particles_are_trimmed_and_longer_repeats_swallow_their_parts() -> None:
    scenes = [
        _scene(1, 1, "他的眼神一下子冷了下去，雨还在下。"),
        _scene(2, 1, "她的眼神一下子冷了下去，灯灭了。"),
    ]
    report = find_repeats(scenes)
    assert [item.phrase for item in report.phrases] == ["眼神一下子冷了下去"], "开头的半截「的」修掉，短的被长的包住"


def test_the_reference_author_habit_is_only_a_hint() -> None:
    scenes = [_scene(1, 1, "她从口袋里摸出一封旧信。"), _scene(1, 2, "他从口袋里摸出案卷。"), _scene(2, 1, "许望从口袋里摸出钥匙。")]
    corpus = "他从口袋里摸出一把刀。" * 40 + "雨" * 2000
    report = find_repeats(scenes, reference_counter=corpus.count, reference_chars=len(corpus))
    item = next(entry for entry in report.phrases if entry.phrase == "从口袋里摸出")
    assert item.reference_count == 40 and item.habitual is True
    plain = find_repeats(scenes)
    assert next(entry for entry in plain.phrases if entry.phrase == "从口袋里摸出").habitual is False


def test_paragraph_edge_kinds() -> None:
    assert paragraph_edge_kind("他还会回来吗？") == "question"
    assert paragraph_edge_kind("“你还会回来吗？”") == "question"
    assert paragraph_edge_kind("雨停了……") == "ellipsis"
    assert paragraph_edge_kind("“走吧。”林昭说。") == "dialogue"
    assert paragraph_edge_kind("快跑！") == "exclaim"
    assert paragraph_edge_kind("雨城的夜很长。") == "narration"
    assert paragraph_edge_kind("“你见过那封旧信吗？”", "opening") == "dialogue", "开头只分对白与叙述"
    assert paragraph_edge_kind("雨停了吗？", "opening") == "narration"


# ---------------------------------------------------------------------------
# 进写作台深改、成稿中心「跨章」
# ---------------------------------------------------------------------------


def _write(session, scene_id: str, paragraphs: list[str]) -> None:
    service = AuthorDraftService(session)
    draft = service.ensure("scene", scene_id, actor_ref="writer")["draft"]
    html = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    service.save(draft["draft_id"], {"content": html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()


def _book(session, chapters: list[list[list[str]]], *, empty_chapters: int = 0) -> None:
    seed_project(PROJECT, title="跨章", outline_text="")
    for number, scenes in enumerate(chapters, start=1):
        chapter_id = f"XCH_CH{number:02d}"
        seed_chapter(chapter_id, project_id=PROJECT, display_order=number, planned_scene_count=len(scenes), chapter_goal="")
        for seq, paragraphs in enumerate(scenes, start=1):
            scene_id = f"{chapter_id}_SC{seq:02d}"
            seed_scene(scene_id, chapter_id=chapter_id, scene_seq=seq, project_id=PROJECT, scene_goal="")
            if paragraphs:
                _write(session, scene_id, paragraphs)
    for extra in range(empty_chapters):
        seed_chapter(f"XCH_CH{len(chapters) + extra + 1:02d}", project_id=PROJECT, display_order=len(chapters) + extra + 1, chapter_goal="")
    session.add(StoryCharacter(character_id=f"{PROJECT}_linzhao", project_id=PROJECT, display_name="林昭（档案修复师）"))
    session.commit()


def test_each_repeat_is_a_finding_in_the_writer_and_the_report_lists_the_whole_book(client, session) -> None:
    _book(
        session,
        [
            [["雨城的夜里，林昭手指慢慢攥得发白。", "她把旧信塞回案卷。"], ["许望站在门口。"]],
            [["档案室的灯亮着，林昭手指慢慢攥得发白。"]],
        ],
    )
    payload = SceneDiagnosisService(session).payload("XCH_CH01_SC01")
    repeats = [finding for finding in payload["findings"] if finding["source"] == "repeat"]
    assert len(repeats) == 1
    finding = repeats[0]
    assert finding["label"] == "跨场重复" and finding["severity"] == "taste"
    assert finding["signal_id"] == repeat_signal_id("手指慢慢攥得发白")
    assert "第 2 章第 1 场也出现过" in finding["issue"] and finding["recommendation"]
    assert finding["evidence"] == {"excerpt": "手指慢慢攥得发白", "paragraph_index": 0, "start": 8, "end": 16}
    assert payload["summary"]["by_source"]["repeat"] == 1
    assert not [f for f in SceneDiagnosisService(session).payload("XCH_CH01_SC02")["findings"] if f["source"] == "repeat"]

    # 在第 2 章那一场忽略：报告里那一处标已忽略，第 1 章那一处照旧开着
    other = client.get("/api/v1/scenes/XCH_CH02_SC01/deep-review").json()["data"]
    saved = client.patch(
        "/api/v1/scenes/XCH_CH02_SC01/deep-review/preferences",
        json={"ignored_issue_keys": [finding["signal_id"]], "base_revision_no": other["preferences"]["revision_no"]},
    )
    assert saved.status_code == 200, saved.text
    response = client.get(f"/api/v1/projects/{PROJECT}/cross-chapter-check")
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["scenes_with_text"] == 3 and report["chapters_with_text"] == 2
    item = report["repeats"][0]
    assert item["phrase"] == "手指慢慢攥得发白" and item["signal_id"] == finding["signal_id"]
    assert [(o["chapter_number"], o["scene_number"], o["ignored"]) for o in item["occurrences"]] == [(1, 1, False), (2, 1, True)]
    assert item["ignored"] is False


def test_the_report_names_streaks_of_endings_and_openings_and_length_outliers(client, session) -> None:
    long_scene = ["雨城的夜很长，林昭把旧信一页一页地摊开。" * 12]
    _book(
        session,
        [
            [["“你见过那封旧信吗？”", "林昭没有回答。", "他还会回来吗？"]],
            [["“走吧。”许望说。", "案卷还在桌上。", "那封信是谁写的？"]],
            [["“灯亮了。”林昭说。", *long_scene, "她到底在等谁？"]],
            [["雨停了。", "许望关上了门。"], []],
        ],
        empty_chapters=1,
    )
    report = CrossChapterService(session).report(PROJECT)
    shapes = report["shapes"]
    assert [c["complete"] for c in shapes["chapters"]] == [True, True, True, False, False], "第 4 章有一场没写，第 5 章没有场"
    streaks = {(item["edge"], item["kind"]): item for item in shapes["streaks"]}
    assert streaks[("ending", "question")]["chapter_numbers"] == [1, 2, 3]
    assert streaks[("opening", "dialogue")]["chapter_numbers"] == [1, 2, 3]
    assert shapes["lengths"]["enough"] is True
    assert [(item["number"], item["kind"]) for item in shapes["lengths"]["items"]] == [(3, "long")]
    assert report["threads"]["open_count"] == 0 and report["threads"]["items"] == []
    assert report["edge_kind_labels"]["question"] == "问句"

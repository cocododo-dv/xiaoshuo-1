"""故事记忆（2026-10-12，``services/story_memory.py`` / ``story_memory_digest.py``）。

起草后面的场次时，远处的事实只从两处来：事件日志的工作记忆（各场当前终稿上抽出来、还没核对完的事实也算，作者驳回
的不算；硬质检与成稿门照旧只认正史）与章摘要的「前情」（取代卷汇总的原文拼接）。这里钉住：

- 工作记忆：抽出来的事实进起草的人物状态、不进正史；驳回即消失；换了一版终稿、新抽取还没跑时不拿旧版的候选；
- 「前情」：此前各章的摘要、没写好的章占位、最近几章全文 / 更早一句、未收的线取最近一份；预算紧时先压后删，
  不再因为远处的记忆判「建议拆场」；
- 章摘要的状态（写完没写完、旧没旧）、后台一轮（先补抽、再按故事顺序写，下一章拿到上一章的线索清单；正文一变、
  后面的章跟着判旧；作者改过的不自动重写）、失败路径（没模型 409 且后台停下、输出不合格 502 且什么也不存）；
- 成稿中心的接口：列表、作者改摘要（编辑期间换了一份 → 409）。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import pytest

from novel_system.db.models import (
    ChapterDigest,
    ChapterGoal,
    FactCandidate,
    FinalScene,
    LlmCall,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
)
from novel_system.services import story_memory, story_memory_digest
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.context_budget import apply_context_budget, collect_prompt_sections
from novel_system.services.errors import DomainError
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.story_memory import (
    chapter_memory_states,
    generate_chapter_digest,
    run_story_memory_pass,
    save_author_digest,
)
from novel_system.services.story_memory_digest import (
    OPEN_THREADS_HEADER,
    STORY_SO_FAR_HEADER,
    compress_story_so_far,
    story_so_far,
)
from tests.narrative_fixtures import WORLD_PROJECT, seed_final_scene, seed_narrative_world, world_scene
from tests.support.llm_fakes import ScriptedTaskRunner

PROJECT = "PRJ_MEMORY"


# ------------------------------------------------------------------ 夹具：一部三章的小书


def _chapter(index: int) -> str:
    return f"{PROJECT}_CH{index:02d}"


def _scene(chapter: int, seq: int) -> str:
    return f"{PROJECT}_CH{chapter:02d}_SC{seq:02d}"


def _seed_book(session, *, chapters: int = 3, scenes_per_chapter: int = 2) -> None:
    session.add(StoryProject(project_id=PROJECT, title="雨城旧信", outline_text=""))
    session.flush()
    titles = ["旧信", "钟楼", "北境", "案卷"]
    for index in range(1, chapters + 1):
        session.add(
            ChapterGoal(
                chapter_id=_chapter(index),
                project_id=PROJECT,
                chapter_goal=f"第 {index} 章",
                planned_scene_count=scenes_per_chapter,
                display_order=index,
                narrative_json={"title": titles[index - 1]},
            )
        )
    session.flush()
    for index in range(1, chapters + 1):
        for seq in range(1, scenes_per_chapter + 1):
            session.add(
                SceneCard(
                    scene_id=_scene(index, seq),
                    chapter_id=_chapter(index),
                    project_id=PROJECT,
                    scene_seq=seq,
                    scene_goal=f"第 {index} 章第 {seq} 场",
                )
            )
    session.add(StoryCharacter(character_id=f"{PROJECT}_LZ", project_id=PROJECT, display_name="林昭", status="active"))
    session.flush()


def _write_final(session, scene_id: str, content: str, *, version: int = 1) -> FinalScene:
    """给一场落一版终稿（版本号递增 = 作者改过），指针指向它，并像归档那样记下「等抽取」。"""
    scene = session.get(SceneCard, scene_id)
    final = FinalScene(
        row_id=f"final_{scene_id}_v{version}",
        scene_id=scene_id,
        chapter_id=scene.chapter_id,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        status="archived",
        source_bundle_id=f"bundle_{scene_id}",
        source_bundle_hash="bundle-hash",
    )
    session.add(final)
    session.flush()
    state = session.get(SceneRunState, scene_id)
    if state is None:
        state = SceneRunState(scene_id=scene_id, scene_status="archived")
        session.add(state)
    state.current_final_scene_row_id = final.row_id
    session.flush()
    CanonContinuityService(session).mark_archive_pending(final.row_id)
    session.flush()
    return final


def _digest_row(chapter_id: str, *, summary: str, threads: list[str] = (), ending: str = "", changes=()) -> ChapterDigest:
    return ChapterDigest(
        row_id=f"digest_{chapter_id}",
        project_id=PROJECT,
        chapter_id=chapter_id,
        digest_json={
            "summary": summary,
            "ending_state": ending,
            "character_changes": [{"character": name, "change": change} for name, change in changes],
            "open_threads": [{"thread": thread, "opened_in": 1} for thread in threads],
            "resolved_threads": [],
        },
        content_hash=f"hash_{chapter_id}",
        source_hash="seeded",
    )


def _digest_reply(summary: str, *, threads: list[str] = (), ending: str = "雨还在下。") -> dict:
    return {
        "summary": summary,
        "ending_state": ending,
        "character_changes": [{"character": "林昭", "change": "收下了旧信"}],
        "open_threads": [{"thread": thread, "opened_in": 1} for thread in threads],
        "resolved_threads": [],
    }


@contextmanager
def _same_session(session):
    yield session


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")


def _install(monkeypatch, runner) -> None:
    # 正史「提取」在调用时从 llm_task_runner 取运行器，章摘要从 story_memory 的模块名字取
    monkeypatch.setattr("novel_system.services.llm_task_runner.LLMNodeRunner", lambda session: runner)
    monkeypatch.setattr(story_memory, "LLMNodeRunner", lambda session: runner)


@pytest.fixture(autouse=True)
def _fresh_attempt_counters(monkeypatch):
    monkeypatch.setattr(story_memory, "_EXTRACTION_ATTEMPTS", {})
    monkeypatch.setattr(story_memory, "_DIGEST_ATTEMPTS", {})


# ------------------------------------------------------------------ 工作记忆（事件日志）


def _extract(session, monkeypatch, scene_id: str, events: list[dict]) -> ScriptedTaskRunner:
    prefix = f"llmcall_extract_{session.query(LlmCall).count()}"
    runner = ScriptedTaskRunner(session, [{"events": events}], call_prefix=prefix)
    monkeypatch.setattr("novel_system.services.llm_task_runner.LLMNodeRunner", lambda session: runner)
    CanonContinuityService(session).extract_scene_candidates(WORLD_PROJECT, scene_id)
    session.commit()
    return runner


_INJURY = {
    "event_type": "character_state",
    "entity_id": "苏晚",
    "fact_key": "injury",
    "fact_value": "左腿中了一箭",
    "evidence": "苏晚的左腿中了一箭",
}


def _drafting_state(session, scene_id: str) -> str:
    return NarrativeEventLog(session).format_state_for_prompt(WORLD_PROJECT, scene_id=scene_id)


def test_unreviewed_extracted_facts_reach_the_drafting_state_but_not_canon(session, monkeypatch, llm_on) -> None:
    seed_narrative_world(session)
    scene = session.get(SceneCard, world_scene(2, 2))
    seed_final_scene(session, scene=scene, content="苏晚的左腿中了一箭，她没有停下。")
    CanonContinuityService(session).mark_archive_pending(f"final_{scene.scene_id}_v1")
    session.commit()
    assert "左腿中了一箭" not in _drafting_state(session, world_scene(2, 3))

    _extract(session, monkeypatch, scene.scene_id, [_INJURY])

    assert "左腿中了一箭" in _drafting_state(session, world_scene(2, 3))
    # 边界照旧：写这一场时看不到这一场自己的事实
    assert "左腿中了一箭" not in _drafting_state(session, world_scene(2, 2))
    # 正史（硬质检、成稿门读的）里没有它
    canon = NarrativeEventLog(session).events(WORLD_PROJECT, before_scene_id=world_scene(2, 3))
    assert "左腿中了一箭" not in {event.fact_value for event in canon}


def test_a_rejected_candidate_leaves_the_drafting_state(session, monkeypatch, llm_on) -> None:
    seed_narrative_world(session)
    scene = session.get(SceneCard, world_scene(2, 2))
    seed_final_scene(session, scene=scene, content="苏晚的左腿中了一箭，她没有停下。")
    CanonContinuityService(session).mark_archive_pending(f"final_{scene.scene_id}_v1")
    session.commit()
    _extract(session, monkeypatch, scene.scene_id, [_INJURY])
    candidate = session.query(FactCandidate).filter_by(scene_id=scene.scene_id).one()

    CanonContinuityService(session).decide_candidate(
        WORLD_PROJECT, candidate.candidate_id, action="reject", actor_ref="test"
    )
    session.commit()

    assert "左腿中了一箭" not in _drafting_state(session, world_scene(2, 3))


def test_a_new_final_drops_the_old_candidates_until_its_own_extraction_runs(session, monkeypatch, llm_on) -> None:
    seed_narrative_world(session)
    scene = session.get(SceneCard, world_scene(2, 2))
    seed_final_scene(session, scene=scene, content="苏晚的左腿中了一箭，她没有停下。")
    CanonContinuityService(session).mark_archive_pending(f"final_{scene.scene_id}_v1")
    session.commit()
    _extract(session, monkeypatch, scene.scene_id, [_INJURY])

    revised = FinalScene(
        row_id=f"final_{scene.scene_id}_v2",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content="苏晚躲过了那一箭，只划破了衣袖。",
        content_hash=hashlib.sha256("苏晚躲过了那一箭，只划破了衣袖。".encode("utf-8")).hexdigest(),
        status="archived",
        source_bundle_id="bundle_v2",
        source_bundle_hash="bundle-hash",
    )
    session.add(revised)
    session.flush()
    session.get(SceneRunState, scene.scene_id).current_final_scene_row_id = revised.row_id
    CanonContinuityService(session).mark_archive_pending(revised.row_id)
    session.commit()
    assert "左腿中了一箭" not in _drafting_state(session, world_scene(2, 3))

    _extract(
        session,
        monkeypatch,
        scene.scene_id,
        [{**_INJURY, "fact_value": "衣袖被箭划破", "evidence": "只划破了衣袖"}],
    )
    state = _drafting_state(session, world_scene(2, 3))
    assert "衣袖被箭划破" in state and "左腿中了一箭" not in state


# ------------------------------------------------------------------ 「前情」


def test_story_so_far_lists_the_earlier_chapters_and_the_latest_open_threads(session) -> None:
    _seed_book(session)
    session.add(
        _digest_row(
            _chapter(1),
            summary="林昭在档案馆找到旧信。他决定去钟楼。",
            threads=["旧信是谁寄出的"],
            ending="林昭揣着旧信出门。",
            changes=[("林昭", "拿到了旧信")],
        )
    )
    session.add(_digest_row(_chapter(2), summary="林昭在钟楼遇到守夜人。", threads=["守夜人认得寄信人"]))
    session.flush()

    memory = story_so_far(session, session.get(SceneCard, _scene(3, 1)))

    text = memory["text"]
    assert text.startswith(STORY_SO_FAR_HEADER)
    assert "第1章《旧信》：林昭在档案馆找到旧信。他决定去钟楼。" in text
    assert "章末：林昭揣着旧信出门。" in text and "人物变化：林昭——拿到了旧信" in text
    assert "第2章《钟楼》：林昭在钟楼遇到守夜人。" in text
    # 未收的线取最近一份摘要（第二章）的，不把第一章的旧清单叠上去
    assert text.endswith(f"{OPEN_THREADS_HEADER}\n- 守夜人认得寄信人（第1章起）")
    assert "- 旧信是谁寄出的" not in text
    assert memory["digest_row_ids"] == [f"digest_{_chapter(1)}", f"digest_{_chapter(2)}"]
    # 第一章的场没有前情
    assert story_so_far(session, session.get(SceneCard, _scene(1, 2))) is None


def test_story_so_far_marks_a_missing_digest_and_shortens_older_chapters(session, monkeypatch) -> None:
    _seed_book(session, chapters=4)
    session.add(_digest_row(_chapter(1), summary="林昭在档案馆找到旧信。他决定去钟楼。", threads=["旧信是谁寄出的"]))
    session.add(_digest_row(_chapter(3), summary="林昭动身去北境。", ending="马车出了城。"))
    session.flush()
    monkeypatch.setattr(story_memory_digest, "RECENT_FULL_CHAPTERS", 1)

    memory = story_so_far(session, session.get(SceneCard, _scene(4, 1)))

    text = memory["text"]
    assert "第1章《旧信》：林昭在档案馆找到旧信。\n" in text  # 更早的章只给第一句
    assert "第2章《钟楼》：（这一章的摘要还没写好）" in text
    assert "第3章《北境》：林昭动身去北境。\n章末：马车出了城。" in text
    assert memory["missing_chapter_ids"] == [_chapter(2)]
    # 最近一份（第三章）没有未收的线：清单块不出现
    assert OPEN_THREADS_HEADER not in text


def test_compressed_story_so_far_keeps_the_last_three_chapters_and_the_open_threads() -> None:
    blocks = [STORY_SO_FAR_HEADER] + [f"第{n}章：事{n}" for n in range(1, 7)] + [f"{OPEN_THREADS_HEADER}\n- 线"]
    compressed = compress_story_so_far("\n\n".join(blocks))
    assert compressed.split("\n\n") == [STORY_SO_FAR_HEADER, "第4章：事4", "第5章：事5", "第6章：事6", f"{OPEN_THREADS_HEADER}\n- 线"]


def _snapshot_with_story(story: str) -> dict:
    return {
        "scene_id": "S1",
        "chapter_id": "C1",
        "inline_digests": {"scene_card": "林昭在北境的驿站等人。", "story_so_far": story},
    }


def test_an_oversized_story_so_far_is_compressed_then_omitted_instead_of_forcing_a_split() -> None:
    chapters = [f"第{n}章：" + "林昭翻看案卷，雨一直没停。" * 120 for n in range(1, 9)]
    story = "\n\n".join([STORY_SO_FAR_HEADER, *chapters, f"{OPEN_THREADS_HEADER}\n- 旧信是谁寄出的"])
    snapshot = _snapshot_with_story(story)

    compressed = apply_context_budget(
        system_prompt="System.",
        task_prompt="Task.",
        bundle_snapshot=snapshot,
        sections=collect_prompt_sections(snapshot),
        max_input_tokens=6000,
        task_kind="hard_qc",
    )
    status = compressed["budget"]["section_status"]
    assert status["story_so_far"]["status"] == "compressed"
    assert compressed["budget"]["split_scene_recommended"] is False
    assert "旧信是谁寄出的" in compressed["user_prompt"] and "第1章：" not in compressed["user_prompt"]

    omitted = apply_context_budget(
        system_prompt="System.",
        task_prompt="Task.",
        bundle_snapshot=snapshot,
        sections=collect_prompt_sections(snapshot),
        max_input_tokens=400,
        task_kind="hard_qc",
    )
    assert omitted["budget"]["section_status"]["story_so_far"]["status"] == "omitted"
    assert omitted["budget"]["split_scene_recommended"] is False
    assert omitted["continuity_warning"] is None
    assert omitted["budget"]["section_status"]["scene_card"]["status"] == "included"


# ------------------------------------------------------------------ 章摘要的状态


def test_a_chapter_is_ready_for_a_digest_once_written_or_once_the_author_moved_on(session) -> None:
    _seed_book(session)
    _write_final(session, _scene(1, 1), "林昭在档案馆找到旧信。")
    _write_final(session, _scene(1, 2), "林昭决定去钟楼。")
    _write_final(session, _scene(2, 1), "林昭在钟楼遇到守夜人。")
    session.commit()

    states = {state.chapter.chapter_id: state for state in chapter_memory_states(session, PROJECT)}
    assert states[_chapter(1)].status == "missing" and states[_chapter(1)].auto_target
    # 第二章只写了一场、后面也没写：还没写完
    assert states[_chapter(2)].status == "not_ready" and not states[_chapter(2)].auto_target

    _write_final(session, _scene(3, 1), "林昭动身去北境。")
    session.commit()
    states = {state.chapter.chapter_id: state for state in chapter_memory_states(session, PROJECT)}
    # 作者往后写了：第二章缺的那一场不再等
    assert states[_chapter(2)].status == "missing" and states[_chapter(2)].eligible


# ------------------------------------------------------------------ 写章摘要、后台一轮


def _seed_written_first_chapter(session) -> None:
    _seed_book(session)
    _write_final(session, _scene(1, 1), "林昭在档案馆找到旧信。")
    _write_final(session, _scene(1, 2), "林昭决定去钟楼。")
    _write_final(session, _scene(2, 1), "林昭在钟楼遇到守夜人。")
    session.commit()


def test_a_pass_extracts_the_written_scenes_then_digests_the_finished_chapter(session, monkeypatch, llm_on) -> None:
    _seed_written_first_chapter(session)
    runner = ScriptedTaskRunner(
        session,
        [{"events": []}] * 3 + [_digest_reply("林昭找到旧信，决定去钟楼。", threads=["旧信是谁寄出的"])],
        call_prefix="llmcall_memory",
    )
    _install(monkeypatch, runner)

    summary = run_story_memory_pass(session_factory=lambda: _same_session(session))

    assert summary["extracted"] == [_scene(1, 1), _scene(1, 2), _scene(2, 1)]
    assert summary["digested"] == [_chapter(1)]
    assert [call["context"].node_id for call in runner.calls] == ["extraction"] * 3 + ["chapter_digest"]
    digest_prompt = json.loads(runner.calls[-1]["prompt_text"].split("\n\n", 1)[1].rsplit("\n\n", 1)[-1])
    assert digest_prompt["chapter"]["number"] == 1 and digest_prompt["chapter"]["title"] == "旧信"
    assert digest_prompt["chapter"]["text"] == "林昭在档案馆找到旧信。\n林昭决定去钟楼。"
    assert digest_prompt["previous"] == {} and digest_prompt["characters"] == ["林昭"]
    row = session.query(ChapterDigest).filter_by(chapter_id=_chapter(1), status="active").one()
    assert row.origin == "llm" and row.llm_call_id == "llmcall_memory_03"
    assert row.digest_json["open_threads"] == [{"thread": "旧信是谁寄出的", "opened_in": 1}]
    assert {state.chapter.chapter_id: state.status for state in chapter_memory_states(session, PROJECT)}[
        _chapter(1)
    ] == "fresh"
    # 下一轮没事可做：不再调模型
    again = run_story_memory_pass(session_factory=lambda: _same_session(session))
    assert (again["extracted"], again["digested"], len(runner.calls)) == ([], [], 4)


def test_the_next_chapter_carries_the_open_threads_and_an_edit_upstream_cascades(session, monkeypatch, llm_on) -> None:
    _seed_written_first_chapter(session)
    session.add(_digest_row(_chapter(1), summary="林昭找到旧信。", threads=["旧信是谁寄出的"]))
    _write_final(session, _scene(2, 2), "守夜人说他认得寄信人。")
    session.commit()
    first = session.get(ChapterDigest, f"digest_{_chapter(1)}")
    first.source_hash = chapter_memory_states(session, PROJECT)[0].source_hash
    session.commit()
    runner = ScriptedTaskRunner(
        session,
        [_digest_reply("守夜人认得寄信人。", threads=["旧信是谁寄出的", "守夜人为何隐瞒"])],
        default={"events": []},
        call_prefix="llmcall_memory",
    )
    _install(monkeypatch, runner)
    monkeypatch.setattr(story_memory, "_extraction_backlog", lambda session, project_id: ([], []))

    assert run_story_memory_pass(session_factory=lambda: _same_session(session))["digested"] == [_chapter(2)]
    prompt = json.loads(runner.calls[0]["prompt_text"].rsplit("\n\n", 1)[-1])
    assert prompt["previous"] == {
        "chapter_number": 1,
        "ending_state": "",
        "open_threads": [{"thread": "旧信是谁寄出的", "opened_in": 1}],
    }

    # 作者改了第一章的一场：第一章判旧、重写；第一章的摘要变了，第二章跟着判旧、重写（按故事顺序）
    _write_final(session, _scene(1, 2), "林昭决定先去北境。", version=2)
    session.commit()
    runner = ScriptedTaskRunner(
        session,
        [
            _digest_reply("林昭找到旧信，转去北境。", threads=["旧信是谁寄出的"]),
            _digest_reply("守夜人认得寄信人。", threads=["旧信是谁寄出的"]),
        ],
        call_prefix="llmcall_memory_cascade",
    )
    _install(monkeypatch, runner)
    assert run_story_memory_pass(session_factory=lambda: _same_session(session))["digested"] == [
        _chapter(1),
        _chapter(2),
    ]
    assert session.query(ChapterDigest).filter_by(chapter_id=_chapter(1), status="active").one().digest_json[
        "summary"
    ] == "林昭找到旧信，转去北境。"


def test_an_author_edited_digest_is_never_rewritten_automatically(session, monkeypatch, llm_on) -> None:
    _seed_written_first_chapter(session)
    edited = save_author_digest(
        session,
        PROJECT,
        _chapter(1),
        base_row_id=None,
        digest={"summary": "林昭找到旧信（作者改过）。", "open_threads": [{"thread": "寄信人", "opened_in": 1}]},
    )
    session.commit()
    assert (edited["origin"], edited["status"]) == ("author", "fresh")
    _write_final(session, _scene(1, 2), "林昭决定先去北境。", version=2)
    session.commit()

    state = chapter_memory_states(session, PROJECT)[0]
    assert state.status == "stale" and not state.auto_target
    with pytest.raises(DomainError) as stale_base:
        save_author_digest(session, PROJECT, _chapter(1), base_row_id="digest_somewhere_else", digest={"summary": "x"})
    assert (stale_base.value.status_code, stale_base.value.code) == (409, "CHAPTER_DIGEST_CHANGED")


def test_without_a_model_the_digest_fails_closed_and_the_pass_stops(session, monkeypatch) -> None:
    _seed_written_first_chapter(session)
    with pytest.raises(DomainError) as missing:
        generate_chapter_digest(session, PROJECT, _chapter(1))
    assert (missing.value.status_code, missing.value.code) == (409, "CHAPTER_DIGEST_LLM_UNAVAILABLE")

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    runner = ScriptedTaskRunner(session, [{"__fail__": "LLM_ROUTE_NOT_CONFIGURED"}], call_prefix="llmcall_memory")
    _install(monkeypatch, runner)
    monkeypatch.setattr(story_memory, "_extraction_backlog", lambda session, project_id: ([], []))
    summary = run_story_memory_pass(session_factory=lambda: _same_session(session))
    assert summary["stopped"] == "CHAPTER_DIGEST_LLM_UNAVAILABLE"
    assert session.query(ChapterDigest).count() == 0


def test_an_invalid_digest_reply_is_a_502_and_stores_nothing(session, monkeypatch, llm_on) -> None:
    _seed_written_first_chapter(session)
    runner = ScriptedTaskRunner(session, [{"summary": "", "open_threads": []}], call_prefix="llmcall_memory")
    _install(monkeypatch, runner)

    with pytest.raises(DomainError) as invalid:
        generate_chapter_digest(session, PROJECT, _chapter(1))

    assert (invalid.value.status_code, invalid.value.code) == (502, "CHAPTER_DIGEST_RESPONSE_INVALID")
    session.rollback()
    assert session.query(ChapterDigest).count() == 0
    call = session.get(LlmCall, "llmcall_memory_00")
    assert (call.accounting_status, call.error_code) == ("failed", "CHAPTER_DIGEST_RESPONSE_INVALID")


# ------------------------------------------------------------------ 接口


def test_story_memory_routes_list_and_save_the_author_digest(client, session) -> None:
    _seed_written_first_chapter(session)

    listed = client.get(f"/api/v1/projects/{PROJECT}/story-memory")
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert [(item["number"], item["title"], item["status"]) for item in data["chapters"]] == [
        (1, "旧信", "missing"),
        (2, "钟楼", "not_ready"),
        (3, "北境", "not_ready"),
    ]
    assert data["extraction"] == {"pending_scene_count": 3, "degraded_scene_count": 0}

    saved = client.post(
        f"/api/v1/projects/{PROJECT}/story-memory/chapters/{_chapter(1)}/digest",
        json={"base_row_id": None, "digest": {"summary": "林昭找到旧信。", "open_threads": [{"thread": "寄信人"}]}},
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["data"]["origin"], saved.json()["data"]["status"]) == ("author", "fresh")

    conflict = client.post(
        f"/api/v1/projects/{PROJECT}/story-memory/chapters/{_chapter(1)}/digest",
        json={"base_row_id": None, "digest": {"summary": "又改了一次。"}},
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "CHAPTER_DIGEST_CHANGED"

    regenerate = client.post(f"/api/v1/projects/{PROJECT}/story-memory/chapters/{_chapter(1)}/digest/regenerate", json={})
    assert regenerate.status_code == 409 and regenerate.json()["error"]["code"] == "CHAPTER_DIGEST_LLM_UNAVAILABLE"


def test_the_worker_is_registered_for_the_app_lifetime_only() -> None:
    """lifespan 启动时登记维护任务、结束时撤掉——不跨用例（以前留在全局登记簿里，后面别的用例的巡检会跑它）。"""
    from novel_system.api.app import create_app
    from novel_system.services.maintenance import SYSTEM_MAINTENANCE
    from tests.support.api_client import AutoKeyTestClient

    assert story_memory.STORY_MEMORY_TASK not in SYSTEM_MAINTENANCE.tasks
    with AutoKeyTestClient(create_app()):
        assert story_memory.STORY_MEMORY_TASK in SYSTEM_MAINTENANCE.tasks
    assert story_memory.STORY_MEMORY_TASK not in SYSTEM_MAINTENANCE.tasks

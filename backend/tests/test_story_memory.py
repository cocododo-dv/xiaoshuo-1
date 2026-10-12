"""故事记忆（2026-10-12，``services/story_memory.py`` / ``story_memory_digest.py``）。

起草后面的场次时，远处的事实只从两处来：事件日志的工作记忆（各场当前终稿上抽出来、还没核对完的事实也算，作者驳回
的不算；硬质检与成稿门照旧只认正史）与章摘要的「前情」（取代卷汇总的原文拼接）。这里钉住：

- 工作记忆：抽出来的事实进起草的人物状态、不进正史；驳回即消失；换了一版终稿、新抽取还没跑时不拿旧版的候选；
- 「前情」：此前各章的摘要、没写好的章占位、最近几章全文 / 更早一句、未收的线来自线索台账；预算紧时先压后删，
  不再因为远处的记忆判「建议拆场」；
- 线索台账：各章摘要的线索更新按故事顺序叠出来（编号换回稳定 id、新线按章编号、计划兑现的章只认输入里给的）、
  冷了 / 计划兑现的章已过的提醒、作者的标记（放下 / 已收，文字对不上就失效）；
- 章摘要的状态（写完没写完、旧没旧）、后台一轮（先补抽、再按故事顺序写，下一章拿到台账里还开着的线与后面几章的
  计划；正文一变、后面的章跟着判旧；作者改过的不自动重写、改字时线索原样带过来）、失败路径（没模型 409 且后台停下、
  输出不合格 502 且什么也不存）；
- 成稿中心的接口：列表（带台账与编辑通读）、作者改摘要（编辑期间换了一份 → 409）、标记一条线。
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
    mark_thread,
    resolve_thread_updates,
    run_story_memory_pass,
    save_author_digest,
    thread_ledger_overview,
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
    titles = ["旧信", "钟楼", "北境", "案卷", "雨夜", "渡口"]
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


def _update(thread_id: str, thread: str, status: str = "opened", *, note: str = "", payoff: str | None = None) -> dict:
    """存进摘要的一条线索更新（id 已定好）。"""
    return {"id": thread_id, "thread": thread, "status": status, "note": note, "payoff_chapter_id": payoff}


def _digest_row(chapter_id: str, *, summary: str, threads=(), ending: str = "", changes=()) -> ChapterDigest:
    """``threads``：字符串 = 这一章新埋下的线（按 ``<章 id>-T<n>`` 编号），字典 = 现成的线索更新。"""
    updates = [
        _update(f"{chapter_id}-T{index}", item) if isinstance(item, str) else item
        for index, item in enumerate(threads, start=1)
    ]
    return ChapterDigest(
        row_id=f"digest_{chapter_id}",
        project_id=PROJECT,
        chapter_id=chapter_id,
        digest_json={
            "summary": summary,
            "ending_state": ending,
            "character_changes": [{"character": name, "change": change} for name, change in changes],
            "threads": updates,
        },
        content_hash=f"hash_{chapter_id}",
        source_hash="seeded",
    )


def _new(thread: str, *, payoff: int = 0, note: str = "") -> dict:
    """模型回复里新埋下的一条线。"""
    return {"ref": "new", "thread": thread, "status": "opened", "note": note, "payoff_chapter": payoff}


def _touch(ref: str, status: str = "advanced", *, note: str = "", payoff: int = 0) -> dict:
    """模型回复里推进 / 兑现台账里的一条线。"""
    return {"ref": ref, "thread": "", "status": status, "note": note, "payoff_chapter": payoff}


def _digest_reply(summary: str, *, threads=(), ending: str = "雨还在下。") -> dict:
    return {
        "summary": summary,
        "ending_state": ending,
        "character_changes": [{"character": "林昭", "change": "收下了旧信"}],
        "threads": [_new(item) if isinstance(item, str) else item for item in threads],
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
    monkeypatch.setattr(story_memory, "_REVIEW_ATTEMPTS", {})


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


def test_story_so_far_lists_the_earlier_chapters_and_the_threads_still_open(session) -> None:
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
    session.add(
        _digest_row(
            _chapter(2),
            summary="林昭在钟楼遇到守夜人。",
            threads=[
                _update(f"{_chapter(1)}-T1", "旧信是谁寄出的", "resolved", note="守夜人说出了寄信人"),
                _update(f"{_chapter(2)}-T1", "守夜人为何隐瞒"),
            ],
        )
    )
    session.flush()

    memory = story_so_far(session, session.get(SceneCard, _scene(3, 1)))

    text = memory["text"]
    assert text.startswith(STORY_SO_FAR_HEADER)
    assert "第1章《旧信》：林昭在档案馆找到旧信。他决定去钟楼。" in text
    assert "章末：林昭揣着旧信出门。" in text and "人物变化：林昭——拿到了旧信" in text
    assert "第2章《钟楼》：林昭在钟楼遇到守夜人。" in text
    # 未收的线来自台账：第一章埋下、第二章兑现的那条不再列
    assert text.endswith(f"{OPEN_THREADS_HEADER}\n- 守夜人为何隐瞒（第2章埋下）")
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
    # 第一章埋下的线第三章没碰，台账里还开着（才两章没碰，不算冷）
    assert text.endswith(f"{OPEN_THREADS_HEADER}\n- 旧信是谁寄出的（第1章埋下）")


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
    assert digest_prompt["threads_so_far"] == []
    # 后面几章的计划（章目标 + 每场一行），让模型给线索标「计划在哪章兑现」
    assert [(item["chapter_number"], item["title"], item["goal"]) for item in digest_prompt["upcoming_plan"]] == [
        (2, "钟楼", "第 2 章"),
        (3, "北境", "第 3 章"),
    ]
    assert digest_prompt["upcoming_plan"][0]["scenes"] == ["第 2 章第 1 场", "第 2 章第 2 场"]
    row = session.query(ChapterDigest).filter_by(chapter_id=_chapter(1), status="active").one()
    assert row.origin == "llm" and row.llm_call_id == "llmcall_memory_03"
    assert row.digest_json["threads"] == [_update(f"{_chapter(1)}-T1", "旧信是谁寄出的")]
    assert {state.chapter.chapter_id: state.status for state in chapter_memory_states(session, PROJECT)}[
        _chapter(1)
    ] == "fresh"
    # 下一轮没事可做：不再调模型
    again = run_story_memory_pass(session_factory=lambda: _same_session(session))
    assert (again["extracted"], again["digested"], len(runner.calls)) == ([], [], 4)


def test_a_failed_extraction_is_reported_as_a_failure_not_as_extracted(session, monkeypatch, llm_on) -> None:
    """抽取失败（供应商报错）不抛错、只把场记成 degraded：一轮的汇总照实记进 failed。"""
    _seed_book(session)
    _write_final(session, _scene(1, 1), "林昭在档案馆找到旧信。")
    session.commit()
    runner = ScriptedTaskRunner(session, [{"__fail__": "LLM_UPSTREAM_ERROR"}], call_prefix="llmcall_memory")
    _install(monkeypatch, runner)

    summary = run_story_memory_pass(session_factory=lambda: _same_session(session))

    assert summary["extracted"] == []
    assert summary["failed"] == [{"scene_id": _scene(1, 1), "code": "EXTRACTION_DEGRADED"}]
    assert session.get(SceneRunState, _scene(1, 1)).narrative_sync_status == "degraded"


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
        [_digest_reply("守夜人认得寄信人。", threads=[_touch("T1", note="守夜人认得寄信人", payoff=3), "守夜人为何隐瞒"])],
        default={"events": []},
        call_prefix="llmcall_memory",
    )
    _install(monkeypatch, runner)
    monkeypatch.setattr(story_memory, "_extraction_backlog", lambda session, project_id: ([], []))

    assert run_story_memory_pass(session_factory=lambda: _same_session(session))["digested"] == [_chapter(2)]
    prompt = json.loads(runner.calls[0]["prompt_text"].rsplit("\n\n", 1)[-1])
    assert prompt["previous"] == {"chapter_number": 1, "ending_state": ""}
    assert prompt["threads_so_far"] == [{"ref": "T1", "thread": "旧信是谁寄出的", "opened_in": 1, "planned_payoff_in": None}]
    # 编号换回稳定 id；推进的那条留它原来的文字；计划兑现的章只认输入里给过的后面几章
    assert session.query(ChapterDigest).filter_by(chapter_id=_chapter(2), status="active").one().digest_json["threads"] == [
        _update(f"{_chapter(1)}-T1", "旧信是谁寄出的", "advanced", note="守夜人认得寄信人", payoff=_chapter(3)),
        _update(f"{_chapter(2)}-T1", "守夜人为何隐瞒"),
    ]
    ledger = thread_ledger_overview(session, PROJECT)
    assert [(row["thread"], row["opened_in"], row["last_touched_in"], row["planned_payoff_in"]) for row in ledger["open"]] == [
        ("旧信是谁寄出的", 1, 2, 3),
        ("守夜人为何隐瞒", 2, 2, None),
    ]

    # 作者改了第一章的一场：第一章判旧、重写；第一章的摘要变了，第二章跟着判旧、重写（按故事顺序）
    _write_final(session, _scene(1, 2), "林昭决定先去北境。", version=2)
    session.commit()
    runner = ScriptedTaskRunner(
        session,
        [
            _digest_reply("林昭找到旧信，转去北境。", threads=["旧信是谁寄出的"]),
            _digest_reply("守夜人认得寄信人。", threads=[_touch("T1")]),
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
        digest={"summary": "林昭找到旧信（作者改过）。"},
    )
    session.commit()
    assert (edited["origin"], edited["status"]) == ("author", "fresh")
    assert edited["digest"]["threads"] == []
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
    runner = ScriptedTaskRunner(session, [{"summary": "", "threads": [_new("寄信人")]}], call_prefix="llmcall_memory")
    _install(monkeypatch, runner)

    with pytest.raises(DomainError) as invalid:
        generate_chapter_digest(session, PROJECT, _chapter(1))

    assert (invalid.value.status_code, invalid.value.code) == (502, "CHAPTER_DIGEST_RESPONSE_INVALID")
    session.rollback()
    assert session.query(ChapterDigest).count() == 0
    call = session.get(LlmCall, "llmcall_memory_00")
    assert (call.accounting_status, call.error_code) == ("failed", "CHAPTER_DIGEST_RESPONSE_INVALID")


# ------------------------------------------------------------------ 线索台账


def test_model_thread_updates_become_stable_ids_and_unplaceable_ones_are_dropped() -> None:
    rows = [{"ref": "T1", "thread_id": "C1-T1", "thread": "旧信是谁寄出的", "opened_in": 1, "planned_payoff_in": None}]
    updates = resolve_thread_updates(
        [
            _touch("t1", "resolved", note="说出了寄信人"),
            _touch("T1"),  # 同一条线说了两次：只认第一次
            {"ref": "T9", "thread": "钟楼的钥匙", "status": "advanced", "note": "", "payoff_chapter": 4},  # 认不出的编号 = 新线
            {"ref": "new", "thread": "", "status": "opened", "note": "", "payoff_chapter": 0},  # 没有文字
            {"ref": "new", "thread": "当场埋下当场收掉", "status": "resolved", "note": "", "payoff_chapter": 0},
            _new("守夜人为何隐瞒", payoff=7),  # 第 7 章不在输入给的计划里
            "不是对象",
        ],
        chapter_id="C2",
        thread_rows=rows,
        plan_numbers={4: "C4"},
    )
    assert updates == [
        _update("C1-T1", "旧信是谁寄出的", "resolved", note="说出了寄信人"),
        _update("C2-T1", "钟楼的钥匙", payoff="C4"),
        _update("C2-T2", "守夜人为何隐瞒"),
    ]


def test_a_resolved_thread_that_comes_back_is_open_again() -> None:
    threads = story_memory_digest.fold_threads(
        [
            (1, "C1", {"threads": [_update("C1-T1", "旧信是谁寄出的")]}),
            (2, "C2", {"threads": [_update("C1-T1", "旧信是谁寄出的", "resolved"), _update("C9-T1", "认不出的兑现", "resolved")]}),
            (3, "C3", {"threads": [_update("C1-T1", "旧信是谁寄出的", "advanced", note="寄信人另有其人")]}),
        ]
    )
    assert list(threads) == ["C1-T1"]
    thread = threads["C1-T1"]
    assert (thread.status, thread.opened_number, thread.last_number, thread.resolved_number) == ("open", 1, 3, None)
    assert [item["status"] for item in thread.history] == ["opened", "resolved", "advanced"]


def _seed_ledger_book(session) -> None:
    """六章的书，前五章有摘要：第一章埋下三条线（钥匙计划第三章兑现），第二章推进守夜人那条，之后没人碰。"""
    _seed_book(session, chapters=6)
    session.add(
        _digest_row(
            _chapter(1),
            summary="林昭找到旧信与一把钥匙。",
            threads=[
                _update(f"{_chapter(1)}-T1", "旧信是谁寄出的"),
                _update(f"{_chapter(1)}-T2", "钟楼的钥匙", payoff=_chapter(3)),
                _update(f"{_chapter(1)}-T3", "守夜人为何隐瞒"),
            ],
        )
    )
    session.add(_digest_row(_chapter(2), summary="守夜人支开了林昭。", threads=[_update(f"{_chapter(1)}-T3", "守夜人为何隐瞒", "advanced")]))
    for index in (3, 4, 5):
        session.add(_digest_row(_chapter(index), summary=f"第 {index} 章的事。"))
    session.flush()


def test_the_ledger_flags_cold_threads_and_missed_payoffs(session) -> None:
    _seed_ledger_book(session)

    ledger = thread_ledger_overview(session, PROJECT)

    assert ledger["current_number"] == 5 and ledger["alert_count"] == 2
    assert [(row["thread"], row["alert"], row["dormant_chapters"]) for row in ledger["open"]] == [
        ("旧信是谁寄出的", "cold", 4),
        ("钟楼的钥匙", "payoff_missed", 4),
        ("守夜人为何隐瞒", None, 3),
    ]
    text = story_so_far(session, session.get(SceneCard, _scene(6, 1)))["text"]
    assert text.endswith(
        "\n".join(
            [
                OPEN_THREADS_HEADER,
                "- 旧信是谁寄出的（第1章埋下）［已经 4 章没碰］",
                "- 钟楼的钥匙（第1章埋下）［计划第3章兑现，还没兑现］",
                "- 守夜人为何隐瞒（第1章埋下；第2章最近推进）",
            ]
        )
    )
    # 写第三章时，钥匙那条还在计划之中：不提醒，告诉模型它计划在哪章兑现
    early = story_so_far(session, session.get(SceneCard, _scene(3, 1)))["text"]
    assert "- 钟楼的钥匙（第1章埋下；计划第3章兑现）" in early and "［" not in early


def test_author_marks_hide_a_thread_until_its_text_changes(session) -> None:
    _seed_ledger_book(session)

    mark_thread(session, PROJECT, f"{_chapter(1)}-T1", status="dropped", note="故意留白")
    ledger = mark_thread(session, PROJECT, f"{_chapter(1)}-T2", status="resolved")

    assert [row["thread"] for row in ledger["open"]] == ["守夜人为何隐瞒"]
    assert {(row["thread"], row["mark"]) for row in ledger["closed"]} == {("旧信是谁寄出的", "dropped"), ("钟楼的钥匙", "resolved")}
    text = story_so_far(session, session.get(SceneCard, _scene(6, 1)))["text"]
    assert "旧信是谁寄出的" not in text and "钟楼的钥匙" not in text

    # 第一章的摘要重写后，同一个编号换成了另一条线：旧标记不再生效
    first = session.get(ChapterDigest, f"digest_{_chapter(1)}")
    first.digest_json = {**first.digest_json, "threads": [_update(f"{_chapter(1)}-T1", "寄信人的笔迹")]}
    session.flush()
    assert [row["thread"] for row in thread_ledger_overview(session, PROJECT)["open"]][0] == "寄信人的笔迹"

    reopened = mark_thread(session, PROJECT, f"{_chapter(1)}-T1", status="open")
    assert all(row["mark"] is None for row in reopened["open"])
    with pytest.raises(DomainError) as missing:
        mark_thread(session, PROJECT, "nope-T1", status="dropped")
    assert (missing.value.status_code, missing.value.code) == (404, "STORY_THREAD_NOT_FOUND")


def test_a_change_to_the_ledger_makes_later_digests_stale_even_when_the_previous_one_holds(session) -> None:
    _seed_book(session)
    for index in (1, 2, 3):
        for seq in (1, 2):
            _write_final(session, _scene(index, seq), f"第 {index} 章第 {seq} 场的正文。")
    session.add(_digest_row(_chapter(1), summary="林昭找到旧信。", threads=["旧信是谁寄出的"]))
    session.add(_digest_row(_chapter(2), summary="林昭去钟楼。"))
    session.add(_digest_row(_chapter(3), summary="林昭去北境。"))
    session.flush()
    for state in chapter_memory_states(session, PROJECT):
        state.digest.source_hash = state.source_hash
    session.flush()
    assert {state.status for state in chapter_memory_states(session, PROJECT)} == {"fresh"}

    # 第一章那条线换了文字：第二章（上一份变了）与第三章（输入的台账变了）都旧了
    first = session.get(ChapterDigest, f"digest_{_chapter(1)}")
    first.digest_json = {**first.digest_json, "threads": [_update(f"{_chapter(1)}-T1", "寄信人的笔迹")]}
    session.flush()
    second = session.get(ChapterDigest, f"digest_{_chapter(2)}")
    second.source_hash = chapter_memory_states(session, PROJECT)[1].source_hash  # 假装第二章已经按新输入重写、内容不变
    session.flush()
    assert [state.status for state in chapter_memory_states(session, PROJECT)] == ["fresh", "fresh", "stale"]


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
    assert data["threads"] == {"current_number": 0, "open": [], "closed": [], "alert_count": 0}
    assert data["editorial"]["latest"] is None and data["editorial"]["can_run"] is False
    # 线索不在这里改：旧的两栏已经不收
    legacy = client.post(
        f"/api/v1/projects/{PROJECT}/story-memory/chapters/{_chapter(1)}/digest",
        json={"base_row_id": None, "digest": {"summary": "林昭找到旧信。", "open_threads": [{"thread": "寄信人"}]}},
    )
    assert legacy.status_code == 422

    saved = client.post(
        f"/api/v1/projects/{PROJECT}/story-memory/chapters/{_chapter(1)}/digest",
        json={"base_row_id": None, "digest": {"summary": "林昭找到旧信。", "character_changes": [{"character": "林昭", "change": "拿到旧信"}]}},
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


def test_the_mark_route_marks_and_reopens_a_thread(client, session) -> None:
    _seed_ledger_book(session)
    session.commit()
    path = f"/api/v1/projects/{PROJECT}/story-memory/threads/{_chapter(1)}-T1/mark"

    dropped = client.post(path, json={"status": "dropped", "note": "故意留白"})
    assert dropped.status_code == 200, dropped.text
    closed = dropped.json()["data"]["closed"]
    assert [(row["thread"], row["mark"], row["mark_note"]) for row in closed] == [("旧信是谁寄出的", "dropped", "故意留白")]
    assert client.post(path, json={"status": "maybe"}).status_code == 422
    missing = client.post(f"/api/v1/projects/{PROJECT}/story-memory/threads/nope-T1/mark", json={"status": "dropped"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "STORY_THREAD_NOT_FOUND"
    reopened = client.post(path, json={"status": "open"})
    assert reopened.status_code == 200 and reopened.json()["data"]["closed"] == []


def test_the_worker_is_registered_for_the_app_lifetime_only() -> None:
    """lifespan 启动时登记维护任务、结束时撤掉——不跨用例（以前留在全局登记簿里，后面别的用例的巡检会跑它）。"""
    from novel_system.api.app import create_app
    from novel_system.services.maintenance import SYSTEM_MAINTENANCE
    from tests.support.api_client import AutoKeyTestClient

    assert story_memory.STORY_MEMORY_TASK not in SYSTEM_MAINTENANCE.tasks
    with AutoKeyTestClient(create_app()):
        assert story_memory.STORY_MEMORY_TASK in SYSTEM_MAINTENANCE.tasks
    assert story_memory.STORY_MEMORY_TASK not in SYSTEM_MAINTENANCE.tasks

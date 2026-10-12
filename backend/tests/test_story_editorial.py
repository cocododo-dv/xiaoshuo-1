"""编辑通读（2026-10-12，``services/story_editorial.py`` / ``editorial_guidance.py``）。

钉住：

- 什么时候读：章摘要都跟上之后、读得到的最后一章比上一份往后多了三章、后面还有章；读过的同一份输入不再读；
- 读什么：全书设计（一句话、五句脊柱、道德前提、长篇大纲）、人物的目标 / 顿悟 / 人物弧 / 恐惧与各章记下的变化、
  各章摘要、台账里还开着的线、最近几章正文、后面几章的场景结构、上一次采纳过的意见；
- 存什么：总评与问题清单（类型 / 轻重 / 章号收口）、意见（章号只认后面几章，别的算整段）；新的一份作废旧份里没决定的；
- 作者的决定：采纳（可改字）的意见进那一章各场的起草 bundle 与场景蓝图、不进硬质检，改动让那几章没写的场的蓝图作废；
  不采纳 / 撤销即消失；作废的意见不能再决定；
- 失败路径：没有摘要 409、没模型 409、输出不合格 502 且什么也不存；后台工人在摘要跟上之后顺带读一次。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import pytest

from novel_system.db.models import (
    ChapterDigest,
    ChapterGoal,
    EditorialGuidance,
    EditorialReview,
    FinalScene,
    LlmCall,
    SceneCard,
    SceneRunState,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
)
from novel_system.services import story_editorial, story_memory
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.editorial_guidance import EDITORIAL_GUIDANCE_SECTION_KEY, EDITORIAL_GUIDANCE_SECTION_LABEL
from novel_system.services.errors import DomainError
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.story_editorial import decide_guidance, editorial_review_due, generate_editorial_review
from novel_system.services.story_memory import chapter_memory_states, run_story_memory_pass, story_memory_overview
from tests.support.llm_fakes import ScriptedTaskRunner

PROJECT = "PRJ_EDITOR"
LZ = f"{PROJECT}_LZ"
TITLES = ["旧信", "钟楼", "北境", "渡口", "雨夜"]


def _chapter(index: int) -> str:
    return f"{PROJECT}_CH{index:02d}"


def _scene(chapter: int, seq: int) -> str:
    return f"{PROJECT}_CH{chapter:02d}_SC{seq:02d}"


def _seed_book(session, *, chapters: int = 4) -> None:
    session.add(StoryProject(project_id=PROJECT, title="雨城旧信", outline_text=""))
    session.flush()
    for index in range(1, chapters + 1):
        session.add(
            ChapterGoal(
                chapter_id=_chapter(index),
                project_id=PROJECT,
                chapter_goal=f"第 {index} 章的目标",
                planned_scene_count=1,
                display_order=index,
                narrative_json={"title": TITLES[index - 1]},
            )
        )
    session.flush()
    for index in range(1, chapters + 1):
        brief = (
            {"scene_form": "proactive", "goal": f"林昭去{TITLES[index - 1]}找寄信人", "conflict": "路被封了", "setback": "寄信人已经走了"}
            if index >= 4
            else {}
        )
        session.add(
            SceneCard(
                scene_id=_scene(index, 1),
                chapter_id=_chapter(index),
                project_id=PROJECT,
                scene_seq=1,
                scene_goal=f"第 {index} 章的场",
                pov_character_id=LZ,
                onstage_chars_json=[LZ],
                writer_brief_json=brief,
            )
        )
    for index in range(4, chapters + 1):  # 还没写的章：场有运行状态、没有终稿
        session.add(SceneRunState(scene_id=_scene(index, 1), scene_status="ready"))
    session.add(StoryCharacter(character_id=LZ, project_id=PROJECT, display_name="林昭", status="active"))
    canon = {
        "one_sentence_summary": {"summary": "林昭必须找出寄旧信的人，但每个知情人都在躲他。"},
        "one_paragraph_summary": {"sentences": ["林昭收到旧信。", "他追到钟楼。"], "moral_premise": "只有开口求人，才能找到真相。"},
        "long_synopsis": {"paragraphs": ["第一幕：旧信。", "第二幕：钟楼与北境。"]},
        "character_sheets": {
            "characters": [{"character_id": LZ, "display_name": "林昭", "role": "主角", "goal": "找出寄信人", "epiphany": "求人不是软弱"}]
        },
        "character_bibles": {
            "characters": [
                {
                    "character_id": LZ,
                    "display_name": "林昭",
                    "psychological_profile": {"character_arc": "从独来独往到学会求人", "deepest_fear": "被人看穿", "greatest_hope": "被信任"},
                }
            ]
        },
    }
    for step_key, draft in canon.items():
        session.add(
            SnowflakeStepRun(
                step_run_id=f"run_{step_key}",
                project_id=PROJECT,
                step_key=step_key,
                version=1,
                status="approved",
                draft_json=draft,
                approved_at="2026-10-12T00:00:00Z",
            )
        )
    session.flush()


def _write_final(session, scene_id: str, content: str) -> None:
    scene = session.get(SceneCard, scene_id)
    final = FinalScene(
        row_id=f"final_{scene_id}_v1",
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
    session.add(SceneRunState(scene_id=scene_id, scene_status="archived", current_final_scene_row_id=final.row_id))
    session.flush()


def _seed_digest(session, index: int, *, threads=()) -> None:
    session.add(
        ChapterDigest(
            row_id=f"digest_{_chapter(index)}",
            project_id=PROJECT,
            chapter_id=_chapter(index),
            digest_json={
                "summary": f"第 {index} 章：林昭又一次独自行动。",
                "ending_state": f"第 {index} 章末，林昭一个人。",
                "character_changes": [{"character": "林昭", "change": f"第 {index} 章仍不肯求人"}],
                "threads": list(threads),
            },
            content_hash=f"hash_{index}",
            source_hash="seeded",
        )
    )
    session.flush()


def _freshen(session) -> None:
    for state in chapter_memory_states(session, PROJECT):
        if state.digest is not None:
            state.digest.source_hash = state.source_hash
    session.flush()


def _seed_written(session, *, digested: int = 3) -> None:
    _seed_book(session)
    for index in (1, 2, 3):
        _write_final(session, _scene(index, 1), f"第 {index} 章的正文：林昭一个人走了很远。")
    thread = {"id": f"{_chapter(1)}-T1", "thread": "旧信是谁寄出的", "status": "opened", "note": "", "payoff_chapter_id": None}
    for index in range(1, digested + 1):
        _seed_digest(session, index, threads=[thread] if index == 1 else [])
    _freshen(session)
    session.commit()


def _reply(**overrides) -> dict:
    reply = {
        "assessment": "前三章林昭一直独来独往，人物弧没动。",
        "findings": [
            {"kind": "arc_stall", "severity": "high", "chapters": [1, 2, 99], "finding": "林昭三章都没求过人", "suggestion": "下一章让他不得不开口"},
            {"kind": "weird", "severity": "loud", "chapters": [], "finding": "三章都以独自离开收尾", "suggestion": ""},
            {"kind": "pacing", "severity": "low", "chapters": [2], "finding": "", "suggestion": "没有问题的问题不收"},
        ],
        "guidance": [
            {"chapter": 4, "guidance": "让林昭在渡口第一次开口求人", "reason": "人物弧停滞"},
            {"chapter": 0, "guidance": "换一种收尾方式，不再独自离开", "reason": "收尾重复"},
            {"chapter": 2, "guidance": "写过的章给的意见算整段", "reason": ""},
            {"chapter": 4, "guidance": "", "reason": "空的不收"},
        ],
    }
    reply.update(overrides)
    return reply


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")


@pytest.fixture(autouse=True)
def _fresh_attempt_counters(monkeypatch):
    monkeypatch.setattr(story_memory, "_EXTRACTION_ATTEMPTS", {})
    monkeypatch.setattr(story_memory, "_DIGEST_ATTEMPTS", {})
    monkeypatch.setattr(story_memory, "_REVIEW_ATTEMPTS", {})
    monkeypatch.setattr(story_memory, "_PROVIDER_BACKOFF", {"delay": 0.0, "until": 0.0})


def _install(monkeypatch, runner) -> None:
    monkeypatch.setattr(story_editorial, "LLMNodeRunner", lambda session: runner)
    monkeypatch.setattr(story_memory, "LLMNodeRunner", lambda session: runner)


def _review(session, monkeypatch, reply: dict | None = None, *, prefix: str = "llmcall_editor") -> tuple[dict, ScriptedTaskRunner]:
    runner = ScriptedTaskRunner(session, [reply or _reply()], call_prefix=prefix)
    _install(monkeypatch, runner)
    result = generate_editorial_review(session, PROJECT)
    session.commit()
    return result, runner


def _prompt(runner: ScriptedTaskRunner, index: int = 0) -> dict:
    return json.loads(runner.calls[index]["prompt_text"].rsplit("\n\n", 1)[-1])


# ------------------------------------------------------------------ 什么时候读


def test_a_review_is_due_after_three_digested_chapters_with_more_to_come(session, monkeypatch, llm_on) -> None:
    _seed_written(session, digested=2)
    # 第三章写完了但摘要还欠着：读不到第三章，才两章，不读
    assert editorial_review_due(session, PROJECT, chapter_memory_states(session, PROJECT)) is None

    _seed_digest(session, 3)
    _freshen(session)
    scope = editorial_review_due(session, PROJECT, chapter_memory_states(session, PROJECT))
    assert scope is not None and (scope.through_index, scope.window_indexes) == (2, [3])

    _review(session, monkeypatch)
    # 刚读过：同一处不再读
    assert editorial_review_due(session, PROJECT, chapter_memory_states(session, PROJECT)) is None


# ------------------------------------------------------------------ 读什么、存什么


def test_a_review_reads_the_design_the_memory_and_the_plan_and_stores_its_notes(session, monkeypatch, llm_on) -> None:
    _seed_written(session)

    result, runner = _review(session, monkeypatch)

    prompt = _prompt(runner)
    assert prompt["book"] == {
        "logline": "林昭必须找出寄旧信的人，但每个知情人都在躲他。",
        "spine": ["林昭收到旧信。", "他追到钟楼。"],
        "moral_premise": "只有开口求人，才能找到真相。",
        "long_synopsis": "第一幕：旧信。 第二幕：钟楼与北境。",
    }
    character = prompt["characters"][0]
    assert (character["name"], character["goal"], character["epiphany"], character["arc"], character["deepest_fear"]) == (
        "林昭", "找出寄信人", "求人不是软弱", "从独来独往到学会求人", "被人看穿",
    )
    assert character["changes_so_far"] == ["第1章：第 1 章仍不肯求人", "第2章：第 2 章仍不肯求人", "第3章：第 3 章仍不肯求人"]
    assert [item["chapter_number"] for item in prompt["story_so_far"]] == [1, 2, 3]
    assert prompt["open_threads"] == [
        {"thread": "旧信是谁寄出的", "opened_in": 1, "last_touched_in": 1, "planned_payoff_in": None, "alert": ""}
    ]
    assert [item["chapter_number"] for item in prompt["recent_chapters"]] == [1, 2, 3]
    assert prompt["recent_chapters"][-1]["text"] == "第 3 章的正文：林昭一个人走了很远。"
    upcoming = prompt["upcoming"]
    assert [(item["chapter_number"], item["title"], item["goal"]) for item in upcoming] == [(4, "渡口", "第 4 章的目标")]
    assert "林昭去渡口找寄信人" in upcoming[0]["scenes"][0] and "寄信人已经走了" in upcoming[0]["scenes"][0]
    assert prompt["previous_guidance"] == [] and prompt["reviewed_through_chapter"] == 3

    review = session.query(EditorialReview).one()
    assert (review.status, review.origin, review.through_chapter_id, review.window_chapter_ids_json) == (
        "active", "manual", _chapter(3), [_chapter(4)],
    )
    call = session.get(LlmCall, review.llm_call_id)
    assert (call.node_id, call.scope_type, call.project_id) == ("editorial_review", "project", PROJECT)
    # 问题清单收口：越界章号丢掉、认不得的类型 / 轻重归位、没写问题的不收
    assert [(item["kind"], item["severity"], item["chapters"]) for item in result["findings"]] == [
        ("arc_stall", "high", [1, 2]),
        ("other", "medium", []),
    ]
    # 意见：章号只认后面几章（第 4 章），0 与写过的章都算整段
    assert [(item["chapter_number"], item["text"], item["status"]) for item in result["guidance"]] == [
        (4, "让林昭在渡口第一次开口求人", "proposed"),
        (None, "换一种收尾方式，不再独自离开", "proposed"),
        (None, "写过的章给的意见算整段", "proposed"),
    ]
    assert result["window"] == [{"chapter_id": _chapter(4), "number": 4, "title": "渡口"}]


# ------------------------------------------------------------------ 作者的决定 → 起草


def _bundle(session, scene_id: str) -> dict:
    return BundleBuilder(session).build(scene_id)["snapshot"]


def test_accepted_guidance_reaches_drafting_and_the_blueprint_but_not_hard_qc(session, monkeypatch, llm_on) -> None:
    _seed_written(session)
    result, _runner = _review(session, monkeypatch)
    superseded: list[list[str]] = []
    monkeypatch.setattr(
        story_editorial,
        "supersede_scene_planning_artifacts",
        lambda session, *, scene_ids, reason: superseded.append(list(scene_ids)) or {"scene_blueprints": len(scene_ids)},
    )
    first, whole_window = result["guidance"][0]["guidance_id"], result["guidance"][1]["guidance_id"]
    assert EDITORIAL_GUIDANCE_SECTION_KEY not in _bundle(session, _scene(4, 1))["inline_digests"]

    decided = decide_guidance(session, PROJECT, first, decision="accept", text="让林昭在渡口第一次开口求人，求的是他最不信的人")
    decide_guidance(session, PROJECT, whole_window, decision="accept")
    session.commit()

    assert decided["guidance"]["author_text"] == "让林昭在渡口第一次开口求人，求的是他最不信的人"
    assert superseded == [[_scene(4, 1)], [_scene(4, 1)]]  # 第四章还没写的场的蓝图作废
    snapshot = _bundle(session, _scene(4, 1))
    assert snapshot["inline_digests"][EDITORIAL_GUIDANCE_SECTION_KEY] == (
        "- 让林昭在渡口第一次开口求人，求的是他最不信的人\n- 换一种收尾方式，不再独自离开"
    )
    assert snapshot["source_version_refs"][EDITORIAL_GUIDANCE_SECTION_KEY] == [first, whole_window]
    assert f"## {EDITORIAL_GUIDANCE_SECTION_LABEL}" in PromptBuilder().build(snapshot, "neutral_draft")["user_prompt"]
    assert f"## {EDITORIAL_GUIDANCE_SECTION_LABEL}" not in PromptBuilder().build(snapshot, "hard_qc")["user_prompt"]
    blueprint = SceneBlueprintService(session)._source_snapshot(
        session.get(SceneCard, _scene(4, 1)), session.get(ChapterGoal, _chapter(4))
    )["snapshot"]
    assert blueprint["inline_digests"][EDITORIAL_GUIDANCE_SECTION_KEY] == snapshot["inline_digests"][EDITORIAL_GUIDANCE_SECTION_KEY]
    # 不在 window 里的章不受影响
    assert EDITORIAL_GUIDANCE_SECTION_KEY not in _bundle(session, _scene(3, 1))["inline_digests"]

    decide_guidance(session, PROJECT, first, decision="dismiss")
    session.commit()
    assert _bundle(session, _scene(4, 1))["inline_digests"][EDITORIAL_GUIDANCE_SECTION_KEY] == "- 换一种收尾方式，不再独自离开"
    assert len(superseded) == 3


def test_a_new_review_supersedes_the_open_proposals_and_keeps_the_accepted_ones(session, monkeypatch, llm_on) -> None:
    _seed_written(session)
    first, _runner = _review(session, monkeypatch)
    kept = first["guidance"][0]["guidance_id"]
    decide_guidance(session, PROJECT, kept, decision="accept")
    session.commit()

    second, runner = _review(
        session,
        monkeypatch,
        _reply(guidance=[{"chapter": 4, "guidance": "渡口那场让守夜人回来", "reason": "线索"}]),
        prefix="llmcall_editor_again",
    )

    assert _prompt(runner)["previous_guidance"] == [{"chapter_number": 4, "guidance": "让林昭在渡口第一次开口求人"}]
    statuses = {row.text: row.status for row in session.query(EditorialGuidance).all()}
    assert statuses == {
        "让林昭在渡口第一次开口求人": "accepted",
        "换一种收尾方式，不再独自离开": "superseded",
        "写过的章给的意见算整段": "superseded",
        "渡口那场让守夜人回来": "proposed",
    }
    assert [row.status for row in session.query(EditorialReview).order_by(EditorialReview.created_at.asc()).all()] == [
        "superseded",
        "active",
    ]
    assert "让林昭在渡口第一次开口求人" in _bundle(session, _scene(4, 1))["inline_digests"][EDITORIAL_GUIDANCE_SECTION_KEY]
    stale = first["guidance"][1]["guidance_id"]
    with pytest.raises(DomainError) as superseded:
        decide_guidance(session, PROJECT, stale, decision="accept")
    assert (superseded.value.status_code, superseded.value.code) == (409, "EDITORIAL_GUIDANCE_SUPERSEDED")
    overview = story_memory_overview(session, PROJECT)["editorial"]
    assert overview["latest"]["review_id"] == second["review_id"] and len(overview["history"]) == 1
    assert [(item["text"], item["chapter_number"]) for item in overview["accepted"]] == [("让林昭在渡口第一次开口求人", 4)]


# ------------------------------------------------------------------ 失败路径


def test_the_review_fails_closed(session, monkeypatch) -> None:
    _seed_book(session)
    session.commit()
    with pytest.raises(DomainError) as not_ready:
        generate_editorial_review(session, PROJECT)
    assert (not_ready.value.status_code, not_ready.value.code) == (409, "EDITORIAL_REVIEW_NOT_READY")

    for index in (1, 2, 3):
        _write_final(session, _scene(index, 1), f"第 {index} 章的正文。")
        _seed_digest(session, index)
    _freshen(session)
    session.commit()
    with pytest.raises(DomainError) as no_model:
        generate_editorial_review(session, PROJECT)
    assert (no_model.value.status_code, no_model.value.code) == (409, "EDITORIAL_REVIEW_LLM_UNAVAILABLE")

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    _install(monkeypatch, ScriptedTaskRunner(session, [{"assessment": "", "findings": []}], call_prefix="llmcall_editor"))
    with pytest.raises(DomainError) as invalid:
        generate_editorial_review(session, PROJECT)
    assert (invalid.value.status_code, invalid.value.code) == (502, "EDITORIAL_REVIEW_RESPONSE_INVALID")
    session.rollback()
    assert session.query(EditorialReview).count() == 0
    call = session.get(LlmCall, "llmcall_editor_00")
    assert (call.accounting_status, call.error_code) == ("failed", "EDITORIAL_REVIEW_RESPONSE_INVALID")


@contextmanager
def _same_session(session):
    yield session


def test_the_worker_reviews_once_the_digests_have_caught_up(session, monkeypatch, llm_on) -> None:
    _seed_book(session)
    for index in (1, 2, 3):
        _write_final(session, _scene(index, 1), f"第 {index} 章的正文。")
    session.commit()
    digest = {"summary": "林昭独自行动。", "ending_state": "", "character_changes": [], "threads": []}
    runner = ScriptedTaskRunner(session, [digest, digest, digest, _reply()], call_prefix="llmcall_worker")
    _install(monkeypatch, runner)
    monkeypatch.setattr(story_memory, "_extraction_backlog", lambda session, project_id: ([], []))

    summary = run_story_memory_pass(session_factory=lambda: _same_session(session))

    assert summary["digested"] == [_chapter(1), _chapter(2), _chapter(3)]
    assert summary["reviewed"] == [PROJECT]
    assert [call["context"].node_id for call in runner.calls] == ["chapter_digest"] * 3 + ["editorial_review"]
    assert session.query(EditorialReview).one().origin == "auto"
    again = run_story_memory_pass(session_factory=lambda: _same_session(session))
    assert (again["digested"], again["reviewed"], len(runner.calls)) == ([], [], 4)


# ------------------------------------------------------------------ 接口


def test_editorial_routes(client, session, monkeypatch) -> None:
    _seed_written(session)

    missing_model = client.post(f"/api/v1/projects/{PROJECT}/story-memory/editorial-review", json={})
    assert missing_model.status_code == 409 and missing_model.json()["error"]["code"] == "EDITORIAL_REVIEW_LLM_UNAVAILABLE"

    session.add(
        EditorialReview(
            review_id="review_1",
            project_id=PROJECT,
            through_chapter_id=_chapter(3),
            window_chapter_ids_json=[_chapter(4)],
            status="active",
            origin="auto",
            review_json={"assessment": "人物弧没动。", "findings": []},
        )
    )
    session.flush()
    session.add(EditorialGuidance(guidance_id="g1", review_id="review_1", project_id=PROJECT, chapter_id=_chapter(4), text="让林昭开口求人", status="proposed"))
    session.commit()
    path = f"/api/v1/projects/{PROJECT}/story-memory/editorial-guidance/g1/decision"

    accepted = client.post(path, json={"decision": "accept", "text": "让林昭开口求他最不信的人"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["guidance"]["author_text"] == "让林昭开口求他最不信的人"
    assert client.post(path, json={"decision": "maybe"}).status_code == 422
    unknown = client.post(f"/api/v1/projects/{PROJECT}/story-memory/editorial-guidance/nope/decision", json={"decision": "dismiss"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "EDITORIAL_GUIDANCE_NOT_FOUND"

    overview = client.get(f"/api/v1/projects/{PROJECT}/story-memory").json()["data"]["editorial"]
    assert overview["latest"]["assessment"] == "人物弧没动。" and overview["can_run"] is True
    assert overview["latest"]["guidance"][0]["status"] == "accepted"
    assert overview["readable_through"] == 3 and overview["next_auto_after_chapter"] == 6

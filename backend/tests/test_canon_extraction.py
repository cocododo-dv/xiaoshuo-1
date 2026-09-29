"""成稿中心正史面板的「提取」：作者点一下，读当前终稿、抽出候选事实、暂存等作者核对。

以前只有「没接模型就 409」一条用例；这里补上抽取成功的整条路径（候选、暂存事件、载荷键），
暂存逻辑与归档时的抽取共用一份之后（B11-13）两边都靠它守住。
"""

from __future__ import annotations

import json

import pytest

from novel_system.db.models import FactCandidate, LlmCall, LlmCallAttempt, NarrativeEvent, SceneCard
from novel_system.services.canon_continuity import CanonContinuityService
from tests.narrative_fixtures import WORLD_PROJECT, seed_final_scene, seed_narrative_world, world_scene

SCENE = world_scene(2, 2)
PROSE = "林远把半页旧信交给苏晚。苏晚这才得知案卷藏在北境。两人从此不再互相试探。"


class _Response:
    def __init__(self, text: str, llm_call_id: str) -> None:
        self.text = text
        self.structured_output = None
        self.llm_call_id = llm_call_id


class ScriptedExtractionRunner:
    """按次回放抽取结果的记账假模型：每次调用各写一条已结算的 LlmCall（产品侧要核对这条父记录）。"""

    provider_execution_mode = "online"

    def __init__(self, session, replies: list[dict]) -> None:
        self.session = session
        self.replies = list(replies)
        self.calls: list[dict] = []

    def run_task(self, *, task_name, prompt_text, system_prompt, context, **_kwargs):
        index = len(self.calls)
        self.calls.append({"task_name": task_name, "prompt_text": prompt_text, "system_prompt": system_prompt})
        call_id = f"llmcall_canon_extract_{index:02d}"
        self.session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={"_accounting_provider_execution_mode": "online"},
                prompt_tokens=9,
                completion_tokens=3,
                total_tokens=12,
                estimated_tokens=12,
                reserved_tokens=12,
                budget_charged_tokens=12,
                latency_ms=1,
                usage_is_estimate=False,
                accounting_status="settled",
                request_dispatched_at="2026-09-30T00:00:00Z",
                settled_at="2026-09-30T00:00:01Z",
            )
        )
        self.session.add(
            LlmCallAttempt(
                attempt_id=f"attempt_{call_id}",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=512,
                prompt_tokens=9,
                completion_tokens=3,
                total_tokens=12,
                estimated_tokens=12,
                reserved_tokens=12,
                budget_charged_tokens=12,
                latency_ms=1,
                usage_is_estimate=False,
                accounting_status="settled",
                request_dispatched_at="2026-09-30T00:00:00Z",
                settled_at="2026-09-30T00:00:01Z",
            )
        )
        self.session.flush()
        reply = self.replies[index] if index < len(self.replies) else {"events": []}
        return _Response(json.dumps(reply, ensure_ascii=False), call_id)


@pytest.fixture
def extraction_world(session, monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    seed_narrative_world(session)
    scene = session.get(SceneCard, SCENE)
    seed_final_scene(session, scene=scene, content=PROSE)
    CanonContinuityService(session).mark_archive_pending(f"final_{SCENE}_v1")
    session.commit()
    return scene


def _install_runner(monkeypatch, runner) -> None:
    monkeypatch.setattr("novel_system.services.llm_task_runner.LLMNodeRunner", lambda session: runner)


def test_author_requested_extraction_stages_pending_candidates(session, monkeypatch, extraction_world) -> None:
    runner = ScriptedExtractionRunner(
        session,
        [
            {
                "events": [
                    {
                        "event_type": "character_learns",
                        "entity_id": "苏晚",
                        "fact_key": "knows_archive",
                        "fact_value": "案卷藏在北境",
                        "evidence": "苏晚这才得知案卷藏在北境",
                    },
                    {
                        "event_type": "relation_change",
                        "entity_id": "林远",
                        "fact_key": "stance_toward_suwan",
                        "fact_value": "不再试探",
                        "evidence": "两人从此不再互相试探",
                    },
                ]
            }
        ],
    )
    _install_runner(monkeypatch, runner)

    result = CanonContinuityService(session).extract_scene_candidates(WORLD_PROJECT, SCENE)

    assert result["already_extracted"] is False
    assert result["product"]["outcome"] == "completed_events"
    assert [call["task_name"] for call in runner.calls] == ["narrative_event_extract"]
    assert PROSE in runner.calls[0]["prompt_text"]
    scene_status = result["scene"]
    assert scene_status["status"] == "pending_review"
    assert scene_status["extraction"]["extraction_outcome"] == "completed_events"
    assert scene_status["extraction"]["requires_scene_confirmation"] is True
    candidates = scene_status["candidates"]
    assert [(c["event_type"], c["entity_type"], c["raw_entity_ref"], c["status"]) for c in candidates] == [
        ("character_learns", "character", "苏晚", "pending"),
        ("relation_change", "relation", "林远", "pending"),
    ]
    assert all(c["evidence"]["grounded"] for c in candidates)
    staged_events = [
        session.get(NarrativeEvent, row.staged_event_id)
        for row in session.query(FactCandidate).filter(FactCandidate.scene_id == SCENE).order_by(FactCandidate.created_at)
    ]
    assert [event.payload_json for event in staged_events] == [
        {"source": "prose", "trigger": "author_requested", "extract_ordinal": 0, "llm_call_id": "llmcall_canon_extract_00"},
        {"source": "prose", "trigger": "author_requested", "extract_ordinal": 1, "llm_call_id": "llmcall_canon_extract_00"},
    ]
    assert {(event.authority_status, event.source_kind, event.confidence) for event in staged_events} == {
        ("pending", "prose_extraction", "extracted")
    }


def test_repeated_extraction_is_not_redispatched(session, monkeypatch, extraction_world) -> None:
    runner = ScriptedExtractionRunner(session, [{"events": []}])
    _install_runner(monkeypatch, runner)
    service = CanonContinuityService(session)

    first = service.extract_scene_candidates(WORLD_PROJECT, SCENE)
    second = service.extract_scene_candidates(WORLD_PROJECT, SCENE)

    assert first["product"]["outcome"] == "completed_empty"
    assert first["scene"]["extraction"]["requires_empty_confirmation"] is True
    assert second["already_extracted"] is True
    assert len(runner.calls) == 1

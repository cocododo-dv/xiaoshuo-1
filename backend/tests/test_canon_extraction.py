"""成稿中心正史面板的「提取」：作者点一下，读当前终稿、抽出候选事实、暂存等作者核对。

以前只有「没接模型就 409」一条用例；这里补上抽取成功的整条路径（候选、暂存事件、载荷键），
暂存逻辑与归档时的抽取共用一份之后（B11-13）两边都靠它守住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novel_system.db.models import FactCandidate, LlmCall, LlmCallAttempt, NarrativeEvent, SceneCard
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.system_config import SystemConfigService
from tests.narrative_fixtures import WORLD_PROJECT, seed_final_scene, seed_narrative_world, world_scene

SCENE = world_scene(2, 2)
PROSE = "林远把半页旧信交给苏晚。苏晚这才得知案卷藏在北境。两人从此不再互相试探。"
REPO_PROMPTS = Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"


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
        reply = self.replies[index] if index < len(self.replies) else {"events": []}
        failure = reply.get("__fail__") if isinstance(reply, dict) else None
        status = "failed" if failure else "settled"
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
                accounting_status=status,
                request_dispatched_at="2026-09-30T00:00:00Z",
                settled_at="2026-09-30T00:00:01Z",
                error_code=failure,
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
                accounting_status=status,
                request_dispatched_at="2026-09-30T00:00:00Z",
                settled_at="2026-09-30T00:00:01Z",
                error_code=failure,
            )
        )
        self.session.flush()
        if failure:
            from novel_system.services.llm_task_runner import LLMNodeExecutionError

            raise LLMNodeExecutionError(
                llm_call_id=call_id,
                error_code=failure,
                message=failure,
                request_summary={},
                response_summary={},
                original_error=RuntimeError(failure),
            )
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
    assert runner.calls[0]["prompt_text"] == f"## Scene prose\n\n{PROSE}"
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
        {
            "source": "prose",
            "trigger": "author_requested",
            "extract_ordinal": 0,
            "extract_chunk": 0,
            "llm_call_id": "llmcall_canon_extract_00",
        },
        {
            "source": "prose",
            "trigger": "author_requested",
            "extract_ordinal": 1,
            "extract_chunk": 0,
            "llm_call_id": "llmcall_canon_extract_00",
        },
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


# 超过一段上限的长场：三段正文，每段一句「林远……」的事实，结尾的挫折落在最后一段。
_LONG_PARAGRAPHS = [
    "林远在雨城的码头醒来，右臂裹着布。" + "雨一直下。" * 1100,
    "苏晚翻开案卷，才知道钟楼下埋着东西。" + "风很冷。" * 1490,
    "天亮时，林远把旧信烧掉，从此再也回不去雨城。",
]
LONG_PROSE = "\n".join(_LONG_PARAGRAPHS)


@pytest.fixture
def long_scene(session, monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    seed_narrative_world(session)
    scene = session.get(SceneCard, SCENE)
    seed_final_scene(session, scene=scene, content=LONG_PROSE)
    CanonContinuityService(session).mark_archive_pending(f"final_{SCENE}_v1")
    session.commit()
    return scene


def _fact(entity: str, key: str, value: str, evidence: str) -> dict:
    return {"event_type": "character_state", "entity_id": entity, "fact_key": key, "fact_value": value, "evidence": evidence}


def test_extraction_reads_the_whole_scene_in_paragraph_chunks(session, monkeypatch, long_scene) -> None:
    """以前只读前 6,000 字就报「抽取完成」：结尾烧信那条事实永远抽不到（批准 #14，B11-15）。"""
    assert len(LONG_PROSE) > 6000
    runner = ScriptedExtractionRunner(
        session,
        [
            {"events": [_fact("林远", "injury", "右臂受伤", "右臂裹着布")]},
            {"events": [_fact("苏晚", "knows_archive", "钟楼下埋着东西", "才知道钟楼下埋着东西")]},
            {"events": [_fact("林远", "item_lost", "旧信", "林远把旧信烧掉")]},
        ],
    )
    _install_runner(monkeypatch, runner)

    result = CanonContinuityService(session).extract_scene_candidates(WORLD_PROJECT, SCENE)

    assert result["chunk_count"] == 3
    assert len(runner.calls) == 3
    for index, call in enumerate(runner.calls, start=1):
        assert f"(Part {index} of 3 of this scene. Report only facts written in this part.)" in call["prompt_text"]
        assert len(call["prompt_text"].split("\n\n", 2)[2]) <= 6000
    chunk_text = "".join(call["prompt_text"].split("\n\n", 2)[2] for call in runner.calls)
    assert chunk_text == LONG_PROSE
    candidates = result["scene"]["candidates"]
    assert [(c["raw_entity_ref"], c["fact_key"]) for c in candidates] == [
        ("林远", "injury"),
        ("苏晚", "knows_archive"),
        ("林远", "item_lost"),
    ]
    assert all(c["evidence"]["grounded"] for c in candidates)
    staged = [
        session.get(NarrativeEvent, row.staged_event_id)
        for row in session.query(FactCandidate).filter(FactCandidate.scene_id == SCENE).order_by(FactCandidate.created_at)
    ]
    assert [(e.payload_json["extract_ordinal"], e.payload_json["extract_chunk"], e.payload_json["llm_call_id"]) for e in staged] == [
        (0, 0, "llmcall_canon_extract_00"),
        (1, 1, "llmcall_canon_extract_01"),
        (2, 2, "llmcall_canon_extract_02"),
    ]
    assert result["scene"]["extraction"]["extraction_outcome"] == "completed_events"


def test_a_failed_chunk_degrades_the_whole_extraction_and_stages_nothing(session, monkeypatch, long_scene) -> None:
    """半场读不出就不算抽完：整场按降级报、不暂存前几段的候选，作者重点一次会整场重读。"""
    runner = ScriptedExtractionRunner(
        session,
        [
            {"events": [_fact("林远", "injury", "右臂受伤", "右臂裹着布")]},
            {"__fail__": "LLM_PROVIDER_TIMEOUT"},
        ],
    )
    _install_runner(monkeypatch, runner)

    result = CanonContinuityService(session).extract_scene_candidates(WORLD_PROJECT, SCENE)

    assert len(runner.calls) == 2
    assert result["product"]["outcome"] == "provider_failed"
    assert result["product"]["error_code"] == "LLM_PROVIDER_TIMEOUT"
    assert result["scene"]["status"] == "degraded"
    assert result["scene"]["candidates"] == []
    assert session.query(FactCandidate).filter(FactCandidate.scene_id == SCENE).count() == 0


def _activate_prompts_snapshot_without_the_extractor(session) -> None:
    """一份只有 ``neutral_draft`` 的活动提示词快照：保存过提示词、还没跑 ``sync_prompt_templates`` 的安装的样子。"""
    template = {
        "version": "2026-09-29.v1",
        "input_token_budget": 24000,
        "system_prompt": "快照里只有这一条。",
        "task_prompt": "写这一场。",
        "structured_schema": {
            "type": "object",
            "required": ["scene_text"],
            "properties": {"scene_text": {"type": "string"}},
        },
    }
    service = SystemConfigService(session)
    created = service.create_draft(
        category="prompts",
        yaml_raw=yaml.safe_dump({"templates": {"neutral_draft": template}}, allow_unicode=True, sort_keys=False),
        secrets=None,
        actor_ref="test",
    )
    service.activate(created["snapshot"]["snapshot_id"], actor_ref="test")
    session.commit()


def test_extraction_uses_the_repo_template_when_the_prompts_snapshot_predates_it(
    session, monkeypatch, extraction_world
) -> None:
    """抽取的提示词以前写死在代码里：已经保存过提示词快照的安装，上线后到跑 ``sync_prompt_templates`` 之前，快照里
    没有 ``narrative_event_extract``——作者点「提取」要用仓库 prompts.yaml 里的那份，而不是报 500。"""
    _activate_prompts_snapshot_without_the_extractor(session)
    assert "narrative_event_extract" not in load_prompt_templates()
    runner = ScriptedExtractionRunner(session, [{"events": []}])
    _install_runner(monkeypatch, runner)

    result = CanonContinuityService(session).extract_scene_candidates(WORLD_PROJECT, SCENE)

    assert result["product"]["outcome"] == "completed_empty"
    repo_template = load_prompt_templates(REPO_PROMPTS)["narrative_event_extract"]
    assert "continuity fact-extractor" in repo_template.system_prompt
    assert [(call["task_name"], call["system_prompt"], call["prompt_text"]) for call in runner.calls] == [
        ("narrative_event_extract", repo_template.system_prompt, f"## Scene prose\n\n{PROSE}")
    ]

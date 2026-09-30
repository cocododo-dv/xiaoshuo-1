"""Wave 3（结果闭环治理 §5.5/§6.3）：Best-of-N 作者终选门。

完成门可复算证明：
- 关键场景候选生成后暂停编排（awaiting_candidate_selection），未选择前
  管线不归档、adopt-current 也拒绝（双入口封死）；
- 盲化视图：默认按 blinded_order 输出全文、剥离机器分数；主动展开不重排；
- 终选一次写入：同选幂等、异选 409，显式重开后方可改选（审计留痕）；
- 选择后 resume-after-selection 从批判修订/QC 继续，安全归档，终稿=选中稿。

2026-09-30 [批准#2]：多稿只剩作者手笔直起——候选 = 首稿 + 定向修改（按读数排序）。这里的场景都绑一本合成参考书
（style_first），读数由替身按正文记号给；首稿带参考样例窗，生命周期预算解除武装（产品默认本来就是解除武装）。
先中性后润色那一套特有的去模板候选谱系用例随那一套删掉。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.near_final import (
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake
from tests.support.style_first_fixtures import bind_style_first, install_readings, reading


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


PROJECT_ID = "PROJECT300"
SCENE_ID = "CH300_SC01"
CHAPTER_ID = "CH300"
# 产品里一场的运行总是经幂等路由（或场景作业）发起；选后续跑只认这两种来源的产物归属
#（scene_run_checkpoint._checkpoint_execution_owner_matches），所以首跑也用一个幂等执行 id。
ORIGIN_EXECUTION_ID = "idempotency:w3-origin-run"


@pytest.fixture(autouse=True)
def _multi_candidate_authorization_for_candidate_gate_tests(monkeypatch) -> None:
    """Candidate-gate mechanics run with three authorized candidates (first draft + two targeted revisions).

    Production drafts a single candidate unless the Best-of-N switch is on and the work is bound
    style_first; these tests exercise the downstream multi-candidate selection state machine.
    """

    from novel_system.services.orchestrator import Orchestrator

    def _three_candidates(self, contract, *, criticality=None):
        if criticality is not None:
            return max(1, min(3, int(criticality.initial_best_of_n)))
        return 3

    monkeypatch.setattr(Orchestrator, "_best_of_n_count", _three_candidates)


@pytest.fixture(autouse=True)
def _style_first_scene_defaults(monkeypatch) -> None:
    """首稿读数可信（修改槽位照改、候选按读数排序），第一份修改稿（供应商第 2 次回稿）读得最近、排第一——终选清单的
    第一份是一份真正的修改稿；生命周期预算解除武装（首稿带参考样例窗，套件默认的武装预算装不下）。"""
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    install_readings(monkeypatch, {"draft #2": reading(0.5, 30.0)}, default=reading(1.0, 60.0))


def _response(payload: dict, *, request_id: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "fake-model",
            "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
        finish_reason="stop",
    )


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests)
        payload = {
            "scene_text": f"Provider-generated draft #{index} for terminal selection.",
            "continuity_notes": [],
        }
        return _response(payload, request_id=f"resp_scene_{index:03d}")


class FakePassQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        return _response(self.payload, request_id="resp_qc_001")


def _hard_pass() -> dict:
    return {
        "resolution_code": "hard_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
    }


def _soft_pass() -> dict:
    return {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
        "carry_forward_note": False,
        "note_scope": None,
        "carry_note_text": None,
    }


def _seed_scene(session, *, constraint_intensity: float | None = 0.9) -> None:
    session.add(
        StoryProject(project_id=PROJECT_ID, title="Selection gate", outline_text="")
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id=CHAPTER_ID, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            project_id=PROJECT_ID,
            chapter_id=CHAPTER_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text="",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
            constraint_intensity=constraint_intensity,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()
    bind_style_first(session, "gate300", project_id=PROJECT_ID)


def _make_orchestrator(session) -> Orchestrator:
    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session, llm_client=FakeSceneClient()
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=FakePassQcClient(_hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=FakePassQcClient(_soft_pass())),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(
        session, llm_client=support
    )
    return orchestrator


def _selection_gate(session) -> HumanReviewEvent:
    events = (
        session.execute(
            select(HumanReviewEvent).order_by(HumanReviewEvent.created_at.desc())
        )
        .scalars()
        .all()
    )
    for event in events:
        if (event.details_json or {}).get("gate_type") == "style_candidate_selection":
            return event
    raise AssertionError("no style_candidate_selection gate event found")


class _IdenticalCandidatesClient(FakeSceneClient):
    """每一份定向修改都回首稿那一段字（作者手笔直起时没过门的修改槽位保留首稿原文，就是这种情形）。"""

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests)
        text = "Every candidate came back with the very same scene text."
        return _response({"scene_text": text, "continuity_notes": []}, request_id=f"resp_scene_{index:03d}")


def test_critical_scene_does_not_pause_on_candidates_that_share_one_text(session) -> None:
    """风格参考 v3（L2）：候选按正文去重之后只剩一份——没有可选的，不开终选门（以前按去重之前的候选数判，
    作者会被叫去对着一份稿子「终选」）。管线照常往下走。"""
    _seed_scene(session)
    orchestrator = _make_orchestrator(session)
    orchestrator.scene_generation_service = SceneGenerationService(session, llm_client=_IdenticalCandidatesClient())

    result = orchestrator.run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    assert result["scene_status"] != "awaiting_candidate_selection"
    assert result.get("candidate_selection_required") is not True
    gates = [
        event
        for event in session.execute(select(HumanReviewEvent)).scalars().all()
        if (event.details_json or {}).get("gate_type") == "style_candidate_selection"
    ]
    assert gates == []
    state = session.get(SceneRunState, SCENE_ID)
    assert len(state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]) >= 2, "确实生成了多份候选"


# ---------- 暂停：关键场景未选择前不可归档 ----------
# ---------- 暂停：关键场景未选择前不可归档 ----------


def test_critical_scene_pauses_before_selection(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(session)

    result = orchestrator.run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    state = session.get(SceneRunState, SCENE_ID)
    assert result["scene_status"] == "awaiting_candidate_selection"
    assert result["author_state"] == "awaiting_author_choice"
    assert result["can_archive"] is False
    assert result["latest_valid_draft_row_id"]
    assert state.current_final_scene_row_id is None
    assert session.execute(select(FinalScene)).scalars().all() == []

    gate = _selection_gate(session)
    details = gate.details_json
    assert state.run_checkpoint == "selection_wait"
    assert (
        state.run_checkpoint_json["artifact_refs"]["selection_event_id"]
        == gate.event_id
    )
    assert (
        state.run_checkpoint_json["artifact_refs"]["selection_candidate_row_ids"]
        == details["candidate_row_ids"]
    )
    rankings = state.run_checkpoint_json["artifact_refs"]["style_candidate_rankings"]
    assert len(rankings) == len(details["candidate_row_ids"])
    # 作者手笔直起的排序：先抄袭门、再读数 distance（读得最近的修改稿在前，平手时首稿在前）
    assert all(item["selection_reason"] == "fidelity_distance" for item in rankings)
    assert all(item["rerank"]["applied_mode"] == "fidelity_distance" for item in rankings)
    assert all(item["plagiarism_checked"] is True and item["plagiarism_passed"] is True for item in rankings)
    assert [item["slot_index"] for item in rankings] == [1, 0, 2]
    assert details["decision_status"] == "awaiting"
    assert details["candidate_row_ids"]
    assert sorted(details["blinded_order"]) == sorted(details["candidate_row_ids"])
    assert "tokens_used" in details
    assert "style_feedback_snapshot" not in details  # 2026-09-14 减法:风格反馈层已删除


def test_explicit_style_selection_reason_is_recorded_in_decision_history(
    client,
    session,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    selected_row_id = gate.details_json["candidate_row_ids"][0]

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{selected_row_id}/select",
        json={
            "no_clear_difference": False,
            "duration_ms": 1234,
            "preference_tags": ["style_match"],
        },
        headers={"X-Idempotency-Key": "w3-style-feedback-1"},
    )

    assert response.status_code == 200
    assert "style_feedback_recorded" not in response.json()["data"]
    session.refresh(gate)
    assert gate.details_json["preference_tags"] == ["style_match"]
    assert "style_feedback" not in gate.details_json
    assert gate.status == "resolved"
    history = gate.details_json["decision_history"]
    assert history[-1]["duration_ms"] == 1234
    assert history[-1]["preference_tags"] == ["style_match"]


def test_selection_rejects_unknown_feedback_reason(client, session) -> None:
    _seed_scene(session)
    row_ids = ["w3_cand_reason"]
    _seed_manual_gate(session, row_ids, row_ids)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{row_ids[0]}/select",
        json={"preference_tags": ["imitate_author_identity"]},
        headers={"X-Idempotency-Key": "w3-style-feedback-invalid"},
    )

    assert response.status_code == 422


def test_candidate_gate_excludes_a_candidate_already_proven_to_copy_reference(
    session,
) -> None:
    _seed_scene(session)
    copied = SimpleNamespace(
        row_id="copy",
        content="一段已经被本地连续重叠检测确认的候选正文。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": False},
    )
    safe = SimpleNamespace(
        row_id="safe",
        content="另一段没有复刻来源文本的候选正文。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": True},
    )

    offered = Orchestrator(session)._offer_candidates_for_selection(
        session.get(SceneCard, SCENE_ID),
        session.get(SceneRunState, SCENE_ID),
        {},
        [copied, safe],
    )

    assert offered == ["safe"]
    assert _selection_gate(session).details_json["candidate_row_ids"] == ["safe"]


def test_standard_scene_does_not_pause(session) -> None:
    _seed_scene(session, constraint_intensity=0.5)  # standard：机器下限自动选择
    orchestrator = _make_orchestrator(session)

    result = orchestrator.run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    assert result["scene_status"] == "archived"


def test_adopt_refuses_before_selection(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "w3-adopt-await"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELECTION_REQUIRED"


# ---------- 盲化视图 ----------


def _seed_manual_gate(
    session, row_ids: list[str], blinded: list[str]
) -> HumanReviewEvent:
    for i, row_id in enumerate(row_ids):
        session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=SCENE_ID,
                chapter_id=CHAPTER_ID,
                stage="style_draft",
                content=f"候选正文 {i}：潮水退去，闸门上的名字露出来。",
                source_bundle_id="bundle_w3",
                source_bundle_hash="hash_w3",
            )
        )
    event = HumanReviewEvent(
        event_id="hre_sel_manual_1",
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        object_ref=f"candidate_selection:{SCENE_ID}",
        event_source="candidate_selection",
        priority="high",
        status="awaiting_review",
        allowed_actions_json=["select"],
        details_json={
            "gate_type": "style_candidate_selection",
            "candidate_row_ids": row_ids,
            "blinded_order": blinded,
            "decision_status": "awaiting",
            "selected_row_id": None,
            "tokens_used": 0,
        },
    )
    session.add(event)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_status = "awaiting_candidate_selection"
    state.current_human_review_event_id = event.event_id
    session.commit()
    return event


def test_blinded_candidates_view_strips_scores_and_uses_blinded_order(
    client, session
) -> None:
    _seed_scene(session)
    row_ids = ["w3_cand_a", "w3_cand_b", "w3_cand_c"]
    blinded = ["w3_cand_b", "w3_cand_c", "w3_cand_a"]
    _seed_manual_gate(session, row_ids, blinded)

    data = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates").json()["data"]

    assert data["blinded"] is True
    assert [c["row_id"] for c in data["candidates"]] == blinded
    for candidate in data["candidates"]:
        assert "adversarial_score" not in candidate  # 默认剥离机器分数（§5.5）
        assert "selected" not in candidate  # 盲化视图不泄漏机器预选
        assert candidate["content"]  # 展示完整正文，不只预览

    # 作者主动展开：附分数但不得重排（分数只做标注，不做默认排序）
    scored = client.get(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates?include_scores=true"
    ).json()["data"]
    assert [c["row_id"] for c in scored["candidates"]] == blinded
    assert all("adversarial_score" in c for c in scored["candidates"])


# ---------- 终选锁定与重开 ----------


def test_select_outside_gate_candidates_rejected(client, session) -> None:
    _seed_scene(session)
    _seed_manual_gate(session, ["w3_cand_a"], ["w3_cand_a"])
    session.add(
        SceneDraft(
            row_id="w3_outside",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            stage="style_draft",
            content="不在终选清单里的旧稿。",
            source_bundle_id="bundle_w3",
            source_bundle_hash="hash_w3",
        )
    )
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/w3_outside/select",
        json={},
        headers={"X-Idempotency-Key": "w3-select-outside"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_IN_GATE"


# ---------- resume：选择后安全续跑 ----------


def test_resume_requires_selection(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-early"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELECTION_REQUIRED"


def test_select_then_resume_archives_the_chosen_candidate(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    chosen_content = session.get(SceneDraft, chosen_row_id).content

    selected = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
        json={"no_clear_difference": True},
        headers={"X-Idempotency-Key": "w3-select-resume-1"},
    )
    assert selected.status_code == 200

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-1"},
    )
    assert resumed.status_code == 200
    data = resumed.json()["data"]
    assert data["scene_status"] == "archived"
    assert data["author_state"] == "archived"

    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert state.active_execution_id == "idempotency:w3-resume-1"
    assert state.run_checkpoint_json["execution_id"] == "idempotency:w3-resume-1"
    final = session.get(FinalScene, state.current_final_scene_row_id)
    assert final is not None and final.status == "archived"
    # 终稿=作者选中稿，或其唯一一次批判修订稿（§5.5 允许；血缘必须指向选中稿）
    if final.content != chosen_content:
        from novel_system.db.models import AttemptTracker

        attempts = (
            session.execute(
                select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID)
            )
            .scalars()
            .all()
        )
        assert any(
            (attempt.details_json or {}).get("source_style_draft_row_id")
            == chosen_row_id
            for attempt in attempts
        ), "修订稿的来源必须是作者选中稿"

    # 重复 resume 不重复归档
    again = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-2"},
    )
    assert again.status_code in (200, 409)
    finals = (
        session.execute(select(FinalScene).where(FinalScene.scene_id == SCENE_ID))
        .scalars()
        .all()
    )
    assert len(finals) == 1

    session.refresh(gate)
    assert gate.details_json["decision_status"] == "selected"
    assert gate.status != "awaiting_review"  # gate 已闭合


def test_selection_resume_surfaces_lifecycle_budget_boundary_as_recoverable_payload(
    client,
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-budget-boundary"},
        ).status_code
        == 200
    )

    def reject_at_budget(self, scene_id: str):  # noqa: ANN001, ANN202
        raise LLMAccountingRejected(
            "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
            "scene token budget exhausted before dispatch",
        )

    monkeypatch.setattr(
        Orchestrator, "_resume_after_selection_pipeline", reject_at_budget
    )
    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-budget-boundary"},
    )

    assert resumed.status_code == 200
    block = resumed.json()["data"]["lifecycle_budget_block"]
    assert block == {
        "code": "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "message": "scene token budget exhausted before dispatch",
        "resume_mode": "selection",
    }
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_execution_status == "waiting_selection"
    assert state.scene_status == "awaiting_candidate_selection"


def test_resume_rejects_selected_candidate_with_non_style_lineage_stage_without_provider_replay(
    client,
    session,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-invalid-stage"},
        ).status_code
        == 200
    )
    chosen = session.get(SceneDraft, chosen_row_id)
    chosen.stage = "soft_patch"
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-invalid-stage"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


@pytest.mark.parametrize("mutation", ("budget", "basis", "counter"))
def test_selection_resume_validates_budget_checkpoint_before_provider_work(
    client,
    session,
    mutation: str,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": f"w3-select-budget-{mutation}"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    if mutation == "budget":
        state.scene_token_budget += 1
    elif mutation == "basis":
        state.scene_budget_basis_json = {"tampered": True}
    else:
        state.total_attempt_count = state.attempt_budget + 1
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": f"w3-resume-budget-{mutation}"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_rejects_selected_candidate_whose_durable_source_was_tampered(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]

    selected = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
        json={},
        headers={"X-Idempotency-Key": "w3-select-tampered"},
    )
    assert selected.status_code == 200
    draft = session.get(SceneDraft, chosen_row_id)
    draft.source_bundle_hash = "sha256:tampered"
    session.commit()

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-tampered"},
    )
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"


def test_resume_reports_missing_checkpoint_candidate_without_provider_replay(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-missing-candidate"},
        ).status_code
        == 200
    )
    session.delete(session.get(SceneDraft, chosen_row_id))
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-missing-candidate"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_validates_neutral_prefix_before_post_selection_provider_work(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-missing-neutral"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    neutral_row_id = state.run_checkpoint_json["artifact_refs"]["neutral_draft_row_id"]
    session.delete(session.get(SceneDraft, neutral_row_id))
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-missing-neutral"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_validates_hard_qc_report_content_hash_before_provider_work(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-corrupt-hard-report"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    report = session.get(
        QcReport, state.run_checkpoint_json["artifact_refs"]["qc_report_id"]
    )
    report.rewrite_brief_json = [{"instruction": "tampered after checkpoint"}]
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-corrupt-hard-report"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_uses_contiguous_hashes_when_the_top_ranked_candidate_is_filtered(
    client,
    session,
    monkeypatch,
) -> None:
    """排第一的候选（读数最近的那份修改稿）被来源安全过滤掉：终选清单与候选排序不再同序，续跑按终选清单的连续哈希核对。"""
    from novel_system.services import source_safety

    _seed_scene(session)
    original_scan = source_safety.scan_source_safety

    def _filter_top_ranked_candidate(
        text: str, *args, **kwargs
    ):  # noqa: ANN002, ANN003, ANN202
        if "draft #2" in text:
            return {"safe": False, "matches": [{"rule": "test-filter"}]}
        return original_scan(text, *args, **kwargs)

    monkeypatch.setattr(
        source_safety, "scan_source_safety", _filter_top_ranked_candidate
    )
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    offered = gate.details_json["candidate_row_ids"]
    state = session.get(SceneRunState, SCENE_ID)
    all_candidates = state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]
    assert len(offered) < len(all_candidates)
    assert offered[0] != all_candidates[0]
    chosen_row_id = offered[0]

    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-filtered-first"},
        ).status_code
        == 200
    )
    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-filtered-first"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["scene_status"] == "archived"


def test_selecting_the_first_draft_candidate_then_resume_archives_it(client, session, monkeypatch) -> None:
    """作者手笔直起的候选里首稿永远在（槽位 0，它没有自己的模型调用，沿用首稿那次调用的谱系）；作者选它，续跑照常
    归档，终稿就是首稿。"""
    _seed_scene(session)
    install_readings(monkeypatch, {"draft #1": reading(0.5, 30.0)}, default=reading(1.0, 60.0))
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    state = session.get(SceneRunState, SCENE_ID)
    rankings = state.run_checkpoint_json["artifact_refs"]["style_candidate_rankings"]
    first_draft_row_id = next(item["row_id"] for item in rankings if item["slot_index"] == 0)
    assert gate.details_json["candidate_row_ids"][0] == first_draft_row_id
    first_draft_content = session.get(SceneDraft, first_draft_row_id).content

    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{first_draft_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-first-draft"},
        ).status_code
        == 200
    )
    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-first-draft"},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["scene_status"] == "archived"
    session.expire_all()
    final = session.get(FinalScene, session.get(SceneRunState, SCENE_ID).current_final_scene_row_id)
    assert final is not None and final.content == first_draft_content

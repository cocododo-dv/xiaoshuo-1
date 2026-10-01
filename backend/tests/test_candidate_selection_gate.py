"""Wave 3（结果闭环治理 §5.5/§6.3）：Best-of-N 作者终选门。

完成门可复算证明：
- 关键场景候选生成后暂停编排（awaiting_candidate_selection），未选择前
  管线不归档、adopt-current 也拒绝（双入口封死）；
- 盲化视图：默认按 blinded_order 输出全文、剥离机器分数；主动展开不重排；
- 终选一次写入：同选幂等、异选 409 SELECTION_LOCKED（不能改选，想换一稿就重新起草这一场）；
- 选择后 resume-after-selection 从批判修订/QC 继续，安全归档，终稿=选中稿。

2026-09-30 [批准#2]：多稿只剩作者手笔直起——候选 = 首稿 + 定向修改（按读数排序）。这里的场景都绑一本合成参考书
（style_first），读数由替身按正文记号给；首稿带参考样例窗，生命周期预算解除武装（产品默认本来就是解除武装）。
先中性后润色那一套特有的去模板候选谱系用例随那一套删掉。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_generation import SceneGenerationService
from tests.support.style_first_fixtures import install_readings, reading

from tests.support.candidate_gate import (
    CHAPTER_ID,
    FakeSceneClient,
    ORIGIN_EXECUTION_ID,
    SCENE_ID,
    make_orchestrator as _make_orchestrator,
    response as _response,
    seed_scene as _seed_scene,
    selection_gate as _selection_gate,
)

pytestmark = pytest.mark.usefixtures("online_pipeline")


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

    # 作者主动展开：附分数但不得重排（分数只做标注，不做默认排序）。这几份手造的候选没有冻结的 bundle 可查，
    # 照旧给房风分；作者手笔直起的候选不给（下一条用例）
    scored = client.get(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates?include_scores=true"
    ).json()["data"]
    assert [c["row_id"] for c in scored["candidates"]] == blinded
    assert all("adversarial_score" in c for c in scored["candidates"])
    assert "scores_withheld" not in scored


def test_style_first_candidates_never_carry_the_house_taste_score(client, session) -> None:
    """重评 R2 复核补充 5：作者手笔直起的场景里，像不像以参考作者为准，房风规则让位——机器的去 AI 味规则分
    （adversarial_score）既不随「展开分数」给作者看，诊断形状也不再按它排序（改按生成时间倒序）。"""
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()
    gate = _selection_gate(session)
    blinded = gate.details_json["blinded_order"]

    scored = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates?include_scores=true").json()["data"]
    assert scored["blinded"] is True
    assert [c["row_id"] for c in scored["candidates"]] == blinded
    assert not [c for c in scored["candidates"] if "adversarial_score" in c]
    assert scored["scores_withheld"] == "style_first"

    diagnostic = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates?diagnostic=true").json()["data"]
    assert diagnostic["blinded"] is False
    assert not [c for c in diagnostic["candidates"] if "adversarial_score" in c]
    assert diagnostic["scores_withheld"] == "style_first"
    drafts = session.execute(
        select(SceneDraft)
        .where(SceneDraft.scene_id == SCENE_ID, SceneDraft.stage == "style_draft")
        .order_by(SceneDraft.created_at.desc())
    ).scalars().all()
    assert [c["row_id"] for c in diagnostic["candidates"]] == [draft.row_id for draft in drafts]
    assert set(blinded) <= {draft.row_id for draft in drafts}


def _forbid_house_taste_scoring(monkeypatch) -> None:
    """运行结果装配候选摘要时一为房风分现算（``adversarial_rank_score``）就失败。"""
    from novel_system.services.scene_run import pipeline as pipeline_module

    def _no_house_taste_score(*_args, **_kwargs):
        raise AssertionError("作者手笔直起的候选摘要不该为房风分现算")

    monkeypatch.setattr(pipeline_module, "adversarial_rank_score", _no_house_taste_score)


def test_style_first_run_result_candidate_summaries_carry_no_house_taste_score(session, monkeypatch) -> None:
    """重评 R2 复核补充 5 的第三处——运行结果里的候选摘要（``style_candidates``）：作者手笔直起的场景只给像不像
    读数，不附房风分，也不为它现算（两个终选视图 P09b 已经收了，这一处在 scene_run 里；I7 合并胶水 G4）。
    排序审计里存着的 quality_score 是检查点产品的一部分，原样留着。"""
    _forbid_house_taste_scoring(monkeypatch)
    _seed_scene(session, constraint_intensity=0.5)  # standard：两份候选，不停下终选，一路归档
    result = _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    summaries = result["style_candidates"]
    assert len(summaries) == 2
    assert not [summary for summary in summaries if "adversarial_score" in summary]
    assert {summary["scores_withheld"] for summary in summaries} == {"style_first"}
    assert all(summary["fidelity_distance"] is not None for summary in summaries)
    assert [summary["selected"] for summary in summaries] == [True, False]
    # 存进检查点的排序审计不变：仍带 quality_score
    rankings = session.get(SceneRunState, SCENE_ID).run_checkpoint_json["artifact_refs"]["style_candidate_rankings"]
    assert len(rankings) == 2 and all(isinstance(ranking.get("quality_score"), float) for ranking in rankings)


def test_the_shipped_single_style_first_candidate_carries_no_house_taste_score(session, monkeypatch) -> None:
    """出厂配置（复核 I7-R2）：Best-of-N 开关默认关，作者手笔直起的场景只起一稿，这一稿没有排序审计（只有多稿排序
    才写）——以前运行结果正是为这一份现算并附上房风分。现在它与多稿一样只标 scores_withheld，不附分、不现算。"""
    from novel_system.services.scene_run.style_candidates import StyleCandidatesMixin

    # 撤掉本文件的三稿授权，回到产品自己的开关（默认关 → 一稿）
    monkeypatch.delenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", raising=False)
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", StyleCandidatesMixin._best_of_n_count)
    _forbid_house_taste_scoring(monkeypatch)
    _seed_scene(session, constraint_intensity=0.5)  # 与上一条同一个标准场，只差开关
    result = _make_orchestrator(session).run_scene(SCENE_ID, execution_id=ORIGIN_EXECUTION_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    refs = session.get(SceneRunState, SCENE_ID).run_checkpoint_json["artifact_refs"]
    assert refs["style_initial_candidate_count"] == 1
    assert refs["style_candidate_rankings"] == [None], "前提：这一稿没有排序审计，也就没有存着的分可用"
    [summary] = result["style_candidates"]
    assert summary["scores_withheld"] == "style_first"
    assert "adversarial_score" not in summary
    assert summary["selected"] is True


def test_a_style_first_candidate_without_a_stored_score_is_not_scored_either(monkeypatch) -> None:
    """复核 I7-R2：作者手笔直起、排序审计里没存分的候选（出厂配置下每一场都是这样）同样只标 scores_withheld——
    不附房风分，也不为它现算。"""
    from novel_system.services.scene_generation import StyleGenerationResult
    from novel_system.services.scene_run.pipeline import PipelineMixin

    _forbid_house_taste_scoring(monkeypatch)
    single = StyleGenerationResult(
        row_id="cand_single_style_first",
        content="只起一稿、没有排序审计的一份。",
        llm_call_id="call_cand_single_style_first",
        bundle_id="bundle_w3",
        bundle_hash="hash_w3",
        ranking_audit=None,
    )
    [summary] = PipelineMixin._candidate_summaries([single], house_taste_withheld=True)

    assert summary["scores_withheld"] == "style_first"
    assert "adversarial_score" not in summary
    assert summary["selected"] is True


def test_candidate_summaries_elsewhere_keep_the_house_taste_score_and_reuse_a_stored_one(monkeypatch) -> None:
    """不是作者手笔直起的场景照旧给 adversarial_score：排序审计里存着分就用存着的、不再现算；只有没存分的（一份
    候选的运行）才现算。"""
    from novel_system.services.scene_generation import StyleGenerationResult
    from novel_system.services.scene_run import pipeline as pipeline_module
    from novel_system.services.scene_run.pipeline import PipelineMixin

    scored: list[str] = []

    def _score(text: str) -> float:
        scored.append(text)
        return 0.4567

    monkeypatch.setattr(pipeline_module, "adversarial_rank_score", _score)

    def _candidate(row_id: str, content: str, ranking: dict | None) -> StyleGenerationResult:
        return StyleGenerationResult(
            row_id=row_id,
            content=content,
            llm_call_id=f"call_{row_id}",
            bundle_id="bundle_w3",
            bundle_hash="hash_w3",
            ranking_audit=ranking,
        )

    stored = _candidate("cand_stored", "排过序的一份。", {"quality_score": 0.81234, "selection_reason": "quality_order"})
    unscored = _candidate("cand_single", "只有一份候选的运行。", None)
    summaries = PipelineMixin._candidate_summaries([stored, unscored])

    assert [summary["adversarial_score"] for summary in summaries] == [0.812, 0.457]
    assert scored == ["只有一份候选的运行。"], "存着分的那一份不再现算"
    assert not [summary for summary in summaries if "scores_withheld" in summary]
    assert list(summaries[0])[:3] == ["row_id", "rank", "adversarial_score"]


def test_candidate_views_carry_no_dispersion_reading(client, session) -> None:
    """重评 R2：候选离散度不再写（补写那一套随先中性后润色删了），两个 GET 都不再带它的读数。"""
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="w3_diag",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            stage="style_draft",
            content="诊断视图里的一份风格稿。",
            source_bundle_id="bundle_w3",
            source_bundle_hash="hash_w3",
        )
    )
    session.commit()

    diagnostic = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates").json()["data"]
    assert diagnostic["blinded"] is False
    assert [c["row_id"] for c in diagnostic["candidates"]] == ["w3_diag"]
    assert "dispersion_score" not in diagnostic and "dispersion_signal" not in diagnostic

    _seed_manual_gate(session, ["w3_cand_a"], ["w3_cand_a"])
    blinded = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates").json()["data"]
    assert blinded["blinded"] is True
    assert "dispersion_score" not in blinded and "dispersion_signal" not in blinded


# ---------- 终选锁定 ----------


def test_a_different_second_selection_is_locked_and_names_no_reopen(client, session) -> None:
    """终选一次写入：同选幂等；换一份 409 SELECTION_LOCKED，提示不再指向已经删掉的「重开」（重评 R2）。"""
    _seed_scene(session)
    _seed_manual_gate(session, ["w3_cand_a", "w3_cand_b"], ["w3_cand_b", "w3_cand_a"])
    path = f"/api/v1/scenes/{SCENE_ID}/style-candidates"

    first = client.post(f"{path}/w3_cand_a/select", json={}, headers={"X-Idempotency-Key": "w3-lock-first"})
    assert first.status_code == 200
    same = client.post(f"{path}/w3_cand_a/select", json={}, headers={"X-Idempotency-Key": "w3-lock-same"})
    assert same.status_code == 200 and same.json()["data"]["message"] == "Candidate already selected"

    other = client.post(f"{path}/w3_cand_b/select", json={}, headers={"X-Idempotency-Key": "w3-lock-other"})
    assert other.status_code == 409
    error = other.json()["error"]
    assert error["code"] == "SELECTION_LOCKED"
    assert "reopen" not in error["message"].lower()
    assert error["details"]["selected_row_id"] == "w3_cand_a"


def test_selecting_without_a_gate_records_a_select_only_gate(client, session) -> None:
    """标准场直接终选时补建的已决门只允许 select——终选没有「重开改选」（重评 R2 复核补充 4）。"""
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="w3_standard",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            stage="style_draft",
            content="标准场的一份风格稿。",
            source_bundle_id="bundle_w3",
            source_bundle_hash="hash_w3",
        )
    )
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/w3_standard/select",
        json={},
        headers={"X-Idempotency-Key": "w3-select-no-gate"},
    )
    assert response.status_code == 200
    session.expire_all()
    gates = session.execute(
        select(HumanReviewEvent).where(
            HumanReviewEvent.scene_id == SCENE_ID,
            HumanReviewEvent.event_source == "candidate_selection",
        )
    ).scalars().all()
    assert len(gates) == 1
    assert gates[0].allowed_actions_json == ["select"]
    assert gates[0].details_json["selected_row_id"] == "w3_standard"


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


class _SecondRevisionRepeatsTheFirstClient(FakeSceneClient):
    """槽位 2 的定向修改回了与槽位 1 一字不差的一段（第 3 次回稿 = 第 2 次）：两份读数一样，并列排在最前。"""

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = 2 if len(self.requests) == 3 else len(self.requests)
        payload = {
            "scene_text": f"Provider-generated draft #{index} for terminal selection.",
            "continuity_notes": [],
        }
        return _response(payload, request_id=f"resp_scene_{len(self.requests):03d}")


def test_resume_uses_contiguous_hashes_when_a_middle_candidate_is_filtered(
    client,
    session,
) -> None:
    """排在中间的候选（与排第一的那份正文重复）不交给作者：终选清单与候选排序不再同序，续跑按终选清单的连续哈希核对。

    （[批准#12] 之后受保护专名不再淘汰候选；终选门只剩正文为空 / 重复与抄袭门三种淘汰，这里用重复。）"""
    _seed_scene(session)
    _make_orchestrator(session, scene_client=_SecondRevisionRepeatsTheFirstClient()).run_scene(
        SCENE_ID, execution_id=ORIGIN_EXECUTION_ID
    )
    session.commit()
    gate = _selection_gate(session)
    offered = gate.details_json["candidate_row_ids"]
    state = session.get(SceneRunState, SCENE_ID)
    all_candidates = state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]
    assert len(offered) < len(all_candidates)
    chosen_row_id = offered[-1]
    # 选的那份在终选清单里的位置与它在候选排序里的位置不同：核对只能按终选清单的连续编号
    assert offered.index(chosen_row_id) != all_candidates.index(chosen_row_id)

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

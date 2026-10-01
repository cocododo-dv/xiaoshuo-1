"""场景编排器的几处行为修正（P01c）：每条都先写成红的，再改代码。

- 终选门只按抄袭门淘汰候选，受保护专名从不淘汰（[批准#12]，B04-15）；
- ``run_policy="auto"`` 不再接收（B01-09）；
- 准终稿第一轮评审 / 软 QC 第一轮之后的分支表存与读共用一份（B01-04）；
- 终选后续跑停在严格模式的待接受稿上算完成，与首跑同一张终态表（B01-05）；
- 终选后续跑交给后半程的选中稿是 ``StyleGenerationResult``（B01-18）；
- 严格停点的结果以 ``base_result`` 的五个键开头，``recommended_actions`` 只是作者状态投影给的那一条（B01-15 的
  待定问题定为不追加 ``author_review_optional_fix``；这一条钉住决定，不是修正）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from novel_system.db.models import SceneBundle, SceneCard, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_generation import StyleGenerationResult
from tests.real_llm_fakes import install_online_pipeline
from tests.support.checkpoint_fakes import _seed_resume_scene
from tests.support.style_first_fixtures import install_readings, reading
from tests.support.candidate_gate import (
    make_orchestrator as _make_gate_orchestrator,
    ORIGIN_EXECUTION_ID as GATE_ORIGIN_EXECUTION_ID,
    SCENE_ID as GATE_SCENE_ID,
    seed_scene as _seed_gate_scene,
    selection_gate as _selection_gate,
)
from tests.support.strict_qc import (
    FakeSequenceQcClient,
    make_orchestrator as _make_strict_orchestrator,
    near_final_fail as _near_final_fail,
    SCENE_ID as STRICT_SCENE_ID,
    seed_scene as _seed_strict_scene,
)

SCENE_ID = "CH_RESUME_SC01"


def test_blind_selection_gate_never_drops_a_candidate_for_a_protected_term(session, monkeypatch) -> None:
    """受保护专名处处只提醒（[批准#12]）：终选门只按候选排序时抄袭门的结论淘汰，含专名的候选照样交给作者。"""
    monkeypatch.setenv("NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON", json.dumps(["旧信"]))
    _seed_resume_scene(session)
    named = SimpleNamespace(
        row_id="named",
        content="她把旧信塞回案卷，没有回头。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": True},
    )
    other = SimpleNamespace(
        row_id="other",
        content="雨城的钟敲过三下，门外没有人。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": True},
    )

    offered = Orchestrator(session)._offer_candidates_for_selection(
        session.get(SceneCard, SCENE_ID),
        session.get(SceneRunState, SCENE_ID),
        {},
        [named, other],
    )

    assert offered == ["named", "other"]


def test_run_policy_auto_is_rejected(client, session) -> None:
    """``auto`` 从没有调用方传过、编排器只按 reliable 处理：和别的未知值一样 422。"""
    _seed_resume_scene(session)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/run/full",
        json={"run_policy": "auto"},
        headers={"X-Idempotency-Key": "p01c-run-policy-auto"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RUN_POLICY"


# ---------------------------------------------------------------------------------------------- B01-04


def _checkpoint_stub(monkeypatch, refs: dict, hashes: dict) -> Orchestrator:
    """只带一份检查点 JSON 的编排器（分支控制的读端只看 artifact_refs / artifact_hashes）。"""
    orchestrator = object.__new__(Orchestrator)
    state = SimpleNamespace(run_checkpoint_json={"artifact_refs": refs, "artifact_hashes": hashes})
    monkeypatch.setattr(orchestrator, "_active_checkpoint_state", lambda: state)
    return orchestrator


def test_near_final_eval0_that_asks_for_human_review_is_never_auto_rewritten_and_resumes(monkeypatch) -> None:
    """评审同时要人工复核又要求重写（near_final 现在不会这样给，但没有谁守着这条跨模块的不变式）：存的一边以前
    先看预算、钱够就记 ``human_review_proposal`` 却照样重写，续跑与终稿复验都判这份控制损坏。现在存与读共用
    一张分支表：交人工复核的不自动重写，存下的控制续跑时认得。"""
    eval0 = {"pass_flag": False, "should_rewrite": True, "requires_human_review": True, "evaluation_id": "ev0"}
    orchestrator = object.__new__(Orchestrator)
    orchestrator.near_final_service = SimpleNamespace(evaluate_scene=lambda *_args, **_kwargs: dict(eval0))
    orchestrator.scene_generation_service = SimpleNamespace(
        generate_near_final_rewrite=lambda *_args, **_kwargs: pytest.fail("a human-review proposal must not be auto-rewritten")
    )
    saved: dict = {}
    monkeypatch.setattr(orchestrator, "_near_final_checkpoint_progress", lambda: -1)
    monkeypatch.setattr(orchestrator, "_reconcile_execution_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_save_near_evaluation_checkpoint", lambda **kwargs: saved.update(kwargs))

    outcome = orchestrator._ensure_near_final_subcheckpoints(
        scene=SimpleNamespace(scene_id=SCENE_ID),
        bundle={},
        source_generation=SimpleNamespace(row_id="source", content="雨城的钟敲过三下。"),
        optional_spend_allowed=lambda: True,
    )

    assert outcome[2:] == (0, "human_review_proposal", None)
    control = saved["control"]
    assert control["branch"] == "human_review_proposal" and control["rewrite_allowed"] is False
    resumed = _checkpoint_stub(
        monkeypatch,
        refs={"near_eval0_control": control},
        hashes={"near_eval0_control": Orchestrator._json_hash(control)},
    )
    assert resumed._load_near_eval0_control(eval0) == control


def test_soft_qc0_control_on_resume_must_match_the_branch_table(monkeypatch) -> None:
    """软 QC 第一轮之后的控制：读端以前只查「不修补就不能记允许」，跳过原因与分支对不上也放行。现在与存的一边
    同一张表：一轮判「继续」的决定配上「预算不够」的跳过原因是损坏；表里给的那份照常认。"""
    decision = SimpleNamespace(branch="continue")
    mismatched = {"patch_allowed": False, "skip_reason": "budget_or_candidate_cap"}
    orchestrator = _checkpoint_stub(
        monkeypatch,
        refs={"soft_qc0_control": mismatched},
        hashes={"soft_qc0_control": Orchestrator._json_hash(mismatched)},
    )
    with pytest.raises(DomainError) as exc_info:
        orchestrator._load_soft_qc0_branch_control(decision)
    assert exc_info.value.code == "RUN_CHECKPOINT_CORRUPT"

    table = {"patch_allowed": False, "skip_reason": "no_patch_requested"}
    orchestrator = _checkpoint_stub(
        monkeypatch,
        refs={"soft_qc0_control": table},
        hashes={"soft_qc0_control": Orchestrator._json_hash(table)},
    )
    assert orchestrator._load_soft_qc0_branch_control(decision) == (False, "no_patch_requested")


def test_branch_tables_only_allow_the_step_on_its_own_branch() -> None:
    """两张分支表的每一种组合：允许改（重写 / 修补）当且仅当分支就是改，改的时候没有跳过原因；存的一边写下的
    控制续跑时认得（等于两种「还能不能花钱」之一）。"""
    from itertools import product

    from novel_system.services.scene_run.branch_control import (
        is_derivable_control,
        near_final_eval0_control,
        soft_qc0_control,
    )

    for pass_flag, should_rewrite, human, spend in product((False, True), repeat=4):
        eval0 = {"pass_flag": pass_flag, "should_rewrite": should_rewrite, "requires_human_review": human}
        control = near_final_eval0_control(eval0, spend_allowed=spend)
        assert control["rewrite_allowed"] == (control["branch"] == "rewrite")
        assert (control["skip_reason"] is None) == control["rewrite_allowed"]
        assert is_derivable_control(control, lambda **kw: near_final_eval0_control(eval0, **kw))
    for branch in ("continue", "patch", "waive", "human_review_required", "accepted_soft_risk"):
        for spend in (False, True):
            control = soft_qc0_control(branch, spend_allowed=spend)
            assert control["patch_allowed"] == (branch == "patch" and spend)
            assert (control["skip_reason"] is None) == control["patch_allowed"]


# ---------------------------------------------------------------------------------------------- B01-05


def test_strict_stop_after_selection_resume_completes_the_execution(session, monkeypatch) -> None:
    """终选后续跑（界面总按 strict 发）停在一份待作者接受 Q2/Q3 的稿子上：与首跑一样是成功的终点。以前续跑只把
    ``archived`` 记成完成，这里记成 ``failed``，下一次续跑还会把它当可重试的失败接手。"""
    _seed_resume_scene(session)
    parent = "idempotency:strict-selection-origin"
    state = session.get(SceneRunState, SCENE_ID)
    state.run_checkpoint = "selection_wait"
    state.run_execution_status = "waiting_selection"
    state.active_execution_id = parent
    state.run_checkpoint_json = {
        "execution_id": parent,
        "node_key": "selection_wait",
        "artifact_refs": {},
        "artifact_hashes": {},
    }
    session.commit()
    monkeypatch.setattr(
        Orchestrator,
        "_resume_after_selection_pipeline",
        lambda self, scene_id: {"scene_status": "quality_warning_pending_acceptance"},
    )

    result = Orchestrator(session).resume_after_selection(SCENE_ID, execution_id="idempotency:strict-selection-resume")

    assert result["scene_status"] == "quality_warning_pending_acceptance"
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert state.active_execution_id == "idempotency:strict-selection-resume"
    assert state.run_execution_status == "completed"


# ---------------------------------------------------------------------------------------------- B01-15


def test_strict_stop_starts_with_the_base_result_and_keeps_only_the_projection_action(session, monkeypatch) -> None:
    """严格停点（准终稿两轮都没过、留下 Q2 警告）：结果以 base_result 的五个键开头；recommended_actions 就是作者状态
    投影给的 adopt_or_patch——不追加归档结果才有的 author_review_optional_fix（稿子没归档，作者本来就要读完警告再
    采纳或改；每条准终稿警告自己写着 recommended_action）。同样的两轮不过在 reliable 下会归档并追加那一条。"""
    install_online_pipeline(monkeypatch)
    _seed_strict_scene(session, must_include="")
    orchestrator = _make_strict_orchestrator(
        session,
        near_final_client=FakeSequenceQcClient([_near_final_fail(), _near_final_fail("重写后仍结构不足")]),
    )

    result = orchestrator.run_scene(STRICT_SCENE_ID, run_policy="strict")
    session.commit()

    state = session.get(SceneRunState, STRICT_SCENE_ID)
    bundle = session.get(SceneBundle, state.current_bundle_id)
    assert {
        key: result[key]
        for key in (
            "scene_status",
            "current_bundle_id",
            "current_bundle_hash",
            "current_qc_report_id",
            "current_human_review_event_id",
        )
    } == {
        "scene_status": "quality_warning_pending_acceptance",
        "current_bundle_id": bundle.bundle_id,
        "current_bundle_hash": bundle.bundle_snapshot_hash,
        "current_qc_report_id": state.current_qc_report_id,
        "current_human_review_event_id": state.current_human_review_event_id,
    }
    assert result["recommended_actions"] == ["adopt_or_patch"]
    near_final_warnings = [
        item for item in result["quality_warnings"] if str(item.get("issue_key") or "").startswith("near_final_")
    ]
    assert near_final_warnings
    assert all(item["recommended_action"] == "author_review_optional_fix" for item in near_final_warnings)


# ---------------------------------------------------------------------------------------------- B01-18


@pytest.fixture
def _selection_gate_pipeline(monkeypatch) -> None:
    """与 test_candidate_selection_gate 相同的场面：在线记账替身、三份候选、合成参考书的读数、预算解除武装。"""
    install_online_pipeline(monkeypatch)

    def _three_candidates(self, contract, *, criticality=None):  # noqa: ANN001, ANN202
        if criticality is not None:
            return max(1, min(3, int(criticality.initial_best_of_n)))
        return 3

    monkeypatch.setattr(Orchestrator, "_best_of_n_count", _three_candidates)
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    install_readings(monkeypatch, {"draft #2": reading(0.5, 30.0)}, default=reading(1.0, 60.0))


@pytest.mark.usefixtures("_selection_gate_pipeline")
def test_selection_resume_hands_a_typed_style_generation_to_the_finalizer(client, session, monkeypatch) -> None:
    """终选后续跑交给后半程（软 QC → 准终稿 → 归档）的选中稿以前是只带五个字段的 SimpleNamespace，缺 bundle_id /
    bundle_hash / lineage / ranking_audit——后半程将来谁读这些字段，只在续跑这条路上出错。现在与首跑同一种值。"""
    _seed_gate_scene(session)
    _make_gate_orchestrator(session).run_scene(GATE_SCENE_ID, execution_id=GATE_ORIGIN_EXECUTION_ID)
    session.commit()
    chosen_row_id = _selection_gate(session).details_json["candidate_row_ids"][0]
    selected = client.post(
        f"/api/v1/scenes/{GATE_SCENE_ID}/style-candidates/{chosen_row_id}/select",
        json={},
        headers={"X-Idempotency-Key": "p01c-typed-selection"},
    )
    assert selected.status_code == 200
    handed: dict = {}

    def capture(self, **kwargs):  # noqa: ANN001, ANN202
        handed.update(kwargs)
        return {"scene_status": "archived"}

    monkeypatch.setattr(Orchestrator, "_finalize_after_style", capture)

    Orchestrator(session).resume_after_selection(GATE_SCENE_ID, execution_id="idempotency:p01c-typed-resume")

    generation = handed["style_generation"]
    bundle = session.get(SceneBundle, handed["bundle"]["bundle_id"])
    assert isinstance(generation, StyleGenerationResult)
    assert generation.row_id == chosen_row_id
    assert (generation.bundle_id, generation.bundle_hash) == (bundle.bundle_id, bundle.bundle_snapshot_hash)
    assert generation.llm_call_id and generation.execution_step_key and generation.artifact_execution_id

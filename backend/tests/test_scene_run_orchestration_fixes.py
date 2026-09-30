"""场景编排器的几处行为修正（P01c）：每条都先写成红的，再改代码。

- 终选门只按抄袭门淘汰候选，受保护专名从不淘汰（[批准#12]，B04-15）；
- ``run_policy="auto"`` 不再接收（B01-09）；
- 准终稿第一轮评审 / 软 QC 第一轮之后的分支表存与读共用一份（B01-04）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from tests.support.checkpoint_fakes import _seed_resume_scene

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

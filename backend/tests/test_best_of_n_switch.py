"""2026-09-14 减法:Best-of-N 从「测试才能打开」变成显式开关。

`NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED` 默认关闭 → 永远 1 个候选(现状);打开后按场景关键度
决定候选数,关键场停在盲选终选门(既有 test_candidate_selection_gate 覆盖门本身)。
2026-09-30 [批准#2]:只有作者手笔直起才出多稿——其余起草方式即使开关打开也只起一稿;补候选的上限
(``_best_of_n_max_count`` / ``_best_of_n_policy_cap``)随先中性后润色的多稿删掉。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_criticality import SceneCriticality
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.settings import get_settings

from tests.support.checkpoint_fakes import _CountingGenerationClient, _HardPassClient, _seed_resume_scene

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def _criticality(level: str, *, initial: int, human_gate: bool) -> SceneCriticality:
    return SceneCriticality(
        level=level,
        reasons=[level],
        skip_critique=False,
        human_gate=human_gate,
        initial_best_of_n=initial,
    )


def test_switch_off_always_drafts_one_candidate(session, monkeypatch) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", raising=False)
    orchestrator = Orchestrator(session)
    critical = _criticality("critical", initial=3, human_gate=True)
    assert orchestrator._best_of_n_count(None, criticality=critical) == 1
    assert not hasattr(orchestrator, "_best_of_n_policy_cap")


def test_switch_on_follows_scene_criticality(session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", "true")
    assert get_settings().scene_best_of_n_enabled is True
    orchestrator = Orchestrator(session)
    transition = _criticality("transition", initial=1, human_gate=False)
    standard = _criticality("standard", initial=2, human_gate=False)
    critical = _criticality("critical", initial=3, human_gate=True)
    assert orchestrator._best_of_n_count(None, criticality=transition) == 1
    assert orchestrator._best_of_n_count(None, criticality=standard) == 2
    assert orchestrator._best_of_n_count(None, criticality=critical) == 3
    # 没有关键度信息时仍然只出一个候选
    assert orchestrator._best_of_n_count(None, criticality=None) == 1


def test_switch_on_still_drafts_one_candidate_without_style_first(session, monkeypatch) -> None:
    """没绑作者手笔直起的场景（先中性后润色），开关打开、标准场也只起一稿；检查点如实记一份候选。"""
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", "true")
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.constraint_intensity = 0.5  # 标准场：开关打开时关键度给 2 份
    session.commit()
    generation_client = _CountingGenerationClient()

    result = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id="idempotency:best-of-n-neutral-first")

    assert result["scene_status"] == "archived"
    refs = session.get(SceneRunState, "CH_RESUME_SC01").run_checkpoint_json["artifact_refs"]
    assert refs["style_initial_candidate_count"] == 1
    assert len(refs["candidate_row_ids"]) == 1

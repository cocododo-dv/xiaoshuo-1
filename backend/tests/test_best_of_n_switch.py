"""2026-09-14 减法:Best-of-N 从「测试才能打开」变成显式开关。

`NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED` 默认关闭 → 永远 1 个候选(现状);打开后按场景关键度
决定候选数与上限,关键场停在盲选终选门(既有 test_candidate_selection_gate 覆盖门本身)。
"""

from __future__ import annotations

from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_criticality import SceneCriticality
from novel_system.settings import get_settings


def _criticality(level: str, *, initial: int, best_of_n: int, human_gate: bool) -> SceneCriticality:
    return SceneCriticality(
        level=level,
        reasons=[level],
        best_of_n=best_of_n,
        skip_critique=False,
        human_gate=human_gate,
        initial_best_of_n=initial,
    )


def test_switch_off_always_drafts_one_candidate(session, monkeypatch) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", raising=False)
    orchestrator = Orchestrator(session)
    critical = _criticality("critical", initial=3, best_of_n=5, human_gate=True)
    assert orchestrator._best_of_n_count(None, criticality=critical) == 1
    assert orchestrator._best_of_n_policy_cap == 1
    assert orchestrator._best_of_n_max_count(criticality=critical, initial_count=1) == 1


def test_switch_on_follows_scene_criticality(session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", "true")
    assert get_settings().scene_best_of_n_enabled is True
    orchestrator = Orchestrator(session)
    transition = _criticality("transition", initial=1, best_of_n=1, human_gate=False)
    standard = _criticality("standard", initial=2, best_of_n=3, human_gate=False)
    critical = _criticality("critical", initial=3, best_of_n=5, human_gate=True)
    assert orchestrator._best_of_n_count(None, criticality=transition) == 1
    assert orchestrator._best_of_n_count(None, criticality=standard) == 2
    assert orchestrator._best_of_n_max_count(criticality=standard, initial_count=2) == 3
    assert orchestrator._best_of_n_count(None, criticality=critical) == 3
    assert orchestrator._best_of_n_max_count(criticality=critical, initial_count=3) == 5
    # 没有关键度信息时仍然只出一个候选
    assert orchestrator._best_of_n_count(None, criticality=None) == 1

"""场景编排器的几处行为修正（P01c）：每条都先写成红的，再改代码。

- 终选门只按抄袭门淘汰候选，受保护专名从不淘汰（[批准#12]，B04-15）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from novel_system.db.models import SceneCard, SceneRunState
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

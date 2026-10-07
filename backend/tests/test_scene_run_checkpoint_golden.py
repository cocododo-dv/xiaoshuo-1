"""Scene-run checkpoint 写端 golden：检查点写端的形状不漂移。

``format_*.json``：现在的代码把几种典型的一场跑完，逐次保存与归档后的检查点 JSON 规范化后比对（来历与规矩见
:mod:`tests.support.checkpoint_golden`）。检查点格式有意变化时用 ``CHECKPOINT_FORMAT_GOLDEN_REGEN=1`` 重生成，并在
提交里说明原因。

读端只认现在的格式：重构前的代码（@60443dc）写下的 ``resume_*.json`` 整库转储与它们的续跑用例随旧格式读取器一起
删了（格式 v2 的「双读一版」已过；部署前用前置查询确认没有停在半路的旧运行）。现在格式的续跑由
``test_scene_run_checkpoint_*`` 各套件的进程内停下 / 续跑用例覆盖。
"""

from __future__ import annotations

import os

import pytest

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _HardPassClient,
    _SequencedNearFinal,
    _SequencedSoftQc,
    _seed_resume_scene,
)
from tests.support.checkpoint_golden import (
    GOLDEN_DIR,
    Canonicalizer,
    read_json,
    write_json,
)

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")

SCENE_ID = "CH_RESUME_SC01"


def _orchestrator(session, generation_client, **services) -> Orchestrator:
    return Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        **services,
    )


# ---------------------------------------------------------------------------------------------- 写端 golden


class _SaveTrace:
    """逐次记下检查点保存：节点、子游标、策略 / 分支，与这一步写进来的引用 / 哈希键。"""

    def __init__(self, monkeypatch) -> None:
        self.saves: list[dict] = []
        original = SceneRunCheckpointService.save_checkpoint
        trace = self

        def recording(service, **kwargs):  # noqa: ANN001, ANN202
            trace.saves.append(
                {
                    "node_key": kwargs.get("node_key"),
                    "sub_index": kwargs.get("sub_index"),
                    "strategy": kwargs.get("strategy"),
                    "branch": kwargs.get("branch"),
                    "refs": sorted((kwargs.get("artifact_refs") or {}).keys()),
                    "hashes": sorted((kwargs.get("artifact_hashes") or {}).keys()),
                }
            )
            return original(service, **kwargs)

        monkeypatch.setattr(SceneRunCheckpointService, "save_checkpoint", recording)


def _run_full(session, *, chapter_last: bool = False, run_policy: str = "reliable", **services) -> dict:
    _seed_resume_scene(session)
    if chapter_last:
        scene = session.get(SceneCard, SCENE_ID)
        scene.is_chapter_last = 1
        session.commit()
    return _orchestrator(session, _CountingGenerationClient(), **services).run_scene(
        SCENE_ID, run_policy=run_policy, execution_id="idempotency:checkpoint-format"
    )


_FORMAT_SCENARIOS = {
    "format_full_run": lambda session: _run_full(session),
    "format_chapter_last": lambda session: _run_full(session, chapter_last=True),
    "format_patch_and_rewrite": lambda session: _run_full(
        session,
        soft_qc_engine=_SequencedSoftQc(session, {"soft_qc:0": "patch", "soft_qc:1": "continue"}),
        near_final_service=_SequencedNearFinal(
            session, {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"}
        ),
    ),
    "format_strict_stop": lambda session: _run_full(
        session,
        run_policy="strict",
        near_final_service=_SequencedNearFinal(session, {"near_final_acceptance:0": "human"}),
    ),
}


@pytest.mark.parametrize("scenario", sorted(_FORMAT_SCENARIOS))
def test_checkpoint_writer_format_matches_the_golden(session, monkeypatch, scenario) -> None:
    trace = _SaveTrace(monkeypatch)
    result = _FORMAT_SCENARIOS[scenario](session)
    state = session.get(SceneRunState, SCENE_ID)
    session.refresh(state)
    canon = Canonicalizer()
    observed = {
        "scene_status": result["scene_status"],
        "run_checkpoint": state.run_checkpoint,
        "run_execution_status": state.run_execution_status,
        "saves": canon.value(trace.saves),
        "checkpoint": canon.value(state.run_checkpoint_json),
    }
    path = GOLDEN_DIR / f"{scenario}.json"
    if os.environ.get("CHECKPOINT_FORMAT_GOLDEN_REGEN") == "1":
        write_json(path, observed)
    assert observed == read_json(path)

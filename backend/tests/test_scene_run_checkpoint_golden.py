"""Scene-run checkpoint resume · goldens：旧代码写下的检查点照样续跑，检查点写端的形状不漂移。

两种 golden 的来历与规矩见 :mod:`tests.support.checkpoint_golden`。

- ``resume_*.json``（读端）：重构前的代码（``refactor/2026-09-29`` @ 60443dc）把 ``CH_RESUME_SC01``（夹具
  ``_seed_resume_scene``，场景生成 ``_CountingGenerationClient``、硬 QC ``_HardPassClient``，其余节点走在线记账替身）
  跑到某个检查点停下时的整库行。停法都是现成测试用的那几种，跑失败后执行记为 ``failed``，库里留着停下那一刻的检查点：

  - ``resume_planning_sub1``：``planning:character_pressure`` 步位前抛错 → ``planning_ready`` 子游标 1；
  - ``resume_soft_qc_sub0``：``soft_qc:0`` 前抛错 → ``soft_qc_ready`` 子游标 0（自动批评产品与 ``soft_input``）；
  - ``resume_near_final_sub0_rewrite``：准终稿 eval0 要重写（``_PassSoftQc`` + ``_SequencedNearFinal``），
    ``near_final_rewrite:0`` 前抛错 → ``near_final_ready`` 子游标 0（带 ``near_eval0_control``）；
  - ``resume_near_final_sub3``：``archiver`` 换成第一次就失败的替身 → 子游标 3（终稿已落、归档一步没做）；
  - ``resume_archive_sub6``：归档第 7 步（向量索引）抛错 → 子游标 6，续跑要走第 7 步再到 8–11；
  - ``resume_archive_sub7_vector_non_persistent``：第 8 步抛错 → 子游标 7，里面是一份历史上的 ``non_persistent``
    向量产品（memory 后端）；
  - ``resume_chapter_last_archive_sub9``：章末场，第 10 步（章级准终稿评审）抛错 → 子游标 9（章汇总 / 卷汇总的产品）；
  - ``resume_archived_replay``：整场跑完、已归档；续跑是已归档的快路径（整张清单复验）。

  这里灌回空库，用现在的代码按同一个执行 id 续跑到归档，并核对：已经做过的步骤不再调模型、不多写一份产品。
  ``expect.generation_requests`` 是捕获时在同一份旧代码上续跑一次记下的场景生成调用数。
- ``format_*.json``（写端）：现在的代码把几种典型的一场跑完，逐次保存与归档后的检查点 JSON 规范化后比对。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterMemory,
    FinalScene,
    LlmCall,
    SceneBlueprint,
    SceneCard,
    SceneMemory,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _HardPassClient,
    _PassSoftQc,
    _SequencedNearFinal,
    _SequencedSoftQc,
    _seed_resume_scene,
)
from tests.support.checkpoint_golden import (
    GOLDEN_DIR,
    Canonicalizer,
    read_json,
    restore_database,
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


def _count(session, model, *conditions) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)


# ---------------------------------------------------------------------------------------------- 读端 golden


def _resume_goldens() -> list[str]:
    return sorted(path.stem for path in GOLDEN_DIR.glob("resume_*.json"))


def _resume_services(session, scenario: str) -> dict:
    """续跑时的服务替身与捕获时相同（捕获时显式注入过的，续跑照样注入）。"""
    if scenario == "resume_near_final_sub0_rewrite":
        return {
            "soft_qc_engine": _PassSoftQc(session),
            "near_final_service": _SequencedNearFinal(
                session,
                {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
            ),
        }
    return {}


@pytest.mark.parametrize("scenario", _resume_goldens())
def test_checkpoint_written_by_the_pre_refactor_code_resumes_to_archive(session, scenario) -> None:
    golden = read_json(GOLDEN_DIR / f"{scenario}.json")
    restore_database(session, golden["rows"])
    execution_id = golden["execution_id"]
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint == golden["stopped_at"]["run_checkpoint"]
    assert (state.run_checkpoint_json or {}).get("sub_index") == golden["stopped_at"]["sub_index"]
    calls_before = _count(session, LlmCall, LlmCall.execution_id == execution_id)
    blueprints_before = _count(session, SceneBlueprint, SceneBlueprint.scene_id == SCENE_ID)

    generation_client = _CountingGenerationClient()
    result = _orchestrator(session, generation_client, **_resume_services(session, scenario)).run_scene(
        SCENE_ID, execution_id=execution_id
    )

    assert result["scene_status"] == "archived"
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert state.scene_status == "archived"
    # 做过的步骤不重做：场景生成客户端只为还没做的生成步调模型
    assert len(generation_client.requests) == golden["expect"]["generation_requests"]
    assert _count(session, SceneBlueprint, SceneBlueprint.scene_id == SCENE_ID) == blueprints_before
    assert _count(session, FinalScene, FinalScene.scene_id == SCENE_ID) == 1
    assert _count(session, SceneMemory, SceneMemory.scene_id == SCENE_ID) == 1
    if golden["stopped_at"]["run_checkpoint"] == "archived":
        # 已归档的重放走快路径：整张清单复验，一次模型调用都不多
        assert _count(session, LlmCall, LlmCall.execution_id == execution_id) == calls_before
    manifest = state.run_checkpoint_json["artifact_refs"]["archive_manifest"]
    assert [entry["sub_index"] for entry in manifest] == list(range(4, 12))
    scene = session.get(SceneCard, SCENE_ID)
    if scene.is_chapter_last == 1:
        assert _count(session, ChapterMemory, ChapterMemory.chapter_id == scene.chapter_id, ChapterMemory.aggregate_stage == "final") == 1
        assert _count(session, WriterEvaluation, WriterEvaluation.object_type == "chapter", WriterEvaluation.object_id == scene.chapter_id) == 1


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

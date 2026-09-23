"""风格参考 v3 · P5b：编排器里的风格步 / 补丁去留 / 归档读数（检查点校验与续跑），对照检查作业，读数接口，旧回测下线。

编排器用 ``test_scene_run_checkpoint_resume`` 的记账替身（首稿 / 补丁走真实的 LLMNodeRunner + 假供应商，软 QC 与准定稿
是写真实账本行的替身）；读数的数值由替身按文字给定。全部是合成文本。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    QcReport,
    SceneDraft,
    SceneRunState,
    StyleFidelityReading,
    StyleReferenceJob,
)
from novel_system.db.session import SessionLocal
from novel_system.services import scene_generation as sg
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.orchestrator import STYLE_PATCH_REVERTED_SKIP_REASON, Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_reference import check_job
from novel_system.services.style_reference import readings as R
from novel_system.services.style_reference import style_step as S
from novel_system.services.style_reference.jobs import JOB_KIND_CHECK, register_job_handler, run_job_inline
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import install_online_pipeline
from tests.style_reference_inject_helpers import bind, seed_reference
from tests.test_scene_run_checkpoint_resume import (
    _CountingGenerationClient,
    _FailNearFinal,
    _HardPassClient,
    _PassNearFinal,
    _SequencedSoftQc,
    _seed_resume_scene,
)
from tests.test_style_fidelity_v3 import PACING_OUT, _install_readings, _reading

SCENE_ID = "CH_RESUME_SC01"


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    install_online_pipeline(monkeypatch)
    register_job_handler(JOB_KIND_CHECK, check_job.run_check_job)
    # 首稿带全部样例窗（数万字），套件默认的武装场景预算装不下；产品默认本来就是解除武装
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")


class _JudgedSoftQc(_SequencedSoftQc):
    """每一轮软 QC 在报告里带参考评审分（10 分制的 reference_judge 条目，与真实软 QC 落库的形状相同）。"""

    def __init__(self, session, branches: dict[str, str], judges: dict[str, float]) -> None:
        super().__init__(session, branches)
        self.judges = judges

    def evaluate(self, **kwargs):  # noqa: ANN003
        decision = super().evaluate(**kwargs)
        self.session.flush()
        report = self.session.get(QcReport, decision.qc_report_id)
        score = self.judges.get(kwargs.get("execution_step_key", "soft_qc:0"))
        if score is not None and not any(
            isinstance(entry, dict) and entry.get("kind") == "reference_judge" for entry in report.rewrite_brief_json or []
        ):
            report.rewrite_brief_json = [
                *list(report.rewrite_brief_json or []),
                {"kind": "reference_judge", "scale": "0-10", "style_score": score, "dimension_scores": {"scene.dialogue": score}},
            ]
            self.session.flush()
        return decision


def _bind_resume_project(session) -> str:
    _seed_resume_scene(session)
    _book, profile_id = seed_reference(session, "fid_orch", chapters=14, per_chapter=100)
    bind(session, profile_id, binding_id="bind_fid_orch", scope="project", scope_ref_id="P_RESUME")
    return profile_id


def _orchestrator(session, generation_client, soft_qc, near_final) -> Orchestrator:
    return Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )


def _readings(session) -> list[StyleFidelityReading]:
    return list(
        session.scalars(
            select(StyleFidelityReading)
            .where(StyleFidelityReading.scene_id == SCENE_ID)
            .order_by(StyleFidelityReading.created_at)
        )
    )


# ---------------------------------------------------------------------------
# 编排器：首稿即风格稿 → 软 QC → 准定稿 → 归档读数
# ---------------------------------------------------------------------------


def test_accepted_first_draft_runs_through_soft_qc_near_final_and_archive(session, monkeypatch) -> None:
    profile_id = _bind_resume_project(session)
    _install_readings(monkeypatch, {}, default=_reading(0.8, 35.0))
    generation_client = _CountingGenerationClient()
    orchestrator = _orchestrator(
        session, generation_client, _JudgedSoftQc(session, {"soft_qc:0": "continue"}, {"soft_qc:0": 8.4}), _PassNearFinal(session)
    )

    result = orchestrator.run_scene(SCENE_ID, execution_id="idempotency:fid-accept")
    session.commit()

    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == 1, "首稿一次调用；风格步不调模型"
    state = session.get(SceneRunState, SCENE_ID)
    style_row = session.get(SceneDraft, state.current_style_draft_row_id)
    first_row = session.get(SceneDraft, state.current_neutral_draft_row_id)
    assert style_row.stage == "style_draft" and style_row.content == first_row.content
    assert style_row.generation_llm_call_id == first_row.generation_llm_call_id
    rows = _readings(session)
    assert [(r.source, r.stage) for r in rows] == [("pipeline", "first_draft"), ("pipeline", "final")]
    final = rows[-1]
    assert final.profile_id == profile_id and final.draft_ref == state.current_final_scene_row_id
    assert final.judge_json["overall"] == 8.4 and final.judge_json["source"] == "reference_judge"
    product = state.run_checkpoint_json["artifact_refs"]["archive_drift_product"]
    assert product["outcome"] == "recorded" and product["reading_id"] == final.reading_id

    # 工作台生成摘要：本次运行的「像不像」
    from novel_system.services.style_fidelity_view import current_run_style_fidelity

    summary = current_run_style_fidelity(session, SCENE_ID, state.current_bundle_id)
    assert summary["style_step"]["decision"] == S.DECISION_FIRST_DRAFT_ACCEPTED
    assert summary["first_draft"]["reading_id"] == rows[0].reading_id
    assert summary["final"]["reading_id"] == final.reading_id and summary["judge"]["overall"] == 8.4
    assert summary["patch"] is None and summary["revision"] is None


@pytest.mark.parametrize(
    ("revision_distance", "expected_decision", "expected_source"),
    [
        (1.0, S.DECISION_REVISION_KEPT, S.CONTENT_SOURCE_TARGETED_REVISION),
        (1.49, S.DECISION_REVISION_REJECTED, S.CONTENT_SOURCE_REVISION_NOT_CLOSER),
    ],
    ids=("revision-kept", "revision-not-closer"),
)
def test_targeted_revision_runs_through_the_orchestrator_and_resumes_without_replay(
    session, monkeypatch, revision_distance, expected_decision, expected_source
) -> None:
    """首稿越界 → 定向修改（第二次调用，style_draft:0 步位）→ 变近就用修改稿、不够近保留首稿；两种产品都要过
    检查点校验，同一次执行续跑不重放任何调用。"""
    _bind_resume_project(session)
    first_text = "门轴在雨声里轻响"
    revision_text = "她没有立刻回答"
    _install_readings(
        monkeypatch,
        {first_text: _reading(1.5, 97.0, out_of_band=PACING_OUT), revision_text: _reading(revision_distance, 60.0)},
    )
    generation_client = _CountingGenerationClient()
    soft_qc = _JudgedSoftQc(session, {"soft_qc:0": "continue"}, {"soft_qc:0": 7.9})
    near_final = _FailNearFinal()

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id=f"idempotency:fid-revise-{expected_decision}"
        )

    assert len(generation_client.requests) == 2, "首稿 + 定向修改"
    revision_request = generation_client.requests[1]
    assert revision_request.node_id == "style_draft"
    assert "## Dimensions To Move Toward The Author" in revision_request.messages[-1]["content"]
    state = session.get(SceneRunState, SCENE_ID)
    style_row = session.get(SceneDraft, state.current_style_draft_row_id)
    first_row = session.get(SceneDraft, state.current_neutral_draft_row_id)
    assert style_row.stage == "style_draft" and style_row.generation_llm_call_id != first_row.generation_llm_call_id
    if expected_decision == S.DECISION_REVISION_KEPT:
        assert revision_text in style_row.content
    else:
        assert style_row.content == first_row.content
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID, AttemptTracker.step == "style_draft")
    ).scalars().one()
    assert attempt.details_json["style_step"]["decision"] == expected_decision
    assert attempt.details_json["content_source"] == expected_source
    assert attempt.details_json["style_step"]["dimensions"] == ["narrative.pacing"]
    stages = [(r.source, r.stage) for r in _readings(session)]
    assert stages == [("pipeline", "first_draft"), ("pipeline", "revision")]

    from novel_system.services.style_fidelity_view import current_run_style_fidelity

    summary = current_run_style_fidelity(session, SCENE_ID, state.current_bundle_id)
    assert summary["style_step"]["decision"] == expected_decision and summary["style_step"]["llm_call"] is True
    assert summary["revision"]["distance"] == revision_distance

    # 同一次执行续跑：风格产品（修改稿 / 保留的首稿）过检查点校验，不重放首稿、修改与软 QC
    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id=f"idempotency:fid-revise-{expected_decision}"
        )
    assert len(generation_client.requests) == 2
    assert soft_qc.calls == ["soft_qc:0"]
    assert near_final.calls == 2


def test_patch_that_moves_away_is_reverted_and_the_checkpoint_resumes(session, monkeypatch) -> None:
    _bind_resume_project(session)
    # 首稿与补丁稿都读得出：补丁后读数变远
    first_text = "门轴在雨声里轻响"
    patch_text = "她没有立刻回答"
    _install_readings(monkeypatch, {first_text: _reading(0.80, 35.0), patch_text: _reading(1.00, 70.0)})
    generation_client = _CountingGenerationClient()
    soft_qc = _JudgedSoftQc(
        session, {"soft_qc:0": "patch", "soft_qc:1": "continue"}, {"soft_qc:0": 7.5, "soft_qc:1": 5.0}
    )
    near_final = _FailNearFinal()

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id="idempotency:fid-revert"
        )

    state = session.get(SceneRunState, SCENE_ID)
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint == "soft_qc_ready" and state.run_checkpoint_json["sub_index"] == 3
    style_row_id = refs["soft_input_draft_row_id"]
    assert refs["soft_completion_skip_reason"] == STYLE_PATCH_REVERTED_SKIP_REASON
    assert refs["soft_final_draft_row_id"] == style_row_id != refs["soft_patch_draft_row_id"]
    assert refs["soft_qc_report_id"] == refs["soft_qc0_report_id"] and refs["soft_qc_branch"] == "waive"
    assert state.current_style_draft_row_id == style_row_id == state.latest_valid_draft_row_id
    assert state.current_qc_report_id == refs["soft_qc0_report_id"]
    keep = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID, AttemptTracker.step == S.STYLE_PATCH_KEEP_STEP)
    ).scalars().one()
    assert keep.details_json["decision"] == S.PATCH_DECISION_REVERTED
    assert keep.details_json["reason"] == S.PATCH_REASON_JUDGE_WORSE
    assert keep.details_json["restored_row_id"] == style_row_id
    patched = next(r for r in _readings(session) if r.stage == "patched")
    assert patched.draft_ref == refs["soft_patch_draft_row_id"] and patched.judge_json["overall"] == 5.0
    codes = [item["code"] for item in sg.latest_style_notices(session, SCENE_ID, bundle_id=state.current_bundle_id)]
    assert sg.STYLE_NOTICE_PATCH_REVERTED in codes and sg.STYLE_NOTICE_FIRST_DRAFT_ACCEPTED in codes
    provider_calls = len(generation_client.requests)
    assert provider_calls == 2, "首稿 + 补丁（风格步没有调用）"
    # L7：补丁退回了，这一场留下的是补丁前的稿子——工作台 / 场景接口给的评审分是补丁前那一轮（7.5），
    # 不是评了没采用那一稿的补丁后那一轮（5.0）
    from novel_system.db.models import SceneCard
    from novel_system.services.style_fidelity_view import current_run_style_fidelity, scene_style_fidelity

    summary = current_run_style_fidelity(session, SCENE_ID, state.current_bundle_id)
    assert summary["patch"]["decision"] == S.PATCH_DECISION_REVERTED
    assert summary["judge"]["overall"] == 7.5 and summary["judge"]["qc_report_id"] == refs["soft_qc0_report_id"]
    assert scene_style_fidelity(session, session.get(SceneCard, SCENE_ID))["judge"]["overall"] == 7.5

    # 同一次执行续跑：接受首稿的风格产品、退回的软 QC 收尾都要过检查点校验，不重放任何调用
    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id="idempotency:fid-revert"
        )
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls
    assert near_final.calls == 2


def test_patch_that_holds_or_improves_is_kept(session, monkeypatch) -> None:
    _bind_resume_project(session)
    _install_readings(monkeypatch, {"门轴在雨声里轻响": _reading(0.80, 35.0), "她没有立刻回答": _reading(0.82, 40.0)})
    soft_qc = _JudgedSoftQc(
        session, {"soft_qc:0": "patch", "soft_qc:1": "continue"}, {"soft_qc:0": 7.0, "soft_qc:1": 7.8}
    )
    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, _CountingGenerationClient(), soft_qc, _FailNearFinal()).run_scene(
            SCENE_ID, execution_id="idempotency:fid-keep"
        )
    state = session.get(SceneRunState, SCENE_ID)
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_completion_skip_reason"] is None
    assert refs["soft_final_draft_row_id"] == refs["soft_patch_draft_row_id"]
    keep = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID, AttemptTracker.step == S.STYLE_PATCH_KEEP_STEP)
    ).scalars().one()
    assert keep.details_json["decision"] == S.PATCH_DECISION_KEPT and keep.details_json["notices"] == []


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_a_failing_patch_keep_observation_cannot_break_the_soft_checkpoint(session, monkeypatch) -> None:
    """L3：补丁去留的观察（读数、记读数、记决定）夹在 soft_qc:1 的调用与它的检查点之间。它失败了（这里：写库撞主键）
    也只回滚自己的保存点、按「留下补丁」处理——检查点照常落下，续跑不会报 RUN_CHECKPOINT_OUTPUT_MISSING，也不重放调用。"""
    _bind_resume_project(session)
    _install_readings(monkeypatch, {"门轴在雨声里轻响": _reading(0.80, 35.0), "她没有立刻回答": _reading(1.00, 70.0)})

    def broken_observation(self, *, scene, bundle, before, after, qc0, qc1):  # noqa: ANN001
        row = self.session.get(SceneDraft, before.row_id)
        self.session.add(
            SceneDraft(
                row_id=row.row_id,
                scene_id=row.scene_id,
                chapter_id=row.chapter_id,
                stage=row.stage,
                content="x",
                source_bundle_id=row.source_bundle_id,
                source_bundle_hash=row.source_bundle_hash,
            )
        )
        self.session.flush()

    monkeypatch.setattr(Orchestrator, "_style_patch_keep_decision", broken_observation)
    generation_client = _CountingGenerationClient()
    soft_qc = _JudgedSoftQc(session, {"soft_qc:0": "patch", "soft_qc:1": "continue"}, {"soft_qc:0": 7.5, "soft_qc:1": 5.0})
    near_final = _FailNearFinal()

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id="idempotency:fid-observe-fail"
        )
    state = session.get(SceneRunState, SCENE_ID)
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint == "soft_qc_ready" and state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_draft_row_id"] == refs["soft_patch_draft_row_id"], "观察失败 = 没有退回的依据，留下补丁"
    calls = len(generation_client.requests)

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _orchestrator(session, generation_client, soft_qc, near_final).run_scene(
            SCENE_ID, execution_id="idempotency:fid-observe-fail"
        )
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"] and len(generation_client.requests) == calls


def test_a_protected_name_in_the_draft_does_not_stop_the_pipeline_archive(session, monkeypatch) -> None:
    """H1：稿子里用了画像的受保护专名——管线的归档检查点照常归档（专名只在软 QC 里请作者复核，成稿门只报不拦的警告），
    以前会在归档检查点 409 SOURCE_SAFETY_BLOCKED、没有任何办法过去。"""
    from novel_system.db.models import SceneCard, StyleReferenceBannedTerm
    from novel_system.services.final_text_gate import FinalTextGateService

    profile_id = _bind_resume_project(session)
    # 首稿（_CountingGenerationClient 的第一段）里的「值夜人」被学习作业收成了本书专名
    session.add(
        StyleReferenceBannedTerm(
            term_id="sr_term_fid_orch_name",
            profile_id=profile_id,
            term="值夜人",
            source="protected_auto",
            scope="generation",
        )
    )
    session.commit()
    _install_readings(monkeypatch, {}, default=_reading(0.8, 35.0))
    orchestrator = _orchestrator(
        session,
        _CountingGenerationClient(),
        _JudgedSoftQc(session, {"soft_qc:0": "continue"}, {"soft_qc:0": 8.0}),
        _PassNearFinal(session),
    )

    result = orchestrator.run_scene(SCENE_ID, execution_id="idempotency:fid-protected-archive")
    session.commit()

    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, SCENE_ID)
    final_text = session.get(SceneDraft, state.current_style_draft_row_id).content
    assert "值夜人" in final_text
    gate = FinalTextGateService(session).evaluate(scene_id=SCENE_ID, content=final_text)
    assert gate["archive_blockers"] == []
    warning = next(item for item in gate["warnings"] if item["issue_key"] == "source_safety:protected_term")
    assert warning["terms"] == ["值夜人"] and warning["blocking"] is False
    assert session.get(SceneCard, SCENE_ID) is not None


# ---------------------------------------------------------------------------
# V6 · 对照检查作业
# ---------------------------------------------------------------------------


class _JudgeLLM(AccountedGenerateMixin):
    def __init__(self, output=None, *, fail: bool = False) -> None:
        self.output = output
        self.fail = fail
        self.requests: list = []

    def generate(self, request):  # noqa: ANN001
        import json

        self.requests.append(request)
        if self.fail:
            raise RuntimeError("relay down")
        structured = self.output or {
            "overall": 72,
            "summary": "对白再松一点就更像",
            "dimensions": {
                "scene.dialogue": {"score": 60, "note": "对白偏正式"},
                "language.rhetoric": {"score": 85, "note": "比方的取向对了"},
            },
        }
        return SimpleNamespace(
            structured_output=structured,
            text=json.dumps(structured, ensure_ascii=False),
            usage={},
            finish_reason="stop",
            provider="fake",
            model="fake",
            response_format="json_object",
            request_id=None,
            raw_response={},
        )


def _check_profile(session) -> tuple[str, str]:
    return seed_reference(session, "fid_check", chapters=14, per_chapter=100)


def _post_check(client, body: dict, key: str):
    return client.post("/api/v2/style-reference/checks", json=body, headers={"X-Idempotency-Key": key})


def test_check_requires_an_llm_at_request_time(client, session, monkeypatch) -> None:
    _book, profile_id = _check_profile(session)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (None, False))
    response = _post_check(client, {"text": "一段文字" * 100, "profile_id": profile_id}, "fid-check-nollm")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STYLE_REFERENCE_LLM_REQUIRED"


def test_check_validates_its_target(client, session, monkeypatch) -> None:
    _book, profile_id = _check_profile(session)
    fake = _JudgeLLM()
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (fake, True))
    both = _post_check(client, {"text": "字", "scene_id": "X", "profile_id": profile_id}, "fid-check-both")
    assert both.status_code == 400 and both.json()["error"]["code"] == check_job.CHECK_TARGET_INVALID_CODE
    no_ref = _post_check(client, {"text": "字" * 50}, "fid-check-noref")
    assert no_ref.status_code == 400
    unbound = _post_check(client, {"text": "字" * 50, "project_id": "P_NOBODY"}, "fid-check-unbound")
    assert unbound.status_code == 409 and unbound.json()["error"]["code"] == check_job.CHECK_NOT_BOUND_CODE


def test_text_check_job_records_a_manual_reading_with_the_judge(client, session, monkeypatch) -> None:
    _book, profile_id = _check_profile(session)
    fake = _JudgeLLM()
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (fake, True))
    dispatched: list[str] = []
    monkeypatch.setattr("novel_system.api.routes.style_fidelity.dispatch_job", dispatched.append)
    text = "他把灯芯拨小了些，屋里的影子便大了一圈。门外的雨还没停。" * 30

    response = _post_check(client, {"text": text, "profile_id": profile_id}, "fid-check-text")
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    assert dispatched == [job_id] and response.json()["data"]["job"]["kind"] == "check"
    run_job_inline(job_id)

    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.state == "succeeded", job.error_json
        reading = db.get(StyleFidelityReading, job.result_json["reading_id"])
        assert (reading.source, reading.stage) == ("manual_check", "manual")
        assert reading.judge_json["overall"] == 7.2 and reading.judge_json["dimensions"]["scene.dialogue"] == {
            "score": 6.0,
            "note": "对白偏正式",
        }
        assert reading.copy_check_json["blocked"] is False
    request = fake.requests[0]
    assert request.node_id == "soft_qc", "评审走现有的 soft_qc 节点路由"
    system = request.messages[0]["content"]
    assert system.startswith("[STYLE_REFERENCE]") and "reference judge" in system
    assert "[风格样例](以下是参考作者的原文片段，按原书顺序排列，是这次评审的标准" in system
    assert text[:20] in request.messages[1]["content"]

    status = client.get(f"/api/v2/style-reference/checks/{job_id}")
    assert status.status_code == 200
    payload = status.json()["data"]
    assert payload["job"]["status"] == "succeeded" and payload["reading"]["judge"]["overall"] == 7.2
    single = client.get(f"/api/v2/style-reference/readings/{payload['reading']['reading_id']}")
    assert single.status_code == 200 and single.json()["data"]["reading"]["source"] == "manual_check"


def test_scene_check_reads_the_scene_final_text(client, session, monkeypatch) -> None:
    from novel_system.db.models import FinalScene
    from tests.test_style_fidelity_v3 import _bound_scene

    scene, _bundle, _book, _profile = _bound_scene(session, "fid_check_scene")
    text = "潮水退去以后，他在闸门前站了很久，才把手里的灯放下。" * 30
    session.add(
        FinalScene(
            row_id="final_fid_check",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content=text,
            source_bundle_id="author_adopt",
            source_bundle_hash="author_adopt",
        )
    )
    session.get(SceneRunState, scene.scene_id).current_final_scene_row_id = "final_fid_check"
    session.commit()
    fake = _JudgeLLM()
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (fake, True))
    monkeypatch.setattr("novel_system.api.routes.style_fidelity.dispatch_job", lambda _job_id: None)

    response = _post_check(client, {"scene_id": scene.scene_id}, "fid-check-scene")
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    run_job_inline(job_id)
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.state == "succeeded", job.error_json
        reading = db.get(StyleFidelityReading, job.result_json["reading_id"])
        assert reading.scene_id == scene.scene_id and reading.draft_ref == "final_scene:final_fid_check"
        assert reading.text_sha256 == R.text_sha256(text)
    assert text[:20] in fake.requests[0].messages[1]["content"]

    fidelity = client.get(f"/api/v1/scenes/{scene.scene_id}/style-fidelity")
    assert fidelity.status_code == 200
    data = fidelity.json()["data"]
    assert data["bound"] is True and data["readings"]["manual"]["source"] == "manual_check"
    assert data["judge"]["overall"] == 7.2 and set(data["readings"]) == set(R.STAGES)
    project = client.get(f"/api/v1/projects/{scene.project_id}/style-fidelity")
    assert project.status_code == 200
    assert project.json()["data"]["bound"] is True
    assert project.json()["data"]["dimension_averages"]["scene.dialogue"]["judge"] == 6.0

    # 同一稿再查一次：新的一条读数带这一次的评审（对照检查不按稿行去重）
    monkeypatch.setattr(
        check_job,
        "resolve_check_client",
        lambda: (_JudgeLLM({"overall": 90, "dimensions": {"scene.dialogue": {"score": 80, "note": "好多了"}}}), True),
    )
    again = _post_check(client, {"scene_id": scene.scene_id}, "fid-check-scene-again")
    run_job_inline(again.json()["data"]["job_id"])
    with SessionLocal() as db:
        second = db.get(StyleReferenceJob, again.json()["data"]["job_id"])
        assert second.state == "succeeded", second.error_json
        assert second.result_json["reading_id"] != job.result_json["reading_id"]
        assert db.get(StyleFidelityReading, second.result_json["reading_id"]).judge_json["overall"] == 9.0
    assert client.get(f"/api/v1/scenes/{scene.scene_id}/style-fidelity").json()["data"]["judge"]["overall"] == 9.0


def test_check_job_fails_loudly_when_the_judge_fails(client, session, monkeypatch) -> None:
    _book, profile_id = _check_profile(session)
    monkeypatch.setattr("novel_system.api.routes.style_fidelity.dispatch_job", lambda _job_id: None)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_JudgeLLM(fail=True), True))
    text = "他把灯芯拨小了些，屋里的影子便大了一圈。" * 30
    failing = _post_check(client, {"text": text, "profile_id": profile_id}, "fid-check-fail")
    run_job_inline(failing.json()["data"]["job_id"])
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_JudgeLLM({"overall": None, "dimensions": {}}), True))
    empty = _post_check(client, {"text": text + "。", "profile_id": profile_id}, "fid-check-empty")
    run_job_inline(empty.json()["data"]["job_id"])
    with SessionLocal() as db:
        for response in (failing, empty):
            job = db.get(StyleReferenceJob, response.json()["data"]["job_id"])
            assert job.state == "failed" and job.error_json["code"] == check_job.CHECK_JUDGE_FAILED_CODE
            assert job.error_json["retryable"] is True
        assert db.scalars(select(StyleFidelityReading).where(StyleFidelityReading.source == "manual_check")).first() is None


class _UsageInvariantError(RuntimeError):
    code = "LLM_USAGE_EXCEEDS_RESERVATION"


@pytest.mark.parametrize(
    "error",
    [
        LLMAccountingError("LLM_ACCOUNTING_CALL_EXISTS", "logical call already exists"),
        _UsageInvariantError("usage settlement invariant failed"),
    ],
    ids=("accounting-error", "usage-invariant"),
)
def test_check_job_keeps_control_plane_failures_distinct(client, session, monkeypatch, error) -> None:
    """控制面失败（记账 / 用量不变式）原样上抛：作业按它自己的错误码失败，不包成「评审失败」、不降级成只有读数的检查
    （取代旧回测工人的同名边界测试）。"""
    _book, profile_id = _check_profile(session)
    monkeypatch.setattr("novel_system.api.routes.style_fidelity.dispatch_job", lambda _job_id: None)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_JudgeLLM(), True))
    seen: list[BaseException] = []

    def raise_error(*_args, **_kwargs):  # noqa: ANN002, ANN003
        seen.append(error)
        raise error

    monkeypatch.setattr(check_job, "execute_accounted_call", raise_error)
    response = _post_check(client, {"text": "他把灯芯拨小了些。" * 60, "profile_id": profile_id}, f"fid-check-cp-{error.code}")
    run_job_inline(response.json()["data"]["job_id"])
    assert seen == [error]
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, response.json()["data"]["job_id"])
        assert job.state == "failed"
        assert job.error_json["code"] == error.code != check_job.CHECK_JUDGE_FAILED_CODE
        assert job.error_json["retryable"] is True
        assert db.scalars(select(StyleFidelityReading).where(StyleFidelityReading.source == "manual_check")).first() is None


def test_judge_output_is_rescaled_and_drops_excluded_dimensions() -> None:
    judge = check_job.normalize_judge_output(
        {
            "overall": 0.8,
            "dimensions": {
                "scene.dialogue": {"score": 0.6, "note": "n"},
                "theme.values": {"score": 0.9},
                "bogus": {"score": 1},
            },
        },
        {"theme.values": "exclude"},
    )
    assert judge["overall"] == 8.0 and judge["dimensions"] == {"scene.dialogue": {"score": 6.0, "note": "n"}}


# ---------------------------------------------------------------------------
# 读数接口 / 旧回测下线
# ---------------------------------------------------------------------------


def test_fidelity_endpoints_404_for_unknown_targets(client) -> None:
    assert client.get("/api/v1/scenes/NOPE/style-fidelity").status_code == 404
    assert client.get("/api/v1/projects/NOPE/style-fidelity").status_code == 404
    assert client.get("/api/v2/style-reference/readings/NOPE").status_code == 404
    assert client.get("/api/v2/style-reference/checks/NOPE").status_code == 404


def test_old_validation_endpoints_are_gone(client, session) -> None:
    """旧「回测」的三个端点与它的报告表都已删除（迁移 0091）：像不像一律走对照检查。"""
    _book, profile_id = _check_profile(session)
    response = client.post(
        f"/api/v2/style-reference/profiles/{profile_id}/validate",
        json={"generated_text": "一段文字", "mode": "sync_only"},
        headers={"X-Idempotency-Key": "fid-validate-retired"},
    )
    assert response.status_code in (404, 405)
    assert client.get(f"/api/v2/style-reference/profiles/{profile_id}/reports").status_code in (404, 405)
    assert client.get("/api/v2/style-reference/reports/sr_rep_x").status_code in (404, 405)

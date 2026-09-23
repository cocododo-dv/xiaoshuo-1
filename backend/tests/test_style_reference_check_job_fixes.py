"""风格参考 v3 · 对照检查作业的修正（H1 评审的参考按 soft_qc 的实际路由判、L7 评审提示压预算、作业所有权）。

- H1：「仅本机」的书遇云端的 soft_qc 路由 → 评审的参考一个字都不渲染，模型一次都不调；
- L7：评审提示按模板的输入预算压（``NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET`` 收紧时照收紧）；
- 所有权：评审回来先确认作业还是自己的（没被接走、没被取消），「完成」写落空就连读数一起回滚；每个进度写之后立刻
  提交，不带着写锁去干重活。

全部合成数据；无真实模型、无真实作者原文。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from novel_system.db.models import StyleFidelityReading, StyleReferenceJob
from novel_system.db.session import SessionLocal
from novel_system.services.context_budget import estimate_tokens
from novel_system.services.style_policy import reset_style_policy_cache
from novel_system.services.style_reference import check_job
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.errors import CloudPolicyBlockedError
from novel_system.services.style_reference.inject.render import reset_render_cache
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CHECK,
    StyleJobService,
    register_job_handler,
    run_job_inline,
)
from novel_system.services.style_reference.runtime_contract import reset_contract_memo
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.style_reference_inject_helpers import seed_reference


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_config_cache()
    reset_render_cache()
    reset_contract_memo()
    reset_style_policy_cache()
    yield
    reset_render_cache()
    reset_style_policy_cache()


def _routes(monkeypatch, local_nodes: set[str] | None) -> None:
    """节点路由打桩：``local_nodes`` 里的节点走本机模型，其余走云端；``None`` = 全部本机。"""
    monkeypatch.setattr(
        policy_module,
        "node_route_is_local",
        lambda node_id, **_k: local_nodes is None or node_id in local_nodes,
    )


def _global_model(monkeypatch, *, local: bool) -> None:
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: local)


class _FakeJudge(AccountedGenerateMixin):
    """对照检查评审的替身：给一份 10 分制的结构化评分；``on_generate`` 在「模型调用期间」做点别的事。"""

    def __init__(self, on_generate=None) -> None:
        self.requests: list = []
        self.on_generate = on_generate

    def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        if self.on_generate is not None:
            self.on_generate()
        structured = {"overall": 7, "summary": "合成", "dimensions": {"scene.dialogue": {"score": 6, "note": "偏正式"}}}
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


# ---------------------------------------------------------------------------
# 评审的参考：云策略与预算
# ---------------------------------------------------------------------------


def test_check_judge_sends_nothing_for_a_local_only_book_on_a_cloud_soft_qc_route(session, monkeypatch) -> None:
    _book, profile_id = seed_reference(session, "h1_check", cloud_policy="local_only")
    policy = check_job._profile_policy(session, profile_id)
    fake = _FakeJudge()
    _global_model(monkeypatch, local=True)
    _routes(monkeypatch, set())
    with pytest.raises(CloudPolicyBlockedError):
        check_job.run_reference_judge(
            session,
            policy=policy,
            scope=check_job._scope(None),
            text="他把灯芯拨小了些，屋里的影子便大了一圈。" * 30,
            llm_client=fake,
            context_scope_id="h1_check",
            project_id=None,
        )
    assert fake.requests == []


def test_check_judge_prompt_is_fitted_to_the_input_budget(session, monkeypatch) -> None:
    _book, profile_id = seed_reference(session, "l7")
    policy = check_job._profile_policy(session, profile_id)
    text = "他把灯芯拨小了些，屋里的影子便大了一圈。" * 30

    def _judge(fake):
        check_job.run_reference_judge(
            session,
            policy=policy,
            scope=check_job._scope(None),
            text=text,
            llm_client=fake,
            context_scope_id="l7",
            project_id=None,
        )
        system, user = fake.requests[0].messages[0]["content"], fake.requests[0].messages[1]["content"]
        return system, user

    wide = _FakeJudge()
    system, user = _judge(wide)
    window_lines = [line for line in system.splitlines() if line.startswith("- (第")]
    assert len(window_lines) == 4
    full = estimate_tokens(system) + estimate_tokens(user)
    budget = full - int(sum(estimate_tokens(line) for line in window_lines) * 0.6)
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET", str(budget))
    reset_render_cache()
    tight = _FakeJudge()
    tight_system, _user = _judge(tight)
    tight_windows = [line for line in tight_system.splitlines() if line.startswith("- (第")]
    assert 1 <= len(tight_windows) < 4 and "[文风卡]" in tight_system


# ---------------------------------------------------------------------------
# 对照检查作业的所有权
# ---------------------------------------------------------------------------


def _start_check(session, key: str) -> str:
    _book, profile_id = seed_reference(session, key, chapters=14, per_chapter=100)
    job = check_job.start_check_job(
        session,
        text="他把灯芯拨小了些，屋里的影子便大了一圈。门外的雨还没停。" * 30,
        profile_id=profile_id,
        llm_client=_FakeJudge(),
        llm_enabled=True,
    )
    session.commit()
    register_job_handler(JOB_KIND_CHECK, check_job.run_check_job)
    return job.job_id


def _readings_count() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(StyleFidelityReading)) or 0)


def test_check_job_taken_over_during_the_judge_records_nothing(session, monkeypatch) -> None:
    job_id = _start_check(session, "own_steal")

    def _steal() -> None:
        # 评审调用期间：清扫把作业重排、另一个工人认领了它（owner_token 换了）
        with SessionLocal() as other:
            other.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id).values(owner_token="stolen"))
            other.commit()

    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(_steal), True))
    run_job_inline(job_id)
    assert _readings_count() == 0
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.owner_token == "stolen" and job.state == "running"


def test_check_job_cancelled_during_the_judge_records_nothing(session, monkeypatch) -> None:
    job_id = _start_check(session, "own_cancel")

    def _cancel() -> None:
        with SessionLocal() as other:
            StyleJobService(other).request_cancel(job_id)
            other.commit()

    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(_cancel), True))
    run_job_inline(job_id)
    assert _readings_count() == 0
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, job_id).state == "cancelled"


def test_check_job_whose_finish_write_misses_rolls_back_the_reading(session, monkeypatch) -> None:
    job_id = _start_check(session, "own_finish")
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    monkeypatch.setattr(StyleJobService, "succeed", lambda self, claimed, result=None: False)
    run_job_inline(job_id)
    assert _readings_count() == 0


def test_check_job_commits_each_progress_step_before_the_heavy_work(session, monkeypatch) -> None:
    job_id = _start_check(session, "own_commit")
    seen: list[str] = []
    real_reading_for_text = check_job.readings.reading_for_text

    def _reading_for_text(db_session, policy, text):  # noqa: ANN001
        # 读数（窗口索引、测量）开始时，「读数」这一步的进度已经提交：别的连接看得见，写锁没有被攥着
        with SessionLocal() as other:
            seen.append(str(other.get(StyleReferenceJob, job_id).phase))
        return real_reading_for_text(db_session, policy, text)

    monkeypatch.setattr(check_job.readings, "reading_for_text", _reading_for_text)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    run_job_inline(job_id)
    assert seen == ["measure"]
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, job_id).state == "succeeded"
    assert _readings_count() == 1

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


# ---------------------------------------------------------------------------
# 一场属于哪部作品按场景本身定，不信客户端（界面复核 #7：换过作品后，读数记到了另一部作品名下）
# ---------------------------------------------------------------------------


OTHER_PROJECT = "proj_other_work"


def _scene_with_final(session, key: str):
    from novel_system.db.models import FinalScene, SceneRunState, StoryProject
    from tests.test_style_fidelity_v3 import _bound_scene

    scene, _bundle, _book, profile_id = _bound_scene(session, key)
    text = "潮水退去以后，他在闸门前站了很久，才把手里的灯放下。" * 30
    session.add(
        FinalScene(
            row_id=f"final_{key}",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content=text,
            source_bundle_id="author_adopt",
            source_bundle_hash="author_adopt",
        )
    )
    session.get(SceneRunState, scene.scene_id).current_final_scene_row_id = f"final_{key}"
    # 界面上换到的另一部作品（客户端带来的是它的 id）
    session.add(StoryProject(project_id=OTHER_PROJECT, title="另一部", outline_text=""))
    session.commit()
    return scene, profile_id


def _reading_of(job_id: str) -> StyleFidelityReading:
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.state == "succeeded", job.error_json
        reading = db.get(StyleFidelityReading, job.result_json["reading_id"])
        db.expunge(reading)
        return reading


def test_scene_check_takes_the_project_from_the_scene_not_from_the_client(session, monkeypatch) -> None:
    scene, profile_id = _scene_with_final(session, "chk_proj")
    job = check_job.start_check_job(
        session,
        scene_id=scene.scene_id,
        profile_id=profile_id,
        project_id=OTHER_PROJECT,
        llm_client=_FakeJudge(),
        llm_enabled=True,
    )
    session.commit()
    assert job.params_json["project_id"] == scene.project_id != OTHER_PROJECT
    register_job_handler(JOB_KIND_CHECK, check_job.run_check_job)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    run_job_inline(job.job_id)
    reading = _reading_of(job.job_id)
    assert (reading.scene_id, reading.project_id) == (scene.scene_id, scene.project_id)


def test_scene_check_job_created_with_the_client_project_still_files_under_the_scene_project(session, monkeypatch) -> None:
    """修正之前建的作业（参数里还是客户端给的另一部作品）：跑的时候照样按场景本身定作品。"""
    scene, profile_id = _scene_with_final(session, "chk_proj_old")
    job = StyleJobService(session).create(
        JOB_KIND_CHECK,
        book_id=check_job._light_policy(session, {"profile_id": profile_id}).book_id,
        profile_id=profile_id,
        params={"target": "scene", "scene_id": scene.scene_id, "profile_id": profile_id, "project_id": OTHER_PROJECT},
        phase="queued",
        allow_parallel=True,
    )
    session.commit()
    register_job_handler(JOB_KIND_CHECK, check_job.run_check_job)
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    run_job_inline(job.job_id)
    assert _reading_of(job.job_id).project_id == scene.project_id


# ---------------------------------------------------------------------------
# 2026-09-24 §8 C1：取消对照检查（与分类 / 学习的取消同形）
# ---------------------------------------------------------------------------

CHECKS = "/api/v2/style-reference/checks"


def _cancel(client, job_id: str, key: str):
    return client.post(f"{CHECKS}/{job_id}/cancel", json={}, headers={"X-Idempotency-Key": key})


def _activity_entry(client, job_id: str) -> dict:
    items = client.get("/api/v2/style-reference/activity").json()["data"]["items"]
    return next(item for item in items if item.get("job_id") == job_id)


def test_cancel_of_a_queued_check_finishes_in_the_request(client, session) -> None:
    job_id = _start_check(session, "c1_queued")
    entry = _activity_entry(client, job_id)
    assert entry["kind"] == "check" and entry["status"] == "queued" and entry["cancellable"] is True

    resp = _cancel(client, job_id, "c1-queued")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["job_id"] == job_id and data["state"] == "cancelled"
    assert data["cancel_requested"] is True and data["finished"] is True
    # 响应与 GET /checks/{id} 同形：作业条目 + 读数（取消的作业没有读数）
    assert data["job"]["status"] == "cancelled" and data["job"]["cancellable"] is False and data["reading"] is None
    assert set(client.get(f"{CHECKS}/{job_id}").json()["data"]) == {"job", "reading"}
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.state == "cancelled" and job.owner_token is None and job.finished_at
    # 已结束 → 409，不是幂等地再「取消」一次
    again = _cancel(client, job_id, "c1-queued-again")
    assert again.status_code == 409 and again.json()["error"]["code"] == check_job.CHECK_NOT_ACTIVE_CODE
    assert again.json()["error"]["code"] == "STYLE_REFERENCE_CHECK_NOT_ACTIVE"


def test_cancel_of_a_running_check_sets_the_flag_and_the_worker_stops_at_its_next_checkpoint(client, session) -> None:
    from novel_system.services.style_reference.jobs import JobCancelled

    job_id = _start_check(session, "c1_running")
    with SessionLocal() as worker_db:
        claimed = StyleJobService(worker_db).claim(job_id)  # 一个还活着的工人正拿着 owner_token
        worker_db.commit()
    assert claimed is not None
    assert _activity_entry(client, job_id)["cancellable"] is True

    resp = _cancel(client, job_id, "c1-running")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "running" and data["finished"] is False and data["cancel_requested"] is True
    assert data["job"]["status"] == "running" and data["job"]["cancel_requested"] is True
    # 工人在下一个检查点看到取消
    with SessionLocal() as worker_db:
        with pytest.raises(JobCancelled):
            StyleJobService(worker_db).check_continue(claimed)
        assert StyleJobService(worker_db).finish_cancelled(claimed)
        worker_db.commit()
    assert _activity_entry(client, job_id)["status"] == "cancelled"


def test_cancel_check_rejects_finished_missing_and_foreign_jobs(client, session, monkeypatch) -> None:
    from novel_system.services.style_reference.jobs import JOB_KIND_LEARN

    job_id = _start_check(session, "c1_done")
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    run_job_inline(job_id)
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, job_id).state == "succeeded"
    finished = _cancel(client, job_id, "c1-done")
    assert finished.status_code == 409 and finished.json()["error"]["code"] == check_job.CHECK_NOT_ACTIVE_CODE
    assert finished.json()["error"]["details"]["state"] == "succeeded"

    missing = _cancel(client, "sr_job_nope", "c1-missing")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "STYLE_REFERENCE_CHECK_NOT_FOUND"

    # 别的种类的作业不走这个口
    with SessionLocal() as db:
        learn = StyleJobService(db).create(JOB_KIND_LEARN, book_id=db.get(StyleReferenceJob, job_id).book_id)
        db.commit()
        learn_id = learn.job_id
    foreign = _cancel(client, learn_id, "c1-foreign")
    assert foreign.status_code == 404
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, learn_id).state == "queued"

    # 幂等键是必需的
    bare = client.post(f"{CHECKS}/{job_id}/cancel", json={})
    assert bare.status_code == 400 and bare.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_cancel_arriving_during_the_judge_records_nothing_through_the_service(session, monkeypatch) -> None:
    """服务层的取消（路由背后的那个函数）在评审期间到达：作业收尾为 cancelled，读数一条不记。"""
    job_id = _start_check(session, "c1_mid_judge")

    def _cancel_mid_judge() -> None:
        with SessionLocal() as other:
            job = check_job.cancel_check_job(other, job_id)
            assert job.state == "running" and job.cancel_requested == 1
            other.commit()

    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(_cancel_mid_judge), True))
    run_job_inline(job_id)
    assert _readings_count() == 0
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, job_id).state == "cancelled"


# ---------------------------------------------------------------------------
# 2026-09-24 §8 C3：评审分按模板声明的刻度（越界丢掉、不夹；没声明才按一次回答推断）
# ---------------------------------------------------------------------------


def _judge_schema() -> dict:
    from novel_system.services.prompt_builder import load_prompt_templates

    schema = load_prompt_templates()[check_job.CHECK_TEMPLATE].structured_schema
    assert schema["properties"]["overall"]["maximum"] == 10
    return schema


def test_judge_scores_follow_the_declared_zero_to_ten_scale() -> None:
    schema = _judge_schema()
    # 一个误写的 85 不再把整份回答按 0–100 除：它被丢掉，其余按 0–10 读
    judge = check_job.normalize_judge_output(
        {"overall": 7, "dimensions": {"scene.dialogue": {"score": 85, "note": "n"}, "theme.values": {"score": 6}}},
        {},
        schema=schema,
    )
    assert judge["overall"] == 7.0
    assert judge["dimensions"] == {"theme.values": {"score": 6.0, "note": ""}}
    # 越界的总分丢掉 → 由按维分均值补；越界的 12 那一维丢掉
    judge = check_job.normalize_judge_output(
        {"overall": 12, "dimensions": {"scene.dialogue": {"score": 6}, "theme.values": {"score": 12}}},
        {},
        schema=schema,
    )
    assert judge["overall"] == 6.0 and set(judge["dimensions"]) == {"scene.dialogue"}
    # 全在 1 以下的回答也按 0–10 读：0.9 就是 0.9 分，不是 9 分
    judge = check_job.normalize_judge_output(
        {"overall": 0.9, "dimensions": {"scene.dialogue": {"score": 0.6}}}, {}, schema=schema
    )
    assert judge["overall"] == 0.9 and judge["dimensions"]["scene.dialogue"]["score"] == 0.6
    # 边界与「不学」维照旧
    judge = check_job.normalize_judge_output(
        {"overall": 10, "dimensions": {"scene.dialogue": {"score": 0}, "theme.values": {"score": 9}}},
        {"theme.values": "exclude"},
        schema=schema,
    )
    assert judge["overall"] == 10.0 and judge["dimensions"] == {"scene.dialogue": {"score": 0.0, "note": ""}}
    # 整份回答都答错了刻度 → 没有分数（作业据此失败，不会把 60 分记成 6 分）
    judge = check_job.normalize_judge_output(
        {"overall": 72, "dimensions": {"scene.dialogue": {"score": 60}}}, {}, schema=schema
    )
    assert judge["overall"] is None and judge["dimensions"] == {}


def test_judge_scores_fall_back_to_inference_only_without_a_declared_scale() -> None:
    """旧提示词快照（模板没写 maximum）：才按一次回答推断量级并夹到边界（旧口径）。"""
    for schema in (None, {"type": "object", "properties": {"overall": {"type": "number"}}}):
        judge = check_job.normalize_judge_output(
            {"overall": 72, "dimensions": {"scene.dialogue": {"score": 60}}}, {}, schema=schema
        )
        assert judge["overall"] == 7.2 and judge["dimensions"]["scene.dialogue"]["score"] == 6.0
        judge = check_job.normalize_judge_output(
            {"overall": 0.8, "dimensions": {"scene.dialogue": {"score": 0.6}}}, {}, schema=schema
        )
        assert judge["overall"] == 8.0 and judge["dimensions"]["scene.dialogue"]["score"] == 6.0


class _HundredScaleJudge(_FakeJudge):
    """答错刻度的评审（0–100）。"""

    def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        structured = {"overall": 72, "summary": "合成", "dimensions": {"scene.dialogue": {"score": 60, "note": "n"}}}
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


def test_check_job_reads_the_judge_on_the_template_scale(session, monkeypatch) -> None:
    """作业里评审按模板刻度读：答 7 / 6 记 7.0 / 6.0；答 72 / 60（0–100）没有一个分在刻度内 → 作业失败，不记读数。"""
    good_id = _start_check(session, "c3_good")
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_FakeJudge(), True))
    run_job_inline(good_id)
    reading = _reading_of(good_id)
    assert reading.judge_json["overall"] == 7.0 and reading.judge_json["dimensions"]["scene.dialogue"]["score"] == 6.0

    bad_id = _start_check(session, "c3_bad")
    monkeypatch.setattr(check_job, "resolve_check_client", lambda: (_HundredScaleJudge(), True))
    run_job_inline(bad_id)
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, bad_id)
        assert job.state == "failed" and job.error_json["code"] == check_job.CHECK_JUDGE_FAILED_CODE
        assert job.error_json["details"]["reason"] == "no_scores"
    assert _readings_count() == 1

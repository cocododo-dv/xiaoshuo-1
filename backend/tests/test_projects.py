from __future__ import annotations


import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    FinalScene,
    OperationLog,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.projects import ProjectChapterFlowService


def _create_project(client, *, title: str = "雨城残响", target_chapter_count: int = 2, key: str = "default") -> dict:
    response = client.post(
        "/api/v1/projects",
        json={
            "title": title,
            "genre": "都市悬疑",
            "target_chapter_count": target_chapter_count,
            "target_word_count": 120000,
            "outline_text": "女主收到一封来自十年前的信。\n她回到雨城，发现旧案和家族秘密有关。\n结尾她决定公开真相。",
        },
        headers={"X-Idempotency-Key": f"create-project-{key}-{target_chapter_count}"},
    )
    assert response.status_code == 200
    return response.json()["data"]["project"]



def _generate_plan(client, project_id: str) -> dict:
    """Seed a reviewable outline plan directly; the LLM/local outline planner was retired."""
    from novel_system.db.models import OutlinePlan, StoryProject
    from novel_system.db.session import SessionLocal
    from novel_system.services.projects import outline_plan_payload

    with SessionLocal() as session:
        project = session.get(StoryProject, project_id)
        assert project is not None
        chapter_count = max(1, int(project.target_chapter_count or 2))
        chapters = []
        for index in range(1, chapter_count + 1):
            chapter_id = f"{project_id}_CH{index:02d}"
            chapters.append(
                {
                    "chapter_id": chapter_id,
                    "title": f"第{index}章",
                    "chapter_goal": f"推进第{index}章的核心冲突",
                    "scenes": [
                        {
                            "scene_id": f"{chapter_id}_SC{seq:02d}",
                            "scene_seq": seq,
                            "scene_goal": f"第{index}章第{seq}场",
                            "beats_json": ["开场", "转折", "收束"],
                            "scene_type": "proactive",
                        }
                        for seq in (1, 2)
                    ],
                }
            )
        plan = OutlinePlan(
            plan_id=f"plan_{project_id}_v1",
            project_id=project_id,
            version=1,
            status="pending_review",
            plan_json={"source": "test_fixture", "chapters": chapters},
        )
        session.add(plan)
        session.commit()
        return outline_plan_payload(plan)


def _approve_plan(client, project_id: str, plan_id: str) -> dict:
    from novel_system.db.session import SessionLocal
    from novel_system.services.projects import ProjectService

    with SessionLocal() as session:
        result = ProjectService(session).approve_outline_plan(project_id, plan_id)
        session.commit()
        return result


def test_project_create_list_and_dashboard_keep_free_text_outline(client) -> None:
    project = _create_project(client, title="雨城残响", key="list-dashboard")

    assert project["project_id"].startswith("PRJ_")
    assert project["title"] == "雨城残响"
    assert project["status"] == "outline_draft"
    assert "来自十年前的信" in project["outline_text"]

    list_response = client.get("/api/v1/projects")
    assert list_response.status_code == 200
    items = list_response.json()["data"]["items"]
    assert [item["project_id"] for item in items] == [project["project_id"]]

    dashboard_response = client.get(f"/api/v1/projects/{project['project_id']}/dashboard")
    assert dashboard_response.status_code == 200
    dashboard = dashboard_response.json()["data"]
    assert dashboard["project"]["project_id"] == project["project_id"]
    assert dashboard["project"]["outline_text"] == project["outline_text"]
    assert dashboard["next_action"] == "generate_outline_plan"
    assert dashboard["chapters"] == []


def test_project_chapter_run_stops_at_final_review_and_approve_final_advances(client, session, monkeypatch) -> None:
    class FakeChapterRunnerService:
        def __init__(self, db_session) -> None:
            self.session = db_session

        def run_full(self, chapter_id: str) -> dict:
                scenes = (
                    self.session.query(SceneCard)
                    .filter(SceneCard.chapter_id == chapter_id)
                    .order_by(SceneCard.scene_seq)
                    .all()
                )
                for scene in scenes:
                    final_row_id = f"fake_final_{scene.scene_id}"
                    self.session.add(
                        FinalScene(
                            row_id=final_row_id,
                            scene_id=scene.scene_id,
                            chapter_id=chapter_id,
                            source_bundle_id=f"fake_bundle_{scene.scene_id}",
                            source_bundle_hash=f"fake_hash_{scene.scene_id}",
                            content=f"Final body for {scene.scene_id}.",
                        )
                    )
                    state = self.session.get(SceneRunState, scene.scene_id)
                    if state is not None:
                        state.current_final_scene_row_id = final_row_id
                        state.narrative_sync_status = "synced"
                        state.narrative_sync_final_scene_row_id = final_row_id
                self.session.flush()
                canon = CanonContinuityService(self.session)
                for scene in scenes:
                    canon.verify_scene_complete(
                        scene.project_id,
                        scene.scene_id,
                        actor_ref="test-runner",
                        note="测试运行器模拟作者完成正史核验。",
                    )
                result = {
                    "chapter_id": chapter_id,
                    "status": "completed",
                    "current_scene_id": scenes[-1].scene_id if scenes else None,
                    "completed_scene_ids": [scene.scene_id for scene in scenes],
                    "blocked_scene_id": None,
                    "latest_error": None,
                }
                self.session.add(
                    ChapterRunJob(
                        job_id=f"fake_job_{chapter_id}",
                        chapter_id=chapter_id,
                        status="completed",
                        job_type="chapter_full",
                        payload_json=result,
                        result_summary_json={"near_final_ready": True},
                    )
                )
                self.session.flush()
                return result

    monkeypatch.setattr("novel_system.services.chapter_final_flow.ChapterRunnerService", FakeChapterRunnerService)
    project = _create_project(client, target_chapter_count=2)
    plan = _generate_plan(client, project["project_id"])
    approved = _approve_plan(client, project["project_id"], plan["plan_id"])
    first_chapter_id = approved["project"]["current_chapter_id"]

    run_response = client.post(
        f"/api/v1/projects/{project['project_id']}/chapters/{first_chapter_id}/run",
        json={},
        headers={"X-Idempotency-Key": "run-project-chapter-1"},
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()["data"]
    assert run_payload["project"]["status"] == "chapter_final_review"
    assert run_payload["run"]["status"] == "completed"

    dashboard_response = client.get(f"/api/v1/projects/{project['project_id']}/dashboard")
    assert dashboard_response.status_code == 200
    dashboard_payload = dashboard_response.json()["data"]
    assert dashboard_payload["next_action"] == "approve_chapter_final"
    body_hash = client.get(f"/api/v1/chapter-manuscripts/{first_chapter_id}").json()["data"]["body_hash"]
    assert body_hash

    approve_response = client.post(
        f"/api/v1/projects/{project['project_id']}/chapters/{first_chapter_id}/approve-final",
        json={
            "revision_notes": "Keep the second scene tense on the next pass.",
            "read_confirmation": {"body_hash": body_hash, "note": "Read in full before approving."},
        },
        headers={"X-Idempotency-Key": "approve-project-chapter-1"},
    )
    assert approve_response.status_code == 200
    approve_payload = approve_response.json()["data"]
    next_project = approve_payload["project"]
    assert next_project["status"] == "chapter_ready"
    assert next_project["current_chapter_id"].endswith("_CH02")
    assert next_project["approved_chapter_ids"] == [first_chapter_id]
    assert approve_payload["approval_note"]["revision_notes"] == "Keep the second scene tense on the next pass."

    approval_log = session.execute(
        select(OperationLog).where(
            OperationLog.event_type == "chapter_final_approval",
            OperationLog.object_type == "chapter",
            OperationLog.object_ref == first_chapter_id,
        )
    ).scalar_one()
    assert approval_log.payload_json["revision_notes"] == "Keep the second scene tense on the next pass."
    assert approval_log.payload_json["project_id"] == project["project_id"]
    assert approval_log.payload_json["body_hash"] == body_hash
    session.expire_all()
    assert session.get(ChapterGoal, first_chapter_id).state == "approved"


def test_project_chapter_final_binds_the_body_the_author_read(client, session) -> None:
    """确认定稿是唯一的「读过并定稿」调用（B2-1）：不带读到的正文哈希 422；读完之后正文又变了，带着旧哈希 409；
    带着现在这一份的哈希才定稿，「已通读」与「定稿」两条审计记在同一个事务里。"""
    project = _create_project(client, target_chapter_count=1, key="read-confirm")
    plan = _generate_plan(client, project["project_id"])
    approved = _approve_plan(client, project["project_id"], plan["plan_id"])
    chapter_id = approved["project"]["current_chapter_id"]
    scenes = session.query(SceneCard).filter(SceneCard.chapter_id == chapter_id).all()
    assert scenes
    final_rows = []
    for index, scene in enumerate(scenes, start=1):
        final_row = FinalScene(
            row_id=f"final_{scene.scene_id}",
            scene_id=scene.scene_id,
            chapter_id=chapter_id,
            source_bundle_id=f"bundle_read_confirm_{index}",
            source_bundle_hash=f"hash_read_confirm_{index}",
            content=f"第一版正文，需要通读后才能批准。场景 {index}。",
        )
        final_rows.append(final_row)
        session.add(final_row)
        state = session.get(SceneRunState, scene.scene_id)
        assert state is not None
        state.current_final_scene_row_id = final_row.row_id
    db_project = session.get(StoryProject, project["project_id"])
    assert db_project is not None
    db_project.status = "chapter_final_review"
    session.commit()
    path = f"/api/v1/projects/{project['project_id']}/chapters/{chapter_id}/approve-final"

    detail = client.get(f"/api/v1/chapter-manuscripts/{chapter_id}").json()["data"]
    assert detail["assembled"]["content"].startswith("第一版正文")
    read_hash = detail["body_hash"]
    assert read_hash

    without_read = client.post(path, json={"revision_notes": "Not read."}, headers={"X-Idempotency-Key": "approve-without-read"})
    assert without_read.status_code == 422, without_read.text
    assert any(issue["field"] == "body.read_confirmation" for issue in without_read.json()["error"]["details"]["issues"])

    previous_final = final_rows[0]
    replacement = FinalScene(
        row_id=f"{previous_final.row_id}_v2",
        scene_id=previous_final.scene_id,
        chapter_id=previous_final.chapter_id,
        source_bundle_id=previous_final.source_bundle_id,
        source_bundle_hash=previous_final.source_bundle_hash,
        parent_final_scene_row_id=previous_final.row_id,
        content="第二版正文已经变化，旧阅读确认不能批准。",
        status="archived",
    )
    previous_final.status = "superseded"
    previous_final.superseded_by_final_scene_row_id = replacement.row_id
    session.add(replacement)
    state = session.get(SceneRunState, previous_final.scene_id)
    state.current_final_scene_row_id = replacement.row_id
    session.commit()
    changed_hash = client.get(f"/api/v1/chapter-manuscripts/{chapter_id}").json()["data"]["body_hash"]
    assert changed_hash != read_hash

    stale = client.post(
        path,
        json={"read_confirmation": {"body_hash": read_hash, "note": "Read the first body."}},
        headers={"X-Idempotency-Key": "approve-stale-read", "X-Operator-Ref": "author-c"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "CHAPTER_FINAL_BODY_CHANGED"
    assert stale.json()["error"]["details"]["body_hash"] == changed_hash
    session.expire_all()
    assert session.get(ChapterGoal, chapter_id).state != "approved"
    assert session.query(OperationLog).filter_by(event_type="chapter_final_read_confirmed", object_ref=chapter_id).count() == 0

    approve_after_read = client.post(
        path,
        json={"revision_notes": "Ready after reading.", "read_confirmation": {"body_hash": changed_hash, "note": "Read changed body."}},
        headers={"X-Idempotency-Key": "approve-after-read", "X-Operator-Ref": "author-c"},
    )
    assert approve_after_read.status_code == 200, approve_after_read.text
    assert approve_after_read.json()["data"]["project"]["status"] == "completed"
    assert approve_after_read.json()["data"]["approval_note"]["read_confirmed_by"] == "author-c"

    read_log = (
        session.query(OperationLog)
        .filter_by(event_type="chapter_final_read_confirmed", object_type="chapter", object_ref=chapter_id)
        .one()
    )
    assert read_log.payload_json["confirmed_by"] == "author-c"
    assert read_log.payload_json["body_hash"] == changed_hash
    assert read_log.payload_json["note"] == "Read changed body."


def test_project_chapter_run_job_blocks_when_llm_disabled(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    project = _create_project(client, target_chapter_count=1, key="run-job-offline")
    plan = _generate_plan(client, project["project_id"])
    approved = _approve_plan(client, project["project_id"], plan["plan_id"])
    chapter_id = approved["project"]["current_chapter_id"]

    response = client.post(
        f"/api/v1/projects/{project['project_id']}/chapters/{chapter_id}/run-job",
        headers={"X-Idempotency-Key": "run-job-offline"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "LLM_DISABLED_FOR_CHAPTER_RUN"
    assert error["details"]["author_action"] == {
        "title": "需要先启用真实模型",
        "message": "当前还没有可用的 LLM 运行配置。配置 provider、密钥/地址并测试通过后，再开始章节起草。",
        "target_view": "config",
        "target_ref": "system_config:llm",
        "primary_button_label": "去系统配置",
        "evidence_summary": ["llm_enabled=false", "generation_mode=offline_disabled"],
    }

    dashboard_response = client.get(f"/api/v1/projects/{project['project_id']}/dashboard")
    assert dashboard_response.status_code == 200
    runtime = dashboard_response.json()["data"]["runtime"]
    assert runtime["llm_enabled"] is False
    assert runtime["provider_ready"] is False
    assert "missing_routes" in runtime
    assert runtime["next_setup_action"]["target_view"] == "config"


def test_project_chapter_run_job_reuses_existing_running_job(client, session, monkeypatch) -> None:
    project = _create_project(client, target_chapter_count=1, key="run-job-reuse")
    plan = _generate_plan(client, project["project_id"])
    approved = _approve_plan(client, project["project_id"], plan["plan_id"])
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    chapter_id = approved["project"]["current_chapter_id"]
    session.add(
        ChapterRunJob(
            job_id="chapter_run_existing",
            chapter_id=chapter_id,
            status="running",
            job_type="chapter_run_full",
            payload_json={
                "scene_ids": [scene["scene_id"] for scene in approved["plan"]["plan_json"]["chapters"][0]["scenes"]],
                "completed_scene_ids": [],
                "current_scene_id": None,
                "blocked_scene_id": None,
            },
            result_summary_json={
                "scene_ids": [scene["scene_id"] for scene in approved["plan"]["plan_json"]["chapters"][0]["scenes"]],
                "completed_scene_ids": [],
                "current_scene_id": None,
                "blocked_scene_id": None,
                "latest_error": None,
            },
        )
    )
    session.commit()

    started_jobs = []
    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda *args: started_jobs.append(args),
        raising=False,
    )

    response = client.post(
        f"/api/v1/projects/{project['project_id']}/chapters/{chapter_id}/run-job",
        json={},
        headers={"X-Idempotency-Key": "run-job-reuse"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["project"]["status"] == "chapter_running"
    assert payload["run"]["job_id"] == "chapter_run_existing"
    assert payload["run"]["status"] == "running"
    assert started_jobs == []


def test_replaying_the_run_job_request_redispatches_a_chapter_job_that_is_still_pending(
    client, session, monkeypatch
) -> None:
    """B12-07：「运行本章」的派发在提交之后经 after_commit 做；同一个键重放时任务还在等（pending）就再派发一次，
    已经结束的不派发。以前重放一律不派发，提交与派发之间进程退出留下的任务只能等下一次启动恢复。"""
    project = _create_project(client, target_chapter_count=1, key="run-job-replay")
    plan = _generate_plan(client, project["project_id"])
    approved = _approve_plan(client, project["project_id"], plan["plan_id"])
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    chapter_id = approved["project"]["current_chapter_id"]
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda *args: dispatched.append(args),
    )
    path = f"/api/v1/projects/{project['project_id']}/chapters/{chapter_id}/run-job"
    headers = {"X-Idempotency-Key": "run-job-replay"}

    first = client.post(path, json={}, headers=headers)
    assert first.status_code == 200, first.text
    job_id = first.json()["data"]["run"]["job_id"]
    assert first.json()["data"]["run"]["status"] == "pending"
    assert dispatched == [(project["project_id"], chapter_id, job_id)]

    replay = client.post(path, json={}, headers=headers)
    assert replay.status_code == 200
    assert replay.headers.get("X-Idempotency-Status") == "replayed"
    assert dispatched == [(project["project_id"], chapter_id, job_id)] * 2

    with SessionLocal() as db:
        db.get(ChapterRunJob, job_id).status = "completed"
        db.commit()
    again = client.post(path, json={}, headers=headers)
    assert again.status_code == 200
    assert again.headers.get("X-Idempotency-Status") == "replayed"
    assert len(dispatched) == 2


def test_project_chapter_run_failure_keeps_each_path_mapping(client, session, monkeypatch) -> None:
    """「运行结果 → 作品状态」三处共用一张表（B08-27），失败这一格各守各的老规矩：同步「运行本章」
    （测试原语）失败回到「可以运行本章」，后台 worker 失败停在「待处理阻断」。"""

    class ExplodingOrchestrator:
        def __init__(self, db_session) -> None:
            self.session = db_session

        def run_scene(self, scene_id: str) -> dict:
            raise RuntimeError("provider exploded mid-scene")

    monkeypatch.setattr("novel_system.services.chapter_runner.Orchestrator", ExplodingOrchestrator)
    project_id = client.post(
        "/api/v2/projects",
        json={"title": "雨城旧信", "outline_text": "林昭翻开旧案卷。"},
        headers={"X-Idempotency-Key": "run-failure-project"},
    ).json()["data"]["project"]["project_id"]
    chapter_id = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters",
        json={"title": "旧信", "current": True, "with_scene": True},
        headers={"X-Idempotency-Key": "run-failure-chapter"},
    ).json()["data"]["chapter"]["chapter_id"]

    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/run",
        json={},
        headers={"X-Idempotency-Key": "run-failure-sync"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["run"]["status"] == "failed"
    assert data["project"]["status"] == "chapter_ready"
    assert client.get(f"/api/v1/projects/{project_id}/dashboard").json()["data"]["next_action"] == "run_current_chapter"

    from novel_system.services.projects import _run_project_chapter_job_worker

    job_id = data["run"]["job_id"]
    _run_project_chapter_job_worker(project_id, chapter_id, job_id)
    session.expire_all()
    assert session.get(ChapterRunJob, job_id).status == "failed"
    assert session.get(StoryProject, project_id).status == "chapter_blocked"
    assert client.get(f"/api/v1/projects/{project_id}/dashboard").json()["data"]["next_action"] == "resolve_blocker"


def test_project_chapter_flow_request_contracts_are_strict(client) -> None:
    base = "/api/v1/projects/missing-project/chapters/missing-chapter"
    cases = [
        # 离线演示已退役：offline_demo 不再是契约字段，和别的未知字段一样 422
        (f"{base}/run-job", {"offline_demo": False}, "body.offline_demo", "extra_forbidden"),
        (f"{base}/run-job", {"allow_demo": True}, "body.allow_demo", "extra_forbidden"),
        (f"{base}/run-job", {"unexpected": 1}, "body.unexpected", "extra_forbidden"),
        (f"{base}/approve-final", {"revision_notes": "x", "unexpected": 1}, "body.unexpected", "extra_forbidden"),
        (
            f"{base}/approve-final",
            {"read_confirmation": {"body_hash": "h", "unexpected": 1}},
            "body.read_confirmation.unexpected",
            "extra_forbidden",
        ),
        # 确认定稿就是「读过并定稿」：读到的哪一份必须带来（B2-1）
        (f"{base}/approve-final", {"revision_notes": "x"}, "body.read_confirmation", "missing"),
    ]

    for path, body, expected_field, expected_type in cases:
        response = client.post(path, json=body, headers={"X-Idempotency-Key": f"strict-{expected_field}-{expected_type}"})
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "REQUEST_VALIDATION_FAILED"
        assert any(
            issue["field"] == expected_field and issue["type"] == expected_type
            for issue in error["details"]["issues"]
        )


def test_project_chapter_run_service_takes_no_demo_flags(session) -> None:
    service = ProjectChapterFlowService(session)
    for legacy_flag in ("offline_demo", "allow_demo"):
        with pytest.raises(TypeError):
            service.prepare_chapter_run_job("missing-project", "missing-chapter", **{legacy_flag: True})


def test_project_chapter_flow_request_length_boundaries(client) -> None:
    base = "/api/v1/projects/missing-project/chapters/missing-chapter"
    read = {"body_hash": "h" * 128, "note": "x" * 1000}
    accepted_cases = [
        (f"{base}/approve-final", {"read_confirmation": {"body_hash": "h"}}),
        (f"{base}/approve-final", {"revision_notes": "x" * 2000, "read_confirmation": read}),
    ]
    for index, (path, body) in enumerate(accepted_cases):
        kwargs = {"headers": {"X-Idempotency-Key": f"accepted-boundary-{index}"}}
        if body is not None:
            kwargs["json"] = body
        response = client.post(path, **kwargs)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    rejected_cases = [
        (f"{base}/approve-final", {"revision_notes": "x" * 2001, "read_confirmation": {"body_hash": "h"}}, "body.revision_notes"),
        (f"{base}/approve-final", {"read_confirmation": {**read, "note": "x" * 1001}}, "body.read_confirmation.note"),
        (f"{base}/approve-final", {"read_confirmation": {"body_hash": "h" * 129}}, "body.read_confirmation.body_hash"),
    ]
    for index, (path, body, expected_field) in enumerate(rejected_cases):
        response = client.post(path, json=body, headers={"X-Idempotency-Key": f"rejected-boundary-{index}"})
        assert response.status_code == 422, response.text
        issues = response.json()["error"]["details"]["issues"]
        assert any(issue["field"] == expected_field and issue["type"] == "string_too_long" for issue in issues)

    no_body = client.post(f"{base}/approve-final", headers={"X-Idempotency-Key": "rejected-boundary-no-body"})
    assert no_body.status_code == 422, no_body.text


def test_reopen_final_cascades_approval_state_and_is_idempotent(client, session) -> None:
    project = _create_project(client, target_chapter_count=3, key="reopen-final")
    plan = _generate_plan(client, project["project_id"])
    _approve_plan(client, project["project_id"], plan["plan_id"])
    chapter_ids = [
        row[0]
        for row in session.execute(
            select(ChapterGoal.chapter_id)
            .where(ChapterGoal.project_id == project["project_id"])
            .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc())
        ).all()
    ]
    assert len(chapter_ids) == 3
    db_project = session.get(StoryProject, project["project_id"])
    db_project.approved_chapter_ids_json = list(chapter_ids)
    db_project.current_chapter_id = None
    db_project.status = "completed"
    for chapter_id in chapter_ids:
        session.get(ChapterGoal, chapter_id).state = "approved"
    session.commit()

    target_id = chapter_ids[1]
    path = f"/api/v1/projects/{project['project_id']}/chapters/{target_id}/reopen-final"
    headers = {"X-Idempotency-Key": "reopen-final-cascade", "X-Operator-Ref": "author-reopener"}
    response = client.post(path, json={"reason": "Revise the causal bridge."}, headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["invalidated_chapter_ids"] == chapter_ids[1:]
    assert payload["project"]["approved_chapter_ids"] == chapter_ids[:1]
    assert payload["project"]["current_chapter_id"] == target_id
    assert payload["project"]["status"] == "chapter_ready"
    session.expire_all()
    assert session.get(ChapterGoal, chapter_ids[0]).state == "approved"
    assert session.get(ChapterGoal, target_id).state == "draft"
    assert session.get(ChapterGoal, chapter_ids[2]).state == "planned"

    audit = session.execute(
        select(OperationLog).where(
            OperationLog.event_type == "chapter_final_reopened",
            OperationLog.object_ref == target_id,
        )
    ).scalar_one()
    assert audit.payload_json["reason"] == "Revise the causal bridge."
    assert audit.payload_json["invalidated_chapter_ids"] == chapter_ids[1:]
    assert audit.payload_json["actor_ref"] == "author-reopener"

    replay = client.post(path, json={"reason": "Revise the causal bridge."}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    assert replay.json()["data"]["invalidated_chapter_ids"] == chapter_ids[1:]
    reopen_log_count = session.execute(
        select(OperationLog).where(OperationLog.event_type == "chapter_final_reopened")
    ).scalars().all()
    assert len(reopen_log_count) == 1

    no_longer_approved = client.post(
        path,
        json={"reason": "Try again."},
        headers={"X-Idempotency-Key": "reopen-final-not-approved"},
    )
    assert no_longer_approved.status_code == 409
    assert no_longer_approved.json()["error"]["code"] == "CHAPTER_FINAL_NOT_APPROVED"


def test_reopen_final_request_contract_is_strict(client) -> None:
    path = "/api/v1/projects/missing-project/chapters/missing-chapter/reopen-final"
    cases = [
        ({}, "body.reason", "missing"),
        ({"reason": "   "}, "body.reason", "value_error"),
        ({"reason": "x" * 1001}, "body.reason", "string_too_long"),
        ({"reason": "valid", "unexpected": True}, "body.unexpected", "extra_forbidden"),
    ]
    for index, (body, expected_field, expected_type) in enumerate(cases):
        response = client.post(
            path,
            json=body,
            headers={"X-Idempotency-Key": f"reopen-strict-{index}"},
        )
        assert response.status_code == 422, response.text
        issues = response.json()["error"]["details"]["issues"]
        assert any(issue["field"] == expected_field and issue["type"] == expected_type for issue in issues)

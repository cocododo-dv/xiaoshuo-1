"""QA3 回归：对「已物化出章节/场景」的项目再次确认（re-approve）上游雪花步，
不得因 chapter_states.chapter_id UNIQUE 约束而 500。

复现的 BUG-1：approve_step → ProjectRuntimeInvalidationService.invalidate_for_snowflake_step
里 263-loop 与 backtracks._apply_block 在 autoflush=False 的会话下各建一遍同一 chapter 的
ChapterState（session.get 看不到 pending 新对象）→ 同一 flush 批次重复 INSERT → IntegrityError。

此前的 re-approve 测试都没先物化章节，故 263-loop early-return，从不触发。本测试先物化再 re-approve。
"""
from __future__ import annotations

import pytest

from novel_system.db.models import ChapterState

pytestmark = pytest.mark.usefixtures("skeleton_snowflake_llm_on")


ALL_STEPS = [
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "character_sheets",
    "short_synopsis",
    "character_synopses",
    "long_synopsis",
    "character_bibles",
    "scene_list",
    "scene_details",
]


def _create_project(client, key: str) -> dict:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": f"QA3再批准 {key}",
            "genre": "悬疑",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": "样例大纲第一行。\n样例大纲第二行。\n样例大纲第三行。",
        },
        headers={"X-Idempotency-Key": f"qa3-reappr-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _generate(client, pid: str, step_key: str, payload: dict | None = None, *, attempt: int = 1) -> dict:
    # 每次生成都是一次新的作者意图：幂等键带上第几次（旧版借 force_new 字段区分，那个字段已删）
    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/{step_key}/generate",
        json=payload or {},
        headers={"X-Idempotency-Key": f"qa3-reappr-gen-{pid}-{step_key}-{attempt}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _approve(client, pid: str, step_key: str):
    return client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-approve-{pid}-{step_key}"},
    )


def _drop_chapter_states(session) -> int:
    """复现 BUG-1 触发条件：章节存在但无 ChapterState 行。
    seed_fixture_works（work-a/work-b）与 catalog.py（FE 章节编排冷启动/导入）建章时都不建 ChapterState，
    只有 approve_outline_plan 会建——本测试经后者建章，故显式删去 ChapterState 以还原 demo/目录态。"""
    rows = session.query(ChapterState).all()
    n = len(rows)
    for r in rows:
        session.delete(r)
    session.commit()
    return n


def test_reapprove_upstream_step_after_materialization_does_not_500(client, session):
    project = _create_project(client, "broad")
    pid = project["project_id"]

    # 1) 一次性批准全部 10 步（首次批准，无 previous → 不触发 invalidate）
    for step_key in ALL_STEPS:
        _generate(client, pid, step_key)
        assert _approve(client, pid, step_key).status_code == 200

    # 2) 物化 + 批准大纲：materialize 仅建 pending OutlinePlan，章节要 outline/approve 才落库
    materialized = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-materialize-{pid}"},
    )
    assert materialized.status_code == 200, materialized.text
    outline_approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-outline-approve-{pid}"},
    )
    assert outline_approved.status_code == 200, outline_approved.text

    catalog = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    scene_total = sum(len(c["scenes"]) for c in catalog["chapters"])
    assert len(catalog["chapters"]) >= 1 and scene_total >= 1, "物化未产出章节/场景，前置不成立"

    # 还原 demo/目录态：章节存在但无 ChapterState 行（这是 BUG-1 的真实触发前置）
    assert _drop_chapter_states(session) >= 1, "前置：approve_outline_plan 应已建 ChapterState 供删除"

    # 3) 再次确认上游步 book_brief（broad impact 覆盖全部已物化场景）
    #    BUG-1：263-loop 与 _apply_block 各建一遍同一 chapter 的 ChapterState → flush 撞 UNIQUE → 500。
    #    修复（263-loop 后 flush）后应 200。
    _generate(client, pid, "book_brief", attempt=2)
    resp = _approve(client, pid, "book_brief")
    assert resp.status_code == 200, f"re-approve 上游步在已物化项目上崩溃: {resp.status_code} {resp.text}"

    body = resp.json()
    assert body["ok"] is True
    # 该步确实落为 approved（后端真批准，非静默失败）
    step = body["data"]["step"]
    status = step.get("status") or step.get("artifact", {}).get("status")
    assert status == "approved", f"book_brief 未真正批准: {status}"


def test_reapprove_scoped_step_after_materialization_does_not_500(client, session):
    """scene_details 的 scoped 路径（_apply_block 带具体 chapter_id）同样不得 500。"""
    project = _create_project(client, "scoped")
    pid = project["project_id"]
    for step_key in ALL_STEPS:
        _generate(client, pid, step_key)
        assert _approve(client, pid, step_key).status_code == 200
    materialized = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-materialize2-{pid}"},
    )
    assert materialized.status_code == 200, materialized.text
    outline_approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-outline-approve2-{pid}"},
    )
    assert outline_approved.status_code == 200, outline_approved.text
    _drop_chapter_states(session)

    _generate(client, pid, "scene_details", attempt=2)
    resp = _approve(client, pid, "scene_details")
    assert resp.status_code == 200, f"re-approve scene_details 崩溃: {resp.status_code} {resp.text}"


def test_repatch_identical_approved_draft_does_not_revert(client, session):
    """QA3 回归（BUG-2 后端守卫）：对已批准步用相同 canonical 草稿 re-PATCH（前端无谓 re-push 的
    "内容未变"语义）不得新建 pending_review 版本、把已确认步打回待审。"""
    from sqlalchemy import select
    from novel_system.db.models import SnowflakeStepRun
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project = _create_project(client, "noop-guard")
    pid = project["project_id"]
    _generate(client, pid, "book_brief")
    assert _approve(client, pid, "book_brief").status_code == 200

    run = session.execute(
        select(SnowflakeStepRun).where(
            SnowflakeStepRun.project_id == pid,
            SnowflakeStepRun.step_key == "book_brief",
            SnowflakeStepRun.status == "approved",
        )
    ).scalars().first()
    assert run is not None, "前置：book_brief 应已批准"
    approved_version = run.version
    approved_draft = dict(run.draft_json or {})

    SnowflakeWorkspaceService(session).update_step(pid, "book_brief", {"draft": approved_draft})
    session.commit()
    session.expire_all()

    latest = session.execute(
        select(SnowflakeStepRun)
        .where(SnowflakeStepRun.project_id == pid, SnowflakeStepRun.step_key == "book_brief")
        .order_by(SnowflakeStepRun.version.desc())
    ).scalars().first()
    assert latest.status == "approved", f"identical re-PATCH 把已批准步打回了 {latest.status}"
    assert latest.version == approved_version, "identical re-PATCH 不应新建版本"


def test_repatch_only_frontend_cache_metadata_keeps_approval_chain(client, session):
    """雪花原型把跨会话 UI 账本寄存在 ``fe_*`` 字段里。确认任意后续步骤都会更新
    ``book_brief.fe_meta`` / ``fe_t``；这些缓存元数据不是故事内容，不能让第 1 步重新待审，
    更不能把已经批准的下游九步全部打成 stale。"""
    from sqlalchemy import select
    from novel_system.db.models import SnowflakeStepRun

    project = _create_project(client, "frontend-cache-metadata")
    pid = project["project_id"]
    for step_key in ("book_brief", "one_sentence_summary"):
        _generate(client, pid, step_key)
        assert _approve(client, pid, step_key).status_code == 200

    first = session.execute(
        select(SnowflakeStepRun).where(
            SnowflakeStepRun.project_id == pid,
            SnowflakeStepRun.step_key == "book_brief",
            SnowflakeStepRun.status == "approved",
        )
    ).scalars().one()
    approved_version = first.version
    metadata_only = {
        **dict(first.draft_json or {}),
        "fe_t": 1_777_777,
        "fe_meta": {
            "revs": {"one_sentence_summary": 1},
            "confirmRevs": {"one_sentence_summary": 1},
            "history": [{"t": 1_777_777, "action": "确认", "key": "logline"}],
        },
    }

    patched = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief",
        json={"draft": metadata_only},
    )
    assert patched.status_code == 200, patched.text
    # 前端照常补发 approve；元数据 PATCH 若保持原批准态，这个调用应天然幂等。
    approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/approve",
        json={},
        headers={"X-Idempotency-Key": f"qa3-reappr-fe-meta-approve-{pid}"},
    )
    assert approved.status_code == 200, approved.text

    session.expire_all()
    latest = session.execute(
        select(SnowflakeStepRun)
        .where(SnowflakeStepRun.project_id == pid, SnowflakeStepRun.step_key == "book_brief")
        .order_by(SnowflakeStepRun.version.desc())
    ).scalars().first()
    downstream = session.execute(
        select(SnowflakeStepRun)
        .where(SnowflakeStepRun.project_id == pid, SnowflakeStepRun.step_key == "one_sentence_summary")
        .order_by(SnowflakeStepRun.version.desc())
    ).scalars().first()
    assert latest.status == "approved"
    assert latest.version == approved_version, "只改 fe_* 缓存元数据不应制造故事内容新版本"
    assert latest.draft_json["fe_meta"] == metadata_only["fe_meta"], "跨会话 UI 账本仍需写穿保存"
    assert downstream.status == "approved", "缓存元数据变化不应让下游故事步骤失效"


def test_first_upstream_approval_keeps_existing_downstream_draft_approvable(client):
    """导入/旧缓存可以先把十步草稿全部存成待审，再由前端按顺序补批准。
    第一次批准上游并不是内容修订，不应把已有下游待审稿误判成 stale。"""
    project = _create_project(client, "first-approval-chain")
    pid = project["project_id"]
    _generate(client, pid, "book_brief")
    _generate(client, pid, "one_sentence_summary")

    first = _approve(client, pid, "book_brief")
    assert first.status_code == 200, first.text
    workspace = first.json()["data"]["workspace"]
    downstream = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert downstream["status"] == "pending_review"

    second = _approve(client, pid, "one_sentence_summary")
    assert second.status_code == 200, second.text
    assert second.json()["data"]["step"]["status"] == "approved"


def _materialize_all(client, pid: str) -> None:
    for step_key in ALL_STEPS:
        _generate(client, pid, step_key)
        assert _approve(client, pid, step_key).status_code == 200
    materialized = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": f"runtime-scoped-materialize-{pid}"},
    )
    assert materialized.status_code == 200, materialized.text
    outline_approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": f"runtime-scoped-outline-{pid}"},
    )
    assert outline_approved.status_code == 200, outline_approved.text


def test_reapproving_scene_details_invalidates_only_the_changed_scenes_runtime(client, session):
    """R9 移植（3a，原 v1 规划器用例）：确认一版改过一场的第 10 步（工作台总是带 sync_catalog），
    只有改过的那一场的执行契约 / 草稿 / QC 过期、运行态打回 needs_replan；没改的那一场原样保留。"""
    from novel_system.db.models import QcReport, SceneCard, SceneDraft, SceneExecutionContract, SceneRunState
    from novel_system.services.scene_execution import SceneExecutionContractService

    pid = _create_project(client, "runtime-scoped")["project_id"]
    _materialize_all(client, pid)
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    rows = [dict(row) for row in next(s for s in workspace["steps"] if s["step_key"] == "scene_details")["draft"]["scenes"]]
    changed, unchanged = rows[0], rows[1]

    contracts: dict[str, str] = {}
    for index, row in enumerate((changed, unchanged), start=1):
        scene_id = row["scene_id"]
        contracts[scene_id] = SceneExecutionContractService(session).generate(scene_id).contract_id
        session.commit()
        state = session.get(SceneRunState, scene_id)
        assert state is not None
        chapter_id = session.get(SceneCard, scene_id).chapter_id
        session.add(
            SceneDraft(
                row_id=f"scene_draft_runtime_scoped_{index}",
                scene_id=scene_id,
                chapter_id=chapter_id,
                stage="neutral_draft",
                content=f"old draft {index}",
                source_bundle_id=f"bundle_runtime_scoped_{index}",
                source_bundle_hash=f"hash_runtime_scoped_{index}",
            )
        )
        session.add(
            QcReport(
                qc_report_id=f"qc_runtime_scoped_{index}",
                scene_id=scene_id,
                chapter_id=chapter_id,
                qc_type="soft_qc",
                source_draft_row_id=f"scene_draft_runtime_scoped_{index}",
                source_bundle_id=f"bundle_runtime_scoped_{index}",
                resolution_code="soft_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
            )
        )
        state.scene_status = "archived"
        state.current_neutral_draft_row_id = f"scene_draft_runtime_scoped_{index}"
        state.current_qc_report_id = f"qc_runtime_scoped_{index}"
    session.commit()

    for row in rows:
        if row["scene_id"] == changed["scene_id"]:
            row["summary"] = "作者改了这一场：她没有追送信人，而是回家把信烧了。"
    patched = client.patch(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details", json={"draft": {"scenes": rows}})
    assert patched.status_code == 200, patched.text
    approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/approve",
        json={"sync_catalog": True},
        headers={"X-Idempotency-Key": f"runtime-scoped-reapprove-{pid}"},
    )
    assert approved.status_code == 200, approved.text
    runtime = approved.json()["data"]["impact"]["runtime"]
    assert runtime["affected_scene_ids"] == [changed["scene_id"]], runtime

    session.expire_all()
    assert session.get(SceneExecutionContract, contracts[changed["scene_id"]]).status == "stale"
    assert session.get(SceneDraft, "scene_draft_runtime_scoped_1").status == "stale"
    assert session.get(QcReport, "qc_runtime_scoped_1").status == "stale"
    changed_state = session.get(SceneRunState, changed["scene_id"])
    assert changed_state.scene_status == "needs_replan"
    assert changed_state.current_neutral_draft_row_id is None and changed_state.current_qc_report_id is None

    assert session.get(SceneExecutionContract, contracts[unchanged["scene_id"]]).status == "active"
    assert session.get(SceneDraft, "scene_draft_runtime_scoped_2").status != "stale"
    assert session.get(QcReport, "qc_runtime_scoped_2").status != "stale"
    assert session.get(SceneRunState, unchanged["scene_id"]).scene_status == "archived"


def test_scene_content_treats_the_default_rendering_mode_as_absent() -> None:
    """生成器写的场景行没有 rendering_mode，前端上行的行总带着 full：两者说的是同一件事（与 semantic_payload
    剥默认值同一口径）。以前运行时失效按内容投影比对时把每一场都判成「改了」。"""
    from novel_system.services.snowflake_staleness import scene_row_content

    generated = {"scene_id": "S1", "summary": "取账本"}
    pushed = {"scene_id": "S1", "summary": "取账本", "rendering_mode": "full", "spine": ""}
    assert scene_row_content(generated) == scene_row_content(pushed)
    assert scene_row_content({**pushed, "rendering_mode": "summary"}) != scene_row_content(generated)

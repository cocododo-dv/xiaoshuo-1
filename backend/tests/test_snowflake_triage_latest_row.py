"""B06-02：工作台的分诊列表与物化用同一个口径——每一场只看最新的一条分诊记录。

历史数据可能一场多行（旧版保存端点每次追加一行；带 triage_id 的保存还能把旧行改成「最新」）。
物化、回流、设计上下文早就经 ``snowflake_triage.latest_triage_rows`` 取最新一行；工作台的列表与
整理闸门却按库里的扫描顺序取行——作者看到「通过」，整理时这一场却按「待删」不建卡。
"""

from __future__ import annotations

from novel_system.db.models import SnowflakeSceneTriageItem
from tests.support.snowflake import (
    RENDER_PROJECT_ID as PROJECT_ID,
    materialize_render_project as _materialize,
    scene_plan as _plan,
    seed_render_project as _seed,
)


def _triage_row(plan, triage_id: str, status: str, *, created_at: str, updated_at: str) -> SnowflakeSceneTriageItem:
    return SnowflakeSceneTriageItem(
        triage_id=triage_id,
        project_id=PROJECT_ID,
        scene_plan_id=plan.scene_plan_id,
        scene_id=plan.scene_id,
        recommended_status="pass",
        manual_status=status,
        effective_status=status,
        blocking=1 if status in {"rewrite", "cut"} else 0,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_workspace_triage_list_and_gate_read_the_latest_row_like_materialization(session) -> None:
    service = _seed(session)
    plan = _plan(session, "u2")
    # 先插入的一行是作者此刻的裁定（改得最晚）：待删；后插入的一行是更早的「通过」。
    session.add(_triage_row(plan, "triage-cut", "cut", created_at="2026-09-01T00:00:00", updated_at="2026-09-03T00:00:00"))
    session.flush()
    session.add(_triage_row(plan, "triage-pass", "pass", created_at="2026-09-02T00:00:00", updated_at="2026-09-02T00:00:00"))
    session.flush()

    workspace = service.workspace(PROJECT_ID)
    item = next(entry for entry in workspace["triage_items"] if entry["scene_plan_id"] == plan.scene_plan_id)
    assert item["triage_id"] == "triage-cut"
    assert item["effective_status"] == "cut"
    gate_kinds = {
        (entry["kind"], entry.get("scene_plan_id")) for entry in workspace["materialization_gate"]["items"]
    }
    assert ("triage_cut", plan.scene_plan_id) in gate_kinds

    # 物化与列表说的是同一件事：这一场不建卡
    plan_json = _materialize(session, service)
    scene_ids = {scene["scene_id"] for chapter in plan_json["chapters"] for scene in chapter["scenes"]}
    assert plan.scene_id not in scene_ids


def test_gate_counts_materializable_scenes_from_the_latest_rows(session) -> None:
    service = _seed(session)
    for index, row_uid in enumerate(("u1", "u2", "u3")):
        plan = _plan(session, row_uid)
        session.add(
            _triage_row(plan, f"triage-cut-{index}", "cut", created_at="2026-09-01T00:00:00", updated_at="2026-09-05T00:00:00")
        )
        session.flush()
        # 更早的一条「通过」后插入：扫描顺序在后，但不是最新
        session.add(
            _triage_row(plan, f"triage-pass-{index}", "pass", created_at="2026-09-02T00:00:00", updated_at="2026-09-02T00:00:00")
        )
        session.flush()

    gate = service.workspace(PROJECT_ID)["materialization_gate"]
    assert gate["status"] == "blocked"
    assert any(entry["kind"] == "no_materializable_scene" for entry in gate["items"])

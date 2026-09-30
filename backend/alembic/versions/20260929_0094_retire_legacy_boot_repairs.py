"""Retire the pre-v3 style-reference repairs that ran on every boot (data only).

Revision ID: 20260929_0094
Revises: 20260924_0092
Create Date: 2026-09-29

B03-29：启动恢复每次开机都扫两遍旧版遗留的状态，第一次 v3 开机之后就再也没有候选了：
- (a) ``style_reference_runs``：旧抽取流程（``RunOrchestrator``，2026-09-23 v3 P3 删除）留下的「运行中」run
  （``status = running`` 且 ``dispatch_state`` 为 queued / running）标 failed（``STYLE_REFERENCE_RUN_RETIRED``，
  可重学）。学习文风作业的血缘 run（``dispatch_state = learn_job``）不动——它们的状态由作业写。
- (b) ``style_reference_books``：旧的书上 JSON 游标分类留下、状态还是 ingesting / cancelling 却没有排队或运行中
  分类作业的书标 failed（「继续分类」会给它建新作业）。v3 里书的状态与分类作业同一事务写、作业失败 / 取消时
  一并落定，这种书只可能来自升级前。

这两件事在迁移里做一次，开机不再扫。只改数据、不改结构；幂等（再跑一次没有候选）；降级是空操作。
历史迁移是冻结的显式 SQL，不导入应用 ORM。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260929_0094"
down_revision = "20260924_0092"
branch_labels = None
depends_on = None

RETIRED_CODE = "STYLE_REFERENCE_RUN_RETIRED"
RETIRED_MESSAGE = "旧的抽取流程已下线:请对这本书「学习文风」"


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC).isoformat()
    legacy_runs = connection.execute(
        sa.text(
            "SELECT run_id, coverage_json FROM style_reference_runs "
            "WHERE status = 'running' AND dispatch_state IN ('queued', 'running')"
        )
    ).fetchall()
    for run_id, coverage_raw in legacy_runs:
        coverage = _load_json(coverage_raw)
        coverage["failure_reason"] = RETIRED_CODE
        coverage["retryable"] = True
        connection.execute(
            sa.text(
                "UPDATE style_reference_runs SET status = 'failed', dispatch_state = 'failed', "
                "coverage_json = :coverage, heartbeat_at = :now, finished_at = :now, "
                "error_code = :code, error_text = :message, retryable = 1 "
                "WHERE run_id = :run_id AND status = 'running' AND dispatch_state IN ('queued', 'running')"
            ),
            {
                "coverage": json.dumps(coverage, ensure_ascii=False),
                "now": now,
                "code": RETIRED_CODE,
                "message": RETIRED_MESSAGE,
                "run_id": run_id,
            },
        )
    connection.execute(
        sa.text(
            "UPDATE style_reference_books SET status = 'failed', updated_at = :now "
            "WHERE status IN ('ingesting', 'cancelling') AND NOT EXISTS ("
            "SELECT 1 FROM style_reference_jobs AS job WHERE job.book_id = style_reference_books.book_id "
            "AND job.kind = 'classify' AND job.state IN ('queued', 'running'))"
        ),
        {"now": now},
    )


def downgrade() -> None:
    # 数据修复不可撤回（被标 failed 的旧 run / 书在任何版本里都不能续跑）。
    pass

"""雪花分诊记录的读取口径（叶子模块：只依赖 ORM，谁都可以引用而不成环）。

2026-09-15 阶段 N：作者裁定「该重写」或「待删」的场不物化——不建卡、已建的卡进回收站——但不再阻断全书。
原著的做法是 No 的场标记待删、下一稿再删；Maybe 的场修完再分诊；哪一场都不该把整本书的结构卡住。
分章体检、设计上下文与工作台共用这里的两条规则：每场只看**最新**一条分诊记录；排除的裁定集合只有一份。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeSceneTriageItem

#: 作者裁定为该重写 / 待删的场：不物化、回流进回收站、节奏按 0 计。cut 是作者专用，模型不能建议。
EXCLUDED_TRIAGE_STATUSES: frozenset[str] = frozenset({"rewrite", "cut"})


def _row_key(row: SnowflakeSceneTriageItem) -> tuple[str, str, str]:
    return (str(row.updated_at or ""), str(row.created_at or ""), str(row.triage_id or ""))


def latest_triage_rows(session: Session, project_id: str) -> dict[str, SnowflakeSceneTriageItem]:
    """每个场景计划最新的一条分诊记录（历史数据可能一场多行；保存端点现在按场对回已有记录）。"""
    rows = session.execute(
        select(SnowflakeSceneTriageItem).where(SnowflakeSceneTriageItem.project_id == project_id)
    ).scalars().all()
    latest: dict[str, SnowflakeSceneTriageItem] = {}
    for row in rows:
        current = latest.get(row.scene_plan_id)
        if current is None or _row_key(row) > _row_key(current):
            latest[row.scene_plan_id] = row
    return latest


def latest_triage_plan_ids(session: Session, project_id: str, statuses: frozenset[str]) -> set[str]:
    """effective_status 落在 ``statuses`` 里的场景计划 id（按最新记录）。"""
    return {
        plan_id
        for plan_id, row in latest_triage_rows(session, project_id).items()
        if plan_id and str(row.effective_status or "").strip().lower() in statuses
    }


def excluded_scene_plan_ids(session: Session, project_id: str) -> set[str]:
    return latest_triage_plan_ids(session, project_id, EXCLUDED_TRIAGE_STATUSES)

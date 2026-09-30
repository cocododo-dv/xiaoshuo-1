"""孤儿场：作者从 09 删掉了它，但目录里的场景卡可能已经有正文——分章面板的 blocker 与它的两个去向。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import OperationLog, SnowflakeScenePlan, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.trash import TrashService

#: 孤儿场的两个合法去向。blocker 文案「请先决定是一并删除还是保留」承诺的就是这两个。
ORPHAN_RESOLUTIONS = ("discard", "keep")


def orphaned_payload(session: Session, project_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id,
            SnowflakeScenePlan.orphaned_flag == 1,
            SnowflakeScenePlan.removed_at.is_(None),
        )
    ).scalars().all()
    return [
        {
            "scene_plan_id": plan.scene_plan_id,
            "scene_id": plan.scene_id,
            "title": plan.title or plan.summary or plan.scene_id,
            "orphaned": True,
        }
        for plan in rows
    ]


def resolve_orphan(
    session: Session,
    project_id: str,
    scene_plan_id: str,
    *,
    action: str,
    actor_ref: str = "operator",
) -> dict[str, Any]:
    """处置一个孤儿场。

    没有这个动作时 ``orphaned_flag`` 是只写字段：blocker 永久挂在分章面板上，
    「确认分章」再也点不动，而提示语还在说「请先决定」——一个没有对应动作的决定。

    - ``discard``：正文也不要了。场景卡进回收站（可恢复，不是物理删除——那上面
      可能有作者写了几千字的稿子），计划行软删。
    - ``keep``：正文留在目录里，只是不再属于构思侧的场景列表。计划行软删，场景卡
      原样不动。

    两条路都软删计划行，孤儿警告因此**持久**消失（``orphaned_payload`` 过滤
    ``removed_at``）；只清 ``orphaned_flag`` 不行，下一次 PATCH 场景列表会立刻
    把它重新标成孤儿，作者陷在同一个循环里。
    """
    if action not in ORPHAN_RESOLUTIONS:
        raise DomainError(
            "SNOWFLAKE_ORPHAN_ACTION_INVALID",
            f"孤儿场的处置只能是 {' / '.join(ORPHAN_RESOLUTIONS)}。",
            status_code=400,
        )
    plan = session.get(SnowflakeScenePlan, scene_plan_id)
    if plan is None or plan.project_id != project_id:
        raise DomainError("SNOWFLAKE_SCENE_PLAN_NOT_FOUND", "找不到这一场。", status_code=404)
    if not plan.orphaned_flag or plan.removed_at:
        raise DomainError(
            "SNOWFLAKE_SCENE_NOT_ORPHANED",
            "这一场不是待处置的孤儿场——可能已经处置过，或者它又回到了场景列表里。",
            status_code=409,
        )

    trashed_scene = False
    if action == "discard":
        # 场景卡走回收站而不是物理删除：作者随时可以在回收站里反悔。
        TrashService(session).trash_scene_in_project(project_id, plan.scene_id, actor_ref=actor_ref)
        trashed_scene = True

    plan.orphaned_flag = 0
    plan.removed_at = utcnow()
    plan.removed_by = actor_ref
    plan.chapter_plan_id = None
    session.add(
        OperationLog(
            event_type="snowflake_scene_plan_orphan_resolved",
            object_type="snowflake_scene_plan",
            object_ref=plan.scene_plan_id,
            payload_json={
                "project_id": project_id,
                "scene_id": plan.scene_id,
                "action": action,
                "trashed_scene_card": trashed_scene,
                "actor_ref": actor_ref,
            },
        )
    )
    session.flush()
    return {
        "scene_plan_id": plan.scene_plan_id,
        "scene_id": plan.scene_id,
        "action": action,
        "trashed_scene_card": trashed_scene,
        "orphaned_remaining": len(orphaned_payload(session, project_id)),
    }

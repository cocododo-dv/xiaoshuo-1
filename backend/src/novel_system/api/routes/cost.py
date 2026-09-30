"""成本看板端点（结果闭环治理设计 §5.8/§6.3/§10）。

GET /api/v2/projects/{project_id}/cost-summary   —— 场景 / 章节 / 全书级 token 聚合（金额只算有单价的模型）。
默认返回项目级；``?scene_id=`` / ``?chapter_id=`` 下钻。
GET /api/v2/projects/{project_id}/cost-dashboard —— 看板一读聚合：summary + 近 N 天
趋势 + 模型 / 节点 / 章节构成 + 用 token 最多的调用 + 全局用量读数。两者均只读，不改任何状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session, request_id_of
from novel_system.api.response import ok
from novel_system.services import cost_aggregation
from novel_system.services.llm_usage_readings import usage_readings

router = APIRouter(tags=["cost"])


@router.get("/api/v2/projects/{project_id}/cost-summary")
def project_cost_summary(
    project_id: str,
    request: Request,
    scene_id: str | None = None,
    chapter_id: str | None = None,
    session: Session = Depends(get_session),
):
    if scene_id:
        payload = {"level": "scene", "summary": cost_aggregation.scene_cost(session, scene_id)}
    elif chapter_id:
        payload = {"level": "chapter", "summary": cost_aggregation.chapter_cost(session, chapter_id)}
    else:
        payload = {"level": "project", "summary": cost_aggregation.project_cost(session, project_id)}
    payload["quota"] = usage_readings(session, project_id=project_id)
    return ok(payload, req_id=request_id_of(request))


@router.get("/api/v2/projects/{project_id}/cost-dashboard")
def project_cost_dashboard(
    project_id: str,
    request: Request,
    days: int = cost_aggregation.DASHBOARD_DEFAULT_DAYS,
    session: Session = Depends(get_session),
):
    payload = cost_aggregation.project_cost_dashboard(session, project_id, days=days)
    payload["quota"] = usage_readings(session, project_id=project_id)
    return ok(payload, req_id=request_id_of(request))

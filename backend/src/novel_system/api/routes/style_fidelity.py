"""风格参考 v3（P5b）—「像不像」读数的两个读接口。

- ``GET /api/v1/scenes/{scene_id}/style-fidelity``：这一场每个阶段最新的读数（首稿 / 定向修改 / 补丁 / 终稿 / 对照检查）、
  风格步与补丁的决定、最近的参考评审分；
- ``GET /api/v1/projects/{project_id}/style-fidelity``：作品的读数走势、近期常见偏差、按维平均（确定性分与评审分分开）。

读数入库只有 ``services.style_reference.readings.record_fidelity_reading`` 一个入口；这里的读接口都不写库。
对照检查（``/api/v2/style-reference/checks*``）在风格参考路由包里（``api/routes/style_reference/checks.py``）。
旧的「回测」接口（``/profiles/{id}/validate``、``/reports``）与它的报告表都已删除（迁移 0091）；单读一条读数的
``GET /api/v2/style-reference/readings/{id}`` 没有界面调用（读数随场景 / 作品 / 对照检查的载荷给出），2026-09-30 删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.response import respond
from novel_system.services.style_fidelity_view import (
    project_style_fidelity,
    scene_style_fidelity,
)
from novel_system.services.scene_lookup import get_scene_or_404, require_project

router = APIRouter(tags=["style_fidelity"])


@router.get("/api/v1/scenes/{scene_id}/style-fidelity")
def get_scene_style_fidelity(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    scene = get_scene_or_404(session, scene_id)
    return respond(request, scene_style_fidelity(session, scene))


@router.get("/api/v1/projects/{project_id}/style-fidelity")
def get_project_style_fidelity(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    require_project(session, project_id)
    return respond(request, project_style_fidelity(session, project_id))

"""R9（2026-09-30 作者批准 #16a）：v1 雪花规划器与 /api/v1/projects/{id}/snowflake* 路由已退役，不许回来。

那是第二套完整的步骤生命周期（snowflake_artifacts 表），界面从不调用；它的「生成」不接模型，把大纲原文拼成
一套固定句子当结果，违反「没有模型就不兜底」。构思只有 v2 工作台一套；snowflake_artifacts 表暂留，等批次二
带备份的迁移再删。
"""

from __future__ import annotations

import importlib

import pytest

V1_PATHS = [
    ("get", "/api/v1/projects/{project_id}/snowflake"),
    ("post", "/api/v1/projects/{project_id}/snowflake/steps/book_brief/generate"),
    ("patch", "/api/v1/projects/{project_id}/snowflake/artifacts/any"),
    ("post", "/api/v1/projects/{project_id}/snowflake/artifacts/any/approve"),
    ("post", "/api/v1/projects/{project_id}/snowflake/materialize-outline-plan"),
]


def test_v1_snowflake_routes_are_not_mounted(client) -> None:
    created = client.post(
        "/api/v1/projects",
        json={"title": "雨城旧信", "outline_text": "旧信把她带回雨城。", "planning_mode": "snowflake"},
        headers={"X-Idempotency-Key": "v1-retired-create"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["data"]["project"]["project_id"]
    for method, template in V1_PATHS:
        path = template.format(project_id=project_id)
        response = getattr(client, method)(path, **({} if method == "get" else {"json": {}}))
        assert response.status_code in (404, 405), (method, path, response.status_code)
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/v1/projects/{project_id}/snowflake")]


def test_v1_planner_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("novel_system.services.snowflake_planner")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("novel_system.api.routes.snowflake")


def test_v1_projects_without_a_planning_mode_default_to_outline_driven(client) -> None:
    """R9 移植（3c）：v1 建作品不带 planning_mode 仍是大纲驱动——v1 规划器的测试里唯一与构思无关的一条。"""
    response = client.post(
        "/api/v1/projects",
        json={"title": "大纲作品", "outline_text": "第一行。\n第二行。"},
        headers={"X-Idempotency-Key": "v1-retired-outline-driven"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["project"]["planning_mode"] == "outline_driven"

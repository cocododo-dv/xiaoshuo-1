"""Style Reference 路由包的共用部分：路径前缀、请求辅助、运行时模型客户端与作业派发。

这里不做序列化：书库 / 画像 / 绑定的载荷由服务层的读模型给出（``services/style_reference/summaries.py``、
``binding_apply.binding_payload``），禁用词行的形状在 ``services/style_reference/banned_terms.py``
（``serialize_banned_term``）。

测试在**包**上打桩运行时模型客户端（``novel_system.api.routes.style_reference._get_llm_client_and_enabled``）与
作业派发（``novel_system.api.routes.style_reference.dispatch_job``），所以各子模块一律经
:func:`llm_client_and_enabled` / :func:`dispatch_response_job` 取——它们在调用时回到包上找这两个名字。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request

PATH_PREFIX = "/api/v2/style-reference"
ROUTE_TAGS = ["style_reference"]


def client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _get_llm_client_and_enabled():
    """委托统一工厂 runtime_llm_client_and_enabled;包上保留同名属性供路由测试打桩。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


def llm_client_and_enabled():
    """运行时模型客户端:回到包上取 ``_get_llm_client_and_enabled``(测试在包上打桩)。"""
    from novel_system.api.routes import style_reference as package

    return package._get_llm_client_and_enabled()


def dispatch(job_id: str) -> None:
    """事务提交后把作业投给工人:回到包上取 ``dispatch_job``(测试在包上打桩,记下派发而不真跑)。"""
    from novel_system.api.routes import style_reference as package

    package.dispatch_job(job_id)


def dispatch_response_job(result: Mapping[str, Any] | None) -> None:
    """建作业的写接口的 ``after_commit``:事务提交后把响应里的作业投给工人(认领是条件写,重复投递无害;漏投的由
    清扫线程补派)。作业 id 取 ``job_id``,没有时取 ``classification.job_id``(导入 / 重新分类的响应)。"""
    payload = result or {}
    job_id = str(payload.get("job_id") or (payload.get("classification") or {}).get("job_id") or "")
    if job_id:
        dispatch(job_id)


__all__ = [
    "PATH_PREFIX",
    "ROUTE_TAGS",
    "client_host",
    "dispatch",
    "dispatch_response_job",
    "llm_client_and_enabled",
]

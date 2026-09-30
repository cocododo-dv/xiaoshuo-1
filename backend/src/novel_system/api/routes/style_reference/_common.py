"""Style Reference 路由包的共用部分：路径前缀、序列化、请求辅助、运行时模型客户端与作业派发。

书库 / 画像 / 绑定的载荷由服务层的读模型给出（``services/style_reference/summaries.py``、
``binding_apply.binding_payload``），这里只剩几个行级序列化。

测试在**包**上打桩运行时模型客户端（``novel_system.api.routes.style_reference._get_llm_client_and_enabled``），
所以各子模块一律经 :func:`llm_client_and_enabled` 取——它在调用时回到包上找这个名字。
"""

from __future__ import annotations


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


__all__ = [
    "PATH_PREFIX",
    "ROUTE_TAGS",
    "client_host",
    "dispatch",
    "llm_client_and_enabled",
]

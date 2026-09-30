"""HTTP 写接口的唯一边界：``mutate()``。

每个写接口都经它提交：``X-Idempotency-Key`` 必须带（缺了 400 ``IDEMPOTENCY_KEY_REQUIRED``），同键同载荷重放
缓存的响应（``X-Idempotency-Status: replayed``），同键不同载荷 409；一个请求一个事务所有者（执行器提交 / 回滚，
路由与服务都不自己提交）。以前还有一个「可选键」的旧壳——没带键就直接执行一次、不留幂等记录；React 客户端与
全部冒烟脚本每个写请求都带键，那个壳只剩测试在用，已删（B12-05 / B09-26）。

请求哈希里的方法与路径模板取自这次请求匹配到的路由（``request.scope["route"]``），不再在每个路由里手抄一遍
（以前 112 处手抄，逐一核对过都与装饰器一致，所以既有幂等记录的哈希不变）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of
from novel_system.api.response import respond
from novel_system.services.errors import DomainError
from novel_system.services.idempotency import execute_with_idempotency


def route_template(request: Request) -> str:
    """这次请求匹配到的路由的路径模板（如 ``/api/v1/scenes/{scene_id}/run/jobs``）。"""

    template = getattr(request.scope.get("route"), "path", None)
    if not isinstance(template, str) or not template:
        raise RuntimeError("mutate() must be called from a routed request handler")
    return template


def mutate(
    request: Request,
    session: Session,
    *,
    payload: Any,
    action: Callable[..., dict],
    after_commit: Callable[[dict], None] | None = None,
    owned_failure_callback: Callable[[DomainError], None] | None = None,
) -> JSONResponse:
    """执行并提交一次写操作，回标准信封。

    ``payload`` 是这次写入的规范载荷（进请求哈希，决定「同一个请求」）；``action`` 不自己提交，要租约的写成
    ``lambda lease: …``（长任务续租、执行 id）。``after_commit`` 在业务与幂等记录都提交之后运行，重放同一个键时
    也再运行一次（派发后台作业用：认领是条件写，重复派发无害，提交与派发之间进程退出留下的排队作业由重试接上）。
    """

    result, status = execute_with_idempotency(
        session,
        idempotency_key=request.headers.get("X-Idempotency-Key"),
        method=request.method,
        path_template=route_template(request),
        payload=payload,
        action=action,
        after_commit=after_commit,
        owned_failure_callback=owned_failure_callback,
        actor_ref=actor_ref_of(request),
    )
    return respond(request, result, headers={"X-Idempotency-Status": status} if status else None)


__all__ = ["mutate", "route_template"]

"""对照检查（风格参考 v3 P5b）：一段文字（或一场的当前正文）像不像参考作者——作业表上的 ``check`` 作业。

- ``POST /checks``：建一个对照检查作业（读数 + 参考评审 + 抄袭门），事务提交后派发；
- ``GET /checks/{job_id}``：作业的进度与结果读数；
- ``POST /checks/{job_id}/cancel``：取消（排队中 / 心跳过期的当场收尾，运行中的在下一个检查点收尾；已结束 409
  ``STYLE_REFERENCE_CHECK_NOT_ACTIVE``），响应与 ``GET`` 同形。

作业派发与路由包的其它作业同一个接缝（``_common.dispatch`` → 包上的 ``dispatch_job``，测试在包上打桩）；建作业前
「有没有模型」按 ``check_job.resolve_check_client`` 判——作业开工时再取一次客户端，也是它（测试在那里打桩）。
读数的两个读接口（``/api/v1/{scenes|projects}/{id}/style-fidelity``）在 ``api/routes/style_fidelity.py``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import CheckRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS, dispatch_response_job
from novel_system.services.style_reference import check_job as check_job_service
from novel_system.services.style_reference.check_job import (
    cancel_check_job,
    check_job_or_404,
    check_job_payload,
    start_check_job,
)
from novel_system.services.style_reference.errors import LLMRequiredError

router = APIRouter(tags=ROUTE_TAGS)


@router.post(f"{PATH_PREFIX}/checks")
def create_style_check(
    request: Request,
    payload: CheckRequest,
    session: Session = Depends(get_session),
):
    """建一个对照检查作业（事务提交后派发）。没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``；没有可对照的参考
    409 ``STYLE_REFERENCE_CHECK_NOT_BOUND``；参数不对 400 ``STYLE_REFERENCE_CHECK_TARGET_INVALID``。"""
    body = payload.model_dump(mode="json")
    # 按模块取（运行时与测试都只在 check_job 一处换客户端工厂）
    client, enabled = check_job_service.resolve_check_client()
    if not enabled or client is None:
        raise LLMRequiredError(operation="style_check")
    op_key = request.headers.get("X-Idempotency-Key")

    def _do() -> dict[str, Any]:
        job = start_check_job(
            session,
            text=body.get("text"),
            scene_id=body.get("scene_id"),
            profile_id=body.get("profile_id"),
            project_id=body.get("project_id"),
            op_key=op_key,
            llm_client=client,
            llm_enabled=enabled,
        )
        return {"job_id": job.job_id, "state": job.state, **check_job_payload(session, job)}

    return mutate(
        request,
        session,
        payload=body,
        action=_do,
        after_commit=dispatch_response_job,
    )


@router.get(f"{PATH_PREFIX}/checks/{{job_id}}")
def get_style_check(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    job = check_job_or_404(session, job_id)
    return respond(request, check_job_payload(session, job))


@router.post(f"{PATH_PREFIX}/checks/{{job_id}}/cancel")
def cancel_style_check(
    job_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """取消一次对照检查（与分类 / 学习的取消同形）：排队中 / 心跳过期的作业在这个请求里收尾为 cancelled，运行中的置
    取消标记、工人在下一个检查点收尾；已结束 409 ``STYLE_REFERENCE_CHECK_NOT_ACTIVE``，没有 404。响应 = ``GET`` 的
    作业载荷（``job`` / ``reading``）加 ``job_id`` / ``state`` / ``cancel_requested`` / ``finished``。"""

    def _do() -> dict[str, Any]:
        job = cancel_check_job(session, job_id)
        return {
            "job_id": job.job_id,
            "state": job.state,
            "cancel_requested": True,
            "finished": job.state == "cancelled",
            **check_job_payload(session, job),
        }

    return mutate(
        request,
        session,
        payload={"job_id": job_id},
        action=_do,
    )

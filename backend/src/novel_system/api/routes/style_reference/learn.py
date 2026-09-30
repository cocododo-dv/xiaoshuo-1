"""学习文风:建 / 续 / 取消学习作业(作业表 kind=learn)、作业摘要与估算。

抽取 run 与发现只是文风卡行的血缘,不单独给接口:文风画像页的依据(发现 → 证据 → 引文)由 ``GET /profiles/{id}``
给出(``GET /books/{id}/runs``、``GET /runs/{id}/findings`` 没有界面调用,2026-09-30 删除)。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import LearnRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    dispatch_response_job,
    llm_client_and_enabled,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import LLMRequiredError, book_not_found
from novel_system.services.style_reference.learn_job import (
    LEARN_NOT_ACTIVE_CODE,
    cancel_learn,
    estimate_learning,
    latest_learn_job,
    learn_payload,
    start_learn_job,
)
from novel_system.services.style_reference.learn_llm import LEARN_NODE_IDS
from novel_system.services.style_reference.policy import resolve_node_endpoint
from novel_system.services.style_reference.repository import StyleReferenceRepository

router = APIRouter(tags=ROUTE_TAGS)


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/learn")
def learn_book_style(
    book_id: str,
    request: Request,
    payload: LearnRequest | None = None,
    session: Session = Depends(get_session),
):
    """「学习文风」:建一个学习作业(作业表 kind=learn;事务提交后派发)。

    七步:整理窗口 → 挑学习样本 → 四层抽取 → 写文风卡 → 识别本书专名 → 给全书片段打标签 → 写入画像;每步之后
    记游标,重启 / 取消后可续。已有画像就地更新(同一个 profile_id,绑定照常生效)。
    书没分类完 409 ``STYLE_REFERENCE_BOOK_NOT_READY``;正在重标段落类型 409 ``STYLE_REFERENCE_BOOK_CLASSIFYING``;
    已在学习 409 ``STYLE_REFERENCE_LEARN_ALREADY_ACTIVE``;没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``;
    书的云策略不允许学习节点的实际路由 409 ``STYLE_REFERENCE_CLOUD_POLICY_*``。
    """
    body = payload.model_dump(mode="json") if payload is not None else LearnRequest().model_dump(mode="json")
    op_key = request.headers.get("X-Idempotency-Key")
    client, enabled = llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation="learn_style")

    def _do() -> dict[str, Any]:
        job = start_learn_job(
            session,
            book_id,
            profile_id=body.get("profile_id"),
            force=bool(body.get("force")),
            resume=bool(body.get("resume")),
            retag=bool(body.get("retag")),
            op_key=op_key,
            llm_client=client,
        )
        return {"book_id": book_id, "job_id": job.job_id, "state": job.state, "learn": learn_payload(job)}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/learn",
        payload={"book_id": book_id, **body},
        action=_do,
        after_commit=dispatch_response_job,
    )


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/learn")
def get_book_learning(
    book_id: str,
    request: Request,
    retag: bool = False,
    session: Session = Depends(get_session),
):
    """只读:这本书最近一次学习文风作业的摘要 + 学一次的调用数估计(学习节点的路由摘要)。
    估算里的标签批数只算还要打的窗口(``windows_to_tag``);``?retag=true`` 按全书重打估。"""
    book = StyleReferenceRepository(session).get_book(book_id)
    if book is None:
        raise book_not_found(book_id)
    client, _enabled = llm_client_and_enabled()
    return respond(
        request,
        {
            "learn": learn_payload(latest_learn_job(session, book_id)),
            "estimate": estimate_learning(session, book, retag=bool(retag)),
            "routes": [resolve_node_endpoint(node_id, llm_client=client).as_dict() for node_id in LEARN_NODE_IDS],
        },
    )


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/learn/cancel")
def cancel_book_learning(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """取消这本书排队 / 运行中的学习作业(排队中或工人已死的在这个请求里收尾;运行中的在下一个检查点收尾)。
    之后可以「继续学习」(``POST …/learn {"resume": true}``)从游标续上。"""

    def _do() -> dict[str, Any]:
        if StyleReferenceRepository(session).get_book(book_id) is None:
            raise book_not_found(book_id)
        job = cancel_learn(session, book_id)
        if job is None:
            raise DomainError(
                LEARN_NOT_ACTIVE_CODE,
                "这本书没有正在排队或运行的学习。",
                status_code=409,
                details={"book_id": book_id},
            )
        return {
            "book_id": book_id,
            "job_id": job.job_id,
            "state": job.state,
            "cancel_requested": True,
            "finished": job.state == "cancelled",
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/learn/cancel",
        payload={"book_id": book_id},
        action=_do,
    )

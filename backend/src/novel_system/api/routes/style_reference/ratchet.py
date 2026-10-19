"""配置对比（风格棘轮，``services/style_reference/ratchet_job.py``）：总览、预估、开跑 / 取消、一次比较的详情与盲读选择。

- ``GET /profiles/{id}/ratchet``：能不能比、现在的配置、可换的模型、噪声记录、在跑的与历史；
- ``POST /profiles/{id}/ratchet/preview``：只读预估（留出段、缓存、要调几次模型、多少字输入），不建作业；
- ``POST /profiles/{id}/ratchet`` / ``…/ratchet/cancel``：建 / 取消配置对比作业（作业表 kind=ratchet，提交后派发）；
- ``GET /ratchet-runs/{job_id}``：结论与逐段数字、盲读三份文字与答案、作者的选择；
- ``PUT /ratchet-runs/{job_id}/votes``：存盲读选择（整份替换），``reveal`` 同时揭晓答案与结论。

揭晓之前不给答案与结论（逐段字数就能对出哪份是哪份），见 ``ratchet_job`` 的「先读后看」。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import RatchetStartRequest, RatchetVotesRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    dispatch_response_job,
    llm_client_and_enabled,
)
from novel_system.services.style_reference import ratchet_job
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.jobs import job_activity_entry

router = APIRouter(tags=ROUTE_TAGS)


def _arms(payload: RatchetStartRequest | None) -> dict[str, Any]:
    body = payload.model_dump(mode="json", exclude_none=True) if payload else {}
    return {"a": body.get("a") or {}, "b": body.get("b") or {}, "passages": body.get("passages")}


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/ratchet")
def get_ratchet(profile_id: str, request: Request, session: Session = Depends(get_session)):
    """「配置对比」页：能不能比（不能就给原因）、现在的配置、可换的模型、噪声记录、在跑的作业、历史比较。"""
    return respond(request, ratchet_job.ratchet_overview(session, profile_id))


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/ratchet/preview")
def preview_ratchet(
    profile_id: str,
    request: Request,
    payload: RatchetStartRequest | None = None,
    session: Session = Depends(get_session),
):
    """只读预估：不建作业、不调用模型、不写库。配置写法不对 / 画像用不了 409 ``STYLE_RATCHET_REFUSED``。"""
    # idempotency-exempt: deterministic read-only preview; no job, no model call, the session is rolled back
    # (the book's window index may be built once as a cache).
    return respond(request, ratchet_job.ratchet_preview(session, profile_id, **_arms(payload)))


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/ratchet")
def start_ratchet(
    profile_id: str,
    request: Request,
    payload: RatchetStartRequest | None = None,
    session: Session = Depends(get_session),
):
    """开一次配置对比（作业表 kind=ratchet；事务提交后派发）。没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``；
    已有对比在跑 409 ``STYLE_RATCHET_ALREADY_ACTIVE``；书的云策略不许 409 ``STYLE_REFERENCE_CLOUD_POLICY_*``。"""
    op_key = request.headers.get("X-Idempotency-Key")
    client, enabled = llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation=ratchet_job.OPERATION)
    arms = _arms(payload)

    def _do() -> dict[str, Any]:
        job = ratchet_job.start_ratchet_job(session, profile_id, op_key=op_key, llm_client=client, **arms)
        return {"profile_id": profile_id, "job_id": job.job_id, "state": job.state, "job": job_activity_entry(job)}

    return mutate(
        request,
        session,
        payload={"profile_id": profile_id, **arms},
        action=_do,
        after_commit=dispatch_response_job,
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/ratchet/cancel")
def cancel_ratchet(
    profile_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """取消这份画像正在排队 / 运行的配置对比；没有 409 ``STYLE_RATCHET_NOT_ACTIVE``。"""

    def _do() -> dict[str, Any]:
        job = ratchet_job.cancel_ratchet(session, profile_id)
        return {"profile_id": profile_id, "job_id": job.job_id, "state": job.state, "finished": job.state == "cancelled"}

    return mutate(request, session, payload={"profile_id": profile_id}, action=_do)


@router.get(f"{PATH_PREFIX}/ratchet-runs/{{job_id}}")
def get_ratchet_run(job_id: str, request: Request, session: Session = Depends(get_session)):
    """一次比较：作业状态、结论与逐段数字、盲读三份文字与答案、作者的选择。没有 404 ``STYLE_RATCHET_RUN_NOT_FOUND``。"""
    return respond(request, ratchet_job.ratchet_run_detail(session, job_id))


@router.put(f"{PATH_PREFIX}/ratchet-runs/{{job_id}}/votes")
def save_ratchet_votes(
    job_id: str,
    request: Request,
    payload: RatchetVotesRequest,
    session: Session = Depends(get_session),
):
    """存盲读选择（整份替换）：每段 ``{best, worst}``（甲 / 乙 / 丙）；``reveal`` 同时揭晓。揭晓过再改选择 409。"""
    votes = {index: vote.model_dump(mode="json") for index, vote in payload.votes.items()}
    return mutate(
        request,
        session,
        payload={"job_id": job_id, "votes": votes, "reveal": payload.reveal},
        action=lambda: ratchet_job.save_reading_votes(session, job_id, votes, reveal=payload.reveal),
    )

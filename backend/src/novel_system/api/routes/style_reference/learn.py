"""学习文风:建 / 续 / 取消学习作业(作业表 kind=learn)、作业摘要与估算;抽取 run 与发现(文风卡行的血缘,
只读——文风画像页的依据直接由 ``GET /profiles/{id}`` 给出)。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    dispatch,
    llm_client_and_enabled,
    req_id,
    serialize_finding,
    serialize_run,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import LLMRequiredError
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


class LearnRequest(BaseModel):
    """「学习文风」:建一个学习作业(或 ``resume`` 续上最近一次失败 / 取消 / 中断的)。

    ``profile_id``:要就地更新的画像(缺省:这本书有绑定的 / active 的 / 最近更新的那份;没有画像就新建);
    ``force``:正文少到四层都被评估为 skip 时仍要学。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str | None = Field(default=None, max_length=128)
    resume: bool = False
    force: bool = False


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
        after_commit=_dispatch_learn,
    )


def _dispatch_learn(result: dict[str, Any]) -> None:
    """事务提交后把学习作业投给工人(认领是条件写,重复投递无害;漏投的由清扫线程补派)。"""
    job_id = str((result or {}).get("job_id") or "")
    if job_id:
        dispatch(job_id)


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/learn")
def get_book_learning(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """只读:这本书最近一次学习文风作业的摘要 + 学一次的调用数估计(学习节点的路由摘要)。"""
    book = StyleReferenceRepository(session).get_book(book_id)
    if book is None:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    client, _enabled = llm_client_and_enabled()
    return ok(
        {
            "learn": learn_payload(latest_learn_job(session, book_id)),
            "estimate": estimate_learning(session, book),
            "routes": [resolve_node_endpoint(node_id, llm_client=client).as_dict() for node_id in LEARN_NODE_IDS],
        },
        req_id=req_id(request),
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
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
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


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/runs")
def list_book_runs(
    book_id: str,
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    """列出某书的抽取 run(最新在前)。v3 起 run 行只作学习作业的血缘(``dispatch_state="learn_job"``),
    矩阵据此找到文风卡行依据的发现与证据;进度在作业行上(``GET …/learn`` / 活动清单)。"""
    repo = StyleReferenceRepository(session)
    runs = repo.list_runs(book_id=book_id, status=status)
    runs = sorted(runs, key=lambda r: (r.created_at or "", r.run_id), reverse=True)
    return ok(
        {"runs": [serialize_run(r) for r in runs]},
        req_id=req_id(request),
    )


@router.get(f"{PATH_PREFIX}/runs/{{run_id}}/findings")
def list_run_findings(
    run_id: str,
    request: Request,
    sub_dimension: str | None = None,
    finding_kind: str | None = None,
    status: str | None = None,
    include: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    findings = repo.list_findings(
        run_id=run_id,
        sub_dimension=sub_dimension,
        finding_kind=finding_kind,
        status=status,
    )
    # PR-23 — ?include=evidence:总查询数固定 3 条(findings + evidences + quotes)
    evidence_map: dict[str, list[dict[str, Any]]] | None = None
    if include == "evidence":
        evidences = repo.list_evidences_for_findings([f.finding_id for f in findings])
        quotes = {
            q.quote_id: q
            for q in repo.list_quotes_by_ids([e.quote_id for e in evidences])
        }
        evidence_map = {}
        for e in evidences:
            quote = quotes.get(e.quote_id)
            evidence_map.setdefault(e.finding_id, []).append(
                {
                    "evidence_id": e.evidence_id,
                    "quote_id": e.quote_id,
                    "anchor_kind": e.anchor_kind,
                    "quote_text": quote.quote_text if quote else "",
                    "paragraph_id": quote.paragraph_id if quote else None,
                    "span": [quote.span_start, quote.span_end] if quote else None,
                }
            )
    return ok(
        {
            "findings": [
                serialize_finding(
                    f,
                    evidence=(evidence_map.get(f.finding_id, []) if evidence_map is not None else None),
                )
                for f in findings
            ]
        },
        req_id=req_id(request),
    )

"""参考书:导入(上传 / 服务器路径)、书库列表与详情、段落原文、删除(单本 / 批量)、(重新)分类与取消、运行时默认值。

书的载荷都来自 ``summaries.book_summaries``:列表不带 ``stats_json``(详情才带),每本书带段落类型的来源、最近的
分类 / 学习作业、这本书的画像摘要(要不要重新学)与用在了哪些作品上。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import BulkDeleteRequest, ImportPathRequest, ReclassifyRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    client_host,
    dispatch_response_job,
    llm_client_and_enabled,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.cleanup import delete_reference_book, delete_reference_books
from novel_system.services.style_reference.errors import LLMRequiredError, book_not_found
from novel_system.services.style_reference.import_job import (
    cancel_classification,
    classification_payload,
    count_paragraphs,
    estimate_classification,
)
from novel_system.services.style_reference.ingest import (
    MAX_REFERENCE_BOOK_BYTES,
    IngestService,
)
from novel_system.services.style_reference.policy import (
    default_cloud_policy,
    ensure_local_only_llm,
    resolve_node_endpoint,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.segmentation.llm import CLASSIFY_NODE_IDS
from novel_system.services.style_reference.summaries import book_summaries
from novel_system.services.system_config import require_admin_token

router = APIRouter(tags=ROUTE_TAGS)


@router.post(f"{PATH_PREFIX}/books/import-path")
def import_book_path(
    payload: ImportPathRequest,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    session: Session = Depends(get_session),
):
    # A path is interpreted by the server process, not the browser. Keep this
    # capability behind the existing local/admin boundary in addition to the
    # configured-root check in IngestService.
    require_admin_token(x_admin_token, client_host=client_host(request))
    body = payload.model_dump(mode="json")

    op_key = request.headers.get("X-Idempotency-Key")
    client = _require_import_llm(body["cloud_policy"])

    def _do() -> dict[str, Any]:
        # 严格 LLM(2026-09-15):导入只做准备工作,整本 LLM 分类是提交后派发的分类作业
        # (作业表 kind=classify,见 import_job),没有启发式兜底。
        service = IngestService(session, llm_enabled=True, op_key=op_key, llm_client=client)
        result = service.ingest_path(
            file_path=body["file_path"],
            title=body["title"],
            author_label=body.get("author_label"),
            cloud_policy=body["cloud_policy"],
            rights_declaration=body.get("rights_declaration"),
        )
        return _import_response(session, result)

    return mutate(
        request,
        session,
        payload=body,
        action=_do,
        after_commit=dispatch_response_job,
    )


def _require_import_llm(cloud_policy: str):
    """2026-09-15 严格 LLM:导入 / 重新分类没有启发式兜底——LLM 未启用 409
    ``STYLE_REFERENCE_LLM_REQUIRED``;「仅本机」策略要求**分类节点的实际路由**是本机模型(v3 I7)。"""
    client, enabled = llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation="import_book")
    if cloud_policy == "local_only":
        ensure_local_only_llm(operation="import_book", node_ids=CLASSIFY_NODE_IDS, llm_client=client)
    return client


def _book_payload(session: Session, book) -> dict[str, Any]:
    """一本书的完整载荷(带 ``stats_json``);书不存在时 None。"""
    rows = book_summaries(session, [book], include_stats=True) if book is not None else []
    return rows[0] if rows else None


def _import_response(session: Session, result) -> dict[str, Any]:
    job = result.job
    return {
        "book": _book_payload(session, result.book),
        "paragraphs_count": result.paragraphs_count,
        "safety": result.safety_payload,
        "classification": classification_payload(job),
        "job_id": job.job_id if job is not None else None,
    }


"""上传体积上限:参考书是纯文本,30 万字 UTF-8 约 1MB;10MB 已极宽裕,
超限直接 413,避免 `file.read()` 把任意大文件整块载入内存。"""
MAX_UPLOAD_BYTES = MAX_REFERENCE_BOOK_BYTES


@router.post(f"{PATH_PREFIX}/books/import-upload")
async def import_book_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=512),
    author_label: str | None = Form(default=None, max_length=255),
    cloud_policy: str = Form(..., min_length=1, max_length=64),
    rights_declaration: str | None = Form(default=None, max_length=20_000),
    session: Session = Depends(get_session),
):
    raw_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise DomainError(
            "STYLE_REFERENCE_UPLOAD_TOO_LARGE",
            f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
            status_code=413,
        )
    # Wave 7 §5.9 — 权属声明以 JSON 串走 multipart form；解析失败当未声明
    rights_obj: dict[str, Any] | None = None
    if rights_declaration:
        try:
            parsed = json.loads(rights_declaration)
        except (ValueError, TypeError) as exc:
            raise DomainError(
                "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID",
                "rights_declaration must be a JSON object",
                status_code=400,
            ) from exc
        if not isinstance(parsed, dict):
            raise DomainError(
                "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID",
                "rights_declaration must be a JSON object",
                status_code=400,
            )
        rights_obj = parsed
    payload: dict[str, Any] = {
        "file_name": file.filename,
        "title": title,
        "author_label": author_label,
        "cloud_policy": cloud_policy,
        "rights_declaration": rights_obj,
    }

    # 分类作业的 op_key = 客户端幂等键;界面按响应里的 job_id 在活动清单(作业表)里跟进度。
    op_key = request.headers.get("X-Idempotency-Key")
    client = _require_import_llm(cloud_policy)

    def _do() -> dict[str, Any]:
        service = IngestService(session, llm_enabled=True, op_key=op_key, llm_client=client)
        result = service.ingest_upload(
            raw_bytes=raw_bytes,
            file_name=payload["file_name"],
            title=payload["title"],
            author_label=payload.get("author_label"),
            cloud_policy=payload["cloud_policy"],
            rights_declaration=payload.get("rights_declaration"),
        )
        return _import_response(session, result)

    # 处理器因为要 await 读上传体而是 async def,准备工作(安全扫描、切段、批量落段落行)是同步的:
    # 放到线程池里执行,不堵事件循环;请求级 Session 只在这一个线程里用。
    def _run_import() -> Any:
        # 幂等边界在这里显式调用(tests/test_route_mutation_policy 按 AST 找调用点),
        # 整段在线程池里执行;分类作业在事务提交后派发。
        return mutate(
            request,
            session,
            payload=payload,
            action=_do,
            after_commit=dispatch_response_job,
        )

    return await run_in_threadpool(_run_import)


@router.get(f"{PATH_PREFIX}/runtime")
def get_style_reference_runtime(request: Request):
    """只读:导入对话框的默认值。``llm_enabled``(有没有可用模型)、``llm_is_local``(两个分类节点的
    实际路由是否都是本机模型)、``default_cloud_policy``(分类走本机模型时 ``local_only``,否则
    ``allow_full_cloud``——云端模型下默认「仅本机」必然导入失败)与分类节点的路由摘要。"""
    client, enabled = llm_client_and_enabled()
    routes = [resolve_node_endpoint(node_id, llm_client=client).as_dict() for node_id in CLASSIFY_NODE_IDS]
    llm_is_local = bool(enabled) and all(route["local"] for route in routes)
    return respond(
        request,
        {
            "llm_enabled": bool(enabled and client is not None),
            "llm_is_local": llm_is_local,
            "default_cloud_policy": (
                default_cloud_policy(CLASSIFY_NODE_IDS, llm_client=client) if enabled else "local_only"
            ),
            "classify_routes": routes,
        },
    )


@router.get(f"{PATH_PREFIX}/books")
def list_books(
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    """书库列表(按导入时间):每本书的状态、段落类型的来源与一致率、最近的分类 / 学习作业、画像摘要
    (``needs_relearn`` / ``relearn_reason``)与 ``applied_projects``;不带 ``stats_json``。"""
    books = StyleReferenceRepository(session).list_books(status=status)
    return respond(request, {"books": book_summaries(session, books)})


@router.get(f"{PATH_PREFIX}/books/{{book_id}}")
def get_book(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """一本书的完整载荷:列表的全部字段 + ``stats_json``(段落类型分布、语料评估、校准信息……)。"""
    book = StyleReferenceRepository(session).get_book(book_id)
    if book is None:
        raise book_not_found(book_id)
    return respond(request, {"book": _book_payload(session, book)})


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/classification/estimate")
def estimate_book_classification(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """只读:整本(重新)分类的批数 / 调用 / 输入输出 token / 分钟(作者就地重标类型前看费用)。

    与分类作业同一套计划(锚定集、按字数分批、同模型时不做快模型对照);均值优先取本地账本,
    取不到用默认值(``basis`` 里如实标出来源)。
    """
    book = StyleReferenceRepository(session).get_book(book_id)
    if book is None:
        raise book_not_found(book_id)
    return respond(request, {"estimate": estimate_classification(session, book)})


# 2026-09-14 风格保真修补(WP4.2):本场参考窗口「展开原文」——按段落序号闭区间读参考书原文。
# 只服务本机作者读自己导入的书,不做云策略门(原文不出本机);每次最多 PARAGRAPH_RANGE_MAX 段,
# 超出的按 start 截到上限并以 capped=true 告知,调用方按返回的 end 续读。
PARAGRAPH_RANGE_MAX = 80


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/paragraphs")
def get_book_paragraph_range(
    book_id: str,
    request: Request,
    start: int,
    end: int,
    session: Session = Depends(get_session),
):
    """只读:``[start, end]``(闭区间,段落序号从 0 起)内的段落原文,按序号升序。"""
    repo = StyleReferenceRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        raise book_not_found(book_id)
    if start < 0 or end < start:
        raise DomainError(
            "STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID",
            "paragraph range must satisfy 0 <= start <= end",
            status_code=400,
            details={"start": start, "end": end},
        )
    effective_end = min(end, start + PARAGRAPH_RANGE_MAX - 1)
    rows = repo.paragraph_range(book_id, start, effective_end)
    return respond(
        request,
        {
            "book_id": book_id,
            "start": start,
            "end": effective_end,
            "capped": effective_end < end,
            "paragraphs": [
                {
                    "paragraph_index": row.paragraph_index,
                    "paragraph_type": row.paragraph_type,
                    "text": row.text,
                }
                for row in rows
            ],
        },
    )


@router.delete(f"{PATH_PREFIX}/books/{{book_id}}")
def delete_book(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """删一本参考书:活动作业收尾、它的绑定所在范围的规划产物作废、派生数据 / 段落 / 书一并删除(``cleanup``)。"""

    def _do() -> dict[str, Any]:
        result = delete_reference_book(session, book_id)
        return {"book_id": book_id, "deleted": True, "unbound": result["unbound"]}

    return mutate(
        request,
        session,
        payload={"book_id": book_id},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/books/bulk-delete")
def bulk_delete_books(
    payload: BulkDeleteRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """书库多选删除(台账 U4 / L6,``cleanup.delete_reference_books``):每本书与单本删除走同一个函数,各在一个保存点
    里——一本失败(不存在)不影响其余的;结果逐本给出 ``deleted`` / ``error``;整批与幂等记录一起提交或回滚。"""
    book_ids = list(dict.fromkeys(str(book_id) for book_id in payload.book_ids))

    def _do() -> dict[str, Any]:
        return delete_reference_books(session, book_ids)

    return mutate(
        request,
        session,
        payload={"book_ids": book_ids},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/reclassify")
def reclassify_book(
    book_id: str,
    request: Request,
    payload: ReclassifyRequest | None = None,
    session: Session = Depends(get_session),
):
    """重新分类(后台分类作业,整本都由 LLM 分类;事务提交后派发)。

    - 缺省:**破坏式**——先清掉这本书的全部派生数据(抽取 run / 发现 / 引文 / 画像 / 绑定 / 禁用词 /
      作业 / 窗口索引;段落与书保留),书置 ``ingesting``,从头分类;
    - ``{"mode": "retype"}``:**就地重标段落类型**——正文不变,抽取、画像、绑定全部保留,书保持 ``ready``,
      完成后 ``paragraph_types_revision`` +1;
    - ``{"resume": true}``:把最近一次失败 / 取消 / 中断的分类作业放回队列,从游标续跑(进程重启后也行)。

    LLM 未启用 409 ``STYLE_REFERENCE_LLM_REQUIRED``;「仅本机」的书要求分类节点走本机模型;
    已有排队 / 运行中的分类作业 409 ``STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE``。
    """
    resume = bool(payload.resume) if payload is not None else False
    mode = payload.mode if payload is not None else "reclassify"
    op_key = request.headers.get("X-Idempotency-Key")
    client, enabled = llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation="reclassify_book")

    def _do() -> dict[str, Any]:
        service = IngestService(session, llm_enabled=True, op_key=op_key, llm_client=client)
        job = service.resume(book_id) if resume else service.start_reclassify(book_id, mode=mode)
        book = StyleReferenceRepository(session).get_book(book_id)
        return {
            "book": _book_payload(session, book),
            "book_id": book_id,
            "status": "classifying",
            "mode": str((job.params_json or {}).get("mode") or mode),
            "resume": resume,
            "job_id": job.job_id,
            "paragraphs_count": count_paragraphs(session, book_id),
            "classification": classification_payload(job),
        }

    return mutate(
        request,
        session,
        payload={"book_id": book_id, "resume": resume, "mode": mode},
        action=_do,
        after_commit=dispatch_response_job,
    )


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/classification/cancel")
def cancel_book_classification(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """取消这本书正在排队 / 运行的分类作业。排队中或工人已死(心跳过期)的作业在这个请求里直接收尾
    (书标 failed;就地重标类型的书回到 ready);运行中的由工人在下一个检查点收尾。之后可「继续分类」。"""

    def _do() -> dict[str, Any]:
        if StyleReferenceRepository(session).get_book(book_id) is None:
            raise book_not_found(book_id)
        job = cancel_classification(session, book_id)
        if job is None:
            raise DomainError(
                "STYLE_REFERENCE_CLASSIFICATION_NOT_ACTIVE",
                "这本书没有正在排队或运行的分类。",
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

    return mutate(
        request,
        session,
        payload={"book_id": book_id},
        action=_do,
    )

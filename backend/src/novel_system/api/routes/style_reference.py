"""Style Reference 路由(prefix: /api/v2/style-reference)。

导入 / 分类(作业表 kind=classify)、学习文风(作业表 kind=learn:``POST /books/{id}/learn`` 一个按钮一个作业,
取代旧的「抽取 run + 同步合成」)、画像与文风卡行状态、绑定、禁用词、回测与注入预览。不含公开 inject 写接口。
P6 会按领域拆分这个文件。
"""

from __future__ import annotations

# Runtime truth: the public injection contract is `SystemPromptFragments`
# returned by the two `injection-preview` endpoints. There is no public
# `/inject` / `InjectionBundle` HTTP API in the current implementation.

import logging
import re
import uuid
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest
from novel_system.api.response import ok
from novel_system.db.models import StyleReferenceJob, StyleReferenceParagraph, utcnow

logger = logging.getLogger(__name__)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.activity import list_activity
from novel_system.services.style_reference.import_progress import (
    IMPORT_KEY_MAX_LENGTH,
    get_import_progress,
)
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.import_job import (
    cancel_classification,
    classification_payload,
    classification_provenance,
    count_paragraphs,
    estimate_classification,
    find_job_by_op_key,
    latest_classification_job,
    legacy_progress_snapshot,
)
from novel_system.services.style_reference.ingest import (
    MAX_REFERENCE_BOOK_BYTES,
    IngestService,
)
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    StyleJobService,
    dispatch_job,
)
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.learn_job import (
    LEARN_NOT_ACTIVE_CODE,
    cancel_learn,
    estimate_learning,
    latest_learn_job,
    learn_payload,
    start_learn_job,
)
from novel_system.services.style_reference.learn_llm import LEARN_NODE_IDS
from novel_system.services.style_reference.policy import (
    default_cloud_policy,
    ensure_local_only_llm,
    resolve_node_endpoint,
)
from novel_system.services.style_reference.segmentation.llm import CLASSIFY_NODE_IDS
from novel_system.services.style_reference.materialization import MaterializationService
from novel_system.services.style_reference.preview import PreviewService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.inject.bindings import describe_binding_layers
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.injection import injection_task_defaults
from novel_system.services.style_reference.schemas import (
    BindingScope,
    InjectionPreviewRequest,
    InjectionPreviewResponse,
    InjectionPreviewStats,
    InjectionStrategy,
    SystemPromptFragments,
    TaskType,
    ValidateRequest,
    ValidationMode,
    ValidationTargetKind,
)
from novel_system.services.style_reference.validation import (
    ValidationOrchestrator,
    start_style_reference_validation_worker,
)
from novel_system.services.system_config import require_admin_token
from novel_system.services.scene_planning_staleness import supersede_for_binding_scope

router = APIRouter(tags=["style_reference"])

PATH_PREFIX = "/api/v2/style-reference"


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class PreviewRequest(BaseModel):
    """示例预览(2026-09-15):前端按段型逐张请求,进度自然可见;不传 = 三种默认段型一次生成。"""

    model_config = ConfigDict(extra="forbid")
    paragraph_types: (
        list[
            Literal[
                "dialogue",
                "description_env",
                "psychology",
                "narration",
                "action",
                "description_char",
                "transition",
                "flashback",
            ]
        ]
        | None
    ) = Field(default=None, max_length=3)


class ReclassifyRequest(BaseModel):
    """重新分类(后台分类作业)。

    - 缺省(``mode="reclassify"``):**破坏式**——先清掉这本书的全部派生数据(抽取 / 画像 / 绑定 /
      禁用词 / 回测 / 作业 / 窗口索引),再从头分类;
    - ``mode="retype"``:**就地重标段落类型**——正文不变,派生数据与绑定全部保留,书保持可用;
    - ``resume=true``:把最近一次失败 / 取消 / 中断的分类作业从游标续跑(进程重启之后也行)。
    """

    model_config = ConfigDict(extra="forbid")
    resume: bool = False
    mode: Literal["reclassify", "retype"] = "reclassify"


class ImportPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=512)
    author_label: str | None = Field(default=None, max_length=255)
    cloud_policy: Literal["allow_full_cloud", "segments_only", "local_only"]
    # Wave 7 §5.9 — 导入权属声明 {analysis_rights, send_rights, declared_by}
    rights_declaration: BoundedJsonObject | None = None


class LearnRequest(BaseModel):
    """「学习文风」:建一个学习作业(或 ``resume`` 续上最近一次失败 / 取消 / 中断的)。

    ``profile_id``:要就地更新的画像(缺省:这本书有绑定的 / active 的 / 最近更新的那份;没有画像就新建);
    ``force``:正文少到四层都被评估为 skip 时仍要学。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str | None = Field(default=None, max_length=128)
    resume: bool = False
    force: bool = False


class CardLineStateRequest(BaseModel):
    """文风卡一句的状态:``pinned`` 永远带上 / ``excluded`` 不再用 / ``null`` 清掉。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["pinned", "excluded"] | None = None


class ApplyConfigMixin(BaseModel):
    """apply 时落入 binding.config_json 的注入配置(MIXED 策略消费)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    intensity: int | None = Field(default=None, ge=0, le=100)
    sub_dimensions: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = (
        Field(default=None, max_length=128)
    )
    include_positive: bool | None = None
    include_forbidden: bool | None = None
    include_metric: bool | None = None
    # 2026-09-12 风格直起(Step 2):起草方式——style_first(作者手笔直起,缺省)/
    # neutral_first(中性稿再上风格,对照组)。缺省不落库,由 injection_budget.yaml 决定。
    draft_mode: Literal["style_first", "neutral_first"] | None = None


class BannedTermCreateRequest(BaseModel):
    """禁用词登记:generation=生成期红线段填充;extraction=抽取期段落过滤。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=512)
    replacement_hint: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="generation", min_length=1, max_length=64)


class ApplyProfileRequest(ApplyConfigMixin):
    scope: str = Field(min_length=1, max_length=64)
    scope_ref_id: str | None = Field(default=None, max_length=255)
    task_type: str = Field(default="scene_generation", min_length=1, max_length=64)
    strategy: str | None = Field(default=None, min_length=1, max_length=64)

    def injection_config(self) -> dict[str, Any]:
        """非空注入配置 → binding.config_json(端到端打通 intensity 滑块)。"""
        config: dict[str, Any] = {}
        if self.intensity is not None:
            config["intensity"] = max(0, min(100, int(self.intensity)))
        if self.sub_dimensions:
            config["sub_dimensions"] = [str(s) for s in self.sub_dimensions]
        for key in ("include_positive", "include_forbidden", "include_metric"):
            value = getattr(self, key)
            if value is not None:
                config[key] = bool(value)
        if self.draft_mode is not None:
            config["draft_mode"] = str(self.draft_mode)
        return config


class ValidateGeneratedRequest(BaseModel):
    """`POST /profiles/{id}/validate` body(profile_id 在 path,不在 body)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    generated_text: str = Field(min_length=1, max_length=2_000_000)
    target_kind: str = Field(default="manual", min_length=1, max_length=64)
    target_ref_id: str | None = Field(default=None, max_length=255)
    # The route translates invalid values to STYLE_REFERENCE_VALIDATE_PARAM_INVALID.
    mode: str = Field(default="async_full", min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


_NO_JOB = object()


def _serialize_book(book, *, classification_job: Any = _NO_JOB, learn_job: Any = _NO_JOB) -> dict[str, Any]:
    """书的载荷。``classification`` = 最近一个分类作业的摘要;``classification_provenance`` = 段落类型
    的来源(新作业写的,老书按校准信息推出来),带一致率;``learn`` = 最近一个学习文风作业的摘要。
    两个作业由列表端点批量传入。"""
    stats = book.stats_json or {}
    session = Session.object_session(book)
    if classification_job is _NO_JOB:
        classification_job = (
            latest_classification_job(session, book.book_id) if session is not None else None
        )
    if learn_job is _NO_JOB:
        learn_job = latest_learn_job(session, book.book_id) if session is not None else None
    return {
        "book_id": book.book_id,
        "title": book.title,
        "author_label": book.author_label,
        "source_kind": book.source_kind,
        "source_path": book.source_path,
        "cloud_policy": book.cloud_policy,
        "text_checksum": book.text_checksum,
        "total_chars": book.total_chars,
        "status": book.status,
        "stats_json": stats,
        "classification": classification_payload(classification_job),
        "classification_provenance": classification_provenance(stats),
        "paragraph_types_revision": int(stats.get("paragraph_types_revision") or 0),
        "learn": learn_payload(learn_job),
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }


def _serialize_run(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "book_id": run.book_id,
        "status": run.status,
        "phase": run.phase,
        "dispatch_state": run.dispatch_state,
        "requested_layers": list(run.requested_layers_json or []),
        "coverage_json": run.coverage_json or {},
        "heartbeat_at": run.heartbeat_at,
        "error_code": run.error_code,
        "error_text": run.error_text,
        "retryable": bool(run.retryable),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _serialize_finding(finding, *, evidence: list | None = None) -> dict[str, Any]:
    """一条抽取发现(文风卡行的依据;``?include=evidence`` 时带证据引文)。v3 起发现不再单独审核 / 投票:
    作者在文风卡上逐句 ✓ / ✗(``POST /profiles/{id}/card-lines/{line_id}``)。"""
    payload = {
        "finding_id": finding.finding_id,
        "book_id": finding.book_id,
        "run_id": finding.run_id,
        "extraction_id": finding.extraction_id,
        "sub_dimension": finding.sub_dimension,
        "finding_kind": finding.finding_kind,
        "statement": finding.statement,
        "confidence": finding.confidence,
    }
    if evidence is not None:
        payload["evidence"] = evidence
    return payload


def _serialize_profile(profile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "book_id": profile.book_id,
        "run_id": profile.run_id,
        "title": profile.title,
        "status": profile.status,
        "profile_json": profile.profile_json or {},
        "coverage_json": profile.coverage_json or {},
        "version_tag": profile.version_tag,
        "source_finding_ids_json": profile.source_finding_ids_json or [],
    }


def _serialize_binding(binding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "profile_id": binding.profile_id,
        "scope": binding.scope,
        "scope_ref_id": binding.scope_ref_id,
        "task_type": binding.task_type,
        "strategy": binding.strategy,
        "status": binding.status,
        "config_json": binding.config_json or {},
    }


def _req_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _get_llm_client_and_enabled():
    """委托统一工厂 build_runtime_llm_client;保留模块级名字供路由测试打桩。"""
    from novel_system.services.system_config import build_runtime_llm_client
    from novel_system.settings import get_settings

    return build_runtime_llm_client(settings=get_settings())


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


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
    require_admin_token(x_admin_token, client_host=_client_host(request))
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
        return _import_response(result)

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/import-path",
        payload=body,
        action=_do,
        after_commit=_dispatch_classification,
    )


def _require_import_llm(cloud_policy: str):
    """2026-09-15 严格 LLM:导入 / 重新分类没有启发式兜底——LLM 未启用 409
    ``STYLE_REFERENCE_LLM_REQUIRED``;「仅本机」策略要求**分类节点的实际路由**是本机模型(v3 I7)。"""
    client, enabled = _get_llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation="import_book")
    if cloud_policy == "local_only":
        ensure_local_only_llm(operation="import_book", node_ids=CLASSIFY_NODE_IDS, llm_client=client)
    return client


def _import_response(result) -> dict[str, Any]:
    job = result.job
    return {
        "book": _serialize_book(result.book, classification_job=job),
        "paragraphs_count": result.paragraphs_count,
        "safety": result.safety_payload,
        "classification": classification_payload(job),
        "job_id": job.job_id if job is not None else None,
    }


def _dispatch_classification(result: dict[str, Any]) -> None:
    """事务提交后把分类作业投给工人(认领是条件写,重复投递无害;漏投的由清扫线程补派)。"""
    job_id = str(result.get("job_id") or (result.get("classification") or {}).get("job_id") or "")
    if job_id:
        dispatch_job(job_id)


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

    # 分类作业的 op_key = 客户端幂等键:活动清单与 GET …/imports/{key}/progress 按它找到这本书的作业。
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
        return _import_response(result)

    # 处理器因为要 await 读上传体而是 async def,准备工作(安全扫描、切段、批量落段落行)是同步的:
    # 放到线程池里执行,不堵事件循环;请求级 Session 只在这一个线程里用。
    def _run_import() -> Any:
        # 幂等边界在这里显式调用(tests/test_route_mutation_policy 按 AST 找调用点),
        # 整段在线程池里执行;分类作业在事务提交后派发。
        return idempotent_response(
            request,
            session,
            method="POST",
            path_template=f"{PATH_PREFIX}/books/import-upload",
            payload=payload,
            action=_do,
            after_commit=_dispatch_classification,
        )

    return await run_in_threadpool(_run_import)


_IMPORT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % IMPORT_KEY_MAX_LENGTH)


@router.get(f"{PATH_PREFIX}/runtime")
def get_style_reference_runtime(request: Request):
    """只读:导入对话框的默认值。``llm_enabled``(有没有可用模型)、``llm_is_local``(两个分类节点的
    实际路由是否都是本机模型)、``default_cloud_policy``(分类走本机模型时 ``local_only``,否则
    ``allow_full_cloud``——云端模型下默认「仅本机」必然导入失败)与分类节点的路由摘要。"""
    client, enabled = _get_llm_client_and_enabled()
    routes = [resolve_node_endpoint(node_id, llm_client=client).as_dict() for node_id in CLASSIFY_NODE_IDS]
    llm_is_local = bool(enabled) and all(route["local"] for route in routes)
    return ok(
        {
            "llm_enabled": bool(enabled and client is not None),
            "llm_is_local": llm_is_local,
            "default_cloud_policy": (
                default_cloud_policy(CLASSIFY_NODE_IDS, llm_client=client) if enabled else "local_only"
            ),
            "classify_routes": routes,
        },
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/activity")
def get_activity(request: Request, session: Session = Depends(get_session)):
    """参考书活动清单:模块里所有在跑 / 十分钟内结束的耗时操作,统一形状。

    作业表(分类作业;键 ``job:<id>``,另带兼容旧前端的幂等键别名)+ 进程内登记簿(合成画像 /
    应用画像建索引 / 回测阶段)+ durable 行(抽取 run、回测报告)合成一份;见
    ``services/style_reference/activity.py``。
    """
    return ok(
        {"items": list_activity(session), "server_time": utcnow()},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/imports/{{import_key}}/progress")
def get_import_progress_route(import_key: str, request: Request, session: Session = Depends(get_session)):
    """兼容别名(旧前端导入轮询用,P7 删除):按幂等键找分类作业(``op_key``),返回旧的进度快照形状;
    作业表里没有时退回进程内登记簿(合成画像等尚未迁出的操作)。都不认识 → 404,前端把它当
    「尚未登记」继续等 POST 的结果。"""
    if not _IMPORT_KEY_RE.match(import_key or ""):
        raise DomainError(
            "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN",
            "import key is not a valid idempotency key",
            status_code=404,
        )
    job = find_job_by_op_key(session, import_key)
    if job is not None:
        book = StyleReferenceRepository(session).get_book(job.book_id) if job.book_id else None
        snapshot = legacy_progress_snapshot(
            job,
            title=book.title if book is not None else None,
            total_chars=int(book.total_chars or 0) if book is not None else None,
        )
        return ok({"progress": snapshot}, req_id=_req_id(request))
    snapshot = get_import_progress(import_key)
    if snapshot is None:
        raise DomainError(
            "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN",
            "no import with this key is known",
            status_code=404,
        )
    return ok({"progress": snapshot}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/books")
def list_books(
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    books = repo.list_books(status=status)
    latest: dict[tuple[str, str], Any] = {}
    if books:
        for job in session.scalars(
            select(StyleReferenceJob)
            .where(
                StyleReferenceJob.kind.in_((JOB_KIND_CLASSIFY, JOB_KIND_LEARN)),
                StyleReferenceJob.book_id.in_([b.book_id for b in books]),
            )
            .order_by(StyleReferenceJob.created_at)
        ):
            latest[(job.kind, job.book_id)] = job  # 按创建时间升序:最后写入的就是最近一个
    return ok(
        {
            "books": [
                _serialize_book(
                    b,
                    classification_job=latest.get((JOB_KIND_CLASSIFY, b.book_id)),
                    learn_job=latest.get((JOB_KIND_LEARN, b.book_id)),
                )
                for b in books
            ]
        },
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/books/{{book_id}}")
def get_book(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    return ok({"book": _serialize_book(book)}, req_id=_req_id(request))


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
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    return ok({"estimate": estimate_classification(session, book)}, req_id=_req_id(request))


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
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    if start < 0 or end < start:
        raise DomainError(
            "STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID",
            "paragraph range must satisfy 0 <= start <= end",
            status_code=400,
            details={"start": start, "end": end},
        )
    effective_end = min(end, start + PARAGRAPH_RANGE_MAX - 1)
    rows = session.scalars(
        select(StyleReferenceParagraph)
        .where(
            StyleReferenceParagraph.book_id == book_id,
            StyleReferenceParagraph.paragraph_index >= start,
            StyleReferenceParagraph.paragraph_index <= effective_end,
        )
        .order_by(StyleReferenceParagraph.paragraph_index)
    ).all()
    return ok(
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
        req_id=_req_id(request),
    )


@router.delete(f"{PATH_PREFIX}/books/{{book_id}}")
def delete_book(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        book = repo.get_book(book_id)
        if book is None:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
        # 同一事务里先把这本书的活动作业收尾为 cancelled:旧工人的条件写全部落空、不再调模型
        # (删书后同一份文本重新导入,旧线程也不会接着把正文发出去,v3 I2)。
        StyleJobService(session).cancel_all_for_book(book_id)
        # FK 反向 cascade(无 ON DELETE CASCADE):派生数据(含作业与窗口索引)走 purge_derived_data
        # (与破坏式重新分类共用),再删 paragraphs → book
        purge_derived_data(session, book_id)
        repo.delete_paragraphs_for_book(book_id)
        repo.delete_book(book_id)
        return {"book_id": book_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}",
        payload={"book_id": book_id},
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
      回测报告 / 作业 / 窗口索引;段落与书保留),书置 ``ingesting``,从头分类;
    - ``{"mode": "retype"}``:**就地重标段落类型**——正文不变,抽取、画像、绑定全部保留,书保持 ``ready``,
      完成后 ``paragraph_types_revision`` +1;
    - ``{"resume": true}``:把最近一次失败 / 取消 / 中断的分类作业放回队列,从游标续跑(进程重启后也行)。

    LLM 未启用 409 ``STYLE_REFERENCE_LLM_REQUIRED``;「仅本机」的书要求分类节点走本机模型;
    已有排队 / 运行中的分类作业 409 ``STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE``。
    """
    resume = bool(payload.resume) if payload is not None else False
    mode = payload.mode if payload is not None else "reclassify"
    op_key = request.headers.get("X-Idempotency-Key")
    client, enabled = _get_llm_client_and_enabled()
    if not enabled or client is None:
        raise LLMRequiredError(operation="reclassify_book")

    def _do() -> dict[str, Any]:
        service = IngestService(session, llm_enabled=True, op_key=op_key, llm_client=client)
        job = service.resume(book_id) if resume else service.start_reclassify(book_id, mode=mode)
        book = StyleReferenceRepository(session).get_book(book_id)
        return {
            "book": _serialize_book(book, classification_job=job),
            "book_id": book_id,
            "status": "classifying",
            "mode": str((job.params_json or {}).get("mode") or mode),
            "resume": resume,
            "job_id": job.job_id,
            "paragraphs_count": count_paragraphs(session, book_id),
            "classification": classification_payload(job),
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/reclassify",
        payload={"book_id": book_id, "resume": resume, "mode": mode},
        action=_do,
        after_commit=_dispatch_classification,
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
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
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

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/classification/cancel",
        payload={"book_id": book_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


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
    client, enabled = _get_llm_client_and_enabled()
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
        dispatch_job(job_id)


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
    client, _enabled = _get_llm_client_and_enabled()
    return ok(
        {
            "learn": learn_payload(latest_learn_job(session, book_id)),
            "estimate": estimate_learning(session, book),
            "routes": [resolve_node_endpoint(node_id, llm_client=client).as_dict() for node_id in LEARN_NODE_IDS],
        },
        req_id=_req_id(request),
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
        {"runs": [_serialize_run(r) for r in runs]},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/runs/{{run_id}}")
def get_run(
    run_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    run = repo.get_run(run_id)
    if run is None:
        raise DomainError(
            "STYLE_REFERENCE_RUN_NOT_FOUND",
            f"run {run_id!r} not found",
            status_code=404,
        )
    return ok({"run": _serialize_run(run)}, req_id=_req_id(request))


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
                _serialize_finding(
                    f,
                    evidence=(evidence_map.get(f.finding_id, []) if evidence_map is not None else None),
                )
                for f in findings
            ]
        },
        req_id=_req_id(request),
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/profiles")
def list_profiles(
    request: Request,
    book_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profiles = repo.list_profiles(book_id=book_id, status=status)
    return ok(
        {"profiles": [_serialize_profile(p) for p in profiles]},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}")
def get_profile(
    profile_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profile = repo.get_profile(profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    return ok({"profile": _serialize_profile(profile)}, req_id=_req_id(request))


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/card-lines/{{line_id}}")
def set_profile_card_line_state(
    profile_id: str,
    line_id: str,
    payload: CardLineStateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """文风卡一句的 ✓ / ✗:``{"state": "pinned"|"excluded"|null}``(台账 U3)。

    原子地改 ``profile_json.card_line_states`` 里这一句的状态,不重新合成、不改画像状态(以前 ✗ 一条发现会把整份
    画像打回 draft,绑定它的作品随即没了风格参考)。画像没有文风卡 409 ``STYLE_REFERENCE_PROFILE_HAS_NO_CARD``;
    句子不在卡上 404 ``STYLE_REFERENCE_CARD_LINE_NOT_FOUND``。
    """
    state = payload.state

    def _do() -> dict[str, Any]:
        return set_card_line_state(session, profile_id, line_id, state)

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/card-lines/{{line_id}}",
        payload={"profile_id": profile_id, "line_id": line_id, "state": state},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/preview")
def preview_profile(
    profile_id: str,
    request: Request,
    payload: PreviewRequest | None = None,
    session: Session = Depends(get_session),
):
    paragraph_types = (
        tuple(payload.paragraph_types) if payload is not None and payload.paragraph_types else None
    )

    def _do() -> dict[str, Any]:
        client, enabled = _get_llm_client_and_enabled()
        svc = PreviewService(session, llm_client=client, llm_enabled=enabled)
        results = svc.generate(profile_id, target_types=paragraph_types)
        return {
            "profile_id": profile_id,
            "samples": [r.model_dump() for r in results],
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/preview",
        payload={"profile_id": profile_id, "paragraph_types": list(paragraph_types or [])},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/apply")
def apply_profile(
    profile_id: str,
    payload: ApplyProfileRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        try:
            scope = BindingScope(body["scope"])
            task_type = TaskType(body.get("task_type") or "scene_generation")
            raw_strategy = body.get("strategy")
            strategy = InjectionStrategy(raw_strategy) if raw_strategy else None
        except ValueError as exc:
            raise DomainError(
                "STYLE_REFERENCE_APPLY_PARAM_INVALID",
                str(exc),
                status_code=400,
            ) from exc
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id,
            scope=scope,
            scope_ref_id=body.get("scope_ref_id"),
            task_type=task_type,
            strategy=strategy,
            config_json=payload.injection_config() or None,
        )
        return {"profile_id": result.profile_id, "binding_id": result.binding_id}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/apply",
        payload={"profile_id": profile_id, **body},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/bindings")
def list_bindings(
    profile_id: str,
    request: Request,
    task_type: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    bindings = repo.list_bindings(profile_id=profile_id, task_type=task_type)
    return ok(
        {"bindings": [_serialize_binding(b) for b in bindings]},
        req_id=_req_id(request),
    )


@router.delete(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def delete_binding(
    binding_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        binding = repo.get_binding(binding_id)
        # 2026-09-22 结构跟随参考书:绑定删了,它作用范围内按这本参考做的规划产物作废(下一次运行重做)
        superseded = (
            supersede_for_binding_scope(
                session,
                scope=str(binding.scope),
                scope_ref_id=binding.scope_ref_id,
                reason=f"style_binding_deleted:{binding_id}",
            )
            if binding is not None
            else None
        )
        rowcount = repo.delete_binding(binding_id)
        if rowcount == 0:
            raise DomainError(
                "STYLE_REFERENCE_BINDING_NOT_FOUND",
                f"binding {binding_id!r} not found",
                status_code=404,
            )
        return {"binding_id": binding_id, "deleted": True, "superseded_planning": superseded}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/bindings/{{binding_id}}",
        payload={"binding_id": binding_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Banned terms(禁用词:generation=注入红线段填充 / extraction=抽取段落过滤)
# ---------------------------------------------------------------------------


BANNED_TERM_SCOPES = ("generation", "extraction")


def _serialize_banned_term(term) -> dict[str, Any]:
    return {
        "term_id": term.term_id,
        "profile_id": term.profile_id,
        "term": term.term,
        "replacement_hint": term.replacement_hint,
        "source": term.source,
        "scope": term.scope,
        "created_at": term.created_at,
    }


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def list_banned_terms(
    profile_id: str,
    request: Request,
    scope: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    if repo.get_profile(profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    terms = repo.list_banned_terms(profile_id, scope=scope)
    return ok(
        {"terms": [_serialize_banned_term(t) for t in terms]},
        req_id=_req_id(request),
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def create_banned_term(
    profile_id: str,
    payload: BannedTermCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    term_text = payload.term.strip()
    scope = payload.scope.strip()

    def _do() -> dict[str, Any]:
        if not term_text:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                "term must be non-empty",
                status_code=400,
            )
        if scope not in BANNED_TERM_SCOPES:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                f"scope must be one of {BANNED_TERM_SCOPES}",
                status_code=400,
            )
        repo = StyleReferenceRepository(session)
        if repo.get_profile(profile_id) is None:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_NOT_FOUND",
                f"profile {profile_id!r} not found",
                status_code=404,
            )
        # (profile_id, term, scope) 唯一:重复创建返回既有行(幂等友好)
        existing = repo.find_banned_term(profile_id, term_text, scope)
        if existing is not None:
            if payload.replacement_hint is not None:
                existing.replacement_hint = payload.replacement_hint
            return {"term": _serialize_banned_term(existing), "created": False}
        row = repo.create_banned_term(
            term_id=f"sr_term_{uuid.uuid4().hex[:12]}",
            profile_id=profile_id,
            term=term_text,
            replacement_hint=payload.replacement_hint,
            source="user",
            scope=scope,
        )
        return {"term": _serialize_banned_term(row), "created": True}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms",
        payload={
            "profile_id": profile_id,
            "term": term_text,
            "scope": scope,
            "replacement_hint": payload.replacement_hint,
        },
        action=_do,
    )


@router.delete(f"{PATH_PREFIX}/banned-terms/{{term_id}}")
def delete_banned_term(
    term_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        row = repo.get_banned_term(term_id)
        if row is None:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_NOT_FOUND",
                f"banned term {term_id!r} not found",
                status_code=404,
            )
        if row.source == "preset":
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_PROTECTED",
                "preset banned terms cannot be deleted",
                status_code=400,
            )
        repo.delete_banned_term(term_id)
        return {"term_id": term_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/banned-terms/{{term_id}}",
        payload={"term_id": term_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# PR-7 — Validation endpoints
# ---------------------------------------------------------------------------


def _serialize_validation_report(report) -> dict[str, Any]:
    status = report.status
    if not report.verdict and status == "completed":
        # Compatibility for reports created before durable async status was
        # introduced (or by a focused repository test without the new field).
        status = "queued"
    public_status = {
        "queued": "pending",
        "completed": "done",
    }.get(status, status)
    return {
        "report_id": report.report_id,
        "profile_id": report.profile_id,
        "target_kind": report.target_kind,
        "target_ref_id": report.target_ref_id,
        "verdict": report.verdict,
        "status": public_status,
        "error_code": report.error_code,
        "error_text": report.error_text,
        "retryable": bool(report.retryable),
        "started_at": report.started_at,
        "heartbeat_at": report.heartbeat_at,
        "finished_at": report.finished_at,
        "quantitative_json": report.quantitative_json or [],
        "semantic_json": report.semantic_json or [],
        "plagiarism_json": report.plagiarism_json or {},
        "forbidden_hits_json": report.forbidden_hits_json or [],
        "mode_executed": report.mode_executed,
        "created_at": report.created_at,
    }


# async_full 的 pending report(verdict 空)超过该时长视为后台 worker 孤儿
# (进程重启 / 线程池丢失),轮询端点上惰性降级为 fail,避免前端永久轮询。
REPORT_PENDING_TIMEOUT_MINUTES = 10


def _reap_orphan_report(session: Session, report) -> None:
    legacy_pending = not report.verdict and report.status == "completed"
    if report.status == "queued":
        # A queued report has not acquired a worker yet. Startup recovery owns
        # detection of a lost queue because this endpoint cannot distinguish it
        # from valid executor backpressure.
        return
    if report.status != "running" and not legacy_pending:
        return
    from datetime import datetime, timedelta, timezone

    try:
        last_seen = datetime.fromisoformat(
            str(report.heartbeat_at or report.started_at or report.created_at).replace(
                "Z", "+00:00"
            )
        )
    except (TypeError, ValueError):
        return
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=REPORT_PENDING_TIMEOUT_MINUTES
    )
    if last_seen < cutoff:
        report.verdict = "fail"
        report.status = "failed"
        report.error_code = "STYLE_REFERENCE_VALIDATION_INTERRUPTED"
        report.error_text = (
            "async validation was interrupted; submit the text again to retry"
        )
        report.retryable = True
        report.heartbeat_at = utcnow()
        report.finished_at = utcnow()
        session.flush()


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/validate")
def validate_profile_generated(
    profile_id: str,
    payload: ValidateGeneratedRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """PR-7 §7 — sync_only / async_full 双路径 validation。"""
    body = payload.model_dump(mode="json")
    try:
        target_kind = ValidationTargetKind(body.get("target_kind") or "manual")
        mode = ValidationMode(body.get("mode") or "async_full")
    except ValueError as exc:
        raise DomainError(
            "STYLE_REFERENCE_VALIDATE_PARAM_INVALID",
            str(exc),
            status_code=400,
        ) from exc

    req = ValidateRequest(
        generated_text=body["generated_text"],
        target_kind=target_kind,
        target_ref_id=body.get("target_ref_id"),
        mode=mode,
    )
    client, enabled = _get_llm_client_and_enabled()
    background = mode == ValidationMode.ASYNC_FULL

    def _do() -> dict[str, Any]:
        orch = ValidationOrchestrator(session, llm_client=client, llm_enabled=enabled)
        result = orch.validate(profile_id, req, defer_dispatch=background)
        return result.model_dump(mode="json")

    def _dispatch(result: dict[str, Any]) -> None:
        start_style_reference_validation_worker(
            report_id=str(result["report_id"]),
            profile_id=profile_id,
            generated_text=req.generated_text,
            llm_client=client,
            llm_enabled=enabled,
        )

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/validate",
        payload={"profile_id": profile_id, **body},
        action=_do,
        after_commit=_dispatch if background else None,
    )


@router.get(f"{PATH_PREFIX}/reports/{{report_id}}")
def get_validation_report(
    report_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    report = repo.get_validation_report(report_id)
    if report is None:
        raise DomainError(
            "STYLE_REFERENCE_REPORT_NOT_FOUND",
            f"validation report {report_id!r} not found",
            status_code=404,
        )
    _reap_orphan_report(session, report)
    return ok({"report": _serialize_validation_report(report)}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/reports")
def list_validation_reports(
    profile_id: str,
    request: Request,
    verdict: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    reports = repo.list_validation_reports(profile_id=profile_id, verdict=verdict)
    return ok(
        {"reports": [_serialize_validation_report(r) for r in reports]},
        req_id=_req_id(request),
    )


# ---------------------------------------------------------------------------
# PR-9 — Injection preview endpoints
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/bindings/{{binding_id}}/injection-preview")
def get_binding_injection_preview(
    binding_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """读已落盘 binding,按起草时同一套选窗与块次序渲染(v3:``inject.preview``)。"""
    repo = StyleReferenceRepository(session)
    binding = repo.get_binding(binding_id)
    if binding is None:
        raise DomainError(
            "STYLE_REFERENCE_BINDING_NOT_FOUND",
            f"binding {binding_id!r} not found",
            status_code=404,
        )
    if repo.get_profile(binding.profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {binding.profile_id!r} not found",
            status_code=404,
        )
    result = preview_render(
        session,
        binding.profile_id,
        binding.config_json or {},
        strategy=binding.strategy,
        project_id=binding.scope_ref_id if binding.scope == "project" else None,
    )
    return ok(_injection_preview_payload(result), req_id=_req_id(request))


def _injection_preview_payload(result: dict[str, Any]) -> dict[str, Any]:
    stats = result.get("stats") or {}
    return InjectionPreviewResponse(
        fragments=SystemPromptFragments(**result["fragments"]),
        prefix=str(result.get("prefix") or ""),
        user_tail=str(result.get("user_tail") or ""),
        stats=InjectionPreviewStats(**stats) if stats else None,
        window_refs=[dict(item) for item in result.get("window_refs") or []],
        reference_mode=result.get("reference_mode"),
        sample_windows=result.get("sample_windows"),
    ).model_dump()


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/injection-preview")
def dryrun_injection_preview(
    profile_id: str,
    payload: InjectionPreviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """dryrun:不写盘,按入参的绑定配置渲染(v3:与起草同一套选窗、同一个块次序;给了 scene_id 就是这一场
    起草时会拿到的窗)。"""
    # idempotency-exempt: deterministic read-only preview; no binding / selection written (the
    # book's window index may be built once as a cache).
    if StyleReferenceRepository(session).get_profile(profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    config: dict[str, Any] = {"intensity": payload.intensity}
    if payload.reference_mode is not None:
        config["reference_mode"] = payload.reference_mode
    if payload.sample_windows is not None:
        config["sample_windows"] = payload.sample_windows
    if payload.dimension_states:
        config["dimension_states"] = dict(payload.dimension_states)
    if payload.draft_mode is not None:
        config["draft_mode"] = payload.draft_mode
    strategy = payload.strategy.value if payload.strategy is not None else None
    result = preview_render(
        session,
        profile_id,
        config,
        scene_id=payload.scene_id,
        project_id=payload.project_id,
        strategy=strategy,
    )
    return ok(_injection_preview_payload(result), req_id=_req_id(request))


# ---------------------------------------------------------------------------
# Injection 只读辅助:任务默认表 + 叠层预览(前端「注入应用」页数据源)
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/injection/task-defaults")
def get_injection_task_defaults(request: Request):
    """TaskType → 默认策略 + 运行时刷新周期(refresh 真源:llm_node_registry)。"""
    return ok({"tasks": injection_task_defaults()}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/injection/layers")
def get_injection_layers(
    request: Request,
    project_id: str | None = None,
    task_type: str = "scene_generation",
    scene_id: str | None = None,
    character_ids: str | None = None,
    session: Session = Depends(get_session),
):
    """只读叠层预览:resolve_binding_layers 命中层 + 权重/预算分配 + 合并概要。

    character_ids 逗号分隔(onstage 多角色)。无命中层时 layers=[]、merged=null。
    """
    chars = [c.strip() for c in (character_ids or "").split(",") if c.strip()] or None
    # v3:只查列、不渲染(U10);只有最具体的一层生效(applied)
    data = describe_binding_layers(
        session,
        project_id,
        task_type,
        character_ids=chars,
        scene_id=scene_id,
    )
    return ok(data, req_id=_req_id(request))



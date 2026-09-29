from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import ipaddress
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from novel_system.api.errors import install_exception_handlers
from novel_system.api.middleware import UnhandledErrorMiddleware
from novel_system.api.readiness import (  # noqa: F401 — SUPPORTED_DATABASE_REVISION 仍从这里导出
    SUPPORTED_DATABASE_REVISION,
    check_database_ready,
)
from novel_system.api.response import error
from novel_system.api.openapi_contract import install_api_openapi_contract
from novel_system.api.request_limits import RequestBodyLimitMiddleware
from novel_system.api.routes import (
    author_drafts,
    catalog,
    canon_continuity,
    chapter_manuscripts,
    chapter_plan,
    chapters,
    cost,
    library,
    literary_quality,
    project_overview,
    projects,
    review,
    scenes,
    snowflake,
    snowflake_workspace,
    style_fidelity,
    style_reference,
    system_config,
    trash,
    writer_deep_review,
)
from novel_system.db import models  # noqa: F401
from novel_system.settings import get_settings


logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Discovery is synchronous and quick; actual generation remains in the
    # existing background workers.  Every dispatched worker still has to win
    # its durable CAS, so concurrent ASGI worker startups cannot execute the
    # same job twice.
    from novel_system.services.background_recovery import run_startup_recovery

    run_startup_recovery()
    # 风格参考 v3 统一作业表:处理器在各自模块导入时注册(register_job_handler);常驻清扫线程启动时
    # 先清扫一次(心跳过期的 running → queued)并派发全部排队作业,之后每 30 s 一次——重启 /
    # --reload 留下的作业不需要人工介入。
    from novel_system.services.style_reference import import_job  # noqa: F401 — 注册 classify 处理器
    from novel_system.services.style_reference import learn_job  # noqa: F401 — 注册 learn 处理器
    from novel_system.services.style_reference import check_job  # noqa: F401 — 注册 check 处理器
    from novel_system.services.style_reference import cleanup  # noqa: F401 — 登记维护任务（审计遥测 90 天留存）
    from novel_system.services.style_reference.jobs import (
        shutdown_job_workers,
        start_job_sweeper,
    )

    start_job_sweeper()
    try:
        yield
    finally:
        shutdown_job_workers(wait=False)


def _is_loopback_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    # Starlette's in-process TestClient uses this sentinel as the peer name.
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


REMOTE_ACCESS_OPERATOR_REF = "remote-access-token"


def _operator_ref_from_request(request: Request, *, trust_client_header: bool) -> str:
    if not trust_client_header:
        # The remote access token is shared authentication, not an identity
        # provider. Never let its holder forge an arbitrary audit principal.
        return REMOTE_ACCESS_OPERATOR_REF
    actor_ref = (request.headers.get("X-Operator-Ref") or "").strip()
    return actor_ref or "operator"


def create_app() -> FastAPI:
    app_settings = get_settings(include_runtime_config=False)
    if not app_settings.local_only and not app_settings.remote_access_token:
        raise RuntimeError(
            "NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN is required when NOVEL_SYSTEM_LOCAL_ONLY=false"
        )
    app = FastAPI(title="Novel System P2", lifespan=_lifespan)
    allow_origins = list(app_settings.cors_origins)
    allow_credentials = app_settings.cors_allow_credentials and "*" not in allow_origins
    # Register the body limiter before CORS. Starlette inserts new middleware
    # at the front, so CORS can still decorate a 413 response and the request-id
    # middleware declared below remains the outermost boundary.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=app_settings.max_request_body_bytes,
    )
    # Unhandled exceptions become the standard 500 envelope inside CORS, so the
    # browser can read them and the request-id middleware stamps them (B12-03).
    app.add_middleware(
        UnhandledErrorMiddleware,
        expose_error_detail=app_settings.expose_error_detail,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        request.state.request_id = f"req_{uuid.uuid4().hex[:12]}"
        request.state.operator_ref = _operator_ref_from_request(
            request,
            trust_client_header=app_settings.local_only,
        )
        peer_host = request.client.host if request.client is not None else None

        def finalize(response):
            response.headers["X-Request-Id"] = request.state.request_id
            logger.info(
                "api_access method=%s path=%s status=%s duration_ms=%.1f request_id=%s peer=%s",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started_at) * 1000,
                request.state.request_id,
                peer_host or "unknown",
            )
            return response

        health_path = request.url.path in {"/live", "/ready"}
        cors_preflight = bool(
            request.method == "OPTIONS"
            and request.headers.get("Origin")
            and request.headers.get("Access-Control-Request-Method")
        )
        forwarded_request = bool(
            request.headers.get("Forwarded")
            or request.headers.get("X-Forwarded-For")
            or request.headers.get("X-Real-IP")
        )
        # Browser preflight never carries application credentials.  Let the
        # CORS middleware validate its Origin/requested headers; the subsequent
        # real request is still authenticated here without exception.
        if not health_path and not cors_preflight:
            if app_settings.local_only and (
                forwarded_request or not _is_loopback_host(peer_host)
            ):
                response = error(
                    "REMOTE_ACCESS_DISABLED",
                    "this service accepts loopback requests only",
                    status_code=403,
                    details={
                        "local_only": True,
                        "forwarded_request_rejected": forwarded_request,
                    },
                    req_id=request.state.request_id,
                )
                return finalize(response)
            if not app_settings.local_only:
                supplied = request.headers.get("X-Novel-Access-Token")
                expected = app_settings.remote_access_token or ""
                if supplied is None or not hmac.compare_digest(
                    supplied.encode("utf-8"), expected.encode("utf-8")
                ):
                    response = error(
                        "REMOTE_ACCESS_TOKEN_REQUIRED",
                        "valid X-Novel-Access-Token is required",
                        status_code=401,
                        details={"local_only": False},
                        req_id=request.state.request_id,
                    )
                    return finalize(response)
        response = await call_next(request)
        return finalize(response)

    @app.get("/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/ready", tags=["health"])
    def ready() -> dict[str, str]:
        check_database_ready()
        return {"status": "ready"}

    install_exception_handlers(app, expose_error_detail=app_settings.expose_error_detail)

    app.include_router(catalog.router)
    app.include_router(canon_continuity.router)
    app.include_router(chapter_plan.router)
    app.include_router(trash.router)
    app.include_router(chapters.router)
    app.include_router(projects.router)
    app.include_router(project_overview.router)
    app.include_router(cost.router)
    app.include_router(author_drafts.router)
    app.include_router(chapter_manuscripts.router)
    app.include_router(scenes.router)
    app.include_router(snowflake.router)
    app.include_router(snowflake_workspace.router)
    app.include_router(writer_deep_review.router)
    app.include_router(review.router)
    app.include_router(library.router)
    app.include_router(system_config.router)
    app.include_router(literary_quality.router)
    app.include_router(style_reference.router)
    app.include_router(style_fidelity.router)
    install_api_openapi_contract(app)
    return app

from __future__ import annotations

import math
import logging
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, ChapterRunJob, LlmCall, SceneCard, SceneRunState, StoryProject
from novel_system.services.context_budget import finalize_request_budget
from novel_system.services.llm_accounting import (
    LLMAccountingRejected,
    LLMCallContext,
    execute_accounted_call,
    record_rejected_call,
)
from novel_system.services.llm_client import (
    MAX_DEGRADE_HOPS,
    MAX_RETRY_BACKOFF_SECONDS,
    LLMClient,
    LLMRequest,
    LLMResponse,
    OnlineAccountedExecution,
    build_llm_request,
    load_model_routing_config,
    resolve_node_route,
)
from novel_system.services.llm_audit import (
    error_audit_summary,
    json_fingerprint,
    sanitize_audit_summary,
)
from novel_system.services.system_config import load_llm_provider_runtime_configs
from novel_system.settings import get_settings


@dataclass(frozen=True, slots=True)
class _LLMExecutionRuntime:
    execution_id: str
    run_job_id: str | None
    lease_renewer: Callable[..., Any] | None
    detached_lease_renewer: Callable[..., Any] | None


_CURRENT_EXECUTION: ContextVar[_LLMExecutionRuntime | None] = ContextVar("llm_execution", default=None)
logger = logging.getLogger(__name__)

# 不限时(timeout_seconds <= 0)的调度租约上限。租约只用来防重复执行,没有
# 超时秒数可乘时不能退回默认 TTL(几分钟)——那样长任务会在调用中途丢租约,
# 客户端重试会把它当崩溃回收并二次执行(审计 P-8)。取一个与
# llm_reservation_recovery_ttl_seconds 同量级的平顶,崩溃后最多滞留这么久。
UNBOUNDED_TIMEOUT_LEASE_SECONDS = 3_600


def begin_llm_execution(
    execution_id: str,
    *,
    run_job_id: str | None = None,
    lease_renewer: Callable[..., Any] | None = None,
) -> Token[_LLMExecutionRuntime | None]:
    detached_lease_renewer = _resolve_detached_lease_renewer(lease_renewer)
    return _CURRENT_EXECUTION.set(
        _LLMExecutionRuntime(
            execution_id=execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
            detached_lease_renewer=detached_lease_renewer,
        )
    )


def _resolve_detached_lease_renewer(
    lease_renewer: Callable[..., Any] | None,
) -> Callable[..., Any] | None:
    if lease_renewer is None:
        return None
    detached = getattr(lease_renewer, "renew_detached", None)
    if callable(detached):
        return detached
    owner = getattr(lease_renewer, "__self__", None)
    detached = getattr(owner, "renew_detached", None)
    return detached if callable(detached) else None


def end_llm_execution(token: Token[_LLMExecutionRuntime | None]) -> None:
    _CURRENT_EXECUTION.reset(token)


def current_llm_execution_id() -> str | None:
    runtime = _CURRENT_EXECUTION.get()
    return runtime.execution_id if runtime is not None else None


def current_llm_run_job_id() -> str | None:
    runtime = _CURRENT_EXECUTION.get()
    return runtime.run_job_id if runtime is not None else None


def _execution_owner_lease_seconds(
    *,
    request_timeout_seconds: float | None,
    client: object | None = None,
) -> int:
    from novel_system.services.idempotency import owner_lease_grace_seconds, owner_lease_ttl_seconds

    default_ttl = owner_lease_ttl_seconds()
    if request_timeout_seconds is None:
        return default_ttl
    if float(request_timeout_seconds) <= 0:
        return max(default_ttl, UNBOUNDED_TIMEOUT_LEASE_SECONDS)
    timeout_seconds = max(1, math.ceil(float(request_timeout_seconds)))
    physical_attempts = 1
    backoff_envelope = 0
    if isinstance(client, LLMClient):
        attempts_per_hop = max(1, int(client._max_retries) + 1)
        degrade_hops = MAX_DEGRADE_HOPS + 1
        physical_attempts = attempts_per_hop * degrade_hops
        if client._retry_backoff_seconds > 0:
            # Retry-After and jitter can dominate the configured exponential delay.
            # Reserve the capped upper bound for every retry in every degrade hop.
            backoff_envelope = math.ceil(
                degrade_hops
                * max(0, attempts_per_hop - 1)
                * MAX_RETRY_BACKOFF_SECONDS
                * 1.2
            )
    envelope = (
        physical_attempts * timeout_seconds
        + backoff_envelope
        + owner_lease_grace_seconds()
    )
    return max(default_ttl, envelope)


def _renew_execution_owner(
    *,
    request_timeout_seconds: float | None = None,
    client: object | None = None,
) -> bool:
    runtime = _CURRENT_EXECUTION.get()
    if runtime is None or runtime.lease_renewer is None:
        return False
    lease_seconds = _execution_owner_lease_seconds(
        request_timeout_seconds=request_timeout_seconds,
        client=client,
    )
    runtime.lease_renewer(lease_seconds=lease_seconds)
    return True


@contextmanager
def _execution_owner_heartbeat(
    *,
    lease_seconds: int,
    interval_seconds: float | None = None,
):
    """Keep an execution fence alive during blocking provider I/O.

    The callback must use its own database session. SQLAlchemy sessions are not
    shared with this daemon thread, so accounting work on the caller remains
    single-threaded.
    """

    runtime = _CURRENT_EXECUTION.get()
    renewer = runtime.detached_lease_renewer if runtime is not None else None
    if renewer is None:
        yield
        return

    if interval_seconds is None:
        from novel_system.services.idempotency import owner_lease_grace_seconds

        interval_seconds = min(
            float(owner_lease_grace_seconds()),
            max(1.0, float(lease_seconds) / 3.0),
        )
    interval_seconds = max(0.01, float(interval_seconds))
    stop = threading.Event()
    owner_lost: list[Exception] = []

    def _heartbeat() -> None:
        while not stop.wait(interval_seconds):
            try:
                renewer(lease_seconds=lease_seconds)
            except Exception as exc:  # pragma: no cover - exact timing is integration-tested
                if getattr(exc, "code", None) == "RUN_OWNER_LEASE_LOST":
                    owner_lost.append(exc)
                    stop.set()
                    return
                logger.warning("execution owner heartbeat failed; retrying", exc_info=True)

    thread = threading.Thread(
        target=_heartbeat,
        name=f"llm-owner-heartbeat:{runtime.execution_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=min(10.0, max(1.0, interval_seconds)))
        if owner_lost:
            raise owner_lost[0]

# Prompt/draft/output prose lives in its authoritative domain rows. LLM audit
# rows retain bounded hashes, sizes, roles and structural fields only.
CONTINUITY_BUDGET_ERROR_CODE = "CONTINUITY_BUDGET_EXCEEDED"
CONTINUITY_BUDGET_MESSAGE = "Prompt still exceeds the safe continuity budget after deterministic compaction."
SCENE_SPLIT_RECOMMENDATION = "Split the scene and retry generation with a smaller continuity scope."
LLM_PROVIDER_DISABLED_ERROR_CODE = "LLM_PROVIDER_DISABLED"
LLM_PROVIDER_DISABLED_MESSAGE = "Live LLM execution is disabled in this runtime."

# Advisory ad-hoc passes (run_task) borrow an existing *registered* route instead of a
# dedicated node, so they never pollute active node_routing nor trip the sync-activation
# guard (which would reject a routeless pseudo-node with a missing provider_id). The
# borrowed node sits in the same provider/model tier as the conceptual task.
_AD_HOC_ROUTE_ALIASES: dict[str, str] = {
    "auto_critique_llm": "soft_qc",          # §8 independent LLM editor critic
    "narrative_event_extract": "extraction",  # §2 prose event extraction
}


@dataclass(slots=True)
class LLMNodeResult:
    llm_call_id: str
    request: LLMRequest
    response: LLMResponse
    request_summary: dict[str, Any]


class LLMNodeExecutionError(Exception):
    def __init__(
        self,
        *,
        llm_call_id: str,
        error_code: str,
        message: str,
        request_summary: dict[str, Any],
        response_summary: dict[str, Any],
        original_error: Exception | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.llm_call_id = llm_call_id
        self.error_code = error_code
        self.message = message
        self.request_summary = request_summary
        self.response_summary = response_summary
        self.original_error = original_error
        self.retryable = retryable


class LLMNodeContinuityError(LLMNodeExecutionError):
    def __init__(
        self,
        *,
        llm_call_id: str,
        request_summary: dict[str, Any],
        response_summary: dict[str, Any],
        continuity_warning: dict[str, Any],
    ) -> None:
        super().__init__(
            llm_call_id=llm_call_id,
            error_code=CONTINUITY_BUDGET_ERROR_CODE,
            message=CONTINUITY_BUDGET_MESSAGE,
            request_summary=request_summary,
            response_summary=response_summary,
            retryable=False,
        )
        self.continuity_warning = continuity_warning


class LLMNodeRunner:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        routing_config: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.session = session
        self._settings = settings
        self._llm_client = llm_client
        self._routing_config = routing_config
        self._provider_configs: dict[str, Any] | None = None
        self._accounting_lifecycle_observer: Callable[[str, str], None] | None = None

    @property
    def settings(self) -> Any:
        # 运行时设置（活动 api 快照 + 密钥解密）第一次用到时才读：只读服务在构造时就建好运行器，
        # 大多数只读请求一次 LLM 调用都不发，不该为此读库解密。
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    @settings.setter
    def settings(self, value: Any) -> None:
        self._settings = value

    def run(
        self,
        *,
        scene_id: str | None,
        chapter_id: str,
        bundle_id: str,
        bundle_hash: str,
        node_id: str,
        step: str,
        prompt: dict[str, Any],
        user_prompt: str,
        source_draft_row_id: str | None = None,
        source_draft_content: str | None = None,
        temperature_override: float | None = None,
        execution_step_key: str | None = None,
        context: LLMCallContext | None = None,
    ) -> LLMNodeResult:
        call_scope_id = scene_id or (context.scope_id if context is not None else chapter_id)
        llm_call_id = f"llm_call_{call_scope_id}_{uuid.uuid4().hex[:12]}"
        task_config: Any | None = None
        request: LLMRequest | None = None
        request_summary: dict[str, Any] = {}
        if self._llm_client is None:
            # 运行时设置延迟到第一次用到才读；但读设置失败（数据库忙等）不是一次 LLM 调用失败，得在下面的
            # 兜底包装之外原样抛出，照旧由 API 层映射成可重试的 DATABASE_BUSY——和原来在构造时读一样。
            self.settings  # noqa: B018

        try:
            execution_mode = self._provider_execution_mode()
            accounted_context = self._resolve_run_context(
                context=context,
                execution_mode=execution_mode,
                scene_id=scene_id,
                chapter_id=chapter_id,
                node_id=node_id,
                step=step,
                execution_step_key=execution_step_key or step,
            )
            try:
                self._assert_online_execution_available()
            except LLMAccountingRejected as rejection:
                # 假生成已退役：LLM 未启用时不再落入离线罐头稿，而是显式拒绝并
                # 落一条审计行，让"为什么没有生成"在审计里可查。
                request_summary = {
                    "node_id": node_id,
                    "template_name": _template_name(prompt, node_id),
                    "template_version": _template_version(prompt),
                    "bundle_id": bundle_id,
                    "bundle_hash": bundle_hash,
                    "recommended_action": "Enable and configure an LLM provider in System Config, then retry.",
                }
                response_summary = error_audit_summary(rejection, promote_attempt_fields=True)
                record_rejected_call(
                    self.session,
                    None,
                    accounted_context,
                    rejection,
                    llm_call_id=llm_call_id,
                    prompt_hash=prompt.get("prompt_hash"),
                    request_payload_summary=request_summary,
                    response_payload_summary=response_summary,
                )
                raise LLMNodeExecutionError(
                    llm_call_id=llm_call_id,
                    error_code=rejection.code,
                    message=str(rejection),
                    request_summary=request_summary,
                    response_summary=response_summary,
                    original_error=rejection,
                    retryable=False,
                ) from rejection
            try:
                task_config = self.task_config(node_id)
            except KeyError as exc:
                # 不论 llm_enabled 与否都给统一的引导错误:离线模式缺路由同样是
                # 配置缺口;旧的裸 KeyError 审计行(error_code="KeyError")对排障无用,
                # 且运行时配置读取瞬时失败时会被误判为离线而落入该分支。
                request_summary = {
                    "node_id": node_id,
                    "template_name": _template_name(prompt, node_id),
                    "template_version": _template_version(prompt),
                    "bundle_id": bundle_id,
                    "bundle_hash": bundle_hash,
                    "source_draft_row_id": source_draft_row_id,
                    "recommended_action": "Configure this node in System Config > LLM node routes, or run sync-missing.",
                }
                response_summary = {
                    "message": f"LLM node route is not configured: {node_id}",
                    "node_id": node_id,
                    "error_code": "LLM_ROUTE_NOT_CONFIGURED",
                    "retryable": False,
                    "recommended_action": "Open System Config > LLM and sync missing active node routes.",
                }
                rejection = LLMAccountingRejected(
                    "LLM_ROUTE_NOT_CONFIGURED",
                    f"LLM node route is not configured: {node_id}",
                    details={"node_id": node_id},
                )
                record_rejected_call(
                    self.session,
                    None,
                    accounted_context,
                    rejection,
                    llm_call_id=llm_call_id,
                    prompt_hash=prompt.get("prompt_hash"),
                    request_payload_summary=request_summary,
                    response_payload_summary=response_summary,
                )
                raise LLMNodeExecutionError(
                    llm_call_id=llm_call_id,
                    error_code="LLM_ROUTE_NOT_CONFIGURED",
                    message=f"LLM node route is not configured: {node_id}",
                    request_summary=request_summary,
                    response_summary=response_summary,
                    original_error=exc,
                    retryable=False,
                ) from exc
            request = self._build_request(prompt, user_prompt=user_prompt, node_id=node_id, task_config=task_config, temperature_override=temperature_override)
            final_budget = finalize_request_budget(
                system_prompt=request.messages[0]["content"],
                user_prompt=request.messages[1]["content"],
                base_budget=prompt["token_budget"],
            )
            request_summary = self._request_summary(
                prompt=prompt,
                request=request,
                task_config=task_config,
                final_budget=final_budget,
                bundle_id=bundle_id,
                bundle_hash=bundle_hash,
                source_draft_row_id=source_draft_row_id,
                source_draft_content=source_draft_content,
            )
            continuity_warning = final_budget["continuity_warning"]
            if _requires_scene_split(continuity_warning):
                response_summary = {
                    "message": CONTINUITY_BUDGET_MESSAGE,
                    "continuity_warning": continuity_warning,
                    "recommended_action": SCENE_SPLIT_RECOMMENDATION,
                    "attempt_count": 0,
                    "max_retries": 0,
                    "retryable": False,
                    "details": {"continuity_warning": continuity_warning},
                }
                rejection = LLMAccountingRejected(
                    CONTINUITY_BUDGET_ERROR_CODE,
                    CONTINUITY_BUDGET_MESSAGE,
                    details={"continuity_warning": continuity_warning},
                )
                record_rejected_call(
                    self.session,
                    request,
                    accounted_context,
                    rejection,
                    llm_call_id=llm_call_id,
                    request_payload_summary=request_summary,
                    response_payload_summary=response_summary,
                )
                raise LLMNodeContinuityError(
                    llm_call_id=llm_call_id,
                    request_summary=request_summary,
                    response_summary=response_summary,
                    continuity_warning=continuity_warning,
                )

            client = self._client()
            request_timeout_seconds = request.timeout_seconds or self.settings.llm_timeout_seconds
            lease_seconds = _execution_owner_lease_seconds(
                request_timeout_seconds=request_timeout_seconds,
                client=client,
            )
            _renew_execution_owner(
                request_timeout_seconds=request_timeout_seconds,
                client=client,
            )
            # Provider I/O must never hold an open database transaction. The owner
            # renewal above is fenced and committed before dispatch.
            self.session.commit()
            try:
                with _execution_owner_heartbeat(lease_seconds=lease_seconds):
                    response = execute_accounted_call(
                        self.session,
                        client,
                        request,
                        accounted_context,
                        llm_call_id=llm_call_id,
                        _lifecycle_observer=self._accounting_lifecycle_observer,
                    )
            finally:
                _renew_execution_owner()
                self.session.commit()
        except LLMNodeExecutionError:
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", exc.__class__.__name__)
            response_summary = error_audit_summary(exc, promote_attempt_fields=True)
            details = getattr(exc, "details", None)
            if isinstance(details, dict) and details.get("llm_call_id"):
                llm_call_id = str(details["llm_call_id"])
            if self.session.get(LlmCall, llm_call_id) is not None:
                self._supplement_accounted_audit(
                    llm_call_id,
                    request_summary=request_summary,
                    response_summary=response_summary,
                )
            raise LLMNodeExecutionError(
                llm_call_id=llm_call_id,
                error_code=error_code,
                message=str(exc),
                request_summary=request_summary,
                response_summary=response_summary,
                original_error=exc,
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc

        response_summary = {
            "request_id": response.request_id,
            "response_format": response.response_format,
            # SceneDraft/FinalScene own prose; the audit row stores a structural fingerprint.
            "structured_output": json_fingerprint(response.structured_output),
            "attempt_count": response.attempt_count,
            "max_retries": response.max_retries,
            "retryable": response.retryable,
        }
        llm_call_id = response.llm_call_id or llm_call_id
        self._supplement_accounted_audit(
            llm_call_id,
            request_summary=request_summary,
            response_summary=response_summary,
        )
        return LLMNodeResult(
            llm_call_id=llm_call_id,
            request=request,
            response=response,
            request_summary=request_summary,
        )

    def run_task(
        self,
        *,
        task_name: str,
        prompt_text: str,
        system_prompt: str,
        context: LLMCallContext,
        temperature_override: float | None = None,
    ) -> LLMResponse:
        """Ad-hoc single-shot LLM call for auxiliary advisory passes (§8 LLM critic,
        §2 prose event extraction).

        Unlike ``run``, this is NOT persisted as a scene draft and never blocks the
        pipeline: callers must opt-in (guard on settings) and wrap the call so that any
        raised exception degrades to a no-op. A disabled, unavailable, or
        misconfigured provider fails fast into the caller's try/except rather
        than silently returning substitute text.
        """
        self._assert_online_execution_available()
        if context.provider_execution_mode != "online":
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_CONTEXT_INVALID",
                "run_task requires online provider execution",
            )
        route_node = _AD_HOC_ROUTE_ALIASES.get(task_name, task_name)
        self._validate_task_context(context, expected_node_id=route_node)
        try:
            task_config = self.task_config(route_node)
        except KeyError as exc:
            rejection = LLMAccountingRejected(
                "LLM_ROUTE_NOT_CONFIGURED",
                f"LLM node route is not configured: {route_node}",
                details={"node_id": route_node, "task_name": task_name},
            )
            llm_call_id = record_rejected_call(
                self.session,
                None,
                context,
                rejection,
                request_payload_summary={"task_name": task_name, "route_node": route_node},
            )
            raise LLMNodeExecutionError(
                llm_call_id=llm_call_id,
                error_code=rejection.code,
                message=str(rejection),
                request_summary={"task_name": task_name, "route_node": route_node},
                response_summary=error_audit_summary(rejection, promote_attempt_fields=True),
                original_error=exc,
                retryable=False,
            ) from exc
        prompt = {"system_prompt": system_prompt, "token_budget": {}}
        request = self._build_request(
            prompt,
            user_prompt=prompt_text,
            node_id=route_node,
            task_config=task_config,
            temperature_override=temperature_override,
        )

        client = self._client()
        llm_call_id = f"llm_task_{uuid.uuid4().hex}"
        request_timeout_seconds = request.timeout_seconds or self.settings.llm_timeout_seconds
        lease_seconds = _execution_owner_lease_seconds(
            request_timeout_seconds=request_timeout_seconds,
            client=client,
        )
        _renew_execution_owner(
            request_timeout_seconds=request_timeout_seconds,
            client=client,
        )
        self.session.commit()
        try:
            try:
                with _execution_owner_heartbeat(lease_seconds=lease_seconds):
                    return execute_accounted_call(
                        self.session,
                        client,
                        request,
                        context,
                        llm_call_id=llm_call_id,
                        _lifecycle_observer=self._accounting_lifecycle_observer,
                    )
            except Exception as exc:
                details = getattr(exc, "details", None)
                if isinstance(details, dict) and details.get("llm_call_id"):
                    llm_call_id = str(details["llm_call_id"])
                raise LLMNodeExecutionError(
                    llm_call_id=llm_call_id,
                    error_code=getattr(exc, "code", exc.__class__.__name__),
                    message=str(exc),
                    request_summary={"task_name": task_name, "route_node": route_node},
                    response_summary=error_audit_summary(exc, promote_attempt_fields=True),
                    original_error=exc,
                    retryable=bool(getattr(exc, "retryable", False)),
                ) from exc
        finally:
            _renew_execution_owner()
            self.session.commit()

    def task_config(self, node_id: str) -> Any:
        # 每个运行时节点必须在自己的 node id 下绑定路由(node_routing 优先,
        # task_routing 兜底,缺失 KeyError——顺序与教训见 resolve_node_route)。
        # 离线模式退役后不再允许"借用"别的节点的 provider/model——那会掩盖
        # 代码与路由快照的漂移。run_task 的 ad-hoc 别名在进入该边界之前已由
        # _AD_HOC_ROUTE_ALIASES 解析。
        return resolve_node_route(self._routing(), node_id)

    def _routing(self) -> Any:
        if self._routing_config is None:
            self._routing_config = load_model_routing_config()
        return self._routing_config

    @staticmethod
    def _build_request(
        prompt: dict[str, Any],
        *,
        user_prompt: str,
        node_id: str,
        task_config: Any,
        temperature_override: float | None = None,
    ) -> LLMRequest:
        return build_llm_request(
            task_config,
            node_id=node_id,
            messages=[
                {"role": "system", "content": prompt["system_prompt"]},
                {"role": "user", "content": user_prompt},
            ],
            response_schema=_response_schema(prompt, node_id=node_id),
            temperature_override=temperature_override,
        )

    @staticmethod
    def _request_summary(
        *,
        prompt: dict[str, Any],
        request: LLMRequest,
        task_config: Any,
        final_budget: dict[str, Any],
        bundle_id: str,
        bundle_hash: str,
        source_draft_row_id: str | None,
        source_draft_content: str | None,
    ) -> dict[str, Any]:
        summary = {
            "template_name": _template_name(prompt, request.node_id or "llm_node"),
            "template_version": _template_version(prompt),
            "messages": request.messages,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "response_format": request.response_format,
            "provider": request.provider,
            "provider_id": request.provider_id,
            "account_id": request.account_id,
            "model_profile": getattr(task_config, "model_profile", None),
            "reasoning_level": request.reasoning_level,
            "credential_mode": request.credential_mode,
            "token_budget": final_budget["budget"],
            "continuity_warning": final_budget["continuity_warning"],
            "bundle_id": bundle_id,
            "bundle_hash": bundle_hash,
        }
        if source_draft_row_id is not None:
            summary["source_draft_row_id"] = source_draft_row_id
        if source_draft_content is not None:
            # The source row owns prose; the audit summary converts this to a fingerprint.
            summary["source_draft_content"] = source_draft_content
        if isinstance(prompt.get("_style_reference_runtime_audit"), dict):
            summary["style_reference_runtime"] = prompt[
                "_style_reference_runtime_audit"
            ]
        return sanitize_audit_summary(summary)

    def _client(self) -> Any:
        self._assert_online_execution_available()
        if self._llm_client is not None:
            return self._llm_client
        return LLMClient(
            provider=self.settings.llm_provider,
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            timeout_seconds=self.settings.llm_timeout_seconds,
            retry_backoff_seconds=self._retry_backoff_seconds(),
            provider_configs=self._runtime_provider_configs(),
        )

    @property
    def provider_execution_mode(self) -> str:
        """Expose the selected execution kind so callers can construct typed ownership."""
        return self._provider_execution_mode()

    def _provider_execution_mode(self) -> str:
        return "online"

    def _assert_online_execution_available(self) -> None:
        if self._llm_client is not None:
            if isinstance(self._llm_client, OnlineAccountedExecution):
                return
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
                "injected LLM clients must implement accounted online execution",
                retryable=False,
            )
        if not self.settings.llm_enabled:
            raise LLMAccountingRejected(
                LLM_PROVIDER_DISABLED_ERROR_CODE,
                LLM_PROVIDER_DISABLED_MESSAGE,
                retryable=False,
            )

    def _validate_task_context(
        self,
        context: LLMCallContext,
        *,
        expected_node_id: str,
    ) -> None:
        def reject(field: str, message: str) -> None:
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_CONTEXT_INVALID",
                message,
                details={"missing_or_invalid_field": field},
            )

        runtime = _CURRENT_EXECUTION.get()
        if context.node_id != expected_node_id:
            reject("node_id", "context node must match the resolved task route")
        if runtime is None:
            if (
                context.execution_id is not None
                or context.execution_step_key is not None
                or context.run_job_id is not None
            ):
                reject("execution_id", "context without an active execution cannot invent execution ownership")
        elif (
            context.execution_id != runtime.execution_id
            or context.run_job_id != runtime.run_job_id
            or not str(context.execution_step_key or "").strip()
        ):
            reject("execution_id", "context must use the current execution owner and a stable step key")

        if context.scope_type == "scene":
            scene = self.session.get(SceneCard, context.scene_id)
            chapter = self.session.get(ChapterGoal, context.chapter_id)
            project_id = (
                scene.project_id or (chapter.project_id if chapter is not None else None)
                if scene is not None
                else None
            )
            if (
                scene is None
                or context.scope_id != context.scene_id
                or scene.chapter_id != context.chapter_id
                or context.project_id != project_id
                or not str(project_id or "").strip()
            ):
                reject("scene_id", "scene context does not match persisted scene ownership")
            self._validate_scene_job_ownership(
                scene=scene,
                chapter_id=context.chapter_id,
                run_job_id=context.run_job_id,
                reject=reject,
            )
            return
        if context.scene_id is not None:
            reject("scene_id", "non-scene context cannot claim scene ownership")
        if context.scope_type == "chapter":
            chapter = self.session.get(ChapterGoal, context.chapter_id)
            if (
                chapter is None
                or context.scope_id != context.chapter_id
                or context.project_id != chapter.project_id
                or not str(chapter.project_id or "").strip()
            ):
                reject("chapter_id", "chapter context does not match persisted chapter ownership")
            self._validate_chapter_job_ownership(
                chapter_id=context.chapter_id,
                run_job_id=context.run_job_id,
                reject=reject,
            )
            return
        if context.scope_type == "project":
            if context.run_job_id is not None:
                reject("run_job_id", "project context cannot claim chapter run ownership")
            if (
                context.chapter_id is not None
                or context.project_id != context.scope_id
                or self.session.get(StoryProject, context.scope_id) is None
            ):
                reject("project_id", "project context does not match a persisted project")
            return
        if context.scope_type == "system":
            if (
                context.project_id is not None
                or context.chapter_id is not None
                or context.run_job_id is not None
            ):
                reject("scope_type", "system context cannot claim project or chapter ownership")
            return
        reject("scope_type", "unsupported LLM accounting scope type")

    def _resolve_run_context(
        self,
        *,
        context: LLMCallContext | None,
        execution_mode: str,
        scene_id: str | None,
        chapter_id: str,
        node_id: str,
        step: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        runtime = _CURRENT_EXECUTION.get()
        if context is None:
            return self._scene_accounting_context(
                runtime=runtime,
                execution_mode=execution_mode,
                scene_id=scene_id,
                chapter_id=chapter_id,
                node_id=node_id,
                step=step,
                execution_step_key=execution_step_key,
            )

        def reject(field: str, message: str) -> None:
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_CONTEXT_INVALID",
                message,
                details={"missing_or_invalid_field": field},
            )

        if context.provider_execution_mode != execution_mode:
            reject("provider_execution_mode", "explicit context execution mode does not match the selected client")
        if context.node_id != node_id or context.step != step:
            reject("node_id", "explicit context node and step must match the runner call")
        if context.scope_type == "scene":
            if scene_id is None:
                reject("scene_id", "scene context requires a scene id")
            expected = self._scene_accounting_context(
                runtime=runtime,
                execution_mode=execution_mode,
                scene_id=scene_id,
                chapter_id=chapter_id,
                node_id=node_id,
                step=step,
                execution_step_key=execution_step_key,
            )
            ownership_fields = (
                "scope_id",
                "project_id",
                "scene_id",
                "chapter_id",
                "execution_id",
                "execution_step_key",
                "run_job_id",
            )
            if any(getattr(context, field) != getattr(expected, field) for field in ownership_fields):
                reject("scene_id", "explicit scene context does not match persisted scene ownership")
            return context
        if context.scene_id is not None:
            reject("scene_id", "non-scene context cannot claim a scene id")
        if context.scope_type == "chapter":
            chapter = self.session.get(ChapterGoal, context.chapter_id)
            if (
                context.scope_id != chapter_id
                or context.chapter_id != chapter_id
                or chapter is None
                or not str(chapter.project_id or "").strip()
                or context.project_id != chapter.project_id
            ):
                reject("chapter_id", "explicit chapter context does not match persisted chapter ownership")
            self._validate_chapter_job_ownership(
                chapter_id=context.chapter_id,
                run_job_id=context.run_job_id,
                reject=reject,
            )
        elif context.scope_type == "project":
            if context.run_job_id is not None:
                reject("run_job_id", "project context cannot claim chapter run ownership")
            if (
                context.chapter_id is not None
                or context.project_id != context.scope_id
                or self.session.get(StoryProject, context.scope_id) is None
            ):
                reject("project_id", "explicit project context does not match a persisted project")
        elif context.scope_type == "system":
            if (
                context.project_id is not None
                or context.chapter_id is not None
                or context.run_job_id is not None
            ):
                reject("scope_type", "system context cannot claim project or chapter ownership")
        else:
            reject("scope_type", "unsupported LLM accounting scope type")
        if runtime is None:
            if context.execution_id is not None or context.execution_step_key is not None:
                reject("execution_id", "context without an active execution cannot invent execution ownership")
        elif (
            context.execution_id != runtime.execution_id
            or context.run_job_id != runtime.run_job_id
            or context.execution_step_key != execution_step_key
        ):
            reject("execution_id", "explicit context must use the current execution owner and stable step key")
        return context

    def _scene_accounting_context(
        self,
        *,
        runtime: _LLMExecutionRuntime | None,
        execution_mode: str,
        scene_id: str,
        chapter_id: str,
        node_id: str,
        step: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        def reject(field: str, message: str) -> None:
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_CONTEXT_INVALID",
                message,
                details={"missing_or_invalid_field": field},
            )

        if runtime is not None and not str(runtime.execution_id).strip():
            reject("execution_id", "durable online execution requires a stable execution id")
        if not str(scene_id).strip():
            reject("scene_id", "durable online execution requires a scene id")
        if not str(step).strip():
            reject("step", "scene execution requires a stable step")
        if runtime is not None and not str(execution_step_key).strip():
            reject("execution_step_key", "durable execution requires a stable step key")
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            reject("scene_id", "durable online execution scene does not exist")
        resolved_chapter_id = scene.chapter_id
        if not str(resolved_chapter_id).strip() or chapter_id != resolved_chapter_id:
            reject("chapter_id", "durable online execution chapter does not match its scene")
        chapter = self.session.get(ChapterGoal, resolved_chapter_id)
        project_id = scene.project_id or (chapter.project_id if chapter is not None else None)
        if not str(project_id or "").strip():
            reject("project_id", "durable online execution requires a project-owned scene")
        self._validate_scene_job_ownership(
            scene=scene,
            chapter_id=resolved_chapter_id,
            run_job_id=runtime.run_job_id if runtime is not None else None,
            reject=reject,
        )
        return LLMCallContext(
            scope_type="scene",
            scope_id=scene.scene_id,
            project_id=project_id,
            scene_id=scene.scene_id,
            chapter_id=resolved_chapter_id,
            node_id=node_id,
            step=step,
            execution_id=runtime.execution_id if runtime is not None else None,
            execution_step_key=execution_step_key if runtime is not None else None,
            run_job_id=runtime.run_job_id if runtime is not None else None,
            provider_execution_mode=execution_mode,
        )

    def _validate_scene_job_ownership(
        self,
        *,
        scene: SceneCard,
        chapter_id: str,
        run_job_id: str | None,
        reject: Callable[[str, str], None],
    ) -> None:
        state = self.session.get(SceneRunState, scene.scene_id)
        active_job_id = state.active_run_job_id if state is not None else None
        if run_job_id is None:
            if active_job_id is not None:
                reject("run_job_id", "scene context omitted the authoritative active run job")
            return
        job = self.session.get(ChapterRunJob, run_job_id)
        if job is None or job.status != "running" or job.chapter_id != chapter_id:
            reject("run_job_id", "run job is missing, inactive, or owned by another chapter")
        if job.job_type == "scene_run_full":
            if active_job_id != run_job_id or job.scene_id != scene.scene_id:
                reject("run_job_id", "scene run job is owned by another scene")
            return
        if job.job_type == "chapter_run_full":
            payload = job.payload_json if isinstance(job.payload_json, dict) else {}
            scene_ids = payload.get("scene_ids")
            if (
                not isinstance(scene_ids, list)
                or scene.scene_id not in scene_ids
                or payload.get("current_scene_id") != scene.scene_id
                or active_job_id not in {None, run_job_id}
            ):
                reject("run_job_id", "chapter run job is not currently executing this scene")
            return
        reject("run_job_id", "unsupported run job type for scene execution")

    def _validate_chapter_job_ownership(
        self,
        *,
        chapter_id: str | None,
        run_job_id: str | None,
        reject: Callable[[str, str], None],
    ) -> None:
        if run_job_id is None:
            return
        job = self.session.get(ChapterRunJob, run_job_id)
        if job is None or job.status != "running" or job.chapter_id != chapter_id:
            reject("run_job_id", "chapter context does not match an active chapter run job")
        if job.job_type == "scene_run_full":
            state = self.session.get(SceneRunState, job.scene_id)
            if job.scene_id is None or state is None or state.active_run_job_id != run_job_id:
                reject("run_job_id", "scene run job is not active for chapter evaluation")
            return
        if job.job_type == "chapter_run_full":
            payload = job.payload_json if isinstance(job.payload_json, dict) else {}
            current_scene_id = payload.get("current_scene_id")
            state = self.session.get(SceneRunState, current_scene_id)
            if (
                not isinstance(current_scene_id, str)
                or state is None
                or state.active_run_job_id != run_job_id
            ):
                reject("run_job_id", "chapter run job has no active current scene")
            return
        reject("run_job_id", "unsupported run job type for chapter execution")

    def _supplement_accounted_audit(
        self,
        llm_call_id: str,
        *,
        request_summary: dict[str, Any],
        response_summary: dict[str, Any],
    ) -> None:
        parent = self.session.get(LlmCall, llm_call_id)
        if parent is None:
            raise RuntimeError(f"accounted llm call {llm_call_id} disappeared after settlement")
        parent.request_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.request_payload_summary or {}),
                **request_summary,
            }
        )
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                **response_summary,
            }
        )
        self.session.commit()

    def _retry_backoff_seconds(self) -> float:
        """生产默认 1.5s 指数退避;models 配置 job_runtime.llm_retry_backoff_seconds 可调。"""
        try:
            value = self._routing().job_runtime.get("llm_retry_backoff_seconds")
            if value is not None:
                return max(0.0, float(value))
        except Exception:
            pass
        return 1.5

    def _runtime_provider_configs(self) -> dict[str, Any]:
        if self._provider_configs is None:
            self._provider_configs = load_llm_provider_runtime_configs()
        return self._provider_configs

def _requires_scene_split(continuity_warning: Any) -> bool:
    return isinstance(continuity_warning, dict) and bool(continuity_warning.get("requires_scene_split"))


def _response_schema(prompt: dict[str, Any], *, node_id: str) -> dict[str, Any]:
    return {"name": _template_name(prompt, node_id), "schema": prompt.get("structured_schema", {})}


def _template_name(prompt: dict[str, Any], fallback: str) -> str:
    value = prompt.get("template_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _template_version(prompt: dict[str, Any]) -> str:
    value = prompt.get("template_version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "unknown"

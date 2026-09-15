"""RunOrchestrator — Style Reference 抽取 run 编排。

§14:启动 run + LLMRequiredError + 按 layers 调度四层 extractor
(language / narrative / scene / theme,见 _LAYER_EXTRACTOR_MAP),默认四层全跑。
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceRun

from novel_system.services.errors import DomainError
from novel_system.services.style_reference.background_heartbeat import periodic_heartbeat
from novel_system.services.style_reference.dimensions import LAYER_TO_SUB_DIMS, Layer, SubDimension
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.extractors import (
    BaseExtractor,
    ExtractionRetryPolicy,
    ExtractionRunResult,
    LanguageExtractor,
    NarrativeExtractor,
    SceneExtractor,
    ThemeExtractor,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.sampling import derive_extraction_rng
from novel_system.services.style_reference.schemas import RunPhase, RunStatus

logger = logging.getLogger(__name__)

# RUNNING 超过该时长的 run 视为僵尸(进程崩溃 / 连接中断遗留),
# 下次同书启动新 run 时自动降级 FAILED,避免永久卡死。
STALE_RUN_TIMEOUT_MINUTES = 60
RUN_HEARTBEAT_INTERVAL_SECONDS = 30

# 后台抽取串行执行(单 worker):抽取是重 LLM 负载,串行同时规避 SQLite 写争用。
# 执行器按需创建并由 FastAPI lifespan 关闭；这也允许同进程 TestClient 重启。
_RUN_EXECUTOR: ThreadPoolExecutor | None = None
_RUN_EXECUTOR_LOCK = threading.Lock()


# layer.value → BaseExtractor 子类
_LAYER_EXTRACTOR_MAP: dict[Layer, type[BaseExtractor]] = {
    Layer.LANGUAGE: LanguageExtractor,
    Layer.NARRATIVE: NarrativeExtractor,
    Layer.SCENE: SceneExtractor,
    Layer.THEME: ThemeExtractor,
}


@dataclass
class RunResult:
    """run 编排执行后的摘要。"""

    run_id: str
    book_id: str
    status: str
    layers: list[str] = field(default_factory=list)
    sub_dim_results: list[ExtractionRunResult] = field(default_factory=list)


def _initial_progress(layers: list[Layer], *, completed: int = 0) -> dict[str, Any]:
    """``coverage_json["progress"]`` 的完整形状(2026-09-15 子维粒度)。

    层粒度键(``layers_total / layers_done / current_layer``)保留给老读者;子维粒度键给
    活动清单 / 前端进度条:``sub_dims_total / sub_dims_done / current_sub_dim``、累计
    ``llm_calls`` 与其中的 ``retries``(同一子维的第 2 次起调用:补抽 / 整维重抽),
    以及每个已完成子维的耗时 ``sub_dim_seconds``(活动清单据此估算剩余时间)。
    """
    return {
        "layers_total": len(layers),
        "layers_done": 0,
        "current_layer": layers[0].value if layers else None,
        "sub_dims_total": sum(len(LAYER_TO_SUB_DIMS[layer]) for layer in layers),
        "sub_dims_done": int(completed),
        "current_sub_dim": None,
        "llm_calls": 0,
        "retries": 0,
        "sub_dim_seconds": [],
        "updated_at": _utcnow_iso(),
    }


class _RunProgressTracker:
    """抽取器的 ``ExtractionProgress`` 实现:把子维粒度进度写进 run 的 ``coverage_json``。

    后台模式(``commit=True``)每次汇报都 commit,轮询端点立刻可见;同步模式只更新
    session,随请求事务一起提交。写失败只记日志,绝不打断抽取。
    """

    def __init__(
        self,
        repo: StyleReferenceRepository,
        session: Session,
        run_id: str,
        layers: list[Layer],
        *,
        commit: bool,
        completed: int = 0,
    ) -> None:
        self._repo = repo
        self._session = session
        self._run_id = run_id
        self._commit = commit
        self.progress = _initial_progress(layers, completed=completed)
        self._calls_in_sub_dim = 0
        self._sub_dim_started_monotonic: float | None = None

    def layer_started(self, layer: Layer, layers_done: int) -> None:
        self.progress["layers_done"] = int(layers_done)
        self.progress["current_layer"] = layer.value
        self._write()

    def layers_finished(self, layers: list[Layer]) -> None:
        self.progress["layers_done"] = len(layers)
        self.progress["current_layer"] = None
        self.progress["current_sub_dim"] = None
        self.progress["sub_dims_done"] = self.progress["sub_dims_total"]
        self._write()

    # ---- ExtractionProgress ------------------------------------------------
    def sub_dim_started(self, sub_dim: SubDimension) -> None:
        self.progress["current_sub_dim"] = sub_dim.value
        self._calls_in_sub_dim = 0
        self._sub_dim_started_monotonic = time.monotonic()
        self._write()

    def sub_dim_done(self, sub_dim: SubDimension) -> None:  # noqa: ARG002
        self.progress["sub_dims_done"] = int(self.progress.get("sub_dims_done") or 0) + 1
        self.progress["current_sub_dim"] = None
        if self._sub_dim_started_monotonic is not None:
            seconds = list(self.progress.get("sub_dim_seconds") or [])
            seconds.append(round(time.monotonic() - self._sub_dim_started_monotonic, 1))
            self.progress["sub_dim_seconds"] = seconds[-64:]
        self._sub_dim_started_monotonic = None
        self._write()

    def llm_call(self, node_id: str) -> None:  # noqa: ARG002
        self.progress["llm_calls"] = int(self.progress.get("llm_calls") or 0) + 1
        self._calls_in_sub_dim += 1
        if self._calls_in_sub_dim > 1:
            self.progress["retries"] = int(self.progress.get("retries") or 0) + 1
        self._write()

    def _write(self) -> None:
        self.progress["updated_at"] = _utcnow_iso()
        try:
            run = self._repo.get_run(self._run_id)
            if run is None:
                return
            coverage = dict(run.coverage_json or {})
            coverage["progress"] = dict(self.progress)
            self._repo.update_run(
                self._run_id,
                coverage_json=coverage,
                heartbeat_at=_utcnow_iso(),
            )
            if self._commit:
                self._session.commit()
        except Exception:  # pragma: no cover - progress must never break extraction
            logger.exception("failed to write extraction progress for run %s", self._run_id)
            if self._commit:
                try:
                    self._session.rollback()
                except Exception:  # pragma: no cover
                    logger.exception("progress rollback failed for run %s", self._run_id)


class RunOrchestrator:
    """启动 run + 按 layers 调度 extractors + 落 4 表 + 更新 run.status。"""

    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_enabled: bool | None = None,
        retry_policy: ExtractionRetryPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)
        self._llm_client = llm_client
        if llm_enabled is None:
            from novel_system.settings import get_settings

            llm_enabled = bool(get_settings().llm_enabled)
        self._llm_enabled = llm_enabled
        self._retry_policy = retry_policy or ExtractionRetryPolicy()
        # None → _execute 时以 sha256(text_checksum + run_id) 定种(同一 run 可复现,
        # resume / 后台 worker 拿到同一采样序列);测试与基准可显式注入。
        self._rng = rng

    def start_extract_run(
        self,
        book_id: str,
        *,
        layers: list[Layer] | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 (预留 PR-4 路由层接入)
        background: bool = False,
        force: bool = False,
        defer_dispatch: bool = False,
    ) -> RunResult:
        """启动一次抽取 run;LLM 不可用 raise DomainError(STYLE_REFERENCE_LLM_REQUIRED)。

        ``background=True`` 时立即返回 RUNNING 的 RunResult,抽取在后台线程
        独立 session 中执行;调用方轮询 ``GET /runs/{run_id}`` 读取
        ``coverage_json["progress"]``(按 layer 粒度)与最终状态。

        §6.4 输入量门槛(2026-07 接线):ingest 时按 input_thresholds 评估为
        ``skip`` 的层**不消耗 LLM 调用**——此前该评估只算不执行,字数不足的书照样
        全四层抽取,与前端矩阵页展示的 skip 相互矛盾。被剔除的层记录在
        ``coverage_json["skipped_layers"]``;全部被剔除时 409
        ``STYLE_REFERENCE_INPUT_TOO_SMALL``。``force=True`` 跳过该门槛(明知
        字数不足仍要抽取的显式逃生门,样本饥饿风险自负)。
        """
        if not self._llm_enabled or self._llm_client is None:
            raise LLMRequiredError(operation="start_extract_run")

        book = self.repo.get_book(book_id)
        if book is None:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
        # 附录 B — local_only 的书禁止把段落送往云端 LLM
        ensure_cloud_llm_allowed(book, operation="start_extract_run")
        # 2026-09-15 严格 LLM:分类还没完成(或失败)的书不能抽取——段型是采样与窗口的依据
        if str(book.status or "") != "ready":
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_READY",
                f"book {book_id!r} is {book.status!r}: paragraph classification has not finished",
                status_code=409,
                details={
                    "book_id": book_id,
                    "status": book.status,
                    "author_action": {
                        "action": "wait_or_resume_classification",
                        "view": "styleref",
                        "label": "等这本书的段落分类完成（或在「参考书活动」里继续分类）后再抽取",
                    },
                },
            )
        # 僵尸 run 回收:同书遗留的超时 RUNNING run 降级 FAILED
        self._reap_stale_runs(book_id)
        # 并发守卫:同书已有活跃 run 时拒绝再启(两个后台线程并发抽同一本书
        # 会互撞 SQLite 写锁,前端连点「重跑抽取」即触发)
        active = self.repo.list_runs(book_id=book_id, status=RunStatus.RUNNING.value)
        if active:
            raise DomainError(
                "STYLE_REFERENCE_RUN_ALREADY_ACTIVE",
                f"这本书已有正在进行的抽取 run({active[0].run_id}),请等它完成或先取消",
                status_code=409,
            )

        layers = layers or [Layer.LANGUAGE, Layer.NARRATIVE, Layer.SCENE, Layer.THEME]
        unknown = [layer for layer in layers if layer not in _LAYER_EXTRACTOR_MAP]
        if unknown:
            raise DomainError(
                "STYLE_REFERENCE_LAYER_NOT_SUPPORTED",
                f"unsupported style-reference layer(s): {unknown!r}; "
                f"supported = {[layer.value for layer in _LAYER_EXTRACTOR_MAP]}",
                status_code=400,
            )

        # §6.4 — 输入量门槛执行:skip 层剔除,不消耗 LLM 调用
        assessment = (book.stats_json or {}).get("input_assessment") or {}
        skipped_layers: list[str] = []
        if assessment and not force:
            kept = [layer for layer in layers if assessment.get(layer.value) != "skip"]
            skipped_layers = [layer.value for layer in layers if layer not in kept]
            if not kept:
                raise DomainError(
                    "STYLE_REFERENCE_INPUT_TOO_SMALL",
                    f"book {book_id!r} 的输入量不足:所请求层 "
                    f"{[layer.value for layer in layers]} 均被评估为 skip"
                    "(见 input_thresholds.yaml);请补足语料后重新导入,"
                    "或以 force=true 强制抽取(样本饥饿风险自负)",
                    status_code=409,
                    details={
                        "book_id": book_id,
                        "input_assessment": assessment,
                        "total_chars": int(getattr(book, "total_chars", 0) or 0),
                    },
                )
            layers = kept

        run_id = f"sr_run_{uuid.uuid4().hex[:12]}"
        coverage_json: dict[str, Any] = {"progress": _initial_progress(layers)}
        if skipped_layers:
            coverage_json["skipped_layers"] = skipped_layers
        self.repo.create_run(
            run_id=run_id,
            book_id=book_id,
            status=RunStatus.RUNNING.value,
            phase=RunPhase.EXTRACT.value,
            dispatch_state="queued" if background else "running",
            requested_layers_json=[layer.value for layer in layers],
            coverage_json=coverage_json,
            heartbeat_at=_utcnow_iso(),
            retryable=False,
            started_at=_utcnow_iso(),
        )

        if not background:
            return self._execute(run_id, book_id, layers, progress_commits=False)

        # 后台模式:先把 run 行落盘,worker 用独立 session 接管
        # Direct service callers retain the historical commit+dispatch
        # behaviour. HTTP mutation boundaries pass defer_dispatch=True so the
        # idempotency transaction commits the run and response atomically, then
        # submits the CAS-protected worker from an after-commit callback.
        if not defer_dispatch:
            self.session.commit()
            start_style_reference_run_worker(
                run_id=run_id,
                book_id=book_id,
                layer_values=[layer.value for layer in layers],
                llm_client=self._llm_client,
                retry_policy=self._retry_policy,
            )
        return RunResult(
            run_id=run_id,
            book_id=book_id,
            status=RunStatus.RUNNING.value,
            layers=[layer.value for layer in layers],
            sub_dim_results=[],
        )

    def resume_extract_run(self, run_id: str) -> RunResult:
        """接续已中断的同步抽取 run，只重跑没有最终持久化标记的 sub_dim。"""
        if not self._llm_enabled or self._llm_client is None:
            raise LLMRequiredError(operation="resume_extract_run")
        run = self.repo.get_run(run_id)
        if run is None:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_FOUND",
                f"run {run_id!r} not found",
                status_code=404,
            )
        if run.status == RunStatus.DONE.value:
            layers = self._requested_layers(run)
            return RunResult(
                run_id=run_id,
                book_id=run.book_id,
                status=RunStatus.DONE.value,
                layers=[layer.value for layer in layers],
                sub_dim_results=[],
            )
        if run.status not in {RunStatus.RUNNING.value, RunStatus.FAILED.value}:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_RESUMABLE",
                f"run {run_id!r} status {run.status!r} cannot be resumed",
                status_code=409,
            )
        book = self.repo.get_book(run.book_id)
        if book is None:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {run.book_id!r} not found",
                status_code=404,
            )
        ensure_cloud_llm_allowed(book, operation="resume_extract_run")
        layers = self._requested_layers(run)
        completed = self._completed_sub_dimensions(run_id)
        self.repo.update_run(
            run_id,
            status=RunStatus.RUNNING.value,
            phase=RunPhase.EXTRACT.value,
            dispatch_state="running",
            heartbeat_at=_utcnow_iso(),
            finished_at=None,
            error_code=None,
            error_text=None,
            retryable=False,
        )
        return self._execute(
            run_id,
            run.book_id,
            layers,
            progress_commits=False,
            completed_sub_dimensions=completed,
        )

    def _execute(
        self,
        run_id: str,
        book_id: str,
        layers: list[Layer],
        *,
        progress_commits: bool,
        completed_sub_dimensions: set[str] | None = None,
    ) -> RunResult:
        """逐层执行抽取并更新 run 状态。

        ``progress_commits=True``(后台模式)时每层完成后 commit 进度,并在
        层边界协作响应 cancel;inline 模式不做中间 commit(整请求单事务,
        失败可整体回滚)。
        """
        sub_dim_results: list[ExtractionRunResult] = []
        rng = self._run_rng(run_id, book_id)
        requested_sub_dims = {
            sub_dim.value for layer in layers for sub_dim in LAYER_TO_SUB_DIMS[layer]
        }
        already_done = len(requested_sub_dims & set(completed_sub_dimensions or set()))
        tracker = _RunProgressTracker(
            self.repo,
            self.session,
            run_id,
            layers,
            commit=progress_commits,
            completed=already_done,
        )
        try:
            for i, layer in enumerate(layers):
                if progress_commits:
                    observed = self._observed_status(run_id)
                    if observed != RunStatus.RUNNING.value:
                        # run 已被外部置为终态:CANCELLED(用户取消)补 finished_at;
                        # FAILED(运行心跳超时被 _reap_stale_runs 回收)等其它终态
                        # **不得复活**——此前僵尸回收后排队 worker 开跑会把 FAILED
                        # 拉回 RUNNING→DONE,并与同书新 run 并发互撞。
                        final = observed or RunStatus.CANCELLED.value
                        if final == RunStatus.CANCELLED.value:
                            self.repo.update_run(
                                run_id,
                                status=RunStatus.CANCELLED.value,
                                dispatch_state="cancelled",
                                heartbeat_at=_utcnow_iso(),
                                finished_at=_utcnow_iso(),
                            )
                            self.session.commit()
                        else:
                            logger.warning(
                                "run %s already in terminal state %s; worker exits without resuming",
                                run_id, final,
                            )
                        return RunResult(
                            run_id=run_id,
                            book_id=book_id,
                            status=final,
                            layers=[la.value for la in layers],
                            sub_dim_results=sub_dim_results,
                        )
                tracker.layer_started(layer, i)
                extractor_cls = _LAYER_EXTRACTOR_MAP[layer]
                extractor = extractor_cls(
                    self.session,
                    self._llm_client,
                    run_id=run_id,
                    book_id=book_id,
                    retry_policy=self._retry_policy,
                    rng=rng,
                    # 后台模式每 sub_dim commit:不让写事务跨分钟级 LLM 调用持锁
                    # (否则并发 UI 写操作等满 busy_timeout 报 database is busy)
                    checkpoint=(
                        (lambda: self._checkpoint_background_run(run_id))
                        if progress_commits
                        else None
                    ),
                    progress=tracker,
                )
                skip = {
                    sub_dim
                    for sub_dim in extractor.sub_dimensions
                    if sub_dim.value in (completed_sub_dimensions or set())
                }
                sub_dim_results.extend(
                    extractor.extract_all_sub_dimensions(
                        skip_sub_dimensions=skip,
                    )
                )
        except Exception:
            self.repo.update_run(
                run_id,
                status=RunStatus.FAILED.value,
                dispatch_state="failed",
                heartbeat_at=_utcnow_iso(),
                finished_at=_utcnow_iso(),
                error_code="STYLE_REFERENCE_EXTRACTION_FAILED",
                error_text="style reference extraction failed; start a new run to retry",
                retryable=True,
            )
            if progress_commits:
                self.session.commit()
            raise

        tracker.layers_finished(layers)
        run = self.repo.get_run(run_id)
        coverage = dict(run.coverage_json or {}) if run is not None else {}
        coverage["progress"] = dict(tracker.progress)
        coverage["sub_dimensions"] = self._persisted_subdimension_coverage(run_id)
        self.repo.update_run(
            run_id,
            status=RunStatus.DONE.value,
            phase=RunPhase.DONE.value,
            dispatch_state="completed",
            heartbeat_at=_utcnow_iso(),
            finished_at=_utcnow_iso(),
            error_code=None,
            error_text=None,
            retryable=False,
            coverage_json=coverage,
        )
        if progress_commits:
            self.session.commit()

        return RunResult(
            run_id=run_id,
            book_id=book_id,
            status=RunStatus.DONE.value,
            layers=[layer.value for layer in layers],
            sub_dim_results=sub_dim_results,
        )

    def _run_rng(self, run_id: str, book_id: str) -> random.Random:
        """本 run 的采样 RNG:显式注入优先,否则 sha256(text_checksum + run_id) 定种。"""
        if self._rng is not None:
            return self._rng
        book = self.repo.get_book(book_id)
        return derive_extraction_rng(getattr(book, "text_checksum", None), run_id)

    @staticmethod
    def _requested_layers(run: StyleReferenceRun) -> list[Layer]:
        raw = run.requested_layers_json or []
        try:
            layers = [Layer(str(value)) for value in raw]
        except ValueError as exc:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_RESUMABLE",
                f"run {run.run_id!r} has invalid requested layers",
                status_code=409,
            ) from exc
        if not layers:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_RESUMABLE",
                f"run {run.run_id!r} has no requested layers",
                status_code=409,
            )
        return layers

    def _completed_sub_dimensions(self, run_id: str) -> set[str]:
        completed: set[str] = set()
        for extraction in self.repo.list_extractions(run_id=run_id):
            payload = extraction.raw_payload_json or {}
            if (
                extraction.status == "done"
                and isinstance(payload, dict)
                and "findings_count" in payload
            ):
                completed.add(str(extraction.sub_dimension))
        return completed

    def _persisted_subdimension_coverage(
        self,
        run_id: str,
    ) -> dict[str, dict[str, int]]:
        extraction_counts: dict[str, int] = {}
        for extraction in self.repo.list_extractions(run_id=run_id):
            key = str(extraction.sub_dimension)
            extraction_counts[key] = extraction_counts.get(key, 0) + 1
        finding_counts: dict[str, int] = {}
        for finding in self.repo.list_findings(run_id=run_id):
            key = str(finding.sub_dimension)
            finding_counts[key] = finding_counts.get(key, 0) + 1
        return {
            key: {
                "findings": finding_counts.get(key, 0),
                "extractions": extraction_counts.get(key, 0),
            }
            for key in sorted(set(extraction_counts) | set(finding_counts))
        }

    def _observed_status(self, run_id: str) -> str | None:
        """读 run 当前状态(跨事务可见);run 行消失返回 None(按 CANCELLED 处理)。"""
        run = self.repo.get_run(run_id)
        if run is None:
            return None
        # 后台 session:refresh 拿到其他事务提交的 cancel / reap
        self.session.refresh(run)
        return run.status

    def _checkpoint_background_run(self, run_id: str) -> None:
        """Commit a sub-dimension and renew its durable heartbeat together."""

        self.repo.update_run(run_id, heartbeat_at=_utcnow_iso())
        self.session.commit()

    def _reap_stale_runs(self, book_id: str) -> int:
        """把同书超时仍 RUNNING 的僵尸 run 降级 FAILED,返回回收数量。

        queued 尚未取得 worker 所有权，不能按运行心跳回收；running（以及
        旧版本没有 dispatch_state 的活动行）超时才表示 worker 已中断。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUN_TIMEOUT_MINUTES)
        reaped = 0
        for run in self.repo.list_runs(book_id=book_id, status=RunStatus.RUNNING.value):
            if run.dispatch_state == "queued":
                continue
            heartbeat = _parse_iso(run.heartbeat_at or run.started_at or run.created_at)
            if heartbeat is None or heartbeat > cutoff:
                continue
            self.repo.update_run(
                run.run_id,
                status=RunStatus.FAILED.value,
                dispatch_state="failed",
                heartbeat_at=_utcnow_iso(),
                finished_at=_utcnow_iso(),
                error_code="STYLE_REFERENCE_RUN_INTERRUPTED",
                error_text="background extraction heartbeat expired; start a new run to retry",
                retryable=True,
                coverage_json={
                    **(run.coverage_json or {}),
                    "failure_reason": "stale_running_reaped",
                    "retryable": True,
                },
            )
            reaped += 1
            logger.warning("reaped stale RUNNING run %s (book %s)", run.run_id, book_id)
        return reaped


def start_style_reference_run_worker(
    *,
    run_id: str,
    book_id: str,
    layer_values: list[str],
    llm_client: Any,
    retry_policy: ExtractionRetryPolicy | None = None,
) -> None:
    """Submit a durable extraction dispatch.

    The worker performs the queued->running CAS, so duplicate submissions from
    concurrent ASGI startup hooks are harmless.
    """

    global _RUN_EXECUTOR
    with _RUN_EXECUTOR_LOCK:
        if _RUN_EXECUTOR is None:
            _RUN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sr_extract")
        _RUN_EXECUTOR.submit(
            _background_run_worker,
            run_id=run_id,
            book_id=book_id,
            layer_values=list(layer_values),
            llm_client=llm_client,
            retry_policy=retry_policy or ExtractionRetryPolicy(),
        )


def shutdown_style_reference_run_executor(*, wait: bool = False) -> None:
    """Stop accepting extraction work and let already submitted work drain."""

    global _RUN_EXECUTOR
    with _RUN_EXECUTOR_LOCK:
        executor = _RUN_EXECUTOR
        _RUN_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=False)


def _background_run_worker(
    *,
    run_id: str,
    book_id: str,
    layer_values: list[str],
    llm_client: Any,
    retry_policy: ExtractionRetryPolicy,
) -> None:
    """后台抽取入口:独立 session 执行 _execute(progress_commits=True)。

    _execute 自身已在异常路径把 run 标 FAILED 并 commit;此处兜底捕获
    (含 session 构造失败),保证线程不带异常退出。
    """
    from novel_system.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            claimed = session.execute(
                update(StyleReferenceRun)
                .where(
                    StyleReferenceRun.run_id == run_id,
                    StyleReferenceRun.status == RunStatus.RUNNING.value,
                    StyleReferenceRun.dispatch_state == "queued",
                )
                .values(
                    dispatch_state="running",
                    heartbeat_at=_utcnow_iso(),
                    error_code=None,
                    error_text=None,
                    retryable=False,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.rollback()
                logger.info("background extract run %s dispatch was already claimed", run_id)
                return
            session.commit()
            orch = RunOrchestrator(
                session,
                llm_client=llm_client,
                llm_enabled=True,
                retry_policy=retry_policy,
            )
            book = orch.repo.get_book(book_id)
            if book is None:
                raise DomainError(
                    "STYLE_REFERENCE_BOOK_NOT_FOUND",
                    "style reference book disappeared before extraction started",
                    status_code=404,
                )
            # Re-check at dispatch time: an operator may tighten the book's
            # cloud policy while it is still queued after the HTTP response.
            ensure_cloud_llm_allowed(book, operation="start_extract_run")
            with periodic_heartbeat(
                lambda: _renew_background_run_heartbeat(run_id),
                interval_seconds=RUN_HEARTBEAT_INTERVAL_SECONDS,
                thread_name=f"sr_extract_heartbeat:{run_id}",
            ):
                orch._execute(
                    run_id,
                    book_id,
                    [Layer(value) for value in layer_values],
                    progress_commits=True,
                )
    except Exception:  # pylint: disable=broad-except
        logger.exception("background extract run %s failed", run_id)
        _mark_background_run_failed(run_id)


def _mark_background_run_failed(run_id: str) -> None:
    """Close failures that happen outside ``_execute`` (including setup)."""

    from novel_system.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            changed = session.execute(
                update(StyleReferenceRun)
                .where(
                    StyleReferenceRun.run_id == run_id,
                    StyleReferenceRun.status == RunStatus.RUNNING.value,
                    StyleReferenceRun.dispatch_state == "running",
                )
                .values(
                    status=RunStatus.FAILED.value,
                    dispatch_state="failed",
                    heartbeat_at=_utcnow_iso(),
                    finished_at=_utcnow_iso(),
                    error_code="STYLE_REFERENCE_EXTRACTION_FAILED",
                    error_text="style reference extraction failed; start a new run to retry",
                    retryable=True,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount:
                session.commit()
            else:
                session.rollback()
    except Exception:  # pragma: no cover - final worker boundary
        logger.exception("failed to persist extraction worker failure for %s", run_id)


def _renew_background_run_heartbeat(run_id: str) -> None:
    """Renew a running worker lease using an independent short transaction."""

    from novel_system.db.session import SessionLocal

    with SessionLocal() as session:
        touched = session.execute(
            update(StyleReferenceRun)
            .where(
                StyleReferenceRun.run_id == run_id,
                StyleReferenceRun.status == RunStatus.RUNNING.value,
                StyleReferenceRun.dispatch_state == "running",
            )
            .values(heartbeat_at=_utcnow_iso())
            .execution_options(synchronize_session=False)
        )
        if touched.rowcount == 1:
            session.commit()
        else:
            session.rollback()


def _utcnow_iso() -> str:
    from novel_system.db.models import utcnow

    return utcnow()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

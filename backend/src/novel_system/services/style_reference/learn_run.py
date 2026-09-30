"""「学习文风」作业的处理器（kind=learn；从 ``learn_job`` 拆出，``workers.install_workers`` 登记）。

取代旧的学习链路（16 次碎片抽取的 ``RunOrchestrator`` + 同步的 ``/runs/{id}/synthesize`` + 合成里建 RAG +
合成后往待办塞「是否应用到本项目」卡）。一本书一次学习走七步，每步之间 ``check_continue``（取消 / 丢了所有权即停），
每步结束把游标写进作业行（条件写，owner_token 仍是自己才算数）——重启、``--reload``、手动「继续学习」都从游标续：

1. ``windows``   整理全书样例窗口（``windows.ensure_window_index``，已是最新就直接读）；记下根哈希 / 类型版本 /
                 索引与测量核版本（定稿写进 ``learned_from``）；
2. ``select``    挑抽取窗口集（``learn_select``：约 12 窗 / 4 万字，按书的校验和定种，确定性）；
3. ``extract``   四层各一次调用读同一组窗口（``learn_extract``：逐字核对证据，重试一次并合并两次的有效发现）；
                 结果落抽取 / 发现 / 引文 / 证据表（挂在一行只作血缘的 run 上）；最多 3 层并行；
4. ``synthesize`` 一次调用写文风卡（``learn_card``：无依据 / 带统计数字的行丢掉，对账去矛盾）；卡暂存在游标里；
5. ``protected`` 受保护专名（``protected_terms``：统计候选 → 模型确认 → 必须在原书里原样出现）；暂存游标；
6. ``tags``      给窗口打场面 / 情绪 / 维度标签（``learn_tags``：每批 ≤8 窗，最多 3 批并行，每批退避重试两次）。
                 标签不依赖文风卡（2026-09-24 §8 O1）：只给 ``tags_version`` 还不是当前版本（或还没有标签）的窗口打，
                 重新学习不再给全书重打；``params.retag`` 强制全打；每批写完即记游标，重启只补没做完的批；
7. ``finalize``  专名与原文重合过滤卡片、沿用作者的 ✓ / ✗、写画像（v3 键）与受保护专名行、结果（计数 / 调用 /
                 token / 耗时）——一个事务：先条件写作业行拿写锁，再重读画像合并行状态（拼装与写库在 ``learn_finalize``）。

每次调用前按节点的实际路由查书的云策略。LLM 客户端在作业开始时按当前配置取（:func:`resolve_learn_client`，测试在
这里打桩；调用的退避 / 等待节拍同样在本模块，测试可调）。处理器的脚手架（检查点、并行调用循环、终态收尾）在
``job_runtime.JobRun``，与分类 / 对照检查共用。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    LlmCall,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    utcnow,
)
from novel_system.services.style_reference.book_text import non_body_kind
from novel_system.services.style_reference.card import DIMENSION_CARD_VERSION
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.fidelity import reference_distribution_for_book
from novel_system.services.style_reference.job_runtime import (
    JobRun,
    retry_attempts,
)
from novel_system.services.style_reference.jobs import (
    ClaimedJob,
    DaemonCallPool,
    JobLost,
    StyleJobService,
)
from novel_system.services.style_reference.learn_card import (
    MIN_CARD_LINES,
    CardAssembly,
    assemble_card,
    fit_synthesis_payload,
    json_clean,
    load_dimension_meta,
    load_run_findings,
    reconcile_card,
    sub_dimension_summary,
    synthesis_payload,
)
from novel_system.services.style_reference.learn_extract import (
    POSITION_LABELS,
    ExtractionSet,
    LayerParse,
    build_extraction_set,
    layer_payload,
    merge_layer_parses,
    needs_retry,
    parse_layer_output,
    persist_layer,
    retry_instruction,
    shrink_to_fit,
)
from novel_system.services.style_reference.learn_finalize import (
    FinalizeInputs,
    filter_learned_card,
    learn_result_summary,
    planning_lines,
    write_learned_profile,
)
from novel_system.services.style_reference.learn_job import (
    CURSOR_VERSION,
    LAYER_LABELS,
    PHASE_EXTRACT,
    PHASE_FINALIZE,
    PHASE_LABELS,
    PHASE_ORDER,
    PHASE_PROTECTED,
    PHASE_SELECT,
    PHASE_SYNTHESIZE,
    PHASE_TAGS,
    PHASE_WINDOWS,
    REASON_EXTRACT_FAILED,
    REASON_INPUT_TOO_SMALL,
    REASON_NO_FINDINGS,
    REASON_PROTECTED_FAILED,
    REASON_SYNTHESIZE_FAILED,
    REASON_TAGGING_FAILED,
    RUN_DISPATCH_STATE,
    LearnFailedError,
    ensure_tag_template_current,
    set_run_status,
    tag_plan_input,
    windows_needing_tags,
)
from novel_system.services.style_reference.learn_llm import (
    EXTRACT_NODES,
    LAYERS,
    NODE_PROTECTED_TERMS,
    NODE_SYNTHESIZE,
    NODE_TAG_WINDOWS,
    LearnCallError,
    call_structured,
    load_learn_runtimes,
    payload_fits,
)
from novel_system.services.style_reference.learn_select import STRATUM_LABELS, select_extraction_windows
from novel_system.services.style_reference.learn_tags import (
    TagBatchMismatch,
    clip_window_text,
    parse_tag_output,
    plan_tag_batches,
    tag_payload,
)
from novel_system.services.style_reference.llm_nodes import NodeRuntime
from novel_system.services.style_reference.measure import KERNEL_VERSION
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.protected_terms import (
    ProtectedTerm,
    dismissed_protected_terms,
    parse_protected_terms,
    proper_noun_candidates,
)
from novel_system.services.style_reference.schemas import FindingKind
from novel_system.services.style_reference.structure_card import compute_structure_card
from novel_system.services.style_reference.structure_render import render_structure_card_parts
from novel_system.services.style_reference.tags import TAGS_VERSION
from novel_system.services.style_reference.voice_signature import (
    VOICE_SIGNATURE_VERSION,
    compute_voice_signature,
    render_voice_habits,
)
from novel_system.services.style_reference.windows import (
    WINDOW_INDEX_VERSION,
    ensure_window_index,
    index_marker,
    load_windows,
    window_texts,
)

logger = logging.getLogger(__name__)

# 并行与重试（测试把退避 / 等待调成 0）
PARALLEL_CALLS = 3
EXTRACT_ATTEMPTS = 2  # 1 次 + 1 次带问题清单的重试(两次的有效发现合并)
SYNTH_ATTEMPTS = 2
PROTECTED_ATTEMPTS = 2
TAG_ATTEMPTS = 3  # 1 次 + 2 次退避重试
CALL_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0)
WAIT_POLL_SECONDS = 2.0


def resolve_learn_client() -> tuple[Any | None, bool]:
    """作业开始时按**当前**运行时配置取 LLM 客户端（不在请求里捕获）；测试在这里打桩。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


# ---------------------------------------------------------------- the handler


@dataclass
class _LayerOutcome:
    layer: str
    parse: LayerParse
    attempts: int
    llm_call_id: str | None
    dropped_windows: list[int]


def set_run_status(session: Session, run_id: str | None, status: str) -> None:
    if not run_id:
        return
    session.execute(
        update(StyleReferenceRun)
        .where(StyleReferenceRun.run_id == str(run_id), StyleReferenceRun.status == "running")
        .values(status=status, dispatch_state=RUN_DISPATCH_STATE, finished_at=utcnow(), heartbeat_at=utcnow())
        .execution_options(synchronize_session=False)
    )


class _LearnRun(JobRun):
    operation = "learn_style"

    def __init__(self, session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
        super().__init__(session, claimed, service)
        self.params = dict(claimed.params or {})
        self.cursor: dict[str, Any] = dict(claimed.cursor or {})
        self.runtimes: dict[str, NodeRuntime] = {}
        self.book_title = ""
        # 这本书的段落（序号、类型、正文、id）与去掉章题 / 副文本后的正文：一次学习各读一遍、算一遍（声音签名、
        # 结构卡、专名候选、定稿的原文重合过滤都用它）。学习与分类在同一本书上互斥，段落不会在学习中途变。
        self._paragraph_rows_cache: list[tuple[Any, Any, Any, Any]] | None = None
        self._prose_texts_cache: list[str] | None = None

    # ---- lifecycle (终态的附带写:血缘 run 行) -----------------------------
    def finish_cancelled(self) -> None:
        self._finish(cancelled=True)

    def finish_failed(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # 失败详情总带 book_id（作者动作 resume_learning / review_book 也带）：前端按它把「继续学习」/「仍然学习」/「查看这本书」做成按钮
        payload = dict(details or {})
        payload.setdefault("book_id", self.book_id)
        action = payload.get("author_action")
        if isinstance(action, Mapping):
            payload["author_action"] = {**dict(action), "book_id": dict(action).get("book_id") or self.book_id}
        self._finish(code=code, message=message, retryable=retryable, details=payload)

    def _finish(
        self,
        *,
        cancelled: bool = False,
        code: str = "",
        message: str = "",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.session.rollback()
        if cancelled:
            ok = self.service.finish_cancelled(self.claimed)
        else:
            ok = self.service.fail(self.claimed, code=code, message=message, retryable=retryable, details=details)
        if not ok:
            self.session.rollback()
            return
        set_run_status(self.session, self.cursor.get("run_id"), "cancelled" if cancelled else "failed")
        self.session.commit()

    def _node_routes(self, node_ids: Sequence[str]) -> dict[str, Any]:
        return {node_id: self.runtimes[node_id].route for node_id in node_ids}

    def _save(self, *, progress: Mapping[str, Any] | None = None, write: Callable[[], None] | None = None) -> None:
        """游标（条件写，本事务第一条写）→ 调用方的写 → 进度，一次提交（``JobRun.save_cursor``）。"""
        self.save_cursor(self.cursor, progress=progress, write=write)

    def _done(self, phase: str) -> bool:
        return phase in (self.cursor.get("phases_done") or [])

    def _mark_done(
        self,
        phase: str,
        began: float,
        *,
        write: Callable[[], None] | None = None,
        detail: str | None = None,
        calls: int = 0,
        step: bool = True,
    ) -> None:
        self._record_done(phase, began, step=step)
        self._save(progress=self._progress_values(phase, detail=detail, calls=calls), write=write)

    def _record_done(self, phase: str, began: float, *, step: bool = True) -> None:
        """记一步完成(``step=False``:这一步的进度已按层 / 按批逐个记过)。"""
        phases = list(self.cursor.get("phases_done") or [])
        if phase not in phases:
            phases.append(phase)
        self.cursor["phases_done"] = phases
        timings = dict(self.cursor.get("timings") or {})
        timings[phase] = round(float(timings.get(phase) or 0.0) + (time.monotonic() - began), 2)
        self.cursor["timings"] = timings
        if step:
            self.cursor["steps_done"] = int(self.cursor.get("steps_done") or 0) + 1

    def _steps_total(self) -> int:
        layers = self.cursor.get("layers") or list(LAYERS)
        batches = len(((self.cursor.get("tags") or {}).get("batches") or [])) or int(
            (self.cursor.get("index") or {}).get("tag_batches") or 0
        )
        return 2 + len(layers) + 1 + 1 + batches + 1

    def _progress_values(self, phase: str, *, detail: str | None = None, calls: int = 0) -> dict[str, Any]:
        return {
            "phase": phase,
            "phase_label": f"学习文风 · {PHASE_LABELS.get(phase, phase)}",
            "done": int(self.cursor.get("steps_done") or 0),
            "total": self._steps_total(),
            "detail": detail,
            "llm_calls_delta": int(calls),
            "extra": {"retries": int(self.cursor.get("retries") or 0), "attempt": self.claimed.attempt},
        }

    def _enter(self, phase: str, detail: str | None = None) -> None:
        values = self._progress_values(phase, detail=detail)
        extra = dict(values.pop("extra"))
        extra.update({"phase_started_at": utcnow(), "phase_done_at_start": int(self.cursor.get("steps_done") or 0)})
        self.fresh()
        self.checkpoint(extra=extra, **values)

    def _count_call(self, attempts: int) -> None:
        self.cursor["llm_calls"] = int(self.cursor.get("llm_calls") or 0) + int(attempts)
        self.cursor["retries"] = int(self.cursor.get("retries") or 0) + max(0, int(attempts) - 1)

    # ---- main -----------------------------------------------------------
    def _run(self) -> None:
        self.check_continue()
        book = self.book()
        self.book_title = str(book.title or "")
        if int(self.cursor.get("version") or 0) != CURSOR_VERSION:
            self.cursor = {"version": CURSOR_VERSION, "phases_done": [], "started_at": utcnow()}
        client, enabled = resolve_learn_client()
        if not enabled or client is None:
            raise LLMRequiredError(operation="learn_style")
        self.client = client
        self.runtimes = load_learn_runtimes()
        ensure_tag_template_current(self.runtimes)
        ensure_cloud_llm_allowed(
            book,
            operation="learn_style",
            routes={node_id: rt.route for node_id, rt in self.runtimes.items()},
            llm_client=self.client,
        )
        skipped = [str(layer) for layer in self.params.get("skipped_layers") or []]
        self.cursor.setdefault("layers", [layer for layer in LAYERS if layer not in skipped])
        run_id = self.cursor.get("run_id")

        def reopen_run() -> None:
            # 续跑:血缘 run 行回到 running(失败 / 取消时它被标成了终态)
            if run_id:
                self.session.execute(
                    update(StyleReferenceRun)
                    .where(StyleReferenceRun.run_id == str(run_id))
                    .values(status="running", finished_at=None, heartbeat_at=utcnow())
                    .execution_options(synchronize_session=False)
                )

        self._save(progress=self._progress_values(self._current_phase()), write=reopen_run)

        if not self._done(PHASE_WINDOWS):
            self._phase_windows()
        if not self._done(PHASE_SELECT):
            self._phase_select()
        if not self._done(PHASE_EXTRACT):
            self._phase_extract()
        if not self._done(PHASE_SYNTHESIZE):
            self._phase_synthesize()
        if not self._done(PHASE_PROTECTED):
            self._phase_protected()
        if not self._done(PHASE_TAGS):
            self._phase_tags()
        self._phase_finalize()

    def _current_phase(self) -> str:
        return next((phase for phase in PHASE_ORDER if not self._done(phase)), PHASE_FINALIZE)

    # ---- a. windows -------------------------------------------------------
    def _phase_windows(self) -> None:
        began = time.monotonic()
        self._enter(PHASE_WINDOWS)
        windows = ensure_window_index(self.session, self.book_id, commit=True)
        if not windows:
            raise LearnFailedError(
                REASON_INPUT_TOO_SMALL,
                "这本书切不出一个样例窗口(正文太少或全是章题 / 分隔行),学不出文风。",
                retryable=False,
                details={"book_id": self.book_id},
            )
        book = self.book()
        marker = index_marker(book.stats_json) or {}
        stats = dict(book.stats_json or {})
        self.cursor["index"] = {
            "root": str(windows[0].root_sha256),
            "index_version": WINDOW_INDEX_VERSION,
            "kernel_version": KERNEL_VERSION,
            "types_revision": int(stats.get("paragraph_types_revision") or 0),
            "window_count": len(windows),
            "marker_root": marker.get("root"),
            "tag_batches": len(plan_tag_batches(tag_plan_input(windows_needing_tags(windows, retag=self._retag())))),
        }
        self._mark_done(PHASE_WINDOWS, began, detail=f"{len(windows)} 个窗口")

    def _retag(self) -> bool:
        return bool(self.params.get("retag"))

    # ---- b. select --------------------------------------------------------
    def _phase_select(self) -> None:
        began = time.monotonic()
        self._enter(PHASE_SELECT)
        book = self.book()
        rows = load_windows(self.session, self.book_id)
        if not rows:
            rows = ensure_window_index(self.session, self.book_id, commit=True)
        selection = select_extraction_windows(rows, seed=str(book.text_checksum or self.book_id))
        if not selection.windows:
            raise LearnFailedError(
                REASON_INPUT_TOO_SMALL,
                "挑不出可以学习的窗口。",
                retryable=False,
                details={"book_id": self.book_id},
            )
        self.cursor["selection"] = selection.to_cursor()
        strata = [STRATUM_LABELS.get(str(item["stratum"]), str(item["stratum"])) for item in self.cursor["selection"]["windows"]]
        self._mark_done(
            PHASE_SELECT,
            began,
            detail=f"{len(selection.windows)} 窗 / {selection.chars} 字(" + "、".join(dict.fromkeys(strata)) + ")",
        )

    # ---- c. extract -------------------------------------------------------
    def _ensure_run(self) -> str:
        run_id = self.cursor.get("run_id")
        if run_id:
            return str(run_id)
        run_id = f"sr_run_{uuid.uuid4().hex[:12]}"
        self.cursor["run_id"] = run_id
        selection = dict(self.cursor.get("selection") or {})

        def write() -> None:
            self.session.add(
                StyleReferenceRun(
                    run_id=run_id,
                    book_id=self.book_id,
                    status="running",
                    phase="extract",
                    dispatch_state=RUN_DISPATCH_STATE,
                    requested_layers_json=list(self.cursor.get("layers") or LAYERS),
                    coverage_json={
                        "learn_job_id": self.claimed.job_id,
                        "extraction_windows": [item["window_no"] for item in selection.get("windows") or []],
                        "extraction_chars": int(selection.get("chars") or 0),
                    },
                    heartbeat_at=utcnow(),
                    retryable=False,
                    started_at=utcnow(),
                )
            )
            self.session.flush()

        self._save(write=write)
        return run_id

    def _extraction_set(self, layers: Sequence[str]) -> ExtractionSet:
        ext_set = build_extraction_set(self.session, self.book_id, (self.cursor.get("selection") or {}).get("windows") or [])

        def fits(candidate: ExtractionSet) -> bool:
            return all(
                payload_fits(self.runtimes[EXTRACT_NODES[layer]], layer_payload(candidate, layer, book_title=self.book_title))
                for layer in layers
            )

        fitted, dropped = shrink_to_fit(ext_set, fits)
        if dropped:
            extract = dict(self.cursor.get("extract") or {})
            extract["dropped_windows"] = sorted(set(extract.get("dropped_windows") or []) | set(dropped))
            self.cursor["extract"] = extract
        return fitted

    def _phase_extract(self) -> None:
        began = time.monotonic()
        run_id = self._ensure_run()
        layers = [layer for layer in self.cursor.get("layers") or LAYERS]
        done = dict(self.cursor.get("layers_done") or {})
        pending = [layer for layer in layers if layer not in done]
        self._enter(PHASE_EXTRACT, detail=f"第 {len(done) + 1}/{len(layers)} 层")
        if pending:
            ext_set = self._extraction_set(pending)
            if not ext_set.paragraphs:
                raise LearnFailedError(REASON_INPUT_TOO_SMALL, "挑出的窗口里没有正文段。", retryable=False)

            def submit(pool: DaemonCallPool, layer: str, stop: threading.Event) -> Future:
                self.pre_call_check(self._node_routes([EXTRACT_NODES[layer]]))
                return pool.submit(self._extract_layer, layer, ext_set, stop)

            def apply(layer: str, outcome: _LayerOutcome) -> None:
                self._apply_layer(run_id, outcome)

            self.run_parallel(
                pending,
                submit,
                apply,
                max_inflight=PARALLEL_CALLS,
                poll_seconds=WAIT_POLL_SECONDS,
                thread_prefix="sr_learn_extract",
            )
        total = sum(
            int(counts.get("observations", 0)) + int(counts.get("avoid", 0))
            for layer_state in (self.cursor.get("layers_done") or {}).values()
            for counts in (layer_state.get("counts") or {}).values()
        )
        if total == 0:
            raise LearnFailedError(
                REASON_NO_FINDINGS,
                "四层都没有一条通过证据核对的发现(引文必须逐字取自原文);检查模型后「继续学习」。",
                retryable=True,
            )
        self._mark_done(PHASE_EXTRACT, began, detail=f"{total} 条发现", step=False)

    def _extract_layer(self, layer: str, ext_set: ExtractionSet, stop: threading.Event) -> _LayerOutcome:
        """工人线程:一层最多两次调用;第二次带问题清单,两次的有效发现合并(台账 E5)。"""
        runtime = self.runtimes[EXTRACT_NODES[layer]]
        payload = layer_payload(ext_set, layer, book_title=self.book_title)
        valid: LayerParse | None = None
        attempts = 0
        last_call: str | None = None
        errors: list[str] = []
        for attempt in retry_attempts(EXTRACT_ATTEMPTS, (), stop):
            try:
                result = call_structured(
                    runtime,
                    payload,
                    self.client,
                    scope_id=self.book_id,
                    step=f"learn:{self.claimed.job_id}:extract:{layer}:{attempt + 1}",
                    extra_instruction=retry_instruction(valid) if valid is not None else None,
                )
            except LearnCallError as exc:
                attempts += 1
                errors.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:300]}")
                continue
            attempts += 1
            last_call = result.llm_call_id
            parse = parse_layer_output(result.structured, layer, ext_set)
            valid = parse if valid is None else merge_layer_parses(valid, parse)
            if not needs_retry(valid):
                break
        if valid is None:
            raise LearnFailedError(
                REASON_EXTRACT_FAILED,
                f"{LAYER_LABELS.get(layer, layer)}的抽取调用失败:{errors[-1] if errors else ''}",
                retryable=True,
                details={"layer": layer, "node_id": runtime.node_id, "attempts": attempts, "problems": errors},
            )
        return _LayerOutcome(layer=layer, parse=valid, attempts=attempts, llm_call_id=last_call, dropped_windows=[])

    def _apply_layer(self, run_id: str, outcome: _LayerOutcome) -> None:
        """作业线程:游标(条件写) → 四张表 → 进度,一次提交。"""
        counts_holder: dict[str, Any] = {}
        done = dict(self.cursor.get("layers_done") or {})
        done[outcome.layer] = {
            "attempts": outcome.attempts,
            "returned": outcome.parse.returned,
            "rejected": outcome.parse.rejected,
            "counts": outcome.parse.counts(),
            "problems": outcome.parse.problems[:8],
        }
        self.cursor["layers_done"] = done
        self._count_call(outcome.attempts)
        self.cursor["steps_done"] = int(self.cursor.get("steps_done") or 0) + 1

        def write() -> None:
            counts_holder["counts"] = persist_layer(
                self.session,
                book_id=self.book_id,
                run_id=run_id,
                parse=outcome.parse,
                attempts=outcome.attempts,
                llm_call_id=outcome.llm_call_id,
            )

        layers = self.cursor.get("layers") or LAYERS
        self._save(
            write=write,
            progress=self._progress_values(
                PHASE_EXTRACT,
                detail=f"{LAYER_LABELS.get(outcome.layer, outcome.layer)}完成(第 {len(done)}/{len(layers)} 层)",
                calls=outcome.attempts,
            ),
        )

    # ---- d. synthesize ----------------------------------------------------
    def _voice(self) -> dict[str, Any]:
        cached = self.cursor.get("voice")
        if isinstance(cached, Mapping) and cached.get("features"):
            return dict(cached)
        book = self.book()
        stats = dict(book.stats_json or {})
        signature = stats.get("voice_signature")
        if not (
            isinstance(signature, Mapping)
            and signature.get("version") == VOICE_SIGNATURE_VERSION
            and signature.get("kernel_version") == KERNEL_VERSION
            and isinstance(signature.get("features"), Mapping)
        ):
            signature = compute_voice_signature(self._body_texts())
        signature = json_clean(dict(signature))
        habits = [line for line in render_voice_habits(signature) if line]
        voice = {**signature, "habits": habits}
        self.cursor["voice"] = voice
        return voice

    def _paragraph_rows(self) -> list[tuple[Any, Any, Any, Any]]:
        """这本书的全部段落 ``(paragraph_index, paragraph_type, text, paragraph_id)``，按序号（一次学习读一遍）。"""
        if self._paragraph_rows_cache is None:
            self._paragraph_rows_cache = [
                tuple(row)
                for row in self.session.execute(
                    select(
                        StyleReferenceParagraph.paragraph_index,
                        StyleReferenceParagraph.paragraph_type,
                        StyleReferenceParagraph.text,
                        StyleReferenceParagraph.paragraph_id,
                    )
                    .where(StyleReferenceParagraph.book_id == self.book_id)
                    .order_by(StyleReferenceParagraph.paragraph_index)
                )
            ]
        return self._paragraph_rows_cache

    def _body_texts(self) -> list[str]:
        return [str(text) for _index, _ptype, text, _pid in self._paragraph_rows() if str(text or "").strip()]

    def _prose_texts(self) -> list[str]:
        """去掉章题 / 书前书后 / 副文本之后的正文段（专名候选、定稿的原文重合过滤用；一次学习算一遍）。"""
        if self._prose_texts_cache is None:
            self._prose_texts_cache = [t for t in self._body_texts() if non_body_kind(t) is None]
        return self._prose_texts_cache

    def _structure_card(self, voice: Mapping[str, Any]) -> dict[str, Any] | None:
        cached = self.cursor.get("structure_card")
        if isinstance(cached, Mapping):
            return dict(cached)
        book = self.book()
        stats = dict(book.stats_json or {})
        rows = [
            {"paragraph_index": index, "paragraph_type": ptype, "text": text, "paragraph_id": pid}
            for index, ptype, text, pid in self._paragraph_rows()
        ]
        breaks = stats.get("scene_breaks")
        try:
            card = compute_structure_card(
                rows,
                voice_signature=voice,
                scene_breaks=[int(i) for i in breaks if isinstance(i, int)] if isinstance(breaks, list) else None,
            )
        except Exception:  # noqa: BLE001 — 结构画像是确定性派生,算不出只让画像缺这个键
            logger.warning("structure card computation failed for book %s", self.book_id, exc_info=True)
            return None
        card = json_clean(card)
        card.pop("chapters", None)
        card.pop("chapters_listed", None)
        self.cursor["structure_card"] = card
        return card

    def _phase_synthesize(self) -> None:
        began = time.monotonic()
        self._enter(PHASE_SYNTHESIZE)
        run_id = str(self.cursor.get("run_id") or "")
        findings = load_run_findings(self.session, run_id)
        meta = load_dimension_meta(self.session, run_id)
        voice = self._voice()
        structure = self._structure_card(voice)
        facts: list[str] = []
        if structure:
            stats_block, _samples = render_structure_card_parts({"structure_card": structure}, include_samples=False)
            facts = [line[2:].strip() for line in stats_block.splitlines() if line.startswith("- ")]
        runtime = self.runtimes[NODE_SYNTHESIZE]
        payload, stage = fit_synthesis_payload(
            lambda **kw: synthesis_payload(
                findings,
                meta,
                book_title=self.book_title,
                voice_habits=voice.get("habits") or [],
                structure_facts=facts,
                **kw,
            ),
            lambda candidate: payload_fits(runtime, candidate),
        )
        best: CardAssembly | None = None
        attempts = 0
        errors: list[str] = []
        extra: str | None = None
        for attempt in range(SYNTH_ATTEMPTS):
            self.pre_call_check(self._node_routes([NODE_SYNTHESIZE]))
            try:
                result = self._call_in_worker(
                    lambda extra=extra, attempt=attempt: call_structured(
                        runtime,
                        payload,
                        self.client,
                        scope_id=self.book_id,
                        step=f"learn:{self.claimed.job_id}:synthesize:{attempt + 1}",
                        extra_instruction=extra,
                    )
                )
            except LearnCallError as exc:
                attempts += 1
                errors.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:300]}")
                continue
            attempts += 1
            assembly = assemble_card(result.structured, findings, meta, generated_at=utcnow())
            card, dropped = reconcile_card(assembly.card, voice_features=voice.get("features") or {})
            assembly.card = card
            assembly.line_count = len(card.all_lines()) if card is not None else 0
            for reason, count in dropped.items():
                assembly.dropped[reason] = assembly.dropped.get(reason, 0) + count
            if best is None or assembly.line_count > best.line_count:
                best = assembly
            if best.line_count >= MIN_CARD_LINES:
                break
            extra = (
                f"【重试说明】上一次的文风卡只有 {assembly.line_count} 条可用的句子"
                f"(丢弃原因:{assembly.dropped or '输出不成形'})。请按要求重写完整的 16 维:每条 do / avoid 都要在 refs 里"
                "写它依据的发现编号(如 f3),不写阿拉伯数字与百分比,每条不超过六十字。"
            )
        self._count_call(attempts)
        if best is None or best.line_count == 0:
            raise LearnFailedError(
                REASON_SYNTHESIZE_FAILED,
                "文风卡没有写出一条可用的句子:" + (errors[-1] if errors else "模型的输出没有通过校验"),
                retryable=True,
                details={"attempts": attempts, "problems": errors[-3:], "dropped": best.dropped if best else {}},
            )
        self.cursor["card"] = best.to_cursor()
        self.cursor["synthesis"] = {"attempts": attempts, "fit_stage": stage, "findings": len(findings)}
        self._mark_done(PHASE_SYNTHESIZE, began, detail=f"{best.line_count} 句", calls=attempts)

    # ---- e. protected terms -----------------------------------------------
    def _phase_protected(self) -> None:
        began = time.monotonic()
        self._enter(PHASE_PROTECTED)
        texts = self._prose_texts()
        corpus = "\n".join(texts)
        candidates = proper_noun_candidates(texts)
        run_id = str(self.cursor.get("run_id") or "")
        avoid_statements = [
            f.statement
            for f in load_run_findings(self.session, run_id)
            if f.kind == FindingKind.FORBIDDEN_PATTERN.value and f.dimension.startswith("theme.")
        ]
        runtime = self.runtimes[NODE_PROTECTED_TERMS]
        items = [c.payload() for c in candidates]

        def build(count: int) -> dict[str, Any]:
            return {"book_title": self.book_title, "candidates": items[:count], "avoid_statements": avoid_statements}

        count = len(items)
        payload = build(count)
        while count > 20 and not payload_fits(runtime, payload):
            count = int(count * 0.8)
            payload = build(count)
        terms: list[ProtectedTerm] | None = None
        attempts = 0
        errors: list[str] = []
        for attempt in range(PROTECTED_ATTEMPTS):
            self.pre_call_check(self._node_routes([NODE_PROTECTED_TERMS]))
            try:
                result = self._call_in_worker(
                    lambda attempt=attempt: call_structured(
                        runtime,
                        payload,
                        self.client,
                        scope_id=self.book_id,
                        step=f"learn:{self.claimed.job_id}:protected:{attempt + 1}",
                    )
                )
            except LearnCallError as exc:
                attempts += 1
                errors.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:300]}")
                continue
            attempts += 1
            if not isinstance(result.structured.get("terms"), list):
                errors.append(f"attempt {attempt + 1}: output has no terms list")
                continue
            terms = parse_protected_terms(result.structured, corpus)
            break
        self._count_call(attempts)
        if terms is None:
            raise LearnFailedError(
                REASON_PROTECTED_FAILED,
                "识别本书专名的调用失败:" + (errors[-1] if errors else ""),
                retryable=True,
                details={"attempts": attempts, "problems": errors[-3:]},
            )
        self.cursor["protected"] = {
            "terms": [t.as_dict() for t in terms],
            "candidates": len(items),
            "sent_candidates": count,
            "attempts": attempts,
        }
        self._mark_done(PHASE_PROTECTED, began, detail=f"{len(terms)} 个专名", calls=attempts)

    # ---- f. tags ----------------------------------------------------------
    def _phase_tags(self) -> None:
        """只给还要打的窗口打标签(标签版本不是 ``TAGS_VERSION`` 的;``params.retag`` 全打)。根哈希变了(索引重建、
        旧标签已丢)的窗口自然没有标签,也在这一批里;打过的窗口重新学习时不再重打——这就是重新学习从 71 次调用降到
        ≈6 次的地方。"""
        began = time.monotonic()
        for _round in range(3):
            rows = ensure_window_index(self.session, self.book_id, commit=True)
            if not rows:
                break
            root = str(rows[0].root_sha256)
            tags_state = dict(self.cursor.get("tags") or {})
            if tags_state.get("root") != root:
                to_tag = windows_needing_tags(rows, retag=self._retag())
                tags_state = {
                    "root": root,
                    "batches": plan_tag_batches(tag_plan_input(to_tag)),
                    "done": [],
                    "tagged": 0,
                    "planned": len(to_tag),
                    "current": len(rows) - len(to_tag),
                }
                self.cursor["tags"] = tags_state
                self._save()
            batches: list[list[int]] = [list(b) for b in tags_state["batches"]]
            done = set(int(i) for i in tags_state.get("done") or [])
            pending = [i for i in range(len(batches)) if i not in done]
            self._enter(
                PHASE_TAGS,
                detail=(
                    f"第 {len(done) + 1}/{len(batches)} 批"
                    if batches
                    else f"{len(rows)} 个窗口的标签都是当前版本,不用重打"
                ),
            )
            by_no = {int(w.window_no): w for w in rows}
            dismissed_tags = self._dismissed_terms(str(self.params.get("profile_id") or self.claimed.profile_id or "") or None)
            protected = [
                t for t in (self.cursor.get("protected") or {}).get("terms") or []
                if str((t or {}).get("term") or "").strip() not in dismissed_tags
            ]

            def submit(pool: DaemonCallPool, index: int, stop: threading.Event) -> Future:
                self.pre_call_check(self._node_routes([NODE_TAG_WINDOWS]))
                batch_rows = [by_no[no] for no in batches[index] if no in by_no]
                texts = window_texts(self.session, batch_rows)
                windows = [
                    {
                        "window": int(w.window_no),
                        "chapter": int(w.chapter_no or 0),
                        "position": POSITION_LABELS.get(str(w.position), "章中"),
                        "text": clip_window_text(texts.get(int(w.window_no), "")),
                    }
                    for w in batch_rows
                ]
                payload = tag_payload(windows, book_title=self.book_title)
                expected = [int(w.window_no) for w in batch_rows]
                return pool.submit(self._tag_batch, index, payload, expected, protected, stop)

            restart = False

            def apply(index: int, outcome: tuple[dict[int, dict[str, Any]], int]) -> None:
                nonlocal restart
                tags, attempts = outcome
                marker = index_marker(self.book().stats_json) or {}
                if marker.get("root") != root:
                    restart = True  # 索引在打标签期间重建了:按新索引重新规划
                    return
                state = dict(self.cursor.get("tags") or {})
                state["done"] = sorted(set(state.get("done") or []) | {index})
                state["tagged"] = int(state.get("tagged") or 0) + len(tags)
                self.cursor["tags"] = state
                self._count_call(attempts)
                self.cursor["steps_done"] = int(self.cursor.get("steps_done") or 0) + 1

                def write() -> None:
                    from novel_system.services.style_reference.windows import set_window_tags

                    set_window_tags(self.session, self.book_id, tags, tags_version=TAGS_VERSION)

                self._save(
                    write=write,
                    progress=self._progress_values(
                        PHASE_TAGS, detail=f"第 {len(state['done'])}/{len(batches)} 批", calls=attempts
                    ),
                )

            if pending:
                self.run_parallel(
                    pending,
                    submit,
                    apply,
                    max_inflight=PARALLEL_CALLS,
                    poll_seconds=WAIT_POLL_SECONDS,
                    thread_prefix="sr_learn_tags",
                    stop_when=lambda: restart,
                )
            if not restart:
                break
            self.cursor["tags"] = {}
        tags_state = dict(self.cursor.get("tags") or {})
        self._mark_done(
            PHASE_TAGS,
            began,
            detail=f"{int(tags_state.get('tagged') or 0)} 个窗口(沿用 {int(tags_state.get('current') or 0)} 个)",
            step=False,
        )

    def _tag_batch(
        self,
        index: int,
        payload: Mapping[str, Any],
        expected: Sequence[int],
        protected: Sequence[Mapping[str, Any]],
        stop: threading.Event,
    ) -> tuple[dict[int, dict[str, Any]], int]:
        """工人线程:一批最多 ``TAG_ATTEMPTS`` 次(退避重试);输出对不上整批重试。"""
        runtime = self.runtimes[NODE_TAG_WINDOWS]
        problems: list[str] = []
        for attempt in retry_attempts(TAG_ATTEMPTS, CALL_RETRY_BACKOFF_SECONDS, stop):
            try:
                result = call_structured(
                    runtime,
                    payload,
                    self.client,
                    scope_id=self.book_id,
                    step=f"learn:{self.claimed.job_id}:tags:{index}:{attempt + 1}",
                )
                tags = parse_tag_output(result.structured, expected, protected_terms=protected)
                return tags, attempt + 1
            except LearnCallError as exc:
                problems.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:200]}")
            except TagBatchMismatch as exc:
                problems.append(f"attempt {attempt + 1}: mismatch: {'; '.join(exc.problems[:4])}")
        raise LearnFailedError(
            REASON_TAGGING_FAILED,
            f"第 {index + 1} 批窗口标签连续 {TAG_ATTEMPTS} 次失败:{problems[-1] if problems else ''}",
            retryable=True,
            details={"batch": index, "windows": list(expected), "attempts": TAG_ATTEMPTS, "problems": problems},
        )

    def _call_in_worker(self, fn: Callable[[], Any]) -> Any:
        """单个调用放到守护线程里,等待时照样查取消 / 所有权(``JobRun.call_in_worker``)。"""
        return self.call_in_worker(fn, thread_prefix="sr_learn_call", poll_seconds=WAIT_POLL_SECONDS)

    # ---- g. finalize ------------------------------------------------------
    def _ledger(self) -> dict[str, Any]:
        rows = self.session.execute(
            select(LlmCall.step, LlmCall.prompt_tokens, LlmCall.completion_tokens).where(
                LlmCall.scope_type == "style_reference_book",
                LlmCall.scope_id == self.book_id,
                LlmCall.step.like(f"learn:{self.claimed.job_id}:%"),
            )
        ).all()
        by_phase: dict[str, dict[str, int]] = {}
        for step, prompt, completion in rows:
            phase = str(step or "").split(":")[2] if str(step or "").count(":") >= 2 else "other"
            entry = by_phase.setdefault(phase, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            entry["calls"] += 1
            entry["input_tokens"] += int(prompt or 0)
            entry["output_tokens"] += int(completion or 0)
        return {
            "calls": sum(e["calls"] for e in by_phase.values()),
            "input_tokens": sum(e["input_tokens"] for e in by_phase.values()),
            "output_tokens": sum(e["output_tokens"] for e in by_phase.values()),
            "by_phase": by_phase,
        }

    def _dismissed_terms(self, profile_id: str | None) -> set[str]:
        """作者在这份画像里删掉过的自动专名(画像还不存在 → 空)。"""
        if not profile_id:
            return set()
        raw = self.session.execute(
            select(StyleReferenceProfile.profile_json).where(StyleReferenceProfile.profile_id == profile_id)
        ).scalar_one_or_none()
        return dismissed_protected_terms(raw)

    def _phase_finalize(self) -> None:
        began = time.monotonic()
        self._enter(PHASE_FINALIZE)
        run_id = str(self.cursor.get("run_id") or "")
        book = self.book()
        texts = self._prose_texts()
        target_id = str(self.params.get("profile_id") or self.claimed.profile_id or "") or None
        # 作者在画像里删掉过的自动专名(多半是误收的日常词):不再当专名、不再滤卡片、不再加回禁用词表
        dismissed = self._dismissed_terms(target_id)
        card = filter_learned_card(self.session, self.cursor, texts=texts, target_id=target_id, dismissed=dismissed)
        voice = self._voice()
        structure = self._structure_card(voice)
        distribution = reference_distribution_for_book(self.session, self.book_id)
        findings = load_run_findings(self.session, run_id)
        planning = planning_lines(card, findings)
        sub_dimensions = sub_dimension_summary(self.session, run_id)
        index_state = dict(self.cursor.get("index") or {})
        selection = dict(self.cursor.get("selection") or {})
        ledger = self._ledger()
        self._record_done(PHASE_FINALIZE, began)

        # --- 一个事务:游标(条件写,拿写锁)→ 重读画像合并行状态 → 画像 / 专名 / run → 作业成功 ---
        self.fresh()
        if not self.service.save_cursor(self.claimed, self.cursor):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        inputs = FinalizeInputs(
            run_id=run_id,
            target_id=target_id,
            book=book,
            book_title=self.book_title,
            card=card,
            dismissed=dismissed,
            voice=voice,
            structure=structure,
            distribution=distribution.summary() if distribution is not None else None,
            findings=findings,
            planning=planning,
            sub_dimensions=sub_dimensions,
            paragraph_count=len(texts),
            learned_from={
                "types_revision": int(index_state.get("types_revision") or 0),
                "root": index_state.get("root"),
                "index_version": index_state.get("index_version") or WINDOW_INDEX_VERSION,
                "kernel_version": index_state.get("kernel_version") or KERNEL_VERSION,
                "card_version": DIMENSION_CARD_VERSION,
                "tags_version": TAGS_VERSION,
                "job_id": self.claimed.job_id,
                "run_id": run_id,
                "learned_at": utcnow(),
                "extraction": {
                    "windows": [item["window_no"] for item in selection.get("windows") or []],
                    "chars": int(selection.get("chars") or 0),
                    "dropped_windows": list((self.cursor.get("extract") or {}).get("dropped_windows") or []),
                },
                "prompt_versions": {node: rt.prompt_version for node, rt in self.runtimes.items()},
                "models": {node: rt.model for node, rt in self.runtimes.items()},
            },
        )
        written = write_learned_profile(self.session, inputs, job_id=self.claimed.job_id)
        result = learn_result_summary(inputs, written, cursor=self.cursor, ledger=ledger)
        self.service.progress(self.claimed, **self._progress_values(PHASE_FINALIZE, detail="完成"))
        if not self.service.succeed(self.claimed, result):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == self.claimed.job_id)
            .values(profile_id=written.profile.profile_id)
            .execution_options(synchronize_session=False)
        )
        self.session.commit()


def run_learn_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``learn`` 作业处理器(工人线程里运行;终态由这里连同 run 行一起写)。"""
    _LearnRun(session, claimed, service).run()


def on_learn_cancelled(session: Session, job: StyleReferenceJob) -> None:
    """请求 / 认领 / 清扫里直接收尾的取消:这次学习的 run 行一并标 cancelled。"""
    set_run_status(session, dict(job.cursor_json or {}).get("run_id"), "cancelled")


__all__ = [
    "CALL_RETRY_BACKOFF_SECONDS",
    "PARALLEL_CALLS",
    "WAIT_POLL_SECONDS",
    "on_learn_cancelled",
    "resolve_learn_client",
    "run_learn_job",
]

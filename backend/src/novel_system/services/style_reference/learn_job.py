"""风格参考 v3（2026-09-23）—「学习文风」作业：一个按钮、一个可续跑的持久作业（kind=learn，台账 N9 / U11 / I12）。

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
7. ``finalize``  专名与原文重合过滤卡片、沿用作者的 ✓ / ✗、写画像（v3 键，见 ``_profile_json``）与受保护专名行、
                 结果（计数 / 调用 / token / 耗时）——一个事务：先条件写作业行拿写锁，再重读画像合并行状态。

已有画像的书**就地更新**那份画像（同一个 profile_id、version_tag +1、状态 active），绑定照常生效——还有生效绑定的
归档画像（迁移 0092 把旧版画像归档）同样就地更新并复活为 active；没有画像的书新建一份 active 画像。严格 LLM：
没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``；学习节点没有路由、提示词模板缺失或还是旧版本（保存过提示词快照、
没同步）建作业时就 409 ``STYLE_REFERENCE_LEARN_CONFIG_MISSING``（开工时再查一次）；每次调用前按节点的实际路由查书的
云策略。LLM 客户端在作业开始时按当前配置取（``resolve_learn_client``，测试在这里打桩）。处理器的脚手架（检查点、
并行调用循环、终态映射）在 ``job_runtime.JobRun``，与分类 / 对照检查共用。
"""

from __future__ import annotations

import logging
import re
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
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.card import (
    DIMENSION_CARD_VERSION,
    PROFILE_VERSION_V3,
    DimensionCard,
    card_from_profile_json,
    line_states_from_profile_json,
)
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.fidelity import DIMENSION_FEATURES, reference_distribution_for_book
from novel_system.services.style_reference.job_runtime import (
    JobRun,
    JobStopped,
    conflict_error_by_kind,
    retry_attempts,
)
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    ClaimedJob,
    DaemonCallPool,
    JobLost,
    StyleJobService,
    heartbeat_is_stale,
    register_job_handler,
)
from novel_system.services.style_reference.learn_card import (
    MIN_CARD_LINES,
    CardAssembly,
    assemble_card,
    carry_line_states,
    derive_narrative_guidance,
    filter_card,
    fit_synthesis_payload,
    json_clean,
    load_dimension_meta,
    load_run_findings,
    reconcile_card,
    sub_dimension_summary,
    synthesis_payload,
)
from novel_system.services.style_reference.learn_extract import (
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
from novel_system.services.style_reference.learn_llm import (
    EXTRACT_NODES,
    LAYERS,
    LEARN_CONFIG_MISSING_CODE,
    LEARN_NODE_IDS,
    NODE_PROTECTED_TERMS,
    NODE_SYNTHESIZE,
    NODE_TAG_WINDOWS,
    LearnCallError,
    LearnNodeRuntime,
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
from novel_system.services.style_reference.measure import KERNEL_VERSION
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.profile_fields import REFERENCE_BASIS_VERSION
from novel_system.services.style_reference.protected_terms import (
    PROTECTED_SOURCE,
    ProtectedTerm,
    DISMISSED_KEY,
    dismissed_protected_terms,
    parse_protected_terms,
    proper_noun_candidates,
    protected_terms_version,
    replace_protected_terms,
)
from novel_system.services.style_reference.schemas import FindingKind
from novel_system.services.style_reference.structure import (
    compute_structure_card,
    derive_planning_guidance,
    non_body_kind,
    render_structure_card_parts,
)
from novel_system.services.style_reference.tags import TAGS_VERSION
from novel_system.services.style_reference.validation.plagiarism import CorpusOverlapIndex
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

CURSOR_VERSION = 1

PHASE_WINDOWS = "windows"
PHASE_SELECT = "select"
PHASE_EXTRACT = "extract"
PHASE_SYNTHESIZE = "synthesize"
PHASE_PROTECTED = "protected"
PHASE_TAGS = "tags"
PHASE_FINALIZE = "finalize"
PHASE_ORDER: tuple[str, ...] = (
    PHASE_WINDOWS,
    PHASE_SELECT,
    PHASE_EXTRACT,
    PHASE_SYNTHESIZE,
    PHASE_PROTECTED,
    PHASE_TAGS,
    PHASE_FINALIZE,
)
PHASE_LABELS: dict[str, str] = {
    PHASE_WINDOWS: "整理全书样例窗口",
    PHASE_SELECT: "挑选学习样本",
    PHASE_EXTRACT: "逐层读原文",
    PHASE_SYNTHESIZE: "写文风卡",
    PHASE_PROTECTED: "识别本书专名",
    PHASE_TAGS: "给全书片段打标签",
    PHASE_FINALIZE: "写入画像",
}
LAYER_LABELS: dict[str, str] = {"language": "语言层", "narrative": "叙事层", "scene": "场景层", "theme": "主题层"}

PARALLEL_CALLS = 3
EXTRACT_ATTEMPTS = 2  # 1 次 + 1 次带问题清单的重试(两次的有效发现合并)
SYNTH_ATTEMPTS = 2
PROTECTED_ATTEMPTS = 2
TAG_ATTEMPTS = 3  # 1 次 + 2 次退避重试
CALL_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0)
WAIT_POLL_SECONDS = 2.0

LEARN_FAILED_CODE = "STYLE_REFERENCE_LEARN_FAILED"
LEARN_ALREADY_ACTIVE_CODE = "STYLE_REFERENCE_LEARN_ALREADY_ACTIVE"
LEARN_NOT_ACTIVE_CODE = "STYLE_REFERENCE_LEARN_NOT_ACTIVE"
LEARN_NOTHING_TO_RESUME_CODE = "STYLE_REFERENCE_LEARN_NOTHING_TO_RESUME"
BOOK_CLASSIFYING_CODE = "STYLE_REFERENCE_BOOK_CLASSIFYING"
BOOK_NOT_READY_CODE = "STYLE_REFERENCE_BOOK_NOT_READY"
INPUT_TOO_SMALL_CODE = "STYLE_REFERENCE_INPUT_TOO_SMALL"

REASON_INPUT_TOO_SMALL = "input_too_small"
REASON_EXTRACT_FAILED = "extract_failed"
REASON_NO_FINDINGS = "no_findings"
REASON_SYNTHESIZE_FAILED = "synthesize_failed"
REASON_PROTECTED_FAILED = "protected_terms_failed"
REASON_TAGGING_FAILED = "tagging_failed"
REASON_CARD_FILTERED_EMPTY = "card_filtered_empty"

RUN_DISPATCH_STATE = "learn_job"


def resolve_learn_client() -> tuple[Any | None, bool]:
    """作业开始时按**当前**运行时配置取 LLM 客户端（不在请求里捕获）；测试在这里打桩。"""
    from novel_system.services.system_config import build_runtime_llm_client
    from novel_system.settings import get_settings

    return build_runtime_llm_client(settings=get_settings())


class LearnFailedError(DomainError):
    """学习作业的某一步失败（``details.reason_code``；可续跑的带 ``retryable`` 与「继续学习」动作）。"""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        retryable: bool = True,
        details: Mapping[str, Any] | None = None,
        author_action: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            **dict(details or {}),
            "reason_code": reason_code,
            "retryable": bool(retryable),
            "author_action": dict(author_action)
            if author_action
            else (
                {"action": "resume_learning", "view": "styleref", "label": "检查模型接入后「继续学习」"}
                if retryable
                else {"action": "review_book", "view": "styleref", "label": "这本书不适合学习，换一本或补足正文"}
            ),
        }
        super().__init__(LEARN_FAILED_CODE, message, status_code=409 if not retryable else 502, details=payload)
        self.retryable = bool(retryable)
        self.reason_code = reason_code


# ---------------------------------------------------------------- 建 / 续 / 取消 / 读


def latest_learn_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    return StyleJobService(session).latest_for_book(book_id, kind=JOB_KIND_LEARN)


def active_learn_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    active = StyleJobService(session).active_for_book(book_id, kind=JOB_KIND_LEARN)
    return active[0] if active else None


def _book_or_404(session: Session, book_id: str) -> StyleReferenceBook:
    book = session.get(StyleReferenceBook, str(book_id))
    if book is None:
        raise DomainError("STYLE_REFERENCE_BOOK_NOT_FOUND", f"book {book_id!r} not found", status_code=404)
    return book


def _target_profile(session: Session, book_id: str, requested: str | None) -> StyleReferenceProfile | None:
    """重新学习要就地更新的画像：指定了就用它（必须属于这本书）；否则优先有绑定的、再 active、再最近更新的。

    归档画像一般不选（作者归档过的不再动）——但**还有生效绑定的**归档画像要选：迁移 0092 把没有文风卡的旧版画像归档、
    绑定保留，「学习文风」就是要就地把它学成 v3 并复活（finalize 置 active，绑定不动），而不是另建一份、让绑定
    继续指着那份旧画像。"""
    if requested:
        profile = session.get(StyleReferenceProfile, str(requested))
        if profile is None:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_NOT_FOUND", f"profile {requested!r} not found", status_code=404
            )
        if str(profile.book_id) != str(book_id):
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_BOOK_MISMATCH",
                "这份画像不属于这本书",
                status_code=409,
                details={"profile_id": requested, "book_id": book_id},
            )
        return profile
    rows = list(session.scalars(select(StyleReferenceProfile).where(StyleReferenceProfile.book_id == str(book_id))))
    if not rows:
        return None
    ids = [p.profile_id for p in rows]
    bound = set(
        session.scalars(
            select(StyleReferenceInjectionBinding.profile_id).where(StyleReferenceInjectionBinding.profile_id.in_(ids))
        )
    )
    actively_bound = set(
        session.scalars(
            select(StyleReferenceInjectionBinding.profile_id).where(
                StyleReferenceInjectionBinding.profile_id.in_(ids),
                StyleReferenceInjectionBinding.status == "active",
            )
        )
    )
    profiles = [p for p in rows if str(p.status or "") != "archived" or p.profile_id in actively_bound]
    if not profiles:
        return None
    profiles.sort(
        key=lambda p: (
            p.profile_id in bound,
            str(p.status or "") == "active",
            str(p.updated_at or ""),
            str(p.profile_id),
        ),
        reverse=True,
    )
    return profiles[0]


def _skipped_layers(book: StyleReferenceBook, *, force: bool) -> list[str]:
    assessment = (book.stats_json or {}).get("input_assessment") or {}
    if force or not isinstance(assessment, Mapping):
        return []
    return [layer for layer in LAYERS if assessment.get(layer) == "skip"]


def _tag_template_is_v2(template: Any) -> bool:
    """窗口标签模板的输出带 ``windows[].dimensions``（v2，2026-09-24）。旧的 v1 模板（保存过提示词快照、没同步）
    还在要 ``devices``：用它打出来的标签没有维度、却会记成当前版本，之后再也不会重打——所以按旧模板一律拒。"""
    schema = getattr(template, "structured_schema", None)
    try:
        return "dimensions" in schema["properties"]["windows"]["items"]["properties"]
    except (KeyError, TypeError):
        return False


def ensure_tag_template_current(runtimes: Mapping[str, LearnNodeRuntime]) -> None:
    """``load_learn_runtimes`` 之外的一条契约：打标签的模板必须是 v2（见 :func:`_tag_template_is_v2`）；不是 → 与
    模板缺失同一个 409 ``STYLE_REFERENCE_LEARN_CONFIG_MISSING``（``details.stale_templates``）。"""
    runtime = runtimes.get(NODE_TAG_WINDOWS)
    if runtime is None or _tag_template_is_v2(runtime.template):
        return
    raise DomainError(
        LEARN_CONFIG_MISSING_CODE,
        "学习文风要用的模型节点还没配好。提示词模板缺失或还是旧版本:"
        f"{NODE_TAG_WINDOWS}(窗口标签 v2 输出维度,保存过提示词快照的安装要同步提示词模板:sync_prompt_templates --execute)。",
        status_code=409,
        details={
            "missing_routes": [],
            "missing_templates": [],
            "stale_templates": [NODE_TAG_WINDOWS],
            "author_action": {
                "action": "sync_prompt_templates",
                "view": "systemConfig",
                "label": "同步提示词模板",
            },
        },
    )


def start_learn_job(
    session: Session,
    book_id: str,
    *,
    profile_id: str | None = None,
    force: bool = False,
    resume: bool = False,
    retag: bool = False,
    op_key: str | None = None,
    llm_client: Any | None = None,
) -> StyleReferenceJob:
    """建（或续）这本书的学习作业（调用方提交后派发）。检查见模块文档与各错误码。

    ``retag``：给全书每个窗口重打标签（缺省只补标签版本不是当前版本的窗口）。"""
    book = _book_or_404(session, book_id)
    if str(book.status or "") != "ready":
        raise DomainError(
            BOOK_NOT_READY_CODE,
            "这本书的段落分类还没完成:等分类完成(或「继续分类」)之后再学习文风。",
            status_code=409,
            details={
                "book_id": book_id,
                "status": book.status,
                "author_action": {"action": "wait_or_resume_classification", "view": "styleref", "book_id": book_id},
            },
        )
    classifying = StyleJobService(session).active_for_book(book_id, kind=JOB_KIND_CLASSIFY)
    if classifying:
        # 就地重标段落类型时书一直是 ready——得看作业表
        raise _classifying_error(classifying[0], book_id)
    active = active_learn_job(session, book_id)
    if active is not None and not (
        active.state == STATE_RUNNING and heartbeat_is_stale(active.heartbeat_at) and resume
    ):
        raise DomainError(
            LEARN_ALREADY_ACTIVE_CODE,
            "这本书正在学习文风:等它完成,或先取消。",
            status_code=409,
            details={"book_id": book_id, "job_id": active.job_id, "state": active.state},
        )
    # 节点路由 / 提示词模板缺失或是旧版本:建作业之前就 409(不建一个注定在工人里失败的作业);作业开工时再查一次
    ensure_tag_template_current(load_learn_runtimes())
    ensure_cloud_llm_allowed(book, operation="learn_style", node_ids=LEARN_NODE_IDS, llm_client=llm_client)
    skipped = _skipped_layers(book, force=force)
    if len(skipped) == len(LAYERS):
        raise DomainError(
            INPUT_TOO_SMALL_CODE,
            "这本书的正文太少,四层文风都学不出可靠的结论;补足正文重新导入,或带 force 强行学习。",
            status_code=409,
            details={"book_id": book_id, "input_assessment": (book.stats_json or {}).get("input_assessment")},
        )
    service = StyleJobService(session)
    if resume:
        latest = latest_learn_job(session, book_id)
        if latest is None or latest.state == STATE_SUCCEEDED:
            raise DomainError(
                LEARN_NOTHING_TO_RESUME_CODE,
                "这本书没有中断的学习可以继续;直接「学习文风」即可。",
                status_code=409,
                details={"book_id": book_id},
            )
        job = service.requeue(latest.job_id, params_update={"retag": True} if retag else None)
        # 放回队列这条 UPDATE 已拿到写锁:再查一次分类作业(与建作业同一个「先写后查」,见 jobs 模块文档)
        conflict = service.first_conflict(book_id, kinds=(JOB_KIND_CLASSIFY,), excluding=job.job_id)
        if conflict is not None:
            raise _classifying_error(conflict, book_id)
        if op_key:
            job.op_key = op_key
        progress = dict(job.progress_json or {})
        progress.update({"phase_label": "排队中", "resumed_at": utcnow()})
        job.progress_json = progress
        session.flush()
        return job
    target = _target_profile(session, book_id, profile_id)
    job = service.create(
        JOB_KIND_LEARN,
        book_id=book_id,
        profile_id=target.profile_id if target is not None else None,
        op_key=op_key or None,
        params={
            "profile_id": target.profile_id if target is not None else None,
            "force": bool(force),
            "retag": bool(retag),
            "skipped_layers": skipped,
        },
        phase="queued",
        exclusive_with=(JOB_KIND_CLASSIFY,),
        conflict_error=lambda other: conflict_error_by_kind(
            other, book_id, by_kind={JOB_KIND_CLASSIFY: _classifying_error}, default=_already_learning_error
        ),
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中"}
    session.flush()
    return job


def _classifying_error(job: StyleReferenceJob, book_id: str) -> DomainError:
    return DomainError(
        BOOK_CLASSIFYING_CODE,
        "这本书正在重标段落类型:等它完成再学习文风(段落类型决定挑样本与窗口的构成)。",
        status_code=409,
        details={"book_id": book_id, "job_id": job.job_id},
    )


def _already_learning_error(other: StyleReferenceJob, book_id: str) -> DomainError:
    return DomainError(
        LEARN_ALREADY_ACTIVE_CODE,
        "这本书正在学习文风:等它完成,或先取消。",
        status_code=409,
        details={"book_id": book_id, "job_id": other.job_id, "state": other.state},
    )


def cancel_learn(session: Session, book_id: str) -> StyleReferenceJob | None:
    """取消这本书排队 / 运行中的学习作业（排队中或工人已死的在这里直接收尾；运行中的在下一个检查点收尾）。"""
    job = active_learn_job(session, book_id)
    if job is None:
        return None
    # 排队中 / 工人已死的作业在请求里直接收尾;学习的 run 行由登记的收尾钩子(_on_learn_cancelled)一并落定
    return StyleJobService(session).request_cancel(job.job_id)


def learn_payload(job: StyleReferenceJob | None) -> dict[str, Any] | None:
    """书载荷里的 ``learn``：最近一个学习作业的摘要（没有返回 None）。"""
    if job is None:
        return None
    cursor = dict(job.cursor_json or {})
    progress = dict(job.progress_json or {})
    error = dict(job.error_json) if job.error_json else None
    stalled = job.state == STATE_RUNNING and heartbeat_is_stale(job.heartbeat_at)
    # 能不能「继续学习」:取消 / 心跳过期的能;失败的看 error.retryable——正文太少(input_too_small)、卡片被滤空这类
    # 失败续跑只会再失败一次,只能「重新学习」(或带 force「仍然学习」)
    resumable = (
        job.state == STATE_CANCELLED
        or stalled
        or (job.state == STATE_FAILED and (error or {}).get("retryable") is not False)
    )
    return {
        "job_id": job.job_id,
        "state": job.state,
        "phase": job.phase,
        "phase_label": progress.get("phase_label"),
        "detail": progress.get("detail"),
        "done": int(progress.get("done") or 0),
        "total": int(progress.get("total") or 0),
        "llm_calls": int(progress.get("llm_calls") or 0),
        "profile_id": job.profile_id or (job.result_json or {}).get("profile_id"),
        "phases_done": list(cursor.get("phases_done") or []),
        "attempt": int(job.attempt or 0),
        "error": error,
        "result": dict(job.result_json) if job.result_json else None,
        "cancel_requested": bool(job.cancel_requested),
        "stalled": stalled,
        "resumable": resumable,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def windows_needing_tags(rows: Sequence[Any], *, retag: bool = False) -> list[Any]:
    """还要打标签的窗口：标签版本不是当前版本(或没有标签)的;``retag`` 时全部。"""
    if retag:
        return list(rows)
    return [w for w in rows if not w.tags_json or str(w.tags_version or "") != TAGS_VERSION]


def _tag_plan_input(rows: Sequence[Any]) -> list[tuple[int, int]]:
    return [(int(w.window_no), int(w.chars or 0)) for w in rows]


def estimate_learning(session: Session, book: StyleReferenceBook, *, retag: bool = False) -> dict[str, Any]:
    """学一次要多少次调用 / 多少字的输入（窗口索引是最新的才给出标签批数，否则按全书字数粗估）。

    标签只算还要打的窗口(``windows_to_tag``;全部是当前版本时 ``tags`` 批数为 0),``retag`` 按全书算。"""
    marker = index_marker(book.stats_json) or {}
    rows = load_windows(session, book.book_id) if marker else []
    to_tag = windows_needing_tags(rows, retag=retag)
    windows = [(int(w.window_no), int(w.chars or 0)) for w in to_tag]
    batches = plan_tag_batches(windows) if windows else []
    tag_chars = sum(min(chars, 3000) for _no, chars in windows)
    return {
        "book_id": book.book_id,
        "windows": len(rows) or None,
        "windows_to_tag": len(to_tag) if rows else None,
        "calls": {
            "extract": len(LAYERS),
            "synthesize": 1,
            "protected": 1,
            "tags": len(batches) if rows else None,
        },
        "est_calls": (len(LAYERS) + 2 + len(batches)) if rows else None,
        "est_input_chars": {
            "extract_per_call": 44_000,
            "tags_total": tag_chars or None,
        },
        "retries_not_included": True,
    }


# ---------------------------------------------------------------- the handler


@dataclass
class _LayerOutcome:
    layer: str
    parse: LayerParse
    attempts: int
    llm_call_id: str | None
    dropped_windows: list[int]


def _set_run_status(session: Session, run_id: str | None, status: str) -> None:
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
        self.runtimes: dict[str, LearnNodeRuntime] = {}
        self.book_title = ""

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
        self._finish(code=code, message=message, retryable=retryable, details=details)

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
        _set_run_status(self.session, self.cursor.get("run_id"), "cancelled" if cancelled else "failed")
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
            "tag_batches": len(plan_tag_batches(_tag_plan_input(windows_needing_tags(windows, retag=self._retag())))),
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

    def _body_texts(self) -> list[str]:
        return [
            str(text)
            for (text,) in self.session.execute(
                select(StyleReferenceParagraph.text)
                .where(StyleReferenceParagraph.book_id == self.book_id)
                .order_by(StyleReferenceParagraph.paragraph_index)
            )
            if str(text or "").strip()
        ]

    def _structure_card(self, voice: Mapping[str, Any]) -> dict[str, Any] | None:
        cached = self.cursor.get("structure_card")
        if isinstance(cached, Mapping):
            return dict(cached)
        book = self.book()
        stats = dict(book.stats_json or {})
        rows = [
            {"paragraph_index": index, "paragraph_type": ptype, "text": text, "paragraph_id": pid}
            for index, ptype, text, pid in self.session.execute(
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
        texts = [t for t in self._body_texts() if non_body_kind(t) is None]
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
                    "batches": plan_tag_batches(_tag_plan_input(to_tag)),
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
                        "position": {"opening": "章首", "closing": "章末", "whole": "整章"}.get(str(w.position), "章中"),
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
        texts = [t for t in self._body_texts() if non_body_kind(t) is None]
        overlap = CorpusOverlapIndex(texts, threshold_chars=12)
        # 作者给这份画像录入的禁用词(任何域)同样不能进卡片:画像已存在时一并过滤
        target_id = str(self.params.get("profile_id") or self.claimed.profile_id or "") or None
        # 作者在画像里删掉过的自动专名(多半是误收的日常词):不再当专名、不再滤卡片、不再加回禁用词表
        dismissed = self._dismissed_terms(target_id)
        protected = [
            ProtectedTerm(term=str(t["term"]), kind=str(t.get("kind") or "term"))
            for t in (self.cursor.get("protected") or {}).get("terms") or []
            if str(t.get("term") or "").strip() not in dismissed
        ]
        author_terms = [
            str(term)
            for term in self.session.scalars(
                select(StyleReferenceBannedTerm.term).where(
                    StyleReferenceBannedTerm.profile_id == target_id,
                    StyleReferenceBannedTerm.source != PROTECTED_SOURCE,
                )
            )
            if str(term or "").strip()
        ] if target_id else []
        assembly, filtered = filter_card(
            CardAssembly.from_cursor(self.cursor.get("card") or {}),
            protected=[t.term for t in protected] + author_terms,
            overlaps=lambda text: overlap.contains_overlap(text, ngram_size=8),
        )
        if assembly.card is None or not assembly.card.all_lines():
            # 重新学习是就地更新:一张空卡会把作者正在用的画像冲掉——宁可失败,画像原样不动
            raise LearnFailedError(
                REASON_CARD_FILTERED_EMPTY,
                "文风卡的句子全被过滤掉了(含本书专名、这份画像的禁用词,或与原文大段重合);画像没有改动。"
                "检查这份画像的禁用词后重新「学习文风」。",
                retryable=False,
                details={"filtered": filtered, "author_terms": len(author_terms), "protected_terms": len(protected)},
                author_action={"action": "review_banned_terms", "view": "styleref", "label": "检查这份画像的禁用词"},
            )
        voice = self._voice()
        structure = self._structure_card(voice)
        distribution = reference_distribution_for_book(self.session, self.book_id)
        findings = load_run_findings(self.session, run_id)
        planning = list(assembly.planning_guidance)
        if not planning:
            # 合成没给规划层手法:从场景 / 主题层的观察里挑(同样过专名与原文重合)
            planning = derive_planning_guidance(
                [
                    {
                        "finding_kind": f.kind,
                        "sub_dimension": f.dimension,
                        "statement": f.statement,
                        "confidence": f.confidence,
                        "status": "pending",
                    }
                    for f in findings
                ],
                overlap_filter=lambda text: overlap.contains_overlap(text, ngram_size=8)
                or any(t.term in text for t in protected),
            )
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
        profile = (
            self.session.execute(
                select(StyleReferenceProfile)
                .where(StyleReferenceProfile.profile_id == target_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if target_id
            else None
        )
        previous_json = dict(profile.profile_json or {}) if profile is not None else {}
        card, states, carried = carry_line_states(
            assembly.card,
            card_from_profile_json(previous_json),
            line_states_from_profile_json(previous_json),
        )
        if card is not None:
            card = card.model_copy(update={"generated_at": utcnow()})
        dismissed = dismissed | dismissed_protected_terms(previous_json)
        profile_json = _profile_json(
            book=book,
            card=card,
            states=states,
            voice=voice,
            distribution=distribution.summary() if distribution is not None else None,
            structure=structure,
            planning=planning,
            narrative=derive_narrative_guidance(card),
            qualitative_summary=assembly.qualitative_summary,
            metrics_baseline={
                **dict((book.stats_json or {}).get("metrics") or {}),
                **dict((book.stats_json or {}).get("prose_shape_metrics") or {}),
            },
            sub_dimensions=sub_dimensions,
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
            protected=protected,
            paragraph_count=len(texts),
        )
        if dismissed:
            profile_json[DISMISSED_KEY] = sorted(dismissed)
        card_lines = card.all_lines() if card is not None else []
        coverage = {
            "learn_job_id": self.claimed.job_id,
            "sub_dim_count": len(sub_dimensions),
            "findings_count": len(findings),
            "quotes_count": sum(int(v.get("quote_count") or 0) for v in sub_dimensions.values()),
            "card_lines": len(card_lines),
        }
        title = assembly.profile_title or f"{self.book_title}的文风"
        created = profile is None
        if profile is None:
            profile = StyleReferenceProfile(
                profile_id=f"sr_profile_{uuid.uuid4().hex[:12]}",
                book_id=self.book_id,
                run_id=run_id,
                title=title,
                status="active",
                profile_json=profile_json,
                coverage_json=coverage,
                source_finding_ids_json=[f.finding_id for f in findings],
                version_tag="v1",
            )
            self.session.add(profile)
        else:
            profile.run_id = run_id
            profile.title = title
            profile.status = "active"
            profile.profile_json = profile_json
            profile.coverage_json = coverage
            profile.source_finding_ids_json = [f.finding_id for f in findings]
            profile.version_tag = _bump_version(profile.version_tag)
        self.session.flush()
        protected_rows = replace_protected_terms(self.session, profile.profile_id, protected, dismissed=dismissed)
        run = self.session.get(StyleReferenceRun, run_id)
        if run is not None:
            run.status = "done"
            run.phase = "done"
            run.finished_at = utcnow()
            run.heartbeat_at = utcnow()
            run.coverage_json = {
                **dict(run.coverage_json or {}),
                "sub_dimensions": {
                    dim: {
                        "findings": int(v["observation_count"]) + int(v["forbidden_pattern_count"]),
                        "quotes": int(v["quote_count"]),
                    }
                    for dim, v in sub_dimensions.items()
                },
            }
        timings = dict(self.cursor.get("timings") or {})
        layers_done = dict(self.cursor.get("layers_done") or {})
        tags_state = dict(self.cursor.get("tags") or {})
        result = {
            "profile_id": profile.profile_id,
            "profile_created": created,
            "version_tag": profile.version_tag,
            "run_id": run_id,
            "book_id": self.book_id,
            "windows": {
                "index": int(index_state.get("window_count") or 0),
                "extraction": len(selection.get("windows") or []),
                "extraction_chars": int(selection.get("chars") or 0),
                "tagged": int(tags_state.get("tagged") or 0),
                "tag_batches": len(tags_state.get("batches") or []),
            },
            "findings": {
                "total": len(findings),
                "observations": sum(1 for f in findings if f.kind == FindingKind.OBSERVATION.value),
                "avoid": sum(1 for f in findings if f.kind == FindingKind.FORBIDDEN_PATTERN.value),
                "quotes": coverage["quotes_count"],
                "by_layer": {
                    layer: {key: state.get(key) for key in ("attempts", "returned", "rejected")}
                    for layer, state in layers_done.items()
                },
            },
            "card": {
                "lines": len(card_lines),
                "do": sum(1 for _d, line in card_lines if line.kind == "do"),
                "avoid": sum(1 for _d, line in card_lines if line.kind == "avoid"),
                "mandatory": sum(1 for _d, line in card_lines if line.mandatory),
                "temperament": len(card.temperament) if card is not None else 0,
                "carried_pins": carried,
                "dropped": dict(assembly.dropped),
                "filtered_at_finalize": filtered,
            },
            "protected_terms": {"count": len(protected), **protected_rows},
            "llm": {**ledger, "retries": int(self.cursor.get("retries") or 0)},
            "seconds": {"by_phase": timings, "total": round(sum(timings.values()), 2)},
        }
        self.service.progress(self.claimed, **self._progress_values(PHASE_FINALIZE, detail="完成"))
        if not self.service.succeed(self.claimed, result):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == self.claimed.job_id)
            .values(profile_id=profile.profile_id)
            .execution_options(synchronize_session=False)
        )
        self.session.commit()


_VERSION_RE = re.compile(r"(\d+)$")


def _bump_version(tag: str | None) -> str:
    match = _VERSION_RE.search(str(tag or ""))
    return f"v{int(match.group(1)) + 1}" if match else "v2"


def _profile_json(
    *,
    book: StyleReferenceBook,
    card: DimensionCard | None,
    states: Mapping[str, str],
    voice: Mapping[str, Any],
    distribution: Mapping[str, Any] | None,
    structure: Mapping[str, Any] | None,
    planning: Sequence[str],
    narrative: Sequence[str],
    qualitative_summary: str,
    metrics_baseline: Mapping[str, Any],
    sub_dimensions: Mapping[str, Any],
    learned_from: Mapping[str, Any],
    protected: Sequence[ProtectedTerm],
    paragraph_count: int,
) -> dict[str, Any]:
    """v3 画像（契约 §2.2）。``voice`` = 测量核声音特征 + 具体习惯句 + 作者自身的参照分布；另写一份不带分布的
    ``voice_signature`` 过渡别名。不写 ``exemplar_windows``（窗口在窗口表）、``scene_samples_index`` 与旧的
    ``style_features`` / ``narrative_patterns`` / ``banned_replication_rules`` / ``calibration_guidance``。"""
    card_json = card.model_dump(mode="json") if card is not None else None
    if card_json is not None:
        for entry in card_json.get("dimensions") or []:
            entry["measurable_features"] = list(DIMENSION_FEATURES.get(entry.get("dimension"), ()))
    voice_block = {**dict(voice)}
    if distribution is not None:
        voice_block["distribution"] = dict(distribution)
    return {
        "profile_version": PROFILE_VERSION_V3,
        "dimension_card": card_json,
        "card_line_states": dict(states),
        "voice": voice_block,
        # 别名(同一次写入、内容同源,不带 distribution)。还在读它的:运行时契约的冻结白名单
        # (runtime_contract.FROZEN_PROFILE_JSON_KEYS——契约里冻结的是这个键)、style_continuity 的契约声音参照与
        # 刻意复沓判定(新鲜度预算)、scene_diagnosis 的刻意复沓校准;渲染器 / 摘要先读 ``voice`` 再退回它。
        # 这些读者都改读 ``voice`` 之前不能删。
        "voice_signature": dict(voice),
        "structure_card": dict(structure) if structure else None,
        "planning_guidance": list(planning),
        "narrative_guidance": list(narrative),
        "qualitative_summary": qualitative_summary,
        "metrics_baseline": dict(metrics_baseline),
        "sub_dimensions": dict(sub_dimensions),
        "reference_basis": {
            "version": REFERENCE_BASIS_VERSION,
            "mode": "reference_derived",
            "scope": "work_or_collection",
            "fixed_author_allowlist": False,
            "book_id": str(book.book_id),
            "source_kind": str(book.source_kind),
            "text_checksum": str(book.text_checksum),
            "source_char_count": int(book.total_chars or 0),
            "paragraph_count": int(paragraph_count),
        },
        "learned_from": dict(learned_from),
        "protected_terms_version": protected_terms_version(protected),
    }


def run_learn_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``learn`` 作业处理器(工人线程里运行;终态由这里连同 run 行一起写)。"""
    _LearnRun(session, claimed, service).run()


def _on_learn_cancelled(session: Session, job: StyleReferenceJob) -> None:
    """请求 / 认领 / 清扫里直接收尾的取消:这次学习的 run 行一并标 cancelled。"""
    _set_run_status(session, dict(job.cursor_json or {}).get("run_id"), "cancelled")


register_job_handler(JOB_KIND_LEARN, run_learn_job, on_cancelled=_on_learn_cancelled)


__all__ = [
    "BOOK_CLASSIFYING_CODE",
    "LEARN_ALREADY_ACTIVE_CODE",
    "LEARN_FAILED_CODE",
    "LEARN_NOTHING_TO_RESUME_CODE",
    "LEARN_NOT_ACTIVE_CODE",
    "LearnFailedError",
    "PHASE_ORDER",
    "active_learn_job",
    "cancel_learn",
    "ensure_tag_template_current",
    "estimate_learning",
    "latest_learn_job",
    "learn_payload",
    "resolve_learn_client",
    "run_learn_job",
    "start_learn_job",
    "windows_needing_tags",
]

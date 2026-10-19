"""风格参考 v3（2026-09-23）—「学习文风」作业的对外接口：一个按钮、一个可续跑的持久作业（kind=learn，台账 N9 / U11 / I12）。

- 建 / 续 / 取消（:func:`start_learn_job` / :func:`cancel_learn`）、书卡与活动面板的摘要（:func:`learn_payload` /
  :func:`learn_resumable`）、学一次的调用数估计（:func:`estimate_learning`）；
- 作业的七步、每步的名字、失败原因码与 :class:`LearnFailedError`（作业本身与定稿步都按它报失败）。

作业处理器（七步：整理窗口 → 挑学习样本 → 四层抽取 → 写文风卡 → 识别本书专名 → 给窗口打标签 → 写入画像）在
``learn_run``，最后一步「写入画像」的拼装与写库在 ``learn_finalize``；处理器由 ``workers.install_workers`` 登记。

已有画像的书**就地更新**那份画像（同一个 profile_id、version_tag +1、状态 active），绑定照常生效——还有生效绑定的
归档画像（迁移 0092 把旧版画像归档）同样就地更新并复活为 active；没有画像的书新建一份 active 画像。严格 LLM：
没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``；学习节点没有路由（或提示词模板缺失）建作业时就 409
``STYLE_REFERENCE_LEARN_CONFIG_MISSING``（开工时再查一次）。
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceProfile,
    StyleReferenceRun,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import book_not_found, profile_not_found
from novel_system.services.style_reference.job_runtime import conflict_error_by_kind
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    StyleJobService,
    heartbeat_is_stale,
)
from novel_system.services.style_reference.learn_consolidate import find_reusable_reading, reading_signature
from novel_system.services.style_reference.learn_llm import (
    EXTRACT_NODES,
    LAYERS,
    LEARN_NODE_IDS,
    load_consolidate_runtime,
    load_foreshadow_runtimes,
    load_learn_runtimes,
    load_situation_runtime,
)
from novel_system.services.style_reference.learn_select import (
    FULL_BATCH_MAX_CHARS,
    FULL_PLAN_VERSION,
    chapter_spans,
    plan_full_batches,
    window_meta,
)
from novel_system.services.style_reference.learn_tags import plan_tag_batches
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.learn_situations import MIN_WINDOWS as SITUATION_MIN_WINDOWS
from novel_system.services.style_reference.tags import SITUATION_TAGS, TAGS_VERSION
from novel_system.services.style_reference.windows import (
    WINDOW_INDEX_VERSION,
    index_marker,
    load_windows,
)


logger = logging.getLogger(__name__)

CURSOR_VERSION = 1

PHASE_WINDOWS = "windows"
PHASE_SELECT = "select"
PHASE_EXTRACT = "extract"
# 通读全书才有：全书各批的发现按维归并（快速学习跳过）
PHASE_CONSOLIDATE = "consolidate"
# 通读全书才有：全书的伏笔与回收配对、统计、手法（结构画像要用，排在写文风卡之前）
PHASE_FORESHADOW = "foreshadow"
PHASE_SYNTHESIZE = "synthesize"
PHASE_PROTECTED = "protected"
PHASE_TAGS = "tags"
# 通读全书才有：按场面的写法（要用窗口的场面标签，排在打标签之后）
PHASE_SITUATIONS = "situations"
PHASE_FINALIZE = "finalize"
PHASE_ORDER: tuple[str, ...] = (
    PHASE_WINDOWS,
    PHASE_SELECT,
    PHASE_EXTRACT,
    PHASE_CONSOLIDATE,
    PHASE_FORESHADOW,
    PHASE_SYNTHESIZE,
    PHASE_PROTECTED,
    PHASE_TAGS,
    PHASE_SITUATIONS,
    PHASE_FINALIZE,
)
# 快速学习没有归并、伏笔与按场面的写法
QUICK_PHASE_ORDER: tuple[str, ...] = tuple(
    phase for phase in PHASE_ORDER if phase not in (PHASE_CONSOLIDATE, PHASE_FORESHADOW, PHASE_SITUATIONS)
)
PHASE_LABELS: dict[str, str] = {
    PHASE_WINDOWS: "整理全书样例窗口",
    PHASE_SELECT: "挑选学习样本",
    PHASE_EXTRACT: "逐层读原文",
    PHASE_CONSOLIDATE: "归并全书的发现",
    PHASE_FORESHADOW: "配对伏笔与回收",
    PHASE_SYNTHESIZE: "写文风卡",
    PHASE_PROTECTED: "识别本书专名",
    PHASE_TAGS: "给全书片段打标签",
    PHASE_SITUATIONS: "按场面学写法",
    PHASE_FINALIZE: "写入画像",
}
# 通读全书时这几步换个说法
FULL_PHASE_LABELS: dict[str, str] = {PHASE_SELECT: "按整章切批", PHASE_EXTRACT: "逐批读原文"}
LAYER_LABELS: dict[str, str] = {"language": "语言层", "narrative": "叙事层", "scene": "场景层", "theme": "主题层"}

# 学习方式：快速学习（约 12 窗 / 4 万字，同一本书永远是同一组，用来比较模型 / 提示词的改动）、通读全书
MODE_QUICK = "quick"
MODE_FULL = "full"
LEARN_MODES: tuple[str, ...] = (MODE_QUICK, MODE_FULL)

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
REASON_CONSOLIDATE_FAILED = "consolidate_failed"
REASON_SYNTHESIZE_FAILED = "synthesize_failed"
REASON_PROTECTED_FAILED = "protected_terms_failed"
REASON_TAGGING_FAILED = "tagging_failed"
REASON_SITUATIONS_FAILED = "situations_failed"
REASON_FORESHADOW_FAILED = "foreshadow_failed"
REASON_CARD_FILTERED_EMPTY = "card_filtered_empty"

RUN_DISPATCH_STATE = "learn_job"


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
        raise book_not_found(book_id)
    return book


def _target_profile(session: Session, book_id: str, requested: str | None) -> StyleReferenceProfile | None:
    """重新学习要就地更新的画像：指定了就用它（必须属于这本书）；否则优先有绑定的、再 active、再最近更新的。

    归档画像一般不选（作者归档过的不再动）——但**还有生效绑定的**归档画像要选：迁移 0092 把没有文风卡的旧版画像归档、
    绑定保留，「学习文风」就是要就地把它学成 v3 并复活（finalize 置 active，绑定不动），而不是另建一份、让绑定
    继续指着那份旧画像。"""
    if requested:
        profile = session.get(StyleReferenceProfile, str(requested))
        if profile is None:
            raise profile_not_found(requested)
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


def start_learn_job(
    session: Session,
    book_id: str,
    *,
    profile_id: str | None = None,
    force: bool = False,
    resume: bool = False,
    retag: bool = False,
    mode: str = MODE_QUICK,
    reread: bool = False,
    op_key: str | None = None,
    llm_client: Any | None = None,
) -> StyleReferenceJob:
    """建（或续）这本书的学习作业（调用方提交后派发）。检查见模块文档与各错误码。

    ``retag``：给全书每个窗口重打标签（缺省只补标签版本不是当前版本的窗口）；``mode``：``quick`` 快速学习 /
    ``full`` 通读全书（续跑沿用原作业的方式）；``reread``：通读全书时不沿用上一次读完的通读。"""
    mode = MODE_FULL if mode == MODE_FULL else MODE_QUICK
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
    # 节点路由 / 提示词模板缺失:建作业之前就 409(不建一个注定在工人里失败的作业);作业开工时再查一次
    load_learn_runtimes()
    if mode == MODE_FULL:
        load_consolidate_runtime()
        load_situation_runtime()
        load_foreshadow_runtimes()
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
        if not learn_resumable(latest):
            # 上一次失败的原因续跑解决不了(正文太少、文风卡被滤空):放回队列只会原样再失败一次
            failure = dict(latest.error_json or {})
            failure_details = failure.get("details") if isinstance(failure.get("details"), Mapping) else {}
            raise DomainError(
                LEARN_NOTHING_TO_RESUME_CODE,
                "上一次学习失败的原因续跑解决不了,「继续学习」只会再失败一次;重新「学习文风」"
                "(正文太少时可以「仍然学习」)。",
                status_code=409,
                details={
                    "book_id": book_id,
                    "job_id": latest.job_id,
                    "reason": "not_retryable",
                    "reason_code": failure_details.get("reason_code"),
                    "author_action": {"action": "learn_style", "view": "styleref", "label": "重新学习", "book_id": book_id},
                },
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
            "mode": mode,
            "reread": bool(reread) and mode == MODE_FULL,
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
    # 排队中 / 工人已死的作业在请求里直接收尾;学习的 run 行由登记的收尾钩子(on_learn_cancelled)一并落定
    return StyleJobService(session).request_cancel(job.job_id)


def learn_resumable(job: StyleReferenceJob) -> bool:
    """能不能「继续学习」:取消 / 心跳过期的能;失败的看 ``error.retryable``——正文太少(input_too_small)、卡片被滤空
    这类失败续跑只会再失败一次,只能「重新学习」(或带 force「仍然学习」)。书载荷、活动条目与续跑请求共用这一条。"""
    error = job.error_json or {}
    stalled = job.state == STATE_RUNNING and heartbeat_is_stale(job.heartbeat_at)
    return (
        job.state == STATE_CANCELLED
        or stalled
        or (job.state == STATE_FAILED and error.get("retryable") is not False)
    )


def learn_payload(job: StyleReferenceJob | None) -> dict[str, Any] | None:
    """书载荷里的 ``learn``：最近一个学习作业的摘要（没有返回 None）。"""
    if job is None:
        return None
    cursor = dict(job.cursor_json or {})
    progress = dict(job.progress_json or {})
    error = dict(job.error_json) if job.error_json else None
    stalled = job.state == STATE_RUNNING and heartbeat_is_stale(job.heartbeat_at)
    resumable = learn_resumable(job)
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


def tag_plan_input(rows: Sequence[Any]) -> list[tuple[int, int]]:
    return [(int(w.window_no), int(w.chars or 0)) for w in rows]


def _full_estimate(
    session: Session,
    book: StyleReferenceBook,
    rows: Sequence[Any],
    *,
    layers: Sequence[str],
    tag_calls: int,
    marker: Mapping[str, Any],
) -> dict[str, Any] | None:
    """通读全书的估算：按窗口字数粗切批（作业按正文段精确切、按预算核，批数可能略多）；上一次通读多半能沿用时给
    ``reusable``（读于何时、归并能不能一起沿用——估算不知道切法细节，作业开工时按完整身份再判）。"""
    if not rows:
        return None
    metas = [window_meta(w) for w in rows]
    starts = sorted((m.start_index, m.chars) for m in metas)
    keys = [start for start, _chars in starts]

    def chars_of(start: int, end: int) -> int:
        return sum(chars for _s, chars in starts[bisect_left(keys, start) : bisect_right(keys, end)])

    spans = chapter_spans(metas)
    plan = plan_full_batches(spans, chars_of, max_chars=FULL_BATCH_MAX_CHARS)
    reusable: dict[str, Any] | None = None
    try:
        runtimes = load_learn_runtimes()
        consolidate = load_consolidate_runtime()
    except DomainError:
        runtimes, consolidate = {}, None
    if runtimes and consolidate is not None:
        signature = reading_signature(
            {
                "root": str(marker.get("root") or ""),
                "index_version": WINDOW_INDEX_VERSION,
                "plan_version": FULL_PLAN_VERSION,
                "layers": sorted(str(layer) for layer in layers),
                "prompts": {
                    str(EXTRACT_NODES[layer]): str(runtimes[EXTRACT_NODES[layer]].prompt_version)
                    for layer in sorted(layers)
                },
                "models": {
                    str(EXTRACT_NODES[layer]): list(runtimes[EXTRACT_NODES[layer]].route_key) for layer in sorted(layers)
                },
            }
        )
        found = find_reusable_reading(session, book.book_id, signature=signature)
        if found.read_run_id:
            reusable = {"read_at": found.read_at, "run_id": found.read_run_id}
    reading_calls = len(plan) * len(layers)
    # 按场面的写法：每类够常见的场面一次（按现有窗口标签估；还没打标签时按词表上限估）
    tagged = [w for w in rows if isinstance(w.tags_json, Mapping) and w.tags_json.get("situations")]
    counts: dict[str, int] = {}
    for w in tagged:
        for situation in set(w.tags_json.get("situations") or []):
            counts[str(situation)] = counts.get(str(situation), 0) + 1
    situation_calls = (
        sum(1 for n in counts.values() if n >= SITUATION_MIN_WINDOWS) if tagged else len(SITUATION_TAGS)
    )
    return {
        "batches": len(plan),
        "chapters": len(spans),
        "chars": sum(int(batch["chars"]) for batch in plan),
        "calls": {
            "extract": reading_calls,
            "consolidate": 16,
            "synthesize": 1,
            "protected": 1,
            "tags": tag_calls,
            "situations": situation_calls,
            "foreshadow": 2,
        },
        "est_calls": reading_calls + 16 + 2 + tag_calls + situation_calls + 2,
        "est_input_chars": sum(int(batch["chars"]) for batch in plan) * len(layers),
        "reusable": reusable,
    }


def estimate_learning(session: Session, book: StyleReferenceBook, *, retag: bool = False) -> dict[str, Any]:
    """学一次要多少次调用 / 多少字的输入（窗口索引是最新的才给出标签批数，否则按全书字数粗估）。

    标签只算还要打的窗口(``windows_to_tag``;全部是当前版本时 ``tags`` 批数为 0),``retag`` 按全书算。
    顶层是快速学习的估算；``full`` 是通读全书的（窗口索引还没建时为 None）。"""
    marker = index_marker(book.stats_json) or {}
    rows = load_windows(session, book.book_id) if marker else []
    to_tag = windows_needing_tags(rows, retag=retag)
    windows = [(int(w.window_no), int(w.chars or 0)) for w in to_tag]
    batches = plan_tag_batches(windows) if windows else []
    tag_chars = sum(min(chars, 3000) for _no, chars in windows)
    layers = [layer for layer in LAYERS if layer not in _skipped_layers(book, force=False)] or list(LAYERS)
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
        "full": _full_estimate(session, book, rows, layers=layers, tag_calls=len(batches), marker=marker),
    }


def set_run_status(session: Session, run_id: str | None, status: str) -> None:
    """学习的血缘 run 行随作业收尾（还在 running 的才改）：作业失败 / 取消时由处理器与取消钩子调用。"""
    if not run_id:
        return
    session.execute(
        update(StyleReferenceRun)
        .where(StyleReferenceRun.run_id == str(run_id), StyleReferenceRun.status == "running")
        .values(status=status, dispatch_state=RUN_DISPATCH_STATE, finished_at=utcnow(), heartbeat_at=utcnow())
        .execution_options(synchronize_session=False)
    )


__all__ = [
    "BOOK_CLASSIFYING_CODE",
    "CURSOR_VERSION",
    "LAYER_LABELS",
    "LEARN_ALREADY_ACTIVE_CODE",
    "LEARN_FAILED_CODE",
    "LEARN_NOTHING_TO_RESUME_CODE",
    "LEARN_NOT_ACTIVE_CODE",
    "LearnFailedError",
    "PHASE_LABELS",
    "PHASE_ORDER",
    "QUICK_PHASE_ORDER",
    "RUN_DISPATCH_STATE",
    "active_learn_job",
    "cancel_learn",
    "estimate_learning",
    "latest_learn_job",
    "learn_payload",
    "learn_resumable",
    "set_run_status",
    "start_learn_job",
    "tag_plan_input",
    "windows_needing_tags",
]

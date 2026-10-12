"""故事记忆：每场定稿之后把事实抽进事件日志，每章写完之后写一份章摘要（2026-10-12）。

起草后面的场次时模型看得到的远处只有两样，都从这里来：

- **事件日志的工作记忆**（人物状态、谁知道什么）：每一场的当前终稿归档之后（流水线、采纳、晋升哪条路径都一样），
  后台用作者「提取」的同一条路径（``CanonContinuityService.extract_scene_candidates``，整场分段抽）把持久的状态变化
  抽成候选。候选不必等作者逐条核对就进起草提示词（``narrative.replay.NarrativeEventStore.working_events``）；作者在
  成稿中心驳回的不认，核对完成的照旧成为正史（硬质检、成稿门只认正史）。
- **章摘要**（``ChapterDigest``，提示词里的「前情」，渲染见 ``story_memory_digest``）：一章「写完」——各活场都有当前
  终稿，或作者已经往后面的章写了——之后由 ``chapter_digest`` 节点写：发生了什么、章末停在哪、人物的持久变化，
  以及这一章对**线索台账**的更新。输入里给台账里至今还开着的线（``T1`` … 编号）与后面几章的计划
  （``story_memory_plan.upcoming_plan``）：模型说出这一章推进 / 兑现了哪几条、新埋下了哪几条、哪条计划在哪章兑现；
  存之前把编号换回稳定的 id（:func:`resolve_thread_updates`）。``source_hash`` 绑住整章正文、上一份摘要的内容与输入的
  台账：任一变了就判旧，后台按故事顺序重写；作者改过的（``origin = author``）不自动重写，只在列表里标「旧」。
- **线索标记**（``StoryThreadMark``）：作者可以把一条线「放下」（不再提醒、不进起草）或判它「已收」（:func:`mark_thread`）。
- **编辑通读**（``story_editorial``）：摘要都跟上之后，每写完几章由同一个后台工人顺带做一次。

后台工人（:func:`story_memory_tick`）挂在运行任务巡检线程的维护任务上，每分钟看一次；真正调模型的一轮
（:func:`run_story_memory_pass`）派到单线程守护车道里跑，不占巡检线程。没有可用的模型 → 什么也不做；一轮最多调
``MAX_LLM_CALLS_PER_PASS`` 次；进程要退出（工人代变了）就在两次调用之间停下。抽取失败的场每个进程最多自动重试
``MAX_EXTRACTION_ATTEMPTS`` 次（之后等作者在成稿中心按「提取」），同一章同一份来源的摘要也一样（之后等作者按「重写」）。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterDigest,
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
    StoryThreadMark,
)
from novel_system.services.background_jobs import close_daemon_lanes, current_worker_generation, daemon_lane
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMCallContext, mark_postprocess_failure, validate_product_call
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.maintenance import register_maintenance_task, unregister_maintenance_task
from novel_system.services.story_editorial import editorial_overview, editorial_review_due, generate_editorial_review
from novel_system.services.story_memory_llm import load_template, parse_json_object
from novel_system.services.story_memory_digest import (
    THREAD_MARK_STATUSES,
    StoryThread,
    active_digests,
    digest_content_hash,
    live_chapters_in_order,
    normalize_digest,
    project_threads,
    thread_alert,
    thread_marks,
)
from novel_system.services.story_memory_plan import upcoming_plan
from novel_system.services.story_memory_state import (
    ChapterMemoryState,
    chapter_display_title,
    chapter_memory_states,
    chapter_state,
    serialize_chapter_state,
)

logger = logging.getLogger(__name__)

CHAPTER_DIGEST_NODE = "chapter_digest"
CHAPTER_DIGEST_TEMPLATE = "chapter_digest"
CHAPTER_DIGEST_STEP = "story_memory:chapter_digest"

STORY_MEMORY_TASK = "story_memory"
STORY_MEMORY_LANE = "story_memory"
STORY_MEMORY_INTERVAL_SECONDS = 60.0
MAX_LLM_CALLS_PER_PASS = 12
MAX_EXTRACTION_ATTEMPTS = 3

# 抽取状态：归档时由 CanonContinuityService.mark_archive_pending 记下；抽取失败（服务报错 / 输出不合格）是 degraded
_EXTRACTION_PENDING = "pending_extraction"
_EXTRACTION_DEGRADED = "degraded"


def _serialize_thread(thread: StoryThread, numbers: Mapping[str, int], current_number: int) -> dict[str, Any]:
    payoff_number = numbers.get(thread.payoff_chapter_id or "")
    alert, dormant = thread_alert(thread, current_number, payoff_number)
    return {
        "thread_id": thread.thread_id,
        "thread": thread.label,
        "status": thread.status,
        "open": thread.is_open,
        "opened_in": thread.opened_number,
        "last_touched_in": thread.last_number,
        "resolved_in": thread.resolved_number,
        "planned_payoff_in": payoff_number,
        "alert": alert,
        "dormant_chapters": dormant,
        "mark": thread.mark,
        "mark_note": thread.mark_note,
        "history": list(thread.history),
    }


def thread_ledger_overview(session: Session, project_id: str) -> dict[str, Any]:
    """线索台账：还开着的（有提醒的在前）与已经收掉 / 放下的；「写到第几章」= 最后一章有摘要的章号。"""
    chapters = live_chapters_in_order(session, project_id)
    digests = active_digests(session, [chapter.chapter_id for chapter in chapters])
    threads, numbers = project_threads(session, project_id, chapters=chapters, digests=digests)
    current = max((numbers[chapter_id] for chapter_id in digests if chapter_id in numbers), default=0)
    rows = [_serialize_thread(thread, numbers, current) for thread in threads]
    open_rows = sorted(
        (row for row in rows if row["open"]),
        key=lambda row: (0 if row["alert"] else 1, -int(row["last_touched_in"] or 0)),
    )
    closed_rows = [row for row in rows if not row["open"]]
    return {
        "current_number": current,
        "open": open_rows,
        "closed": closed_rows,
        "alert_count": sum(1 for row in open_rows if row["alert"]),
    }


def story_memory_overview(session: Session, project_id: str) -> dict[str, Any]:
    """成稿中心「故事记忆」：各章的摘要与状态、线索台账、编辑通读，外加事实抽取还欠着几场。"""
    from novel_system.services.scene_lookup import require_project
    from novel_system.settings import get_settings

    require_project(session, project_id)
    states = chapter_memory_states(session, project_id)
    pending, degraded = _extraction_backlog(session, project_id)
    return {
        "project_id": project_id,
        "chapters": [serialize_chapter_state(state) for state in states],
        "threads": thread_ledger_overview(session, project_id),
        "editorial": editorial_overview(session, project_id, states),
        "extraction": {"pending_scene_count": len(pending), "degraded_scene_count": len(degraded)},
        "llm_enabled": bool(get_settings().llm_enabled),
    }


def _supersede_active(session: Session, chapter_id: str) -> None:
    for row in session.execute(
        select(ChapterDigest).where(ChapterDigest.chapter_id == chapter_id, ChapterDigest.status == "active")
    ).scalars():
        row.status = "superseded"


def _store_digest(
    session: Session,
    state: ChapterMemoryState,
    digest_json: Mapping[str, Any],
    *,
    origin: str,
    llm_call_id: str | None = None,
    prompt_version: str | None = None,
) -> ChapterDigest:
    _supersede_active(session, state.chapter.chapter_id)
    session.flush()
    normalized = normalize_digest(digest_json)
    row = ChapterDigest(
        row_id=f"chapter_digest_{uuid.uuid4().hex[:16]}",
        project_id=str(state.chapter.project_id),
        chapter_id=state.chapter.chapter_id,
        status="active",
        origin=origin,
        body_hash=state.body_hash,
        previous_digest_row_id=state.previous.row_id if state.previous else None,
        previous_digest_hash=state.previous.content_hash if state.previous else None,
        source_hash=state.source_hash,
        digest_json=normalized,
        content_hash=digest_content_hash(normalized),
        llm_call_id=llm_call_id,
        prompt_version=prompt_version,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------- 写一份章摘要（LLM）
def _character_names(session: Session, project_id: str) -> list[str]:
    names = session.execute(
        select(StoryCharacter.display_name)
        .where(StoryCharacter.project_id == project_id)
        .order_by(StoryCharacter.created_at.asc(), StoryCharacter.character_id.asc())
    ).scalars()
    return list(dict.fromkeys(str(name).strip() for name in names if name and str(name).strip()))


def digest_prompt_payload(session: Session, state: ChapterMemoryState) -> tuple[dict[str, Any], dict[int, str]]:
    """章摘要的输入，另给后面几章的章号 → 章 id（模型说「计划第几章兑现」时换回章 id）。"""
    previous = normalize_digest(state.previous.digest_json) if state.previous else None
    chapters = live_chapters_in_order(session, str(state.chapter.project_id))
    plan, plan_numbers = upcoming_plan(session, chapters, state.number)
    payload = {
        "chapter": {"number": state.number, "title": chapter_display_title(state.chapter), "text": state.body},
        "previous": (
            {"chapter_number": state.previous_number, "ending_state": previous["ending_state"]} if previous else {}
        ),
        "threads_so_far": [
            {key: row[key] for key in ("ref", "thread", "opened_in", "planned_payoff_in")} for row in state.thread_rows
        ],
        "upcoming_plan": plan,
        "characters": _character_names(session, str(state.chapter.project_id)),
    }
    return payload, plan_numbers


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_thread_updates(
    raw_threads: Any,
    *,
    chapter_id: str,
    thread_rows: list[dict[str, Any]],
    plan_numbers: Mapping[int, str],
) -> list[dict[str, Any]]:
    """模型给的线索更新 → 存进摘要的那一份：台账里的线（``ref`` 认得出）用它的 id 与原文字，状态只能是推进 / 兑现；
    认不出的当作这一章新埋下的线，按 ``<章 id>-T<n>`` 编号（这一章当场埋下又当场收掉的不进台账）。计划兑现的章号
    只认输入里给过的后面几章。"""
    aliases = {str(row["ref"]).upper(): row for row in thread_rows}
    updates: list[dict[str, Any]] = []
    used: set[str] = set()
    minted = 0
    for raw in raw_threads if isinstance(raw_threads, list) else []:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "").strip().lower()
        label = str(raw.get("thread") or "").strip()
        note = str(raw.get("note") or "").strip()
        payoff = plan_numbers.get(_int(raw.get("payoff_chapter")) or 0)
        row = aliases.get(str(raw.get("ref") or "").strip().upper())
        if row is not None:
            if row["thread_id"] in used:
                continue
            used.add(row["thread_id"])
            updates.append(
                {
                    "id": row["thread_id"],
                    "thread": row["thread"],
                    "status": status if status in {"advanced", "resolved"} else "advanced",
                    "note": note,
                    "payoff_chapter_id": payoff,
                }
            )
            continue
        if status == "resolved" or not label:
            continue
        minted += 1
        updates.append(
            {"id": f"{chapter_id}-T{minted}", "thread": label, "status": "opened", "note": note, "payoff_chapter_id": payoff}
        )
    return updates


def generate_chapter_digest(
    session: Session,
    project_id: str,
    chapter_id: str,
    *,
    runner: LLMNodeRunner | None = None,
) -> dict[str, Any]:
    """为一章写（或重写）摘要并存为当前那一份。fail-closed：模型没配好 409，调用失败 / 输出不合格 502。"""
    state = chapter_state(session, project_id, chapter_id)
    if not state.body.strip():
        raise DomainError(
            "CHAPTER_DIGEST_NOT_READY",
            "这一章还没有任何一场定稿的正文，写不了章摘要",
            status_code=409,
            details={"chapter_id": chapter_id, "retryable": False},
        )
    if runner is None:
        from novel_system.settings import get_settings

        if not get_settings().llm_enabled:
            raise DomainError(
                "CHAPTER_DIGEST_LLM_UNAVAILABLE",
                "还没有接入可用的模型，写不了章摘要：先到系统设置里配置模型",
                status_code=409,
                details={"chapter_id": chapter_id, "retryable": False},
            )
        runner = LLMNodeRunner(session)
    template = load_template(CHAPTER_DIGEST_TEMPLATE)
    context = LLMCallContext(
        scope_type="chapter",
        scope_id=chapter_id,
        node_id=CHAPTER_DIGEST_NODE,
        step=CHAPTER_DIGEST_STEP,
        project_id=project_id,
        chapter_id=chapter_id,
        provider_execution_mode=runner.provider_execution_mode,
    )
    payload, plan_numbers = digest_prompt_payload(session, state)
    prompt_text = f"{str(template.task_prompt or '').rstrip()}\n\n{json.dumps(payload, ensure_ascii=False)}"
    try:
        response = runner.run_task(
            task_name=CHAPTER_DIGEST_TEMPLATE,
            prompt_text=prompt_text,
            system_prompt=template.system_prompt,
            context=context,
            template=template,
        )
    except LLMNodeExecutionError as exc:
        raise_llm_domain_error(
            exc,
            capability_code="CHAPTER_DIGEST_LLM_UNAVAILABLE",
            failure_code="CHAPTER_DIGEST_LLM_FAILED",
            operation="Chapter digest",
            node_id=CHAPTER_DIGEST_NODE,
            next_action="configure_chapter_digest_route_or_retry",
            extra_details={"chapter_id": chapter_id},
        )
    llm_call_id = str(getattr(response, "llm_call_id", "") or "")
    parsed = parse_json_object(response)
    digest = None
    if parsed is not None:
        threads = resolve_thread_updates(
            parsed.get("threads"),
            chapter_id=chapter_id,
            thread_rows=state.thread_rows,
            plan_numbers=plan_numbers,
        )
        digest = normalize_digest({**parsed, "threads": threads})
    if digest is None or not digest["summary"]:
        if llm_call_id:
            mark_postprocess_failure(
                session,
                llm_call_id,
                error_code="CHAPTER_DIGEST_RESPONSE_INVALID",
                error_text="chapter digest response is not a JSON object with a summary",
            )
            session.commit()
        raise DomainError(
            "CHAPTER_DIGEST_RESPONSE_INVALID",
            "模型返回的章摘要不合格（不是带「summary」的 JSON），这一次没有存；可以再试一次",
            status_code=502,
            details={"chapter_id": chapter_id, "llm_call_id": llm_call_id or None, "retryable": True},
        )
    validate_product_call(session, llm_call_id, context, expected_outcome="completed")
    row = _store_digest(
        session,
        state,
        digest,
        origin="llm",
        llm_call_id=llm_call_id,
        prompt_version=getattr(template, "version", None),
    )
    return serialize_chapter_state(chapter_state(session, project_id, row.chapter_id))


def save_author_digest(
    session: Session,
    project_id: str,
    chapter_id: str,
    *,
    base_row_id: str | None,
    digest: Mapping[str, Any],
) -> dict[str, Any]:
    """作者改过的摘要存成当前那一份（``origin = author``：之后正文再变也不自动重写，只标「旧」）。

    作者改的是文字（发生了什么、章末、人物变化）；这一章对线索台账的更新原样带过来（线索用标记管，见
    :func:`mark_thread`）。``base_row_id`` 是作者打开编辑时的那一份；这期间后台或别的窗口换了一份 → 409
    ``CHAPTER_DIGEST_CHANGED``。
    """
    state = chapter_state(session, project_id, chapter_id)
    current = state.digest.row_id if state.digest else None
    if (base_row_id or None) != current:
        raise DomainError(
            "CHAPTER_DIGEST_CHANGED",
            "这一章的摘要在你编辑期间换了一份（后台重写或别的窗口改过）：刷新后再改",
            status_code=409,
            details={"chapter_id": chapter_id, "current_digest_row_id": current},
        )
    carried = normalize_digest(state.digest.digest_json)["threads"] if state.digest else []
    normalized = normalize_digest({**dict(digest), "threads": carried})
    if not normalized["summary"]:
        raise DomainError(
            "CHAPTER_DIGEST_SUMMARY_REQUIRED",
            "章摘要的「发生了什么」不能为空",
            status_code=422,
            details={"chapter_id": chapter_id},
        )
    row = _store_digest(session, state, normalized, origin="author")
    return serialize_chapter_state(chapter_state(session, project_id, row.chapter_id))


def mark_thread(session: Session, project_id: str, thread_id: str, *, status: str, note: str = "") -> dict[str, Any]:
    """作者标记台账里的一条线：``dropped``（放下）/ ``resolved``（已收）/ ``open``（取消标记）。返回新的台账。"""
    from novel_system.services.scene_lookup import require_project

    require_project(session, project_id)
    threads, _numbers = project_threads(session, project_id)
    thread = next((item for item in threads if item.thread_id == thread_id), None)
    if thread is None:
        raise DomainError(
            "STORY_THREAD_NOT_FOUND",
            "线索台账里没有这条线（可能所在章的摘要刚被重写）：刷新后再试",
            status_code=404,
            details={"thread_id": thread_id},
        )
    existing = thread_marks(session, project_id).get(thread_id)
    if status == "open":
        if existing is not None:
            session.delete(existing)
    elif status in THREAD_MARK_STATUSES:
        if existing is None:
            existing = StoryThreadMark(
                row_id=f"thread_mark_{uuid.uuid4().hex[:16]}", project_id=project_id, thread_id=thread_id, status=status
            )
            session.add(existing)
        existing.status = status
        existing.label_snapshot = thread.label
        existing.note = str(note or "").strip()
    else:
        raise DomainError(
            "STORY_THREAD_MARK_INVALID", "不认得的线索标记：只能放下、判为已收或取消标记", status_code=422, details={"status": status}
        )
    session.flush()
    return thread_ledger_overview(session, project_id)


# ---------------------------------------------------------------------- 后台工人
_PASS_LOCK = threading.Lock()
_EXTRACTION_ATTEMPTS: dict[str, int] = {}
# 同一章同一份来源（source_hash）每个进程最多自动写这么多次：模型一直交不合格的摘要时不每分钟烧一次钱
_DIGEST_ATTEMPTS: dict[tuple[str, str], int] = {}
# 编辑通读同理：同一部作品同一份输入（ReviewScope.source_hash）
_REVIEW_ATTEMPTS: dict[tuple[str, str], int] = {}


def _extraction_backlog(session: Session, project_id: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """还欠抽取的场（按故事顺序）：(scene_id, 当前终稿行 id)；分成「等抽取」与「上次失败」两张表。"""
    rows = session.execute(
        select(SceneCard.scene_id, SceneRunState.narrative_sync_status, SceneRunState.narrative_sync_final_scene_row_id)
        .join(SceneRunState, SceneRunState.scene_id == SceneCard.scene_id)
        .join(ChapterGoal, ChapterGoal.chapter_id == SceneCard.chapter_id)
        .where(
            SceneCard.project_id == project_id,
            SceneCard.trashed_flag == 0,
            ChapterGoal.trashed_flag == 0,
            SceneRunState.narrative_sync_status.in_((_EXTRACTION_PENDING, _EXTRACTION_DEGRADED)),
            SceneRunState.narrative_sync_final_scene_row_id.is_not(None),
        )
        .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc(), SceneCard.scene_seq.asc())
    ).all()
    finals = {
        row_id
        for row_id in session.execute(
            select(FinalScene.row_id).where(FinalScene.row_id.in_([row[2] for row in rows]))
        ).scalars()
    }
    pending = [(scene_id, final_id) for scene_id, status, final_id in rows if status == _EXTRACTION_PENDING and final_id in finals]
    degraded = [(scene_id, final_id) for scene_id, status, final_id in rows if status == _EXTRACTION_DEGRADED and final_id in finals]
    return pending, degraded


def _project_ids(session: Session) -> list[str]:
    """有活章、自己不在回收站里的作品。"""
    return list(
        session.execute(
            select(ChapterGoal.project_id)
            .join(StoryProject, StoryProject.project_id == ChapterGoal.project_id)
            .where(ChapterGoal.trashed_flag == 0, StoryProject.trashed_flag == 0)
            .distinct()
            .order_by(ChapterGoal.project_id.asc())
        ).scalars()
    )


def _capability_missing(exc: DomainError) -> bool:
    return exc.status_code == 409 and exc.code in {
        "CHAPTER_DIGEST_LLM_UNAVAILABLE",
        "EDITORIAL_REVIEW_LLM_UNAVAILABLE",
        "LLM_DISABLED_FOR_CANON_EXTRACTION",
        "LLM_ROUTE_NOT_CONFIGURED",
    }


def run_story_memory_pass(
    *,
    session_factory: Callable[[], Session] | None = None,
    max_llm_calls: int = MAX_LLM_CALLS_PER_PASS,
) -> dict[str, Any]:
    """后台一轮：先补抽各场的事实（故事顺序），再按故事顺序补写 / 重写章摘要，摘要都跟上了再看要不要编辑通读。
    一次只跑一轮。"""
    from novel_system.services.canon_continuity import CanonContinuityService

    if session_factory is None:
        from novel_system.db.session import SessionLocal

        session_factory = SessionLocal
    summary: dict[str, Any] = {"extracted": [], "digested": [], "reviewed": [], "failed": [], "stopped": None}
    if not _PASS_LOCK.acquire(blocking=False):
        summary["stopped"] = "already_running"
        return summary
    generation = current_worker_generation()
    calls = 0
    try:
        with session_factory() as session:
            for project_id in _project_ids(session):
                pending, degraded = _extraction_backlog(session, project_id)
                retry = [item for item in degraded if _EXTRACTION_ATTEMPTS.get(item[1], 0) < MAX_EXTRACTION_ATTEMPTS]
                for scene_id, final_id in [*pending, *retry]:
                    if calls >= max_llm_calls or current_worker_generation() != generation:
                        summary["stopped"] = "budget" if calls >= max_llm_calls else "shutdown"
                        return summary
                    calls += 1
                    _EXTRACTION_ATTEMPTS[final_id] = _EXTRACTION_ATTEMPTS.get(final_id, 0) + 1
                    try:
                        result = CanonContinuityService(session).extract_scene_candidates(project_id, scene_id)
                        session.commit()
                        # 抽取失败（供应商 / 解析）不抛错，只把场记成 degraded：照实记进 failed，不算「抽完了」
                        if (result.get("scene") or {}).get("status") == _EXTRACTION_DEGRADED:
                            summary["failed"].append({"scene_id": scene_id, "code": "EXTRACTION_DEGRADED"})
                        else:
                            summary["extracted"].append(scene_id)
                    except DomainError as exc:
                        session.rollback()
                        summary["failed"].append({"scene_id": scene_id, "code": exc.code})
                        if _capability_missing(exc):
                            summary["stopped"] = exc.code
                            return summary
                    except Exception:  # noqa: BLE001 — 一场抽不成不挡别的场
                        session.rollback()
                        logger.exception("story memory: extraction failed for scene %s", scene_id)
                        summary["failed"].append({"scene_id": scene_id, "code": "UNEXPECTED"})
                attempted: set[str] = set()
                digests_caught_up = True
                while True:
                    target = next(
                        (
                            state
                            for state in chapter_memory_states(session, project_id)
                            if state.auto_target
                            and state.chapter.chapter_id not in attempted
                            and _DIGEST_ATTEMPTS.get((state.chapter.chapter_id, state.source_hash), 0)
                            < MAX_EXTRACTION_ATTEMPTS
                        ),
                        None,
                    )
                    if target is None:
                        break
                    if calls >= max_llm_calls or current_worker_generation() != generation:
                        summary["stopped"] = "budget" if calls >= max_llm_calls else "shutdown"
                        return summary
                    calls += 1
                    attempted.add(target.chapter.chapter_id)
                    attempt_key = (target.chapter.chapter_id, target.source_hash)
                    _DIGEST_ATTEMPTS[attempt_key] = _DIGEST_ATTEMPTS.get(attempt_key, 0) + 1
                    try:
                        generate_chapter_digest(session, project_id, target.chapter.chapter_id)
                        session.commit()
                        summary["digested"].append(target.chapter.chapter_id)
                    except DomainError as exc:
                        session.rollback()
                        summary["failed"].append({"chapter_id": target.chapter.chapter_id, "code": exc.code})
                        if _capability_missing(exc):
                            summary["stopped"] = exc.code
                            return summary
                        # 一章没写成：后面的章拿不到它的线索清单，这一轮先不往后写
                        digests_caught_up = False
                        break
                    except Exception:  # noqa: BLE001 — 一章写不成不带走工人
                        session.rollback()
                        logger.exception("story memory: chapter digest failed for %s", target.chapter.chapter_id)
                        summary["failed"].append({"chapter_id": target.chapter.chapter_id, "code": "UNEXPECTED"})
                        digests_caught_up = False
                        break
                if not digests_caught_up:
                    continue
                # 摘要都跟上了：到了该通读的时候就顺带读一次（同一份输入每个进程最多自动试 MAX_EXTRACTION_ATTEMPTS 次）
                scope = editorial_review_due(session, project_id, chapter_memory_states(session, project_id))
                if scope is None:
                    continue
                review_key = (project_id, scope.source_hash)
                if _REVIEW_ATTEMPTS.get(review_key, 0) >= MAX_EXTRACTION_ATTEMPTS:
                    continue
                if calls >= max_llm_calls or current_worker_generation() != generation:
                    summary["stopped"] = "budget" if calls >= max_llm_calls else "shutdown"
                    return summary
                calls += 1
                _REVIEW_ATTEMPTS[review_key] = _REVIEW_ATTEMPTS.get(review_key, 0) + 1
                try:
                    generate_editorial_review(session, project_id, origin="auto")
                    session.commit()
                    summary["reviewed"].append(project_id)
                except DomainError as exc:
                    session.rollback()
                    summary["failed"].append({"project_id": project_id, "code": exc.code})
                    if _capability_missing(exc):
                        summary["stopped"] = exc.code
                        return summary
                except Exception:  # noqa: BLE001 — 通读做不成不带走工人
                    session.rollback()
                    logger.exception("story memory: editorial review failed for %s", project_id)
                    summary["failed"].append({"project_id": project_id, "code": "UNEXPECTED"})
        return summary
    finally:
        _PASS_LOCK.release()


def story_memory_tick() -> bool:
    """维护任务的一拍：有可用的模型、上一轮已经跑完 → 往故事记忆车道派一轮（不在巡检线程里调模型）。"""
    from novel_system.settings import get_settings

    try:
        if not get_settings().llm_enabled:
            return False
    except Exception:  # noqa: BLE001 — 读设置失败（库忙）下一拍再看
        logger.warning("story memory: settings unavailable, skipping this tick", exc_info=True)
        return False
    if _PASS_LOCK.locked():
        return False
    lane = daemon_lane(STORY_MEMORY_LANE, max_workers=1)
    if lane.pending():
        return False
    return lane.submit(run_story_memory_pass)


def install_story_memory_worker() -> None:
    """登记维护任务（lifespan 在库结构跟上之后调；重复调用只是覆盖同名登记）。启动那一拍不跑，等满一个间隔。"""
    register_maintenance_task(
        STORY_MEMORY_TASK,
        story_memory_tick,
        interval_seconds=STORY_MEMORY_INTERVAL_SECONDS,
        run_at_start=False,
    )


def uninstall_story_memory_worker() -> None:
    """lifespan 结束时：撤掉维护任务、丢下车道里还没开始的一轮（正在跑的那一轮看到工人代变了，在下一次调用之前停下）。"""
    unregister_maintenance_task(STORY_MEMORY_TASK)
    close_daemon_lanes(STORY_MEMORY_LANE)


__all__ = [
    "CHAPTER_DIGEST_NODE",
    "ChapterMemoryState",
    "chapter_memory_states",
    "generate_chapter_digest",
    "install_story_memory_worker",
    "mark_thread",
    "resolve_thread_updates",
    "run_story_memory_pass",
    "save_author_digest",
    "story_memory_overview",
    "story_memory_tick",
    "thread_ledger_overview",
    "uninstall_story_memory_worker",
]

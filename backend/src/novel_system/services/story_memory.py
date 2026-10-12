"""故事记忆：每场定稿之后把事实抽进事件日志，每章写完之后写一份章摘要（2026-10-12）。

起草后面的场次时模型看得到的远处只有两样，都从这里来：

- **事件日志的工作记忆**（人物状态、谁知道什么）：每一场的当前终稿归档之后（流水线、采纳、晋升哪条路径都一样），
  后台用作者「提取」的同一条路径（``CanonContinuityService.extract_scene_candidates``，整场分段抽）把持久的状态变化
  抽成候选。候选不必等作者逐条核对就进起草提示词（``narrative.replay.NarrativeEventStore.working_events``）；作者在
  成稿中心驳回的不认，核对完成的照旧成为正史（硬质检、成稿门只认正史）。
- **章摘要**（``ChapterDigest``，提示词里的「前情」，渲染见 ``story_memory_digest``）：一章「写完」——各活场都有当前
  终稿，或作者已经往后面的章写了——之后由 ``chapter_digest`` 节点写：发生了什么、章末停在哪、人物的持久变化、
  还没收的线。未收的线一章章往下带：输入里给上一份摘要的线索清单。``source_hash`` 绑住整章正文与上一份摘要的内容：
  任一变了就判旧，后台按故事顺序重写；作者改过的（``origin = author``）不自动重写，只在列表里标「旧」。

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
from dataclasses import dataclass
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
)
from novel_system.services.background_jobs import close_daemon_lanes, current_worker_generation, daemon_lane
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_plain
from novel_system.services.llm_accounting import LLMCallContext, mark_postprocess_failure, validate_product_call
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.maintenance import register_maintenance_task, unregister_maintenance_task
from novel_system.services.scene_lookup import require_project_chapter
from novel_system.services.story_memory_digest import (
    active_digests,
    chapter_display_title,
    digest_content_hash,
    live_chapters_in_order,
    normalize_digest,
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


# ---------------------------------------------------------------------- 章摘要的状态
@dataclass(slots=True)
class ChapterMemoryState:
    """一章此刻的故事记忆状态（只读推导）。"""

    chapter: ChapterGoal
    number: int
    body: str
    body_hash: str
    scene_count: int
    written_scene_count: int
    eligible: bool
    previous: ChapterDigest | None
    previous_number: int | None
    source_hash: str
    digest: ChapterDigest | None

    @property
    def status(self) -> str:
        """``not_ready``（还没写完）/ ``missing`` / ``fresh`` / ``stale``（正文或上一份摘要变了）。"""
        if self.digest is None:
            return "missing" if self.eligible else "not_ready"
        return "fresh" if self.digest.source_hash == self.source_hash else "stale"

    @property
    def auto_target(self) -> bool:
        """后台该写这一章：写完了、没有摘要，或摘要旧了而且不是作者改过的。"""
        if not self.eligible:
            return False
        status = self.status
        return status == "missing" or (status == "stale" and self.digest is not None and self.digest.origin == "llm")


def _source_hash(body_hash: str, previous: ChapterDigest | None) -> str:
    return sha256_json_plain({"body": body_hash, "previous": previous.content_hash if previous else None})


def chapter_memory_states(session: Session, project_id: str) -> list[ChapterMemoryState]:
    """作品各活章按故事顺序的故事记忆状态。

    一章「写完」= 至少一场有当前终稿，且各活场都有，或后面某一章已经有终稿（作者往后写了，这一章缺的场不再等）。
    上一份摘要 = 往前最近一章的当前摘要（中间没写的章跳过）。
    """
    chapters = live_chapters_in_order(session, project_id)
    if not chapters:
        return []
    manuscripts = ChapterManuscriptService(session)
    bodies = [manuscripts.assembled_body(chapter.chapter_id) for chapter in chapters]
    digests = active_digests(session, [chapter.chapter_id for chapter in chapters])
    later_written = [False] * len(chapters)
    seen = False
    for index in range(len(chapters) - 1, -1, -1):
        later_written[index] = seen
        seen = seen or bool(bodies[index]["generated_scene_count"])
    states: list[ChapterMemoryState] = []
    previous: ChapterDigest | None = None
    previous_number: int | None = None
    for index, (chapter, body) in enumerate(zip(chapters, bodies)):
        written = int(body["generated_scene_count"] or 0)
        complete = written > 0 and not body["missing_scene_ids"]
        digest = digests.get(chapter.chapter_id)
        states.append(
            ChapterMemoryState(
                chapter=chapter,
                number=index + 1,
                body=str(body["content"] or ""),
                body_hash=str(body["body_hash"] or ""),
                scene_count=int(body["scene_count"] or 0),
                written_scene_count=written,
                eligible=written > 0 and (complete or later_written[index]),
                previous=previous,
                previous_number=previous_number,
                source_hash=_source_hash(str(body["body_hash"] or ""), previous),
                digest=digest,
            )
        )
        if digest is not None:
            previous, previous_number = digest, index + 1
    return states


def _serialize_state(state: ChapterMemoryState) -> dict[str, Any]:
    digest = state.digest
    return {
        "chapter_id": state.chapter.chapter_id,
        "number": state.number,
        "title": chapter_display_title(state.chapter),
        "status": state.status,
        "eligible": state.eligible,
        "scene_count": state.scene_count,
        "written_scene_count": state.written_scene_count,
        "digest_row_id": digest.row_id if digest else None,
        "origin": digest.origin if digest else None,
        "updated_at": digest.created_at if digest else None,
        "digest": normalize_digest(digest.digest_json) if digest else None,
    }


def story_memory_overview(session: Session, project_id: str) -> dict[str, Any]:
    """成稿中心「故事记忆」：各章的摘要与状态，外加事实抽取还欠着几场。"""
    from novel_system.services.scene_lookup import require_project
    from novel_system.settings import get_settings

    require_project(session, project_id)
    states = chapter_memory_states(session, project_id)
    pending, degraded = _extraction_backlog(session, project_id)
    return {
        "project_id": project_id,
        "chapters": [_serialize_state(state) for state in states],
        "extraction": {"pending_scene_count": len(pending), "degraded_scene_count": len(degraded)},
        "llm_enabled": bool(get_settings().llm_enabled),
    }


def _state_for(session: Session, project_id: str, chapter_id: str) -> ChapterMemoryState:
    require_project_chapter(session, project_id, chapter_id)
    for state in chapter_memory_states(session, project_id):
        if state.chapter.chapter_id == chapter_id:
            return state
    raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)


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
def _template() -> Any:
    from novel_system.services.prompt_builder import load_prompt_templates

    template = load_prompt_templates().get(CHAPTER_DIGEST_TEMPLATE)
    if template is None:
        raise LookupError(f"prompt template {CHAPTER_DIGEST_TEMPLATE!r} is missing from config/prompts.yaml")
    return template


def _character_names(session: Session, project_id: str) -> list[str]:
    names = session.execute(
        select(StoryCharacter.display_name)
        .where(StoryCharacter.project_id == project_id)
        .order_by(StoryCharacter.created_at.asc(), StoryCharacter.character_id.asc())
    ).scalars()
    return list(dict.fromkeys(str(name).strip() for name in names if name and str(name).strip()))


def digest_prompt_payload(session: Session, state: ChapterMemoryState) -> dict[str, Any]:
    previous = normalize_digest(state.previous.digest_json) if state.previous else None
    return {
        "chapter": {"number": state.number, "title": chapter_display_title(state.chapter), "text": state.body},
        "previous": (
            {
                "chapter_number": state.previous_number,
                "ending_state": previous["ending_state"],
                "open_threads": previous["open_threads"],
            }
            if previous
            else {}
        ),
        "characters": _character_names(session, str(state.chapter.project_id)),
    }


def _parse(response: Any) -> dict[str, Any] | None:
    structured = getattr(response, "structured_output", None)
    if isinstance(structured, dict) and structured:
        return structured
    raw = str(getattr(response, "text", "") or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        parsed = json.loads(raw.strip())
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def generate_chapter_digest(
    session: Session,
    project_id: str,
    chapter_id: str,
    *,
    runner: LLMNodeRunner | None = None,
) -> dict[str, Any]:
    """为一章写（或重写）摘要并存为当前那一份。fail-closed：模型没配好 409，调用失败 / 输出不合格 502。"""
    state = _state_for(session, project_id, chapter_id)
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
    template = _template()
    context = LLMCallContext(
        scope_type="chapter",
        scope_id=chapter_id,
        node_id=CHAPTER_DIGEST_NODE,
        step=CHAPTER_DIGEST_STEP,
        project_id=project_id,
        chapter_id=chapter_id,
        provider_execution_mode=runner.provider_execution_mode,
    )
    payload = digest_prompt_payload(session, state)
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
    parsed = _parse(response)
    digest = normalize_digest(parsed) if parsed is not None else None
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
    return _serialize_state(_state_for(session, project_id, row.chapter_id))


def save_author_digest(
    session: Session,
    project_id: str,
    chapter_id: str,
    *,
    base_row_id: str | None,
    digest: Mapping[str, Any],
) -> dict[str, Any]:
    """作者改过的摘要存成当前那一份（``origin = author``：之后正文再变也不自动重写，只标「旧」）。

    ``base_row_id`` 是作者打开编辑时的那一份；这期间后台或别的窗口换了一份 → 409 ``CHAPTER_DIGEST_CHANGED``。
    """
    state = _state_for(session, project_id, chapter_id)
    current = state.digest.row_id if state.digest else None
    if (base_row_id or None) != current:
        raise DomainError(
            "CHAPTER_DIGEST_CHANGED",
            "这一章的摘要在你编辑期间换了一份（后台重写或别的窗口改过）：刷新后再改",
            status_code=409,
            details={"chapter_id": chapter_id, "current_digest_row_id": current},
        )
    normalized = normalize_digest(digest)
    if not normalized["summary"]:
        raise DomainError(
            "CHAPTER_DIGEST_SUMMARY_REQUIRED",
            "章摘要的「发生了什么」不能为空",
            status_code=422,
            details={"chapter_id": chapter_id},
        )
    row = _store_digest(session, state, normalized, origin="author")
    return _serialize_state(_state_for(session, project_id, row.chapter_id))


# ---------------------------------------------------------------------- 后台工人
_PASS_LOCK = threading.Lock()
_EXTRACTION_ATTEMPTS: dict[str, int] = {}
# 同一章同一份来源（source_hash）每个进程最多自动写这么多次：模型一直交不合格的摘要时不每分钟烧一次钱
_DIGEST_ATTEMPTS: dict[tuple[str, str], int] = {}


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
        "LLM_DISABLED_FOR_CANON_EXTRACTION",
        "LLM_ROUTE_NOT_CONFIGURED",
    }


def run_story_memory_pass(
    *,
    session_factory: Callable[[], Session] | None = None,
    max_llm_calls: int = MAX_LLM_CALLS_PER_PASS,
) -> dict[str, Any]:
    """后台一轮：先补抽各场的事实（故事顺序），再按故事顺序补写 / 重写章摘要。一次只跑一轮。"""
    from novel_system.services.canon_continuity import CanonContinuityService

    if session_factory is None:
        from novel_system.db.session import SessionLocal

        session_factory = SessionLocal
    summary: dict[str, Any] = {"extracted": [], "digested": [], "failed": [], "stopped": None}
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
                        CanonContinuityService(session).extract_scene_candidates(project_id, scene_id)
                        session.commit()
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
                        break
                    except Exception:  # noqa: BLE001 — 一章写不成不带走工人
                        session.rollback()
                        logger.exception("story memory: chapter digest failed for %s", target.chapter.chapter_id)
                        summary["failed"].append({"chapter_id": target.chapter.chapter_id, "code": "UNEXPECTED"})
                        break
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
    "run_story_memory_pass",
    "save_author_digest",
    "story_memory_overview",
    "story_memory_tick",
    "uninstall_story_memory_worker",
]

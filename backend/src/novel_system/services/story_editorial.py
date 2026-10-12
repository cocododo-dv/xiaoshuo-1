"""编辑通读（2026-10-12）：每写完几章，AI 像编辑一样把全书读一遍，指出问题并给后面几章写意见。

长篇写到中途最容易出的毛病，单看一场看不出来：人物弧停在原地、开头许下的承诺迟迟不兑现、几条线冷了、节奏连着几章
一个样、正文已经偏离了构思（后面几章的设计还按原计划假设）。``editorial_review`` 节点读：

- 全书设计（一句话、五句脊柱、道德前提、长篇大纲的段落）与主要人物的目标、顿悟、人物弧、恐惧与希望（04 / 08），外加
  各章摘要里记下的人物变化；
- 各章摘要、线索台账里还开着的线（带「冷了」「计划兑现的章已过」的提醒）、最近几章的正文（最多
  ``RECENT_TEXT_MAX_CHARS`` 字）；
- 后面 ``WINDOW_CHAPTERS`` 章的计划（章目标与每场的整段场景结构）与上一份通读里采纳过的意见。

写出一段总评、问题清单（``review_json.findings``：类型、轻重、涉及的章、问题、建议；「正文偏离计划」的建议是改构思，
作者自己去改）与给后面几章的意见（``EditorialGuidance``，针对其中一章或整段）。作者采纳（可以改字）的意见进那几章
各场的起草与场景蓝图（``editorial_guidance``）；采纳、改字或撤销都会让那几章还没写的场的场景蓝图作废，下一次运行
按新意见重新规划。

什么时候读：章摘要都跟上（没有后台还欠着的章）之后，读到的最后一章比上一份通读往后多了 ``REVIEW_INTERVAL_CHAPTERS``
章、后面还有章，后台工人（``story_memory.run_story_memory_pass``）顺带做一次（:func:`editorial_review_due`）；作者也可以
在成稿中心「记忆」里随时让它现在读。新的一份存下时旧的作废，旧份里还没决定的意见随之作废，已采纳的照旧生效。
fail-closed：模型没配好 409、调用失败 / 输出不合格 502。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    EditorialGuidance,
    EditorialReview,
    SceneRunState,
    StoryCharacter,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_plain
from novel_system.services.llm_accounting import LLMCallContext, mark_postprocess_failure, validate_product_call
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.scene_planning_staleness import supersede_scene_planning_artifacts
from novel_system.services.snowflake_queries import latest_step_run
from novel_system.services.story_memory_digest import live_chapters_in_order, normalize_digest, project_threads, thread_alert
from novel_system.services.story_memory_llm import load_template, parse_json_object
from novel_system.services.story_memory_plan import chapter_scenes, window_plan
from novel_system.services.story_memory_state import ChapterMemoryState, chapter_display_title, chapter_memory_states

EDITORIAL_REVIEW_NODE = "editorial_review"
EDITORIAL_REVIEW_TEMPLATE = "editorial_review"
EDITORIAL_REVIEW_STEP = "story_memory:editorial_review"

# 每往后写这么多章读一次；意见针对后面这么多章
REVIEW_INTERVAL_CHAPTERS = 3
WINDOW_CHAPTERS = 3
# 最近几章的正文最多带这么多字（从最后一章往前，整章整章地带；最后一章本身太长就只带它的后半）
RECENT_TEXT_MAX_CHARS = 30000
LONG_SYNOPSIS_MAX_CHARS = 6000
# 更早的章只带摘要（不带章末与人物变化），免得书写长了输入撑破
FULL_DIGEST_CHAPTERS = 12

FINDING_KINDS: tuple[str, ...] = (
    "arc_stall",
    "promise_unpaid",
    "thread_cold",
    "pacing",
    "continuity",
    "plan_drift",
    "repetition",
    "other",
)
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
GUIDANCE_DECISIONS: tuple[str, ...] = ("accept", "dismiss")
_CANON_STATUSES = ("approved", "stale")
_FINDING_LIMIT = 20
_GUIDANCE_LIMIT = 12


# ---------------------------------------------------------------------- 读到哪、什么时候读
@dataclass(slots=True)
class ReviewScope:
    """一次通读的范围：读到第 ``through_index`` 章（下标），意见针对 ``window_indexes`` 这几章。"""

    through_index: int
    window_indexes: list[int]
    source_hash: str


def review_scope(states: Sequence[ChapterMemoryState]) -> ReviewScope | None:
    """读到哪一章：从头往后，遇到后台还欠着摘要的章（缺、或旧了且不是作者改过的）就停；停之前最后一章有摘要的。"""
    through: int | None = None
    for index, state in enumerate(states):
        if state.auto_target:
            break
        if state.digest is not None:
            through = index
    if through is None:
        return None
    window = list(range(through + 1, min(len(states), through + 1 + WINDOW_CHAPTERS)))
    source_hash = sha256_json_plain(
        {
            "digests": [state.digest.content_hash for state in states[: through + 1] if state.digest is not None],
            "window": [states[index].chapter.chapter_id for index in window],
        }
    )
    return ReviewScope(through_index=through, window_indexes=window, source_hash=source_hash)


def latest_review(session: Session, project_id: str) -> EditorialReview | None:
    return session.execute(
        select(EditorialReview)
        .where(EditorialReview.project_id == project_id, EditorialReview.status == "active")
        .order_by(EditorialReview.created_at.desc(), EditorialReview.review_id.desc())
    ).scalars().first()


def _last_through_number(states: Sequence[ChapterMemoryState], latest: EditorialReview | None) -> int:
    if latest is None:
        return 0
    return next((state.number for state in states if state.chapter.chapter_id == latest.through_chapter_id), 0)


def editorial_review_due(session: Session, project_id: str, states: Sequence[ChapterMemoryState]) -> ReviewScope | None:
    """后台该不该现在读：读得到的最后一章比上一份通读往后多了 ``REVIEW_INTERVAL_CHAPTERS`` 章、后面还有章、读的东西变了。"""
    scope = review_scope(states)
    if scope is None or not scope.window_indexes:
        return None
    latest = latest_review(session, project_id)
    if scope.through_index + 1 - _last_through_number(states, latest) < REVIEW_INTERVAL_CHAPTERS:
        return None
    if latest is not None and latest.source_hash == scope.source_hash:
        return None
    return scope


# ---------------------------------------------------------------------- 输入
def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, limit: int | None = None) -> str:
    text = "" if value is None or isinstance(value, (list, dict)) else str(value).strip()
    return text[:limit] if limit is not None else text


def _canon_drafts(session: Session, project_id: str) -> dict[str, dict[str, Any]]:
    drafts: dict[str, dict[str, Any]] = {}
    for step_key in ("one_sentence_summary", "one_paragraph_summary", "long_synopsis", "character_sheets", "character_bibles"):
        run = latest_step_run(session, project_id, step_key, statuses=_CANON_STATUSES)
        if run is not None and isinstance(run.draft_json, dict):
            drafts[step_key] = run.draft_json
    return drafts


def _book(canon: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    paragraph = canon.get("one_paragraph_summary") or {}
    synopsis = " ".join(_text(item) for item in _as_list((canon.get("long_synopsis") or {}).get("paragraphs")) if _text(item))
    return {
        "logline": _text((canon.get("one_sentence_summary") or {}).get("summary")),
        "spine": [_text(item) for item in _as_list(paragraph.get("sentences")) if _text(item)],
        "moral_premise": _text(paragraph.get("moral_premise")),
        "long_synopsis": synopsis[:LONG_SYNOPSIS_MAX_CHARS],
    }


def _find(items: list[dict[str, Any]], character_id: str, name: str) -> dict[str, Any]:
    for item in items:
        if character_id and _text(item.get("character_id")) == character_id:
            return item
    for item in items:
        if name and (_text(item.get("display_name")) == name or _text(item.get("name")) == name):
            return item
    return {}


def _characters(
    session: Session,
    project_id: str,
    canon: Mapping[str, dict[str, Any]],
    digested: list[tuple[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """主要人物（04 摘要表里的；没有就用作品的角色表）：目标、顿悟、人物弧、恐惧、希望与各章摘要记下的变化。"""
    sheets = [item for item in _as_list((canon.get("character_sheets") or {}).get("characters")) if isinstance(item, dict)]
    bibles = [item for item in _as_list((canon.get("character_bibles") or {}).get("characters")) if isinstance(item, dict)]
    roster = session.execute(
        select(StoryCharacter.character_id, StoryCharacter.display_name)
        .where(StoryCharacter.project_id == project_id)
        .order_by(StoryCharacter.created_at.asc(), StoryCharacter.character_id.asc())
    ).all()
    names = {character_id: _text(name) for character_id, name in roster}
    people = (
        [(_text(item.get("character_id")), _text(item.get("display_name")) or names.get(_text(item.get("character_id")), "")) for item in sheets]
        if sheets
        else [(character_id, name) for character_id, name in names.items()]
    )
    result: list[dict[str, Any]] = []
    for character_id, name in people:
        if not name:
            continue
        sheet = _find(sheets, character_id, name)
        bible = _find(bibles, character_id, name)
        psych = bible.get("psychological_profile") if isinstance(bible.get("psychological_profile"), dict) else {}
        changes = [
            f"第{number}章：{item['change']}"
            for number, digest in digested
            for item in digest.get("character_changes") or []
            if item.get("character") == name
        ]
        entry = {
            "name": name,
            "role": _text(sheet.get("role")) or _text(bible.get("role")),
            "goal": _text(sheet.get("goal")),
            "epiphany": _text(sheet.get("epiphany")),
            "arc": _text(psych.get("character_arc")),
            "deepest_fear": _text(psych.get("deepest_fear")),
            "greatest_hope": _text(psych.get("greatest_hope")),
            "changes_so_far": changes[-12:],
        }
        result.append({key: value for key, value in entry.items() if value})
    return result


def _recent_chapters(states: Sequence[ChapterMemoryState], through: int) -> list[dict[str, Any]]:
    recent: list[dict[str, Any]] = []
    budget = RECENT_TEXT_MAX_CHARS
    for index in range(through, -1, -1):
        if len(recent) >= REVIEW_INTERVAL_CHAPTERS:
            break
        state = states[index]
        body = state.body.strip()
        if not body:
            continue
        entry = {"chapter_number": state.number, "title": chapter_display_title(state.chapter)}
        if len(body) > budget:
            if not recent:
                recent.append({**entry, "text": "（前略）" + body[-budget:]})
            break
        recent.insert(0, {**entry, "text": body})
        budget -= len(body)
    return recent


def editorial_prompt_payload(
    session: Session,
    project_id: str,
    states: Sequence[ChapterMemoryState],
    scope: ReviewScope,
) -> tuple[dict[str, Any], dict[int, str]]:
    """通读的输入，另给后面几章的章号 → 章 id（模型说「给第几章的意见」时换回章 id）。"""
    canon = _canon_drafts(session, project_id)
    through = scope.through_index
    digested = [
        (state.number, normalize_digest(state.digest.digest_json))
        for state in states[: through + 1]
        if state.digest is not None
    ]
    titles = {state.number: chapter_display_title(state.chapter) for state in states}
    full_from = max(0, (through + 1) - FULL_DIGEST_CHAPTERS)
    story = []
    for number, digest in digested:
        entry: dict[str, Any] = {"chapter_number": number, "title": titles.get(number, ""), "summary": digest["summary"]}
        if number - 1 >= full_from:
            entry["ending_state"] = digest["ending_state"]
            entry["character_changes"] = [f"{item['character']}：{item['change']}" for item in digest["character_changes"]]
        story.append(entry)
    threads, numbers = project_threads(session, project_id, before_index=through + 1)
    thread_rows = []
    for thread in threads:
        if not thread.is_open:
            continue
        payoff = numbers.get(thread.payoff_chapter_id or "")
        alert, dormant = thread_alert(thread, through + 1, payoff)
        thread_rows.append(
            {
                "thread": thread.label,
                "opened_in": thread.opened_number,
                "last_touched_in": thread.last_number,
                "planned_payoff_in": payoff,
                "alert": {"payoff_missed": "planned payoff chapter passed without payoff", "cold": f"untouched for {dormant} chapters"}.get(alert or "", ""),
            }
        )
    chapters = [state.chapter for state in states]
    window_numbers = {index + 1: chapters[index].chapter_id for index in scope.window_indexes}
    payload = {
        "book": _book(canon),
        "characters": _characters(session, project_id, canon, digested),
        "story_so_far": story,
        "open_threads": thread_rows,
        "recent_chapters": _recent_chapters(states, through),
        "upcoming": window_plan(session, chapters, scope.window_indexes),
        "previous_guidance": _previous_guidance(session, project_id, numbers),
        "reviewed_through_chapter": through + 1,
    }
    return payload, window_numbers


def _previous_guidance(session: Session, project_id: str, numbers: Mapping[str, int]) -> list[dict[str, Any]]:
    """上一份通读里作者采纳过的意见（让这一次看看落实了没有）。"""
    latest = latest_review(session, project_id)
    if latest is None:
        return []
    rows = session.execute(
        select(EditorialGuidance)
        .where(EditorialGuidance.review_id == latest.review_id, EditorialGuidance.status == "accepted")
        .order_by(EditorialGuidance.position.asc())
    ).scalars()
    return [
        {"chapter_number": numbers.get(row.chapter_id or ""), "guidance": (row.author_text or row.text).strip()}
        for row in rows
    ]


# ---------------------------------------------------------------------- 输出
def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_review(raw: Mapping[str, Any], *, window_numbers: Mapping[int, str], max_chapter: int) -> dict[str, Any] | None:
    """模型的输出 → 存下的形状；没有总评 → ``None``（不合格）。意见的章号只认 window 里的章，别的当作针对整段。"""
    assessment = _text(raw.get("assessment"), 800)
    if not assessment:
        return None
    findings: list[dict[str, Any]] = []
    for item in _as_list(raw.get("findings")):
        if not isinstance(item, Mapping):
            continue
        finding = _text(item.get("finding"), 240)
        if not finding:
            continue
        kind = _text(item.get("kind"))
        severity = _text(item.get("severity"))
        chapters = sorted({number for number in (_int(value) for value in _as_list(item.get("chapters"))) if number and 1 <= number <= max_chapter})
        findings.append(
            {
                "kind": kind if kind in FINDING_KINDS else "other",
                "severity": severity if severity in SEVERITIES else "medium",
                "chapters": chapters,
                "finding": finding,
                "suggestion": _text(item.get("suggestion"), 240),
            }
        )
        if len(findings) >= _FINDING_LIMIT:
            break
    guidance: list[dict[str, Any]] = []
    for item in _as_list(raw.get("guidance")):
        if not isinstance(item, Mapping):
            continue
        text = _text(item.get("guidance"), 240)
        if not text:
            continue
        guidance.append(
            {"chapter_id": window_numbers.get(_int(item.get("chapter")) or 0), "guidance": text, "reason": _text(item.get("reason"), 200)}
        )
        if len(guidance) >= _GUIDANCE_LIMIT:
            break
    return {"assessment": assessment, "findings": findings, "guidance": guidance}


def _store_review(
    session: Session,
    project_id: str,
    states: Sequence[ChapterMemoryState],
    scope: ReviewScope,
    review: Mapping[str, Any],
    *,
    origin: str,
    llm_call_id: str | None,
    prompt_version: str | None,
) -> EditorialReview:
    for row in session.execute(
        select(EditorialReview).where(EditorialReview.project_id == project_id, EditorialReview.status == "active")
    ).scalars():
        row.status = "superseded"
    for row in session.execute(
        select(EditorialGuidance).where(EditorialGuidance.project_id == project_id, EditorialGuidance.status == "proposed")
    ).scalars():
        row.status = "superseded"
    session.flush()
    stored = EditorialReview(
        review_id=f"editorial_review_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        through_chapter_id=states[scope.through_index].chapter.chapter_id,
        window_chapter_ids_json=[states[index].chapter.chapter_id for index in scope.window_indexes],
        status="active",
        origin=origin,
        source_hash=scope.source_hash,
        review_json={"assessment": review["assessment"], "findings": list(review["findings"])},
        llm_call_id=llm_call_id or None,
        prompt_version=prompt_version,
    )
    session.add(stored)
    session.flush()
    for position, item in enumerate(review["guidance"]):
        session.add(
            EditorialGuidance(
                guidance_id=f"editorial_guidance_{uuid.uuid4().hex[:16]}",
                review_id=stored.review_id,
                project_id=project_id,
                chapter_id=item["chapter_id"],
                position=position,
                text=item["guidance"],
                reason=item["reason"],
                status="proposed",
            )
        )
    session.flush()
    return stored


def generate_editorial_review(
    session: Session,
    project_id: str,
    *,
    origin: str = "manual",
    runner: LLMNodeRunner | None = None,
) -> dict[str, Any]:
    """现在通读一次并存为当前那一份。还没有任何一章有摘要（或后台还欠着摘要）→ 409；模型没配好 409，失败 / 不合格 502。"""
    from novel_system.services.scene_lookup import require_project

    require_project(session, project_id)
    states = chapter_memory_states(session, project_id)
    scope = review_scope(states)
    if scope is None:
        raise DomainError(
            "EDITORIAL_REVIEW_NOT_READY",
            "还没有可以通读的章：至少要有一章写完并整理好章摘要（后台还在补写的摘要要先跟上）",
            status_code=409,
            details={"project_id": project_id, "retryable": False},
        )
    if runner is None:
        from novel_system.settings import get_settings

        if not get_settings().llm_enabled:
            raise DomainError(
                "EDITORIAL_REVIEW_LLM_UNAVAILABLE",
                "还没有接入可用的模型，做不了编辑通读：先到系统设置里配置模型",
                status_code=409,
                details={"project_id": project_id, "retryable": False},
            )
        runner = LLMNodeRunner(session)
    template = load_template(EDITORIAL_REVIEW_TEMPLATE)
    context = LLMCallContext(
        scope_type="project",
        scope_id=project_id,
        node_id=EDITORIAL_REVIEW_NODE,
        step=EDITORIAL_REVIEW_STEP,
        project_id=project_id,
        provider_execution_mode=runner.provider_execution_mode,
    )
    payload, window_numbers = editorial_prompt_payload(session, project_id, states, scope)
    prompt_text = f"{str(template.task_prompt or '').rstrip()}\n\n{json.dumps(payload, ensure_ascii=False)}"
    try:
        response = runner.run_task(
            task_name=EDITORIAL_REVIEW_TEMPLATE,
            prompt_text=prompt_text,
            system_prompt=template.system_prompt,
            context=context,
            template=template,
        )
    except LLMNodeExecutionError as exc:
        raise_llm_domain_error(
            exc,
            capability_code="EDITORIAL_REVIEW_LLM_UNAVAILABLE",
            failure_code="EDITORIAL_REVIEW_LLM_FAILED",
            operation="Editorial review",
            node_id=EDITORIAL_REVIEW_NODE,
            next_action="configure_editorial_review_route_or_retry",
            extra_details={"project_id": project_id},
        )
    llm_call_id = str(getattr(response, "llm_call_id", "") or "")
    parsed = parse_json_object(response)
    review = (
        normalize_review(parsed, window_numbers=window_numbers, max_chapter=scope.through_index + 1 + len(scope.window_indexes))
        if parsed is not None
        else None
    )
    if review is None:
        if llm_call_id:
            mark_postprocess_failure(
                session,
                llm_call_id,
                error_code="EDITORIAL_REVIEW_RESPONSE_INVALID",
                error_text="editorial review response is not a JSON object with an assessment",
            )
            session.commit()
        raise DomainError(
            "EDITORIAL_REVIEW_RESPONSE_INVALID",
            "模型返回的编辑通读不合格（不是带「assessment」的 JSON），这一次没有存；可以再试一次",
            status_code=502,
            details={"project_id": project_id, "llm_call_id": llm_call_id or None, "retryable": True},
        )
    validate_product_call(session, llm_call_id, context, expected_outcome="completed")
    stored = _store_review(
        session,
        project_id,
        states,
        scope,
        review,
        origin=origin,
        llm_call_id=llm_call_id,
        prompt_version=getattr(template, "version", None),
    )
    return serialize_review(session, stored, _numbers(states), _titles(states))


# ---------------------------------------------------------------------- 作者的决定
def _target_chapter_ids(guidance: EditorialGuidance, review: EditorialReview | None) -> list[str]:
    if guidance.chapter_id:
        return [guidance.chapter_id]
    window = review.window_chapter_ids_json if review is not None else None
    return [str(item) for item in window if item] if isinstance(window, list) else []


def _unwritten_scene_ids(session: Session, chapter_ids: list[str]) -> list[str]:
    scene_ids: list[str] = []
    for chapter_id in chapter_ids:
        for scene in chapter_scenes(session, chapter_id):
            state = session.get(SceneRunState, scene.scene_id)
            if state is None or not state.current_final_scene_row_id:
                scene_ids.append(scene.scene_id)
    return scene_ids


def decide_guidance(
    session: Session,
    project_id: str,
    guidance_id: str,
    *,
    decision: str,
    text: str | None = None,
) -> dict[str, Any]:
    """作者对一条意见：``accept``（可以带改过的字；已采纳的再 accept = 改字）/ ``dismiss``（不采纳，或撤销采纳）。

    已被新一份通读作废的意见不能再决定（409 ``EDITORIAL_GUIDANCE_SUPERSEDED``）。进起草的内容变了（采纳、撤销、改字），
    那几章还没写的场的场景蓝图作废。
    """
    guidance = session.get(EditorialGuidance, guidance_id)
    if guidance is None or guidance.project_id != project_id:
        raise DomainError(
            "EDITORIAL_GUIDANCE_NOT_FOUND",
            "找不到这条编辑意见：刷新后再试",
            status_code=404,
            details={"guidance_id": guidance_id},
        )
    if guidance.status == "superseded":
        raise DomainError(
            "EDITORIAL_GUIDANCE_SUPERSEDED",
            "这条意见所在的那份通读已经被新的一份取代：看新的那一份",
            status_code=409,
            details={"guidance_id": guidance_id},
        )
    if decision not in GUIDANCE_DECISIONS:
        raise DomainError(
            "EDITORIAL_GUIDANCE_DECISION_INVALID",
            "不认得的决定：只能采纳或不采纳",
            status_code=422,
            details={"decision": decision},
        )
    before = (guidance.status, guidance.author_text)
    if decision == "accept":
        edited = (text or "").strip()
        guidance.author_text = edited if edited and edited != guidance.text.strip() else None
        guidance.status = "accepted"
    else:
        guidance.status = "dismissed"
    guidance.decided_at = utcnow()
    session.flush()
    superseded: dict[str, Any] = {}
    was_live = before[0] == "accepted"
    is_live = guidance.status == "accepted"
    if was_live != is_live or (is_live and before[1] != guidance.author_text):
        review = session.get(EditorialReview, guidance.review_id)
        scene_ids = _unwritten_scene_ids(session, _target_chapter_ids(guidance, review))
        if scene_ids:
            superseded = supersede_scene_planning_artifacts(
                session, scene_ids=scene_ids, reason="editorial_guidance_changed"
            )
    return {
        "guidance": _serialize_guidance(guidance, _chapter_numbers(session, project_id)),
        "superseded_planning": superseded,
    }


# ---------------------------------------------------------------------- 读
def _numbers(states: Sequence[ChapterMemoryState]) -> dict[str, int]:
    return {state.chapter.chapter_id: state.number for state in states}


def _titles(states: Sequence[ChapterMemoryState]) -> dict[str, str]:
    return {state.chapter.chapter_id: chapter_display_title(state.chapter) for state in states}


def _chapter_numbers(session: Session, project_id: str) -> dict[str, int]:
    return {chapter.chapter_id: index + 1 for index, chapter in enumerate(live_chapters_in_order(session, project_id))}


def _serialize_guidance(row: EditorialGuidance, numbers: Mapping[str, int]) -> dict[str, Any]:
    return {
        "guidance_id": row.guidance_id,
        "review_id": row.review_id,
        "chapter_id": row.chapter_id,
        "chapter_number": numbers.get(row.chapter_id or ""),
        "text": row.text,
        "author_text": row.author_text,
        "reason": row.reason,
        "status": row.status,
        "decided_at": row.decided_at,
    }


def serialize_review(
    session: Session,
    review: EditorialReview,
    numbers: Mapping[str, int],
    titles: Mapping[str, str],
) -> dict[str, Any]:
    guidance = session.execute(
        select(EditorialGuidance)
        .where(EditorialGuidance.review_id == review.review_id)
        .order_by(EditorialGuidance.position.asc())
    ).scalars()
    body = review.review_json if isinstance(review.review_json, dict) else {}
    window = review.window_chapter_ids_json if isinstance(review.window_chapter_ids_json, list) else []
    return {
        "review_id": review.review_id,
        "status": review.status,
        "origin": review.origin,
        "created_at": review.created_at,
        "through_chapter_id": review.through_chapter_id,
        "through_chapter_number": numbers.get(review.through_chapter_id),
        "window": [
            {"chapter_id": chapter_id, "number": numbers.get(chapter_id), "title": titles.get(chapter_id, "")}
            for chapter_id in window
        ],
        "assessment": str(body.get("assessment") or ""),
        "findings": list(body.get("findings") or []),
        "guidance": [_serialize_guidance(row, numbers) for row in guidance],
    }


def editorial_overview(session: Session, project_id: str, states: Sequence[ChapterMemoryState]) -> dict[str, Any]:
    """成稿中心「记忆」里的编辑通读：最新的一份（带意见）、更早的几份、仍在生效的已采纳意见、下一次什么时候读。"""
    numbers, titles = _numbers(states), _titles(states)
    reviews = list(
        session.execute(
            select(EditorialReview)
            .where(EditorialReview.project_id == project_id)
            .order_by(EditorialReview.created_at.desc(), EditorialReview.review_id.desc())
            .limit(11)
        ).scalars()
    )
    latest = next((row for row in reviews if row.status == "active"), None)
    accepted = session.execute(
        select(EditorialGuidance)
        .where(EditorialGuidance.project_id == project_id, EditorialGuidance.status == "accepted")
        .order_by(EditorialGuidance.decided_at.desc(), EditorialGuidance.guidance_id.asc())
        .limit(30)
    ).scalars()
    scope = review_scope(states)
    last_through = _last_through_number(states, latest)
    through_number = scope.through_index + 1 if scope is not None else 0
    return {
        "interval_chapters": REVIEW_INTERVAL_CHAPTERS,
        "latest": serialize_review(session, latest, numbers, titles) if latest is not None else None,
        "history": [
            {
                "review_id": row.review_id,
                "created_at": row.created_at,
                "origin": row.origin,
                "through_chapter_number": numbers.get(row.through_chapter_id),
                "finding_count": len((row.review_json or {}).get("findings") or []) if isinstance(row.review_json, dict) else 0,
            }
            for row in reviews
            if latest is None or row.review_id != latest.review_id
        ][:10],
        "accepted": [
            {**_serialize_guidance(row, numbers), "window": None if row.chapter_id else _window_numbers(session, row, numbers)}
            for row in accepted
        ],
        "can_run": scope is not None,
        "readable_through": through_number,
        "next_auto_after_chapter": last_through + REVIEW_INTERVAL_CHAPTERS,
    }


def _window_numbers(session: Session, guidance: EditorialGuidance, numbers: Mapping[str, int]) -> list[int]:
    review = session.get(EditorialReview, guidance.review_id)
    return sorted(numbers[chapter_id] for chapter_id in _target_chapter_ids(guidance, review) if chapter_id in numbers)


__all__ = [
    "EDITORIAL_REVIEW_NODE",
    "REVIEW_INTERVAL_CHAPTERS",
    "ReviewScope",
    "decide_guidance",
    "editorial_overview",
    "editorial_review_due",
    "generate_editorial_review",
    "review_scope",
]

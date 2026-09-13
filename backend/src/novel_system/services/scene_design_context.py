"""场景设计上下文（Scene Design Context）——把雪花前几步的设计送到起草模型面前。

2026-09-13 对雪花模块的第二轮评估核实：阶段 A 之后起草 bundle 仍然只看得到第 10 步的
``Scene Structure (Snowflake)`` 段和一句章目标。``bundle_builder`` 不引用任何雪花模型，于是
一句话概括、五句脊柱与道德前提（02 / 03）、POV 角色的目标 / 抱负 / 价值观 / 冲突 / 顿悟与
故事线（04）、视角故事（06）、章的幕次与灾难标记（07 章表）、上一场怎么收、下一场怎么开
（09 / 10 相邻行）——写手一个字都看不到。Ingermanson 第十步的原话是「读完为这场规划的
一切，然后开写」；现在读到的不到十分之一。

本模块从**已确认**的雪花产出（``approved`` 或 ``stale``——stale 仍是作者确认过的内容，只是
上游有了新版本；``pending_review`` 未经作者确认，不算事实）与场景 / 章计划行组装一段
带标签的背景，作为 ``context_budget.SECTION_SPECS`` 里紧随结构简报的
``scene_design_context`` section 进入起草 bundle 与蓝图快照。

与结构简报的分工：结构简报是这一场的**事实**（永不压缩）；设计上下文是**背景**——预算
紧时先压掉视角故事 / 五句脊柱 / 在场人物，再不够整段省略；硬 QC 类任务不看它。
「怎么用」的指令在提示词模板里（``neutral_draft`` / ``style_first_draft`` / ``scene_blueprint``），
这里只陈述作者写下的东西，不夹带写作指令。

``NOVEL_SYSTEM_SCENE_DESIGN_CONTEXT=false`` 可整体关闭注入（默认开，只作回滚用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryCharacter,
)
from novel_system.services.context_budget import compress_design_context
from novel_system.settings import get_settings

SCENE_DESIGN_SECTION_KEY = "scene_design_context"
SCENE_DESIGN_SECTION_LABEL = "Scene Design Context (Snowflake)"

# 只有作者确认过的版本才是设计事实；stale 是「确认过、上游又改了」，仍然优于没有。
CANON_STATUSES: frozenset[str] = frozenset({"approved", "stale"})
_CANON_STEP_KEYS: tuple[str, ...] = (
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "character_sheets",
    "character_synopses",
)
POV_STORY_EXCERPT_CHARS = 300
_POV_STORY_PREFIX = "视角故事："


@dataclass(slots=True)
class SceneDesignContext:
    text: str
    step_run_ids: list[str] = field(default_factory=list)


def design_context_enabled() -> bool:
    """环境开关；不读库内配置快照（这不是作者在界面上配的东西）。"""
    settings = get_settings(include_runtime_config=False)
    return bool(getattr(settings, "scene_design_context_enabled", True))


def render_scene_design_context(scene: SceneCard, session: Session | None) -> str | None:
    context = build_scene_design_context(scene, session)
    return context.text if context is not None else None


def build_scene_design_context(scene: SceneCard, session: Session | None) -> SceneDesignContext | None:
    """组装设计上下文；没有任何已确认的雪花设计、或开关关闭时返回 ``None``（调用方不注入）。"""
    if session is None or not design_context_enabled():
        return None
    project_id = _text(getattr(scene, "project_id", None))
    if not project_id:
        return None
    canon = _latest_canon_runs(session, project_id)
    plan = _scene_plan(session, project_id, _text(scene.scene_id))
    if not canon and plan is None:
        return None

    lines: list[str] = []
    used_runs: list[str] = []

    def _use(step_key: str) -> dict[str, Any]:
        run = canon.get(step_key)
        if run is None:
            return {}
        if run.step_run_id not in used_runs:
            used_runs.append(run.step_run_id)
        return run.draft_json if isinstance(run.draft_json, dict) else {}

    logline = _text((canon.get("one_sentence_summary").draft_json if canon.get("one_sentence_summary") else {}).get("summary"))
    if logline:
        _use("one_sentence_summary")
        lines.append(f"Book logline: {logline}")
    # 阶段 J：全书的叙述人称与时态（Dynamite Scene 第 4 章：每一场都要决定视角与时态）——这一行有约束力。
    stance = _text((canon.get("book_brief").draft_json if canon.get("book_brief") else {}).get("narrative_stance"))
    if stance:
        _use("book_brief")
        lines.append(f"Narrative stance (binding: person and tense): {stance}")

    paragraph = canon.get("one_paragraph_summary").draft_json if canon.get("one_paragraph_summary") else {}
    sentences = [_text(item) for item in _as_list(paragraph.get("sentences")) if _text(item)]
    premise = _text(paragraph.get("moral_premise"))
    if sentences or premise:
        _use("one_paragraph_summary")
    if sentences:
        lines.append(
            "Story spine (five sentences): "
            + " ".join(f"({index}) {sentence}" for index, sentence in enumerate(sentences, start=1))
        )
    if premise:
        lines.append(f"Moral premise: {premise}")

    chapter_line = _chapter_line(session, scene, plan)
    if chapter_line:
        lines.append(chapter_line)
    position_line = _position_line(session, project_id, plan)
    if position_line:
        lines.append(position_line)

    sheets = canon.get("character_sheets").draft_json if canon.get("character_sheets") else {}
    characters = [item for item in _as_list(sheets.get("characters")) if isinstance(item, dict)]
    names = _character_names(session, project_id)
    pov_id = _text(getattr(scene, "pov_character_id", None)) or (_text(plan.pov_character_id) if plan is not None else "")
    pov_sheet = _find_character(characters, pov_id, names.get(pov_id))
    if pov_sheet is not None:
        _use("character_sheets")
        lines.append(_sheet_line("POV character sheet", pov_sheet, names.get(pov_id)))

    synopses = canon.get("character_synopses").draft_json if canon.get("character_synopses") else {}
    synopsis_items = [item for item in _as_list(synopses.get("characters")) if isinstance(item, dict)]
    pov_synopsis = _find_character(synopsis_items, pov_id, names.get(pov_id))
    pov_story = _pov_story_excerpt(pov_synopsis)
    if pov_story:
        _use("character_synopses")
        lines.append(f"POV story so far (视角故事, excerpt): {pov_story}")

    onstage_ids = [
        _text(item)
        for item in (getattr(scene, "onstage_chars_json", None) or (plan.onstage_chars_json if plan is not None else None) or [])
        if _text(item) and _text(item) != pov_id
    ]
    for character_id in dict.fromkeys(onstage_ids):
        sheet = _find_character(characters, character_id, names.get(character_id))
        if sheet is None:
            continue
        _use("character_sheets")
        one_liner = _text(sheet.get("one_sentence_summary")) or _text(sheet.get("goal"))
        label = _character_label(sheet, names.get(character_id))
        lines.append(f"Onstage — {label}: {one_liner}" if one_liner else f"Onstage — {label}")

    previous_line, next_line = _neighbour_lines(session, project_id, plan, names)
    if previous_line:
        lines.append(previous_line)
    if next_line:
        lines.append(next_line)

    if not lines:
        return None
    return SceneDesignContext(text="\n".join(lines), step_run_ids=used_runs)


def compress_scene_design_context(text: str) -> str:
    """预算紧时的压缩形态（实现在 context_budget，避免 settings 反向成环）：只留一句话、道德前提、
    章位置、POV 摘要表与相邻两场；视角故事、五句脊柱、在场人物先让路。"""
    return compress_design_context(text)


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


def _latest_canon_runs(session: Session, project_id: str) -> dict[str, SnowflakeStepRun]:
    rows = session.execute(
        select(SnowflakeStepRun)
        .where(
            SnowflakeStepRun.project_id == project_id,
            SnowflakeStepRun.step_key.in_(_CANON_STEP_KEYS),
            SnowflakeStepRun.status.in_(sorted(CANON_STATUSES)),
        )
        .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
    ).scalars().all()
    latest: dict[str, SnowflakeStepRun] = {}
    for row in rows:
        latest.setdefault(row.step_key, row)
    return latest


def _scene_plan(session: Session, project_id: str, scene_id: str) -> SnowflakeScenePlan | None:
    if not scene_id:
        return None
    return session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id,
            SnowflakeScenePlan.scene_id == scene_id,
            SnowflakeScenePlan.removed_at.is_(None),
        )
    ).scalars().first()


def _ordered_plans(session: Session, project_id: str) -> list[SnowflakeScenePlan]:
    plans = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id,
            SnowflakeScenePlan.removed_at.is_(None),
        )
    ).scalars().all()
    chapter_seq: dict[str, int] = {
        row.chapter_plan_id: int(row.chapter_seq or 0)
        for row in session.execute(
            select(SnowflakeChapterPlan).where(
                SnowflakeChapterPlan.project_id == project_id,
                SnowflakeChapterPlan.removed_at.is_(None),
            )
        ).scalars().all()
    }
    return sorted(
        plans,
        key=lambda item: (
            chapter_seq.get(item.chapter_plan_id or "", 0),
            _text(item.chapter_id),
            int(item.scene_seq or 0),
            _text(item.scene_id),
        ),
    )


def _character_names(session: Session, project_id: str) -> dict[str, str]:
    rows = session.execute(
        select(StoryCharacter).where(StoryCharacter.project_id == project_id)
    ).scalars().all()
    return {row.character_id: _text(row.display_name) or row.character_id for row in rows}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _chapter_line(session: Session, scene: SceneCard, plan: SnowflakeScenePlan | None) -> str | None:
    seq: int | None = None
    title = ""
    act: int | None = None
    spine = ""
    goal = ""
    chapter_plan = None
    if plan is not None and plan.chapter_plan_id:
        chapter_plan = session.get(SnowflakeChapterPlan, plan.chapter_plan_id)
    if chapter_plan is not None:
        seq = int(chapter_plan.chapter_seq or 0) or None
        title = _text(chapter_plan.title)
        act = int(chapter_plan.act or 0) or None
        spine = _text(chapter_plan.spine)
        goal = _text(chapter_plan.chapter_goal) or _text(chapter_plan.summary)
    else:
        chapter = session.get(ChapterGoal, _text(getattr(scene, "chapter_id", None))) if _text(getattr(scene, "chapter_id", None)) else None
        if chapter is None:
            return None
        narrative = chapter.narrative_json if isinstance(getattr(chapter, "narrative_json", None), dict) else {}
        title = _text(narrative.get("title"))
        act = _coerce_int(narrative.get("act"))
        spine = _text(narrative.get("spine"))
        goal = _text(chapter.chapter_goal)
        seq = _coerce_int(getattr(chapter, "display_order", None))
    parts: list[str] = []
    head = ""
    if seq:
        head = f"第{seq}章"
    if title:
        head = f"{head}《{title}》" if head else f"《{title}》"
    if head:
        parts.append(head)
    if act:
        parts.append(f"Act {act}")
    if spine:
        parts.append(f"spine {spine}")
    if goal:
        parts.append(f"chapter goal: {goal}")
    if not parts:
        return None
    return "Chapter: " + " · ".join(parts)


def _position_line(session: Session, project_id: str, plan: SnowflakeScenePlan | None) -> str | None:
    if plan is None:
        return None
    ordered = _ordered_plans(session, project_id)
    index = next((i for i, item in enumerate(ordered, start=1) if item.scene_plan_id == plan.scene_plan_id), None)
    parts: list[str] = []
    if index is not None:
        parts.append(f"scene {index} of {len(ordered)} in the book")
    role = _text(plan.chapter_role)
    if role:
        parts.append(f"chapter role: {role}")
    spine = _text(plan.spine)
    if spine:
        parts.append(f"spine {spine}")
    if not parts:
        return None
    return "Scene position: " + " · ".join(parts)


def _sheet_line(label: str, sheet: dict[str, Any], fallback_name: str | None) -> str:
    parts: list[str] = []
    for key, tag in (
        ("goal", "Goal"),
        ("ambition", "Ambition"),
        ("conflict", "Conflict"),
        ("epiphany", "Epiphany"),
        ("one_sentence_summary", "Storyline"),
    ):
        value = _text(sheet.get(key))
        if value:
            parts.append(f"{tag}: {value}")
    values = [_text(item) for item in _as_list(sheet.get("values")) if _text(item)]
    if values:
        parts.insert(min(2, len(parts)), "Values: " + " / ".join(values))
    head = f"{label} — {_character_label(sheet, fallback_name)}"
    return f"{head}: " + "; ".join(parts) if parts else head


def _character_label(sheet: dict[str, Any], fallback_name: str | None) -> str:
    name = _text(sheet.get("display_name")) or _text(sheet.get("name")) or _text(fallback_name) or _text(sheet.get("character_id"))
    role = _text(sheet.get("role"))
    return f"{name} ({role})" if role else name


def _find_character(items: list[dict[str, Any]], character_id: str, display_name: str | None) -> dict[str, Any] | None:
    if not character_id and not display_name:
        return None
    for item in items:
        if character_id and _text(item.get("character_id")) == character_id:
            return item
    if display_name:
        for item in items:
            if _text(item.get("display_name")) == display_name or _text(item.get("name")) == display_name:
                return item
    return None


def _pov_story_excerpt(synopsis_item: dict[str, Any] | None) -> str:
    if synopsis_item is None:
        return ""
    synopsis = synopsis_item.get("synopsis")
    text = ""
    if isinstance(synopsis, dict):
        text = _text(synopsis.get("pov_story")) or _text(synopsis.get("视角故事"))
    elif isinstance(synopsis, str):
        lines = synopsis.split("\n")
        collected: list[str] = []
        capturing = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(_POV_STORY_PREFIX):
                capturing = True
                collected.append(stripped[len(_POV_STORY_PREFIX):].strip())
                continue
            if capturing:
                # 六个前缀行里视角故事排最后，之后的行都属于它；遇到别的前缀行则停。
                if any(stripped.startswith(prefix) for prefix in ("信念：", "旧伤：", "欲望：", "恐惧：", "关系：")):
                    break
                collected.append(stripped)
        text = " ".join(part for part in collected if part)
    text = " ".join(text.split())
    if len(text) > POV_STORY_EXCERPT_CHARS:
        text = text[:POV_STORY_EXCERPT_CHARS].rstrip() + "…"
    return text


def _neighbour_lines(
    session: Session,
    project_id: str,
    plan: SnowflakeScenePlan | None,
    names: dict[str, str],
) -> tuple[str | None, str | None]:
    if plan is None:
        return None, None
    ordered = _ordered_plans(session, project_id)
    index = next((i for i, item in enumerate(ordered) if item.scene_plan_id == plan.scene_plan_id), None)
    if index is None:
        return None, None
    previous = ordered[index - 1] if index > 0 else None
    following = ordered[index + 1] if index + 1 < len(ordered) else None
    previous_line = None
    if previous is not None:
        pov = names.get(_text(previous.pov_character_id)) or _text(previous.pov_character_id)
        head = f"Previous scene (S{int(previous.scene_seq or 0):02d}"
        head += f", POV {pov})" if pov else ")"
        if _is_skipped(previous):
            # 阶段 I：页面上略过的反应场——读者没看到它，写手却要知道它：三拍整段带过来。
            beats = "; ".join(
                f"{label}: {_text(getattr(previous, key))}"
                for key, label in (("reaction", "Reaction"), ("dilemma", "Dilemma"), ("decision", "Decision"))
                if _text(getattr(previous, key))
            )
            previous_line = f"{head} is skipped on the page (the reader never sees it); its designed beat — {beats or _text(previous.summary)}"
        elif _text(previous.scene_type) == "reactive" and _text(previous.decision):
            previous_line = f"{head} ended on Decision: {_text(previous.decision)}"
        elif _text(previous.setback):
            previous_line = f"{head} ended on Setback: {_text(previous.setback)}"
        elif _text(previous.summary):
            previous_line = f"{head}: {_text(previous.summary)}"
    next_line = None
    if following is not None:
        head = f"Next scene (S{int(following.scene_seq or 0):02d})"
        if _is_skipped(following):
            head += " is skipped on the page; it"
        if _text(following.scene_type) == "reactive" and _text(following.reaction):
            next_line = f"{head} opens on Reaction: {_text(following.reaction)}"
        elif _text(following.goal):
            next_line = f"{head} opens on Goal: {_text(following.goal)}"
        elif _text(following.summary):
            next_line = f"{head}: {_text(following.summary)}"
    return previous_line, next_line


def _is_skipped(plan: SnowflakeScenePlan) -> bool:
    return _text(plan.scene_type) == "reactive" and _text(plan.rendering_mode).lower() == "skip"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _coerce_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _text(value: Any) -> str:
    return str(value or "").strip()

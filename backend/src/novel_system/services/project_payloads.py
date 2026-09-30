"""作品域的回包形状与写入时的字段归一——v1 作品 / 大纲计划 / 章 / 场景的序列化，
终审包里的质检摘要，以及建作品、改档案、物化时共用的几个取值小工具。

从 ``projects.py`` 拆出（B08-09）；``projects`` 照旧再导出这些名字。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, OutlinePlan, QcReport, SceneCard, StoryProject


def project_summary_payload(project: StoryProject) -> dict[str, Any]:
    payload = project_payload(project)
    payload.pop("outline_text", None)
    return payload


def project_payload(project: StoryProject) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "title": project.title,
        "genre": project.genre,
        "target_word_count": project.target_word_count,
        "target_chapter_count": project.target_chapter_count,
        # FE-ALIGN P2 作品档案字段（原型 WsWorks 作品对象）
        "mark": project.mark,
        "accent": project.accent,
        "synopsis_line": project.synopsis_line,
        "words_target_daily": project.words_target_daily,
        # 演示作品在迁移 20260717_0074 退役、不再入库；前端 ws-works.jsx 仍按这个键过滤退役演示作品，
        # 那道过滤去掉之后这个恒为 False 的键可以一起删。
        "is_demo": False,
        "outline_text": project.outline_text,
        "planning_mode": project.planning_mode or "outline_driven",
        "snowflake_schema_version": project.snowflake_schema_version,
        "snowflake_workflow_mode": project.snowflake_workflow_mode or "strict",
        "status": project.status,
        "active_outline_plan_id": project.active_outline_plan_id,
        "current_chapter_id": project.current_chapter_id,
        "approved_chapter_ids": list(project.approved_chapter_ids_json or []),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def outline_plan_payload(plan: OutlinePlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "version": plan.version,
        "status": plan.status,
        "plan_json": plan.plan_json or {},
        "created_at": plan.created_at,
        "approved_at": plan.approved_at,
    }


def chapter_payload(session: Session, chapter: ChapterGoal) -> dict[str, Any]:
    scenes = (
        session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0
            )
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        )
        .scalars()
        .all()
    )
    return {
        "chapter_id": chapter.chapter_id,
        "project_id": chapter.project_id,
        "outline_plan_id": chapter.outline_plan_id,
        "chapter_goal": chapter.chapter_goal,
        "main_plot_push": chapter.main_plot_push,
        "emotional_target": chapter.emotional_target,
        "ending_effect": chapter.ending_effect,
        "must_not": chapter.must_not,
        "planned_scene_count": chapter.planned_scene_count,
        "scenes": [scene_payload(scene) for scene in scenes],
    }


def scene_payload(scene: SceneCard) -> dict[str, Any]:
    return {
        "scene_id": scene.scene_id,
        "chapter_id": scene.chapter_id,
        "project_id": scene.project_id,
        "outline_plan_id": scene.outline_plan_id,
        "scene_seq": scene.scene_seq,
        "scene_goal": scene.scene_goal,
        "beats_json": list(scene.beats_json or []),
        "must_include_text": scene.must_include_text,
        "forbidden_text": scene.forbidden_text,
        "exit_change": scene.exit_change,
        "hook": scene.hook,
        "target_length_band": scene.target_length_band,
        "scene_type": scene.scene_type,
        "is_chapter_last": scene.is_chapter_last,
    }


def qc_issue_summaries(
    report: QcReport | None, *, limit: int = 3
) -> list[dict[str, str]]:
    if report is None:
        return []
    items: list[dict[str, str]] = []
    for issue in list(report.issues_json or [])[:limit]:
        if not isinstance(issue, dict):
            continue
        evidence = ""
        spans = (
            issue.get("evidence_spans")
            if isinstance(issue.get("evidence_spans"), list)
            else []
        )
        if spans:
            first = spans[0]
            if isinstance(first, dict):
                evidence = str(
                    first.get("text")
                    or first.get("excerpt")
                    or first.get("snippet")
                    or ""
                ).strip()
        items.append(
            {
                "issue": str(
                    issue.get("message")
                    or issue.get("issue")
                    or issue.get("dimension")
                    or "质检提示"
                ).strip(),
                "evidence": evidence[:180],
                "suggested_action": str(
                    issue.get("recommendation")
                    or issue.get("next_action")
                    or report.next_action
                    or "先回到场景工作台处理。"
                ).strip(),
                "qc_report_id": report.qc_report_id,
            }
        )
    return items


def optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]

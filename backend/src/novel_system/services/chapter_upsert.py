"""v1 章接口的整行建 / 改（``POST /api/v1/chapters``，CLAUDE.md 里刻意保留的测试原语）。

从路由文件里搬出来（B08-16）：错误码与判定原样——终审只能走项目定稿流程、已存在的章不能换作品 / 换大纲计划、
章序不许和同作品的活跃章撞、已终审的章只接受「完全相同」的重放。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, ChapterState
from novel_system.services.chapter_approval import is_chapter_approved, require_chapter_mutation_allowed
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.errors import DomainError
from novel_system.services.text_validation import validate_user_text_payload
from novel_system.services.writer_briefs import normalize_chapter_writer_brief


def upsert_chapter(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """v1 ``POST /api/v1/chapters``：按 chapter_id 建章或整行更新（测试 / 运维入口）。"""
    validate_user_text_payload(payload, field_prefix="chapter")
    payload = {
        **payload,
        "writer_brief_json": normalize_chapter_writer_brief(payload.get("writer_brief_json")),
    }
    chapter = session.get(ChapterGoal, payload["chapter_id"])
    created = chapter is None
    _assert_chapter_display_order_available(
        session,
        chapter_id=payload["chapter_id"],
        project_id=(payload.get("project_id") if chapter is None else chapter.project_id),
        display_order=(
            payload.get("display_order")
            if "display_order" in payload
            else (chapter.display_order if chapter is not None else None)
        ),
    )
    if chapter is None:
        if str(payload.get("state") or "").strip() == "approved":
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        chapter = ChapterGoal(**payload)
        session.add(chapter)
        session.flush()
        changed = True
    else:
        if chapter.trashed_flag == 1:
            raise DomainError("CHAPTER_TRASHED", "chapter is currently in author trash")
        if (
            str(payload.get("state") or "").strip() == "approved"
            and not is_chapter_approved(session, chapter)
        ):
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        if "project_id" in payload and payload["project_id"] != chapter.project_id:
            raise DomainError(
                "CHAPTER_IDENTITY_IMMUTABLE",
                "an existing chapter cannot be moved to another project",
                status_code=409,
            )
        if "outline_plan_id" in payload and payload["outline_plan_id"] != chapter.outline_plan_id:
            raise DomainError(
                "CHAPTER_IDENTITY_IMMUTABLE",
                "an existing chapter cannot be rebound to another outline plan",
                status_code=409,
            )
        changed_fields = [
            key
            for key, value in payload.items()
            if key != "chapter_id" and getattr(chapter, key) != value
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="chapters.upsert",
        )
        if changed:
            for key, value in payload.items():
                setattr(chapter, key, value)

    state = session.get(ChapterState, payload["chapter_id"])
    # Replaying the same payload against a locked final is a true no-op: do not
    # opportunistically create runtime rows or touch update timestamps.
    should_create_state = state is None and (
        created or not is_chapter_approved(session, chapter)
    )
    if should_create_state:
        ensure_chapter_state(session, payload["chapter_id"])
        changed = True
    session.flush()
    return {"chapter_id": chapter.chapter_id, "changed": changed}


def _assert_chapter_display_order_available(
    session: Session,
    *,
    chapter_id: str,
    project_id: str | None,
    display_order: int | None,
) -> None:
    if project_id is None or display_order is None:
        return
    conflict = session.execute(
        select(ChapterGoal.chapter_id).where(
            ChapterGoal.project_id == project_id,
            ChapterGoal.display_order == int(display_order),
            ChapterGoal.trashed_flag == 0,
            ChapterGoal.chapter_id != chapter_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise DomainError(
            "CHAPTER_DISPLAY_ORDER_CONFLICT",
            "another active chapter already uses this display_order",
            status_code=409,
            details={
                "project_id": project_id,
                "display_order": int(display_order),
                "conflicting_chapter_id": conflict,
            },
        )

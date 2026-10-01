"""v1 场景接口的整行建 / 改（``POST /api/v1/scenes``，CLAUDE.md 里刻意保留的测试原语）。

从路由文件搬出（B12-01，与 ``chapter_upsert`` 同一个做法）：错误码与判定原样——场景不能换章 / 换作品 / 换大纲计划、
章内场序不许和活跃场景撞、已终审章里的改动走章级改动闸门、回收站里的场景不能改。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.chapter_approval import is_chapter_approved, require_chapter_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.text_input import validate_user_text_payload
from novel_system.services.writer_briefs import normalize_scene_writer_brief


def upsert_scene(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """v1 ``POST /api/v1/scenes``：按 scene_id 建场景卡或整行更新（测试 / 运维入口）。"""
    validate_user_text_payload(payload, field_prefix="scene")
    payload = {
        **payload,
        "writer_brief_json": normalize_scene_writer_brief(
            payload.get("writer_brief_json")
        ),
    }
    lifecycle = AuthorLifecycleService(session)
    chapter_id = payload.get("chapter_id")
    if not isinstance(chapter_id, str) or not chapter_id:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)

    chapter = lifecycle.require_active_chapter(chapter_id)

    scene = session.get(SceneCard, payload["scene_id"])
    created = scene is None
    effective_scene_seq = (
        payload.get("scene_seq")
        if payload.get("scene_seq") is not None
        else (
            scene.scene_seq
            if scene is not None
            else lifecycle.next_scene_append_seq(chapter_id)
        )
    )
    _assert_scene_seq_available(
        session,
        scene_id=payload["scene_id"],
        chapter_id=chapter_id,
        scene_seq=int(effective_scene_seq),
    )
    if scene is None:
        require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=["scenes.create"],
            operation="scenes.upsert_create",
        )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": lifecycle.next_scene_append_seq(chapter_id),
            }
        scene = SceneCard(**payload)
        session.add(scene)
        session.flush()
        changed = True
    else:
        if scene.trashed_flag == 1:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash")
        if payload["chapter_id"] != scene.chapter_id:
            raise DomainError(
                "SCENE_IDENTITY_IMMUTABLE",
                "an existing scene cannot be moved to another chapter",
                status_code=409,
            )
        if "project_id" in payload:
            requested_project_id = payload["project_id"]
            may_bind_from_chapter = (
                scene.project_id is None
                and requested_project_id is not None
                and requested_project_id == chapter.project_id
            )
            if requested_project_id != scene.project_id and not may_bind_from_chapter:
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be moved to another project",
                    status_code=409,
                )
        if "outline_plan_id" in payload:
            requested_outline_id = payload["outline_plan_id"]
            may_bind_from_chapter = (
                scene.outline_plan_id is None
                and requested_outline_id is not None
                and requested_outline_id == chapter.outline_plan_id
            )
            if (
                requested_outline_id != scene.outline_plan_id
                and not may_bind_from_chapter
            ):
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be rebound to another outline plan",
                    status_code=409,
                )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": scene.scene_seq,
            }
        changed_fields = [
            key
            for key, value in payload.items()
            if key not in {"scene_id", "chapter_id"} and getattr(scene, key) != value
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="scenes.upsert_update",
        )
        if changed:
            for key, value in payload.items():
                setattr(scene, key, value)

    state = session.get(SceneRunState, payload["scene_id"])
    should_create_state = state is None and (
        created or not is_chapter_approved(session, chapter)
    )
    if should_create_state:
        state = SceneRunState(scene_id=payload["scene_id"], scene_status="ready")
        session.add(state)
        changed = True
    session.flush()
    return {"scene_id": scene.scene_id, "changed": changed}


def _assert_scene_seq_available(
    session: Session,
    *,
    scene_id: str,
    chapter_id: str,
    scene_seq: int,
) -> None:
    conflict = session.execute(
        select(SceneCard.scene_id).where(
            SceneCard.chapter_id == chapter_id,
            SceneCard.scene_seq == scene_seq,
            SceneCard.trashed_flag == 0,
            SceneCard.scene_id != scene_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise DomainError(
            "SCENE_SEQUENCE_CONFLICT",
            "another active scene already uses this scene_seq",
            status_code=409,
            details={
                "chapter_id": chapter_id,
                "scene_seq": scene_seq,
                "conflicting_scene_id": conflict,
            },
        )


__all__ = ["upsert_scene"]

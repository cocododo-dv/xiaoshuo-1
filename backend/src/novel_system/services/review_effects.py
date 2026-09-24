"""FE-ALIGN Phase 5: 待办卡 effect 注册表（D4：后端事务执行）。

handler 签名 (session, project_id, payload) -> result dict。
resolve 端点在同一事务里执行 effect 并把卡置 resolved；未知 type 返回结构化错误。

首批 effect：
- insert_scene       复用 CatalogService.create_scene（P3）
- rename_chapter     复用 CatalogService.update_chapter
- create_entity / add_timeline_event  P6 资料库接通时注册

旧「应用画像」卡的 effect ``bind_style_profile`` 已删（2026-09-24，风格参考 v3 清理 S1）：界面直接绑定、不再发卡，
库里旧版写下的待办行由迁移 ``20260924_0092`` 删掉；再遇到这个 type 按未知 effect 拒绝。
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.services.errors import DomainError

EffectHandler = Callable[[Session, str, dict[str, Any]], dict[str, Any]]

_REGISTRY: dict[str, EffectHandler] = {}


def register_effect(effect_type: str, handler: EffectHandler) -> None:
    _REGISTRY[effect_type] = handler


def run_effect(
    session: Session, project_id: str | None, effect: dict[str, Any]
) -> dict[str, Any]:
    effect_type = str((effect or {}).get("type") or "").strip()
    handler = _REGISTRY.get(effect_type)
    if handler is None:
        raise DomainError(
            "REVIEW_EFFECT_UNKNOWN",
            f"unknown review effect type: {effect_type!r}",
            status_code=400,
            details={"effect_type": effect_type, "registered": sorted(_REGISTRY)},
        )
    if not project_id:
        raise DomainError(
            "REVIEW_EFFECT_PROJECT_REQUIRED",
            "effect execution requires a project context",
            status_code=400,
        )
    return handler(session, project_id, dict(effect))


# ---------------------------------------------------------------------------
# 首批 handler
# ---------------------------------------------------------------------------


def _insert_scene(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.catalog import CatalogService

    chapter_id = str(payload.get("chapter_id") or "").strip()
    if not chapter_id:
        raise DomainError(
            "REVIEW_EFFECT_INVALID", "insert_scene requires chapter_id", status_code=400
        )
    scene = dict(payload.get("scene") or {})
    body = {
        "title": scene.get("title"),
        "kind": scene.get("kind"),
        "state": scene.get("state") or "todo",
        "brief": dict(scene.get("brief") or {}),
    }
    if payload.get("at") is not None:
        body["at"] = payload["at"]
    return CatalogService(session).create_scene(project_id, chapter_id, body)


def _rename_chapter(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.catalog import CatalogService

    chapter_id = str(payload.get("chapter_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not chapter_id or not title:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            "rename_chapter requires chapter_id and title",
            status_code=400,
        )
    return CatalogService(session).update_chapter(
        project_id, chapter_id, {"title": title}
    )


def _create_entity(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.library import LibraryService

    return LibraryService(session).create_entity(
        project_id,
        {
            "name": payload.get("name"),
            "kind": payload.get("kind") or "concept",
            "summary": payload.get("summary") or "",
            "aliases": payload.get("aliases") or [],
            "details": payload.get("details") or {},
            "tags": payload.get("tags") or [],
        },
    )


def _add_timeline_event(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.library import LibraryService

    return LibraryService(session).create_timeline_event(
        project_id,
        {
            "label": payload.get("label"),
            "time_label": payload.get("time_label") or "",
            "chapter_ref": payload.get("chapter_ref") or "",
            "entity_refs": payload.get("entity_refs") or [],
            "note": payload.get("note") or "",
        },
    )


register_effect("insert_scene", _insert_scene)
register_effect("rename_chapter", _rename_chapter)
register_effect("create_entity", _create_entity)
register_effect("add_timeline_event", _add_timeline_event)

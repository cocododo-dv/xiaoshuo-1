"""FE-ALIGN Phase 5: 待办卡 effect 注册表（D4：后端事务执行）。

handler 签名 (session, project_id, payload) -> result dict。
resolve 端点在同一事务里执行 effect 并把卡置 resolved；未知 type 返回结构化错误。

首批 effect：
- insert_scene       复用 CatalogService.create_scene（P3）
- rename_chapter     复用 CatalogService.update_chapter
- bind_style_profile 复用 style_reference binding_apply.apply_style_profile(旧卡照样能批准)
- create_entity / add_timeline_event  P6 资料库接通时注册
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


def _bind_style_profile(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """旧「应用画像」决策卡(风格参考 v3 起界面直接绑定,不再发卡):待办里还没处理的旧卡照样能批准——
    走与 ``POST /profiles/{id}/apply`` 同一个 ``apply_style_profile``;卡上的旧键(strategy / intensity /
    sub_dimensions / include_* / draft_mode)由 ``normalize_binding_config`` 一次映射成 v3 配置。"""
    from novel_system.services.style_reference.binding_apply import apply_style_profile

    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            "bind_style_profile requires profile_id",
            status_code=400,
        )
    scope_value = str(payload.get("scope") or "project")
    raw_scope_ref = str(payload.get("scope_ref_id") or "").strip()
    # 立项 A — scene/character 级绑定必须显式带目标 id;缺失则拒绝(否则静默回退
    # project_id 会落成「场景级绑定却指向项目」的脏数据)。项目级缺省回退 project_id。
    if scope_value in ("scene", "character") and not raw_scope_ref:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            f"bind_style_profile scope={scope_value} requires scope_ref_id",
            status_code=400,
        )
    change = apply_style_profile(
        session,
        profile_id,
        scope=scope_value,
        scope_ref_id=raw_scope_ref or project_id,
        config=_legacy_binding_config(payload),
        legacy_strategy=str(payload.get("strategy") or "mixed"),
    )
    return {
        "profile_id": profile_id,
        "binding_id": change.binding.binding_id,
        "replaced": change.replaced,
    }


_LEGACY_CONFIG_KEYS = (
    "intensity",
    "sub_dimensions",
    "include_positive",
    "include_forbidden",
    "include_metric",
    "draft_mode",
    "reference_mode",
    "sample_windows",
    "dimension_states",
)


def _legacy_binding_config(payload: dict[str, Any]) -> dict[str, Any]:
    """旧决策卡 effect 载荷里的绑定配置键(原样取出,交给 ``normalize_binding_config`` 映射)。"""
    return {key: payload[key] for key in _LEGACY_CONFIG_KEYS if payload.get(key) is not None}


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
register_effect("bind_style_profile", _bind_style_profile)
register_effect("create_entity", _create_entity)
register_effect("add_timeline_event", _add_timeline_event)

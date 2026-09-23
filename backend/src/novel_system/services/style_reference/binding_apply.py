"""风格参考 v3 — 直接绑定:把一份画像用于作品 / 某一场 / 某个角色,改绑定配置,解除(台账 U1 / U9 / N2 / N8)。

过去「应用」只往待办里塞一张决策卡,批准后才绑定;改了参数再点一次会被待办去重静默吞掉,合成后的全局待办卡
还会绑到当时打开的作品上。现在一步到位:

- :func:`apply_style_profile`:直接写绑定行(同一画像 + 同一目标 → 更新这一行的配置;新目标 → 新建);
  **一个目标只有一条生效的绑定**——把另一份画像用于同一目标时,旧的那条置 ``disabled``,并在结果里说出来;
- :func:`update_binding_config`:改配置(``dimension_states`` 按维合并:文风画像页一次只改一维);
- :func:`remove_binding`:解除(删行);
- :func:`project_style_binding`:一部作品现在实际生效的绑定(用于作品页与各起草台读)。

配置一律经 ``binding_config.normalize_binding_config`` 落成 v3 四键(参考方式 / 样例窗数 / 维度状态 /
起草方式);旧 ``strategy`` 列恒写 ``mixed``(怎么送参考只看 ``reference_mode``)。参考变了(新绑定 / 配置变了 /
换了画像 / 解除),作用范围内按旧参考做的场景蓝图、人物压力蓝图与章架构作废,下一次运行重做。

待办里还没处理的旧「应用画像」卡(effect ``bind_style_profile``)也走 :func:`apply_style_profile`:
卡上的旧键(strategy / intensity / sub_dimensions / include_*)由 ``normalize_binding_config`` 一次映射。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    SceneCard,
    StoryCharacter,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.scene_planning_staleness import supersede_for_binding_scope
from novel_system.services.style_policy import style_policy_live
from novel_system.services.style_reference.binding_config import (
    effective_reference_mode,
    normalize_binding_config,
)
from novel_system.services.style_reference.schemas import (
    BindingStatus,
    InjectionStrategy,
    ProfileStatus,
    TaskType,
)
from novel_system.services.style_reference.inject.bindings import resolve_active_binding
from novel_system.services.style_reference.summaries import profile_summaries

APPLY_SCOPES: tuple[str, ...] = ("project", "scene", "character")
TASK_TYPE = TaskType.SCENE_GENERATION.value
BINDING_STRATEGY = InjectionStrategy.MIXED.value

APPLY_PARAM_INVALID_CODE = "STYLE_REFERENCE_APPLY_PARAM_INVALID"
APPLY_TARGET_NOT_FOUND_CODE = "STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND"
BINDING_NOT_FOUND_CODE = "STYLE_REFERENCE_BINDING_NOT_FOUND"
PROJECT_NOT_FOUND_CODE = "STYLE_REFERENCE_PROJECT_NOT_FOUND"

_TOP_LEVEL_KEYS = ("reference_mode", "sample_windows", "draft_mode")


@dataclass
class BindingChange:
    """一次绑定写入的结果(路由直接序列化)。"""

    binding: StyleReferenceInjectionBinding
    created: bool = False
    changed: bool = False
    replaced: list[dict[str, Any]] = field(default_factory=list)
    superseded_planning: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def stored_config(binding: StyleReferenceInjectionBinding) -> dict[str, Any]:
    """一条绑定按 v3 语义的配置(旧 strategy / intensity 在这里映射)。"""
    return normalize_binding_config(binding.strategy, binding.config_json or {})


def merge_config(base: Mapping[str, Any] | None, patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """``base``(已规范的 v3 配置)⊕ ``patch``:顶层三键覆盖,``dimension_states`` 按维合并;结果再规范一次。"""
    merged: dict[str, Any] = dict(normalize_binding_config(BINDING_STRATEGY, base or {}))
    patch = dict(patch or {})
    for key in _TOP_LEVEL_KEYS:
        if patch.get(key) is not None:
            merged[key] = patch[key]
    states = patch.get("dimension_states")
    if isinstance(states, Mapping):
        merged["dimension_states"] = {**dict(merged.get("dimension_states") or {}), **dict(states)}
    return normalize_binding_config(BINDING_STRATEGY, merged)


def binding_payload(
    binding: StyleReferenceInjectionBinding,
    *,
    cloud_policy: str | None = None,
) -> dict[str, Any]:
    """绑定的对外形状:v3 配置(``config``)+ 书的云策略压过之后真正生效的参考方式(``effective_reference_mode``)。

    ``config_json`` / ``strategy`` 原样留着(旧读者与审计);v3 起写入的绑定两者已经是规范值。
    """
    config = stored_config(binding)
    return {
        "binding_id": binding.binding_id,
        "profile_id": binding.profile_id,
        "scope": binding.scope,
        "scope_ref_id": binding.scope_ref_id,
        "task_type": binding.task_type,
        "status": binding.status,
        "strategy": binding.strategy,
        "config": config,
        "config_json": dict(binding.config_json or {}),
        "effective_reference_mode": effective_reference_mode(config["reference_mode"], cloud_policy=cloud_policy),
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def _profile_or_error(session: Session, profile_id: str) -> StyleReferenceProfile:
    profile = session.get(StyleReferenceProfile, str(profile_id))
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    if (profile.coverage_json or {}).get("stale"):
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_STALE",
            "这份画像的依据变过了,先对它的参考书「学习文风」,再用于作品。",
            status_code=409,
            details={
                "profile_id": profile_id,
                "author_action": {"action": "learn_style", "view": "styleref", "book_id": profile.book_id},
            },
        )
    if profile.status == ProfileStatus.ARCHIVED.value:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_ARCHIVED",
            "这份画像已经归档,不能再用于作品。",
            status_code=409,
            details={"profile_id": profile_id},
        )
    return profile


def _check_target(session: Session, scope: str, scope_ref_id: str) -> None:
    if scope not in APPLY_SCOPES:
        raise DomainError(
            APPLY_PARAM_INVALID_CODE,
            f"scope must be one of {APPLY_SCOPES}",
            status_code=400,
            details={"scope": scope},
        )
    if not scope_ref_id:
        raise DomainError(
            APPLY_PARAM_INVALID_CODE,
            f"scope={scope} requires a non-empty scope_ref_id",
            status_code=400,
        )
    missing = False
    if scope == "project":
        missing = session.get(StoryProject, scope_ref_id) is None
    elif scope == "scene":
        missing = session.get(SceneCard, scope_ref_id) is None
    if missing:
        raise DomainError(
            APPLY_TARGET_NOT_FOUND_CODE,
            "要用这份画像的作品 / 场景不存在(可能已经删掉了)。",
            status_code=404,
            details={"scope": scope, "scope_ref_id": scope_ref_id},
        )


def _supersede(session: Session, binding: StyleReferenceInjectionBinding, reason: str) -> dict[str, Any] | None:
    return supersede_for_binding_scope(
        session,
        scope=str(binding.scope),
        scope_ref_id=binding.scope_ref_id,
        reason=reason,
    )


def _replaced_payload(session: Session, bindings: list[StyleReferenceInjectionBinding]) -> list[dict[str, Any]]:
    if not bindings:
        return []
    rows = {
        str(pid): (title, book_id)
        for pid, title, book_id in session.execute(
            select(StyleReferenceProfile.profile_id, StyleReferenceProfile.title, StyleReferenceProfile.book_id).where(
                StyleReferenceProfile.profile_id.in_(sorted({str(b.profile_id) for b in bindings}))
            )
        )
    }
    books = {
        str(bid): title
        for bid, title in session.execute(
            select(StyleReferenceBook.book_id, StyleReferenceBook.title).where(
                StyleReferenceBook.book_id.in_(sorted({str(v[1]) for v in rows.values() if v[1]}))
            )
        )
    }
    out = []
    for binding in bindings:
        title, book_id = rows.get(str(binding.profile_id), (None, None))
        out.append(
            {
                "binding_id": binding.binding_id,
                "profile_id": binding.profile_id,
                "profile_title": title,
                "book_id": book_id,
                "book_title": books.get(str(book_id)) if book_id else None,
                "scope": binding.scope,
                "scope_ref_id": binding.scope_ref_id,
            }
        )
    return out


def apply_style_profile(
    session: Session,
    profile_id: str,
    *,
    scope: str,
    scope_ref_id: str | None,
    config: Mapping[str, Any] | None = None,
    legacy_strategy: str | None = None,
) -> BindingChange:
    """把画像用于一个目标(只 flush)。

    ``config``:v3 配置里要设的键(缺的键:这一行已有的配置保留、新建时取默认)。``legacy_strategy``:只给
    旧待办卡用——卡上的 strategy 与旧配置键一起经 ``normalize_binding_config`` 映射成 v3 配置。
    """
    scope = str(scope or "").strip()
    ref = str(scope_ref_id or "").strip()
    _check_target(session, scope, ref)
    profile = _profile_or_error(session, profile_id)
    patch: dict[str, Any] = dict(config or {})
    if legacy_strategy is not None:
        patch = normalize_binding_config(legacy_strategy, patch)

    same_target = list(
        session.scalars(
            select(StyleReferenceInjectionBinding)
            .where(
                StyleReferenceInjectionBinding.scope == scope,
                StyleReferenceInjectionBinding.scope_ref_id == ref,
                StyleReferenceInjectionBinding.task_type == TASK_TYPE,
            )
            .order_by(StyleReferenceInjectionBinding.created_at.desc(), StyleReferenceInjectionBinding.binding_id)
        )
    )
    # (画像, 范围, 目标, 任务) 有唯一约束:同一画像在同一目标上至多一行
    own = next((b for b in same_target if str(b.profile_id) == str(profile.profile_id)), None)
    others_active = [
        b for b in same_target if str(b.profile_id) != str(profile.profile_id) and b.status == BindingStatus.ACTIVE.value
    ]
    now = utcnow()
    created = False
    if own is not None:
        binding = own
        before = stored_config(binding)
        new_config = merge_config(before, patch)
        # 「变了」按语义算:v3 配置不同,或这一行原本不生效;旧格式的行改写成 v3 四键不算参考变了
        changed = new_config != before or binding.status != BindingStatus.ACTIVE.value
        binding.config_json = new_config
        binding.strategy = BINDING_STRATEGY
        binding.status = BindingStatus.ACTIVE.value
        binding.updated_at = now
    else:
        binding = StyleReferenceInjectionBinding(
            binding_id=f"sr_bind_{uuid.uuid4().hex[:12]}",
            profile_id=profile.profile_id,
            scope=scope,
            scope_ref_id=ref,
            task_type=TASK_TYPE,
            strategy=BINDING_STRATEGY,
            config_json=merge_config({}, patch),
            status=BindingStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
        created = True
        changed = True
    for other in others_active:
        other.status = BindingStatus.DISABLED.value
        other.updated_at = now
    # 用于作品即启用画像(注入只认 active 的画像)
    profile.status = ProfileStatus.ACTIVE.value
    session.flush()
    superseded = None
    if changed or others_active:
        superseded = _supersede(session, binding, f"style_binding_applied:{binding.binding_id}")
    return BindingChange(
        binding=binding,
        created=created,
        changed=changed or bool(others_active),
        replaced=_replaced_payload(session, others_active),
        superseded_planning=superseded,
    )


def update_binding_config(session: Session, binding_id: str, config: Mapping[str, Any] | None) -> BindingChange:
    """改一条绑定的配置(只 flush;``dimension_states`` 按维合并)。"""
    binding = session.get(StyleReferenceInjectionBinding, str(binding_id))
    if binding is None:
        raise DomainError(BINDING_NOT_FOUND_CODE, f"binding {binding_id!r} not found", status_code=404)
    before = stored_config(binding)
    new_config = merge_config(before, config)
    changed = new_config != before
    binding.config_json = new_config
    binding.strategy = BINDING_STRATEGY
    binding.updated_at = utcnow()
    session.flush()
    superseded = None
    if changed and binding.status == BindingStatus.ACTIVE.value:
        superseded = _supersede(session, binding, f"style_binding_config_changed:{binding.binding_id}")
    return BindingChange(binding=binding, changed=changed, superseded_planning=superseded)


def remove_binding(session: Session, binding_id: str) -> dict[str, Any]:
    """解除一条绑定(删行;只 flush)。它作用范围内按这本参考做的规划产物作废。"""
    binding = session.get(StyleReferenceInjectionBinding, str(binding_id))
    if binding is None:
        raise DomainError(BINDING_NOT_FOUND_CODE, f"binding {binding_id!r} not found", status_code=404)
    superseded = _supersede(session, binding, f"style_binding_deleted:{binding_id}")
    session.delete(binding)
    session.flush()
    return {"binding_id": binding_id, "deleted": True, "superseded_planning": superseded}


# ---------------------------------------------------------------------------
# 读:一部作品现在用的是哪一份
# ---------------------------------------------------------------------------


def project_style_binding(session: Session, project_id: str) -> dict[str, Any]:
    """一部作品现在实际生效的绑定(只读,不冻结契约、不写库)。

    ``binding``:项目层生效的那条(同层取最新;没有项目层时是全局层的旧绑定,``scope`` 如实给出);
    ``profile`` / ``book``:它的画像与参考书摘要;``policy``:按当前绑定现解析的风格策略审计(与写作台等无 bundle 的
    节点同一个口径——``style_policy_live(freeze_contract=False)``);``scene_bindings`` / ``character_bindings``:
    这部作品里场景级 / 角色级绑定的条数(它们在那一场 / 那个角色上盖过项目层)。
    """
    project = session.get(StoryProject, str(project_id))
    if project is None:
        raise DomainError(PROJECT_NOT_FOUND_CODE, f"project {project_id!r} not found", status_code=404)
    binding = resolve_active_binding(session, str(project_id), TASK_TYPE)
    policy = style_policy_live(
        session,
        SimpleNamespace(project_id=str(project_id), scene_id=None, pov_character_id=None, onstage_chars_json=None),
        task_type=TASK_TYPE,
        freeze_contract=False,
    )
    scene_count = 0
    character_count = 0
    scene_ids = [
        str(sid)
        for sid in session.scalars(select(SceneCard.scene_id).where(SceneCard.project_id == str(project_id)))
    ]
    if scene_ids:
        scene_count = len(
            list(
                session.scalars(
                    select(StyleReferenceInjectionBinding.binding_id).where(
                        StyleReferenceInjectionBinding.scope == "scene",
                        StyleReferenceInjectionBinding.scope_ref_id.in_(scene_ids),
                        StyleReferenceInjectionBinding.status == BindingStatus.ACTIVE.value,
                        StyleReferenceInjectionBinding.task_type == TASK_TYPE,
                    )
                )
            )
        )
    character_ids = [
        str(cid)
        for cid in session.scalars(select(StoryCharacter.character_id).where(StoryCharacter.project_id == str(project_id)))
    ]
    if character_ids:
        character_count = len(
            list(
                session.scalars(
                    select(StyleReferenceInjectionBinding.binding_id).where(
                        StyleReferenceInjectionBinding.scope == "character",
                        StyleReferenceInjectionBinding.scope_ref_id.in_(character_ids),
                        StyleReferenceInjectionBinding.status == BindingStatus.ACTIVE.value,
                        StyleReferenceInjectionBinding.task_type == TASK_TYPE,
                    )
                )
            )
        )
    payload: dict[str, Any] = {
        "project_id": str(project_id),
        "project_title": project.title,
        "binding": None,
        "profile": None,
        "book": None,
        "policy": policy.audit(),
        "scene_bindings": scene_count,
        "character_bindings": character_count,
    }
    if binding is None:
        return payload
    profile = session.get(StyleReferenceProfile, str(binding.profile_id))
    book = session.get(StyleReferenceBook, str(profile.book_id)) if profile is not None else None
    payload["binding"] = binding_payload(binding, cloud_policy=book.cloud_policy if book is not None else None)
    if profile is not None:
        summaries = profile_summaries(session, [profile.profile_id])
        payload["profile"] = summaries.get(profile.profile_id)
    if book is not None:
        payload["book"] = {
            "book_id": book.book_id,
            "title": book.title,
            "author_label": book.author_label,
            "cloud_policy": book.cloud_policy,
            "status": book.status,
        }
    return payload


__all__ = [
    "APPLY_SCOPES",
    "BINDING_STRATEGY",
    "BindingChange",
    "TASK_TYPE",
    "apply_style_profile",
    "binding_payload",
    "merge_config",
    "project_style_binding",
    "remove_binding",
    "stored_config",
    "update_binding_config",
]

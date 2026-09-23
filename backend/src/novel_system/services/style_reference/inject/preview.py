"""风格参考 v3 — 注入预览（J12 / U6 的后端）：与起草同一套选窗、同一个块次序，只算不写。

过去预览走的是另一套路径（不设对白配额、不看场景设计、滑一次滑块 0.75 秒重算根哈希），作者看到的和这一场
真正拿到的不一样。现在预览就是 :func:`render_style`：

- 按画像 + 一份（未落盘的）v3 绑定配置造一份预览用的契约（不校验、不入库），得到同样形状的 ``StylePolicy``；
- 给了 ``scene_id``：按这一场的设计（章内位置、场面标签、对白 / 概述倾向）与同一个种子算选窗——就是起草时会
  冻结的那组窗（蓝图另给了场面标签时以冻结行为准）；不写冻结行；
- 块次序与起草提示一致：system 前缀（指路句 + 文风卡 + 声音 + 红线）+ user 尾块（样例 + 收口）。

返回旧预览端点的形状（``fragments`` / ``prefix`` / ``stats`` / ``window_refs``，``stats`` 的键即
``InjectionPreviewStats``），外加 ``user_tail`` 与 ``reference_mode``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, StyleReferenceBook, StyleReferenceProfile
from novel_system.services.style_policy import policy_from_contract
from novel_system.services.style_reference.binding_config import (
    REFERENCE_MODE_CARD_ONLY,
    REFERENCE_MODE_SAMPLES_ONLY,
    normalize_binding_config,
)
from novel_system.services.style_reference.inject.gaps import recent_gaps_for_project
from novel_system.services.style_reference.inject.render import render_style
from novel_system.services.style_reference.inject.request import (
    PLACEMENT_USER_TAIL,
    ROLE_DRAFT,
    StyleRenderRequest,
)
from novel_system.services.style_reference.inject.selection import (
    derive_situation_tags,
    scene_chapter_position,
    scene_dialogue_heavy,
    scene_rendering_mode,
)
from novel_system.services.style_reference.policy import cloud_llm_allowed
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    frozen_profile_json,
    legacy_forbidden_findings,
)

PREVIEW_MODE = "preview"


def preview_contract(
    session: Session,
    profile: StyleReferenceProfile,
    config: Mapping[str, Any],
    *,
    strategy: str | None = None,
) -> dict[str, Any]:
    """预览用的契约（形状同 v2 契约的一层，不校验、不入库；契约哈希带 ``preview:`` 前缀）。"""
    repo = StyleReferenceRepository(session)
    raw_json = profile.profile_json if isinstance(profile.profile_json, Mapping) else {}
    profile_json = frozen_profile_json(raw_json)
    book = session.get(StyleReferenceBook, str(profile.book_id))
    stats = book.stats_json if book is not None and isinstance(book.stats_json, Mapping) else {}
    banned_terms = sorted(
        {
            str(term.term or "").strip()
            for term in repo.list_banned_terms(str(profile.profile_id), scope="generation")
            if str(term.term or "").strip()
        }
    )
    layer = {
        "order": 0,
        "binding": {
            "binding_id": f"preview:{profile.profile_id}",
            "profile_id": str(profile.profile_id),
            "scope": "preview",
            "scope_ref_id": "",
            "task_type": "scene_generation",
            "strategy": str(strategy or "mixed"),
            "status": "active",
            "config_json": dict(config),
        },
        "profile": {
            "profile_id": str(profile.profile_id),
            "book_id": str(profile.book_id),
            "status": str(profile.status),
            "profile_json": profile_json,
        },
        "forbidden_findings": legacy_forbidden_findings(repo, profile, raw_json),
        "banned_terms": banned_terms,
        "book": {
            "book_id": str(profile.book_id),
            "cloud_policy": str(getattr(book, "cloud_policy", "") or ""),
            "cloud_llm_allowed_at_freeze": bool(book is not None and cloud_llm_allowed(book)),
            "paragraph_root_sha256": stats.get("paragraph_root_sha256"),
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            {"profile": str(profile.profile_id), "updated": str(profile.updated_at), "config": dict(config), "terms": banned_terms},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "layers": [layer],
        "layer_count": 1,
        "draft_mode": str(config.get("draft_mode") or "style_first"),
        "contract_hash": f"preview:{digest}",
    }


def _strategy_label(reference_mode: str) -> str:
    """旧 ``fragments.strategy`` 字段（前端旧页面按它显示提示）：只用卡 → A，只用样例 → B，全面模仿 → mixed。"""
    if reference_mode == REFERENCE_MODE_CARD_ONLY:
        return "A"
    if reference_mode == REFERENCE_MODE_SAMPLES_ONLY:
        return "B"
    return "mixed"


def preview_render(
    session: Session,
    profile_id: str,
    config: Mapping[str, Any] | None,
    *,
    scene_id: str | None = None,
    project_id: str | None = None,
    strategy: str | None = None,
) -> dict[str, Any]:
    """按 (画像, 绑定配置[, 场景 / 作品]) 渲染起草时的参考；画像不存在 → ``LookupError``。"""
    profile = session.get(StyleReferenceProfile, str(profile_id))
    if profile is None:
        raise LookupError(profile_id)
    normalized = normalize_binding_config(strategy, dict(config or {}))
    contract = preview_contract(session, profile, normalized, strategy=strategy)
    policy = policy_from_contract(contract, mode=PREVIEW_MODE)
    scene = session.get(SceneCard, str(scene_id)) if scene_id else None
    project = project_id or (getattr(scene, "project_id", None) if scene is not None else None)
    request = StyleRenderRequest(
        role=ROLE_DRAFT,
        placement=PLACEMENT_USER_TAIL,
        scene_id=str(scene_id) if scene_id else None,
        position=scene_chapter_position(scene),
        situation_tags=derive_situation_tags(scene),
        dialogue_heavy=scene_dialogue_heavy(scene),
        rendering_mode=scene_rendering_mode(scene),
        recent_gaps=recent_gaps_for_project(session, project_id=project, profile_id=str(profile.profile_id)),
    )
    rendered = render_style(
        session,
        policy,
        request,
        scene=scene,
        persist_selection=False,
        use_cache=False,
        commit_index=True,
    )
    parts = rendered.parts
    blocks: dict[str, Any] = {"samples": "", "card": "", "voice": "", "red_line": ""}
    if parts is not None and not rendered.empty:
        _prefix, _tail, assembled = parts.assemble()
        blocks.update({name: str(assembled.get(name) or "") for name in blocks})
    fragments = {
        "positive_block": blocks["card"],
        "forbidden_block": "",
        "metric_anchor_block": "",
        "voice_block": blocks["voice"],
        "few_shot_block": blocks["samples"],
        "anti_plagiarism_block": blocks["red_line"],
        "strategy": _strategy_label(policy.reference_mode),
    }
    return {
        "fragments": fragments,
        "prefix": rendered.system_prefix,
        "user_tail": rendered.user_tail,
        "stats": dict(rendered.stats) if rendered.stats else {},
        "window_refs": [dict(item) for item in rendered.window_refs],
        "reference_mode": policy.reference_mode,
        "sample_windows": policy.sample_windows,
        "audit": dict(rendered.audit),
    }


__all__ = ["PREVIEW_MODE", "preview_contract", "preview_render"]

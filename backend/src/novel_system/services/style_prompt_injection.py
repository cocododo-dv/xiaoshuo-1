"""Shared ``[STYLE_REFERENCE]`` injection for every node that writes, revises, reviews or plans.

风格模仿 v2：``inject_style_reference_prefix`` 原是 ``scene_generation`` 的模块级函数，
``qc_engine.SoftQcEngine`` 也要用它给 soft_qc 注入同一冻结契约前缀。两个模块互相
import 会形成依赖环（架构守卫 ``tests/test_service_architecture.py``），因此抽到这个
不依赖二者的中立模块；``scene_generation`` 再导出同名符号以保持既有调用面。

风格参考 v3（2026-09-23）：本模块只是一层薄适配——

1. 解析**一份**风格策略（``StylePolicy``）：调用方给了契约 → 那份契约（``resolved``）；给了 bundle → bundle 里
   冻结的契约（``style_policy_for_bundle``，按契约哈希记忆）；没有 bundle（写作台、章级评审）或旧 bundle 没冻结
   契约 → 按当前活动绑定现解析（``style_policy_live``，审计 ``mode=live`` 并记契约哈希——原来那条不记哈希的
   ``legacy_live`` 路径没有了，J15）；
2. 按调用参数造一个 ``StyleRenderRequest``（新参数 ``role``；不给时按旧参数推断：样例进 user 尾部 → 起草，
   窗数上限 ≤3 → 规划，=4 → 评审，其余 → 起草）；场景的章内位置、场面标签、对白 / 概述倾向从场景设计推，
   首稿自动带上近期常见偏差；
3. ``render_style`` 渲染（每场冻结选窗、进程内缓存），``fit_rendered`` 贪心压进预算；
4. 返回的 prompt 字典与以前同形：``system_prompt`` 前缀、``STYLE_USER_TAIL_KEY`` 尾块、
   ``_style_reference_runtime_audit`` 审计。

``context_text``（被润色的稿子）不再影响任何东西——选窗只看本场设计（J2 / J4），参数保留只为调用面不变。
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.style_policy import (
    MODE_ABSENT,
    MODE_DEGRADED,
    MODE_LIVE,
    MODE_NONE,
    StylePolicy,
    policy_from_contract,
    style_policy_for_bundle,
    style_policy_live,
)
from novel_system.services.style_reference.inject.fit import fit_rendered
from novel_system.services.style_reference.inject.gaps import recent_gaps_for_project
from novel_system.services.style_reference.inject.render import (
    attach_chapter_position_mandate,
    chapter_position_mandate,
    render_style,
)
from novel_system.services.style_reference.inject.request import (
    PLACEMENT_SYSTEM,
    PLACEMENT_USER_TAIL,
    PLAN_K,
    REVIEW_K,
    ROLE_DRAFT,
    StyleRenderRequest,
    infer_role,
)
from novel_system.services.style_reference.inject.selection import (
    derive_situation_tags,
    scene_chapter_position,
    scene_dialogue_heavy,
    scene_rendering_mode,
)
from novel_system.services.style_reference.runtime_contract import (
    style_runtime_contract_status_from_bundle,
    validate_style_runtime_contract,
)

_LOGGER = logging.getLogger(__name__)

# styled-draft gate 自身未能执行时的 verdict。``qc_engine``（产出）与 ``scene_generation``
# （翻译成 STYLE_GATE_UNAVAILABLE notice）都要认它；两者不能互相 import，所以放在这里。
STYLED_GATE_UNAVAILABLE_VERDICT = "unavailable"

# 规划节点（scene_blueprint、写作台的章级 / 局部节点）只要冻结选窗的前 3 窗，评审节点前 4 窗（L4）。
PLANNING_FEW_SHOT_K_CAP = PLAN_K
REVIEW_FEW_SHOT_K_CAP = REVIEW_K
# 调用方显式给出契约(而非 bundle)时的审计标签:契约是本次调用按当前 active 绑定解析的,
# 与冻结进 SceneBundle 的契约区分开。
RESOLVED_CONTRACT_STATUS = "resolved_live"
RESOLVED_CONTRACT_MODE = "resolved"
# 没有 bundle（或旧 bundle 没冻结契约）时按当前活动绑定现解析的审计标签（J15：记契约哈希）。
LIVE_CONTRACT_STATUS = "live"
# 注入器把样例尾巴放在 prompt dict 的这个键下;调用方用 :func:`apply_style_user_tail` 接到最终
# user prompt 上(runner 的 user_prompt 是单独传的,注入器改不到)。
STYLE_USER_TAIL_KEY = "_style_reference_user_tail"
STYLE_RUNTIME_AUDIT_KEY = "_style_reference_runtime_audit"

__all__ = [
    "LIVE_CONTRACT_STATUS",
    "PLACEMENT_SYSTEM",
    "PLACEMENT_USER_TAIL",
    "PLANNING_FEW_SHOT_K_CAP",
    "RESOLVED_CONTRACT_MODE",
    "RESOLVED_CONTRACT_STATUS",
    "REVIEW_FEW_SHOT_K_CAP",
    "STYLED_GATE_UNAVAILABLE_VERDICT",
    "STYLE_RUNTIME_AUDIT_KEY",
    "STYLE_USER_TAIL_KEY",
    "apply_style_user_tail",
    "attach_chapter_position_mandate",
    "chapter_position_mandate",
    "inject_style_reference_prefix",
    "resolve_style_scope",
    "style_render_request_for_scene",
]


def apply_style_user_tail(prompt: Mapping[str, Any] | None, user_prompt: str) -> str:
    """把注入器留下的样例尾巴接到最终 user prompt 末尾;没有尾巴时原样返回。"""
    tail = prompt.get(STYLE_USER_TAIL_KEY) if isinstance(prompt, Mapping) else None
    if not tail:
        return user_prompt
    return str(user_prompt).rstrip() + str(tail)


def resolve_style_scope(
    session: Session,
    *,
    scene_id: str | None = None,
    chapter_id: str | None = None,
    project_id: str | None = None,
) -> Any | None:
    """写手侧 / 章级调用的风格作用域对象(WP6.3)。

    ``inject_style_reference_prefix`` 只按属性读作用域(``project_id`` / ``scene_id`` /
    ``pov_character_id`` / ``onstage_chars_json`` / 选窗提示),所以:

    - 有场景行 → 直接用 ``SceneCard``(scene > character > project > global 全部作用域;
      旧场景行没有 ``project_id`` 时补上其章的 ``project_id``,否则 project 层绑定看不见);
    - 只有章 / 项目(整章稿、项目稿、章级评审) → 一个只带 ``project_id`` 的作用域对象
      (project + global 层;无场景种子,窗口按契约确定);
    - 连项目都定不出 → ``None``(调用方跳过注入)。
    """
    scene = session.get(SceneCard, str(scene_id)) if scene_id else None
    if scene is not None and getattr(scene, "project_id", None):
        return scene
    resolved_project = str(project_id or "").strip() or None
    lookup_chapter_id = chapter_id or (getattr(scene, "chapter_id", None) if scene is not None else None)
    if not resolved_project and lookup_chapter_id:
        chapter = session.get(ChapterGoal, str(lookup_chapter_id))
        resolved_project = getattr(chapter, "project_id", None) if chapter is not None else None
    if not resolved_project:
        return scene  # 场景层 / 角色层绑定仍可命中;None 时调用方直接跳过
    if scene is not None:
        return SimpleNamespace(
            project_id=str(resolved_project),
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            pov_character_id=getattr(scene, "pov_character_id", None),
            onstage_chars_json=list(getattr(scene, "onstage_chars_json", None) or []),
            scene_seq=getattr(scene, "scene_seq", 0),
            is_chapter_last=getattr(scene, "is_chapter_last", 0),
            writer_brief_json=dict(getattr(scene, "writer_brief_json", None) or {}),
            scene_goal=getattr(scene, "scene_goal", None),
            beats_json=list(getattr(scene, "beats_json", None) or []),
            scene_type=getattr(scene, "scene_type", None),
        )
    return SimpleNamespace(
        project_id=str(resolved_project),
        scene_id=None,
        chapter_id=str(lookup_chapter_id) if lookup_chapter_id else None,
        pov_character_id=None,
        onstage_chars_json=[],
        scene_seq=0,
        is_chapter_last=0,
        writer_brief_json={},
    )


def _bundle_id(bundle: Mapping[str, Any] | None) -> str | None:
    if not isinstance(bundle, Mapping):
        return None
    value = str(bundle.get("bundle_id") or "").strip()
    return value or None


def style_render_request_for_scene(
    session: Session,
    scene: Any,
    policy: StylePolicy,
    *,
    role: str,
    placement: str,
    k_cap: int | None = None,
    bundle_id: str | None = None,
    situation_tags: Sequence[str] | None = None,
    recent_gaps: Sequence[str] | None = None,
    revise_dimensions: Sequence[str] | None = None,
) -> StyleRenderRequest:
    """场景（或作用域对象）→ 渲染请求：章内位置、场面标签、对白 / 概述倾向从场景设计推；首稿默认带近期偏差。"""
    if recent_gaps is None:
        recent_gaps = (
            recent_gaps_for_project(
                session,
                project_id=getattr(scene, "project_id", None),
                profile_id=policy.profile_id,
            )
            if role == ROLE_DRAFT
            else ()
        )
    return StyleRenderRequest(
        role=role,
        placement=placement,
        k_cap=k_cap,
        scene_id=getattr(scene, "scene_id", None),
        bundle_id=bundle_id,
        position=scene_chapter_position(scene),
        situation_tags=tuple(situation_tags) if situation_tags is not None else derive_situation_tags(scene),
        dialogue_heavy=scene_dialogue_heavy(scene),
        rendering_mode=scene_rendering_mode(scene),
        revise_dimensions=tuple(revise_dimensions or ()),
        recent_gaps=tuple(recent_gaps or ()),
    )


def _resolve_policy(
    session: Session,
    scene: Any,
    bundle: Mapping[str, Any] | None,
    runtime_contract: Mapping[str, Any] | None,
    *,
    task_type: str,
) -> tuple[StylePolicy, str | None, str]:
    """(策略, runtime_contract_status, runtime_contract_mode)。"""
    if runtime_contract is not None:
        contract = validate_style_runtime_contract(runtime_contract)
        return (
            policy_from_contract(contract, mode=RESOLVED_CONTRACT_MODE),
            RESOLVED_CONTRACT_STATUS,
            RESOLVED_CONTRACT_MODE,
        )
    if bundle is not None:
        policy = style_policy_for_bundle(bundle, task_type=task_type)
        if policy.mode != MODE_NONE:
            status = style_runtime_contract_status_from_bundle(bundle, task_type=task_type)
            return policy, status, policy.mode
    # 没有 bundle / 旧 bundle 没冻结任何契约记录：按当前活动绑定现解析（记契约哈希）
    policy = style_policy_live(session, scene, task_type=task_type)
    return policy, LIVE_CONTRACT_STATUS, policy.mode if policy.mode != MODE_NONE else MODE_LIVE


def _degraded(
    prompt: dict[str, Any],
    *,
    task_type: str,
    status: str | None,
    mode: str | None,
    error_code: str | None,
) -> dict[str, Any]:
    degraded = dict(prompt)
    degraded[STYLE_RUNTIME_AUDIT_KEY] = {
        "outcome": "degraded",
        "task_type": task_type,
        "runtime_contract_status": status,
        "runtime_contract_mode": mode,
        "error_code": error_code,
    }
    return degraded


def inject_style_reference_prefix(
    session: Session,
    prompt: dict[str, Any] | None,
    scene: SceneCard | None,
    bundle: dict[str, Any] | None = None,
    *,
    task_type: str = "scene_generation",
    context_text: str | None = None,
    final_user_prompt: str | None = None,
    few_shot_k_cap: int | None = None,
    runtime_contract: Mapping[str, Any] | None = None,
    placement: str = PLACEMENT_SYSTEM,
    role: str | None = None,
    situation_tags: Sequence[str] | None = None,
    recent_gaps: Sequence[str] | None = None,
    revise_dimensions: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """把风格参考渲染进提示：``system_prompt`` 前缀 + （起草 / 改稿）user 尾块 + 审计。

    无绑定 / 无作用域 → 原样返回；bundle 明确冻结了「没有绑定」→ 原样返回；契约损坏 / 渲染失败 → 基础
    prompt + ``outcome="degraded"`` 审计（风格参考是增强，不阻断生成）。``final_user_prompt`` 与模板的
    ``token_budget.target_input_tokens`` 都在时贪心压预算（整窗 / 整句，红线不截）。

    v3 新参数（都可不传）：``role``（draft / revise / review / plan）、``situation_tags``（蓝图给的场面标签，
    缺省从场景设计推）、``recent_gaps``（缺省：起草角色读作品最近的读数）、``revise_dimensions``（改稿要改的维）。
    ``context_text`` 保留签名但不再使用。
    """
    del context_text  # 选窗与渲染都不看草稿（J2 / J4）
    if prompt is None or scene is None:
        return prompt
    project_id = getattr(scene, "project_id", None)
    scene_id = getattr(scene, "scene_id", None)
    has_characters = bool(getattr(scene, "pov_character_id", None) or getattr(scene, "onstage_chars_json", None))
    if not project_id and not scene_id and not has_characters and runtime_contract is None and bundle is None:
        return prompt
    status: str | None = None
    mode: str | None = None
    try:
        policy, status, mode = _resolve_policy(session, scene, bundle, runtime_contract, task_type=task_type)
        if policy.mode == MODE_ABSENT:
            # 这份 bundle 明确冻结了「没有绑定」：后来加的绑定不能改变已建场景的重放
            return prompt
        if policy.mode == MODE_DEGRADED:
            return _degraded(prompt, task_type=task_type, status=status, mode=mode, error_code=policy.error_code)
        if not policy.bound:
            return prompt
        request = style_render_request_for_scene(
            session,
            scene,
            policy,
            role=role or infer_role(placement, few_shot_k_cap),
            placement=placement,
            k_cap=few_shot_k_cap,
            bundle_id=_bundle_id(bundle),
            situation_tags=situation_tags,
            recent_gaps=recent_gaps,
            revise_dimensions=revise_dimensions,
        )
        rendered = render_style(session, policy, request, scene=scene)
        budget_fit: dict[str, Any] | None = None
        token_budget = prompt.get("token_budget") or {}
        target_input_tokens = token_budget.get("target_input_tokens") if isinstance(token_budget, Mapping) else None
        if final_user_prompt is not None and target_input_tokens is not None and not rendered.empty:
            rendered, budget_fit = fit_rendered(
                rendered,
                base_system_prompt=str(prompt.get("system_prompt") or ""),
                user_prompt=final_user_prompt,
                target_input_tokens=int(target_input_tokens),
            )
    except Exception as exc:  # noqa: BLE001 — 风格参考是增强：渲染失败回退基础 prompt 并记审计
        _LOGGER.warning(
            "style_reference injection skipped for scene %s task %s: %s",
            getattr(scene, "scene_id", None),
            task_type,
            exc,
        )
        return _degraded(
            prompt,
            task_type=task_type,
            status=status,
            mode=mode,
            error_code=getattr(exc, "code", exc.__class__.__name__),
        )
    prefix = rendered.system_prefix
    user_tail = rendered.user_tail
    if not prefix and not user_tail and policy.mode == MODE_LIVE and not (budget_fit or {}).get("compacted"):
        # 现解析路径渲染不出任何东西（画像空）：保持严格 no-op，没有冻结谱系可记
        return prompt
    injected = dict(prompt)
    if prefix:
        injected["system_prompt"] = prefix + (prompt.get("system_prompt") or "")
    if user_tail:
        injected[STYLE_USER_TAIL_KEY] = user_tail
    rendered_text = prefix + user_tail
    # 渲染结果是进程内缓存的共享对象：审计给调用方一份深拷贝（下游会往里补字段再落库）
    audit = {
        **copy.deepcopy(dict(rendered.audit)),
        "task_type": task_type,
        "runtime_contract_status": status,
        "runtime_contract_mode": mode,
        "placement": request.placement,
        "prefix_chars": len(rendered_text),
        "prefix_sha256": hashlib.sha256(rendered_text.encode("utf-8")).hexdigest(),
    }
    if budget_fit is not None:
        audit["budget_fit"] = budget_fit
        if not rendered_text and budget_fit.get("style_payload_omitted"):
            audit["outcome"] = "degraded_budget"
    injected[STYLE_RUNTIME_AUDIT_KEY] = audit
    return injected

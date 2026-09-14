"""Shared ``[STYLE_REFERENCE]`` prefix injection for scene generation and QC.

风格模仿 v2：``inject_style_reference_prefix`` 原是 ``scene_generation`` 的模块级函数，
``qc_engine.SoftQcEngine`` 也要用它给 soft_qc 注入同一冻结契约前缀。两个模块互相
import 会形成依赖环（架构守卫 ``tests/test_service_architecture.py``），因此抽到这个
不依赖二者的中立模块；``scene_generation`` 再导出同名符号以保持既有调用面。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.style_reference.injection import (
    scene_sampling_hints,
    InjectionService,
    fit_fragments_to_input_budget,
    ordered_character_ids,
)
from novel_system.services.style_reference.runtime_contract import (
    StyleRuntimeContractState,
    extract_style_generation_context,
    resolve_style_runtime_contract_state,
)

_LOGGER = logging.getLogger(__name__)

# styled-draft gate 自身未能执行时的 verdict。``qc_engine``（产出）与 ``scene_generation``
# （翻译成 STYLE_GATE_UNAVAILABLE notice）都要认它；两者不能互相 import，所以放在这里。
STYLED_GATE_UNAVAILABLE_VERDICT = "unavailable"

# 2026-09-14 保真修补(WP6):规划(scene_blueprint)与评审 / 局部补丁(writer_deep_review /
# writer_passage_patch)节点只要少量样例窗口;起草类调用不传上限,行为逐字不变。
PLANNING_FEW_SHOT_K_CAP = 3
# 调用方显式给出契约(而非 bundle)时的审计标签:契约是本次调用按当前 active 绑定解析的,
# 与冻结进 SceneBundle 的契约区分开。
RESOLVED_CONTRACT_STATUS = "resolved_live"
RESOLVED_CONTRACT_MODE = "resolved"

__all__ = [
    "PLANNING_FEW_SHOT_K_CAP",
    "RESOLVED_CONTRACT_MODE",
    "RESOLVED_CONTRACT_STATUS",
    "STYLED_GATE_UNAVAILABLE_VERDICT",
    "inject_style_reference_prefix",
    "resolve_style_scope",
]


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
      (project + global 层;无场景种子,窗口取确定性前 k);
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
) -> dict[str, Any] | None:
    """PR-8 §5.1 — 把冻结契约渲染成 ``[STYLE_REFERENCE]`` 前缀，prepend 到 system_prompt。

    无 binding / project_id / profile 时 no-op（返回原对象）；有候选 binding 但召回 /
    渲染失败时吞掉异常、回退基础 prompt 并在 ``_style_reference_runtime_audit`` 记
    ``outcome="degraded"``（风格注入是可选增强，不阻断 LLM 生成流程）。

    2026-09 v2（W5）：抽成模块级函数，``SceneGenerationService._inject_style_reference``
    与 ``qc_engine.SoftQcEngine``（soft_qc 阶段注入同一前缀）共用；反抄袭红线段随任一
    风格块非空必附、原文样例经 secure_reference_block 封装、cloud_llm_allowed 守卫都在
    InjectionService 内，本函数不触碰。

    立项 C §12 — ``context_text``（续写最新正文）透传给 Strategy C（RAG）作为三粒度检索
    query；其余策略忽略此参数。

    2026-09-14 保真修补(WP6):``few_shot_k_cap`` 把样例窗口数压到 ``min(k(intensity), cap)``
    (规划 / 评审 / 局部补丁节点用 :data:`PLANNING_FEW_SHOT_K_CAP`;不传 = 起草通道逐字不变)。
    ``runtime_contract`` 让调用方直接给出**本次调用已解析**的契约(scene_blueprint 的来源快照
    按当前 active 绑定解析契约并登记其哈希,前缀必须与那份契约同源),此时不看 ``bundle``;审计
    记 ``runtime_contract_status=resolved_live`` / ``mode=resolved``。
    """
    if prompt is None or scene is None:
        return prompt
    project_id = getattr(scene, "project_id", None)
    # PR-14/18 — character scope 用 pov ∪ onstage 匹配集(pov 优先)
    character_ids = ordered_character_ids(
        getattr(scene, "pov_character_id", None),
        getattr(scene, "onstage_chars_json", None),
    )
    # PR-15 — scene scope 用 scene_id 匹配(优先级最高)
    scene_id = getattr(scene, "scene_id", None)
    if not project_id and not character_ids and not scene_id:
        return prompt
    svc = InjectionService(session)
    svc.few_shot_k_cap = int(few_shot_k_cap) if few_shot_k_cap is not None else None
    # 2026-09-09 样例优先:few-shot 窗口按场景轮换——同一场景的 style_draft / soft_qc /
    # 近终稿改写 / 验收评审看到同一组窗口,不同场景看到不同窗口。
    svc.few_shot_seed = str(scene_id) if scene_id else None
    # 2026-09-14 保真修补(WP3.4):章首 / 章末场偏好参考书的开章 / 收章窗口,概述场偏好叙述窗口——
    # 第一稿没有可分析的正文时这是选窗唯一的场景信号。
    svc.scene_position, svc.scene_hint_types = scene_sampling_hints(scene)
    # §9 Defect B: read drift_ptype_priority from bundle (set by bundle_builder
    # when drift guidance includes structured dimension data) so the few-shot
    # selection prioritizes exemplars relevant to drifted dimensions ("show > tell")
    snapshot = (
        bundle.get("snapshot")
        if bundle
        and isinstance(bundle, dict)
        and isinstance(bundle.get("snapshot"), dict)
        else bundle
    )
    if isinstance(snapshot, dict):
        drift_priority = (snapshot.get("inline_digests") or {}).get(
            "_drift_ptype_priority"
        )
        # bundle inline_digests 只能存 str（哈希投影约束），W6 以 JSON 字符串写入；
        # 旧的 list 形式也继续接受。
        if isinstance(drift_priority, str) and drift_priority.strip():
            try:
                drift_priority = json.loads(drift_priority)
            except ValueError:
                drift_priority = None
        if drift_priority and isinstance(drift_priority, list):
            svc.drift_ptype_priority = [str(p) for p in drift_priority if str(p).strip()]
    # All callers now share one bounded prose-context extractor. The initial
    # style pass supplies the neutral draft; continuation calls supply the
    # latest accumulated prose.
    from novel_system.services.style_reference.rag import load_rag_config

    context = extract_style_generation_context(
        context_text,
        source_kind="generation_source" if context_text else "profile_fallback",
        max_chars=int(load_rag_config().get("rag_context_query_max_chars", 2000)),
    )
    # 调用方显式给出的契约与下面按 bundle 解析出的 ``runtime_contract`` 局部变量分开持有
    caller_contract = dict(runtime_contract) if runtime_contract is not None else None
    runtime_contract = None
    contract_state = None
    try:
        if caller_contract is not None:
            contract_state = StyleRuntimeContractState(
                status=RESOLVED_CONTRACT_STATUS,
                mode=RESOLVED_CONTRACT_MODE,
                contract=caller_contract,
            )
        else:
            contract_state = resolve_style_runtime_contract_state(
                bundle,
                task_type=task_type,
            )
        runtime_contract = contract_state.contract
        if contract_state.error_code is not None:
            raise ValueError(contract_state.error_code)
        if runtime_contract is not None:
            fragments = svc.fragments_for_contract(
                runtime_contract,
                project_id=project_id,
                context=context,
                drift_ptype_priority=svc.drift_ptype_priority,
            )
        elif contract_state.mode == "absent":
            # This new bundle explicitly froze "no style binding". A binding
            # added later must not alter replay of the already-built scene.
            return prompt
        else:
            # Backward compatibility for old bundles created before the frozen
            # runtime contract. New bundles never re-resolve live bindings here.
            svc.context_text = context.query_text
            fragments = svc.fragments_for(
                project_id,
                task_type,
                character_ids=character_ids,
                scene_id=scene_id,
            )
        budget_fit_audit = None
        token_budget = prompt.get("token_budget") or {}
        target_input_tokens = token_budget.get("target_input_tokens")
        if final_user_prompt is not None and target_input_tokens is not None:
            fragments, budget_fit_audit = fit_fragments_to_input_budget(
                fragments,
                base_system_prompt=str(prompt.get("system_prompt") or ""),
                user_prompt=final_user_prompt,
                target_input_tokens=int(target_input_tokens),
            )
        prefix = fragments.to_system_prompt_prefix()
    except Exception as exc:  # noqa: BLE001
        # 风格注入是可选增强：召回/渲染失败时吞掉并回退到基础 prompt，不阻断 LLM 生成
        # 流程（顾问型降级，与离线退役无关）。
        _LOGGER.warning(
            "style_reference injection skipped for scene %s task %s: %s",
            getattr(scene, "scene_id", None),
            task_type,
            exc,
        )
        degraded = dict(prompt)
        degraded["_style_reference_runtime_audit"] = {
            "outcome": "degraded",
            "task_type": task_type,
            "context": context.audit_dict(),
            "runtime_contract_status": (
                contract_state.status if contract_state is not None else None
            ),
            "runtime_contract_mode": (
                contract_state.mode if contract_state is not None else None
            ),
            "error_code": (
                contract_state.error_code
                if contract_state is not None
                and contract_state.error_code is not None
                else getattr(exc, "code", exc.__class__.__name__)
            ),
        }
        return degraded
    if (
        not prefix
        and runtime_contract is None
        and not (budget_fit_audit or {}).get("compacted")
    ):
        # Preserve the established strict no-op contract for legacy scenes
        # with no applicable binding. The miss is already recorded by the
        # injection metric; there is no frozen lineage to attach to the LLM
        # request or attempt record.
        return prompt
    injected = dict(prompt)
    if prefix:
        injected["system_prompt"] = prefix + (prompt.get("system_prompt") or "")
    if svc.last_runtime_audit is not None:
        assert contract_state is not None
        injected["_style_reference_runtime_audit"] = {
            **svc.last_runtime_audit,
            "runtime_contract_status": contract_state.status,
            "runtime_contract_mode": contract_state.mode,
        }
        if budget_fit_audit is not None:
            injected["_style_reference_runtime_audit"].update(
                {
                    "outcome": (
                        "hit"
                        if prefix
                        else "degraded_budget"
                    ),
                    "prefix_chars": len(prefix),
                    "prefix_sha256": hashlib.sha256(
                        prefix.encode("utf-8")
                    ).hexdigest(),
                    "budget_fit": budget_fit_audit,
                }
            )
    return injected

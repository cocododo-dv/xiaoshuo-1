"""Best-of-N 风格候选（``NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED``，默认关）。

只有作者手笔直起才出多稿：候选 = 首稿 + (N−1) 个定向修改，先按抄袭门、再按读数 distance 排序（最像的在前，
首稿赢平手），每个候选写排序审计（``ranking_audit``）；关键场景的匿名终选门由编排器开（按正文去重、没查成抄袭门的
不交给作者）。其余起草方式即使打开开关也只起一稿。

2026-09-30 [批准#2]：先中性后润色的那一套多稿整套删掉——温度展开、低分散时逐个补候选（温度加宽 / 发散提示 /
风格侧重轮换）、按系统自己的去 AI 味分挑稿与它的逐候选评估、分散度写回。它挑稿用的是系统自己的口味，和「以参考
作者为准」正好相反，也从来没有人用过。
"""

from __future__ import annotations

from typing import Any, Callable

import novel_system.services.scene_generation.fidelity_probe as fidelity_probe
from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services import reference_copy_gate
from novel_system.services.literary_quality import adversarial_rank_score
from novel_system.services.scene_generation.contracts import (
    LINEAGE_FIRST_DRAFT_ACCEPTED,
    GenerationHost,
    ProductCallback,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.ledger import DraftLedger
from novel_system.services.scene_generation.style_first import style_first_step
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.fidelity import within_author_range


def generate_candidates(
    svc: GenerationHost,
    scene_id: str,
    bundle: dict[str, Any],
    *,
    neutral_draft_row_id: str,
    neutral_content: str,
    author_note: str | None = None,
    n_candidates: int = 3,
    step_reconciler: Callable[[str], None] | None = None,
    resume_bases: dict[str, StyleGenerationResult] | None = None,
    resume_products: dict[str, StyleGenerationResult] | None = None,
    product_callback: ProductCallback | None = None,
) -> list[StyleGenerationResult]:
    """Best-of-N 风格候选。作者手笔直起：首稿 + (N−1) 个定向修改，按读数排序（:func:`style_first_candidates`）。
    其余起草方式即使打开了开关也只起一稿，与编排器的单稿路径同一个做法（同一个步位、同一份续跑基稿）。"""
    policy = style_policy_for_bundle(bundle)
    if not policy.style_first:
        bases = dict(resume_bases or {})
        if step_reconciler is not None and "initial:0" not in bases:
            step_reconciler("style_draft:0")
        return [
            svc.generate_style_draft(
                scene_id,
                bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                neutral_content=neutral_content,
                author_note=author_note,
                resume_base=bases.get("initial:0"),
                product_callback=product_callback,
                step_reconciler=step_reconciler,
            )
        ]
    return style_first_candidates(
        svc,
        scene=svc.session.get(SceneCard, scene_id),
        state=svc.session.get(SceneRunState, scene_id),
        bundle=bundle,
        policy=policy,
        first_row_id=neutral_draft_row_id,
        first_content=neutral_content,
        author_note=author_note,
        n_candidates=n_candidates,
        step_reconciler=step_reconciler,
        resume_bases=dict(resume_bases or {}),
        resume_products=dict(resume_products or {}),
        product_callback=product_callback,
    )


def style_first_candidates(
    svc: GenerationHost,
    *,
    scene: SceneCard,
    state: SceneRunState,
    bundle: dict[str, Any],
    policy: Any,
    first_row_id: str,
    first_content: str,
    author_note: str | None,
    n_candidates: int,
    step_reconciler: Callable[[str], None] | None,
    resume_bases: dict[str, StyleGenerationResult],
    resume_products: dict[str, StyleGenerationResult],
    product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
) -> list[StyleGenerationResult]:
    """Best-of-N（作者手笔直起）：候选 = 首稿 + (N−1) 个定向修改，按读数 distance 排序（最像的在前）。

    首稿永远是槽位 initial:0（不调模型）；读数不可信 / 读不出时只有首稿一个候选。修改槽位不做分散度补候选
    （它们是按测得的差异定向改的，不是独立采样）。关键场景的匿名终选门不变（按正文去重后给作者选）。
    """
    thresholds = style_step.fidelity_thresholds()
    first_reading, first_reading_id, first_reading_error = fidelity_probe.record_first_draft_reading(
        svc.session, scene, policy, first_row_id, first_content, thresholds
    )
    usable = first_reading is not None and first_reading.reliable
    try:
        base_temp = svc._llm_runner.task_config("style_draft").temperature
    except KeyError:
        base_temp = 0.7
    slot_count = max(1, int(n_candidates)) if usable else 1
    # L1：续跑时读数可能与第一次不同（书改过、这次读不出），槽位数不能因此缩回去——已经落下检查点的槽位
    # （产品或基稿）一个都不能丢，否则检查点里的工作项对不上，续跑报 RUN_CHECKPOINT_CORRUPT
    resumed_indices = [
        int(key.split(":", 1)[1])
        for key in (*resume_products, *resume_bases)
        if key.startswith("initial:") and key.split(":", 1)[1].isdigit()
    ]
    if resumed_indices:
        slot_count = max(slot_count, max(resumed_indices) + 1)
    results: list[tuple[StyleGenerationResult, int]] = []
    for idx in range(slot_count):
        slot_key = f"initial:{idx}"
        if slot_key in resume_products:
            results.append((resume_products[slot_key], idx))
            continue
        row_id = versioned_scene_artifact_id("draft_style_cand", scene.scene_id, bundle) + f"_{idx}"
        temperature = round(min(2.0, max(0.0, float(base_temp) + 0.05 * idx)), 3)
        result = style_first_step(
            svc,
            scene=scene,
            state=state,
            bundle=bundle,
            policy=policy,
            first_row_id=first_row_id,
            first_content=first_content,
            author_note=author_note,
            row_id=row_id,
            slot_key=slot_key,
            slot_order=idx,
            execution_step_key=f"style_draft:{idx}",
            resume_base=resume_bases.get(slot_key),
            product_callback=product_callback,
            step_reconciler=step_reconciler,
            first_reading=first_reading,
            first_reading_id=first_reading_id,
            first_reading_done=True,
            first_reading_error=first_reading_error,
            candidate_mode=idx > 0,
            force_accept=idx == 0,
            temperature_override=temperature if idx > 0 else None,
            attempt_details_extra={
                "source_neutral_draft_row_id": first_row_id,
                "candidate_index": idx,
                "n_candidates": n_candidates,
                **({"temperature_override": temperature} if idx > 0 else {}),
            },
        )
        results.append((result, idx))
    ranked = rank_style_first_candidates(
        svc,
        results, policy=policy, first_reading=first_reading, first_row_id=first_row_id
    )
    DraftLedger(svc.session, scene, state, bundle).point_style(ranked[0].row_id)
    return ranked


def rank_style_first_candidates(
    svc: GenerationHost,
    results: list[tuple[StyleGenerationResult, int]],
    *,
    policy: Any,
    first_reading: Any,
    first_row_id: str,
) -> list[StyleGenerationResult]:
    """先按抄袭门（与参考书原文连续相同的候选一律排最后——M2：否则一份被拦的首稿可以凭 distance 赢过干净的
    修改稿、成为风格稿），再按读数 distance 升序排（读不出的排在能读的后面，平手时槽位靠前的在前——首稿赢平手）；
    写每个候选的排序审计。"""
    # 抄袭门的结论：True = 查过没重合，False = 查出重合，None = 没查成（书已删 / 策略降级 / 检查出错）
    scored: list[tuple[StyleGenerationResult, int, Any, bool | None]] = []
    for result, idx in results:
        if (result.content or "") == "":
            reading = None
        elif result.lineage == LINEAGE_FIRST_DRAFT_ACCEPTED or idx == 0:
            reading = first_reading
        else:
            # L3：读不出排最后；读数在保存点里读，失败不弄坏会话
            reading, _error = fidelity_probe.observe(
                svc.session, policy, result.content, ref=result.row_id, what="candidate"
            )
        copy_passed: bool | None
        try:
            copy = reference_copy_gate.check_reference_copy(svc.session, result.content or "", policy=policy)
            # 有一边没有查成又没查出命中：不能当成「查过、没重合」（终选门不把没查成的候选交给作者）
            copy_passed = None if (copy.unavailable and not copy.hits) else not copy.blocked
        except Exception:  # noqa: BLE001 — 抄袭门查不成：候选按没查成处理（排最后，终选门会剔除）
            copy_passed = None
        scored.append((result, idx, reading, copy_passed))
    scored.sort(
        key=lambda item: (
            item[3] is not True,
            item[2] is None,
            float(item[2].distance) if item[2] is not None else 0.0,
            item[1],
        )
    )
    seen_texts: dict[str, str] = {}
    ranked: list[StyleGenerationResult] = []
    max_percentile = style_step.fidelity_thresholds().style_step_max_percentile
    for rank, (result, idx, reading, copy_passed) in enumerate(scored):
        normalized = (result.content or "").strip()
        duplicate_of = seen_texts.get(normalized)
        seen_texts.setdefault(normalized, result.row_id)
        result.ranking_audit = {
            "row_id": result.row_id,
            "rank": rank,
            "selected": rank == 0,
            "selection_reason": "fidelity_distance",
            "slot_index": idx,
            "quality_score": round(float(adversarial_rank_score(result.content or "")), 6),
            "style_score": None,
            "fidelity_distance": getattr(reading, "distance", None),
            "fidelity_percentile": getattr(reading, "percentile", None),
            "within_range": (
                within_author_range(reading, max_percentile=max_percentile) if reading is not None else None
            ),
            "duplicate_of_row_id": duplicate_of,
            "plagiarism_checked": copy_passed is not None,
            "plagiarism_passed": copy_passed,
            "rerank": {"applied_mode": "fidelity_distance", "reason": None},
        }
        ranked.append(result)
    return ranked

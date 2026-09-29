"""Best-of-N 候选（``NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED``，默认关）：两种起草方式各一套。

- 作者手笔直起：候选 = 首稿 + (N−1) 个定向修改，先按抄袭门、再按读数 distance 排序（最像的在前，首稿赢平手）。
- neutral_first：同一中性稿按温度展开 N 份风格稿，按对抗质量分排序；分散度低于 0.15 时在预算允许下逐个补候选
  （温度加宽 → 发散提示 → 风格侧重轮换），补到上限或分散达标即停。

每个候选都写排序审计（``ranking_audit``）；关键场景的匿名终选门由编排器开（按正文去重、没查成抄袭门的不交给作者）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import novel_system.services.scene_generation.fidelity_probe as fidelity_probe
from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services import reference_copy_gate, scene_budget
from novel_system.services.errors import DomainError
from novel_system.services.llm_task_runner import LLMNodeExecutionError
from novel_system.services.literary_quality import adversarial_rank_score
from novel_system.services.scene_generation.briefs import (
    _NEUTRAL_STYLE_INSTRUCTION,
    _author_note_instruction_for_bundle,
)
from novel_system.services.scene_generation.contracts import (
    LINEAGE_FIRST_DRAFT_ACCEPTED,
    NEUTRAL_DRAFT_SOURCE_LABEL,
    GenerationHost,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.ledger import DraftLedger
from novel_system.services.scene_generation.neutral_style import run_style_generation
from novel_system.services.scene_generation.style_first import style_first_step
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.fidelity import within_author_range


_LOGGER = logging.getLogger(__name__)


# §6.3 multi-strategy diversification prompts for low-dispersion retry
_DIVERSIFICATION_PROMPT = (
    "[DIVERSIFICATION] 前一轮生成的候选在表达上高度相似。请刻意尝试不同的叙述入口：\n"
    "换一种感官开场（如果之前用了视觉，试听觉或触觉）、\n"
    "换一种时间结构（如果之前是顺叙，试倒叙或插叙的片段）、\n"
    "换一种节奏（如果之前是长句铺陈，试短句切入）。\n"
    "保持场景spec的所有结构要求不变，只改变'怎么去'。\n\n"
)
# §6.3 style emphasis rotation prefixes — rotate which style dimension the LLM focuses on
_STYLE_EMPHASIS_ROTATION: list[str] = [
    (
        # 2026-09-22 风格参考优先:补候选时也不把「不做什么」抬成首要约束——先在心里复读三段样例的
        # 句法与口吻再动笔;禁忌只作校核。
        "[风格强调·样例优先] 本次生成请先在心里复读 [风格样例] 里的三段原文——它的句子怎么起、"
        "在哪儿停、旁白怎么插话、对白怎么接——再动笔,让每一段都像那位作者写的;禁忌模式只用来自检。\n\n"
    ),
    (
        "[风格强调·节奏分布优先] 本次生成关注风格参考中的整体节奏倾向——"
        "句群长短、段落功能与停顿习惯应自然呈现；不要为任何统计数字机械增删标点或拆段。\n\n"
    ),
]


def _progressive_top_up_variants(
    base_temp: float,
) -> list[tuple[float, str | None, str]]:
    """Wave 3（§5.5）渐进补候选的变体轮换：温度加宽 → 发散提示 → 风格侧重轮换。

    返回 (temperature, extra_system_prefix, strategy_label) 序列；补候选按序取用，
    每次只补 1 个。
    """
    variants: list[tuple[float, str | None, str]] = [
        (round(min(2.0, base_temp + 0.15), 3), None, "temperature_widen"),
        (
            round(min(2.0, base_temp + 0.10), 3),
            _DIVERSIFICATION_PROMPT,
            "prompt_variation",
        ),
    ]
    for idx, prefix in enumerate(_STYLE_EMPHASIS_ROTATION):
        variants.append(
            (
                round(min(2.0, base_temp + 0.05 * (idx + 1)), 3),
                prefix,
                f"style_emphasis_{idx}",
            )
        )
    return variants


def _candidate_dispersion(texts: list[str]) -> float:
    """Measure pairwise surface dissimilarity of candidate texts (0=identical, 1=fully disjoint).

    Blueprint §6.3: dispersion is a necessary condition for surprise — if candidates
    are highly similar, sampling hasn't explored the tail.
    Uses character-level 4-gram Jaccard distance averaged over all pairs.
    """
    if len(texts) < 2:
        return 1.0

    def _char_ngrams(text: str, n: int = 4) -> set[str]:
        return {text[i : i + n] for i in range(max(0, len(text) - n + 1))}

    ngram_sets = [_char_ngrams(t) for t in texts]
    distances: list[float] = []
    for i in range(len(ngram_sets)):
        for j in range(i + 1, len(ngram_sets)):
            a, b = ngram_sets[i], ngram_sets[j]
            union = len(a | b)
            if union == 0:
                distances.append(0.0)
            else:
                distances.append(1.0 - len(a & b) / union)
    return sum(distances) / len(distances) if distances else 0.0


def generate_candidates(
    svc: GenerationHost,
    scene_id: str,
    bundle: dict[str, Any],
    *,
    neutral_draft_row_id: str,
    neutral_content: str,
    author_note: str | None = None,
    n_candidates: int = 3,
    max_candidates: int | None = None,
    resume_candidates: list[StyleGenerationResult] | None = None,
    candidate_checkpoint: (
        Callable[[int, StyleGenerationResult], None] | None
    ) = None,
    step_reconciler: Callable[[str], None] | None = None,
    resume_bases: dict[str, StyleGenerationResult] | None = None,
    resume_products: dict[str, StyleGenerationResult] | None = None,
    product_callback: (
        Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
    ) = None,
) -> list[StyleGenerationResult]:
    """Generate N style-draft candidates with evidence-gated style reranking.

    Wave 3（治理 §5.5）：低分散补救为**渐进补候选**——初始 n_candidates，
    分散度 <0.15 时在预算允许下逐个补到 max_candidates（关键 3→5、标准
    2→3），不再一次生成后整批无上限重试。

    风格评分默认 shadow，仅落可审计诊断；只有冻结的人评证据授权后，才可在
    adversarial 质量差距受限的候选间改序。连续复刻参考原文的候选由独立硬
    guard 后置，不依赖未校准的风格分数。
    """
    scene = svc.session.get(SceneCard, scene_id)
    state = svc.session.get(SceneRunState, scene_id)
    policy = style_policy_for_bundle(bundle)
    if policy.style_first:
        # 风格参考 v3（P5b）：作者手笔直起时候选 = 首稿 + (N−1) 个定向修改，按读数 distance 排序
        #（取代旧的候选重排风格分）；关键场景的匿名终选门照旧。
        durable_products = dict(resume_products or {})
        if not durable_products:
            durable_products.update(
                (f"initial:{index}", candidate)
                for index, candidate in enumerate(resume_candidates or [])
            )
        return style_first_candidates(
            svc,
            scene=scene,
            state=state,
            bundle=bundle,
            policy=policy,
            first_row_id=neutral_draft_row_id,
            first_content=neutral_content,
            author_note=author_note,
            n_candidates=n_candidates,
            step_reconciler=step_reconciler,
            resume_bases=dict(resume_bases or {}),
            resume_products=durable_products,
            product_callback=product_callback,
        )

    try:
        task_config = svc._llm_runner.task_config("style_draft")
        base_temp = task_config.temperature
    except KeyError:
        base_temp = 0.7

    if n_candidates <= 1:
        temperatures = [base_temp]
    else:
        spread = 0.05
        temperatures = [
            round(base_temp + spread * (2 * i / (n_candidates - 1) - 1), 3)
            for i in range(n_candidates)
        ]
        temperatures = [max(0.0, min(2.0, t)) for t in temperatures]

    durable_products = dict(resume_products or {})
    durable_bases = dict(resume_bases or {})
    if not durable_products:
        durable_products.update(
            (f"initial:{index}", candidate)
            for index, candidate in enumerate(resume_candidates or [])
        )
    candidates: list[tuple[StyleGenerationResult, float]] = [
        (
            candidate,
            adversarial_rank_score(candidate.content),
        )
        for candidate in durable_products.values()
    ]
    for idx, temp in enumerate(temperatures):
        slot_key = f"initial:{idx}"
        if slot_key in durable_products:
            continue
        cand_row_id = (
            versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
            + f"_{idx}"
        )
        try:
            if step_reconciler is not None and slot_key not in durable_bases:
                step_reconciler(f"style_draft:{idx}")
            result = run_style_generation(
                svc,
                scene=scene,
                state=state,
                bundle=bundle,
                row_id=cand_row_id,
                stage="style_draft",
                llm_step="style_draft",
                neutral_content=neutral_content,
                source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
                source_row_id=neutral_draft_row_id,
                extra_instruction=(
                    _NEUTRAL_STYLE_INSTRUCTION
                    + _author_note_instruction_for_bundle(bundle, author_note)
                ),
                source_draft_row_id=neutral_draft_row_id,
                source_draft_content=neutral_content,
                client_kind="style",
                temperature_override=temp,
                execution_step_key=f"style_draft:{idx}",
                attempt_details_extra={
                    "source_neutral_draft_row_id": neutral_draft_row_id,
                    "candidate_index": idx,
                    "temperature_override": temp,
                    "n_candidates": n_candidates,
                },
                product_slot_key=slot_key,
                product_slot_order=idx,
                resume_base=durable_bases.get(slot_key),
                product_callback=product_callback,
                step_reconciler=step_reconciler,
            )
            score = adversarial_rank_score(result.content)
            candidates.append((result, score))
            if candidate_checkpoint is not None:
                candidate_checkpoint(idx, result)
        except (DomainError, LLMNodeExecutionError):
            _LOGGER.warning(
                "candidate %d/%d failed for scene %s",
                idx + 1,
                n_candidates,
                scene_id,
            )
            if candidate_checkpoint is not None or product_callback is not None:
                raise
            continue

    if not candidates:
        return [
            svc.generate_style_draft(
                scene_id,
                bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                neutral_content=neutral_content,
                author_note=author_note,
                product_callback=product_callback,
                step_reconciler=step_reconciler,
            )
        ]

    candidates.sort(key=lambda pair: pair[1], reverse=True)

    # Wave 3（§5.5）：渐进补候选——每次只补 1 个（温度加宽 / 发散提示 /
    # 风格侧重轮换作为逐个变体来源），每步过预算闸，补到上限或分散达标即停。
    candidate_cap = max(n_candidates, max_candidates or n_candidates)
    if len(candidates) >= 2 and candidate_cap > len(candidates):
        variants = _progressive_top_up_variants(base_temp)
        known_top_up_indices = {
            int(slot_key.rsplit(":", 1)[-1])
            for slot_key in {*durable_products, *durable_bases}
            if slot_key.startswith("topup:")
            and slot_key.rsplit(":", 1)[-1].isdigit()
        }
        pending_top_up_indices = sorted(
            index
            for index in known_top_up_indices
            if f"topup:{index}" in durable_bases
            and f"topup:{index}" not in durable_products
        )
        top_up_index = max(known_top_up_indices, default=0)
        while len(candidates) < candidate_cap:
            dispersion = _candidate_dispersion([c.content for c, _ in candidates])
            pending_top_up_index = (
                pending_top_up_indices.pop(0) if pending_top_up_indices else None
            )
            if pending_top_up_index is None and dispersion >= 0.15:
                break
            if pending_top_up_index is None and not scene_budget.can_spend(
                state, scene_budget.budget_unit(state)
            ):
                _LOGGER.warning(
                    "budget exhausted — stop progressive candidate top-up for scene %s "
                    "(dispersion=%.3f, %d candidates)",
                    scene_id,
                    dispersion,
                    len(candidates),
                )
                break
            if pending_top_up_index is None:
                top_up_index += 1
            else:
                top_up_index = pending_top_up_index
            temp, prefix, strategy = variants[(top_up_index - 1) % len(variants)]
            _LOGGER.warning(
                "low candidate dispersion (%.3f) for scene %s — progressive top-up #%d via %s (§5.5)",
                dispersion,
                scene_id,
                top_up_index,
                strategy,
            )
            top_up_row_id = (
                versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
                + f"_topup_{top_up_index}"
            )
            slot_key = f"topup:{top_up_index}"
            try:
                if step_reconciler is not None and slot_key not in durable_bases:
                    step_reconciler(f"style_draft:topup:{top_up_index}")
                result = run_style_generation(
                    svc,
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    row_id=top_up_row_id,
                    stage="style_draft",
                    llm_step="style_draft",
                    neutral_content=neutral_content,
                    source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
                    source_row_id=neutral_draft_row_id,
                    extra_instruction=(
                        _NEUTRAL_STYLE_INSTRUCTION
                        + _author_note_instruction_for_bundle(bundle, author_note)
                    ),
                    source_draft_row_id=neutral_draft_row_id,
                    source_draft_content=neutral_content,
                    client_kind="style",
                    temperature_override=temp,
                    execution_step_key=f"style_draft:topup:{top_up_index}",
                    extra_system_prefix=prefix,
                    attempt_details_extra={
                        "source_neutral_draft_row_id": neutral_draft_row_id,
                        "candidate_index": f"topup_{top_up_index}",
                        "temperature_override": temp,
                        "n_candidates": n_candidates,
                        "max_candidates": candidate_cap,
                        "diversification_strategy": strategy,
                        "progressive_top_up": True,
                    },
                    product_slot_key=slot_key,
                    product_slot_order=n_candidates + top_up_index - 1,
                    resume_base=durable_bases.get(slot_key),
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
                candidates.append(
                    (
                        result,
                        adversarial_rank_score(result.content),
                    )
                )
                if candidate_checkpoint is not None:
                    candidate_checkpoint(len(candidates) - 1, result)
            except (DomainError, LLMNodeExecutionError):
                # 失败即停：不无上限重试（Wave 3 项 5）
                _LOGGER.warning(
                    "progressive top-up #%d failed for scene %s — stop",
                    top_up_index,
                    scene_id,
                )
                if candidate_checkpoint is not None or product_callback is not None:
                    raise
                break
        candidates.sort(key=lambda pair: pair[1], reverse=True)

    # 2026-09-14 减法:候选重排层(shadow / active、基准授权)已删除——候选保持质量序;每个候选仍读一次
    # 「像不像」读数(style_score)并过唯一抄袭门(盲选门据此剔除抄袭 / 没检查成的候选,工作台据此展示)。
    for rank, (result, score) in enumerate(candidates):
        result.ranking_audit = candidate_style_assessment(
            svc,
            bundle, result, float(score), rank=rank
        )

    best_result = candidates[0][0]
    # §6 Defect D: persist dispersion score for author-facing quality signal
    if len(candidates) >= 2:
        final_dispersion = _candidate_dispersion([c.content for c, _ in candidates])
        state.candidate_dispersion_score = round(final_dispersion, 4)
    DraftLedger(svc.session, scene, state, bundle).point_style(best_result.row_id)

    return [result for result, _ in candidates]


def candidate_style_assessment(
    svc: GenerationHost,
    bundle: dict[str, Any],
    result: "StyleGenerationResult",
    quality_score: float,
    *,
    rank: int,
) -> dict[str, Any]:
    """单个候选的审计：``style_score`` 由读数给（``1 − percentile/100``，四位小数；读不出 / 不可信 → None），
    原文重合走唯一抄袭门；候选保持质量序（风格参考 v3 S2：旧的 21 指标包络已删）。

    读数或抄袭门抛异常 → ``plagiarism_checked=False`` / ``plagiarism_passed=None``：有绑定时终选门不把
    「没检查成」的候选交给作者盲选（fail-closed；成稿门仍是最后一道），候选本身照常交付。"""
    rerank: dict[str, Any] = {"applied_mode": "off", "reason": None}
    audit: dict[str, Any] = {
        "row_id": result.row_id,
        "quality_score": round(float(quality_score), 6),
        "style_score": None,
        "fidelity_distance": None,
        "fidelity_percentile": None,
        "rank": rank,
        "selected": rank == 0,
        "selection_reason": "quality_order",
        "plagiarism_checked": False,
        "plagiarism_passed": None,
        "plagiarism_hit_count": 0,
        "plagiarism_max_match_chars": 0,
    }
    content = result.content or ""
    try:
        policy = style_policy_for_bundle(bundle)
        rerank["runtime_contract_mode"] = policy.mode
        if not policy.bound:
            rerank["reason"] = (
                "bundle_has_no_style_profile"
                if policy.mode == "absent"
                else (policy.error_code or "frozen_runtime_contract_unavailable")
            )
            return {**audit, "rerank": rerank}
        if not content.strip():
            rerank["reason"] = "empty_candidate"
            return {**audit, "rerank": rerank}
        # 读数在保存点里读（第一次读一本书要建窗口索引、写库），失败只回滚保存点、不弄坏会话
        with svc.session.begin_nested():
            reading = style_readings.reading_for_text(svc.session, policy, content)
        # 风格参考 v3：候选的原文重合走唯一抄袭门（按书一次索引、同一稿不重复扫描）
        copy = reference_copy_gate.check_reference_copy(svc.session, content, policy=policy)
    except Exception as exc:  # noqa: BLE001 — 读数 / 抄袭门是可选增强,不阻断候选交付;但「没检查成」要如实记
        _LOGGER.warning(
            "style candidate assessment degraded for scene %s", result.row_id, exc_info=True
        )
        return {
            **audit,
            "rerank": {
                **rerank,
                "reason": "assessment_internal_error",
                "error_code": getattr(exc, "code", exc.__class__.__name__),
            },
        }
    if reading is None:
        rerank["reason"] = "reading_unavailable"
    else:
        audit["fidelity_distance"] = reading.distance
        audit["fidelity_percentile"] = reading.percentile
        if reading.reliable:
            audit["style_score"] = round(1.0 - float(reading.percentile) / 100.0, 4)
        else:
            rerank["reason"] = "reading_unreliable"
    if copy.unavailable and not copy.hits:
        # 有一边没有查成（书已删 / 策略降级）：不能当成「查过、没重合」
        rerank["copy_gate"] = "unavailable"
        return {**audit, "rerank": rerank}
    audit.update(
        {
            "plagiarism_checked": True,
            "plagiarism_passed": not copy.hits,
            "plagiarism_hit_count": len(copy.hits),
            "plagiarism_max_match_chars": max((hit.matched_chars for hit in copy.hits), default=0),
        }
    )
    return {**audit, "rerank": rerank}


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
    best = ranked[0]
    if len(ranked) >= 2:
        state.candidate_dispersion_score = round(_candidate_dispersion([c.content for c in ranked]), 4)
    DraftLedger(svc.session, scene, state, bundle).point_style(best.row_id)
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

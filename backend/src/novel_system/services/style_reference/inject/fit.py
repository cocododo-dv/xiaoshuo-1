"""风格参考 v3 — 把渲染结果贪心地压进真实输入预算（J14）。

旧实现在「正向 × 禁忌 × 声音」三层组合上暴力搜索（小上下文配置下一次 6.3 秒）。现在一条固定次序、每步只去一整
个单元，够了就停：

1. 样例窗整窗去掉，从选窗顺序的末尾（典型度补位的窗）开始，至少留一窗；
2. 文风卡按辨识度从低到高逐句去掉（必须 / 钉住的句最后去，近期偏差次之，气质最后）；
3. 去掉声音块；
4. 一窗 + 空卡仍装不下：换一条路——不要样例窗，整张卡与声音放回来，再按 2、3 的次序去；
5. 仍装不下 → 整份风格参考都不发（审计 ``style_payload_omitted``）。

红线从不截断：只要还有任一块参考，红线原样随注。估算用各块 token 之和（``estimate_tokens`` 可加，拼接处
取整的误差用余量兜住），停下后再对拼好的全文精确估算一次，万一仍超就继续按同一次序去。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from novel_system.services.context_budget import estimate_tokens
from novel_system.services.style_reference.inject.audit import block_digest
from novel_system.services.style_reference.inject.render import (
    RenderedStyle,
    render_stats,
    rendered_window_refs,
)

FIT_VERSION = "greedy_fit_v3"
_SEPARATOR_SLACK = 2


def _exact(prefix: str, tail: str, base_system_prompt: str, user_prompt: str) -> int:
    return estimate_tokens(prefix + base_system_prompt) + estimate_tokens(str(user_prompt).rstrip() + tail)


def fit_rendered(
    rendered: RenderedStyle,
    *,
    base_system_prompt: str,
    user_prompt: str,
    target_input_tokens: int,
) -> tuple[RenderedStyle, dict[str, Any]]:
    """返回 (压过之后的渲染, 预算审计)。审计只有规模与策略，没有正文。"""
    target = max(0, int(target_input_tokens or 0))
    full_prefix = rendered.system_prefix
    full_tail = rendered.user_tail
    base_tokens = estimate_tokens(base_system_prompt) + estimate_tokens(user_prompt)
    full_tokens = _exact(full_prefix, full_tail, base_system_prompt, user_prompt)

    def _audit(final_prefix: str, final_tail: str, *, policy: str, steps: dict[str, int]) -> dict[str, Any]:
        omitted = bool((full_prefix or full_tail) and not (final_prefix or final_tail))
        return {
            "version": FIT_VERSION,
            "compacted": (final_prefix, final_tail) != (full_prefix, full_tail),
            "policy": policy,
            "target_input_tokens": target,
            "base_estimated_input_tokens": base_tokens,
            "full_estimated_input_tokens": full_tokens,
            "final_estimated_input_tokens": _exact(final_prefix, final_tail, base_system_prompt, user_prompt),
            "prefix_chars_before": len(full_prefix) + len(full_tail),
            "prefix_chars_after": len(final_prefix) + len(final_tail),
            "dropped_windows": int(steps.get("windows", 0)),
            "dropped_card_units": int(steps.get("card_units", 0)),
            "voice_dropped": bool(steps.get("voice", 0)),
            "card_dropped": bool(steps.get("card", 0)),
            "style_payload_omitted": omitted,
            # 红线从不截断：要么原样随注，要么整份参考都不发
            "anti_plagiarism_preserved": True,
        }

    parts = rendered.parts
    if parts is None or rendered.empty or target <= 0 or full_tokens <= target:
        return rendered, _audit(full_prefix, full_tail, policy="no_compaction_needed", steps={})

    # ---- 各块的 token 估算（可加近似）----
    window_cost = {w.priority: estimate_tokens(w.line) + 1 for w in parts.windows}
    fixed = (
        estimate_tokens(parts.samples_header)
        + estimate_tokens(parts.red_line)
        + estimate_tokens(parts.closing)
        + 24  # [STYLE_REFERENCE] 包装、指路句的差额、各块之间的换行
    )
    voice_cost = estimate_tokens(parts.voice)
    card_cache: dict[frozenset[str], int] = {}

    def _card_cost(excluded: frozenset[str], include_card: bool) -> int:
        if parts.card is None or not include_card:
            return 0
        if excluded not in card_cache:
            card_cache[excluded] = estimate_tokens(parts.card.render(excluded))
        return card_cache[excluded]

    keep = sorted(window_cost)  # 按选窗顺序的优先级（小的优先）
    excluded: frozenset[str] = frozenset()
    include_voice = bool(parts.voice)
    include_card = parts.card is not None
    steps = {"windows": 0, "card_units": 0, "voice": 0, "card": 0}
    card_order = list(parts.card.drop_order) if parts.card is not None else []
    slack = _SEPARATOR_SLACK * (len(keep) + 4)

    def _approx() -> int:
        return (
            base_tokens
            + fixed
            + sum(window_cost[p] for p in keep)
            + _card_cost(excluded, include_card)
            + (voice_cost if include_voice else 0)
            + slack
        )

    all_windows = list(keep)

    def _shed_abstract(policy_suffix: str):
        nonlocal excluded, include_voice, include_card
        for unit in card_order:
            if unit in excluded:
                continue
            excluded = excluded | {unit}
            steps["card_units"] += 1
            yield f"shed_card_lines_{policy_suffix}"
        if include_voice:
            include_voice = False
            steps["voice"] = 1
            yield f"drop_voice_{policy_suffix}"
        if include_card:
            include_card = False
            steps["card"] = 1
            yield f"drop_card_{policy_suffix}"

    def _steps():
        nonlocal keep, excluded, include_voice, include_card
        # 路一：留一窗，去卡句、声音
        while len(keep) > 1:
            keep = keep[:-1]
            steps["windows"] += 1
            yield "shed_sample_windows_v3"
        yield from _shed_abstract("keep_one_window_v3")
        if not all_windows:
            return
        # 路二：不要样例窗，卡与声音放回来重新去
        keep = []
        steps["windows"] = len(all_windows)
        excluded = frozenset()
        include_voice = bool(parts.voice)
        include_card = parts.card is not None
        steps["card_units"] = 0
        steps["voice"] = 0
        steps["card"] = 0
        yield "drop_sample_windows_v3"
        yield from _shed_abstract("no_windows_v3")

    policy = "no_compaction_needed"
    generator = _steps()
    exhausted = False
    for policy in generator:
        if _approx() <= target:
            break
    else:
        exhausted = True

    def _assemble() -> tuple[str, str, dict[str, Any]]:
        return parts.assemble(
            keep=frozenset(keep),
            excluded=excluded,
            include_voice=include_voice,
            include_card=include_card,
        )

    prefix, tail, blocks = _assemble()
    if not exhausted:
        # 近似够了：精确复核；万一仍超，按同一次序继续
        while _exact(prefix, tail, base_system_prompt, user_prompt) > target:
            try:
                policy = next(generator)
            except StopIteration:
                exhausted = True
                break
            prefix, tail, blocks = _assemble()
    if exhausted and _exact(prefix, tail, base_system_prompt, user_prompt) > target:
        prefix, tail, blocks = "", "", {"samples": "", "card": "", "voice": "", "red_line": "", "windows": []}
        policy = "omit_style_payload_v3"
    stats = render_stats(system_prefix=prefix, user_tail=tail, blocks=blocks, k=int(rendered.stats.get("few_shot_k", 0)))
    refs = rendered_window_refs(blocks["windows"])
    audit_payload = _audit(prefix, tail, policy=policy, steps=steps)
    fitted_audit = dict(rendered.audit)
    fitted_audit.update(
        {
            "render_stats": stats,
            "few_shot_window_refs": [dict(item) for item in refs],
            "blocks": {name: block_digest(str(blocks.get(name) or "")) for name in ("samples", "card", "voice", "red_line")},
            "budget_fit": audit_payload,
        }
    )
    fitted = replace(
        rendered,
        system_prefix=prefix,
        user_tail=tail,
        window_refs=refs,
        stats=stats,
        audit=fitted_audit,
    )
    return fitted, audit_payload


__all__ = ["FIT_VERSION", "fit_rendered"]

"""抽取样本的采样策略(纯函数,无 DB)。

依据《风格参考模块重构执行手册 v1.1》§A.3 与风格模仿 v2 执行方案 §2.W2:

- `derive_extraction_rng`:以 ``sha256(text_checksum + run_id)`` 定种,
  同一 run 的采样可复现(resume 时重放采样即可回到同一 RNG 位置);
- `scale_sample_target`:样本量随全书字数分档(<50k 用基准值,≥50k ×1.5,
  ≥200k ×2,上限 60 段 / 子维);
- `proportional_stratified_sample`(language / scene 层):按段型**真实分布**
  比例分配名额,每种出现过的段型下限 1;段型内按 char_count 加权无放回抽样;
  返回按 paragraph_index 排序,让抽取 LLM 看到原文顺序;
- `sample_windows`(narrative / theme 层):抽 **连续窗口**(默认 3–6 个相邻段),
  窗口在全书位置上分层(首 / 中 / 尾都有覆盖),窗口之间至少留 1 段空隙,
  因此 `group_consecutive_windows` 能从扁平列表无损还原窗口边界;
- `stratified_sample`:v1.1 的「每型至少 min_per_type 再补齐」策略,保留给
  仍在使用它的调用方与测试。

对 StyleReferenceParagraph 类型友好,但接受任何含 `paragraph_type` / `char_count`
(/ `paragraph_index`)属性的对象(供测试用 dataclass mock)。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from typing import Any, Sequence

# 样本量分档:total_chars ≥ min_chars 时基准值 × multiplier(取最大命中档)
DEFAULT_SAMPLE_SCALING: tuple[tuple[int, float], ...] = ((200_000, 2.0), (50_000, 1.5))
DEFAULT_MAX_SAMPLES_PER_SUB_DIMENSION = 60
DEFAULT_WINDOW_SIZE_RANGE: tuple[int, int] = (3, 6)


# ---------------------------------------------------------------------------
# RNG
# ---------------------------------------------------------------------------


def derive_extraction_rng(text_checksum: str | None, run_id: str | None) -> random.Random:
    """``random.Random(int(sha256(text_checksum + run_id)[:16], 16))``。

    RunOrchestrator / BaseExtractor 未显式注入 rng 时的默认种子:同一本书的
    同一个 run 无论在哪个进程、是否 resume,采样序列都一致。
    """
    material = f"{text_checksum or ''}{run_id or ''}".encode("utf-8")
    seed = int(hashlib.sha256(material).hexdigest()[:16], 16)
    return random.Random(seed)


# ---------------------------------------------------------------------------
# 样本量分档
# ---------------------------------------------------------------------------


def scale_sample_target(
    base_n: int,
    total_chars: int | None,
    *,
    scaling: Sequence[tuple[int, float]] | Sequence[dict[str, Any]] = DEFAULT_SAMPLE_SCALING,
    max_n: int = DEFAULT_MAX_SAMPLES_PER_SUB_DIMENSION,
) -> int:
    """按全书字数放大基准样本量;`scaling` 接受 ``(min_chars, multiplier)`` 或
    ``{"min_chars", "multiplier"}``(yaml 形态),取字数命中的最大档。"""
    base_n = int(base_n or 0)
    if base_n <= 0:
        return 0
    chars = int(total_chars or 0)
    multiplier = 1.0
    best_floor = -1
    for tier in scaling or ():
        if isinstance(tier, dict):
            min_chars = int(tier.get("min_chars", 0) or 0)
            mult = float(tier.get("multiplier", 1.0) or 1.0)
        else:
            min_chars, mult = int(tier[0]), float(tier[1])
        if chars >= min_chars and min_chars > best_floor:
            best_floor = min_chars
            multiplier = mult
    target = int(round(base_n * multiplier))
    if max_n and max_n > 0:
        target = min(target, int(max_n))
    return max(1, target)


# ---------------------------------------------------------------------------
# 排序 / 分组辅助
# ---------------------------------------------------------------------------


def _order_key(p: Any) -> tuple[int, str]:
    index = getattr(p, "paragraph_index", None)
    return (int(index) if index is not None else 0, str(getattr(p, "paragraph_id", "")))


def _paragraph_index(p: Any, fallback: int) -> int:
    index = getattr(p, "paragraph_index", None)
    return int(index) if index is not None else fallback


def group_consecutive_windows(paragraphs: Sequence[Any]) -> list[list[Any]]:
    """把(已按 paragraph_index 排序的)扁平样本按索引连续性切成窗口。

    `sample_windows` 保证窗口间至少留 1 段空隙,所以这里还原出的窗口与采样
    时的窗口一一对应;缺 `paragraph_index` 属性的对象按列表位置视为连续。
    """
    windows: list[list[Any]] = []
    previous_index: int | None = None
    for position, p in enumerate(paragraphs):
        index = _paragraph_index(p, position)
        if previous_index is not None and index == previous_index + 1 and windows:
            windows[-1].append(p)
        else:
            windows.append([p])
        previous_index = index
    return windows


def window_position(position: int, size: int) -> str:
    """窗口内位置标签:first / middle / last(单段窗口为 first)。"""
    if position <= 0:
        return "first"
    if position >= size - 1:
        return "last"
    return "middle"


# ---------------------------------------------------------------------------
# language / scene:按真实段型分布比例分配名额
# ---------------------------------------------------------------------------


def proportional_stratified_sample(
    paragraphs: Sequence[Any],
    *,
    target_n: int,
    min_per_type: int = 1,
    rng: random.Random | None = None,
) -> list[Any]:
    """按 paragraph_type 真实分布比例分配名额(每型下限 `min_per_type`,受该型
    段数约束),型内按 char_count 加权无放回抽样;返回按 paragraph_index 排序。

    - 总段数 ≤ target_n 时全收;
    - 名额用最大余数法凑齐 target_n;段型过多、target_n 太小时从最稀少的段型
      开始让出下限名额。
    """
    if target_n <= 0 or not paragraphs:
        return []
    rng = rng or random.Random()
    ordered = sorted(paragraphs, key=_order_key)
    if len(ordered) <= target_n:
        return ordered

    buckets: dict[str, list[Any]] = defaultdict(list)
    for p in ordered:
        buckets[str(getattr(p, "paragraph_type", "narration") or "narration")].append(p)
    types = list(buckets)
    total = len(ordered)

    shares = {t: target_n * len(buckets[t]) / total for t in types}
    quotas = {
        t: min(len(buckets[t]), max(min(max(min_per_type, 0), len(buckets[t])), int(shares[t])))
        for t in types
    }
    diff = target_n - sum(quotas.values())
    if diff > 0:
        # 最大余数法补齐;余数相同时段数多者优先,再按名字稳定排序
        order = sorted(
            types,
            key=lambda t: (-(shares[t] - int(shares[t])), -len(buckets[t]), t),
        )
        while diff > 0:
            progressed = False
            for t in order:
                if diff <= 0:
                    break
                if quotas[t] < len(buckets[t]):
                    quotas[t] += 1
                    diff -= 1
                    progressed = True
            if not progressed:
                break
    elif diff < 0:
        # 下限之和超过 target_n:从最稀少的段型开始让出
        for t in sorted(types, key=lambda t: (len(buckets[t]), t)):
            if diff >= 0:
                break
            take = min(quotas[t], -diff)
            quotas[t] -= take
            diff += take

    selected: list[Any] = []
    for t in types:
        if quotas[t] > 0:
            selected.extend(_weighted_sample(buckets[t], quotas[t], rng))
    return sorted(selected, key=_order_key)


# ---------------------------------------------------------------------------
# narrative / theme:连续窗口
# ---------------------------------------------------------------------------


def sample_windows(
    paragraphs: Sequence[Any],
    target_n: int,
    window_size_range: tuple[int, int] = DEFAULT_WINDOW_SIZE_RANGE,
    rng: random.Random | None = None,
    *,
    gap: int = 1,
) -> list[list[Any]]:
    """抽若干个**相邻段**组成的连续窗口,窗口段数之和 ≈ `target_n`。

    - 窗口大小在 `window_size_range` 内随机;窗口起点在全书位置上分层
      (把段落序列切成 k 个区段,轮流在每个区段里抽),保证首 / 中 / 尾都有覆盖;
    - 窗口内段落 paragraph_index 必须连续(被禁用词过滤掉的段不会被跨越);
    - 窗口之间至少留 `gap` 段空隙,便于 `group_consecutive_windows` 还原边界;
    - 剩余名额不足一个最小窗口时停止(不产生 1–2 段的碎窗口),但 `target_n`
      本身小于最小窗口时仍产出一个 `target_n` 段的窗口;
    - 全书段数 ≤ target_n 时整本按顺序作为一个窗口返回。

    返回 list[窗口],每个窗口是按 paragraph_index 排序的段列表;窗口按起点排序。
    """
    if target_n <= 0 or not paragraphs:
        return []
    rng = rng or random.Random()
    ordered = sorted(paragraphs, key=_order_key)
    n = len(ordered)
    lo, hi = window_size_range
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    if n <= target_n:
        return [ordered]

    indices = [_paragraph_index(p, pos) for pos, p in enumerate(ordered)]
    used = [False] * n
    windows: list[tuple[int, list[Any]]] = []
    total = 0
    regions = max(1, math.ceil(target_n / ((lo + hi) / 2)))
    attempts = 0
    region = 0
    while total < target_n and attempts < regions * 4:
        attempts += 1
        remaining = target_n - total
        size = rng.randint(lo, hi)
        if remaining < lo:
            if windows:
                break
            size = remaining
        size = min(size, remaining)
        r = region % regions
        region += 1
        region_lo = (r * n) // regions
        region_hi = ((r + 1) * n) // regions - 1
        candidates = [
            s
            for s in range(region_lo, region_hi + 1)
            if _window_fits(indices, used, s, size, gap)
        ]
        if not candidates:
            candidates = [
                s for s in range(0, n - size + 1) if _window_fits(indices, used, s, size, gap)
            ]
        if not candidates:
            break
        start = rng.choice(candidates)
        for j in range(start, start + size):
            used[j] = True
        windows.append((start, ordered[start : start + size]))
        total += size
    windows.sort(key=lambda item: item[0])
    return [window for _start, window in windows]


def _window_fits(indices: list[int], used: list[bool], start: int, size: int, gap: int) -> bool:
    n = len(indices)
    if start < 0 or size <= 0 or start + size > n:
        return False
    first = indices[start]
    for j in range(size):
        if used[start + j] or indices[start + j] != first + j:
            return False
    for j in range(max(0, start - gap), min(n, start + size + gap)):
        if used[j]:
            return False
    return True


# ---------------------------------------------------------------------------
# v1.1 分层抽样(保留)
# ---------------------------------------------------------------------------


def stratified_sample(
    paragraphs: Sequence[Any],
    *,
    target_n: int,
    min_per_type: int = 3,
    rng: random.Random | None = None,
) -> list[Any]:
    """v1.1 分层抽样:每 paragraph_type 至少 `min_per_type` 段(不足则全收),
    总数不足 `target_n` 时从剩余段按 char_count 加权随机补,超出时按 type 多样性截断。"""
    if target_n <= 0 or not paragraphs:
        return []

    rng = rng or random.Random()

    # 按 type 分桶
    buckets: dict[str, list[Any]] = defaultdict(list)
    for p in paragraphs:
        buckets[getattr(p, "paragraph_type", "narration")].append(p)

    # 每 type 先按 char_count 加权随机抽至少 min_per_type 段(or 全量)
    selected_by_type: dict[str, list[Any]] = {}
    for ptype, items in buckets.items():
        if len(items) <= min_per_type:
            selected_by_type[ptype] = list(items)
        else:
            selected_by_type[ptype] = _weighted_sample(items, min_per_type, rng)

    selected: list[Any] = [p for items in selected_by_type.values() for p in items]

    # 若总数不足 target_n,从剩余段补
    if len(selected) < target_n:
        selected_ids = {id(p) for p in selected}
        remaining = [p for p in paragraphs if id(p) not in selected_ids]
        need = target_n - len(selected)
        if remaining:
            extra = _weighted_sample(remaining, min(need, len(remaining)), rng)
            selected.extend(extra)

    # 若超 target_n,按 type 多样性截断(round-robin 各 type 选样)
    if len(selected) > target_n:
        selected = _truncate_by_type_diversity(selected, target_n)

    return selected


def _weighted_sample(items: list[Any], k: int, rng: random.Random) -> list[Any]:
    """从 items 中按 char_count 加权无放回抽 k 个。"""
    if k >= len(items):
        return list(items)
    weights = [max(1, int(getattr(p, "char_count", 1) or 1)) for p in items]
    indices = list(range(len(items)))
    chosen: list[int] = []
    pool_weights = weights.copy()
    for _ in range(k):
        if not indices:
            break
        idx = rng.choices(range(len(indices)), weights=pool_weights, k=1)[0]
        chosen.append(indices.pop(idx))
        pool_weights.pop(idx)
    return [items[i] for i in chosen]


def _truncate_by_type_diversity(selected: list[Any], target_n: int) -> list[Any]:
    """按 type round-robin 顺序保留前 target_n 个,保留 type 多样性。"""
    buckets: dict[str, list[Any]] = defaultdict(list)
    for p in selected:
        buckets[getattr(p, "paragraph_type", "narration")].append(p)
    # round-robin
    types = list(buckets.keys())
    result: list[Any] = []
    while len(result) < target_n:
        progressed = False
        for ptype in types:
            if not buckets[ptype]:
                continue
            result.append(buckets[ptype].pop(0))
            progressed = True
            if len(result) >= target_n:
                break
        if not progressed:
            break
    return result

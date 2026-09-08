"""sampling.py 单测:分层抽样 / min_per_type / target_n 不足时补 / 截断。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略(PR-3)"。
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass

import pytest

from novel_system.services.style_reference.sampling import stratified_sample


@dataclass
class FakeParagraph:
    paragraph_id: str
    paragraph_type: str
    char_count: int


def _make_paragraphs(spec: dict[str, int], char_count: int = 200) -> list[FakeParagraph]:
    paras: list[FakeParagraph] = []
    idx = 0
    for ptype, count in spec.items():
        for _ in range(count):
            paras.append(FakeParagraph(f"p_{idx}", ptype, char_count))
            idx += 1
    return paras


def test_empty_paragraphs_returns_empty() -> None:
    assert stratified_sample([], target_n=10) == []


def test_target_zero_returns_empty() -> None:
    paras = _make_paragraphs({"narration": 5})
    assert stratified_sample(paras, target_n=0) == []


def test_min_per_type_three_each() -> None:
    paras = _make_paragraphs({"dialogue": 10, "narration": 10, "psychology": 10})
    rng = random.Random(42)
    result = stratified_sample(paras, target_n=15, min_per_type=3, rng=rng)
    counter = Counter(p.paragraph_type for p in result)
    # 每 type 至少 3 段
    for ptype in ("dialogue", "narration", "psychology"):
        assert counter[ptype] >= 3, f"{ptype} 仅 {counter[ptype]} 段,不足 min_per_type=3"
    assert len(result) == 15


def test_type_under_min_takes_all() -> None:
    """某 type 全量段数 < min_per_type 时,该 type 应全收。"""
    paras = _make_paragraphs({"dialogue": 2, "narration": 10})
    rng = random.Random(42)
    result = stratified_sample(paras, target_n=10, min_per_type=3, rng=rng)
    counter = Counter(p.paragraph_type for p in result)
    assert counter["dialogue"] == 2  # 全量
    assert counter["narration"] >= 3


def test_total_below_target_fills_to_target() -> None:
    """每 type 抽 min_per_type 后总数 < target_n,应从剩余继续补。"""
    paras = _make_paragraphs({"dialogue": 10, "narration": 10})  # 总 20 段
    rng = random.Random(42)
    result = stratified_sample(paras, target_n=15, min_per_type=3, rng=rng)
    assert len(result) == 15


def test_total_above_target_truncates_keeps_diversity() -> None:
    """初步选了 >target_n 段时按 type 多样性截断。"""
    paras = _make_paragraphs({"a": 5, "b": 5, "c": 5})  # 三 type 各 3 段 = 9
    rng = random.Random(42)
    result = stratified_sample(paras, target_n=5, min_per_type=3, rng=rng)
    counter = Counter(p.paragraph_type for p in result)
    assert len(result) == 5
    # 至少 2 个 type 出现(round-robin 多样性)
    assert len(counter) >= 2


def test_rng_determinism() -> None:
    """同 seed 的 rng 应产出同结果(可重复测试)。"""
    paras = _make_paragraphs({"dialogue": 8, "narration": 8})
    first = stratified_sample(paras, target_n=10, rng=random.Random(123))
    second = stratified_sample(paras, target_n=10, rng=random.Random(123))
    assert [p.paragraph_id for p in first] == [p.paragraph_id for p in second]


def test_min_per_type_zero_works() -> None:
    paras = _make_paragraphs({"dialogue": 5, "narration": 5})
    result = stratified_sample(paras, target_n=4, min_per_type=0, rng=random.Random(7))
    assert len(result) == 4


def test_char_count_weighted() -> None:
    """char_count 大的段被选中概率更高(经验性)。"""
    paras = [FakeParagraph(f"p_{i}", "narration", char_count=10) for i in range(5)]
    paras.append(FakeParagraph("p_heavy", "narration", char_count=10000))
    # 多次抽样验证 heavy 段在 100 次抽样中至少出现 50% 以上
    rng = random.Random(0)
    hits = 0
    for _ in range(100):
        result = stratified_sample(paras, target_n=1, min_per_type=0, rng=rng)
        if result and result[0].paragraph_id == "p_heavy":
            hits += 1
    # 10000 vs 5*10 = 50,权重比 200:1,1 次抽样 hit 概率应远超 50%
    assert hits >= 50, f"heavy paragraph only hit {hits}/100 — weighting may be broken"


# ---------------------------------------------------------------------------
# v2(风格模仿 v2 · W2):定种 rng / 样本量分档 / 比例分层 / 连续窗口
# ---------------------------------------------------------------------------


from novel_system.services.style_reference.sampling import (  # noqa: E402
    derive_extraction_rng,
    group_consecutive_windows,
    proportional_stratified_sample,
    sample_windows,
    scale_sample_target,
    window_position,
)


@dataclass
class IndexedParagraph:
    paragraph_id: str
    paragraph_index: int
    paragraph_type: str
    char_count: int = 200


def _indexed(types: list[str]) -> list[IndexedParagraph]:
    return [IndexedParagraph(f"p_{i:03d}", i, t) for i, t in enumerate(types)]


def test_derive_extraction_rng_is_deterministic_per_checksum_and_run() -> None:
    a = derive_extraction_rng("abc", "sr_run_1")
    b = derive_extraction_rng("abc", "sr_run_1")
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]
    assert derive_extraction_rng("abc", "sr_run_1").random() != derive_extraction_rng("abc", "sr_run_2").random()
    assert derive_extraction_rng("abc", "sr_run_1").random() != derive_extraction_rng("abd", "sr_run_1").random()
    # None 容错
    assert derive_extraction_rng(None, None).random() == derive_extraction_rng("", "").random()


def test_scale_sample_target_tiers() -> None:
    assert scale_sample_target(25, 10_000) == 25
    assert scale_sample_target(25, 49_999) == 25
    assert scale_sample_target(20, 50_000) == 30
    assert scale_sample_target(20, 120_000) == 30
    assert scale_sample_target(25, 250_000) == 50
    assert scale_sample_target(40, 250_000) == 60, "上限 60 段 / 子维"
    assert scale_sample_target(0, 250_000) == 0
    assert scale_sample_target(20, None) == 20
    # yaml 形态的分档
    assert scale_sample_target(
        20, 60_000, scaling=[{"min_chars": 50000, "multiplier": 1.5}], max_n=60
    ) == 30


def test_proportional_sample_follows_type_distribution_with_floor_one() -> None:
    paras = _indexed(["narration"] * 60 + ["dialogue"] * 30 + ["psychology"] * 9 + ["transition"] * 1)
    result = proportional_stratified_sample(paras, target_n=20, rng=random.Random(1))
    counter = Counter(p.paragraph_type for p in result)
    assert len(result) == 20
    assert counter == {"narration": 12, "dialogue": 6, "psychology": 1, "transition": 1}
    indices = [p.paragraph_index for p in result]
    assert indices == sorted(indices), "按 paragraph_index 排序,让 LLM 看到原文顺序"


def test_proportional_sample_returns_all_when_pool_fits() -> None:
    paras = _indexed(["narration"] * 5 + ["dialogue"] * 3)
    result = proportional_stratified_sample(paras, target_n=20, rng=random.Random(0))
    assert [p.paragraph_id for p in result] == [p.paragraph_id for p in paras]


def test_proportional_sample_gives_up_floors_when_target_below_type_count() -> None:
    paras = _indexed(["a"] * 10 + ["b"] * 5 + ["c"] * 2 + ["d"] * 1)
    result = proportional_stratified_sample(paras, target_n=2, rng=random.Random(0))
    assert len(result) == 2
    types = Counter(p.paragraph_type for p in result)
    # 最稀少的段型先让出下限名额
    assert types == {"a": 1, "b": 1}


def test_proportional_sample_is_deterministic() -> None:
    paras = _indexed(["narration"] * 30 + ["dialogue"] * 20)
    first = proportional_stratified_sample(paras, target_n=10, rng=random.Random(9))
    second = proportional_stratified_sample(paras, target_n=10, rng=random.Random(9))
    assert [p.paragraph_id for p in first] == [p.paragraph_id for p in second]


def test_sample_windows_are_contiguous_gapped_and_spread() -> None:
    n = 200
    paras = _indexed(["narration"] * n)
    windows = sample_windows(paras, 20, (3, 6), random.Random(3))
    assert len(windows) >= 3
    total = sum(len(w) for w in windows)
    assert 15 <= total <= 20
    for w in windows:
        assert 1 <= len(w) <= 6
        idx = [p.paragraph_index for p in w]
        assert idx == list(range(idx[0], idx[0] + len(w))), "窗口内段落必须相邻"
    starts = [w[0].paragraph_index for w in windows]
    assert starts == sorted(starts)
    for prev, nxt in zip(windows, windows[1:]):
        assert nxt[0].paragraph_index > prev[-1].paragraph_index + 1, "窗口之间至少留 1 段空隙"
    assert starts[-1] - starts[0] >= n // 5, "首 / 中 / 尾位置分层"


def test_sample_windows_whole_book_when_pool_fits() -> None:
    paras = _indexed(["narration"] * 8)
    windows = sample_windows(paras, 20, (3, 6), random.Random(0))
    assert len(windows) == 1
    assert [p.paragraph_index for p in windows[0]] == list(range(8))


def test_sample_windows_never_bridge_filtered_gaps() -> None:
    """被禁用词过滤掉的段(index 5 / 21)不得被窗口跨越。"""
    paras = [p for p in _indexed(["narration"] * 40) if p.paragraph_index not in (5, 21)]
    windows = sample_windows(paras, 12, (3, 4), random.Random(2))
    assert windows
    for w in windows:
        idx = [p.paragraph_index for p in w]
        assert idx == list(range(idx[0], idx[0] + len(w)))
        assert 5 not in idx and 21 not in idx


def test_sample_windows_deterministic_and_group_roundtrip() -> None:
    paras = _indexed(["narration"] * 120)
    w1 = sample_windows(paras, 18, (3, 6), random.Random(11))
    w2 = sample_windows(paras, 18, (3, 6), random.Random(11))
    assert [[p.paragraph_id for p in w] for w in w1] == [[p.paragraph_id for p in w] for w in w2]
    flat = [p for w in w1 for p in w]
    assert group_consecutive_windows(flat) == w1


def test_sample_windows_small_target_yields_single_short_window() -> None:
    paras = _indexed(["narration"] * 50)
    windows = sample_windows(paras, 2, (3, 6), random.Random(0))
    assert len(windows) == 1 and len(windows[0]) == 2


def test_window_position_labels() -> None:
    assert window_position(0, 1) == "first"
    assert window_position(0, 3) == "first"
    assert window_position(1, 3) == "middle"
    assert window_position(2, 3) == "last"

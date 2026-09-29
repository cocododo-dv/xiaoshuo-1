"""Wave 6（结果闭环治理 §5.8）：价格快照解析与成本折算。

价格集中在 ``config/pricing.yaml``，按 (provider, model) + effective_at 解析；
未命中回落 default_estimate 并标 is_estimate=True；成本 = token/1000 × 单价。
"""
from __future__ import annotations

import textwrap

import pytest

from novel_system.services import pricing


def test_resolve_price_known_model_from_config():
    snap = pricing.resolve_price("openai_compatible", "gpt-5")
    assert snap.provider == "openai_compatible"
    assert snap.model == "gpt-5"
    assert snap.input_per_1k > 0
    assert snap.output_per_1k > 0
    assert snap.currency == "USD"


def test_resolve_price_unknown_model_falls_back_to_estimate():
    snap = pricing.resolve_price("some_provider", "unknown-model-xyz")
    assert snap.is_estimate is True
    # 兜底口径仍给出可用单价，方便成本页永远能显示一个（估算）数字
    assert snap.input_per_1k > 0
    assert snap.output_per_1k > 0


def test_resolve_price_picks_latest_effective_snapshot(tmp_path, monkeypatch):
    book = tmp_path / "pricing.yaml"
    book.write_text(
        textwrap.dedent(
            """
            version: 1
            default_estimate: {input_per_1k: 0.5, output_per_1k: 1.5, currency: USD, is_estimate: true}
            snapshots:
              - {provider: p, model: m, effective_at: "2026-01-01T00:00:00Z", input_per_1k: 1.0, output_per_1k: 2.0, currency: USD, is_estimate: false}
              - {provider: p, model: m, effective_at: "2026-06-01T00:00:00Z", input_per_1k: 3.0, output_per_1k: 4.0, currency: USD, is_estimate: false}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pricing, "_price_book_path", lambda: book)
    pricing.reset_price_book_cache()
    # at 落在两个快照之间 → 取较早生效的那个
    early = pricing.resolve_price("p", "m", at="2026-03-01T00:00:00Z")
    assert early.input_per_1k == 1.0
    # at 落在两者之后 → 取最新生效
    late = pricing.resolve_price("p", "m", at="2026-09-01T00:00:00Z")
    assert late.input_per_1k == 3.0


def test_compute_cost_math():
    # 用已知单价的临时价书，断言算式 = tokens/1000 × 单价
    result = pricing.compute_cost("openai_compatible", "gpt-5", 2000, 1000)
    snap = pricing.resolve_price("openai_compatible", "gpt-5")
    expected_input = 2000 / 1000 * snap.input_per_1k
    expected_output = 1000 / 1000 * snap.output_per_1k
    assert result["input_cost"] == pytest.approx(expected_input)
    assert result["output_cost"] == pytest.approx(expected_output)
    assert result["cost"] == pytest.approx(expected_input + expected_output)
    assert result["currency"] == "USD"
    assert result["is_estimate"] is True  # 占位价书全部估算


def test_compute_cost_zero_tokens():
    result = pricing.compute_cost("openai_compatible", "gpt-5", 0, 0)
    assert result["cost"] == 0.0


def test_load_price_book_missing_file_hard_fallback(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(pricing, "_price_book_path", lambda: missing)
    pricing.reset_price_book_cache()
    # 文件缺失不得抛：硬回退到内置估算，成本页永不 500
    snap = pricing.resolve_price("openai_compatible", "gpt-5")
    assert snap.is_estimate is True
    assert snap.input_per_1k > 0

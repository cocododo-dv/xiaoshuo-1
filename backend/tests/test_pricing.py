"""模型单价（``config/pricing.yaml``）：只有写了单价的模型才折算金额，其余「未定价」（批准#4，审计 B09-17）。

价书按 (provider, model) + ``effective_at`` 解析；没有兜底估算价——仓库里的价书不带任何单价，
所以默认安装的每个模型都是未定价。成本 = token / 1000 × 单价。
"""
from __future__ import annotations

import logging
import textwrap

import pytest

from novel_system.services import pricing


@pytest.fixture
def price_book(tmp_path, monkeypatch):
    path = tmp_path / "pricing.yaml"
    monkeypatch.setattr(pricing, "_price_book_path", lambda: path)
    pricing.reset_price_book_cache()

    def write(text: str):
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    return write


def test_repository_price_book_prices_nothing():
    """仓库里的价书不带占位价：gpt-5 这类以前的占位估算与任何真实模型都是未定价。"""
    book = pricing.load_price_book()
    assert book.prices == ()
    assert book.currency == "USD"
    assert pricing.resolve_price("openai_compatible", "gpt-5") is None
    assert pricing.resolve_price("openai_compatible", "any-relay-model") is None
    assert pricing.compute_cost("openai_compatible", "gpt-5", 2000, 1000) == {
        "priced": False,
        "cost": None,
        "input_cost": None,
        "output_cost": None,
        "currency": None,
        "unit": None,
    }


def test_compute_cost_math_for_a_priced_model(price_book):
    price_book(
        """
        currency: USD
        prices:
          - {provider: p, model: m, input_per_1k: 1.5, output_per_1k: 6}
        """
    )
    result = pricing.compute_cost("p", "m", 2000, 1000)
    assert result["priced"] is True
    assert result["input_cost"] == pytest.approx(3.0)
    assert result["output_cost"] == pytest.approx(6.0)
    assert result["cost"] == pytest.approx(9.0)
    assert result["currency"] == "USD"
    assert result["unit"] == {"input_per_1k": 1.5, "output_per_1k": 6.0, "effective_at": None}
    assert pricing.compute_cost("p", "m", 0, 0)["cost"] == 0.0
    # 同一服务的另一个模型、另一服务的同名模型：都未定价
    assert pricing.compute_cost("p", "other", 10, 10)["priced"] is False
    assert pricing.compute_cost("q", "m", 10, 10)["priced"] is False


def test_resolve_price_picks_the_entry_in_effect_at_call_time(price_book):
    price_book(
        """
        prices:
          - {provider: p, model: m, effective_at: "2026-01-01T00:00:00Z", input_per_1k: 1.0, output_per_1k: 2.0}
          - {provider: p, model: m, effective_at: "2026-06-01T00:00:00Z", input_per_1k: 3.0, output_per_1k: 4.0}
        """
    )
    assert pricing.resolve_price("p", "m", at="2026-03-01T00:00:00Z").input_per_1k == 1.0
    assert pricing.resolve_price("p", "m", at="2026-09-01T00:00:00Z").input_per_1k == 3.0
    assert pricing.resolve_price("p", "m").input_per_1k == 3.0
    # 生效之前的调用没有单价：不拿后来的价去折算
    assert pricing.resolve_price("p", "m", at="2025-12-31T00:00:00Z") is None


def test_invalid_foreign_currency_and_retired_entries_are_skipped_with_a_warning(price_book, caplog):
    price_book(
        """
        currency: CNY
        default_estimate: {input_per_1k: 0.5, output_per_1k: 1.5, currency: USD, is_estimate: true}
        snapshots:
          - {provider: p, model: old, input_per_1k: 1, output_per_1k: 1}
        prices:
          - {provider: p, model: ok, input_per_1k: 0.2, output_per_1k: 0.4, currency: CNY}
          - {provider: p, model: usd, input_per_1k: 1, output_per_1k: 1, currency: USD}
          - {provider: p, model: negative, input_per_1k: -1, output_per_1k: 1}
          - {provider: p, model: text, input_per_1k: "cheap", output_per_1k: 1}
          - {model: no-provider, input_per_1k: 1, output_per_1k: 1}
          - just a string
        """
    )
    with caplog.at_level(logging.WARNING, logger="novel_system.services.pricing"):
        book = pricing.load_price_book()
    assert book.currency == "CNY"
    assert [price.model for price in book.prices] == ["ok"]
    assert pricing.compute_cost("p", "ok", 1000, 1000)["currency"] == "CNY"
    # 退役格式里的兜底价与占位条目不再折算任何东西
    assert pricing.resolve_price("p", "old") is None
    assert pricing.resolve_price("anything", "else") is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "default_estimate, snapshots" in messages
    assert "p/usd in USD" in messages
    assert messages.count("skipped a price entry") == 4


def test_an_edited_price_book_takes_effect_without_a_restart(price_book):
    price_book("prices: []\n")
    assert pricing.resolve_price("p", "m") is None
    price_book("prices: [{provider: p, model: m, input_per_1k: 1, output_per_1k: 1}]\n")
    assert pricing.resolve_price("p", "m") is not None


@pytest.mark.parametrize("text", [None, "prices: [\n", "- just\n- a list\n"])
def test_missing_or_unreadable_price_book_prices_nothing_and_never_raises(price_book, text):
    if text is not None:
        price_book(text)
    assert pricing.load_price_book() == pricing.EMPTY_PRICE_BOOK
    assert pricing.compute_cost("p", "m", 10, 10)["priced"] is False

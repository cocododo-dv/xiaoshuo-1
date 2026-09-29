"""模型单价：把一次调用的 token 折算成金额——只对作者在 ``config/pricing.yaml`` 里写了单价的模型。

2026-09-30 重构 P06（批准#4，审计 B09-17）：成本看板以 token 为主。以前价书里只有两条占位估算价
（``openai_compatible`` 的 gpt-5 / gpt-5-mini），其余每个真实模型（经中转接入的全部模型）都回落到
``default_estimate`` 的 0.5 / 1.5 美元每千 token——看板上的美元数是编出来的。现在没有兜底价：
价书里没有的 (provider, model) 就是「未定价」，:func:`compute_cost` 回 ``priced: False`` / ``cost: None``。

价书格式（``prices`` 里每一条）：``provider``（调用记录里的服务类型，经中转接入的一般是 ``openai_compatible``）、
``model``（发给服务的模型 id）、``input_per_1k`` / ``output_per_1k``（每 1000 token 的单价，非负数）、
可选的 ``effective_at``（ISO 时间，起生效；同一模型可写多条，按调用时间取当时生效的最新一条；不写 = 一直生效）。
整本价书一个币种（顶层 ``currency``，默认 USD）：一条写了别的币种的单价不参与折算（记一条告警），
免得把两种货币加在一起。

读路径永不抛：文件缺失、解析失败、条目不合法都只是少了单价（记告警），成本页不会因为价书 500。
缓存按文件正文：改了价书，下一次读取就用新单价，不用重启后端。每次 :func:`load_price_book` 都读一遍文件，
所以一次聚合要折算很多条调用时，先读一次价书，再把它经 ``book=`` 交给 :func:`compute_cost`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_system.cache_registry import register_cache_reset
from novel_system.services.config_cache import safe_load_yaml

_LOGGER = logging.getLogger(__name__)

DEFAULT_CURRENCY = "USD"
# 以前的价书格式：占位估算价与它的兜底口径（已退役，出现时只记告警、不读）
RETIRED_KEYS = ("default_estimate", "snapshots")


@dataclass(frozen=True, slots=True)
class Price:
    provider: str
    model: str
    input_per_1k: float
    output_per_1k: float
    effective_at: str | None = None


@dataclass(frozen=True, slots=True)
class PriceBook:
    currency: str
    prices: tuple[Price, ...]


EMPTY_PRICE_BOOK = PriceBook(currency=DEFAULT_CURRENCY, prices=())

# (价书正文, 解析结果)；正文变了就重新解析
_CACHE: tuple[str, PriceBook] | None = None


def _price_book_path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "pricing.yaml"


def reset_price_book_cache() -> None:
    global _CACHE
    _CACHE = None


register_cache_reset("pricing.price_book", reset_price_book_cache)


def _price_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_price(raw: Any, currency: str) -> Price | None:
    if not isinstance(raw, dict):
        _LOGGER.warning("pricing.yaml: skipped a price entry that is not a mapping: %r", raw)
        return None
    provider, model = _text(raw.get("provider")), _text(raw.get("model"))
    input_per_1k, output_per_1k = _price_number(raw.get("input_per_1k")), _price_number(raw.get("output_per_1k"))
    if not provider or not model or input_per_1k is None or output_per_1k is None:
        _LOGGER.warning(
            "pricing.yaml: skipped a price entry without provider / model / non-negative input_per_1k and output_per_1k: %r",
            raw,
        )
        return None
    entry_currency = _text(raw.get("currency")) or currency
    if entry_currency != currency:
        _LOGGER.warning(
            "pricing.yaml: skipped the price of %s/%s in %s (the price book is in %s)",
            provider,
            model,
            entry_currency,
            currency,
        )
        return None
    effective_at = _text(raw.get("effective_at")) or None
    return Price(provider, model, input_per_1k, output_per_1k, effective_at)


def parse_price_book(raw: Any) -> PriceBook:
    """价书内容（``yaml`` 解析后的对象）→ :class:`PriceBook`；不合法的部分跳过并记告警，从不抛。"""
    if not isinstance(raw, dict):
        return EMPTY_PRICE_BOOK
    retired = [key for key in RETIRED_KEYS if key in raw]
    if retired:
        _LOGGER.warning(
            "pricing.yaml: %s belong to the retired placeholder-estimate format and are ignored; "
            "write real prices under `prices`",
            ", ".join(retired),
        )
    currency = _text(raw.get("currency")) or DEFAULT_CURRENCY
    entries = raw.get("prices")
    parsed = (_parse_price(entry, currency) for entry in (entries if isinstance(entries, list) else []))
    return PriceBook(currency=currency, prices=tuple(price for price in parsed if price is not None))


def load_price_book() -> PriceBook:
    global _CACHE
    path = _price_book_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_PRICE_BOOK
    except OSError:
        _LOGGER.warning("pricing.yaml could not be read at %s; every model is unpriced", path, exc_info=True)
        return EMPTY_PRICE_BOOK
    cached = _CACHE
    if cached is not None and cached[0] == text:
        return cached[1]
    try:
        book = parse_price_book(safe_load_yaml(text))
    except Exception:  # 解析失败：全部未定价，读路径不抛
        _LOGGER.warning("pricing.yaml could not be parsed; every model is unpriced", exc_info=True)
        book = EMPTY_PRICE_BOOK
    _CACHE = (text, book)
    return book


def resolve_price(
    provider: str | None,
    model: str | None,
    at: str | None = None,
    *,
    book: PriceBook | None = None,
) -> Price | None:
    """(provider, model) 在 ``at`` 时生效的单价；价书里没有 → ``None``（未定价）。

    ``effective_at`` 与 ``at`` 都是 UTC ISO 字符串，按字典序即时间序比较；``at=None`` 取最新一条。
    """
    candidates = [
        price
        for price in (book if book is not None else load_price_book()).prices
        if price.provider == provider
        and price.model == model
        and (at is None or price.effective_at is None or price.effective_at <= at)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda price: price.effective_at or "")


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def unpriced() -> dict[str, Any]:
    return {"priced": False, "cost": None, "input_cost": None, "output_cost": None, "currency": None, "unit": None}


def compute_cost(
    provider: str | None,
    model: str | None,
    prompt_tokens: Any,
    completion_tokens: Any,
    at: str | None = None,
    *,
    book: PriceBook | None = None,
) -> dict[str, Any]:
    """token → 金额（token / 1000 × 单价）。未定价的模型回 ``priced: False``，金额字段全是 ``None``。"""
    if book is None:
        book = load_price_book()
    price = resolve_price(provider, model, at=at, book=book)
    if price is None:
        return unpriced()
    input_cost = _as_int(prompt_tokens) / 1000.0 * price.input_per_1k
    output_cost = _as_int(completion_tokens) / 1000.0 * price.output_per_1k
    return {
        "priced": True,
        "cost": input_cost + output_cost,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "currency": book.currency,
        "unit": {
            "input_per_1k": price.input_per_1k,
            "output_per_1k": price.output_per_1k,
            "effective_at": price.effective_at,
        },
    }


__all__ = [
    "DEFAULT_CURRENCY",
    "EMPTY_PRICE_BOOK",
    "Price",
    "PriceBook",
    "compute_cost",
    "load_price_book",
    "parse_price_book",
    "reset_price_book_cache",
    "resolve_price",
    "unpriced",
]

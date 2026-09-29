"""价格快照（结果闭环治理设计 §5.8，Wave 6）。

成本聚合的价格层：把 ``LlmCall`` 的 token 折算成费用。设计要求「新增可配置价格
快照」且「不向每条调用写死价格」——价格集中在 ``config/pricing.yaml``，按
(provider, model) + ``effective_at`` 解析，未命中回落 ``default_estimate``。

诚实标注：占位价书全部 ``is_estimate: true``；文件缺失时硬回退到内置估算，
读路径永不抛（成本页/信号面板不能因价书问题 500）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_system.services.config_cache import safe_load_yaml

_LOGGER = logging.getLogger(__name__)

# 内置硬回退（价书文件缺失/解析失败时用；与 config/pricing.yaml default_estimate 一致语义）
_BUILTIN_DEFAULT = {
    "input_per_1k": 0.5,
    "output_per_1k": 1.5,
    "currency": "USD",
    "is_estimate": True,
}


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    provider: str | None
    model: str | None
    input_per_1k: float
    output_per_1k: float
    currency: str
    is_estimate: bool
    effective_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_per_1k": self.input_per_1k,
            "output_per_1k": self.output_per_1k,
            "currency": self.currency,
            "is_estimate": self.is_estimate,
            "effective_at": self.effective_at,
        }


@dataclass(frozen=True, slots=True)
class PriceBook:
    default_estimate: PriceSnapshot
    snapshots: tuple[PriceSnapshot, ...]


_CACHE: PriceBook | None = None


def _price_book_path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "pricing.yaml"


def reset_price_book_cache() -> None:
    """测试/配置热更用：清缓存下次 load 重读文件。"""
    global _CACHE
    _CACHE = None


def _coerce_snapshot(raw: dict[str, Any], *, default_is_estimate: bool = True) -> PriceSnapshot:
    return PriceSnapshot(
        provider=raw.get("provider"),
        model=raw.get("model"),
        input_per_1k=float(raw.get("input_per_1k", _BUILTIN_DEFAULT["input_per_1k"])),
        output_per_1k=float(raw.get("output_per_1k", _BUILTIN_DEFAULT["output_per_1k"])),
        currency=str(raw.get("currency", "USD")),
        is_estimate=bool(raw.get("is_estimate", default_is_estimate)),
        effective_at=raw.get("effective_at"),
    )


def _builtin_default_snapshot() -> PriceSnapshot:
    return _coerce_snapshot(dict(_BUILTIN_DEFAULT))


def load_price_book() -> PriceBook:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _price_book_path()
    default = _builtin_default_snapshot()
    snapshots: list[PriceSnapshot] = []
    try:
        raw = safe_load_yaml(path.read_text(encoding="utf-8")) or {}
        default_raw = raw.get("default_estimate")
        if isinstance(default_raw, dict):
            default = _coerce_snapshot(default_raw)
        for entry in raw.get("snapshots") or []:
            if isinstance(entry, dict) and entry.get("provider") and entry.get("model"):
                snapshots.append(_coerce_snapshot(entry, default_is_estimate=True))
    except FileNotFoundError:
        _LOGGER.info("pricing.yaml not found at %s; using builtin estimate", path)
    except Exception:  # 解析失败硬回退，读路径永不抛（§5.8）
        _LOGGER.warning("pricing.yaml could not be parsed; using builtin estimate", exc_info=True)
    _CACHE = PriceBook(default_estimate=default, snapshots=tuple(snapshots))
    return _CACHE


def resolve_price(provider: str | None, model: str | None, at: str | None = None) -> PriceSnapshot:
    """取 (provider, model) 生效于 ``at`` 的最新快照；未命中回落 default_estimate。

    ``effective_at`` 为 ISO 字符串，按字典序即时间序比较（UTC "Z" 后缀）。``at=None``
    视为「现在或之后」——取该 (provider, model) 最新一条。
    """
    book = load_price_book()
    candidates = [
        s for s in book.snapshots
        if s.provider == provider and s.model == model
        and (at is None or (s.effective_at or "") <= at)
    ]
    if candidates:
        return max(candidates, key=lambda s: s.effective_at or "")
    return book.default_estimate


def compute_cost(
    provider: str | None,
    model: str | None,
    prompt_tokens: Any,
    completion_tokens: Any,
    at: str | None = None,
) -> dict[str, Any]:
    """token → 费用：cost = tokens/1000 × 单价。返回口径含 is_estimate 与 unit。"""
    snap = resolve_price(provider, model, at=at)
    p = _as_int(prompt_tokens)
    c = _as_int(completion_tokens)
    input_cost = p / 1000.0 * snap.input_per_1k
    output_cost = c / 1000.0 * snap.output_per_1k
    return {
        "cost": input_cost + output_cost,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "currency": snap.currency,
        "is_estimate": snap.is_estimate,
        "unit": {
            "input_per_1k": snap.input_per_1k,
            "output_per_1k": snap.output_per_1k,
            "effective_at": snap.effective_at,
        },
    }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

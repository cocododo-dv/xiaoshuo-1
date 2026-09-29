"""The shared coercion helpers keep each former variant's behaviour under its own name."""

from __future__ import annotations

import math

from novel_system.services import value_coercion as vc


def test_finite_variants_differ_only_where_the_old_copies_did() -> None:
    assert vc.finite_or_none(True) is None
    assert vc.finite_or_none_accepting_bool(True) == 1.0
    assert vc.finite_or_zero(True) == 1.0
    for bad in ("x", None, math.nan, math.inf):
        assert vc.finite_or_none(bad) is None
        assert vc.finite_or_none_accepting_bool(bad) is None
        assert vc.finite_or_zero(bad) == 0.0
    assert vc.finite_or_none("2.5") == 2.5


def test_int_usage_and_text_helpers() -> None:
    assert vc.int_or_default("7", 1) == 7
    assert vc.int_or_default("x", 3) == 3
    assert vc.int_or_default(None, 0) == 0
    assert vc.usage_int(12.0) == 12
    assert vc.usage_int(True) is None
    assert vc.usage_int(-1) is None
    assert vc.usage_int(1.5) is None
    assert vc.optional_text("  旧信 ") == "旧信"
    assert vc.optional_text("  ") is None
    assert vc.optional_text(3) is None


def test_list_value_and_quantile_helpers() -> None:
    assert vc.coerce_string_list("a\n\n b ") == ["a", "b"]
    assert vc.coerce_string_list([" a", "", 3]) == ["a", "3"]
    assert vc.coerce_string_list({"a": 1}) == []
    assert vc.has_value({"a": [" ", {"b": None}]}) is False
    assert vc.has_value({"a": [0]}) is True
    assert vc.quantile([], 0.5) == 0.0
    assert vc.quantile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert vc.quantile([1.0, 2.0], 0.25) == 1.25

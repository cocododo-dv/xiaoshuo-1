"""The shared environment parsers keep the lenient / strict rules per variable."""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_system import env_parsing


def test_lenient_bool_treats_unknown_as_false(monkeypatch) -> None:
    monkeypatch.setenv("XR_FLAG", "ture")
    assert env_parsing.bool_env("XR_FLAG", True) is False
    monkeypatch.setenv("XR_FLAG", " Yes ")
    assert env_parsing.bool_env("XR_FLAG", False) is True
    monkeypatch.delenv("XR_FLAG")
    assert env_parsing.bool_env("XR_FLAG", True) is True


def test_strict_bool_rejects_unknown(monkeypatch) -> None:
    monkeypatch.setenv("XR_FLAG", "off")
    assert env_parsing.bool_env("XR_FLAG", True, strict=True) is False
    monkeypatch.setenv("XR_FLAG", "ture")
    with pytest.raises(ValueError, match="XR_FLAG must be a boolean"):
        env_parsing.bool_env("XR_FLAG", True, strict=True)


def test_integer_and_float_parsers(monkeypatch) -> None:
    monkeypatch.setenv("XR_N", "0")
    assert env_parsing.quota_int_env("XR_N", 5) == 0
    with pytest.raises(ValueError, match="XR_N must be a positive integer"):
        env_parsing.positive_int_env("XR_N", 5)
    monkeypatch.setenv("XR_N", "-1")
    with pytest.raises(ValueError, match=r"non-negative integer \(0 disables the limit\)"):
        env_parsing.quota_int_env("XR_N", 5)
    with pytest.raises(ValueError, match="XR_N must be non-negative"):
        env_parsing.non_negative_float_env("XR_N", 1.0)
    monkeypatch.setenv("XR_N", "x")
    with pytest.raises(ValueError, match="XR_N must be a valid number"):
        env_parsing.non_negative_float_env("XR_N", 1.0)


def test_list_parsers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XR_LIST", " , ")
    assert env_parsing.list_env("XR_LIST", ("a",)) == ("a",)
    monkeypatch.setenv("XR_LIST", "a, b,,c")
    assert env_parsing.list_env("XR_LIST", ()) == ("a", "b", "c")
    import os

    monkeypatch.setenv("XR_PATHS", os.pathsep.join(["one", " ", "two"]))
    assert env_parsing.path_list_env("XR_PATHS", lambda item: tmp_path / item) == (
        tmp_path / "one",
        tmp_path / "two",
    )
    monkeypatch.setenv("XR_PATHS", "  ")
    assert env_parsing.path_list_env("XR_PATHS", Path) == ()

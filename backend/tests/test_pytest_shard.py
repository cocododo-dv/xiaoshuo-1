from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import pytest_shard


def _make_test_files(root: Path, count: int) -> list[Path]:
    files = []
    for index in reversed(range(count)):
        path = root / f"test_{index:02d}.py"
        path.write_text("", encoding="utf-8")
        files.append(path)
    (root / "helper.py").write_text("", encoding="utf-8")
    return sorted(files, key=lambda path: path.name)


def test_shards_cover_every_test_file_once(monkeypatch, tmp_path: Path) -> None:
    expected = _make_test_files(tmp_path, 11)
    monkeypatch.setattr(pytest_shard, "TESTS_ROOT", tmp_path)

    shards = [
        pytest_shard.select_shard(shard_index=index, shard_count=4)
        for index in range(4)
    ]

    assert sorted(path for shard in shards for path in shard) == expected
    assert sum(len(set(shard)) for shard in shards) == len(expected)
    assert all(set(left).isdisjoint(right) for i, left in enumerate(shards) for right in shards[i + 1 :])


@pytest.mark.parametrize(
    ("index", "count"),
    [(0, 0), (-1, 4), (4, 4)],
)
def test_invalid_shard_parameters_fail(index: int, count: int) -> None:
    with pytest.raises(ValueError):
        pytest_shard.select_shard(shard_index=index, shard_count=count)


def _names(paths: list[Path]) -> list[str]:
    return [path.name for path in paths]


@pytest.mark.parametrize("shard_count", [1, 2, 3, 4, 5, 6, 8])
def test_real_suite_partitions_with_committed_durations(shard_count: int) -> None:
    selected = [
        _names(pytest_shard.select_shard(shard_index=index, shard_count=shard_count))
        for index in range(shard_count)
    ]
    flat = [name for names in selected for name in names]
    assert sorted(flat) == _names(pytest_shard.discover_test_files())
    assert len(flat) == len(set(flat))
    assert all(selected)
    assert Path(__file__).name in flat


def test_split_is_deterministic_and_independent_of_listing_order() -> None:
    names = _names(pytest_shard.discover_test_files())
    durations = pytest_shard.load_durations()
    first = pytest_shard.plan_shards(names, durations, 4)
    assert pytest_shard.plan_shards(list(reversed(names)), durations, 4) == first
    assert pytest_shard.plan_shards(names, dict(reversed(list(durations.items()))), 4) == first
    assert [_names(pytest_shard.select_shard(shard_index=index, shard_count=4)) for index in range(4)] == first


def test_packing_follows_durations_not_file_order() -> None:
    durations = {"test_a.py": 9.0, "test_b.py": 5.0, "test_c.py": 4.0, "test_d.py": 3.0, "test_e.py": 3.0}
    plan = pytest_shard.plan_shards(durations, durations, 2)
    # file-index modulo would give a+c+e = 16 s against b+d = 8 s
    assert plan == [["test_a.py", "test_d.py"], ["test_b.py", "test_c.py", "test_e.py"]]
    assert [sum(durations[name] for name in names) for names in plan] == [12.0, 12.0]


def test_new_files_weigh_the_median_and_stale_entries_are_ignored() -> None:
    durations = {"test_a.py": 1.0, "test_b.py": 2.0, "test_c.py": 30.0, "test_gone.py": 500.0}
    names = ["test_a.py", "test_b.py", "test_c.py", "test_new.py"]
    # median of the present known files is 2 s: test_new.py joins the small files; test_gone.py no longer exists
    assert pytest_shard.plan_shards(names, durations, 2) == [["test_c.py"], ["test_a.py", "test_b.py", "test_new.py"]]
    # no durations at all: equal weights, dealt out in name order
    assert pytest_shard.plan_shards(names, {}, 2) == [["test_a.py", "test_c.py"], ["test_b.py", "test_new.py"]]


def test_committed_durations_file_is_well_formed() -> None:
    durations = json.loads(pytest_shard.DURATIONS_PATH.read_text(encoding="utf-8"))
    assert durations
    for name, seconds in durations.items():
        assert name.startswith("test_") and name.endswith(".py"), name
        assert isinstance(seconds, (int, float)) and seconds >= 0, name


def test_list_only_prints_backend_relative_paths(capsys) -> None:
    printed: list[str] = []
    for index in range(4):
        assert pytest_shard.main(["--shard-index", str(index), "--shard-count", "4", "--list-only"]) == 0
        printed.extend(capsys.readouterr().out.split())
    assert sorted(printed) == [f"tests/{name}" for name in _names(pytest_shard.discover_test_files())]


@pytest.mark.parametrize(
    "argv",
    [
        ["--shard-index", "4", "--shard-count", "4", "--list-only"],
        ["--shard-count", "4", "--list-only"],
    ],
)
def test_cli_rejects_invalid_shard_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        pytest_shard.main(argv)
    assert exc_info.value.code == 2


def test_update_durations_sums_junit_times_per_file(monkeypatch, tmp_path: Path) -> None:
    junit = tmp_path / "backend-shard-0.xml"
    junit.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_alpha" name="test_one" time="1.25" />'
        '<testcase classname="tests.test_alpha" name="test_two[x]" time="0.5" />'
        '<testcase classname="tests.test_alpha.TestGroup" name="test_three" time="0.25" />'
        '<testcase classname="tests.test_beta" name="test_one" time="3" />'
        '<testcase classname="tests.fixture_runtime" name="helper" time="9" />'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    target = tmp_path / ".durations.json"
    monkeypatch.setattr(pytest_shard, "DURATIONS_PATH", target)

    assert pytest_shard.main(["--update-durations", str(junit)]) == 0

    assert json.loads(target.read_text(encoding="utf-8")) == {"test_alpha.py": 2.0, "test_beta.py": 3.0}

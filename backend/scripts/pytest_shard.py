"""Run one deterministic shard of the backend test-file suite.

Test files are packed longest-first over the committed CI durations in ``tests/.durations.json`` (seconds per
test file): each file goes to the shard with the least time so far (greedy LPT), so the shards finish at about
the same time instead of one shard carrying the slowest files. A file missing from the durations file (a new
test file) counts as the median duration. Every file lands in exactly one shard, and the split depends only on
the file names and the durations file.

Refresh the durations file when test files were added, split or became much slower or faster: download the
JUnit files of all shards of a green CI run and rewrite it from ``backend/``::

    gh run download <run-id> --pattern 'backend-shard-*-junit' --dir /tmp/junit
    python scripts/pytest_shard.py --update-durations /tmp/junit/*/*.xml
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = BACKEND_ROOT / "tests"
DURATIONS_PATH = TESTS_ROOT / ".durations.json"
# Weight of every file when no duration is known at all (the split then balances file counts).
FALLBACK_SECONDS = 1.0


def discover_test_files(tests_root: Path | None = None) -> list[Path]:
    return sorted((tests_root or TESTS_ROOT).glob("test_*.py"), key=lambda path: path.name)


def load_durations(path: Path | None = None) -> dict[str, float]:
    path = path or DURATIONS_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): float(seconds) for name, seconds in data.items()}


def plan_shards(names: Iterable[str], durations: Mapping[str, float], shard_count: int) -> list[list[str]]:
    """Greedy LPT: longest file first, each into the least-loaded shard (ties: file name, then lower shard index)."""

    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    names = sorted(set(names))
    known = [durations[name] for name in names if name in durations]
    default = statistics.median(known) if known else FALLBACK_SECONDS
    weight = {name: durations.get(name, default) for name in names}
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count
    for name in sorted(names, key=lambda item: (-weight[item], item)):
        target = min(range(shard_count), key=lambda index: (loads[index], index))
        shards[target].append(name)
        loads[target] += weight[name]
    return [sorted(shard) for shard in shards]


def select_shard(
    *,
    shard_index: int,
    shard_count: int,
    files: list[Path] | None = None,
    durations: Mapping[str, float] | None = None,
) -> list[Path]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must satisfy 0 <= index < count")
    files = discover_test_files() if files is None else files
    durations = load_durations() if durations is None else durations
    by_name = {path.name: path for path in files}
    chosen = plan_shards(by_name, durations, shard_count)[shard_index]
    return [by_name[name] for name in chosen]


def durations_from_junit(paths: Iterable[Path]) -> dict[str, float]:
    """Seconds per test file from pytest JUnit XML (``classname`` is ``tests.test_x`` or ``tests.test_x.TestCls``)."""

    totals: dict[str, float] = {}
    for path in paths:
        for case in ET.parse(path).iter("testcase"):
            parts = case.get("classname", "").split(".")
            if len(parts) < 2 or parts[0] != "tests" or not parts[1].startswith("test_"):
                continue
            name = f"{parts[1]}.py"
            totals[name] = totals.get(name, 0.0) + float(case.get("time") or 0.0)
    return {name: round(seconds, 2) for name, seconds in sorted(totals.items())}


def write_durations(durations: Mapping[str, float], path: Path | None = None) -> None:
    (path or DURATIONS_PATH).write_text(json.dumps(dict(sorted(durations.items())), indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--update-durations",
        nargs="+",
        type=Path,
        metavar="JUNIT_XML",
        help="rewrite tests/.durations.json from the JUnit files of every shard of one run, then exit",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.update_durations:
        durations = durations_from_junit(args.update_durations)
        if not durations:
            parser.error("the JUnit files contain no backend test cases")
        write_durations(durations)
        print(f"wrote {len(durations)} file durations to {DURATIONS_PATH}")
        return 0
    if args.shard_index is None or args.shard_count is None:
        parser.error("--shard-index and --shard-count are required")
    try:
        selected = select_shard(shard_index=args.shard_index, shard_count=args.shard_count)
    except ValueError as exc:
        parser.error(str(exc))
    if not selected:
        parser.error("selected shard contains no test files")
    if args.list_only:
        for path in selected:
            print(path.relative_to(BACKEND_ROOT).as_posix())
        return 0
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
        *(str(path.relative_to(BACKEND_ROOT)) for path in selected),
    ]
    return subprocess.run(command, cwd=BACKEND_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""ORM 模型按领域分包、一个门面（B12-09）：``from novel_system.db.models import <类> | utcnow | Base`` 照旧可用。

守住：门面导出全部映射类；每个类都住在某个领域子模块里；子模块从不回头引门面（没有导入环）；外键查找索引
（``_indexes``，要在全部表登记之后建）都登记上了。
"""

from __future__ import annotations

import ast
from pathlib import Path

from novel_system.db import models
from novel_system.db.base import Base
from novel_system.db.models._indexes import _FOREIGN_KEY_LOOKUP_INDEXES

PACKAGE_DIR = Path(models.__file__).resolve().parent


def test_facade_exports_every_mapped_class_utcnow_and_base() -> None:
    mapped = {mapper.class_.__name__ for mapper in Base.registry.mappers}
    assert len(mapped) > 60
    assert mapped | {"Base", "utcnow"} == set(models.__all__)
    for name in models.__all__:
        assert getattr(models, name) is not None


def test_every_mapped_class_lives_in_a_domain_submodule() -> None:
    misplaced = sorted(
        f"{mapper.class_.__name__} ({mapper.class_.__module__})"
        for mapper in Base.registry.mappers
        if not mapper.class_.__module__.startswith("novel_system.db.models.")
        or mapper.class_.__module__.rsplit(".", 1)[1].startswith("_")
    )
    assert misplaced == []


def test_submodules_never_import_the_facade() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "novel_system.db.models":
                offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.level and node.module in {None, ""}:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_foreign_key_lookup_indexes_are_registered_after_every_table() -> None:
    missing = [
        f"ix_{table}_{column}"
        for table, column in _FOREIGN_KEY_LOOKUP_INDEXES
        if f"ix_{table}_{column}" not in {index.name for index in Base.metadata.tables[table].indexes}
    ]
    assert missing == []

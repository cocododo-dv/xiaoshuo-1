from __future__ import annotations

import ast
from pathlib import Path

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.writer_briefs import (
    empty_scene_writer_brief,
    normalize_scene_writer_brief,
    writer_brief_has_content,
)


SERVICES_ROOT = Path(__file__).resolve().parents[1] / "src" / "novel_system" / "services"


def _source_depends_on(source: str, module: str) -> bool:
    """``source`` 是否依赖 ``module``：引它本身、它的子模块（拆成包之后的 ``module.xxx``），或
    ``from <上一级包> import <它>``——函数体里的延迟导入也算。"""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    return any(name == module or name.startswith(f"{module}.") for name in imported)


def _depends_on(filename: str, module: str) -> bool:
    return _source_depends_on((SERVICES_ROOT / filename).read_text(encoding="utf-8"), module)


def test_removed_service_cycles_do_not_regress() -> None:
    assert not _depends_on("quality_classifier.py", "novel_system.services.qc_engine")
    assert not _depends_on("final_text_gate.py", "novel_system.services.qc_engine")


def test_dependency_check_sees_submodule_and_parent_package_imports() -> None:
    """qc_engine 拆成包之后，``from …qc_engine.issues import …`` 与 ``from novel_system.services import qc_engine``
    都是对它的依赖；只比整个模块名的旧写法两样都看不见，守卫就形同虚设。"""
    module = "novel_system.services.qc_engine"
    assert _source_depends_on("from novel_system.services.qc_engine import HardQcEngine\n", module)
    assert _source_depends_on("from novel_system.services.qc_engine.issues import _dedupe_issues\n", module)
    assert _source_depends_on("def f():\n    from novel_system.services import qc_engine\n", module)
    assert _source_depends_on("import novel_system.services.qc_engine.base\n", module)
    assert not _source_depends_on("from novel_system.services.qc_constraints import forbidden_hits\n", module)
    assert not _source_depends_on("from novel_system.services import qc_constraints, quality_classifier\n", module)


def test_writer_brief_contract_is_independent_and_preserves_validation() -> None:
    empty = empty_scene_writer_brief()
    assert empty["schema_version"] == "writer_brief_v2"
    assert writer_brief_has_content(empty) is False

    normalized = normalize_scene_writer_brief({"character_desire": 42, "obstacle": None})
    assert normalized["character_desire"] == "42"
    assert normalized["obstacle"] == ""
    assert writer_brief_has_content(normalized) is True

    with pytest.raises(DomainError) as error:
        normalize_scene_writer_brief({"character_desire": ["not", "scalar"]})
    assert error.value.code == "WRITER_BRIEF_INVALID"

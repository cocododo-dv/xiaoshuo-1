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


def _imported_modules(filename: str) -> set[str]:
    tree = ast.parse((SERVICES_ROOT / filename).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_removed_service_cycles_do_not_regress() -> None:
    assert "novel_system.services.writer_review" not in _imported_modules("bundle_builder.py")
    assert "novel_system.services.qc_engine" not in _imported_modules("quality_classifier.py")
    assert "novel_system.services.qc_engine" not in _imported_modules("final_text_gate.py")


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

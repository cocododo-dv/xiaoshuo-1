"""Helper modules shared by several test files. Nothing here is collected, and nothing here imports a ``test_*.py``.

pytest rewrites ``assert`` only in test modules and conftest; a helper module keeps assertion introspection only
when it is registered before its first import, so every module of this package is listed here
(``tests/test_suite_layout.py`` checks that the list is complete).
"""

import pytest

pytest.register_assert_rewrite(
    "tests.support.accounting",
    "tests.support.api_client",
    "tests.support.candidate_gate",
    "tests.support.catalog",
    "tests.support.chaptering",
    "tests.support.checkpoint_fakes",
    "tests.support.checkpoint_golden",
    "tests.support.diagnosis",
    "tests.support.fixtures",
    "tests.support.import_graph",
    "tests.support.llm_fakes",
    "tests.support.migrations",
    "tests.support.qc",
    "tests.support.scene_pipeline",
    "tests.support.schema",
    "tests.support.snowflake",
    "tests.support.strict_qc",
    "tests.support.style_first_fixtures",
    "tests.support.style_gate",
    "tests.support.style_reference",
)

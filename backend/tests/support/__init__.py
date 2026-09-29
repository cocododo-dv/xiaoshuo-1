"""Helper modules shared by several test files. Nothing here is collected, and nothing here imports a ``test_*.py``.

pytest rewrites ``assert`` only in test modules and conftest; a helper module keeps assertion introspection only
when it is registered before its first import, so every module of this package is listed here.
"""

import pytest

pytest.register_assert_rewrite("tests.support.checkpoint_fakes")

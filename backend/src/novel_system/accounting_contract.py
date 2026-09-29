"""兼容转出：记账的冻结默认值住在 ``env_config``（B09-05）。

还从这里 import 的：``db/models.py``（P09b）、``services/scene_budget.py``（P01a 在改）与两个测试文件。
它们改成 ``from novel_system.env_config import …`` 之后删掉本文件。
"""

from novel_system.env_config import DEFAULT_PROVIDER_ATTEMPT_BUDGET, PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY

__all__ = ["DEFAULT_PROVIDER_ATTEMPT_BUDGET", "PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY"]

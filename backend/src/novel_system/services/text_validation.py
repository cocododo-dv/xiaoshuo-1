"""旧路径的转手壳（B04-32）：正文输入校验的家是 ``services/text_input.py``。

还有从这里导入的调用点——v1 章接口（路由这一侧另一个重构包正把它挪进服务，那边也从这里导入）。它们都改从
``services.text_input`` 导入之后删掉本文件；新代码不要再引这里。
"""

from novel_system.services.text_input import clean_backfill_markers, validate_user_text_payload

__all__ = ["clean_backfill_markers", "validate_user_text_payload"]

"""Style Reference 路由(prefix: /api/v2/style-reference),按领域拆成几个子模块,在这里合成一个 ``router``。

- ``books``:导入(上传 / 服务器路径)、书库列表与详情、段落原文、删除、(重新)分类与取消、运行时默认值;
- ``learn``:学习文风作业(建 / 续 / 取消 / 摘要与估算)、抽取 run 与发现(文风卡行的血缘);
- ``profiles``:画像列表与详情、文风卡行 ✓ / ✗、禁用词、注入预览;
- ``bindings``:应用画像、绑定的列出与删除、叠层只读视图;
- ``activity``:参考书活动清单。

不含公开 inject 写接口:注入契约是注入预览端点返回的 ``SystemPromptFragments`` / 起草提示本身。
测试在包上打桩运行时模型客户端(``_get_llm_client_and_enabled``)与作业派发(``dispatch_job``);子模块经
``_common.llm_client_and_enabled`` / ``_common.dispatch`` 回到包上取。
"""

from __future__ import annotations

from fastapi import APIRouter

from novel_system.api.routes.style_reference import activity, bindings, books, learn, profiles
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    _get_llm_client_and_enabled,
)
from novel_system.api.routes.style_reference.bindings import ApplyConfigMixin, ApplyProfileRequest
from novel_system.services.style_reference.jobs import dispatch_job

router = APIRouter()
for _module in (books, activity, learn, profiles, bindings):
    router.include_router(_module.router)

__all__ = [
    "ApplyConfigMixin",
    "ApplyProfileRequest",
    "PATH_PREFIX",
    "ROUTE_TAGS",
    "_get_llm_client_and_enabled",
    "activity",
    "bindings",
    "books",
    "dispatch_job",
    "learn",
    "profiles",
    "router",
]

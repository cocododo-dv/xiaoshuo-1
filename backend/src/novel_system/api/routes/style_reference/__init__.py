"""Style Reference 路由(prefix: /api/v2/style-reference),按领域拆成几个子模块,在这里合成一个 ``router``。

- ``books``:导入(上传 / 服务器路径)、书库列表(摘要)与详情、段落原文、删除(单本 / 批量)、(重新)分类与取消、
  运行时默认值;
- ``learn``:学习文风作业(建 / 续 / 取消 / 摘要与估算)、抽取 run 与发现(文风卡行的血缘);
- ``profiles``:画像摘要与文风画像详情、文风卡行 ✓ / ✗、禁用词、本场预览(注入预览 dryrun);
- ``bindings``:直接绑定(用于作品)、改配置、解除、一部作品现在用的是哪一份、叠层只读视图;
- ``activity``:参考书活动清单(作业表)。

不含公开 inject 写接口:注入契约是本场预览端点返回的块与起草提示本身。
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
from novel_system.api.routes.style_reference.bindings import ApplyProfileRequest, BindingConfigBody
from novel_system.services.style_reference.jobs import dispatch_job

router = APIRouter()
for _module in (books, activity, learn, profiles, bindings):
    router.include_router(_module.router)

__all__ = [
    "ApplyProfileRequest",
    "BindingConfigBody",
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

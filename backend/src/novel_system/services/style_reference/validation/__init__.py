"""风格参考 v3（2026-09-23，P5b）：旧校验层（「回测」）已删除，只留两样东西。

- :func:`check_plagiarism`（``validation/plagiarism.py``）：规范化 n-gram 重合检测，唯一抄袭门
  （``services/reference_copy_gate``）、学习作业的原文重合过滤、自我重复守卫都用它的规范化与判定口径；
- 退役桩 :class:`ValidationOrchestrator` / :func:`start_style_reference_validation_worker`：旧的
  ``POST /api/v2/style-reference/profiles/{id}/validate`` 路由（在 ``api/routes/style_reference.py``，由 P6a / P7 删除）
  还 import 它们——调用即 410 ``STYLE_REFERENCE_VALIDATION_RETIRED``，指向「对照检查」
  ``POST /api/v2/style-reference/checks``（读数 + 参考评审 + 抄袭门，作业表 kind=check，``check_job.py``）。

删掉的：量化回测（只作诊断、分不清两位作者）、语义回测与禁忌模式语义判定（两个 LLM 节点，从未在产品里跑过、
失败被吞）、同步裁决、后台执行器与启动恢复。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.errors import DomainError
from novel_system.services.style_reference.validation.plagiarism import check_plagiarism

VALIDATION_RETIRED_CODE = "STYLE_REFERENCE_VALIDATION_RETIRED"
CHECKS_ENDPOINT = "/api/v2/style-reference/checks"


def validation_retired_error() -> DomainError:
    return DomainError(
        VALIDATION_RETIRED_CODE,
        "旧的「回测」已下线：请改用「对照检查」（读数 + 参考评审 + 抄袭门）。",
        status_code=410,
        details={
            "replacement": CHECKS_ENDPOINT,
            "author_action": {
                "action": "run_style_check",
                "view": "styleref",
                "label": "改用「对照检查」",
            },
        },
    )


class ValidationOrchestrator:
    """退役桩：旧回测路由构造它、调 ``validate`` 即 410（见模块说明）。"""

    def __init__(self, session: Any = None, **_kwargs: Any) -> None:
        self.session = session

    def validate(self, *_args: Any, **_kwargs: Any) -> Any:
        raise validation_retired_error()


def start_style_reference_validation_worker(**_kwargs: Any) -> None:
    """退役桩：旧回测的后台执行器已删除。"""
    raise validation_retired_error()


__all__ = [
    "CHECKS_ENDPOINT",
    "VALIDATION_RETIRED_CODE",
    "ValidationOrchestrator",
    "check_plagiarism",
    "start_style_reference_validation_worker",
    "validation_retired_error",
]

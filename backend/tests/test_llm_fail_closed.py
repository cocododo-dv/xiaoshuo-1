"""LLM 节点失败的唯一一份领域错误翻译（审计 B09-24）：能力缺失 409、上游失败 502、记账拒绝 409。"""

from __future__ import annotations

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_client import LLMClientError, LLMConfigurationError
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import LLMNodeExecutionError

CODES = {
    "capability_code": "PROBE_LLM_REQUIRED",
    "failure_code": "PROBE_LLM_FAILED",
    "operation": "探测",
    "node_id": "probe_node",
    "next_action": "configure_probe_route_and_retry",
}


def _error(error_code: str, original: Exception | None = None, *, retryable: bool = False) -> LLMNodeExecutionError:
    return LLMNodeExecutionError(
        llm_call_id="llm_1",
        error_code=error_code,
        message="boom",
        request_summary={},
        response_summary={"status": "x"},
        original_error=original,
        retryable=retryable,
    )


def _translate(exc: LLMNodeExecutionError, **extra) -> DomainError:
    with pytest.raises(DomainError) as caught:
        raise_llm_domain_error(exc, **CODES, **extra)
    assert caught.value.__cause__ is exc
    return caught.value


@pytest.mark.parametrize(
    "exc",
    [
        _error("LLM_ROUTE_NOT_CONFIGURED"),
        _error("SOMETHING", LLMConfigurationError("LLM_MODEL_CONFIG_INVALID", "bad")),
    ],
)
def test_missing_capability_is_409_with_the_callers_capability_code(exc) -> None:
    error = _translate(exc)
    assert (error.code, error.status_code) == ("PROBE_LLM_REQUIRED", 409)
    assert error.details["next_action"] == "configure_probe_route_and_retry"


def test_upstream_failure_is_502_with_the_callers_failure_code() -> None:
    error = _translate(_error("LLM_TIMEOUT", LLMClientError("LLM_TIMEOUT", "slow"), retryable=True))
    assert (error.code, error.status_code) == ("PROBE_LLM_FAILED", 502)
    assert error.details == {
        "llm_call_id": "llm_1",
        "node_id": "probe_node",
        "error_code": "LLM_TIMEOUT",
        "retryable": True,
        "next_action": "configure_probe_route_and_retry",
        "response_summary": {"status": "x"},
    }


def test_accounting_rejection_keeps_its_own_code() -> None:
    error = _translate(_error("LLM_SCENE_BUDGET_EXCEEDED", LLMAccountingRejected("LLM_SCENE_BUDGET_EXCEEDED", "full")))
    assert (error.code, error.status_code) == ("LLM_SCENE_BUDGET_EXCEEDED", 409)
    assert error.details["next_action"] == "restore_llm_accounting_capacity_and_retry"


def test_extra_details_are_added_but_cannot_replace_the_standard_keys() -> None:
    error = _translate(_error("LLM_TIMEOUT"), extra_details={"step": "writer_passage_review", "node_id": "spoofed"})
    assert error.details["step"] == "writer_passage_review"
    assert error.details["node_id"] == "probe_node"

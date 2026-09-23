"""风格参考控制面边界：记账 / 用量不变式失败原样上抛，不包装、不重试、不降级。

（2026-09-23 风格参考 v3 P5b：旧回测工人的四条同类边界测试随校验层删除；对照检查作业的同类边界见
``tests/test_style_fidelity_pipeline_v3.py::test_check_job_keeps_control_plane_failures_distinct``。）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.style_reference import segmentation
from novel_system.services.style_reference.segmentation import llm as segmentation_llm


def _anchor_runtime() -> segmentation_llm.NodeRuntime:
    return segmentation_llm.load_classification_runtimes()[segmentation_llm.NODE_ANCHOR]


def test_segmentation_accounted_execution_preserves_control_plane_exception(
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = LLMAccountingError(
        "LLM_ACCOUNTING_CALL_EXISTS",
        "logical call already exists",
    )

    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(segmentation_llm, "execute_accounted_call", raise_error)

    with pytest.raises(LLMAccountingError) as exc_info:
        segmentation_llm.classify_batch(
            _anchor_runtime(),
            [0],
            ["text"],
            [0],
            object(),
            session=session,
            scope_id="sr_book_control_plane",
            step="paragraph_classification:anchor_strong:0:1",
        )

    assert exc_info.value is error


@pytest.mark.parametrize(
    "error",
    [
        segmentation_llm.SegmentationLLMError(
            "LLM_USAGE_EXCEEDS_RESERVATION",
            "usage settlement invariant failed",
        ),
        LLMAccountingError(
            "LLM_ACCOUNTING_CALL_EXISTS",
            "logical call already exists",
        ),
    ],
    ids=("segmentation-error", "accounting-error"),
)
def test_segmentation_never_uses_heuristic_for_control_plane_failure(
    session,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """2026-09-15 严格 LLM:控制面失败原样上抛(作业记失败),绝不落到启发式。"""
    heuristic_calls = 0

    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    def forbidden_heuristic(*_args: Any, **_kwargs: Any) -> None:
        nonlocal heuristic_calls
        heuristic_calls += 1
        raise AssertionError("LLM failures must not use the heuristic fallback")

    monkeypatch.setattr(segmentation_llm, "execute_accounted_call", raise_error)
    monkeypatch.setattr(segmentation, "classify_heuristic", forbidden_heuristic)

    with pytest.raises(type(error)) as exc_info:
        segmentation_llm.classify_batch(
            _anchor_runtime(),
            [0],
            ["text"],
            [0],
            object(),
            session=session,
            scope_id="sr_book_control_plane",
            step="paragraph_classification:anchor_strong:0:1",
        )

    # 两者都是控制面失败(用量越过预留 / 逻辑调用已存在):原样上抛,不包装、不重试、不降级
    assert exc_info.value is error
    assert heuristic_calls == 0


def test_segmentation_delivers_reasoning_inflated_usage_when_no_fence_is_armed(session) -> None:
    """2026-09-15 真实回归：参考书导入的分类节点路由到 gemini-*-high 类中转，中转把思考
    token 计入 completion_tokens 且不受 max_tokens 封顶（2000 上限的分类应答回报 11776 个
    completion token），预留额被超出——过去整本导入在这一批 500。没有任何配额栅栏武装时，
    分类结果必须照常交付，账本按真实用量落账并记下超出量。"""
    from novel_system.db.models import LlmCall, LlmCallAttempt
    from novel_system.services.llm_client import LLMResponse, OnlineAccountedExecution

    class ThinkingRelayClient(OnlineAccountedExecution):
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def generate_accounted(self, request, *, accounting_hook):
            self.requests.append(request)
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            structured = {
                "classifications": [
                    {"paragraph_index": 0, "paragraph_type": "dialogue", "confidence": "high"},
                    {"paragraph_index": 1, "paragraph_type": "narration", "confidence": "medium"},
                ]
            }
            # 预留额 = 请求 UTF-8 上界 + 8192 输出预算;中转回报的完成 token 远超于此
            usage = {"prompt_tokens": 2244, "completion_tokens": 31776, "total_tokens": 34020}
            response = LLMResponse(
                request_id="thinking-relay",
                provider="openai",
                model=request.model,
                text=json.dumps(structured, ensure_ascii=False),
                structured_output=structured,
                response_format="json_object",
                raw_response={},
                usage=dict(usage),
                raw_usage=dict(usage),
                usage_present=True,
                usage_complete=True,
            )
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
            return response

    client = ThinkingRelayClient()
    runtime = segmentation_llm.load_classification_runtimes()[segmentation_llm.NODE_BULK]
    result = segmentation_llm.classify_batch(
        runtime,
        [0, 1],
        ["「你来了。」", "他没有回答，只是把门关上。"],
        [0, 1],
        client,
        session=session,
        scope_id="sr_book_thinking_relay",
        step="paragraph_classification:rest:0:2",
    )

    assert result == {0: ("dialogue", 0.9), 1: ("narration", 0.6)}
    # 2026-09-23 v3:分类节点默认关推理、输出预算按 ≤100 段一批给到 8192
    assert client.requests[0].max_output_tokens == 8192
    assert client.requests[0].reasoning_level == "off"
    parent = session.query(LlmCall).one()
    attempt = session.query(LlmCallAttempt).one()
    assert parent.accounting_status == attempt.accounting_status == "settled"
    assert parent.total_tokens == 34020
    assert attempt.total_tokens > attempt.reserved_tokens
    assert parent.response_payload_summary["usage_overage_tokens"] == (
        attempt.total_tokens - attempt.reserved_tokens
    )

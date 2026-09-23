from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceProfile,
    StyleReferenceRun,
    StyleReferenceValidationReport,
)
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.style_reference import segmentation
from novel_system.services.style_reference._llm_helper import LLMNodeError
from novel_system.services.style_reference.segmentation import llm as segmentation_llm
from novel_system.services.style_reference.validation import runner
from novel_system.services.style_reference.validation import core as validation_core
from novel_system.services.style_reference.validation import forbidden_local
from novel_system.services.style_reference.validation import forbidden_semantic
from novel_system.services.style_reference.validation import plagiarism
from novel_system.services.style_reference.validation import quantitative
from novel_system.services.style_reference.validation import semantic as validation_semantic
from novel_system.services.style_reference import policy


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


def _seed_durable_validation_report(session, seed: str) -> str:
    book_id = f"sr_book_cp_{seed}"
    run_id = f"sr_run_cp_{seed}"
    profile_id = f"sr_profile_cp_{seed}"
    report_id = f"sr_report_cp_{seed}"
    session.add(
        StyleReferenceBook(
            book_id=book_id,
            title="control plane",
            source_kind="upload",
            cloud_policy="segments_only",
            text_checksum=f"checksum_cp_{seed}",
        )
    )
    session.add(
        StyleReferenceRun(
            run_id=run_id,
            book_id=book_id,
            status="done",
            phase="done",
        )
    )
    session.add(
        StyleReferenceProfile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="control plane",
        )
    )
    session.add(
        StyleReferenceValidationReport(
            report_id=report_id,
            profile_id=profile_id,
            target_kind="manual",
            verdict="",
            status="queued",
            mode_executed="async_full",
        )
    )
    session.commit()
    return report_id


@pytest.mark.parametrize(
    "boundary",
    ["semantic", "forbidden", "outer"],
)
def test_validation_worker_persists_control_plane_failure_at_every_boundary(
    session,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    error: Exception
    if boundary == "semantic":
        error = LLMAccountingError(
            "LLM_ACCOUNTING_CALL_EXISTS",
            "logical call already exists",
        )
    else:
        class UsageInvariantError(RuntimeError):
            code = "LLM_USAGE_EXCEEDS_RESERVATION"

        error = UsageInvariantError("usage settlement invariant failed")

    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        raise error

    semantic = raise_error if boundary == "semantic" else (lambda *_args, **_kwargs: [])
    forbidden = raise_error if boundary == "forbidden" else (lambda *_args, **_kwargs: [])
    corpus = raise_error if boundary == "outer" else None
    seed = f"{boundary}"
    report_id = _seed_durable_validation_report(session, seed)
    profile_id = f"sr_profile_cp_{seed}"
    monkeypatch.setattr(
        validation_core,
        "_load_plagiarism_corpus",
        corpus if corpus is not None else (lambda *_args: []),
    )
    monkeypatch.setattr(
        plagiarism,
        "check_plagiarism",
        lambda *_args: SimpleNamespace(passed=True, model_dump=lambda: {"passed": True}),
    )
    monkeypatch.setattr(quantitative, "check_quantitative", lambda *_args: [])
    monkeypatch.setattr(validation_semantic, "check_semantic", semantic)
    monkeypatch.setattr(forbidden_semantic, "check_forbidden_semantic", forbidden)
    monkeypatch.setattr(
        validation_core,
        "_compute_full_verdict",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("control-plane failure must not be degraded")
        ),
    )
    monkeypatch.setattr(forbidden_local, "check_forbidden_local", lambda *_args: [])
    monkeypatch.setattr(policy, "cloud_llm_allowed", lambda _book: True)

    runner._async_worker(
        report_id=report_id,
        profile_id=profile_id,
        generated_text="generated",
        llm_client=object(),
        llm_enabled=True,
    )

    session.expire_all()
    row = session.get(StyleReferenceValidationReport, report_id)
    assert row is not None
    assert row.verdict == "fail"
    assert row.status == "failed"
    assert row.error_code == "STYLE_REFERENCE_VALIDATION_CONTROL_PLANE_FAILED"
    assert row.retryable is True
    assert row.quantitative_json == []
    assert row.semantic_json == []


def test_validation_worker_fails_the_report_on_explicit_provider_failure(
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-15 严格 LLM:critic 调用失败就是报告失败(status=failed + 可重试),不再降成 partial。"""
    degraded: list[bool] = []

    def provider_failure(*_args: Any, **_kwargs: Any) -> None:
        raise LLMNodeError("provider failed", error_code="RuntimeError")

    def compute_verdict(**kwargs: Any) -> SimpleNamespace:
        degraded.append(kwargs["semantic_degraded"])
        return SimpleNamespace(value="partial")

    report_id = _seed_durable_validation_report(session, "provider_failure")
    profile_id = "sr_profile_cp_provider_failure"
    monkeypatch.setattr(validation_core, "_load_plagiarism_corpus", lambda *_args: [])
    monkeypatch.setattr(
        plagiarism,
        "check_plagiarism",
        lambda *_args: SimpleNamespace(passed=True, model_dump=lambda: {"passed": True}),
    )
    monkeypatch.setattr(quantitative, "check_quantitative", lambda *_args: [])
    monkeypatch.setattr(validation_semantic, "check_semantic", provider_failure)
    monkeypatch.setattr(forbidden_semantic, "check_forbidden_semantic", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(validation_core, "_compute_full_verdict", compute_verdict)
    monkeypatch.setattr(forbidden_local, "check_forbidden_local", lambda *_args: [])
    monkeypatch.setattr(policy, "cloud_llm_allowed", lambda _book: True)

    runner._async_worker(
        report_id=report_id,
        profile_id=profile_id,
        generated_text="generated",
        llm_client=object(),
        llm_enabled=True,
    )

    assert degraded == []  # 没有走到结论计算:报告直接失败
    session.expire_all()
    row = session.get(StyleReferenceValidationReport, report_id)
    assert row is not None
    assert row.status == "failed"
    assert row.verdict == "fail"
    assert row.error_code == "STYLE_REFERENCE_VALIDATION_FAILED"
    assert row.retryable is True


def test_async_full_worker_without_llm_fails_the_report(
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-15 严格 LLM:worker 只在 validate() 确认 LLM 可用后才会派发;没有 LLM 的 worker
    是状态错误,报告直接 failed(STYLE_REFERENCE_LLM_REQUIRED),不再降成 partial。"""
    report_id = _seed_durable_validation_report(session, "semantic_unavailable")
    profile_id = "sr_profile_cp_semantic_unavailable"
    monkeypatch.setattr(validation_core, "_load_plagiarism_corpus", lambda *_args: [])
    monkeypatch.setattr(
        plagiarism,
        "check_plagiarism",
        lambda *_args: SimpleNamespace(passed=True, model_dump=lambda: {"passed": True}),
    )
    monkeypatch.setattr(quantitative, "check_quantitative", lambda *_args: [])
    monkeypatch.setattr(forbidden_local, "check_forbidden_local", lambda *_args: [])
    monkeypatch.setattr(policy, "cloud_llm_allowed", lambda _book: True)

    runner._async_worker(
        report_id=report_id,
        profile_id=profile_id,
        generated_text="generated",
        llm_client=None,
        llm_enabled=False,
    )

    session.expire_all()
    row = session.get(StyleReferenceValidationReport, report_id)
    assert row is not None
    assert row.status == "failed"
    assert row.verdict == "fail"
    assert row.error_code == "STYLE_REFERENCE_LLM_REQUIRED"
    assert row.retryable is True


@pytest.mark.parametrize(
    ("llm_enabled", "llm_client", "expected_semantic_calls"),
    [
        (True, object(), 1),
    ],
    ids=("semantic-empty-result",),
)
def test_async_full_without_semantic_evidence_cannot_report_full_pass(
    session,
    monkeypatch: pytest.MonkeyPatch,
    llm_enabled: bool,
    llm_client: object | None,
    expected_semantic_calls: int,
) -> None:
    degraded: list[bool] = []
    semantic_calls = 0

    def empty_semantic(*_args: Any, **_kwargs: Any) -> list:
        nonlocal semantic_calls
        semantic_calls += 1
        return []

    def compute_verdict(**kwargs: Any) -> SimpleNamespace:
        degraded.append(kwargs["semantic_degraded"])
        return SimpleNamespace(value="partial" if kwargs["semantic_degraded"] else "pass")

    seed = "semantic_unavailable" if not llm_enabled else "semantic_empty"
    report_id = _seed_durable_validation_report(session, seed)
    profile_id = f"sr_profile_cp_{seed}"
    monkeypatch.setattr(validation_core, "_load_plagiarism_corpus", lambda *_args: [])
    monkeypatch.setattr(
        plagiarism,
        "check_plagiarism",
        lambda *_args: SimpleNamespace(passed=True, model_dump=lambda: {"passed": True}),
    )
    monkeypatch.setattr(quantitative, "check_quantitative", lambda *_args: [])
    monkeypatch.setattr(validation_semantic, "check_semantic", empty_semantic)
    monkeypatch.setattr(forbidden_semantic, "check_forbidden_semantic", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(validation_core, "_compute_full_verdict", compute_verdict)
    monkeypatch.setattr(forbidden_local, "check_forbidden_local", lambda *_args: [])
    monkeypatch.setattr(policy, "cloud_llm_allowed", lambda _book: True)

    runner._async_worker(
        report_id=report_id,
        profile_id=profile_id,
        generated_text="generated",
        llm_client=llm_client,
        llm_enabled=llm_enabled,
    )

    assert semantic_calls == expected_semantic_calls
    assert degraded == [True]
    session.expire_all()
    row = session.get(StyleReferenceValidationReport, report_id)
    assert row is not None
    assert row.verdict == "partial"
    assert row.status == "completed"


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

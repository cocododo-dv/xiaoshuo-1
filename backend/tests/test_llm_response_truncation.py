"""被 max_tokens 砍断的结构化输出必须响亮失败，不能静默救援成错答。

真实故障：场景规划生成 finish_reason=length、error_code 为空被当成功，救援解析从
半截 JSON 里捞出第一个场景对象当整份结果，于是接口报成功、草稿却原地不动。
"""
from __future__ import annotations

import json

import pytest

from novel_system.services.llm_client import (
    MAX_OUTPUT_TOKENS_CEILING,
    LLMRequest,
    LLMResponseError,
    _loads_json_object_text,
)
from novel_system.services.llm_degrade import degrade_request_after_failure


def _scene_payload(count: int) -> dict:
    return {"scenes": [
        {"scene_id": f"SC{i:03d}", "title": f"标题{i}", "summary": f"第{i}场的完整内容",
         "goal": "目标", "conflict": "冲突", "setback": "挫折"}
        for i in range(1, count + 1)]}


@pytest.mark.parametrize("cut", [0.1, 0.3, 0.6, 0.95])
def test_truncated_json_raises_instead_of_salvaging_an_inner_object(cut):
    """半截 JSON 的内层成员自身是配平的——绝不能把数组的第一个成员当整份结果。"""
    text = json.dumps(_scene_payload(20), ensure_ascii=False)
    truncated = text[: int(len(text) * cut)]

    with pytest.raises(json.JSONDecodeError):
        _loads_json_object_text(truncated)


def test_markdown_fenced_json_still_parses():
    """救援解析本来的用途（围栏/前后缀）不能被回归打坏。"""
    payload = _scene_payload(3)
    text = "这是结果：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n以上。"
    assert _loads_json_object_text(text) == payload


def test_prose_containing_braces_before_the_payload_still_parses():
    """回归：前缀说明里带一对花括号，不该把整份可用回复判成坏 JSON。

    模型很常见地写「按 {step_key} 的要求，输出如下：」再给 JSON。曾经的救援规则是
    「只看正文第一个 `{`」，于是只考察 `{step_key}` —— 它配平但不是 JSON，救援当场
    放弃、原始 JSONDecodeError 被重抛，接着白烧一整轮 INVALID_JSON 重试预算。
    """
    payload = _scene_payload(3)
    text = "按 {step_key} 的要求，输出如下：\n" + json.dumps(payload, ensure_ascii=False)
    assert _loads_json_object_text(text) == payload


def test_a_truncated_payload_behind_prose_braces_still_refuses_to_salvage():
    """放宽到「所有顶层配平片段」不能把防截断那条红线一起放掉。

    前缀花括号 + 被砍断的载荷：唯一的顶层候选是那个占位符（不是 JSON），被砍断的
    载荷始终没闭合，内层的场景对象深度≥1 进不了候选 —— 必须照样抛错。
    """
    text = "按 {step_key} 的要求：\n" + json.dumps(_scene_payload(20), ensure_ascii=False)[:400]
    with pytest.raises(json.JSONDecodeError):
        _loads_json_object_text(text)


def test_braces_inside_json_strings_do_not_confuse_the_scanner():
    """正文字符串里的 `{` / `"` 不能把深度算歪（否则会在半路收出一个假候选）。"""
    payload = {"note": 'he said "{" and then "}"', "scenes": []}
    text = "结果：\n" + json.dumps(payload, ensure_ascii=False) + "\n完。"
    assert _loads_json_object_text(text) == payload


def _request(max_output_tokens: int) -> LLMRequest:
    return LLMRequest(
        model="m", messages=[{"role": "user", "content": "hi"}], temperature=0.2,
        max_output_tokens=max_output_tokens, response_format="json_object", provider="openai_compatible",
    )


@pytest.mark.parametrize("provider_type", ["openai_compatible", "openai"])
def test_truncation_degrades_by_doubling_the_output_budget(provider_type):
    from novel_system.services.system_config import ProviderRuntimeConfig

    config = ProviderRuntimeConfig(
        provider_id="p", provider_type=provider_type, base_url="http://x", api_key="k",
    )
    exc = LLMResponseError("LLM_RESPONSE_TRUNCATED", "truncated")

    degraded = degrade_request_after_failure(_request(3200), exc, config)
    assert degraded is not None, "截断必须能通过抬高输出预算重试"
    request, reason = degraded
    assert request.max_output_tokens == 6400
    assert "6400" in reason


def test_truncation_at_the_ceiling_is_not_degradable():
    """已经顶到上限还截断 → 不再重试，如实报错（半截结构化输出没有可用形态）。"""
    from novel_system.services.system_config import ProviderRuntimeConfig

    config = ProviderRuntimeConfig(
        provider_id="p", provider_type="openai_compatible", base_url="http://x", api_key="k",
    )
    exc = LLMResponseError("LLM_RESPONSE_TRUNCATED", "truncated")
    assert degrade_request_after_failure(_request(MAX_OUTPUT_TOKENS_CEILING), exc, config) is None


def test_client_escalates_the_budget_and_succeeds_on_the_retry():
    """端到端：首答被砍断 → 抬预算重试 → 拿到完整 JSON。作者看到的是成功，不是错答。"""
    import httpx

    from novel_system.services.llm_client import LLMClient

    seen_budgets: list[int] = []
    complete = _scene_payload(12)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        budget = body.get("max_output_tokens") or body.get("max_tokens")
        seen_budgets.append(budget)
        if len(seen_budgets) == 1:
            text = json.dumps(complete, ensure_ascii=False)[:400]  # 被 max_tokens 砍断
            return httpx.Response(200, json={"id": "r1", "model": "m", "output_text": text,
                                             "finish_reason": "length",
                                             "usage": {"input_tokens": 5, "output_tokens": budget}})
        return httpx.Response(200, json={"id": "r2", "model": "m",
                                         "output_text": json.dumps(complete, ensure_ascii=False),
                                         "finish_reason": "stop",
                                         "usage": {"input_tokens": 5, "output_tokens": 900}})

    client = LLMClient(provider="openai_compatible", base_url="https://example.test/v1",
                       api_key="k", timeout_seconds=12, transport=httpx.MockTransport(handler))
    response = client.generate(_request(3200))

    assert seen_budgets == [3200, 6400], f"没有按截断抬高输出预算：{seen_budgets}"
    assert response.structured_output == complete
    assert len(response.structured_output["scenes"]) == 12


def test_responses_api_incomplete_status_is_detected_as_truncation():
    """真实 Responses API 的截断信号是 status=incomplete + incomplete_details.reason，

    顶层没有 finish_reason。snowflake_step_generate 默认 api_mode=responses，
    若只看顶层 finish_reason 就永远抓不到截断——这是本修复的主目标节点。
    """
    from novel_system.services.llm_providers import get_adapter

    adapter = get_adapter("openai_compatible")
    incomplete_body = {
        "id": "r", "model": "m", "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": '{"scenes": [{"scene_id": "SC001"',  # 被砍断的半截 JSON
    }
    reason = adapter.extract_finish_reason(incomplete_body, api_mode="responses")
    assert reason == "max_output_tokens", f"Responses 截断信号没被归一：{reason!r}"

    from novel_system.services.llm_client import TRUNCATED_FINISH_REASONS

    assert str(reason).lower() in TRUNCATED_FINISH_REASONS, "归一后的截断信号没被截断判定覆盖"

    # 完成态（status=completed）不该被误判为截断
    ok_body = {"id": "r", "model": "m", "status": "completed", "output_text": "{}"}
    assert adapter.extract_finish_reason(ok_body, api_mode="responses") != "max_output_tokens"


def test_client_escalates_budget_for_responses_api_incomplete_truncation():
    """端到端（responses 模式）：首答 status=incomplete 被砍断 → 抬预算重试 → 拿到完整 JSON。"""
    import httpx

    from novel_system.services.llm_client import LLMClient, LLMRequest

    seen_budgets: list[int] = []
    complete = _scene_payload(10)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        budget = body.get("max_output_tokens") or body.get("max_tokens")
        seen_budgets.append(budget)
        if len(seen_budgets) == 1:
            text = json.dumps(complete, ensure_ascii=False)[:300]  # 半截
            return httpx.Response(200, json={
                "id": "r1", "model": "m", "output_text": text,
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": budget}})
        return httpx.Response(200, json={
            "id": "r2", "model": "m", "output_text": json.dumps(complete, ensure_ascii=False),
            "status": "completed", "usage": {"input_tokens": 5, "output_tokens": 900}})

    request = LLMRequest(
        model="m", messages=[{"role": "user", "content": "hi"}], temperature=0.2,
        max_output_tokens=3200, response_format="json_object", provider="openai_compatible",
        api_mode="responses",
    )
    client = LLMClient(provider="openai_compatible", base_url="https://example.test/v1",
                       api_key="k", timeout_seconds=12, transport=httpx.MockTransport(handler))
    response = client.generate(request)

    assert seen_budgets == [3200, 6400], f"responses 截断没抬预算：{seen_budgets}"
    assert response.structured_output == complete


def test_complete_json_that_ends_exactly_at_the_cap_is_not_discarded():
    """finish_reason=length 但 JSON 其实是完整的（正好在上限处收尾）→ 必须当成功返回，

    不能因为 finish_reason 就把好答案误杀（先解析、解析成功即采纳）。
    """
    import httpx

    from novel_system.services.llm_client import LLMClient

    complete = _scene_payload(2)

    def handler(request: httpx.Request) -> httpx.Response:
        # 完整 JSON，但 provider 因输出正好填满上限而报 finish_reason=length
        return httpx.Response(200, json={"id": "r", "model": "m",
                                         "output_text": json.dumps(complete, ensure_ascii=False),
                                         "finish_reason": "length",
                                         "usage": {"input_tokens": 5, "output_tokens": 3200}})

    client = LLMClient(provider="openai_compatible", base_url="https://example.test/v1",
                       api_key="k", timeout_seconds=12, transport=httpx.MockTransport(handler))
    response = client.generate(_request(3200))

    assert response.structured_output == complete, "上限处收尾的完整 JSON 被误当截断丢弃"


def test_client_reports_truncation_when_the_ceiling_cannot_help():
    """顶到上限还截断 → 如实报错，绝不把半截 JSON 救援成"成功"。"""
    import httpx

    from novel_system.services.llm_client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.dumps(_scene_payload(40), ensure_ascii=False)[:900]
        return httpx.Response(200, json={"id": "r", "model": "m", "output_text": text,
                                         "finish_reason": "length",
                                         "usage": {"input_tokens": 5, "output_tokens": 8192}})

    client = LLMClient(provider="openai_compatible", base_url="https://example.test/v1",
                       api_key="k", timeout_seconds=12, transport=httpx.MockTransport(handler))

    with pytest.raises(LLMResponseError) as excinfo:
        client.generate(_request(MAX_OUTPUT_TOKENS_CEILING))
    assert excinfo.value.code == "LLM_RESPONSE_TRUNCATED"

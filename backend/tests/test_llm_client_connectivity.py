"""LLM 连通性加固:api_mode 归一化 + 404/结构化输出降级阶梯 + 超时配置。

回归背景(2026-07-03 风格抽取连环失败):
1) chat-only 中转收到 /responses → 404(任务路由缺省 responses 无视 provider 声明);
2) 中转后端引擎(lightllm/qwen)不支持 json_schema 约束解码 →
   `guided_grammar ... compile_grammar_error`,重试三连击同一错误后 run 失败。
"""

from __future__ import annotations

import json

import httpx
import pytest

from novel_system.services.llm_client import LLMClient, _load_task_model_config
from novel_system.services.llm_providers.base import (
    LLMHTTPError,
    LLMRateLimitError,
    LLMRequest,
    ProviderRuntimeConfig,
)


@pytest.fixture(autouse=True)
def _clear_connectivity_caps():
    from novel_system.services import llm_client as mod

    mod._CONNECTIVITY_CAPS.clear()
    yield
    mod._CONNECTIVITY_CAPS.clear()


def _chat_ok(content: str = '{"ok": true}') -> httpx.Response:
    return httpx.Response(200, json={
        "id": "chat-1",
        "object": "chat.completion",
        "model": "m",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def _request(**kw) -> LLMRequest:
    base = dict(
        model="test-model",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "输出 JSON"}],
        temperature=0.1,
        max_output_tokens=200,
        response_format="json_object",
        provider="openai_compatible",
        api_mode="responses",
        node_id="style_ref_extract_language",
        response_schema={"name": "x", "schema": {"type": "object"}},
    )
    base.update(kw)
    return LLMRequest(**base)


def _client(handler, provider_configs=None, max_retries=2) -> LLMClient:
    return LLMClient(
        provider="openai_compatible",
        base_url="http://mock-relay/v1",
        api_key="k",
        timeout_seconds=5,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        provider_configs=provider_configs,
    )


def test_api_mode_normalized_to_provider_declaration() -> None:
    """provider 声明 chat 时,任务路由缺省的 responses 必须在入口归一化——
    首个请求就打 /chat/completions,不吃 404。"""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.url.path.endswith("/chat/completions")
        return _chat_ok()

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    resp = _client(handler, provider_configs=configs).generate(_request())
    assert resp.structured_output == {"ok": True}
    assert len(calls) == 1


def test_responses_404_degrades_to_chat() -> None:
    """无 provider 声明可依(env 回退)时,/responses 404 → 自动换 chat 重试。"""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return _chat_ok()

    resp = _client(handler).generate(_request())
    assert resp.structured_output == {"ok": True}
    assert [p.rsplit("/", 1)[-1] for p in calls] == ["responses", "chat%2Fcompletions"] or [
        p for p in calls
    ] == ["/v1/responses", "/v1/chat/completions"]


def test_json_schema_rejected_degrades_to_json_object() -> None:
    """引擎级 guided_grammar 错误:立即降级(不重试三连击),json_schema → json_object。"""
    seen_formats: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        fmt = (payload.get("response_format") or {}).get("type", "none")
        seen_formats.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(500, json={"error": {
                "message": "guided_grammar '{...}' has compile_grammar_error: Unsupported tokenizer type",
            }})
        return _chat_ok()

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    resp = _client(handler, provider_configs=configs).generate(_request())
    assert resp.structured_output == {"ok": True}
    # 早退:json_schema 只试 1 次(不按 5xx 重试预算三连击),随后 json_object 成功
    assert seen_formats == ["json_schema", "json_object"]


def test_json_object_also_rejected_degrades_to_prompt_only() -> None:
    """连 json_object 都不支持的中转:第三跳去掉 wire response_format,
    仍按 json_object 解析文本(prompt 已要求 JSON)。"""
    seen_formats: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        if "response_format" in payload:
            seen_formats.append(payload["response_format"].get("type"))
            return httpx.Response(400, json={"error": {
                "message": "Invalid parameter: 'response_format' is not supported with this model.",
            }})
        seen_formats.append("none")
        return _chat_ok('{"data": 1}')

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    resp = _client(handler, provider_configs=configs).generate(_request())
    assert resp.structured_output == {"data": 1}
    assert seen_formats == ["json_schema", "json_object", "none"]


def test_connectivity_caps_cached_across_calls() -> None:
    """降级结论按 (provider_id, model) 进程内缓存:第二次 generate 直接按学到的
    档位发请求,不再重复浪费一跳注定失败的 json_schema 探测。"""
    from novel_system.services import llm_client as mod

    mod._CONNECTIVITY_CAPS.clear()
    seen_formats: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        fmt = (payload.get("response_format") or {}).get("type", "none")
        seen_formats.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema is not supported"}})
        return _chat_ok()

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    client = _client(handler, provider_configs=configs)
    client.generate(_request(model="cap-model"))
    client.generate(_request(model="cap-model"))
    # 第 1 次:schema 探测失败 + json_object 成功;第 2 次:直接 json_object
    assert seen_formats == ["json_schema", "json_object", "json_object"]
    mod._CONNECTIVITY_CAPS.clear()


def test_missing_text_degrades_reasoning_off_and_bigger_budget() -> None:
    """reasoning 模型把 max_tokens 烧在思考上(content 空):不做无脑重试,
    立即降级——去掉 reasoning 参数 + 输出预算×2;结论(关 reasoning)进能力缓存。"""
    from novel_system.services import llm_client as mod

    requests_seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        requests_seen.append(payload)
        if "reasoning" in payload:
            # 思考吃满预算:200 但 content 为空
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "model": "m",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": "",
                                          "reasoning_content": "思考了很久…"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        return _chat_ok()

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    client = _client(handler, provider_configs=configs)
    resp = client.generate(_request(model="think-model", response_schema=None, max_output_tokens=1000))
    assert resp.structured_output == {"ok": True}
    # 2 次调用:空正文不做同参重试;第 2 次无 reasoning 参数、带引擎思考开关、预算翻倍
    assert len(requests_seen) == 2
    assert "reasoning" in requests_seen[0] and "reasoning" not in requests_seen[1]
    assert requests_seen[1]["max_tokens"] == 2000
    assert requests_seen[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert requests_seen[1]["enable_thinking"] is False
    # 缓存:第 3 次调用直接不带 reasoning 且带思考开关
    client.generate(_request(model="think-model", response_schema=None, max_output_tokens=1000))
    assert "reasoning" not in requests_seen[2]
    assert requests_seen[2]["enable_thinking"] is False
    mod._CONNECTIVITY_CAPS.clear()


def test_missing_text_on_strict_openai_does_not_send_unknown_params() -> None:
    """严格官方 API(provider_type=openai)不发 enable_thinking 等未知参数,
    空正文只做关 reasoning + 扩预算两跳。"""
    requests_seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        requests_seen.append(payload)
        if "reasoning" in payload:
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "model": "m",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": ""}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        return _chat_ok()

    configs = {
        "openai_official": ProviderRuntimeConfig(
            provider_id="openai_official",
            provider_type="openai",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    client = LLMClient(
        provider="openai",
        base_url="http://mock-relay/v1",
        api_key="k",
        timeout_seconds=5,
        max_retries=2,
        transport=httpx.MockTransport(handler),
        provider_configs=configs,
    )
    resp = client.generate(_request(
        provider="openai", provider_id="openai_official",
        model="o-model", response_schema=None, max_output_tokens=1000,
    ))
    assert resp.structured_output == {"ok": True}
    for payload in requests_seen:
        assert "enable_thinking" not in payload
        assert "chat_template_kwargs" not in payload


def test_chat_completions_legacy_text_field_is_parsed() -> None:
    """部分中转按 completions 风格把正文放 choices[0].text:不应报 MISSING_TEXT。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "text": '{"legacy": true}'}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    resp = _client(handler).generate(_request(api_mode="chat", response_schema=None))
    assert resp.structured_output == {"legacy": True}


def test_rate_limit_is_not_degraded() -> None:
    """429 是容量问题不是能力问题:不进降级阶梯,按原语义抛 LLMRateLimitError。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited, response_format irrelevant"}})

    with pytest.raises(LLMRateLimitError):
        _client(handler, max_retries=0).generate(_request(api_mode="chat"))


def test_hard_failure_without_signature_is_not_degraded() -> None:
    """无结构化输出特征的 400:原样抛出,不做多余的降级重放。"""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad model name"}})

    with pytest.raises(LLMHTTPError):
        _client(handler, max_retries=0).generate(_request(api_mode="chat", response_schema=None))
    assert len(calls) == 1


def test_task_config_timeout_seconds_parses() -> None:
    cfg = _load_task_model_config("n", {
        "provider": "openai_compatible",
        "model": "m",
        "temperature": 0.1,
        "max_output_tokens": 100,
        "response_format": "json_object",
        "timeout_seconds": 180,
    })
    assert cfg.timeout_seconds == 180.0
    cfg2 = _load_task_model_config("n", {
        "provider": "openai_compatible",
        "model": "m",
        "temperature": 0.1,
        "max_output_tokens": 100,
        "response_format": "json_object",
    })
    assert cfg2.timeout_seconds is None


def test_style_ref_helper_leaves_unrouted_timeout_to_the_client(monkeypatch, session) -> None:
    """style_ref 节点路由未配置超时时不自造上限——重抽取本来就慢,交给 client 全局设置。"""
    from types import SimpleNamespace

    from novel_system.services.style_reference import _llm_helper

    cfg = SimpleNamespace(
        model="m", provider="openai_compatible", provider_id=None, temperature=0.1,
        max_output_tokens=100, response_format="json_object", api_mode="chat",
        reasoning_level="medium", credential_mode=None, account_id=None,
        provider_options={}, timeout_seconds=None,
    )
    routing = SimpleNamespace(task_routing={"n1": cfg}, node_routing={})
    template = SimpleNamespace(system_prompt="s", task_prompt="t", structured_schema=None)
    monkeypatch.setattr(_llm_helper, "load_model_routing_config", lambda: routing)
    monkeypatch.setattr(_llm_helper, "load_prompt_templates", lambda: {"n1": template})

    captured = {}

    from novel_system.services.llm_accounting import LLMCallContext
    from tests.accounted_llm_fakes import AccountedGenerateMixin

    class _C(AccountedGenerateMixin):
        def generate(self, request):
            captured["timeout"] = request.timeout_seconds
            return SimpleNamespace(structured_output={})

    from novel_system.services.style_reference.untrusted_data import UntrustedPayload

    _llm_helper.call_llm_node(
        "n1",
        UntrustedPayload({}),
        _C(),
        session=session,
        context=LLMCallContext(
            scope_type="system",
            scope_id="style_reference_timeout_test",
            node_id="n1",
            step="timeout_floor",
        ),
    )
    assert captured["timeout"] is None


def test_schema_degrade_inlines_schema_into_prompt() -> None:
    """弃 wire json_schema 时必须把 schema 内联进 system prompt——否则模型
    看不到输出形状,自造字段名(实测 statement→description)下游校验全灭。"""

    seen_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen_payloads.append(payload)
        fmt = (payload.get("response_format") or {}).get("type", "none")
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema is not supported"}})
        return _chat_ok()

    configs = {
        "openai_compatible": ProviderRuntimeConfig(
            provider_id="openai_compatible",
            provider_type="openai_compatible",
            base_url="http://mock-relay/v1",
            api_key="k",
            api_mode="chat",
        )
    }
    client = _client(handler, provider_configs=configs)
    schema = {"name": "x", "schema": {"type": "object", "required": ["statement"],
                                      "properties": {"statement": {"type": "string"}}}}
    client.generate(_request(model="inline-model", response_schema=schema))
    # 第 2 次请求(json_object)的 system prompt 里必须能看到 schema 字段名
    sys2 = seen_payloads[1]["messages"][0]["content"]
    assert "JSON Schema" in sys2
    assert '"statement"' in sys2
    # 缓存路径同样内联
    client.generate(_request(model="inline-model", response_schema=schema))
    sys3 = seen_payloads[2]["messages"][0]["content"]
    assert '"statement"' in sys3


# ---------------------------------------------------------------------------
# 2026-09-22 风格参考优先附带的两条连通性修补
# ---------------------------------------------------------------------------


def _responses_ok(text: str = '{"ok": true}', *, output_tokens: int = 1) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "resp-1",
        "object": "response",
        "model": "m",
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1, "output_tokens": output_tokens, "total_tokens": 1 + output_tokens},
    })


def test_thinking_with_forced_tool_rejection_degrades_reasoning_off_and_caches_it() -> None:
    import json as _json

    from novel_system.services import llm_client as mod

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        bodies.append(body)
        if body.get("reasoning"):
            return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": (
                '{"type":"error","error":{"type":"invalid_request_error","message":'
                '"Thinking may not be enabled when tool_choice forces tool use."}}'
            )}})
        return _responses_ok()

    client = _client(handler)
    response = client.generate(_request(reasoning_level="medium"))
    assert response.structured_output == {"ok": True}
    assert [("reasoning" in b) for b in bodies] == [True, False]
    # 连通性缓存记住了 reasoning off:第二次调用直接不带 reasoning
    assert mod._CONNECTIVITY_CAPS[("openai_compatible", "test-model")]["reasoning_level"] == "off"
    client.generate(_request(reasoning_level="medium"))
    assert "reasoning" not in bodies[-1]


def test_malformed_json_at_the_output_ceiling_is_treated_as_truncation() -> None:
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        bodies.append(body)
        cap = int(body.get("max_output_tokens") or 0)
        if cap <= 200:
            # 中转不报 finish_reason,但输出 token 数正好顶到上限——被砍断的 JSON
            return _responses_ok('{"ok": tr', output_tokens=cap)
        return _responses_ok(output_tokens=40)

    client = _client(handler)
    response = client.generate(_request(max_output_tokens=200, reasoning_level="off"))
    assert response.structured_output == {"ok": True}
    # 第一跳 200 顶满 → TRUNCATED → 预算翻倍重试,而不是原样重发同一上限
    assert [b["max_output_tokens"] for b in bodies] == [200, 400]


def test_malformed_json_below_the_ceiling_is_still_invalid_json() -> None:
    from novel_system.services.llm_client import LLMResponseError

    def handler(request: httpx.Request) -> httpx.Response:
        return _responses_ok('{"ok": tr', output_tokens=20)

    client = _client(handler, max_retries=0)
    with pytest.raises(LLMResponseError) as excinfo:
        client.generate(_request(max_output_tokens=200, reasoning_level="off"))
    assert excinfo.value.code == "LLM_RESPONSE_INVALID_JSON"

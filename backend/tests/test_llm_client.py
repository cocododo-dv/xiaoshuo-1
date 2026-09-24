from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from novel_system.settings import get_settings
from novel_system.services import llm_client
from novel_system.services.llm_client import (
    LLM_CONNECT_TIMEOUT_SECONDS,
    LLMClient,
    LLMConfigurationError,
    LLMHTTPError,
    LLMRequest,
    LLMResponseError,
    LLMTimeoutError,
    ProviderRuntimeConfig,
    load_model_routing_config,
    parse_model_routing_config,
    resolve_request_timeout,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_llm_client_generates_structured_json_from_responses_api() -> None:
    captured_request: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "model": "gpt-5-mini",
                "output_text": '{"outline": ["beat-1", "beat-2"]}',
                "finish_reason": "stop",
                "usage": {
                    "input_tokens": 15,
                    "output_tokens": 8,
                    "total_tokens": 23,
                },
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    response = client.generate(
        LLMRequest(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": "Build a short outline."},
            ],
            temperature=0.2,
            max_output_tokens=256,
            response_format="json_object",
        )
    )

    assert captured_request["model"] == "gpt-5-mini"
    assert captured_request["max_output_tokens"] == 256
    assert captured_request["text"] == {"format": {"type": "json_object"}}
    assert response.provider == "openai_compatible"
    assert response.request_id == "resp_123"
    assert response.model == "gpt-5-mini"
    assert response.text == '{"outline": ["beat-1", "beat-2"]}'
    assert response.structured_output == {"outline": ["beat-1", "beat-2"]}
    assert response.usage == {
        "input_tokens": 15,
        "output_tokens": 8,
        "total_tokens": 23,
    }
    assert response.finish_reason == "stop"


def test_llm_client_generates_structured_json_from_chat_completions_api() -> None:
    captured_request: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_123",
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"scene_goal": "advance the argument"}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 9,
                    "total_tokens": 21,
                },
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    response = client.generate(
        LLMRequest(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "Return JSON."}],
            temperature=0.1,
            max_output_tokens=128,
            response_format="json_object",
            api_mode="chat",
        )
    )

    assert captured_request["model"] == "gpt-5-mini"
    assert captured_request["max_tokens"] == 128
    assert captured_request["response_format"] == {"type": "json_object"}
    assert response.request_id == "chatcmpl_123"
    assert response.text == '{"scene_goal": "advance the argument"}'
    assert response.structured_output == {"scene_goal": "advance the argument"}
    assert response.usage == {
        "input_tokens": 12,
        "output_tokens": 9,
        "total_tokens": 21,
    }
    assert response.finish_reason == "stop"


def test_llm_client_responses_404_degrades_to_chat_then_raises() -> None:
    """连通性加固后:/responses 404 先自动降级重试 chat completions(不再一击即抛
    协议提示);两个端点都 404 才最终抛错——此时多半是 base_url 配错。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, json={"detail": "Not Found"})

    client = LLMClient(
        provider="openai",
        base_url="https://example.test/v1",
        api_key="relay-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
        provider_configs={
            "gcli2api": ProviderRuntimeConfig(
                provider_id="gcli2api",
                provider_type="openai",
                base_url="https://example.test/v1",
                api_key="relay-key",
                api_mode="responses",
            )
        },
    )

    with pytest.raises(LLMHTTPError) as error:
        client.generate(
            LLMRequest(
                node_id="snowflake_step_generate",
                provider="openai",
                provider_id="gcli2api",
                model="gemini-3.1-pro-preview",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0.2,
                max_output_tokens=100,
                response_format="json_object",
                api_mode="responses",
            )
        )

    assert error.value.status_code == 404
    # 降级链:先 /responses,404 后自动改打 /chat/completions
    assert calls == ["/v1/responses", "/v1/chat/completions"]


def test_llm_client_retries_http_429_before_succeeding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_retry",
                "model": "gpt-5-mini",
                "output_text": '{"status": "ok"}',
                "finish_reason": "stop",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    response = client.generate(
        LLMRequest(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "Return JSON."}],
            temperature=0,
            max_output_tokens=64,
            response_format="json_object",
        )
    )

    assert attempts == 2
    assert response.request_id == "resp_retry"
    assert response.text == '{"status": "ok"}'
    assert response.structured_output == {"status": "ok"}
    assert response.usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }
    assert response.finish_reason == "stop"
    assert response.attempt_count == 2
    assert response.max_retries == 1
    assert response.retryable is False


def test_llm_client_retries_malformed_json_before_succeeding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_bad_json_once",
                    "model": "local-qwen",
                    "output_text": "I will explain instead of returning JSON.",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_good_json_retry",
                "model": "local-qwen",
                "output_text": '{"scene_text": "ok"}',
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key=None,
        timeout_seconds=12,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    response = client.generate(
        LLMRequest(
            model="local-qwen",
            messages=[{"role": "user", "content": "Return JSON."}],
            temperature=0,
            max_output_tokens=64,
            response_format="json_object",
        )
    )

    assert attempts == 2
    assert response.request_id == "resp_good_json_retry"
    assert response.structured_output == {"scene_text": "ok"}
    assert response.attempt_count == 2
    assert response.max_retries == 1
    assert response.retryable is False


def test_llm_client_raises_normalized_error_for_malformed_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_bad_json",
                "model": "gpt-5-mini",
                "output_text": "definitely not valid json",
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMResponseError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_RESPONSE_INVALID_JSON"


def test_llm_client_extracts_wrapped_json_object_from_local_model_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_wrapped_json",
                "model": "local-qwen",
                "output_text": 'Here is the JSON:\n```json\n{"scene_text": "ok"}\n```',
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key=None,
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    response = client.generate(
        LLMRequest(
            model="local-qwen",
            messages=[{"role": "user", "content": "Return JSON."}],
            temperature=0,
            max_output_tokens=64,
            response_format="json_object",
        )
    )

    assert response.text == 'Here is the JSON:\n```json\n{"scene_text": "ok"}\n```'
    assert response.structured_output == {"scene_text": "ok"}


def test_llm_client_rejects_invalid_runtime_response_format() -> None:
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
    )

    with pytest.raises(LLMConfigurationError, match="unsupported response_format json_typo"):
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_typo",
            )
        )


def test_llm_client_rejects_invalid_runtime_api_mode() -> None:
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
    )

    with pytest.raises(LLMConfigurationError, match="unsupported api_mode invalid_mode"):
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
                api_mode="invalid_mode",  # type: ignore[arg-type]
            )
        )


def test_llm_client_rejects_non_object_json_in_json_object_mode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_array",
                "model": "gpt-5-mini",
                "output_text": '["not", "an", "object"]',
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMResponseError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_RESPONSE_INVALID_JSON_OBJECT"


def test_llm_client_preserves_provider_error_details_for_non_429_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "invalid api key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMHTTPError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_HTTP_FAILURE"
    assert exc_info.value.status_code == 401
    assert exc_info.value.details == {
        "message": "invalid api key",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
        "attempt_count": 1,
        "max_retries": 2,
    }
    assert "invalid api key" in exc_info.value.message


def test_llm_client_preserves_provider_error_details_for_429_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "too many requests",
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                }
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMHTTPError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_RATE_LIMITED"
    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {
        "message": "too many requests",
        "type": "rate_limit_error",
        "code": "rate_limit_exceeded",
        "attempt_count": 1,
        "max_retries": 0,
    }
    assert "too many requests" in exc_info.value.message


def test_llm_client_normalizes_timeout_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMTimeoutError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_REQUEST_TIMEOUT"
    assert exc_info.value.retryable is True
    assert exc_info.value.details["attempt_count"] == 1
    assert exc_info.value.details["max_retries"] == 0


def test_resolve_request_timeout_drops_response_ceiling_but_keeps_connect() -> None:
    """不限时只豁免 read(等模型出字)。写请求体和取连接不存在「合法的慢」。

    三者全设 None 会把「模型在想」的豁免错发给两个纯传输动作:一次写卡住就永久占着
    一个同步 worker 线程,作者重试几次线程池就空了。
    """
    for unbounded_value in (0, 0.0, None, -1):
        timeout = resolve_request_timeout(unbounded_value)
        assert timeout.read is None, unbounded_value
        # 连不上/写不出去/取不到连接必须快速失败,否则填错 base_url 只会无限转圈。
        assert timeout.connect == LLM_CONNECT_TIMEOUT_SECONDS, unbounded_value
        assert timeout.write == LLM_CONNECT_TIMEOUT_SECONDS, unbounded_value
        assert timeout.pool == LLM_CONNECT_TIMEOUT_SECONDS, unbounded_value

    bounded = resolve_request_timeout(45)
    assert bounded.read == 45
    assert bounded.connect == 45


def test_the_default_transport_enables_tcp_keepalive() -> None:
    """read 不限时的代价是半开连接——只有内核探针能把它和「模型在想」区分开。

    中转接了 TCP、随后一个字节都不发、也不回 RST(路由被丢/网关过载):没有 keepalive
    时 ``client.post`` 会永久阻塞。活着的对端一定回 ACK,所以慢生成完全不受影响。
    """
    import socket as _socket

    from novel_system.services.llm_client import _keepalive_socket_options

    options = _keepalive_socket_options()
    assert (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1) in options
    # 平台缺哪个常量跳哪个(Windows 一个都没有),但 SO_KEEPALIVE 必须永远在。
    if hasattr(_socket, "TCP_KEEPIDLE"):
        assert any(option == _socket.TCP_KEEPIDLE for _, option, _ in options)
        assert any(option == _socket.TCP_KEEPCNT for _, option, _ in options)


def test_llm_client_sends_no_response_deadline_when_timeout_disabled() -> None:
    captured_timeout: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_timeout.update(request.extensions["timeout"])
        return httpx.Response(
            200,
            json={
                "id": "resp_slow",
                "model": "gpt-5",
                "output_text": '{"ok": true}',
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    client.generate(
        LLMRequest(
            model="gpt-5",
            messages=[{"role": "user", "content": "Write a long chapter."}],
            temperature=0,
            max_output_tokens=64,
            response_format="json_object",
        )
    )

    # 大任务本来就慢:等模型出字不设上限,只有建连仍然封顶。
    assert captured_timeout["read"] is None
    assert captured_timeout["connect"] == LLM_CONNECT_TIMEOUT_SECONDS


def test_llm_client_timeout_message_names_the_connect_ceiling_when_unbounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMTimeoutError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    message = str(exc_info.value)
    assert "timed out while connecting" in message
    assert "no response ceiling" in message
    # 不限时下绝不能谎报成"响应等了 0 秒就超时"。
    assert "timed out after" not in message


def test_llm_settings_default_to_finite_response_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS", raising=False)

    assert get_settings().llm_timeout_seconds == 900.0


def test_llm_client_normalizes_request_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMHTTPError) as exc_info:
        client.generate(
            LLMRequest(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "Return JSON."}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
            )
        )

    assert exc_info.value.code == "LLM_HTTP_REQUEST_FAILED"
    assert exc_info.value.retryable is True
    assert exc_info.value.details["attempt_count"] == 1
    assert exc_info.value.details["max_retries"] == 0


def test_llm_settings_and_model_routing_config_load_from_env_and_repo_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_API_KEY", "env-test-key")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS", "41")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    settings = get_settings()
    config = load_model_routing_config(_repo_root() / "config" / "models.yaml")

    assert settings.llm_provider == "openai_compatible"
    assert settings.llm_base_url == "https://llm.example.test/v1"
    assert settings.llm_api_key == "env-test-key"
    assert settings.llm_timeout_seconds == 41
    assert settings.llm_enabled is True

    extraction = config.task_routing["extraction"]
    stylize = config.task_routing["stylize"]
    writer_patch = config.task_routing["writer_passage_patch"]
    assert extraction.provider == "openai_compatible"
    assert extraction.response_format == "json_object"
    assert extraction.max_output_tokens > 0
    assert stylize.temperature >= extraction.temperature
    assert writer_patch.response_format == "json_object"
    assert writer_patch.max_output_tokens > 0


def test_llm_settings_raise_clear_error_for_invalid_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError, match="NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS"):
        get_settings()


@pytest.mark.parametrize(
    ("yaml_body", "expected_message"),
    [
        ("[]\n", "models config must decode to a mapping"),
        ("task_routing: []\nretry_budget: {}\njob_runtime: {}\n", "task_routing must be a mapping"),
        ("task_routing: {}\nretry_budget: []\njob_runtime: {}\n", "retry_budget must be a mapping"),
        ("task_routing: {}\nretry_budget: {}\njob_runtime: []\n", "job_runtime must be a mapping"),
    ],
)
def test_load_model_routing_config_rejects_malformed_top_level_shapes(
    tmp_path: Path,
    yaml_body: str,
    expected_message: str,
) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(LLMConfigurationError, match=expected_message):
        load_model_routing_config(config_path)


def test_provider_attempt_budget_defaults_old_snapshots_and_preserves_explicit_value() -> None:
    empty_snapshot = parse_model_routing_config(
        {
            "node_routing": {},
            "task_routing": {},
            "model_profiles": {},
            "retry_budget": {},
            "job_runtime": {},
        }
    )
    explicit_snapshot = parse_model_routing_config(
        {
            "node_routing": {},
            "task_routing": {},
            "model_profiles": {},
            "retry_budget": {"provider_attempt_budget": 17},
            "job_runtime": {},
        }
    )

    assert getattr(llm_client, "DEFAULT_PROVIDER_ATTEMPT_BUDGET", None) == 32
    assert empty_snapshot.retry_budget["provider_attempt_budget"] == 32
    assert explicit_snapshot.retry_budget["provider_attempt_budget"] == 17


@pytest.mark.parametrize(
    ("yaml_body", "expected_message"),
    [
        (
            """
task_routing:
  extraction:
    provider: openai_compatible
    model: gpt-5-mini
    temperature: 0.1
    max_output_tokens: 100
    response_format: json_typo
retry_budget: {}
job_runtime: {}
""".strip(),
            "task_routing.extraction has unsupported response_format json_typo",
        ),
        (
            """
task_routing:
  extraction:
    provider: openai_compatible
    model: gpt-5-mini
    temperature: not-a-number
    max_output_tokens: 100
    response_format: json_object
retry_budget: {}
job_runtime: {}
""".strip(),
            "task_routing.extraction.temperature must be a valid float",
        ),
        (
            """
task_routing:
  extraction:
    provider: openai_compatible
    model: gpt-5-mini
    temperature: 0.1
    max_output_tokens: not-a-number
    response_format: json_object
retry_budget: {}
job_runtime: {}
""".strip(),
            "task_routing.extraction.max_output_tokens must be a valid integer",
        ),
        (
            """
task_routing:
  extraction:
    provider: bad_provider
    model: gpt-5-mini
    temperature: 0.1
    max_output_tokens: 100
    response_format: json_object
retry_budget: {}
job_runtime: {}
""".strip(),
            "task_routing.extraction has unsupported provider bad_provider",
        ),
    ],
)
def test_load_model_routing_config_rejects_invalid_task_model_values(
    tmp_path: Path,
    yaml_body: str,
    expected_message: str,
) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(LLMConfigurationError, match=expected_message):
        load_model_routing_config(config_path)


def test_parse_model_routing_config_preserves_legacy_stylize_without_marking_style_nodes_configured() -> None:
    config = parse_model_routing_config(
        {
            "node_routing": {
                "neutral_draft": {
                    "provider": "openai",
                    "provider_id": "openai_primary",
                    "account_id": "acct_ops",
                    "model": "gpt-5.4",
                    "temperature": 0.35,
                    "max_output_tokens": 3200,
                    "response_format": "json_object",
                    "reasoning_level": "medium",
                    "api_mode": "responses",
                    "provider_options": {"text_verbosity": "low"},
                }
            },
            "task_routing": {
                "stylize": {
                    "provider": "anthropic",
                    "provider_id": "claude_primary",
                    "model": "claude-sonnet-4-5",
                    "temperature": 0.7,
                    "max_output_tokens": 5000,
                    "response_format": "json_object",
                    "reasoning_level": "high",
                }
            },
            "retry_budget": {"total_attempt_budget": 3},
            "job_runtime": {},
        }
    )

    neutral = config.node_routing["neutral_draft"]
    assert neutral.provider == "openai"
    assert neutral.provider_id == "openai_primary"
    assert neutral.account_id == "acct_ops"
    assert neutral.reasoning_level == "medium"
    assert neutral.provider_options == {"text_verbosity": "low"}
    assert "style_draft" not in config.node_routing
    assert "style_patch" not in config.node_routing
    assert config.task_routing["stylize"].model == "claude-sonnet-4-5"


def test_parse_model_routing_config_rejects_invalid_reasoning_level() -> None:
    with pytest.raises(LLMConfigurationError, match="unsupported reasoning_level turbo"):
        parse_model_routing_config(
            {
                "node_routing": {
                    "neutral_draft": {
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "temperature": 0.2,
                        "max_output_tokens": 1000,
                        "response_format": "json_object",
                        "reasoning_level": "turbo",
                    }
                },
                "retry_budget": {},
                "job_runtime": {},
            }
        )


def test_parse_model_routing_config_rejects_oauth2_credential_mode() -> None:
    with pytest.raises(LLMConfigurationError, match="unsupported credential_mode oauth2"):
        parse_model_routing_config(
            {
                "node_routing": {
                    "neutral_draft": {
                        "provider": "gemini",
                        "provider_id": "gemini_oauth",
                        "model": "gemini-2.5-pro",
                        "temperature": 0.2,
                        "max_output_tokens": 1000,
                        "response_format": "json_object",
                        "credential_mode": "oauth2",
                    }
                },
                "retry_budget": {},
                "job_runtime": {},
            }
        )


def test_openai_adapter_maps_reasoning_and_json_schema() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = {
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": json.loads(request.content.decode("utf-8")),
        }
        return httpx.Response(
            200,
            json={
                "id": "resp_openai_reasoning",
                "model": "gpt-5.4",
                "output_text": '{"scene_text": "ok"}',
                "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
                "finish_reason": "stop",
            },
        )

    client = LLMClient(
        provider="openai",
        base_url="https://example.test/v1",
        api_key="legacy-unused",
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
        provider_configs={
            "openai_primary": ProviderRuntimeConfig(
                provider_id="openai_primary",
                provider_type="openai",
                base_url="https://example.test/v1",
                api_key="openai-key",
                api_mode="responses",
            )
        },
    )

    response = client.generate(
        LLMRequest(
            node_id="neutral_draft",
            provider_id="openai_primary",
            account_id="acct_openai",
            model="gpt-5.4",
            messages=[{"role": "user", "content": "Return JSON."}],
            temperature=0.2,
            max_output_tokens=100,
            response_format="json_object",
            response_schema={"name": "scene_payload", "schema": {"type": "object"}},
            reasoning_level="high",
        )
    )

    body = captured["body"]
    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer openai-key"
    assert body["reasoning"] == {"effort": "high"}
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "scene_payload"
    assert response.provider == "openai"
    assert response.model == "gpt-5.4"
    assert response.structured_output == {"scene_text": "ok"}


@pytest.mark.parametrize(
    ("provider_id", "provider_config", "request_kwargs", "response_body", "assert_payload"),
    [
        (
            "claude_primary",
            ProviderRuntimeConfig(
                provider_id="claude_primary",
                provider_type="anthropic",
                base_url="https://api.anthropic.test/v1",
                api_key="claude-key",
            ),
            {"model": "claude-sonnet-4-5", "reasoning_level": "medium", "max_output_tokens": 12000},
            {
                "id": "msg_123",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": '{"scene_text": "claude"}'}],
                "usage": {"input_tokens": 11, "output_tokens": 22},
                "stop_reason": "end_turn",
            },
            lambda captured: (
                captured["url"].endswith("/messages")
                and captured["headers"]["x-api-key"] == "claude-key"
                and captured["body"]["thinking"] == {"type": "enabled", "budget_tokens": 4096}
            ),
        ),
        (
            "deepseek_primary",
            ProviderRuntimeConfig(
                provider_id="deepseek_primary",
                provider_type="deepseek",
                base_url="https://api.deepseek.test/v1",
                api_key="deepseek-key",
            ),
            {"model": "deepseek-reasoner", "reasoning_level": "high"},
            {
                "id": "chatcmpl_deepseek",
                "model": "deepseek-reasoner",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"scene_text": "deepseek"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
            lambda captured: (
                captured["url"].endswith("/chat/completions")
                and captured["headers"]["authorization"] == "Bearer deepseek-key"
                and captured["body"]["thinking"] == {"type": "enabled"}
            ),
        ),
        (
            "glm_primary",
            ProviderRuntimeConfig(
                provider_id="glm_primary",
                provider_type="zhipu_glm",
                base_url="https://open.bigmodel.test/api/paas/v4",
                api_key="glm-key",
            ),
            {"model": "glm-4.6", "reasoning_level": "off"},
            {
                "id": "chatcmpl_glm",
                "model": "glm-4.6",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"scene_text": "glm"}'}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 9, "total_tokens": 17},
            },
            lambda captured: (
                captured["url"].endswith("/chat/completions")
                and captured["headers"]["authorization"] == "Bearer glm-key"
                and captured["body"]["thinking"] == {"type": "disabled"}
            ),
        ),
        (
            "gemini_primary",
            ProviderRuntimeConfig(
                provider_id="gemini_primary",
                provider_type="gemini",
                base_url="https://generativelanguage.googleapis.test/v1beta",
                api_key="gemini-key",
            ),
            {"model": "gemini-2.5-pro", "reasoning_level": "medium"},
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": '{"scene_text": "gemini"}'}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 7,
                },
            },
            lambda captured: (
                "models/gemini-2.5-pro:generateContent?key=gemini-key" in captured["url"]
                and captured["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 4096
                and captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
            ),
        ),
    ],
)
def test_provider_adapters_normalize_successful_json_responses(
    provider_id: str,
    provider_config: ProviderRuntimeConfig,
    request_kwargs: dict[str, object],
    response_body: dict[str, object],
    assert_payload,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = {
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": json.loads(request.content.decode("utf-8")),
        }
        return httpx.Response(200, json=response_body)

    client = LLMClient(
        provider="openai",
        base_url="https://unused.test/v1",
        api_key=None,
        timeout_seconds=12,
        transport=httpx.MockTransport(handler),
        provider_configs={provider_id: provider_config},
    )

    response = client.generate(
        LLMRequest(
            node_id="style_draft",
            provider_id=provider_id,
            account_id="acct_primary",
            model=str(request_kwargs["model"]),
            messages=[
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": "Draft the scene."},
            ],
            temperature=0.2,
            max_output_tokens=int(request_kwargs.get("max_output_tokens", 1000)),
            response_format="json_object",
            reasoning_level=str(request_kwargs["reasoning_level"]),
        )
    )

    assert assert_payload(captured)
    assert response.provider == provider_config.provider_type
    assert response.structured_output
    assert response.usage["total_tokens"] > 0
    assert response.finish_reason


class _RecordingAttemptHook:
    def __init__(self, *, reject_on: int | None = None) -> None:
        self.reject_on = reject_on
        self.before: list[tuple[object, str, int]] = []
        self.responses: list[tuple[object, str | None]] = []
        self.errors: list[tuple[object, str | None]] = []

    def before_dispatch(self, *, request: LLMRequest, dispatch_kind: str) -> object:
        ordinal = len(self.before) + 1
        if self.reject_on == ordinal:
            raise RuntimeError("attempt rejected")
        handle = f"attempt-{ordinal}"
        self.before.append((handle, dispatch_kind, request.max_output_tokens))
        return handle

    def after_response(self, handle, *, request, response, latency_ms) -> None:
        self.responses.append((handle, response.request_id))

    def after_error(
        self,
        handle,
        *,
        request,
        error,
        raw_response,
        provider_request_id,
        latency_ms,
    ) -> None:
        self.errors.append((handle, getattr(error, "code", None)))


def test_llm_client_preserves_missing_usage_provenance() -> None:
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"id": "no-usage", "model": "test", "output_text": "ok"},
            )
        ),
    )

    response = client.generate(
        LLMRequest(
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_output_tokens=8,
            response_format="text",
        )
    )

    assert response.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert response.raw_usage is None
    assert response.usage_present is False
    assert response.usage_complete is False


def test_llm_client_attempt_hook_wraps_transport_retry_and_can_reject_next_post() -> None:
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    hook = _RecordingAttemptHook(reject_on=2)

    with pytest.raises(RuntimeError, match="attempt rejected"):
        client.generate(
            LLMRequest(
                model="test",
                messages=[{"role": "user", "content": "hello"}],
                temperature=0,
                max_output_tokens=8,
                response_format="text",
            ),
            accounting_hook=hook,
        )

    assert post_count == 1
    assert hook.before == [("attempt-1", "initial", 8)]
    assert hook.errors == [("attempt-1", "LLM_REQUEST_TIMEOUT")]


def test_llm_client_missing_text_degrade_recomputes_attempt_request() -> None:
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return httpx.Response(200, json={"id": "empty", "model": "test"})
        return httpx.Response(
            200,
            json={"id": "ok", "model": "test", "output_text": "done"},
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    hook = _RecordingAttemptHook()

    response = client.generate(
        LLMRequest(
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_output_tokens=8,
            response_format="text",
            reasoning_level="medium",
        ),
        accounting_hook=hook,
    )

    assert response.text == "done"
    assert hook.before == [
        ("attempt-1", "initial", 8),
        ("attempt-2", "missing_text_degrade", 16),
    ]
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_MISSING_TEXT")]
    assert hook.responses == [("attempt-2", "ok")]


def test_llm_client_attempt_hook_wraps_response_parse_retry() -> None:
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return httpx.Response(
                200,
                json={"id": "bad-json", "model": "test", "output_text": "not json"},
            )
        return httpx.Response(
            200,
            json={"id": "parse-ok", "model": "test", "output_text": '{"ok":true}'},
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    hook = _RecordingAttemptHook()

    response = client.generate(
        LLMRequest(
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_output_tokens=8,
            response_format="json_object",
        ),
        accounting_hook=hook,
    )

    assert response.structured_output == {"ok": True}
    assert hook.before == [
        ("attempt-1", "initial", 8),
        ("attempt-2", "response_parse_retry", 8),
    ]
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_INVALID_JSON")]
    assert hook.responses == [("attempt-2", "parse-ok")]


def test_llm_client_attempt_hook_wraps_structured_output_degrade() -> None:
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return httpx.Response(500, text="guided_grammar compile error")
        return httpx.Response(
            200,
            json={"id": "degrade-ok", "model": "test", "output_text": '{"ok":true}'},
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    hook = _RecordingAttemptHook()

    response = client.generate(
        LLMRequest(
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_output_tokens=8,
            response_format="json_object",
            response_schema={"name": "payload", "schema": {"type": "object"}},
        ),
        accounting_hook=hook,
    )

    assert response.structured_output == {"ok": True}
    assert hook.before == [
        ("attempt-1", "initial", 8),
        ("attempt-2", "structured_output_degrade", 8),
    ]
    assert hook.errors == [("attempt-1", "LLM_HTTP_STRUCTURED_OUTPUT_REJECTED")]
    assert hook.responses == [("attempt-2", "degrade-ok")]


def test_llm_client_non_object_json_response_still_terminates_physical_attempt_once() -> None:
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=["not", "an", "object"])),
    )
    hook = _RecordingAttemptHook()

    with pytest.raises(LLMResponseError) as exc_info:
        client.generate(
            LLMRequest(
                model="test",
                messages=[{"role": "user", "content": "hello"}],
                temperature=0,
                max_output_tokens=8,
                response_format="text",
            ),
            accounting_hook=hook,
        )

    assert exc_info.value.code == "LLM_RESPONSE_INVALID"
    assert hook.before == [("attempt-1", "initial", 8)]
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_INVALID")]
    assert hook.responses == []


def test_llm_client_unexpected_adapter_parser_error_still_terminates_attempt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_adapter = llm_client.get_adapter
    delegate = original_get_adapter("openai_compatible")

    class ExplodingParserAdapter:
        def __getattr__(self, name: str):
            return getattr(delegate, name)

        def extract_output_text(self, body, *, request):
            raise AttributeError("provider parser defect")

    monkeypatch.setattr(llm_client, "get_adapter", lambda _provider: ExplodingParserAdapter())
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"id": "parser-defect", "output_text": "ok"})
        ),
    )
    hook = _RecordingAttemptHook()

    with pytest.raises(LLMResponseError) as exc_info:
        client.generate(
            LLMRequest(
                model="test",
                messages=[{"role": "user", "content": "hello"}],
                temperature=0,
                max_output_tokens=8,
                response_format="text",
            ),
            accounting_hook=hook,
        )

    assert exc_info.value.code == "LLM_RESPONSE_INVALID"
    assert exc_info.value.details["parser_error_type"] == "AttributeError"
    assert hook.before == [("attempt-1", "initial", 8)]
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_INVALID")]
    assert hook.responses == []


def test_llm_client_unexpected_post_error_terminates_attempt_once() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport implementation exploded")

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    hook = _RecordingAttemptHook()

    with pytest.raises(LLMHTTPError) as exc_info:
        client.generate(
            LLMRequest(
                model="test",
                messages=[{"role": "user", "content": "hello"}],
                temperature=0,
                max_output_tokens=8,
                response_format="text",
            ),
            accounting_hook=hook,
        )

    assert exc_info.value.code == "LLM_HTTP_CLIENT_EXCEPTION"
    assert exc_info.value.details["original_error_type"] == "RuntimeError"
    assert hook.before == [("attempt-1", "initial", 8)]
    assert hook.errors == [("attempt-1", "LLM_HTTP_CLIENT_EXCEPTION")]
    assert hook.responses == []


def _replacement_client(outputs: list[str], *, max_retries: int) -> tuple[LLMClient, list[int]]:
    posts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        posts.append(1)
        index = min(len(posts), len(outputs)) - 1
        return httpx.Response(
            200,
            json={"id": f"resp-{len(posts)}", "model": "test", "output_text": outputs[index]},
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
    )
    return client, posts


def _prose_request(prompt: str = "写一段。", *, response_format: str = "text") -> LLMRequest:
    return LLMRequest(
        model="test",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_output_tokens=64,
        response_format=response_format,
    )


def test_llm_client_resends_output_that_lost_bytes_in_transit() -> None:
    # 中转偶尔把多字节字符换成 U+FFFD;同一请求重发一次就能换回干净正文。
    client, posts = _replacement_client(["雨下\ufffd\ufffd一夜。", "雨下了一夜。"], max_retries=2)
    hook = _RecordingAttemptHook()

    response = client.generate(_prose_request(), accounting_hook=hook)

    assert response.text == "雨下了一夜。"
    assert len(posts) == 2
    assert hook.before == [
        ("attempt-1", "initial", 64),
        ("attempt-2", "response_parse_retry", 64),
    ]
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_CORRUPTED_TEXT")]
    assert hook.responses == [("attempt-2", "resp-2")]


def test_llm_client_resends_structured_output_whose_json_escapes_carry_replacement_characters() -> None:
    client, posts = _replacement_client(
        ['{"scene_text": "雨下\\ufffd一夜。"}', '{"scene_text": "雨下了一夜。"}'],
        max_retries=1,
    )

    response = client.generate(_prose_request(response_format="json_object"))

    assert response.structured_output == {"scene_text": "雨下了一夜。"}
    assert len(posts) == 2


def test_llm_client_hands_still_corrupted_output_to_the_caller_after_the_retry_budget() -> None:
    # 重试额度用完仍带乱码:照常交回(不抛错),由调用方的文本完整性闸门拒稿。
    client, posts = _replacement_client(["雨下\ufffd一夜。"], max_retries=1)
    hook = _RecordingAttemptHook()

    response = client.generate(_prose_request(), accounting_hook=hook)

    assert response.text == "雨下\ufffd一夜。"
    assert len(posts) == 2
    assert hook.errors == [("attempt-1", "LLM_RESPONSE_CORRUPTED_TEXT")]
    assert hook.responses == [("attempt-2", "resp-2")]


def test_llm_client_keeps_replacement_characters_the_prompt_itself_carries() -> None:
    # prompt 里本来就有 U+FFFD(作者贴进来的坏字),模型照抄不算传输损坏,不白白重发。
    client, posts = _replacement_client(["原文里的\ufffd照抄。"], max_retries=2)

    response = client.generate(_prose_request("把这句改写：原文里的\ufffd。"))

    assert response.text == "原文里的\ufffd照抄。"
    assert len(posts) == 1

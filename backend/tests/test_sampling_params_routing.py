"""Blueprint §7 anti-mean sampling — DB node-routing + Anthropic mapping regression.

Two gaps this guards:

1. The decoding-level penalties (frequency_penalty/presence_penalty) once reached the
   request only on the YAML task_routing path: a UI-configured DB ``node_routing`` route
   silently dropped them to None. Since approval #5a a DB route stores only the author's
   choice (provider / model) and the parser fills every other parameter from the node
   spec, so the route_payload -> (DB) -> _load_task_model_config round-trip keeps them.

2. The Anthropic adapter forwarded none of the sampling params. Anthropic's Messages API
   supports ``top_p`` but not frequency/presence penalties (OpenAI-only). Fix: map only
   the legal ``top_p``.
"""

from __future__ import annotations


def test_route_payload_carries_only_the_authors_choice_and_the_spec_carries_the_penalties() -> None:
    """批准#5a:快照里的路由只存作者的选择;解码惩罚随节点 spec 走(见下一条的往返)。"""
    from novel_system.services.llm_node_registry import default_task_config_payload, get_llm_node_spec

    payload = default_task_config_payload("style_draft", provider_id="acct1")
    assert "frequency_penalty" not in payload and "presence_penalty" not in payload
    # 2026-09-09 样例优先:惩罚归零(会抹平参考作者刻意的复沓),但键仍随路由发出。
    defaults = get_llm_node_spec("style_draft").route_defaults()
    assert defaults["frequency_penalty"] == 0.0
    assert defaults["presence_penalty"] == 0.0


def test_db_node_route_roundtrip_preserves_penalties() -> None:
    """The core regression: a DB-stored node route (built from route_payload) must keep
    the penalties after reload — previously they were dropped to None on this path."""
    from novel_system.services.llm_node_registry import default_task_config_payload
    from novel_system.services.llm_client import _load_task_model_config

    payload = default_task_config_payload("style_draft", provider_id="acct1")
    # Simulate persist-to-DB then reload into a TaskModelConfig.
    cfg = _load_task_model_config("style_draft", payload)
    assert cfg.frequency_penalty == 0.0
    assert cfg.presence_penalty == 0.0


def test_non_style_node_omits_penalties() -> None:
    """QC / extraction nodes want low-randomness determinism, not anti-mean penalties."""
    from novel_system.services.llm_node_registry import default_task_config_payload

    payload = default_task_config_payload("hard_qc", provider_id="acct1")
    assert "frequency_penalty" not in payload
    assert "presence_penalty" not in payload


def test_anthropic_maps_top_p_only() -> None:
    from novel_system.services.llm_client import TaskModelConfig
    from novel_system.services.llm_task_runner import LLMNodeRunner
    from novel_system.services.llm_providers.anthropic import AnthropicAdapter
    from novel_system.services.llm_providers.base import ProviderRuntimeConfig

    tc = TaskModelConfig(
        provider="anthropic", model="claude-test", temperature=1.0,
        max_output_tokens=1000, response_format="text", api_mode="messages",
        frequency_penalty=0.3, presence_penalty=0.15, top_p=0.9,
    )
    req = LLMNodeRunner._build_request(
        {"system_prompt": "sys", "token_budget": {}},
        user_prompt="u", node_id="style_draft", task_config=tc,
    )
    assert req.top_p == 0.9
    assert req.frequency_penalty == 0.3  # present on the request object...

    cfg = ProviderRuntimeConfig(
        provider_id="p", provider_type="anthropic",
        base_url="https://api.anthropic.com/v1", api_mode="messages",
    )
    http = AnthropicAdapter().build_request(req, cfg)
    # ...but only the Anthropic-legal top_p is forwarded to the wire payload.
    assert http.payload.get("top_p") == 0.9
    assert "frequency_penalty" not in http.payload
    assert "presence_penalty" not in http.payload

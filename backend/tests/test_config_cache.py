"""配置解析缓存（2026-09-29 重构 P00b，审计 X01-01 / 02 / 15 / 21、X04-02）。

提示词模板与模型路由按**来源内容**记忆（``services/config_cache.py``）：同一份内容只解析一次，内容一变下一次
读取就是新配置。这里守两件事：

1. 作者在系统配置里保存 / 切换 / 回滚快照、``sync_prompt_templates`` / ``raise_llm_output_budget`` 激活新快照、
   迁移就地改写快照、改仓库 yaml（哪怕大小与修改时间都不变）——下一次读取立刻看到新内容，不必重启；
2. C 解析器与纯 Python 解析器对仓库里每份 yaml 解析结果相同，出错时的报错逐字相同。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy import update

from novel_system.db.models import SystemConfigSnapshot
from novel_system.services import config_cache, idempotency, llm_client, prompt_builder
from novel_system.services.config_cache import ContentKeyedCache, safe_load_yaml
from novel_system.services.llm_client import load_model_routing_config, reset_model_routing_cache
from novel_system.services.prompt_builder import (
    PromptBuilder,
    load_prompt_templates,
    reset_prompt_template_cache,
)
from novel_system.services.system_config import SystemConfigService, validate_config


REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
ADMIN_HEADERS = {"X-Admin-Token": "admin-token", "X-Operator-Ref": "ops.config"}


@pytest.fixture(autouse=True)
def _fresh_parse_caches():
    reset_prompt_template_cache()
    reset_model_routing_cache()
    yield
    reset_prompt_template_cache()
    reset_model_routing_cache()


def _template(system_prompt: str, *, version: str = "2026-09-29.v1") -> dict:
    return {
        "version": version,
        "input_token_budget": 24000,
        "system_prompt": system_prompt,
        "task_prompt": "写这一场。",
        "structured_schema": {
            "type": "object",
            "required": ["scene_text"],
            "properties": {"scene_text": {"type": "string"}},
        },
    }


def _models(max_output_tokens: int, *, claim_ttl: int = 90) -> dict:
    return {
        "task_routing": {
            "neutral_draft": {
                "provider": "openai_compatible",
                "model": "fixture-model",
                "temperature": 0.7,
                "max_output_tokens": max_output_tokens,
                "response_format": "text",
            }
        },
        "job_runtime": {"idempotency_claim_ttl_seconds": claim_ttl},
    }


def _activate(session, category: str, payload: dict) -> str:
    service = SystemConfigService(session)
    created = service.create_draft(
        category=category,
        yaml_raw=yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        secrets=None,
        actor_ref="test",
    )
    snapshot_id = created["snapshot"]["snapshot_id"]
    service.activate(snapshot_id, actor_ref="test")
    return snapshot_id


def _count_calls(monkeypatch, module, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(module, name)

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counting)
    return calls


def _count_yaml_parses(monkeypatch) -> dict[str, int]:
    """数一数仓库 prompts.yaml / models.yaml 各被 YAML 解析了几次（C 与纯 Python 解析器都经 yaml.load）。"""
    watched = {
        (REPO_CONFIG / "prompts.yaml").read_text(encoding="utf-8"): "prompts",
        (REPO_CONFIG / "models.yaml").read_text(encoding="utf-8"): "models",
    }
    counts = {"prompts": 0, "models": 0}
    real_load = yaml.load

    def counting_load(stream, Loader):  # noqa: N803 — 与 yaml.load 的参数名一致
        name = watched.get(stream) if isinstance(stream, str) else None
        if name is not None:
            counts[name] += 1
        return real_load(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", counting_load)
    return counts


# ---------------------------------------------------------------- YAML 解析入口


def test_fast_loader_parses_every_shipped_yaml_exactly_like_the_pure_loader() -> None:
    paths = sorted(REPO_CONFIG.rglob("*.yaml"))
    assert {"prompts.yaml", "models.yaml", "injection_budget.yaml"} <= {path.name for path in paths}
    if yaml.__with_libyaml__:
        assert config_cache._FAST_SAFE_LOADER is yaml.CSafeLoader
    for path in paths:
        source = path.read_text(encoding="utf-8")
        pure = yaml.load(source, Loader=yaml.SafeLoader)
        # repr 比 == 严：1 与 1.0、键的顺序不同都会露出来
        assert repr(safe_load_yaml(source)) == repr(pure), path.name
        if yaml.__with_libyaml__:
            assert repr(yaml.load(source, Loader=yaml.CSafeLoader)) == repr(pure), path.name


def test_yaml_errors_read_exactly_like_the_pure_loaders() -> None:
    broken = "task_routing:\n  neutral_draft: [1, 2\n  hard_qc: 3\n"
    with pytest.raises(yaml.YAMLError) as pure:
        yaml.load(broken, Loader=yaml.SafeLoader)
    with pytest.raises(yaml.YAMLError) as fast:
        safe_load_yaml(broken)
    assert type(fast.value) is type(pure.value)
    assert str(fast.value) == str(pure.value)
    # 系统配置的校验提示原样显示这句报错
    assert validate_config("models", broken) == ({}, {"ok": False, "message": str(pure.value)})


def test_content_keyed_cache_is_a_small_lru_that_never_caches_failures() -> None:
    cache = ContentKeyedCache(maxsize=2)
    assert cache.get_or_build("a", lambda: 1) == 1
    assert cache.get_or_build("a", lambda: 2) == 1
    with pytest.raises(ValueError):
        cache.get_or_build("b", lambda: (_ for _ in ()).throw(ValueError("broken")))
    assert cache.get_or_build("b", lambda: 3) == 3
    assert cache.get_or_build("c", lambda: 4) == 4
    assert len(cache) == 2
    assert cache.get_or_build("a", lambda: 5) == 5  # 最久没用的 a 已被挤出
    assert cache.builds == 4


# ---------------------------------------------------------------- 提示词模板


def test_prompt_templates_are_parsed_once_per_content(monkeypatch) -> None:
    parses = _count_calls(monkeypatch, prompt_builder, "parse_prompt_templates")
    yaml_parses = _count_yaml_parses(monkeypatch)

    for _ in range(3):
        assert "neutral_draft" in load_prompt_templates()
        assert PromptBuilder().has_template("hard_qc")
        assert "neutral_draft" in load_prompt_templates(REPO_CONFIG / "prompts.yaml")

    assert len(parses) == 1
    assert yaml_parses["prompts"] == 1


def test_each_caller_gets_its_own_template_mapping() -> None:
    first = load_prompt_templates()
    first.pop("neutral_draft")
    first["made_up"] = first["hard_qc"]

    second = load_prompt_templates()
    assert "neutral_draft" in second
    assert "made_up" not in second


def test_prompt_snapshot_save_switch_and_rollback_are_visible_at_once(session) -> None:
    from_file = load_prompt_templates()["neutral_draft"]

    first = _activate(session, "prompts", {"templates": {"neutral_draft": _template("甲版：雨城的旧信")}})
    assert load_prompt_templates()["neutral_draft"].system_prompt == "甲版：雨城的旧信"

    _activate(session, "prompts", {"templates": {"neutral_draft": _template("乙版：林昭的案卷")}})
    assert load_prompt_templates()["neutral_draft"].system_prompt == "乙版：林昭的案卷"
    assert PromptBuilder().build({}, "neutral_draft")["system_prompt"] == "乙版：林昭的案卷"

    SystemConfigService(session).activate(first, actor_ref="test")  # 回滚到甲版
    assert load_prompt_templates()["neutral_draft"].system_prompt == "甲版：雨城的旧信"
    assert not PromptBuilder().has_template("hard_qc")

    session.execute(update(SystemConfigSnapshot).values(active_flag=0, status="superseded"))
    session.commit()
    assert load_prompt_templates()["neutral_draft"] == from_file


def test_snapshot_rewritten_in_place_is_visible_at_once(session) -> None:
    """迁移（如 0074）会就地改写活动快照：同一 snapshot_id、同一版本号，内容不同。"""
    _activate(session, "prompts", {"templates": {"neutral_draft": _template("改写前")}})
    assert load_prompt_templates()["neutral_draft"].system_prompt == "改写前"

    rewritten = {"templates": {"neutral_draft": _template("改写后")}}
    session.execute(
        update(SystemConfigSnapshot)
        .where(SystemConfigSnapshot.category == "prompts", SystemConfigSnapshot.active_flag == 1)
        .values(parsed_json=rewritten)
    )
    session.commit()
    assert load_prompt_templates()["neutral_draft"].system_prompt == "改写后"


def test_prompts_file_edit_is_visible_even_when_size_and_mtime_do_not_change(tmp_path, monkeypatch) -> None:
    prompts = tmp_path / "prompts.yaml"
    prompts.write_text(
        yaml.safe_dump({"templates": {"neutral_draft": _template("旧信甲")}}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_builder, "_default_prompts_config_path", lambda: prompts)
    assert load_prompt_templates()["neutral_draft"].system_prompt == "旧信甲"

    before = prompts.stat()
    prompts.write_text(prompts.read_text(encoding="utf-8").replace("旧信甲", "旧信乙"), encoding="utf-8")
    os.utime(prompts, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = prompts.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    assert load_prompt_templates()["neutral_draft"].system_prompt == "旧信乙"
    assert load_prompt_templates(prompts)["neutral_draft"].system_prompt == "旧信乙"


def test_sync_prompt_templates_activation_is_visible_at_once(session, tmp_path, monkeypatch) -> None:
    from novel_system.tools import sync_prompt_templates

    repo = tmp_path / "prompts.yaml"
    repo.write_text(
        yaml.safe_dump(
            {"templates": {"neutral_draft": _template("仓库新版", version="2026-09-29.v2")}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_prompt_templates, "_repo_prompts_path", lambda: repo)
    _activate(session, "prompts", {"templates": {"neutral_draft": _template("快照旧版")}})
    assert load_prompt_templates()["neutral_draft"].system_prompt == "快照旧版"

    assert sync_prompt_templates.main(["--execute"]) == 0

    synced = load_prompt_templates()["neutral_draft"]
    assert (synced.version, synced.system_prompt) == ("2026-09-29.v2", "仓库新版")


# ---------------------------------------------------------------- 模型路由与租约


def test_model_routing_is_parsed_once_per_content(monkeypatch) -> None:
    parses = _count_calls(monkeypatch, llm_client, "parse_model_routing_config")
    yaml_parses = _count_yaml_parses(monkeypatch)

    routings = {id(load_model_routing_config()) for _ in range(5)}
    for _ in range(20):
        idempotency.owner_lease_ttl_seconds()
        idempotency.owner_lease_grace_seconds()

    assert len(routings) == 1
    assert len(parses) == 1
    assert yaml_parses["models"] == 1


def test_models_snapshot_changes_reach_routing_and_lease_ttl_at_once(session) -> None:
    from novel_system.tools import raise_llm_output_budget

    _activate(session, "models", _models(1200, claim_ttl=321))
    assert load_model_routing_config().task_routing["neutral_draft"].max_output_tokens == 1200
    assert idempotency.owner_lease_ttl_seconds() == 321

    _activate(session, "models", _models(1500, claim_ttl=654))
    assert load_model_routing_config().task_routing["neutral_draft"].max_output_tokens == 1500
    assert idempotency.owner_lease_ttl_seconds() == 654

    assert raise_llm_output_budget.main(["--node", "neutral_draft", "--floor", "4096", "--execute"]) == 0
    assert load_model_routing_config().task_routing["neutral_draft"].max_output_tokens == 4096


def test_node_routes_saved_through_system_config_are_visible_at_once(client, monkeypatch) -> None:
    """系统配置界面保存节点路由（同一个 HTTP 入口）之后，下一次读路由就是新的输出预算。"""
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("NOVEL_SYSTEM_CONFIG_SECRET", "config-secret")
    provider = client.post(
        "/api/v1/system-config/llm/providers",
        headers=ADMIN_HEADERS,
        json={
            "provider_id": "fixture_local",
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "enabled": True,
            "credential_mode": "none",
            "api_mode": "chat",
            "models": ["fixture-model"],
        },
    )
    assert provider.status_code == 200, provider.json()
    load_model_routing_config()  # 先把保存之前的路由读进缓存

    for budget in (1111, 2222):
        route = {
            "provider": "openai_compatible",
            "provider_id": "fixture_local",
            "model": "fixture-model",
            "temperature": 0.5,
            "max_output_tokens": budget,
            "response_format": "text",
            "reasoning_level": "medium",
            "api_mode": "chat",
            "credential_mode": "none",
        }
        saved = client.post(
            "/api/v1/system-config/llm/node-routes",
            headers=ADMIN_HEADERS,
            json={"node_routing": {"neutral_draft": route}, "activate": True},
        )
        assert saved.status_code == 200, saved.json()
        assert load_model_routing_config().node_routing["neutral_draft"].max_output_tokens == budget

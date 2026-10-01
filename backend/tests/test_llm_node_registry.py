"""LLM 节点注册表 ↔ prompts.yaml 对齐守卫。

背景（健康审计 · 架构契约）：`prompt_builder.py` 用 `self._templates[template_name]`
裸字典索引取模板，缺键即在运行期抛 KeyError；而 `llm_node_registry.py` 的
`LLMNodeSpec.template_name` 元数据与真实模板集此前没有任何对齐测试——新增节点若
误填 template_name，CI 不会捕获，只在该节点首次执行时炸。

窄约束关键：并非所有 `template_name` 都经 PromptBuilder.build() 索引。有三类例外
**不会**触发 KeyError，因此即便不在 prompts.yaml 也属正常，必须显式豁免（否则 naive
守卫会误报）：
  1) 任务路由别名（style_draft/style_patch 节点声明 template_name="stylize"，
     但实际 .build() 用 "style_draft"，由 llm_task_runner 路由到 task_routing["stylize"]）；
  2) 内联构造 prompt（scene_quality.py 直接拼 prompt，不经 PromptBuilder）；
  3) run_task + _AD_HOC_ROUTE_ALIASES 等非 PromptBuilder 路径，或当前无调用方的保留节点。

本守卫做两件事，互为自清理：
  A. 每个节点的 template_name 必须 ∈ prompts.yaml 或 ∈ 文档化豁免集；
  B. 豁免集成员必须确实不在 prompts.yaml（一旦某项被补进 prompts.yaml，应从豁免集
     移除，让它重新受 A 守护）——防止豁免集腐烂成长期掩盖真缺失的地毯。
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import yaml

from novel_system.services.llm_client import TaskModelConfig, load_model_routing_config, parse_model_routing_config
from novel_system.services.llm_node_registry import get_llm_node_spec, llm_node_specs
from novel_system.services.prompt_builder import load_prompt_templates


# 不经 PromptBuilder.build() 直接索引的 template_name —— 缺席 prompts.yaml 属正常。
# 每项都注明豁免理由；新增豁免必须同步说明为何不会触发 KeyError。
_NON_PROMPTBUILDER_TEMPLATE_NAMES = {
    # 任务路由别名：节点 template_name="stylize"，实际 .build() 用 "style_draft"
    # （llm_task_runner.py 把 style_draft/style_patch 路由到 task_routing["stylize"]）。
    "stylize",
    # 当前无 PromptBuilder 调用方（保留 / 旧元数据）。
    "snowflake_step_generate",
}


def test_node_template_names_resolve_or_are_documented_non_builder():
    """每个节点的 template_name 必须在 prompts.yaml，或登记为非 PromptBuilder 路径。"""
    templates = load_prompt_templates()
    missing = [
        (spec.node_id, spec.template_name)
        for spec in llm_node_specs()
        if spec.template_name
        and spec.template_name not in templates
        and spec.template_name not in _NON_PROMPTBUILDER_TEMPLATE_NAMES
    ]
    assert not missing, (
        "这些节点的 template_name 既不在 config/prompts.yaml，也未登记为非 PromptBuilder 路径："
        f"{missing}。新增 LLM 节点时：若走 PromptBuilder.build()，在 prompts.yaml 加同名模板；"
        "若走内联/别名/run_task，把它加入 _NON_PROMPTBUILDER_TEMPLATE_NAMES 并注明为何不会触发 KeyError。"
    )


def test_documented_non_builder_names_are_actually_absent_from_prompts():
    """自清理：豁免集成员一旦进了 prompts.yaml，应移出豁免集（恢复 A 守护）。"""
    templates = load_prompt_templates()
    leaked = sorted(n for n in _NON_PROMPTBUILDER_TEMPLATE_NAMES if n in templates)
    assert not leaked, (
        f"{leaked} 现已在 prompts.yaml 中定义，应从 _NON_PROMPTBUILDER_TEMPLATE_NAMES 移除，"
        "让它们重新受 test_node_template_names_resolve_or_are_documented_non_builder 守护。"
    )


def test_prompts_yaml_templates_are_well_formed():
    """prompts.yaml 每个模板都应成功解析且带非空 system/task prompt（半成品模板拦在 CI）。"""
    templates = load_prompt_templates()
    assert templates, "prompts.yaml 未解析出任何模板"
    malformed = [
        name
        for name, tpl in templates.items()
        if not (getattr(tpl, "system_prompt", "") or "").strip()
        or not (getattr(tpl, "task_prompt", "") or "").strip()
    ]
    assert not malformed, f"这些 prompts.yaml 模板缺少非空 system_prompt / task_prompt：{malformed}"


def test_style_analysis_defaults_are_stable_and_have_verified_output_headroom():
    """风格参考的分析节点:确定性(温度 0)与验证过的输出余量。节点 spec 是唯一的默认值来源(重评 R4)。"""
    # 2026-09-23 v3 学习文风作业:四层各一次读同一组 4 万字窗口、整张文风卡一次写完 → 16384;
    # 专名 8192、窗口标签每批 8 窗 4096(关推理)
    expected = {
        "style_ref_paragraph_classify_bulk": 8192,
        "style_ref_extract_language": 16384,
        "style_ref_extract_narrative": 16384,
        "style_ref_extract_scene": 16384,
        "style_ref_extract_theme": 16384,
        "style_ref_synthesize_profile": 16384,
        "style_ref_protected_terms": 8192,
        "style_ref_tag_windows": 4096,
    }

    for node_id, max_output_tokens in expected.items():
        spec = get_llm_node_spec(node_id)
        assert spec is not None
        assert spec.temperature == 0.0
        assert spec.max_output_tokens == max_output_tokens


def _spec_as_task_config(spec) -> dict:
    return {
        "provider": spec.provider,
        "model": spec.model,
        "temperature": spec.temperature,
        "max_output_tokens": spec.max_output_tokens,
        "response_format": spec.response_format,
        "provider_id": None,
        "account_id": None,
        "reasoning_level": spec.reasoning_level,
        "api_mode": spec.api_mode,
        "credential_mode": None,
        "provider_options": {},
        "frequency_penalty": spec.frequency_penalty,
        "presence_penalty": spec.presence_penalty,
        "top_p": spec.top_p,
        "timeout_seconds": None,
    }


def test_repo_models_yaml_declares_no_routes_any_more():
    """重评 R4:models.yaml 的 task_routing 是注册表逐字段的抄本(删之前证明过 0 处差异),已删;
    文件只剩设置页不编辑的两段运行参数。"""
    root = Path(__file__).resolve().parents[2]
    payload = yaml.safe_load((root / "config" / "models.yaml").read_text(encoding="utf-8"))
    assert set(payload) == {"retry_budget", "job_runtime"}


def test_snapshot_less_resolution_equals_the_spec_for_every_node():
    """没有 models 快照、库里也没有保存过服务(API 配置来自环境变量)时,每个节点逐字段就是它的 spec。"""
    routing = load_model_routing_config()
    specs = {spec.node_id: spec for spec in llm_node_specs()}
    assert set(routing.node_routing) == set(specs)
    diffs = [
        (node_id, field.name, getattr(routing.node_routing[node_id], field.name), _spec_as_task_config(spec)[field.name])
        for node_id, spec in specs.items()
        for field in fields(TaskModelConfig)
        if getattr(routing.node_routing[node_id], field.name) != _spec_as_task_config(spec)[field.name]
    ]
    assert diffs == []
    assert routing.task_routing["stylize"] == routing.node_routing["style_draft"]
    assert routing.retry_budget["provider_attempt_budget"] == 32
    assert routing.job_runtime["idempotency_claim_ttl_seconds"] == 600


def test_writer_passage_patch_output_budget_fits_two_long_rewrites():
    """重评 R12:改写候选不再回抄原文,两版近 2000 字的改写(含思考 token)一次装下——节点默认值 8192,
    快照里只存了服务 / 模型的路由也按它发(已存过 models 快照的安装由迁移 0096 去掉当年抄进去的 2600)。"""
    spec = get_llm_node_spec("writer_passage_patch")
    assert spec is not None and spec.max_output_tokens == 8192
    lean = parse_model_routing_config(
        {"node_routing": {"writer_passage_patch": {"provider_id": "relay", "model": "m", "api_mode": "chat"}}}
    )
    assert lean.node_routing["writer_passage_patch"].max_output_tokens == 8192

"""2026-09-16：角色摘要表「采纳并结构化」连续报「过于稀疏」的真实故障。

根因：两条 OpenAI 线路都把模板 structured_schema 作为 json_schema 下发，而集合步的成员对象只写了
``additionalProperties: true``——按 schema 约束解码的后端（经中转的 Gemini）对这种对象只能吐 ``{}``，
审计里的输出正是 54 / 57 字节的 ``[{},{}]`` / ``[{},{},{}]``。三道修补各有一条守卫：

1. 下发的 schema 按编辑器模板补全成员 properties（教练补丁 / 分诊修补同理）；
2. 清洗后仍只剩身份键 → 带原因重试一次，再空就如实 409（``sparse_output``），空成员不铸幽灵 id；
3. 节点默认输出预算 8192（与 config/models.yaml 一致；补全后的角色表实测 4.7k 输出 token，3200 装不下）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.accounted_llm_fakes import accounted_generate_method

from novel_system.services.llm_client import LLMResponse, load_model_routing_config
from novel_system.services.llm_node_registry import get_llm_node_spec
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.snowflake_prompt_budget import PROTECTED_KEYS
from novel_system.services.snowflake_steps import get_step_definition
from novel_system.services.snowflake_workspace_llm import (
    _sanitize_character_items,
    _sanitize_scene_list_items,
    enrich_structured_schema,
)

FULL_SHEETS = {
    "protagonist_character_id": "c1",
    "characters": [
        {
            "character_id": "c1",
            "display_name": "沈何来",
            "role": "主角",
            "goal": "还原父亲失踪当年被改写的卷宗",
            "ambition": "夺回被污名化的人格尊严",
            "values": ["没有什么比还原卷宗真相更重要", "没有什么比保全父亲的体面更重要"],
            "conflict": "恩师周砚以养育之恩与馆长职权层层封锁档案",
            "epiphany": "体面的隐瞒是对受害者的二次谋杀",
            "one_sentence_summary": "档案修复师沈何来为查清父亲的冤案与恩师决裂。",
            "one_paragraph_summary": "沈何来借调回县档案馆，在纸张纤维层间发现恩师的涂改痕迹……",
        },
        {
            "character_id": "",
            "display_name": "周砚",
            "role": "对手",
            "goal": "销毁二十年前失踪案的原始记录",
            "ambition": "守住地方秩序与自己的道德体面",
            "values": ["没有什么比多数人的安稳更重要"],
            "conflict": "得意门生带着顶尖修复技术步步紧逼",
            # 两个角色每个字段都非空：否则既有的 completeness_repair 会再补一次调用，call 数就不精确了
            "epiphany": "在药水显影出自己当年的笔迹时明白体面守不住任何人",
            "one_sentence_summary": "馆长周砚为守护体面阻击养子追查旧案，终至身败名裂。",
            "one_paragraph_summary": "周砚以长辈姿态迎接沈何来归来……",
        },
    ],
}


def _create_project(client, key: str = "sparse-schema-project") -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "何来", "outline_text": "档案修复师回县城查父亲的旧案。"},
        headers={"X-Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _install_fake(monkeypatch, responses: list[dict], captured: list) -> None:
    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        payload = responses[min(len(captured), len(responses)) - 1]
        return LLMResponse(
            request_id=f"resp_{len(captured)}",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{len(captured)}"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        accounted_generate_method(fake_generate),
    )


# ---------------------------------------------------------------------------
# 1. wire schema：成员对象按编辑器模板补全 properties
# ---------------------------------------------------------------------------


def test_collection_item_schemas_list_the_canonical_keys() -> None:
    templates = load_prompt_templates()
    for step_key, field_key in (
        ("character_sheets", "characters"),
        ("character_synopses", "characters"),
        ("character_bibles", "characters"),
        ("scene_list", "scenes"),
        ("scene_details", "scenes"),
        ("long_synopsis", "chapters"),
    ):
        template = templates[f"snowflake_generate_{step_key}"]
        assert not template.structured_schema["properties"][field_key]["items"].get("properties"), (
            "yaml 里的成员 schema 本就没有 properties——补全在服务端按编辑器模板派生，别再手抄一份"
        )
        enriched, applied = enrich_structured_schema(template.structured_schema, step_key=step_key)
        assert applied == [f"{field_key}_items"]
        editor_template = next(
            field["template"]
            for field in get_step_definition(step_key)["editor"]["fields"]
            if field["key"] == field_key
        )
        item_schema = enriched["properties"][field_key]["items"]
        assert set(item_schema["properties"]) == set(editor_template)
        # yaml 原有的 additionalProperties 原样保留；不加 required（留白规则允许成员省略字段）
        assert item_schema["additionalProperties"] is True
        assert "required" not in item_schema
        # 原模板对象不被就地改写（每次请求都从它派生）
        assert "properties" not in template.structured_schema["properties"][field_key]["items"]


def test_template_value_types_map_to_schema_types() -> None:
    template = load_prompt_templates()["snowflake_generate_character_bibles"].structured_schema
    enriched, _ = enrich_structured_schema(template, step_key="character_bibles")
    props = enriched["properties"]["characters"]["items"]["properties"]
    assert props["display_name"] == {"type": "string"}
    assert props["personality_profile"]["type"] == "object"
    assert props["personality_profile"]["properties"]["preferences"] == {"type": "array", "items": {"type": "string"}}
    assert props["psychological_profile"]["properties"]["character_arc"] == {"type": "string"}

    template = load_prompt_templates()["snowflake_generate_character_sheets"].structured_schema
    enriched, _ = enrich_structured_schema(template, step_key="character_sheets")
    props = enriched["properties"]["characters"]["items"]["properties"]
    assert props["values"] == {"type": "array", "items": {"type": "string"}}

    template = load_prompt_templates()["snowflake_generate_long_synopsis"].structured_schema
    enriched, _ = enrich_structured_schema(template, step_key="long_synopsis")
    props = enriched["properties"]["chapters"]["items"]["properties"]
    assert props["chapter_seq"] == {"type": "integer"} and props["act"] == {"type": "integer"}


def test_coach_patch_and_triage_repair_patch_are_enriched_too() -> None:
    templates = load_prompt_templates()
    enriched, applied = enrich_structured_schema(
        templates["snowflake_workspace_assistant"].structured_schema, step_key="character_sheets"
    )
    assert applied == ["candidate_patch"]
    patch = enriched["properties"]["candidate_patch"]
    assert patch["properties"]["protagonist_character_id"] == {"type": "string"}
    assert set(patch["properties"]["characters"]["items"]["properties"]) >= {"role", "goal", "values"}
    # 教练补丁里 brief_update 本来就有完整 schema，不动
    assert enriched["properties"]["brief_update"] == templates["snowflake_workspace_assistant"].structured_schema["properties"]["brief_update"]

    enriched, applied = enrich_structured_schema(
        templates["snowflake_scene_triage_suggest"].structured_schema,
        step_key="scene_details",
        template_name="snowflake_scene_triage_suggest",
    )
    assert applied == ["items_items_repair_patch"]
    repair = enriched["properties"]["items"]["items"]["properties"]["repair_patch"]
    assert {"goal", "conflict", "setback", "reaction", "dilemma", "decision", "summary"} <= set(repair["properties"])


def test_every_snowflake_template_reaches_the_wire_without_property_less_objects() -> None:
    """守卫：雪花家族的模板经补全后不再有「没有 properties 的对象」——新增模板要么给 properties，
    要么让 enrich_structured_schema 学会派生它。"""
    templates = load_prompt_templates()
    step_by_template = {f"snowflake_generate_{step}": step for step in (
        "book_brief", "one_sentence_summary", "one_paragraph_summary", "character_sheets", "short_synopsis",
        "character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details",
    )}
    step_by_template["snowflake_workspace_assistant"] = "character_sheets"
    step_by_template["snowflake_scene_triage_suggest"] = "scene_details"
    step_by_template["snowflake_chapter_plan_suggest"] = None
    step_by_template["snowflake_step_candidates"] = None

    def open_objects(node, path=""):
        found = []
        if isinstance(node, dict):
            if node.get("type") == "object" and not node.get("properties"):
                found.append(path or "<root>")
            for key, child in (node.get("properties") or {}).items():
                found += open_objects(child, f"{path}.{key}" if path else key)
            if isinstance(node.get("items"), dict):
                found += open_objects(node["items"], f"{path}[]")
        return found

    offenders = {}
    for name, step_key in step_by_template.items():
        enriched, _ = enrich_structured_schema(templates[name].structured_schema, step_key=step_key, template_name=name)
        holes = open_objects(enriched)
        if holes:
            offenders[name] = holes
    assert not offenders, offenders


def test_unknown_step_and_missing_schema_degrade_to_no_op() -> None:
    schema = {"type": "object", "properties": {"characters": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}}
    enriched, applied = enrich_structured_schema(schema, step_key="no_such_step")
    assert applied == [] and enriched == schema
    assert enrich_structured_schema(None, step_key="character_sheets") == ({}, [])


def test_generate_route_sends_the_enriched_schema_and_records_it_in_the_audit(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    _install_fake(monkeypatch, [FULL_SHEETS], captured)
    pid = _create_project(client)

    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"direction_text": "主角沈何来是档案修复师，对手是恩师周砚。", "require_llm": True, "direction_kind": "candidate"},
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    wire = captured[0].response_schema["schema"]["properties"]["characters"]["items"]
    assert set(wire["properties"]) == {
        "character_id", "display_name", "role", "goal", "ambition", "values", "conflict", "epiphany",
        "one_sentence_summary", "one_paragraph_summary",
    }
    user_prompt = captured[0].messages[1]["content"]
    assert '"current_draft_how_to_use"' in user_prompt and "空着的字段正是本次要生成的目标" in user_prompt

    from novel_system.db.models import LlmCall
    from novel_system.db.session import SessionLocal

    session = SessionLocal()
    try:
        row = session.query(LlmCall).filter(LlmCall.node_id == "snowflake_step_generate").one()
        summary = row.request_payload_summary
        summary = json.loads(summary) if isinstance(summary, str) else summary
        assert summary["response_schema_enriched"] == ["characters_items"]  # 审计里原样可读，不被指纹化
    finally:
        session.close()


def test_current_draft_how_to_use_is_never_shed_by_the_prompt_budget() -> None:
    assert "current_draft_how_to_use" in PROTECTED_KEYS


# ---------------------------------------------------------------------------
# 2. 空成员：一次带原因的重试，再空就如实报错；`{}` 不铸幽灵 id
# ---------------------------------------------------------------------------


def test_empty_member_objects_trigger_one_reasoned_retry(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    sparse = {"protagonist_character_id": "c1", "characters": [{}, {}]}
    _install_fake(monkeypatch, [sparse, FULL_SHEETS], captured)
    pid = _create_project(client)

    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"direction_text": "主角沈何来是档案修复师，对手是恩师周砚。", "require_llm": True},
    )
    assert response.status_code == 200, response.text
    names = [item["display_name"] for item in response.json()["data"]["step"]["draft"]["characters"]]
    assert names == ["沈何来", "周砚"]

    assert len(captured) == 2
    assert "completeness_repair" not in captured[0].messages[1]["content"]
    repair_prompt = captured[1].messages[1]["content"]
    assert "completeness_repair" in repair_prompt
    assert "过于稀疏" in repair_prompt or "结果为空" in repair_prompt
    assert "blank slots in current_draft are what this step must write" in repair_prompt


def test_still_sparse_after_the_retry_fails_loudly_in_chinese(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    ids_only = {"protagonist_character_id": "c1", "characters": [{"character_id": "c1", "display_name": "沈何来"}]}
    _install_fake(monkeypatch, [ids_only, ids_only], captured)
    pid = _create_project(client)

    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/character_sheets/generate",
        json={"require_llm": True},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA"
    assert "过于稀疏" in error["message"]
    assert "too sparse" not in error["message"]
    assert len(captured) == 2  # 只重试一次


def test_empty_items_never_become_phantom_members() -> None:
    template = next(
        field["template"] for field in get_step_definition("character_sheets")["editor"]["fields"] if field["key"] == "characters"
    )
    assert _sanitize_character_items([{}, {}], template=template, project_id="PRJ", latest_by_step={}, base_items=[]) == []
    kept = _sanitize_character_items(
        [{}, {"display_name": "周砚", "role": "对手"}], template=template, project_id="PRJ", latest_by_step={}, base_items=[]
    )
    assert [item["display_name"] for item in kept] == ["周砚"]
    # 只有 id 的成员仍然保留：它是「补全」语义里的合法无操作（不清空既有内容）
    assert _sanitize_character_items([{"character_id": "c1"}], template=template, project_id="PRJ", latest_by_step={}, base_items=[])[0]["character_id"] == "c1"

    scene_template = next(
        field["template"] for field in get_step_definition("scene_list")["editor"]["fields"] if field["key"] == "scenes"
    )
    assert _sanitize_scene_list_items([{}, {}], template=scene_template, project_id="PRJ", base_items=[]) == []
    kept = _sanitize_scene_list_items([{}, {"summary": "她潜入库房"}], template=scene_template, project_id="PRJ", base_items=[])
    assert [item["summary"] for item in kept] == ["她潜入库房"]


# ---------------------------------------------------------------------------
# 3. 输出预算：节点默认值与 config/models.yaml 一致
# ---------------------------------------------------------------------------


def test_snowflake_step_generate_output_budget_matches_models_yaml() -> None:
    root = Path(__file__).resolve().parents[2]
    routing = load_model_routing_config(root / "config" / "models.yaml")
    spec = get_llm_node_spec("snowflake_step_generate")
    assert spec is not None
    assert spec.max_output_tokens == routing.task_routing["snowflake_step_generate"].max_output_tokens == 8192, (
        "系统设置同步进库的 node_routing 以节点默认值为准、且运行时优先于 task_routing——两处不一致时"
        "「一键补齐」过的安装会按较小的那个发请求"
    )


@pytest.mark.parametrize("template_name", ["snowflake_chapter_plan_suggest"])
def test_step_less_templates_carry_their_properties_in_yaml(template_name: str) -> None:
    schema = load_prompt_templates()[template_name].structured_schema
    assert set(schema["properties"]["assignments"]["items"]["properties"]) == {"scene_plan_id", "chapter_row_uid"}

"""提示词模板契约（``config/prompts.yaml``，表驱动；风格参考 v3 台账 U17）。

取代散在各测试文件里的「钉模板版本号」断言：版本号每改一次提示词就要动，钉它只证明「改过」，不证明「改对了」。
这里钉的是所有模板共同的结构约定，与关键模板里编码了行为的措辞。改提示词时仍然要升 ``version``——保存过提示词
快照的安装靠它（``tools.sync_prompt_templates``）判断库内快照该不该更新——但测试不再关心具体是哪一版。

1. 每个模板都有 ``version``（``YYYY-MM-DD.vN``）、正的 ``input_token_budget``、非空的 ``system_prompt`` /
   ``task_prompt`` 与 ``structured_schema``；
2. ``structured_schema`` 里每个「成员是对象的数组」都声明 ``properties``——按 schema 约束解码的中转（经中转的
   Gemini）对没声明 properties 的成员只解出 ``{}``（2026-09-16 角色表「过于稀疏」、准定稿评审的 ``findings = [{}]``
   都是这样来的）。雪花整步生成的集合模板在 yaml 里故意只写开放对象，由 ``enrich_structured_schema`` 在请求时按
   步骤编辑器补全，这里按补全后的 schema 判；
3. 分数字段（名字带 score、在 ``scores`` / ``dimension_scores`` 里）都声明 ``minimum`` / ``maximum``——代码按一次
   回答的量级换算，范围写明才能让模型不在 0–1 / 0–10 / 0–100 之间随意挑。项目的 schema 子集里
   ``additionalProperties`` 只能是布尔值（``prompt_builder._validate_structured_schema``），维度键由评审自定的开放
   ``scores`` 对象写不出每维的范围——这种模板必须在提示词里写明刻度（``from 0 to N``）；
4. 关键模板的行为措辞：必须有的与不许有的。风格通道模板一律点名 ``[风格样例]`` 块。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from novel_system.services.snowflake_workspace_llm import enrich_structured_schema

PROMPTS_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"
TEMPLATES: dict[str, dict[str, Any]] = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))["templates"]
TEMPLATE_NAMES = sorted(TEMPLATES)

VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.v\d+$")
SCALE_STATED_RE = re.compile(r"from 0 to (?:1|10|100)\b")
SNOWFLAKE_STEP_TEMPLATE_PREFIX = "snowflake_generate_"
# 风格通道：起草 / 改稿 / 评审节点，[STYLE_REFERENCE] 前缀与样例尾块都会进来
STYLE_PASS_TEMPLATES = (
    "style_first_draft",
    "style_draft",
    "style_targeted_revision",
    "style_length_patch",
    "style_salvage_patch",
    "scene_literary_rewrite",
    "soft_qc",
    "style_ref_check_judge",
    "chapter_near_final_review",
)
SAMPLE_BLOCK_NAMES = ("[风格样例]", "【风格样例】")


def _text(name: str) -> str:
    template = TEMPLATES[name]
    return f"{template['system_prompt']}\n{template['task_prompt']}"


def _types(schema: Mapping[str, Any]) -> set[str]:
    raw = schema.get("type")
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return {str(raw)} if raw else set()


def _walk(schema: Any, path: str = "$") -> Iterator[tuple[str, Mapping[str, Any]]]:
    """(路径, 子 schema)：properties / items / additionalProperties（为对象时）/ anyOf·oneOf·allOf 全部下钻。"""
    if not isinstance(schema, Mapping):
        return
    yield path, schema
    for key, child in (schema.get("properties") or {}).items():
        yield from _walk(child, f"{path}.{key}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        yield from _walk(items, f"{path}[]")
    extra = schema.get("additionalProperties")
    if isinstance(extra, Mapping):
        yield from _walk(extra, f"{path}.*")
    for combinator in ("anyOf", "oneOf", "allOf"):
        for index, option in enumerate(schema.get(combinator) or []):
            yield from _walk(option, f"{path}.{combinator}[{index}]")


def _wire_schema(name: str) -> dict[str, Any]:
    """请求时真正下发的 schema：雪花整步生成模板按步骤编辑器补全成员 properties，其余原样。"""
    schema = TEMPLATES[name]["structured_schema"]
    if name.startswith(SNOWFLAKE_STEP_TEMPLATE_PREFIX):
        enriched, _applied = enrich_structured_schema(
            schema, step_key=name[len(SNOWFLAKE_STEP_TEMPLATE_PREFIX):], template_name=name
        )
        return enriched
    return schema


def _is_score_field(path: str) -> bool:
    last = path.rsplit(".", 1)[-1]
    return "score" in last.lower() or ".scores." in path or ".dimension_scores." in path or path.endswith(".scores.*")


# ---------------------------------------------------------------------------
# 1–3. 所有模板共同的结构约定
# ---------------------------------------------------------------------------


# 每条规则一个用例、在用例里走完全部模板（conftest 给每个用例建一个 sqlite，逐模板参数化会让这个纯 yaml 检查
# 慢上几分钟）；失败信息按模板列出。


def test_every_template_declares_version_budget_prompts_and_schema() -> None:
    problems: dict[str, list[str]] = {}
    for name in TEMPLATE_NAMES:
        template = TEMPLATES[name]
        issues = []
        if not VERSION_RE.match(str(template.get("version") or "")):
            issues.append("version 必须是 YYYY-MM-DD.vN")
        budget = template.get("input_token_budget")
        if not (isinstance(budget, int) and budget > 0):
            issues.append("input_token_budget 必须是正整数")
        issues.extend(f"{key} 为空" for key in ("system_prompt", "task_prompt") if not str(template.get(key) or "").strip())
        schema = template.get("structured_schema")
        if not (isinstance(schema, Mapping) and "object" in _types(schema)):
            issues.append("structured_schema 必须是对象 schema")
        if issues:
            problems[name] = issues
    assert not problems, problems


def test_object_array_members_declare_properties() -> None:
    bare: dict[str, list[str]] = {}
    for name in TEMPLATE_NAMES:
        paths = [
            path
            for path, node in _walk(_wire_schema(name))
            if "array" in _types(node)
            and isinstance(node.get("items"), Mapping)
            and "object" in _types(node["items"])
            and not node["items"].get("properties")
        ]
        if paths:
            bare[name] = paths
    assert not bare, f"这些数组的成员是对象却没声明 properties——按 schema 约束解码的中转只会解出 {{}}：{bare}"


def test_score_fields_declare_their_range() -> None:
    unbounded: dict[str, list[str]] = {}
    for name in TEMPLATE_NAMES:
        paths = []
        for path, node in _walk(TEMPLATES[name]["structured_schema"]):
            types = _types(node)
            if (types & {"number", "integer"}) and _is_score_field(path):
                if "minimum" not in node or "maximum" not in node:
                    paths.append(path)
            elif "object" in types and path.rsplit(".", 1)[-1] in ("scores", "dimension_scores"):
                if not node.get("properties") and SCALE_STATED_RE.search(_text(name)) is None:
                    paths.append(f"{path} (开放的分数对象，提示词也没写明刻度)")
        if paths:
            unbounded[name] = paths
    assert not unbounded, f"这些分数没写范围（minimum / maximum）：{unbounded}"


def test_the_contract_walks_every_known_score_family() -> None:
    """自检：上面的分数规则确实看到了评审模板的分数（防止改名后规则静默失效）。"""
    seen = {
        name
        for name in TEMPLATE_NAMES
        for path, node in _walk(TEMPLATES[name]["structured_schema"])
        if (_types(node) & {"number", "integer"}) and _is_score_field(path)
    }
    assert {
        "soft_qc",
        "style_ref_check_judge",
        "near_final_acceptance_review",
        "chapter_near_final_review",
        "writer_deep_review",
    } <= seen


# ---------------------------------------------------------------------------
# 4. 关键模板的行为措辞
# ---------------------------------------------------------------------------

# 模板 → (必须出现的措辞, 不许出现的措辞)。措辞是行为的载体：删掉一句就等于改了行为，应当在这里看得见。
BEHAVIOUR: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # 作者手笔直起：设计只是框架；文风卡「必须」项每场体现；蓝图只给事实；样例在 user 尾部
    "style_first_draft": (
        (
            "the plot framework only",
            "Every item the style card marks 必须 (must) shows up in this scene",
            "It never supplies a line to write",
            "[风格样例] block at the end of the user message",
            "never copy a sample sentence",
        ),
        ("never imitate their length", "[禁止复刻]", "UNTRUSTED_REFERENCE_DATA", "even when the reference author uses another"),
    ),
    # 风格稿：中性稿重写 / 作者手笔首稿细改两种口径；作者尺度优先于样例长度
    "style_draft": (
        (
            "First Draft (already in the reference author's hand)",
            "a sample's length is never the target length",
            "Never wash the voice back toward a neutral register",
            "[风格样例] block at the end of the user message",
        ),
        ("never imitate their length", "[禁止复刻]", "UNTRUSTED_REFERENCE_DATA", "even when the reference author uses another"),
    ),
    # 定向修改：只动越界的维，测得的差异不是配额，像作者的句子一字不动
    "style_targeted_revision": (
        (
            "Dimensions To Move Toward The Author",
            "never a quota",
            "Every sentence that already sounds like the author stays exactly as it is",
        ),
        ("never imitate their length", "[禁止复刻]"),
    ),
    # 软 QC = 参考评审：不带房风规则，16 维 0–10
    "soft_qc": (
        (
            "Do not bring a house rubric of your own",
            "dimension_scores",
            "from 0 to 10",
            "Emotional clarity is a goal only when no [STYLE_REFERENCE] block is present",
        ),
        ("[禁止复刻]",),
    ),
    # 对照检查的参考评审：0–10、不引用样例
    "style_ref_check_judge": (
        ("from 0 to 10", "never use a 0–1 or 0–100 scale", "never quote the samples"),
        (),
    ),
    # 场景验收：场景三问、0–10、简报三键；无风格块时才守房风
    "near_final_acceptance_review": (
        (
            "Always fill scene_story_check",
            "never use a 0–1 or 0–100 scale",
            "target (what to change), issue (what is wrong), fix_direction (how to fix it)",
            "If no [STYLE_REFERENCE] block is present, do not pass scenes",
        ),
        (),
    ),
    # 章级验收：按参考作者的开合判；0–10；简报三键
    "chapter_near_final_review": (
        ("never use a 0–1 or 0–100 scale", "target, issue, fix_direction", "章首 / 章末"),
        (),
    ),
    # 事实 QC：写法从来不是硬违规
    "hard_qc": (("never a hard violation", "its facts are bundle facts"), ()),
    # 有绑定时的事实版蓝图：只写事实与场面标签，不写台词、不定收尾
    "scene_blueprint_facts": (
        ("situation_tags", "Never write prose or dialogue", "never a line of prose or dialogue"),
        (),
    ),
    # 段落分类 v3：去掉「短段默认过渡」，前后一段只读上下文
    "style_ref_paragraph_classify_anchor": (
        ("段落长短不决定类型", "context_before", "context_after"),
        ("默认 transition", "仅看当前段本身"),
    ),
    "style_ref_paragraph_classify_bulk": (
        ("段落长短不决定类型", "context_before", "context_after"),
        ("默认 transition", "仅看当前段本身"),
    ),
    # 文风卡合成：全面模仿、对账、不写数字；不再要求卡片重述声音习惯
    "style_ref_synthesize_profile": (
        ("全面模仿", "对账", "voice_habits", "不写阿拉伯数字"),
        ("不得逐字抄写这些行",),
    ),
    # 写作台深评：证据逐字、≤80 字、按参考作者判、0–1
    "writer_deep_review": (
        ("evidence_excerpt is a verbatim quote", "at most 80 characters", "judge in the reference author's hand", "from 0 to 1"),
        (),
    ),
}
for _layer in ("language", "narrative", "scene", "theme"):
    # 分层抽取：全面模仿口径、带「模型默认写法」对照与手法名
    BEHAVIOUR[f"style_ref_extract_{_layer}"] = (("全面模仿", "model_default", "devices"), ("不得引用、复述",))


def test_key_templates_carry_their_behaviour() -> None:
    problems: dict[str, dict[str, list[str]]] = {}
    for name, (required, forbidden) in sorted(BEHAVIOUR.items()):
        text = _text(name)
        missing = [phrase for phrase in required if phrase not in text]
        present = [phrase for phrase in forbidden if phrase in text]
        if missing or present:
            problems[name] = {"缺少的行为措辞": missing, "出现了已废弃的措辞": present}
    assert not problems, problems


def test_style_pass_templates_name_the_sample_block() -> None:
    silent = [name for name in STYLE_PASS_TEMPLATES if not any(block in _text(name) for block in SAMPLE_BLOCK_NAMES)]
    assert not silent, f"风格通道模板必须点名样例块：{silent}"
    assert not [name for name in STYLE_PASS_TEMPLATES if "never imitate their length" in _text(name)]


def test_behaviour_table_only_names_existing_templates() -> None:
    assert set(BEHAVIOUR) <= set(TEMPLATES)
    assert set(STYLE_PASS_TEMPLATES) <= set(TEMPLATES)

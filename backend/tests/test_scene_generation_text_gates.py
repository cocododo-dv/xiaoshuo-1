"""场景生成 · 确定性文本门（services/scene_generation/text_gates.py）：禁用词只按 qc_constraints 拆、取正文时修双重转义、
回包残留标记、去模板与安全修复的不回退判定（X04-19，自 test_scene_generation 拆出）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.db.models import SceneCard
from novel_system.services.llm_client import LLMResponse
from novel_system.services.scene_generation import (
    LengthPolicy,
    _assess_de_template_rewrite,
    _assess_neutral_draft,
    _assess_style_base_rewrite,
    _extract_scene_text,
    _neutral_repair_brief,
    _scene_text_integrity_markers,
)
from tests.support.scene_generation import seed_generation_scene as _seed_scene


_LEGACY_POLICY_SENTENCE = "不得复制参考书原文表达、人物、设定或桥段。"


def test_legacy_reference_policy_sentence_is_not_a_forbidden_word_list() -> None:
    """B04-05：旧卡的 forbidden_text 还带着防抄袭政策句——那不是禁用词表，「人物」不是禁用词；
    中性稿验收、修复简报与两个改写验收都只经 qc_constraints.forbidden_terms 读这个字段。"""
    scene = SimpleNamespace(
        scene_id="SC_POLICY",
        chapter_id="CH_POLICY",
        must_include_text="",
        forbidden_text=_LEGACY_POLICY_SENTENCE,
        target_length_band=None,
    )
    content = "这号人物站在雨里，手里攥着那封旧信，一句话也没说。" * 3
    source = "他站在雨里，手里什么也没拿，一句话也没说。" * 3

    assessment = _assess_neutral_draft(scene, content, LengthPolicy.plain(scene))
    assert "forbidden_content_present" not in assessment["reasons"]
    assert assessment["forbidden_hit_count"] == 0
    assert "人物" not in _neutral_repair_brief(scene, source_content=content, assessment=assessment)
    base = _assess_style_base_rewrite(
        scene=scene, source_content=source, rewritten_content=content, lengths=LengthPolicy.plain(scene)
    )
    assert "forbidden_content_added" not in base["reasons"]
    rewrite = _assess_de_template_rewrite(
        scene=scene,
        lengths=LengthPolicy.plain(scene),
        source_content=source,
        rewritten_content=content,
        source_quality_gate={"score": 0.0, "findings": []},
    )
    assert "forbidden_content_added" not in rewrite["reasons"]

    # 作者真写的禁用词照样拦——哪怕接在政策句后面
    scene.forbidden_text = _LEGACY_POLICY_SENTENCE + "旧信"
    assessment = _assess_neutral_draft(scene, content, LengthPolicy.plain(scene))
    assert "forbidden_content_present" in assessment["reasons"]
    assert "旧信" in _neutral_repair_brief(scene, source_content=content, assessment=assessment)
    assert "forbidden_content_added" in _assess_style_base_rewrite(
        scene=scene, source_content=source, rewritten_content=content, lengths=LengthPolicy.plain(scene)
    )["reasons"]


def test_scene_card_forbidden_text_is_only_split_by_qc_constraints() -> None:
    """B04-05 守卫：场景卡 forbidden_text 只经 qc_constraints.forbidden_terms / strip_reference_policy 读——
    别处不许用 constraint_terms(...) 直接拆它（防抄袭政策句会被拆成「人物」这样的假禁用词）。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "novel_system"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "qc_constraints.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "constraint_terms" and "forbidden" in ast.unparse(node):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


def test_extract_scene_text_normalizes_double_encoded_unicode_fragments() -> None:
    payload = {"scene_text": "她走进雨\\u6ccc\\u4e2d，神色依u7136平静。"}
    response = LLMResponse(
        request_id="resp_unicode_normalize",
        provider="fake-provider",
        model="fake-model",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={"id": "resp_unicode_normalize", "usage": {}},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    assert _extract_scene_text(response) == "她走进雨泌中，神色依然平静。"


@pytest.mark.parametrize(
    "text,marker",
    [
        ("```json\n{\"scene_text\": \"她走了。\"}\n```", "model_response_artifact"),
        ("Let me refine the final JSON before returning it.", "model_response_artifact"),
        ("他停住；C季青却没有回头。", "orphan_ascii_before_cjk"),
    ],
)
def test_scene_text_integrity_rejects_provider_response_artifacts(
    text: str, marker: str
) -> None:
    assert marker in _scene_text_integrity_markers(text)


def test_safety_repair_does_not_reject_safe_text_for_quality_score_drop(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    rewritten = (
        "她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听了片刻，"
        "随即关灯，把信封收进抽屉。"
    )

    assessment = _assess_de_template_rewrite(
        scene=scene,
        lengths=LengthPolicy.plain(scene),
        source_content="红色信封在她手中。",
        authoritative_content="她接过红色信封，确认门外有人，随后把信封收好。",
        rewritten_content=rewritten,
        source_quality_gate={"score": 1.0, "findings": []},
        style_conformance={
            "available": True,
            "comparable": True,
            "regressed": True,
        },
    )

    assert assessment["accepted"] is True
    assert assessment["reasons"] == []
    assert assessment["quality_non_regression_enforced"] is False
    assert assessment["style_non_regression_enforced"] is False


def test_ordinary_de_template_rejects_measurable_frozen_style_regression(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    source = "她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关灯。"
    rewritten = "她把红色信封放在桌角。门外有人。她听了一会儿，然后关灯。"

    assessment = _assess_de_template_rewrite(
        scene=scene,
        lengths=LengthPolicy.plain(scene),
        source_content=source,
        rewritten_content=rewritten,
        source_quality_gate={"score": 0.0, "findings": []},
        style_conformance={
            "available": True,
            "comparable": True,
            "regressed": True,
        },
    )

    assert assessment["accepted"] is False
    assert "style_conformance_regressed" in assessment["reasons"]
    assert assessment["style_non_regression_enforced"] is True


def test_ordinary_de_template_requires_actionable_target_defect_reduction(
    session,
    monkeypatch,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    unchanged_gate = {
        "triggered": True,
        "score": 0.4,
        "risk_dimensions": ["model_voice"],
        "findings": [{"dimension": "model_voice"}],
    }
    monkeypatch.setattr(
        "novel_system.services.scene_generation.text_gates._anti_template_quality_gate",
        lambda *args, **kwargs: unchanged_gate,
    )

    assessment = _assess_de_template_rewrite(
        scene=scene,
        lengths=LengthPolicy.plain(scene),
        source_content="她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关灯。",
        rewritten_content="她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关了灯。",
        source_quality_gate=unchanged_gate,
    )

    assert assessment["accepted"] is False
    assert "target_quality_defects_not_reduced" in assessment["reasons"]
    assert assessment["source_target_evidence_available"] is True

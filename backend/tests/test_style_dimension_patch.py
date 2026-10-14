"""2026-10-14 评审统一第 2 项：软 QC 的评审判出不像的维时，补丁改成按维整场修改。

实库：评审说 CH01_SC05 「通篇缺作者的自嘲吐槽」，而旧补丁「只动简报点名的句子、其余逐字保留、温度 0.3」——贯穿全篇的
口吻改不动。现在：评审判出的维（对白、情感基调……）→ 整场范围的按维修改（这几维的评审说明与改法、读数差异、文风卡句，
样例窗换成示范这几维的窗，正常温度）；只有确定性问题时照旧局部补丁。留不留由成对比较定（见 test_style_fidelity_pipeline）。

全部合成数据；无真实模型、无真实作者原文。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from novel_system.db.models import AttemptTracker, QcReport
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_generation import style_first as SF
from novel_system.services.style_reference import style_step as S
from tests.support.llm_fakes import ScriptedStepRunner
from tests.support.style_first_fixtures import bound_scene

DRAFT = "窗外的雨下了一整夜，他把茶杯推到桌角，信封就压在杯底。" * 30
PATCHED = "雨下了一夜。他把茶杯往桌角一推，信封压在杯底，谁也没去碰。" * 30


def _report(session, scene, *, judged: bool, key: str) -> QcReport:
    issues = [{"issue_key": "length_band", "message": "篇幅偏短"}]
    brief = ["把第三段补足到设计要求的长度。"]
    entries: list[dict] = []
    if judged:
        issues = [
            {"issue_key": "style_reference.scene.dialogue", "message": "对白太正式"},
            {"issue_key": "style_reference.theme.emotional_tone", "message": "通篇沉重"},
            *issues,
        ]
        brief = [
            "scene.dialogue（对话写法，high）：第二段的盘问改成互相拆台的口吻",
            "theme.emotional_tone（情感基调，medium）：危急处先来一句自嘲再落回现实",
            *brief,
        ]
        entries = [
            {
                "kind": "reference_judge",
                "scale": "0-10",
                "overall": 2.0,
                "calibration": {"state": "calibrated"},
                "pass_score": 6.0,
                "dimensions": {
                    "scene.dialogue": {"score": 1.1, "raw": 3.5, "note": "对白太正式", "fix": "第二段的盘问改成互相拆台的口吻"},
                    "theme.emotional_tone": {"score": 2.0, "raw": 4.5, "note": "通篇沉重", "fix": "危急处先来一句自嘲再落回现实"},
                },
            }
        ]
    report = QcReport(
        qc_report_id=f"qc_dimpatch_{key}",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        qc_type="soft_qc",
        resolution_code="soft_patch",
        pass_flag=0,
        next_action="patch",
        issues_json=issues,
        rewrite_brief_json=[*({"instruction": line} for line in brief), *entries],
    )
    session.add(report)
    session.flush()
    return report, brief


def _patch(session, key: str, *, judged: bool):
    scene, bundle, _book, _profile = bound_scene(session, key)
    report, brief = _report(session, scene, judged=judged, key=key)
    session.commit()
    runner = ScriptedStepRunner(outputs={"soft_patch": PATCHED}, default=PATCHED)
    service = SceneGenerationService(session, llm_runner=runner)
    result = service.generate_style_patch(
        scene.scene_id,
        bundle,
        source_style_draft_row_id=f"draft_style_{scene.scene_id}",
        source_style_content=DRAFT,
        rewrite_brief=brief,
        source_qc_report_id=report.qc_report_id,
        execution_step_key="soft_patch:soft_qc:0",
    )
    session.commit()
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "soft_patch")
    ).scalars().first()
    return runner, result, attempt


def test_judged_dimensions_turn_the_soft_patch_into_a_scene_wide_dimension_revision(session) -> None:
    runner, result, attempt = _patch(session, "dimpatch_judged", judged=True)
    call = runner.calls[-1]
    assert call["node_id"] == "style_patch" and call["step"] == "soft_patch"
    user = call["user_prompt"]
    assert "voice revision across the whole scene, not a local patch" in user
    assert "do not rewrite the full scene" not in user and "Keep every sentence the brief does not name verbatim" not in user
    assert "## Dimensions To Move Toward The Author" in user
    assert "对话写法（scene.dialogue）：评审 1.1 / 10——对白太正式" in user
    assert "改法：第二段的盘问改成互相拆台的口吻" in user
    # 评审之外的条目（确定性问题）照旧逐条给；评审那两条只在维度段里出现一次
    assert "把第三段补足到设计要求的长度。" in user and user.count("互相拆台的口吻") == 1
    assert call["temperature_override"] is None, "整场修改用正常温度"
    audit = call["prompt"]["_style_reference_runtime_audit"]
    assert audit["role"] == "revise"
    assert audit["request"]["revise_dimensions"] == ["scene.dialogue", "theme.emotional_tone"], "样例窗换成示范这几维的窗"
    assert result.content == PATCHED
    assert attempt.details_json["patch_mode"] == SF.PATCH_MODE_DIMENSION
    assert attempt.details_json["revise_dimensions"] == ["scene.dialogue", "theme.emotional_tone"]


def test_only_deterministic_issues_keep_the_local_low_temperature_patch(session) -> None:
    runner, _result, attempt = _patch(session, "dimpatch_local", judged=False)
    call = runner.calls[-1]
    assert "do not rewrite the full scene" in call["user_prompt"]
    assert "## Dimensions To Move Toward The Author" not in call["user_prompt"]
    assert call["temperature_override"] == 0.3
    assert attempt.details_json["patch_mode"] == SF.PATCH_MODE_LOCAL and "revise_dimensions" not in attempt.details_json


def test_the_plan_reads_the_judged_dimensions_in_patch_order_and_the_brief_names_them() -> None:
    report = SimpleNamespace(
        issues_json=[
            {"issue_key": "style_reference.narrative.pacing"},
            {"issue_key": "scene_shape"},
            {"issue_key": "style_reference.scene.dialogue"},
            {"issue_key": "style_reference.narrative.pacing"},
        ]
    )
    assert SF.reference_judge_dimensions(report) == ["narrative.pacing", "scene.dialogue"]
    assert SF.reference_judge_dimensions(None) == []
    assert SF.dimension_patch_plan(
        None, policy=SimpleNamespace(style_first=False), report=report, source_content="", rewrite_brief=[], scene_id="x"
    ) is None, "中性稿起草（对照组）照旧局部补丁"
    text = S.judge_revision_brief_sections(
        dimensions=["scene.dialogue"],
        judged={"scene.dialogue": {"score": 2.0, "note": "太正式", "fix": "改成拆台"}},
        differences=[],
        card_lines={"scene.dialogue": ["对白里人物互相拆台"]},
    )
    assert text.splitlines() == [
        "## Dimensions To Move Toward The Author",
        "- 对话写法（scene.dialogue）：评审 2.0 / 10——太正式",
        "  改法：改成拆台",
        "",
        "## Style Card Lines For These Dimensions",
        "- 对话写法：对白里人物互相拆台",
    ]

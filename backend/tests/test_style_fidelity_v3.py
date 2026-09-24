"""风格参考 v3 · P5b：读数入库、风格步按读数决定（L1）、补丁不更像就退回（N6 / V7）、对照检查（V6）、读数接口（N5）。

契约 docs/style-reference-v3-2026-09-23.md §2.4 / §3.1。全部是合成文本（无真实参考书原文 / 作者作品里的人名）；模型一律是
测试替身。读数的具体数值由 ``_install_readings`` 按文字里的记号给定（决定逻辑要可控）；一处用真实测量核读数验证接线。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    StyleFidelityReading,
    StyleReferenceJob,
)
from novel_system.db.session import SessionLocal
from novel_system.services import scene_generation as sg
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_reference import readings as R
from novel_system.services.style_reference import style_step as S
from novel_system.services.style_reference.fidelity import FidelityReading
from tests.style_reference_inject_helpers import bind, seed_reference
from tests.test_style_first_draft import _Runner, _frozen_bundle

LONG_FIRST = ("窗外的雨下了一整夜，他把茶杯推到桌角，信封就压在杯底。" * 30)
REVISED = ("雨下了一夜。他把茶杯往桌角一推，信封压在杯底，谁也没去碰。" * 30)
OTHER_REVISED = ("那一夜的雨没停过，他推开茶杯，杯底压着信封，像压着一句没说的话。" * 30)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _reading(
    distance: float,
    percentile: float,
    *,
    reliable: bool = True,
    out_of_band: list[dict] | None = None,
    emphasized: tuple[str, ...] = (),
    chars: int = 1200,
) -> FidelityReading:
    return FidelityReading(
        distance=distance,
        percentile=percentile,
        out_of_band=list(out_of_band or []),
        dimension_scores={"narrative.pacing": 6.0, "language.punctuation": 8.5},
        feature_z={},
        char_count=chars,
        window_count=28,
        reliable=reliable,
        kernel_version="measure_v1",
        reference_version="ref_test",
        emphasized_dimensions=emphasized,
    )


PACING_OUT = [
    {
        "feature": "para_len_mean",
        "dimension": "narrative.pacing",
        "z": 3.1,
        "direction": "high",
        "phrase": "段落比作者长，换段太少",
        "value": 180.0,
        "author_typical": 60.0,
    }
]


def _install_readings(monkeypatch, table: dict[str, FidelityReading], default: FidelityReading | None = None) -> None:
    """``readings.reading_for_text`` 的替身：文字里含哪个记号就给哪个读数（未绑定照旧 None）。"""

    def fake(session, policy, text):  # noqa: ANN001
        if policy is None or not getattr(policy, "bound", False) or not str(text or "").strip():
            return None
        for marker, reading in table.items():
            if marker in text:
                return reading
        return default

    monkeypatch.setattr(R, "reading_for_text", fake)


def _seed_scene(session, *, project_id: str, scene_id: str, chapter_id: str, band: str = "short", must: str = "信封") -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="读数", outline_text=""))
    session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g"))
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="茶馆",
        scene_goal="把信交出去",
        beats_json=["到场"],
        must_include_text=must,
        target_length_band=band,
        scene_type="reveal",
        is_chapter_last=0,
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
    session.commit()
    return scene


def _bound_scene(session, key: str, *, draft_mode: str = "style_first", card: bool = True, **scene_kwargs):
    project_id = f"proj_{key}"
    book_id, profile_id = seed_reference(session, key, card=card, chapters=14, per_chapter=100)
    bind(
        session,
        profile_id,
        binding_id=f"bind_{key}",
        scope="project",
        scope_ref_id=project_id,
        config_json={"draft_mode": draft_mode},
    )
    scene = _seed_scene(session, project_id=project_id, scene_id=f"{key.upper()}_SC01", chapter_id=f"{key.upper()}_CH", **scene_kwargs)
    bundle = _frozen_bundle(project_id, scene.scene_id, scene.chapter_id)
    return scene, bundle, book_id, profile_id


class _SeqRunner(_Runner):
    """按调用次序给正文（同一步位的几次调用给不同的稿子）。"""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(outputs={}, default=texts[-1])
        self.texts = list(texts)

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return SimpleNamespace(
            llm_call_id=f"llm_call_seq_{len(self.calls)}",
            response=SimpleNamespace(structured_output={"scene_text": text}),
        )

    def task_config(self, node_id):  # noqa: ANN001
        return SimpleNamespace(temperature=0.7)


def _readings(session, scene_id: str) -> list[StyleFidelityReading]:
    return list(
        session.scalars(
            select(StyleFidelityReading)
            .where(StyleFidelityReading.scene_id == scene_id)
            .order_by(StyleFidelityReading.created_at)
        )
    )


def _style_attempt(session, scene_id: str) -> AttemptTracker:
    return session.execute(
        select(AttemptTracker).where(
            AttemptTracker.scene_id == scene_id,
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()


# ---------------------------------------------------------------------------
# L1 · 风格步按读数决定
# ---------------------------------------------------------------------------


def test_first_draft_within_range_is_the_style_draft_without_a_model_call(session, monkeypatch) -> None:
    scene, bundle, _book, profile_id = _bound_scene(session, "fid_accept")
    runner = _Runner(outputs={}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(monkeypatch, {}, default=_reading(0.9, 42.0))

    products: list[tuple[str, dict]] = []
    result = service.generate_style_draft(
        scene.scene_id,
        bundle,
        neutral_draft_row_id=first.row_id,
        neutral_content=first.content,
        product_callback=lambda _slot, phase, _res, meta: products.append((phase, meta)),
    )
    session.commit()

    assert len(runner.calls) == 1, "读数在作者范围内：风格步不调模型"
    assert result.content == first.content and result.lineage == sg.LINEAGE_FIRST_DRAFT_ACCEPTED
    assert result.llm_call_id == first.llm_call_id and result.execution_step_key == "neutral_draft"
    row = session.get(SceneDraft, result.row_id)
    assert row.stage == "style_draft" and row.content == first.content
    assert row.generation_llm_call_id == first.llm_call_id
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["content_source"] == S.CONTENT_SOURCE_FIRST_DRAFT_ACCEPTED
    assert details["style_step"]["decision"] == S.DECISION_FIRST_DRAFT_ACCEPTED
    assert details["style_step"]["reason"] == S.REASON_WITHIN_RANGE and details["style_step"]["llm_call"] is False
    codes = [item["code"] for item in details["notices"]]
    assert sg.STYLE_NOTICE_FIRST_DRAFT_ACCEPTED in codes
    # 检查点回调：基稿即终稿，房风门让位、不跑去模板
    assert [phase for phase, _meta in products] == ["base", "final"]
    final_meta = products[-1][1]
    assert final_meta["gate_decision"]["triggered"] is False
    assert final_meta["gate_decision"]["style_step"]["decision"] == S.DECISION_FIRST_DRAFT_ACCEPTED
    assert final_meta["de_template_outcome"] == {"status": "not_required"}
    rows = _readings(session, scene.scene_id)
    assert [(r.source, r.stage, r.draft_ref) for r in rows] == [("pipeline", "first_draft", first.row_id)]
    assert rows[0].profile_id == profile_id and rows[0].percentile == 42.0
    assert rows[0].reading_json["within_range"] is True
    # API 回读：同一次运行的 notices 里有「首稿即风格稿」
    assert sg.STYLE_NOTICE_FIRST_DRAFT_ACCEPTED in [
        item["code"] for item in sg.latest_style_notices(session, scene.scene_id, bundle_id=bundle["bundle_id"])
    ]


def test_short_first_draft_is_unreliable_and_kept_with_a_real_reading(session) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_short")
    short = "脚步在门外停了；他将信封搁到桌上——也不说话，只等着。她终于没有去接。"
    runner = _Runner(outputs={}, default=short)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    assert len(runner.calls) == 1 and result.content == short
    step = _style_attempt(session, scene.scene_id).details_json["style_step"]
    assert step["reason"] == S.REASON_READING_UNRELIABLE
    rows = _readings(session, scene.scene_id)
    assert len(rows) == 1 and rows[0].stage == "first_draft"
    # 真实测量核读数：合成参考书有足够的窗口，文字太短 → 不可靠
    assert rows[0].reading_json["reliable"] is False and rows[0].reading_json["window_count"] >= 8
    assert rows[0].percentile is not None and rows[0].distance is not None


def test_targeted_revision_is_kept_when_it_moves_closer(session, monkeypatch) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_keep")
    runner = _Runner(outputs={"style_draft": REVISED}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(1.20, 71.0)},
    )

    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    call = runner.calls[-1]
    assert call["node_id"] == "style_draft" and call["step"] == "style_draft"
    assert call["execution_step_key"] == "style_draft:0"
    assert call["prompt"]["template_name"] == "style_targeted_revision"
    user = call["user_prompt"]
    assert f"## {sg.FIRST_DRAFT_SOURCE_LABEL}" in user and LONG_FIRST[:20] in user
    assert "## Dimensions To Move Toward The Author" in user and "节奏控制（narrative.pacing）" in user
    assert "段落比作者长，换段太少（作者一般约六十字，这一稿约一百八十字）" in user
    assert "危急时把时间切成倒计时推着走" in user, "这一维的文风卡句进了提示"
    audit = call["prompt"]["_style_reference_runtime_audit"]
    assert audit["role"] == "revise" and audit["request"]["revise_dimensions"] == ["narrative.pacing"]
    assert "这次修改唯一的文风权威" in user, "样例块按改稿口径落在 user 尾部"
    assert result.content == REVISED and result.lineage is None
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["content_source"] == S.CONTENT_SOURCE_TARGETED_REVISION
    assert details["style_step"]["decision"] == S.DECISION_REVISION_KEPT
    assert details["style_step"]["reason"] == S.REASON_CLOSER and details["style_step"]["dimensions"] == ["narrative.pacing"]
    assert sg.STYLE_NOTICE_REVISION_REJECTED not in [item["code"] for item in details["notices"]]
    stages = [(r.stage, r.distance) for r in _readings(session, scene.scene_id)]
    assert stages == [("first_draft", 1.40), ("revision", 1.20)]


def test_targeted_revision_that_is_not_closer_keeps_the_first_draft(session, monkeypatch) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_reject")
    runner = _Runner(outputs={"style_draft": REVISED}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    # 只近了 0.01（< revision_min_improvement 0.03）
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(1.39, 96.0)},
    )

    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    assert len(runner.calls) == 2
    assert result.content == first.content, "不更像就保留首稿（永不越改越远）"
    row = session.get(SceneDraft, result.row_id)
    assert row.stage == "style_draft" and row.generation_llm_call_id == "llm_call_sfd_2"
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["content_source"] == S.CONTENT_SOURCE_REVISION_NOT_CLOSER
    assert details["style_step"]["decision"] == S.DECISION_REVISION_REJECTED
    assert details["style_step"]["reason"] == S.REASON_NOT_CLOSER
    rejected = session.get(SceneDraft, details["rejected_candidate_row_id"])
    assert rejected.stage == "style_rejected" and rejected.status == "rejected" and rejected.content == REVISED
    notice = next(item for item in details["notices"] if item["code"] == sg.STYLE_NOTICE_REVISION_REJECTED)
    assert notice["severity"] == "info" and notice["reason"] == S.REASON_NOT_CLOSER
    revision_row = next(r for r in _readings(session, scene.scene_id) if r.stage == "revision")
    assert revision_row.draft_ref == rejected.row_id and revision_row.distance == 1.39


def test_targeted_revision_that_copies_the_reference_is_rejected_even_when_closer(session, monkeypatch) -> None:
    scene, bundle, book_id, _profile = _bound_scene(session, "fid_copy")
    from novel_system.db.models import StyleReferenceParagraph

    source = next(
        p.text
        for p in session.scalars(
            select(StyleReferenceParagraph)
            .where(StyleReferenceParagraph.book_id == book_id)
            .order_by(StyleReferenceParagraph.paragraph_index)
        )
        if len(p.text) >= 24 and "第" not in p.text[:2]
    )
    copied = REVISED + source
    runner = _Runner(outputs={"style_draft": copied}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(0.80, 20.0)},
    )

    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    assert result.content == first.content
    step = _style_attempt(session, scene.scene_id).details_json["style_step"]
    assert step["reason"] == S.REASON_COPY_BLOCKED and step["copy_check"]["blocked"] is True
    revision_row = next(r for r in _readings(session, scene.scene_id) if r.stage == "revision")
    assert revision_row.copy_check_json["blocked"] is True and revision_row.copy_check_json["hits"] >= 1
    assert source not in json.dumps(revision_row.copy_check_json, ensure_ascii=False), "抄袭门结果只记计数"


def test_targeted_revision_losing_a_required_fact_keeps_the_first_draft(session, monkeypatch) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_unsafe")
    lost = REVISED.replace("信封", "东西")
    runner = _Runner(outputs={"style_draft": lost}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(0.5, 10.0)},
    )
    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    assert result.content == first.content
    step = _style_attempt(session, scene.scene_id).details_json["style_step"]
    assert step["reason"] == S.REASON_BASE_UNSAFE and step["base_safety_accepted"] is False


def test_neutral_first_keeps_the_old_restyle_template_and_reads_both_drafts(session, monkeypatch) -> None:
    """对照组（neutral_first）仍走旧的「改成作者手笔」模板；2026-09-24（S2 c）起有绑定时改完读一次读数，
    与中性稿比——更像就采用（下面），不更像就交付中性稿（test_style_first_draft 的 S2 块）。"""
    scene, bundle, _book, _profile = _bound_scene(session, "fid_neutral", draft_mode="neutral_first")
    runner = _Runner(outputs={"style_draft": REVISED}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(monkeypatch, {"窗外的雨": _reading(1.40, 97.0), "雨下了一夜": _reading(1.10, 60.0)})
    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    call = next(item for item in runner.calls if item["step"] == "style_draft")
    assert call["prompt"]["template_name"] == "style_draft"
    assert "## Approved Neutral Draft" in call["user_prompt"]
    assert result.content == REVISED
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["content_source"] == "provider_style_output"
    assert details["style_step"]["decision"] == S.DECISION_REVISION_KEPT
    assert details["style_step"]["reason"] == S.REASON_CLOSER and details["style_step"]["draft_mode"] == "neutral_first"
    rows = _readings(session, scene.scene_id)
    assert [(row.stage, row.draft_ref, row.distance) for row in rows] == [
        (R.STAGE_FIRST_DRAFT, first.row_id, 1.40),
        (R.STAGE_REVISION, result.row_id, 1.10),
    ]


def test_missing_revision_template_keeps_the_first_draft_with_a_warning(session, monkeypatch) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_tpl")
    runner = _Runner(outputs={}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(monkeypatch, {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT)})
    monkeypatch.setattr(service._prompt_builder(), "has_template", lambda name: name != "style_targeted_revision")
    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    assert len(runner.calls) == 1 and result.content == first.content
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["style_step"]["reason"] == S.REASON_TEMPLATE_MISSING
    notice = details["notices"][0]
    assert notice["code"] == sg.STYLE_NOTICE_REVISION_REJECTED and notice["severity"] == "warning"


# ---------------------------------------------------------------------------
# 定向修改的零件（纯函数）
# ---------------------------------------------------------------------------


def test_revision_dimensions_put_emphasized_first_skip_excluded_and_cap_at_four() -> None:
    out = [
        {"feature": "punct_comma_per_1k", "dimension": "language.punctuation", "z": -4.0, "direction": "low", "phrase": "p"},
        {"feature": "para_len_mean", "dimension": "narrative.pacing", "z": 2.2, "direction": "high", "phrase": "q"},
        {"feature": "fw_modal_per_1k", "dimension": "language.vocabulary", "z": 3.0, "direction": "low", "phrase": "r"},
        {"feature": "dialogue_char_share", "dimension": "scene.dialogue", "z": 2.5, "direction": "low", "phrase": "s"},
        {"feature": "sent_len_mean", "dimension": "language.sentence_structure", "z": 2.1, "direction": "high", "phrase": "t"},
        {"feature": "person_first_share", "dimension": "narrative.perspective", "z": 5.0, "direction": "high", "phrase": "u"},
    ]
    reading = _reading(1.5, 98.0, out_of_band=out)
    states = {"narrative.pacing": "emphasize", "narrative.perspective": "exclude"}
    dims = S.revision_dimensions(reading, states)
    assert dims[0] == "narrative.pacing" and "narrative.perspective" not in dims and len(dims) == 4
    assert dims[1:] == ["language.punctuation", "language.vocabulary", "scene.dialogue"]
    # 没有单独越界的特征：取确定性分最低的维
    fallback = S.revision_dimensions(_reading(1.2, 95.0), {})
    assert fallback[0] == "narrative.pacing"


def test_level_words_speak_in_words_not_numbers() -> None:
    assert S.level_words("punct_comma_per_1k", 62.0) == "每千字约六十二个"
    assert S.level_words("sentence_final_modal_ratio", 0.2) == "大约每五句一次"
    assert S.level_words("dialogue_char_share", 0.41) == "约四成"
    assert S.level_words("para_len_mean", 60) == "约六十字"
    assert S.level_words("lexical_char_ttr", 0.3) == ""
    assert not any(ch.isdigit() for ch in S.level_words("numeral_unit_per_1k", 3.4))


def test_patch_keep_decision_reverts_only_when_the_patch_moved_away() -> None:
    t = S.DEFAULT_THRESHOLDS
    assert S.patch_keep_decision(before_judge=0.70, after_judge=0.55, before_distance=1.0, after_distance=0.9, thresholds=t) == (
        S.PATCH_DECISION_REVERTED,
        S.PATCH_REASON_JUDGE_WORSE,
    )
    # M3：两次独立评审差不到一分（10 分制上 7.0 → 6.2）是评审自己的噪声，不算变差——容差 0.1（= 1 分），不是 0.02
    assert S.patch_keep_decision(before_judge=0.70, after_judge=0.62, before_distance=1.0, after_distance=1.0, thresholds=t) == (
        S.PATCH_DECISION_KEPT,
        S.PATCH_REASON_NOT_WORSE,
    )
    assert S.patch_keep_decision(before_judge=0.70, after_judge=0.70, before_distance=1.0, after_distance=1.2, thresholds=t) == (
        S.PATCH_DECISION_REVERTED,
        S.PATCH_REASON_DISTANCE_WORSE,
    )
    # 读数变远但评审分明显提高：留下补丁
    assert S.patch_keep_decision(before_judge=0.60, after_judge=0.75, before_distance=1.0, after_distance=1.2, thresholds=t)[0] == (
        S.PATCH_DECISION_KEPT
    )
    # 评审分在容差内的小波动、读数没变远：留下
    assert S.patch_keep_decision(before_judge=0.70, after_judge=0.69, before_distance=1.0, after_distance=1.02, thresholds=t)[0] == (
        S.PATCH_DECISION_KEPT
    )
    assert S.patch_keep_decision(before_judge=None, after_judge=None, before_distance=None, after_distance=None, thresholds=t) == (
        S.PATCH_DECISION_KEPT,
        S.PATCH_REASON_NO_EVIDENCE,
    )


def test_fidelity_thresholds_come_from_the_budget_file() -> None:
    thresholds = S.fidelity_thresholds()
    assert thresholds.audit() == {
        "style_step_max_percentile": 90.0,
        "revision_min_improvement": 0.03,
        "patch_max_distance_increase": 0.05,
        "judge_tolerance": 0.1,
    }
    assert S.DEFAULT_THRESHOLDS.judge_tolerance == 0.1, "默认值与预算文件一致"


def test_render_audit_notices_become_style_notices() -> None:
    prompt = {
        "_style_reference_runtime_audit": {
            "outcome": "hit",
            "notices": ["STYLE_REFERENCE_BOOK_CHANGED", "STYLE_REFERENCE_SAMPLES_BLOCKED", "UNKNOWN"],
            "samples_blocked": "cloud_policy_now",
        }
    }
    notices = sg.style_injection_notices(prompt)
    assert [item["code"] for item in notices] == ["STYLE_REFERENCE_BOOK_CHANGED", "STYLE_REFERENCE_SAMPLES_BLOCKED"]
    assert all(item["severity"] == "warning" for item in notices)
    assert notices[1]["samples_blocked"] == "cloud_policy_now"


# ---------------------------------------------------------------------------
# Best-of-N：候选 = 首稿 + (N−1) 个定向修改，按 distance 排序
# ---------------------------------------------------------------------------


def test_best_of_n_ranks_the_first_draft_and_revisions_by_distance(session, monkeypatch) -> None:
    scene, bundle, _book, _profile = _bound_scene(session, "fid_bon")
    runner = _SeqRunner([LONG_FIRST, OTHER_REVISED, REVISED])
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {
            "窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT),
            "那一夜的雨": _reading(1.30, 90.5),
            "雨下了一夜": _reading(1.10, 60.0),
        },
    )
    products: list[tuple[str, str]] = []
    candidates = service.generate_style_draft_candidates(
        scene.scene_id,
        bundle,
        neutral_draft_row_id=first.row_id,
        neutral_content=first.content,
        n_candidates=3,
        product_callback=lambda slot, phase, _res, _meta: products.append((slot, phase)),
    )
    session.commit()

    assert [c.content for c in candidates] == [REVISED, OTHER_REVISED, LONG_FIRST]
    assert [c.ranking_audit["selection_reason"] for c in candidates] == ["fidelity_distance"] * 3
    assert [c.ranking_audit["fidelity_distance"] for c in candidates] == [1.10, 1.30, 1.40]
    assert candidates[-1].lineage == sg.LINEAGE_FIRST_DRAFT_ACCEPTED, "首稿永远在候选里（槽位 0，不调模型）"
    assert len(runner.calls) == 3, "首稿 1 次 + 两个修改槽位"
    assert products == [
        ("initial:0", "base"),
        ("initial:0", "final"),
        ("initial:1", "base"),
        ("initial:1", "final"),
        ("initial:2", "base"),
        ("initial:2", "final"),
    ]
    state = session.get(SceneRunState, scene.scene_id)
    assert state.current_style_draft_row_id == candidates[0].row_id


def test_best_of_n_duplicate_first_draft_is_offered_once(session, monkeypatch) -> None:
    from novel_system.services.orchestrator import Orchestrator

    scene, bundle, _book, _profile = _bound_scene(session, "fid_bondup")
    lost = REVISED.replace("信封", "东西")
    runner = _SeqRunner([LONG_FIRST, lost, REVISED])
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(1.10, 60.0)},
    )
    candidates = service.generate_style_draft_candidates(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content, n_candidates=3
    )
    session.commit()
    contents = [c.content for c in candidates]
    assert contents.count(LONG_FIRST) == 2, "没过安全门的修改槽位保留首稿原文"
    duplicate = next(c for c in candidates if c.ranking_audit["duplicate_of_row_id"])
    assert duplicate.content == LONG_FIRST
    state = session.get(SceneRunState, scene.scene_id)
    offered = Orchestrator(session)._offer_candidates_for_selection(scene, state, bundle, candidates)
    assert len(offered) == 2 and duplicate.row_id not in offered


def _book_source_sentence(session, book_id: str) -> str:
    from novel_system.db.models import StyleReferenceParagraph

    return next(
        p.text
        for p in session.scalars(
            select(StyleReferenceParagraph)
            .where(StyleReferenceParagraph.book_id == book_id)
            .order_by(StyleReferenceParagraph.paragraph_index)
        )
        if len(p.text) >= 24 and "第" not in p.text[:2]
    )


def test_targeted_revision_is_not_blamed_for_a_copy_the_first_draft_already_had(session, monkeypatch) -> None:
    """M2：首稿里本来就有一段与参考书相同的字，修改稿照旧留着、别的地方改得更像了——这不是修改带进来的照抄，
    不能因此丢掉修改稿（白花一次调用，还把「照抄」记到修改头上）；首稿自己的重合由硬 QC / 成稿门对全文把关。"""
    scene, bundle, book_id, _profile = _bound_scene(session, "fid_precopy")
    source = _book_source_sentence(session, book_id)
    runner = _Runner(outputs={"style_draft": REVISED + source}, default=LONG_FIRST + source)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(1.20, 71.0)},
    )

    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    assert result.content == REVISED + source, "修改稿更像、没有新带进重合：采用"
    step = _style_attempt(session, scene.scene_id).details_json["style_step"]
    assert step["decision"] == S.DECISION_REVISION_KEPT and step["reason"] == S.REASON_CLOSER
    assert step["copy_check"]["blocked"] is True, "全文仍记着原有的重合"
    assert step["copy_check_introduced"]["blocked"] is False and step["copy_check_introduced"]["hits"] == 0


def test_best_of_n_ranks_copy_blocked_candidates_last(session, monkeypatch) -> None:
    """M2：一份与参考书原文连续相同的首稿不能凭 distance 更小赢过干净的修改稿、成为风格稿——被抄袭门拦的候选排最后。"""
    scene, bundle, book_id, _profile = _bound_scene(session, "fid_boncopy")
    source = _book_source_sentence(session, book_id)
    runner = _SeqRunner([LONG_FIRST + source, OTHER_REVISED, REVISED])
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {
            "窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT),
            "那一夜的雨": _reading(1.45, 97.5),
            "雨下了一夜": _reading(1.50, 98.0),
        },
    )
    candidates = service.generate_style_draft_candidates(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content, n_candidates=3
    )
    session.commit()

    assert [c.content for c in candidates] == [OTHER_REVISED, REVISED, LONG_FIRST + source]
    assert [c.ranking_audit["plagiarism_passed"] for c in candidates] == [True, True, False]
    assert session.get(SceneRunState, scene.scene_id).current_style_draft_row_id == candidates[0].row_id


def test_best_of_n_resume_keeps_every_already_produced_slot(session, monkeypatch) -> None:
    """L1：续跑时首稿读数变了（这次读不出 / 不可信），槽位数不能缩回 1——已经落下检查点的槽位一个都不能丢，
    否则检查点里的工作项对不上（RUN_CHECKPOINT_CORRUPT）；也不能为它们重新调模型。"""
    scene, bundle, _book, _profile = _bound_scene(session, "fid_bonresume")
    runner = _SeqRunner([LONG_FIRST, OTHER_REVISED, REVISED])
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {
            "窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT),
            "那一夜的雨": _reading(1.30, 90.5),
            "雨下了一夜": _reading(1.10, 60.0),
        },
    )
    produced: dict[str, sg.StyleGenerationResult] = {}

    def keep_final(slot, phase, result, _meta):  # noqa: ANN001
        if phase == "final":
            produced[slot] = result

    service.generate_style_draft_candidates(
        scene.scene_id,
        bundle,
        neutral_draft_row_id=first.row_id,
        neutral_content=first.content,
        n_candidates=3,
        product_callback=keep_final,
    )
    session.commit()
    assert sorted(produced) == ["initial:0", "initial:1", "initial:2"]
    calls_before = len(runner.calls)

    # 续跑：这一次首稿读不出（书改过 / 读数出错）——槽位数按已落下的槽位算
    _install_readings(monkeypatch, {})
    resumed = service.generate_style_draft_candidates(
        scene.scene_id,
        bundle,
        neutral_draft_row_id=first.row_id,
        neutral_content=first.content,
        n_candidates=3,
        resume_products=dict(produced),
    )
    session.commit()

    assert len(resumed) == 3 and sorted(c.row_id for c in resumed) == sorted(r.row_id for r in produced.values())
    assert len(runner.calls) == calls_before, "续跑不重新调模型"


def test_run_display_follows_the_selected_candidate_not_the_last_slot(session, monkeypatch) -> None:
    """L7：Best-of-N 的一次运行有好几份风格稿尝试；工作台的「像不像」与风格链路提示要跟着**选中**的那一份候选
    （排第一的），不是最后一个槽位的。"""
    from novel_system.services.style_fidelity_view import current_run_style_fidelity, scene_decisions

    scene, bundle, _book, _profile = _bound_scene(session, "fid_bonsel")
    lost = REVISED.replace("信封", "东西")
    runner = _SeqRunner([LONG_FIRST, REVISED, lost])
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {"窗外的雨": _reading(1.40, 97.0, out_of_band=PACING_OUT), "雨下了一夜": _reading(1.10, 60.0)},
    )
    candidates = service.generate_style_draft_candidates(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content, n_candidates=3
    )
    session.commit()
    selected = candidates[0]
    assert selected.content == REVISED and selected.row_id.endswith("_1"), "选中的是槽位 1，不是最后一个槽位"
    last_slot = session.execute(
        select(AttemptTracker)
        .where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "style_draft")
        .order_by(AttemptTracker.attempt_id.desc())
    ).scalars().first()
    assert last_slot.details_json["row_id"].endswith("_2")
    assert sg.STYLE_NOTICE_REVISION_REJECTED in [item["code"] for item in last_slot.details_json["notices"]]

    summary = current_run_style_fidelity(session, scene.scene_id, bundle["bundle_id"])
    assert summary["style_step"]["row_id"] == selected.row_id
    assert summary["style_step"]["decision"] == S.DECISION_REVISION_KEPT
    codes = [item["code"] for item in sg.latest_style_notices(session, scene.scene_id, bundle_id=bundle["bundle_id"])]
    assert sg.STYLE_NOTICE_REVISION_REJECTED not in codes, "最后一个槽位的「修改没采用」不是这次运行选中稿的提示"
    steps = [item for item in scene_decisions(session, scene.scene_id) if item["kind"] == "style_step"]
    assert [item["row_id"] for item in steps] == [selected.row_id], "一次运行只列选中那份候选的风格步"


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_a_failed_first_draft_reading_is_not_reported_as_a_missing_yardstick(session, monkeypatch) -> None:
    """L8 + L3：读数本身出错（这里：读数时建索引写库撞了主键）与「参考书没有可用的尺子」是两回事——决定原因是
    ``reading_failed``、提示说实话；读数在保存点里读，出错只回滚保存点，会话照样能落库（风格稿行、尝试行都写得下）。"""
    scene, bundle, _book, _profile = _bound_scene(session, "fid_readfail")
    runner = _Runner(outputs={}, default=LONG_FIRST)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    first_row = session.get(SceneDraft, first.row_id)

    def broken_reading(session_arg, policy, text):  # noqa: ANN001
        # 模拟读数途中写库失败：与已有的首稿行撞主键
        session_arg.add(
            SceneDraft(
                row_id=first_row.row_id,
                scene_id=first_row.scene_id,
                chapter_id=first_row.chapter_id,
                stage="neutral_draft",
                content="x",
                source_bundle_id=first_row.source_bundle_id,
                source_bundle_hash=first_row.source_bundle_hash,
            )
        )
        session_arg.flush()

    monkeypatch.setattr(R, "reading_for_text", broken_reading)
    result = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()

    assert len(runner.calls) == 1 and result.content == first.content
    details = _style_attempt(session, scene.scene_id).details_json
    assert details["style_step"]["reason"] == S.REASON_READING_FAILED
    notice = next(item for item in details["notices"] if item["code"] == sg.STYLE_NOTICE_FIRST_DRAFT_ACCEPTED)
    assert notice["severity"] == "warning" and "读数出了错" in notice["message"] and "没有可用的读数尺子" not in notice["message"]
    assert session.get(SceneDraft, result.row_id).content == first.content


# ---------------------------------------------------------------------------
# N7 · 近期常见偏差进下一场首稿的文风卡
# ---------------------------------------------------------------------------


def test_recent_first_draft_gaps_reach_the_next_first_draft_card(session) -> None:
    from novel_system.services.style_policy import style_policy_for_bundle

    scene, bundle, _book, profile_id = _bound_scene(session, "fid_gaps")
    policy = style_policy_for_bundle(bundle)
    gap = [dict(PACING_OUT[0])]
    for index in range(3):
        R.record_fidelity_reading(
            session,
            policy=policy,
            text=f"第{index}场的首稿" * 50,
            source=R.SOURCE_PIPELINE,
            stage=R.STAGE_FIRST_DRAFT,
            scene_id=f"OTHER_{index}",
            project_id=scene.project_id,
            draft_ref=f"draft_{index}",
            reading=_reading(1.5, 97.0, out_of_band=gap),
        )
    # 修改 / 终稿 / 对照检查的读数不计票
    R.record_fidelity_reading(
        session,
        policy=policy,
        text="终稿" * 300,
        source=R.SOURCE_ARCHIVE,
        stage=R.STAGE_FINAL,
        scene_id="OTHER_0",
        project_id=scene.project_id,
        reading=_reading(0.5, 10.0),
    )
    session.commit()
    assert R.recent_gaps(session, project_id=scene.project_id, profile_id=profile_id) == ["段落比作者长，换段太少"]

    runner = _Runner(outputs={}, default=LONG_FIRST)
    SceneGenerationService(session, llm_runner=runner).generate_neutral_draft(scene.scene_id, bundle)
    system = runner.calls[0]["prompt"]["system_prompt"]
    assert "[近期常见偏差]" in system and "段落比作者长，换段太少" in system
    assert runner.calls[0]["prompt"]["_style_reference_runtime_audit"]["role"] == "draft"


def test_first_draft_uses_the_blueprint_situation_tags_frozen_in_the_bundle(session, monkeypatch) -> None:
    captured: list[dict] = []
    real = sg.inject_style_reference_prefix

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        captured.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sg, "inject_style_reference_prefix", spy)
    scene, bundle, _book, _profile = _bound_scene(session, "fid_tags")
    bundle["snapshot"]["inline_digests"]["_scene_situation_tags"] = json.dumps(["对峙审问"], ensure_ascii=False)
    SceneGenerationService(session, llm_runner=_Runner(outputs={}, default=LONG_FIRST)).generate_neutral_draft(
        scene.scene_id, bundle
    )
    assert captured[0]["role"] == "draft" and list(captured[0]["situation_tags"]) == ["对峙审问"]


@pytest.mark.parametrize("pass_kind", ["salvage", "de_template", "safety_repair", "length_patch"])
def test_patch_and_repair_passes_render_the_reference_as_a_revision(session, monkeypatch, pass_kind) -> None:
    """注入口径：救稿 / 去模板 / 安全修复 / 长度补丁都只是改稿，按改稿角色渲染（不是「写这一场」的起草口径，
    也不带近期常见偏差）。以前这四处不传角色，适配器按落点推成起草。"""
    captured: list[dict] = []
    real = sg.inject_style_reference_prefix

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        captured.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sg, "inject_style_reference_prefix", spy)
    draft_mode = "style_first" if pass_kind in {"safety_repair", "length_patch"} else "neutral_first"
    band = "200-400" if pass_kind == "length_patch" else "short"
    scene, bundle, _book, _profile = _bound_scene(session, f"fid_role_{pass_kind}", draft_mode=draft_mode, band=band)
    service = SceneGenerationService(session, llm_runner=_Runner(outputs={}, default=LONG_FIRST))
    state = session.get(SceneRunState, scene.scene_id)
    common = dict(scene=scene, state=state, bundle=bundle, execution_step_key=None)
    if pass_kind == "salvage":
        service._run_style_salvage_pass(
            **common,
            checkpoint_base_row_id="row_base",
            rejected_style_row_id="row_rejected",
            rejected_style_content=REVISED,
            neutral_row_id="row_neutral",
            neutral_content=LONG_FIRST,
            quality_gate={"base_safety": {"accepted": False, "reasons": ["required_facts_missing"]}},
        )
    else:
        reasons = ["target_length_not_met"] if pass_kind == "length_patch" else ["required_facts_missing"]
        service._run_de_template_pass(
            **common,
            base_prompt=service._prompt_builder().build(bundle["snapshot"], "style_draft"),
            checkpoint_base_row_id="row_base",
            source_row_id="row_source",
            source_content=REVISED,
            authoritative_row_id=None if pass_kind == "de_template" else "row_neutral",
            authoritative_content=None if pass_kind == "de_template" else LONG_FIRST,
            quality_gate={
                "base_safety": {"accepted": pass_kind == "de_template", "reasons": [] if pass_kind == "de_template" else reasons},
                "findings": [],
                "risk_dimensions": [],
            },
        )
    assert captured, "这一处带了参考"
    assert captured[-1]["role"] == "revise"


# ---------------------------------------------------------------------------
# 读数入库（唯一入口）：幂等、未绑定不写、只记计数
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 读数入库（唯一入口）：幂等、未绑定不写、只记计数
# ---------------------------------------------------------------------------


def test_record_fidelity_reading_is_idempotent_per_draft_and_noop_when_unbound(session) -> None:
    from novel_system.services.style_policy import UNBOUND, style_policy_for_bundle

    scene, bundle, _book, _profile = _bound_scene(session, "fid_rec")
    policy = style_policy_for_bundle(bundle)
    assert R.record_fidelity_reading(session, policy=UNBOUND, text="x" * 700, source="pipeline", stage="final") is None
    with pytest.raises(ValueError):
        R.record_fidelity_reading(session, policy=policy, text="x", source="nowhere", stage="final")
    text = "他把灯芯拨小了些，屋里的影子便大了一圈。" * 40
    first = R.record_fidelity_reading(
        session, policy=policy, text=text, source="archive", stage="final", scene_id=scene.scene_id, draft_ref="final_1"
    )
    again = R.record_fidelity_reading(
        session, policy=policy, text=text, source="archive", stage="final", scene_id=scene.scene_id, draft_ref="final_1"
    )
    assert first is not None and again.reading_id == first.reading_id
    payload = R.reading_payload(first)
    assert set(payload) >= {
        "reading_id",
        "scene_id",
        "source",
        "stage",
        "percentile",
        "distance",
        "within_range",
        "out_of_band",
        "dimension_scores",
        "judge",
        "copy_check",
        "created_at",
    }
    assert all(set(item) >= {"feature", "dimension", "dimension_label", "direction", "phrase", "z"} for item in payload["out_of_band"])
    assert first.reading_json["policy"]["profile_id"] == policy.profile_id
    assert "chapter_id" not in payload, "读数按场景键，不按章键"


def test_project_summary_counts_one_judge_vote_per_scene(session) -> None:
    """同一份软 QC 评审挂在补丁与终稿两条读数上时，按维平均的评审分每场只算一票（取最新）；不在场景上的
    文字检查各算一票；确定性分只数每场最新的终稿读数。"""
    from novel_system.services.style_policy import style_policy_for_bundle

    scene, bundle, _book, _profile = _bound_scene(session, "fid_sum")
    policy = style_policy_for_bundle(bundle)
    text = "他把灯芯拨小了些，屋里的影子便大了一圈。" * 40
    old_judge = {"overall": 4.0, "dimensions": {"scene.dialogue": {"score": 4.0}}}
    new_judge = {"overall": 8.0, "dimensions": {"scene.dialogue": {"score": 8.0}}}
    kwargs = dict(policy=policy, text=text, source="pipeline", scene_id=scene.scene_id, project_id=scene.project_id)
    assert R.record_fidelity_reading(session, stage="patched", draft_ref="patch_1", judge=old_judge, **kwargs)
    assert R.record_fidelity_reading(session, stage="final", draft_ref="final_1", judge=new_judge, **kwargs)
    assert R.record_fidelity_reading(session, stage="patched", draft_ref="patch_2", judge=new_judge, **kwargs)
    assert R.record_fidelity_reading(
        session,
        policy=policy,
        text=text + "。",
        source="manual_check",
        stage="manual",
        project_id=scene.project_id,
        judge={"overall": 2.0, "dimensions": {"scene.dialogue": {"score": 2.0}}},
    )
    session.commit()
    summary = R.project_fidelity_summary(session, scene.project_id, profile_id=policy.profile_id)
    dialogue = summary["dimension_averages"]["scene.dialogue"]
    assert dialogue["judged"] == 2 and dialogue["judge"] == 5.0  # 这一场最新的 8.0 + 文字检查的 2.0
    assert summary["final_scene_count"] == 1 and summary["reading_count"] == 4
    assert all(item["scenes"] <= 1 for item in summary["dimension_averages"].values())


# ---------------------------------------------------------------------------
# 归档 / 采纳 / 写作台采纳的读数
# ---------------------------------------------------------------------------


def _final(session, scene: SceneCard, text: str, *, row_id: str = "final_fid") -> FinalScene:
    final = FinalScene(
        row_id=row_id,
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content=text,
        source_bundle_id="author_adopt",
        source_bundle_hash="author_adopt",
    )
    session.add(final)
    session.get(SceneRunState, scene.scene_id).current_final_scene_row_id = final.row_id
    session.commit()
    return final


def test_archive_hook_records_the_final_reading_and_is_idempotent(session) -> None:
    from novel_system.services.scene_archive_effects import SceneArchiveEffects

    scene, _bundle, _book, _profile = _bound_scene(session, "fid_arch")
    final = _final(session, scene, "他把灯芯拨小了些，屋里的影子便大了一圈。" * 40)
    effects = SceneArchiveEffects(session, None, execution_id=None, run_job_id=None)
    result = effects._record_archive_fidelity_reading(scene)
    again = effects._record_archive_fidelity_reading(scene)
    session.commit()
    assert result["outcome"] == "recorded" and again["reading_id"] == result["reading_id"]
    row = session.get(StyleFidelityReading, result["reading_id"])
    assert (row.source, row.stage, row.draft_ref) == ("pipeline", "final", final.row_id)
    # 未绑定的场景：不适用
    other = _seed_scene(session, project_id="proj_fid_unbound", scene_id="FID_UNBOUND_SC01", chapter_id="FID_UNBOUND_CH")
    _final(session, other, "空无一物" * 200, row_id="final_unbound")
    assert effects._record_archive_fidelity_reading(other) == {"outcome": "not_applicable", "reason": "unbound"}


def test_adopt_route_records_an_adopt_reading(client, session) -> None:
    from tests.test_chapter_manuscripts import _create_chapter, _create_scene

    _create_chapter(client, "chapter_fid_adopt")
    _create_scene(client, "scene_fid_adopt", chapter_id="chapter_fid_adopt", scene_seq=1)
    _book, profile_id = seed_reference(session, "fid_adopt", chapters=14, per_chapter=100)
    bind(session, profile_id, binding_id="bind_fid_adopt", scope="scene", scope_ref_id="scene_fid_adopt")
    text = "潮水退去以后，他在闸门前站了很久，才把手里的灯放下。" * 30
    session.add(
        SceneDraft(
            row_id="draft_fid_adopt",
            scene_id="scene_fid_adopt",
            chapter_id="chapter_fid_adopt",
            stage="style_draft",
            content=text,
            source_bundle_id="bundle_scene_fid_adopt",
            source_bundle_hash="hash",
        )
    )
    session.get(SceneRunState, "scene_fid_adopt").current_style_draft_row_id = "draft_fid_adopt"
    session.commit()
    response = client.post(
        "/api/v1/scenes/scene_fid_adopt/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "fid-adopt-1"},
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    rows = _readings(session, "scene_fid_adopt")
    assert [(r.source, r.stage) for r in rows] == [("adopt", "final")]
    assert rows[0].profile_id == profile_id

    # L7：已归档之后作者在起草台再确认（同一终稿行、同一段文字）——不再多记一条终稿读数（走势图不出重复点）
    again = client.post(
        "/api/v1/scenes/scene_fid_adopt/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "fid-adopt-2"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["already_archived"] is True
    session.expire_all()
    assert [(r.source, r.stage) for r in _readings(session, "scene_fid_adopt")] == [("adopt", "final")]


def test_reconfirming_a_pipeline_archived_scene_adds_no_second_final_point(client, session) -> None:
    """L7：管线归档记过一条终稿读数（source=pipeline）；作者之后在起草台再确认同一份终稿（source=adopt）——终稿读数
    跨来源幂等，还是那一条，作品走势里这一场只有一个终稿点。"""
    from novel_system.services.scene_archive_effects import SceneArchiveEffects
    from novel_system.services.style_fidelity_view import project_style_fidelity

    scene, _bundle, _book, _profile = _bound_scene(session, "fid_reconfirm")
    final = _final(session, scene, "他把灯芯拨小了些，屋里的影子便大了一圈，信封压在灯座下。" * 30, row_id="final_fid_reconfirm")
    state = session.get(SceneRunState, scene.scene_id)
    state.scene_status = "archived"
    session.commit()
    recorded = SceneArchiveEffects(session, None, execution_id=None, run_job_id=None)._record_archive_fidelity_reading(scene)
    session.commit()
    assert recorded["outcome"] == "recorded"

    response = client.post(
        f"/api/v1/scenes/{scene.scene_id}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "fid-reconfirm"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["already_archived"] is True
    session.expire_all()
    rows = _readings(session, scene.scene_id)
    assert [(r.source, r.stage, r.draft_ref) for r in rows] == [("pipeline", "final", final.row_id)]
    trend = project_style_fidelity(session, scene.project_id)["trend"]
    assert [point["stage"] for point in trend] == ["final"]


def test_author_draft_adoption_records_author_draft_readings(session) -> None:
    from novel_system.db.models import AuthorDraft, PassagePatchCandidate
    from novel_system.services.writer_deep_review import WriterDeepReviewService

    scene, _bundle, _book, _profile = _bound_scene(session, "fid_author")
    body = "旧的一句话在这里。" + "他把灯芯拨小了些，屋里的影子便大了一圈。" * 40
    session.add(
        AuthorDraft(
            draft_id="adraft_fid",
            object_type="scene",
            object_id=scene.scene_id,
            source_text_ref="blank",
            status="current",
            revision_no=3,
            content=f"<p>{body}</p>",
        )
    )
    session.add(
        PassagePatchCandidate(
            patch_id="patch_fid",
            object_type="scene",
            object_id=scene.scene_id,
            scene_id=scene.scene_id,
            source_draft_id="adraft_fid",
            source_excerpt="旧的一句话在这里。",
            issue_dimension="author_instruction",
            replacement_options_json=[{"option_id": "opt_1", "replacement_text": "新的一句话落在这里。"}],
        )
    )
    session.commit()
    WriterDeepReviewService(session).accept_patch_candidate("patch_fid", {"selected_option_id": "opt_1"})
    session.commit()
    row = next(r for r in _readings(session, scene.scene_id) if r.source == "author_draft")
    assert row.stage == "patched" and row.draft_ref == "passage_patch:patch_fid:opt_1"
    # 读的是把原句换成改写之后的作者稿
    expected = R.text_sha256(body.replace("旧的一句话在这里。", "新的一句话落在这里。", 1))
    assert row.text_sha256 == expected

    R.record_author_draft_reading(
        session, scene_id=scene.scene_id, text=f"<p>{body}</p>", stage=R.STAGE_REVISION, draft_ref="author_draft:adraft_fid:rev3"
    )
    session.commit()
    assert any(r.stage == "revision" and r.source == "author_draft" for r in _readings(session, scene.scene_id))

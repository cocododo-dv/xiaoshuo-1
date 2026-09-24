"""风格参考 v3（P6b）—「像不像」给界面的几个小补充（读接口不写库）。

- 一条读数的 API 形状带上「正常范围」的百分位上限、重点维、读数为什么只能参考（文字太短 / 参考书窗口太少）——
  界面据此说「前 N 位算正常」「重点维越界不算正常」，不在前端再抄一份阈值；
- 作品汇总带上结构化的近期常见偏差（短语 + 维度，与首稿补充强调的那几条逐条相同）与每一场最新的终稿读数
  （成稿中心按场的角标）；
- 起草台工作台的「本场参考窗口」：v3 冻结选窗的引用带窗号，补上学习作业给这一窗打的标签与一句话梗概；
  旧审计（没有窗号）的形状不变。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.api.routes.scenes import _serialize_generation_summary
from novel_system.db.models import StyleFidelityReading, StyleReferenceWindow
from novel_system.services.style_reference import readings as R
from novel_system.services.style_reference.fidelity import (
    MIN_REFERENCE_WINDOWS,
    MIN_RELIABLE_CHARS,
    recent_gap_entries,
    recent_gap_phrases,
)
from novel_system.services.style_reference.windows import WINDOW_INDEX_VERSION
from tests.test_style_windows_surface import _attempt, _seed_book, _seed_profile, _seed_scene


def _row(
    reading_id: str,
    *,
    project_id: str = "P_UI",
    scene_id: str | None = "SC_UI",
    profile_id: str = "pf_ui",
    source: str = R.SOURCE_PIPELINE,
    stage: str = R.STAGE_FINAL,
    percentile: float = 40.0,
    char_count: int = 1800,
    created_at: str = "2026-09-23T08:00:00",
    **reading_json,
) -> StyleFidelityReading:
    data = {"percentile": percentile, "within_range": percentile <= 90.0, "reliable": True, "window_count": 40}
    data.update(reading_json)
    return StyleFidelityReading(
        reading_id=reading_id,
        project_id=project_id,
        scene_id=scene_id,
        profile_id=profile_id,
        source=source,
        stage=stage,
        draft_ref=f"draft:{reading_id}",
        text_sha256=f"sha_{reading_id}",
        char_count=char_count,
        percentile=percentile,
        distance=0.5,
        reading_json=data,
        created_at=created_at,
    )


def _gap(feature: str, dimension: str, direction: str = "low", z: float = -2.4) -> dict:
    return {"feature": feature, "dimension": dimension, "direction": direction, "z": z, "phrase": f"{feature}·{direction}"}


def test_reading_payload_says_what_counts_as_normal_and_why_a_reading_is_only_indicative(session: Session) -> None:
    session.add_all(
        [
            _row(
                "r_ok",
                percentile=72.0,
                max_percentile=85.0,
                emphasized_dimensions=["scene.dialogue"],
                excluded_dimensions=["theme.values"],
            ),
            _row("r_short", char_count=MIN_RELIABLE_CHARS - 1, reliable=False),
            _row("r_few", char_count=MIN_RELIABLE_CHARS + 500, reliable=False, window_count=MIN_REFERENCE_WINDOWS - 1),
            # 早于这一版的读数没有记阈值：按当前阈值（与入库时同一个默认）
            _row("r_old", percentile=30.0),
        ]
    )
    session.commit()

    ok = R.reading_payload(session.get(StyleFidelityReading, "r_ok"))
    assert ok["max_percentile"] == 85.0 and ok["emphasized_dimensions"] == ["scene.dialogue"]
    assert ok["excluded_dimensions"] == ["theme.values"]
    assert ok["reliable"] is True and ok["unreliable_reason"] is None and ok["window_count"] == 40
    assert (ok["min_reliable_chars"], ok["min_reference_windows"]) == (MIN_RELIABLE_CHARS, MIN_REFERENCE_WINDOWS)

    short = R.reading_payload(session.get(StyleFidelityReading, "r_short"))
    assert short["reliable"] is False and short["unreliable_reason"] == "too_short"
    few = R.reading_payload(session.get(StyleFidelityReading, "r_few"))
    assert few["reliable"] is False and few["unreliable_reason"] == "few_windows"

    old = R.reading_payload(session.get(StyleFidelityReading, "r_old"))
    assert old["max_percentile"] == R._default_max_percentile() and old["emphasized_dimensions"] == []
    assert old["excluded_dimensions"] == []


def test_recent_gap_entries_keep_the_dimension_and_agree_with_the_phrases() -> None:
    readings = [
        {"created_at": f"2026-09-2{i}", "reading_json": {"out_of_band": [_gap("fw_modal_per_1k", "language.vocabulary")]}}
        for i in range(4)
    ] + [{"created_at": "2026-09-19", "reading_json": {"out_of_band": [_gap("punct_dash_per_1k", "language.punctuation")]}}]
    entries = recent_gap_entries(readings, min_hits=3, window=5)
    assert entries == [
        {
            "feature": "fw_modal_per_1k",
            "direction": "low",
            "dimension": "language.vocabulary",
            "phrase": "fw_modal_per_1k·low",
            "hits": 4,
            "window": 5,
        }
    ]
    assert recent_gap_phrases(readings, min_hits=3, window=5) == [entry["phrase"] for entry in entries]
    # 越界条目没写维度（旧读数）：按特征表补上
    legacy = [{"created_at": f"2026-09-2{i}", "reading_json": {"out_of_band": [{"feature": "punct_dash_per_1k", "z": 2.5}]}} for i in range(3)]
    assert recent_gap_entries(legacy)[0]["dimension"] == "language.punctuation"


def test_project_summary_gives_structured_gaps_and_the_latest_final_per_scene(session: Session) -> None:
    gap = [_gap("fw_modal_per_1k", "language.vocabulary")]
    session.add_all(
        [
            # 最近五次首稿读数里三次同一处越界 → 近期常见偏差
            _row("fd1", scene_id="SC_A", stage=R.STAGE_FIRST_DRAFT, created_at="2026-09-23T01:00:00", out_of_band=gap),
            _row("fd0", scene_id="SC_Z", stage=R.STAGE_FIRST_DRAFT, created_at="2026-09-22T23:00:00", max_percentile=85.0),
            _row("fd2", scene_id="SC_B", stage=R.STAGE_FIRST_DRAFT, created_at="2026-09-23T02:00:00", out_of_band=gap),
            _row("fd3", scene_id="SC_C", stage=R.STAGE_FIRST_DRAFT, created_at="2026-09-23T03:00:00", out_of_band=gap),
            _row("fd4", scene_id="SC_C", stage=R.STAGE_FIRST_DRAFT, created_at="2026-09-23T04:00:00"),
            # SC_A 两条终稿：取后一条；SC_B 的终稿不可靠也照给（角标自己说「量不准」）
            _row("fa_old", scene_id="SC_A", percentile=95.0, created_at="2026-09-23T05:00:00"),
            _row("fa_new", scene_id="SC_A", percentile=35.0, source=R.SOURCE_ADOPT, created_at="2026-09-23T06:00:00"),
            _row("fb", scene_id="SC_B", percentile=60.0, reliable=False, char_count=200, created_at="2026-09-23T07:00:00"),
            # 别的画像的终稿不算（作品现在对照的是 pf_ui）
            _row("fx", scene_id="SC_C", profile_id="pf_other", created_at="2026-09-23T08:00:00"),
        ]
    )
    session.commit()

    summary = R.project_fidelity_summary(session, "P_UI", profile_id="pf_ui")
    assert summary["recent_gaps"] == ["fw_modal_per_1k·low"]
    assert summary["recent_gap_details"] == [
        {
            "feature": "fw_modal_per_1k",
            "direction": "low",
            "dimension": "language.vocabulary",
            "dimension_label": "词汇选择",
            "phrase": "fw_modal_per_1k·low",
            "hits": 3,
            "window": 5,
        }
    ]
    finals = summary["scene_finals"]
    assert set(finals) == {"SC_A", "SC_B"}  # SC_Z 只有首稿
    assert finals["SC_A"] == {
        "reading_id": "fa_new",
        "source": R.SOURCE_ADOPT,
        "percentile": 35.0,
        "within_range": True,
        "reliable": True,
        "created_at": "2026-09-23T06:00:00",
    }
    assert finals["SC_B"]["reliable"] is False and finals["SC_B"]["percentile"] == 60.0
    assert summary["final_scene_count"] == 2
    # 走势行带可靠与否（图上把量不准的点画成空心）与入库时的正常范围上限（图上的范围带）
    assert {row["reading_id"]: row["reliable"] for row in summary["trend"]}["fb"] is False
    trend_limits = {row["reading_id"]: row["max_percentile"] for row in summary["trend"]}
    assert trend_limits["fd0"] == 85.0 and trend_limits["fd1"] is None


def test_workbench_windows_carry_the_learned_gist_and_tags_for_v3_refs(session: Session) -> None:
    state = _seed_scene(session, project_id="proj_p6b_windows")
    scene_id = state.scene_id
    _seed_book(session, "book_p6b", paragraphs=5)
    _seed_profile(session, book_id="book_p6b", profile_id="profile_p6b")
    session.add(
        StyleReferenceWindow(
            window_id="w_p6b_7",
            book_id="book_p6b",
            index_version=WINDOW_INDEX_VERSION,
            root_sha256="root_p6b",
            window_no=7,
            start_index=120,
            end_index=179,
            chapter_no=3,
            position="opening",
            chars=3820,
            paragraph_count=60,
            tags_json={"situations": ["开章引入"], "moods": ["平静"], "dimensions": ["scene.environment"], "gist": "某人在渡口等船"},
        )
    )
    v3_ref = {
        "window_no": 7,
        "start": 120,
        "end": 179,
        "chapter": 3,
        "position": "opening",
        "chars": 3820,
        "paragraphs": 60,
        "slot": "position",
        "situations": ["旧标签"],
        "dimensions": [],
        "paragraph_type": "narration",
        "dialogue_share": 0.1,
        "typicality": 0.8,
    }
    missing_ref = {**v3_ref, "window_no": 8, "start": 200, "end": 230, "situations": ["冻结时的场面"], "dimensions": ["narrative.time_handling"]}
    legacy_ref = {"start": 640, "end": 662, "chapter": 9, "position": "closing", "paragraph_type": "dialogue", "paragraphs": 23, "chars": 1510}
    session.add(
        _attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[v3_ref, missing_ref, legacy_ref], profile_ids=["profile_p6b"])
    )
    session.commit()

    windows = _serialize_generation_summary(session, scene_id, state)["style_windows"]["windows"]
    assert windows[0] == {
        "start": 120,
        "end": 179,
        "position": "opening",
        "paragraph_type": "narration",
        "chapter": 3,
        "paragraphs": 60,
        "chars": 3820,
        "window_no": 7,
        "slot": "position",
        "situations": ["开章引入"],
        "moods": ["平静"],
        "dimensions": ["scene.environment"],
        "gist": "某人在渡口等船",
    }
    # 索引里没有这一窗（换过索引）：留下冻结时的标签，没有梗概
    assert (windows[1]["window_no"], windows[1]["gist"], windows[1]["situations"], windows[1]["dimensions"]) == (
        8,
        "",
        ["冻结时的场面"],
        ["narrative.time_handling"],
    )
    # 旧审计（没有窗号）的形状不变
    assert windows[2] == {**legacy_ref}

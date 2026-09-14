"""2026-09-14 风格保真修补 · WP4「作者看得见」——本场参考窗口的两端。

- ``GET /api/v2/style-reference/books/{book_id}/paragraphs?start=&end=``:按段落序号闭区间读
  参考书原文(展开窗口用),每次最多 80 段、未知书 404、倒置 / 负区间 400。
- ``_serialize_generation_summary`` 的 ``style_windows``:只读本次运行 bundle 内最近一次带
  ``few_shot_window_refs`` 的 completed 尝试(风格稿 > 首稿 > 重写稿),窗口只有段落区间与
  读数,参考书由契约最具体层的画像解析。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from novel_system.api.routes.scenes import _serialize_generation_summary
from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    LlmCall,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository

PREFIX = "/api/v2/style-reference"


def _seed_book(session: Session, book_id: str, *, paragraphs: int = 100) -> None:
    repo = StyleReferenceRepository(session)
    repo.create_book(
        book_id=book_id,
        title=f"参考书 {book_id}",
        source_kind="upload",
        cloud_policy="local_only",
        text_checksum=f"sha_{book_id}",
        total_chars=paragraphs * 10,
        status="ready",
        stats_json={},
    )
    for index in range(paragraphs):
        text = f"{book_id} 第 {index} 段的原文。"
        repo.create_paragraph(
            paragraph_id=f"{book_id}_p{index:04d}",
            book_id=book_id,
            paragraph_index=index,
            paragraph_type="dialogue" if index % 3 == 0 else "narration",
            start_offset=index * 10,
            end_offset=index * 10 + len(text),
            text=text,
            char_count=len(text),
            classifier_confidence=0.9,
        )


def _seed_profile(session: Session, *, book_id: str, profile_id: str) -> None:
    repo = StyleReferenceRepository(session)
    repo.create_run(run_id=f"run_{profile_id}", book_id=book_id, status="done", phase="done")
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=f"run_{profile_id}",
        title=f"画像 {profile_id}",
        status="active",
        profile_json={},
        coverage_json={},
        source_finding_ids_json=[],
    )


# ---------------------------------------------------------------------------
# GET /books/{book_id}/paragraphs
# ---------------------------------------------------------------------------


def test_paragraph_range_returns_inclusive_ordered_slice(client: TestClient) -> None:
    with SessionLocal() as session:
        _seed_book(session, "book_range", paragraphs=30)
        session.commit()

    resp = client.get(f"{PREFIX}/books/book_range/paragraphs", params={"start": 10, "end": 14})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["book_id"] == "book_range"
    assert (data["start"], data["end"], data["capped"]) == (10, 14, False)
    assert [p["paragraph_index"] for p in data["paragraphs"]] == [10, 11, 12, 13, 14]
    assert data["paragraphs"][0] == {
        "paragraph_index": 10,
        "paragraph_type": "narration",
        "text": "book_range 第 10 段的原文。",
    }
    assert data["paragraphs"][2]["paragraph_type"] == "dialogue"

    # 单段区间同样合法;超出书末的区间只返回存在的段
    single = client.get(f"{PREFIX}/books/book_range/paragraphs", params={"start": 29, "end": 29})
    assert [p["paragraph_index"] for p in single.json()["data"]["paragraphs"]] == [29]
    beyond = client.get(f"{PREFIX}/books/book_range/paragraphs", params={"start": 28, "end": 40})
    assert [p["paragraph_index"] for p in beyond.json()["data"]["paragraphs"]] == [28, 29]
    assert beyond.json()["data"]["capped"] is False


def test_paragraph_range_is_capped_to_80_paragraphs_per_call(client: TestClient) -> None:
    with SessionLocal() as session:
        _seed_book(session, "book_cap", paragraphs=120)
        session.commit()

    resp = client.get(f"{PREFIX}/books/book_cap/paragraphs", params={"start": 0, "end": 119})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["paragraphs"]) == 80
    assert data["paragraphs"][0]["paragraph_index"] == 0
    assert data["paragraphs"][-1]["paragraph_index"] == 79
    # 返回的 end 是实际截到的位置,调用方据此续读
    assert (data["start"], data["end"], data["capped"]) == (0, 79, True)

    # 正好 80 段不算截断;从中间起读同样按 start 截
    exact = client.get(f"{PREFIX}/books/book_cap/paragraphs", params={"start": 0, "end": 79})
    assert exact.json()["data"]["capped"] is False
    assert len(exact.json()["data"]["paragraphs"]) == 80
    offset = client.get(f"{PREFIX}/books/book_cap/paragraphs", params={"start": 30, "end": 119})
    assert (offset.json()["data"]["start"], offset.json()["data"]["end"]) == (30, 109)
    assert len(offset.json()["data"]["paragraphs"]) == 80


def test_paragraph_range_unknown_book_is_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/books/book_missing/paragraphs", params={"start": 0, "end": 5})
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "STYLE_REFERENCE_BOOK_NOT_FOUND"


def test_paragraph_range_rejects_inverted_or_negative_range(client: TestClient) -> None:
    with SessionLocal() as session:
        _seed_book(session, "book_bad", paragraphs=10)
        session.commit()

    inverted = client.get(f"{PREFIX}/books/book_bad/paragraphs", params={"start": 5, "end": 3})
    assert inverted.status_code == 400
    assert inverted.json()["error"]["code"] == "STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID"

    negative = client.get(f"{PREFIX}/books/book_bad/paragraphs", params={"start": -1, "end": 3})
    assert negative.status_code == 400
    assert negative.json()["error"]["code"] == "STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID"

    # 缺参数走统一的请求校验封套(不是 500)
    missing = client.get(f"{PREFIX}/books/book_bad/paragraphs", params={"start": 0})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# generation_summary.style_windows
# ---------------------------------------------------------------------------


def _seed_scene(session: Session, *, project_id: str, scene_id: str = "CH701_SC01") -> SceneRunState:
    chapter_id = "CH701"
    session.add(StoryProject(project_id=project_id, title="WP4 windows", outline_text=""))
    session.add(
        ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g")
    )
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A"],
            location="旧城门廊",
            scene_goal="reveal",
            beats_json=["arrival"],
            must_include_text=None,
            target_length_band="short",
            scene_type="reveal",
            is_chapter_last=0,
        )
    )
    # 生成摘要要能解析出 llm_call:中性稿行指向一条 LlmCall
    session.add(
        LlmCall(
            llm_call_id=f"llm_{scene_id}",
            step="neutral_draft",
            scope_type="scene",
            scope_id=scene_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
        )
    )
    session.add(
        SceneDraft(
            row_id=f"draft_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            stage="neutral_draft",
            content="x",
            source_bundle_id="bundle_v2",
            source_bundle_hash="h_v2",
            generation_llm_call_id=f"llm_{scene_id}",
        )
    )
    state = SceneRunState(
        scene_id=scene_id,
        scene_status="near_final",
        current_bundle_id="bundle_v2",
        current_neutral_draft_row_id=f"draft_{scene_id}",
    )
    session.add(state)
    session.commit()
    return state


def _attempt(
    scene_id: str,
    *,
    step: str,
    bundle_id: str,
    refs: list | None,
    status: str = "completed",
    profile_ids: list[str] | None = None,
) -> AttemptTracker:
    runtime: dict = {"outcome": "hit", "profile_ids": profile_ids or []}
    if refs is not None:
        runtime["few_shot_window_refs"] = refs
    return AttemptTracker(
        scene_id=scene_id,
        chapter_id="CH701",
        step=step,
        status=status,
        source_bundle_id=bundle_id,
        details_json={"style_reference_runtime": runtime},
    )


_WINDOW_A = {
    "start": 120,
    "end": 179,
    "chapter": 3,
    "position": "opening",
    "paragraph_type": "narration",
    "paragraphs": 60,
    "chars": 3820,
}
_WINDOW_B = {
    "start": 640,
    "end": 662,
    "chapter": 9,
    "position": "closing",
    "paragraph_type": "dialogue",
    "paragraphs": 23,
    "chars": 1510,
}


def test_generation_summary_exposes_style_windows_scoped_to_the_current_bundle(session: Session) -> None:
    state = _seed_scene(session, project_id="proj_wp4_scope")
    scene_id = state.scene_id
    _seed_book(session, "book_global", paragraphs=5)
    _seed_book(session, "book_project", paragraphs=5)
    _seed_profile(session, book_id="book_global", profile_id="profile_global")
    _seed_profile(session, book_id="book_project", profile_id="profile_project")
    session.add_all(
        [
            # 上一次运行(bundle_v1)的窗口——不得混进本次运行
            _attempt(scene_id, step="style_draft", bundle_id="bundle_v1", refs=[_WINDOW_B],
                     profile_ids=["profile_global"]),
            # 本次运行:失败的风格稿尝试不算(文本不是它写的)
            _attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[_WINDOW_B], status="failed",
                     profile_ids=["profile_global", "profile_project"]),
            # 本次运行的风格稿:契约层序 global → project,样例窗口来自最具体层(末尾)的书
            _attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[_WINDOW_A, _WINDOW_B],
                     profile_ids=["profile_global", "profile_project"]),
        ]
    )
    session.commit()

    summary = _serialize_generation_summary(session, scene_id, state)
    assert summary is not None
    assert summary["style_windows"] == {
        "step": "style_draft",
        "profile_id": "profile_project",
        "book_id": "book_project",
        "windows": [_WINDOW_A, _WINDOW_B],
    }

    # 切到上一次运行的 bundle → 只看到那次的窗口(书按那次契约的画像解析)
    state.current_bundle_id = "bundle_v1"
    session.commit()
    previous = _serialize_generation_summary(session, scene_id, state)["style_windows"]
    assert previous["windows"] == [_WINDOW_B]
    assert (previous["profile_id"], previous["book_id"]) == ("profile_global", "book_global")

    # 没有带窗口尝试的 bundle → null;解析不出 bundle 同样 null(不做无范围回读)
    state.current_bundle_id = "bundle_v3"
    session.commit()
    assert _serialize_generation_summary(session, scene_id, state)["style_windows"] is None


def test_style_windows_prefers_style_draft_then_first_draft_then_rewrite(session: Session) -> None:
    state = _seed_scene(session, project_id="proj_wp4_order")
    scene_id = state.scene_id
    _seed_book(session, "book_one", paragraphs=5)
    _seed_profile(session, book_id="book_one", profile_id="profile_one")

    # 只有重写稿带窗口 → 兜底到重写稿
    session.add(_attempt(scene_id, step="scene_literary_rewrite", bundle_id="bundle_v2", refs=[_WINDOW_B],
                         profile_ids=["profile_one"]))
    session.commit()
    assert _serialize_generation_summary(session, scene_id, state)["style_windows"]["step"] == "scene_literary_rewrite"

    # 风格直起的首稿(中性步位)带窗口 → 优先于重写稿
    session.add(_attempt(scene_id, step="neutral_draft", bundle_id="bundle_v2", refs=[_WINDOW_A],
                         profile_ids=["profile_one"]))
    session.commit()
    first_draft = _serialize_generation_summary(session, scene_id, state)["style_windows"]
    assert (first_draft["step"], first_draft["windows"]) == ("neutral_draft", [_WINDOW_A])

    # 风格稿没有窗口(注入未命中 / 回退)→ 越过它,仍取首稿
    session.add(_attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[], profile_ids=["profile_one"]))
    session.add(_attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=None, profile_ids=["profile_one"]))
    session.commit()
    assert _serialize_generation_summary(session, scene_id, state)["style_windows"]["step"] == "neutral_draft"

    # 风格稿带窗口 → 风格稿优先(最近一次 completed)
    session.add(_attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[_WINDOW_B],
                         profile_ids=["profile_one"]))
    session.commit()
    styled = _serialize_generation_summary(session, scene_id, state)["style_windows"]
    assert (styled["step"], styled["windows"]) == ("style_draft", [_WINDOW_B])


def test_style_windows_normalizes_refs_and_tolerates_missing_profile(session: Session) -> None:
    state = _seed_scene(session, project_id="proj_wp4_norm")
    scene_id = state.scene_id
    session.add(
        _attempt(
            scene_id,
            step="style_draft",
            bundle_id="bundle_v2",
            refs=[
                {"start": 4, "end": 2},  # 倒置 → 丢弃
                {"end": 9},  # 无起点 → 丢弃
                "garbage",
                {"start": "0", "end": 5},  # 非整数 → 丢弃
                {"start": 10, "end": 12, "position": None, "paragraph_type": None},  # 缺读数 → 补 0 / 按区间算段数
            ],
            profile_ids=["profile_deleted"],  # 画像已删(参考书删书级联)→ 只回 profile_id,book_id 为 null
        )
    )
    session.commit()

    windows = _serialize_generation_summary(session, scene_id, state)["style_windows"]
    assert windows == {
        "step": "style_draft",
        "profile_id": "profile_deleted",
        "book_id": None,
        "windows": [
            {
                "start": 10,
                "end": 12,
                "position": "",
                "paragraph_type": "",
                "chapter": 0,
                "paragraphs": 3,
                "chars": 0,
            }
        ],
    }

    # 全部条目都不合法 → 视同没有窗口
    session.add(
        _attempt(scene_id, step="style_draft", bundle_id="bundle_v2", refs=[{"start": 3, "end": 1}, 42])
    )
    session.commit()
    assert _serialize_generation_summary(session, scene_id, state)["style_windows"] is None

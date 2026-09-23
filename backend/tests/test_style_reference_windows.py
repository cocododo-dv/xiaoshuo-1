"""风格参考 v3 持久化窗口索引(windows.py)与段落根哈希快速路径(paragraph_root.py)。

全部用合成书(无真实作者原文):章题 + 对白 / 叙述交替的段落,按种子生成,窗口之间有自然的差异。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph, StyleReferenceWindow
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import windows as W
from novel_system.services.style_reference.measure import FEATURE_NAMES, KERNEL_VERSION, kernel_features, measure_text
from novel_system.services.style_reference.paragraph_root import (
    compute_paragraph_root_fast,
    ensure_paragraph_root,
    patch_book_stats,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_factories import make_book
from novel_system.services.style_reference.runtime_contract import compute_paragraph_root
from novel_system.services.style_reference.structure import compute_structure_card, split_book_chapters

_NAMES = ("老周", "小满", "阿禾", "陈叔")
_PLACES = ("院子", "渡口", "灶间", "巷口", "桥头")
_DIALOGUE = (
    "“{a}，你到底去不去？”{b}把灯芯拨小了些。",
    "“去。”{a}说，“等雨停了就走。”",
    "“你听见了吗？”",
    "{b}问：“船什么时候到？”",
    "“别问了，吃饭吧。”",
)
_NARRATION = (
    "{a}没有回答，先把袖口的水拧了拧，{p}里两只碗一只是干的。",
    "雨又密起来了，{p}那株桂树被打得低了头，叶子上的水一颗一颗落在石阶上。",
    "{a}在{p}站了很久，直到天色暗下去，才慢慢转身。",
    "他想起三年前的那个冬天，{b}也是这样站在{p}，一句话也不说。",
    "风从{p}吹过来，带着一点河水的腥气。",
)


def synthetic_rows(seed: str = "a", chapters: int = 8, per_chapter: int = 70) -> list[dict]:
    """合成书的段落行(章题 + 正文):对白 / 叙述比例随章变化,段长随机。"""
    rng = random.Random(seed)
    rows: list[dict] = []

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for chapter in range(1, chapters + 1):
        add(f"第{chapter}章 灯下", "transition")
        dialogue_bias = 0.25 + 0.5 * rng.random()
        for _ in range(per_chapter):
            a, b = rng.sample(_NAMES, 2)
            p = rng.choice(_PLACES)
            if rng.random() < dialogue_bias:
                text = rng.choice(_DIALOGUE).format(a=a, b=b, p=p)
                ptype = "dialogue"
            else:
                text = "".join(rng.choice(_NARRATION).format(a=a, b=b, p=p) for _ in range(rng.randint(1, 3)))
                ptype = "narration"
            add(text, ptype)
    return rows


def seed_book(session, book_id: str, rows: list[dict], *, stats: dict | None = None) -> str:
    make_book(session, book_id, title="合成书", paragraphs=rows, stats=stats)
    session.commit()
    return book_id


def _stats(book_id: str) -> dict:
    with SessionLocal() as other:
        return dict(other.get(StyleReferenceBook, book_id).stats_json or {})


# ---------------------------------------------------------------------------
# 段落根哈希
# ---------------------------------------------------------------------------


def test_fast_root_matches_the_contract_root_and_is_written_back(session) -> None:
    book_id = seed_book(session, "win_root", synthetic_rows("root", chapters=2, per_chapter=10))
    fast = compute_paragraph_root_fast(session, book_id)
    assert fast == compute_paragraph_root(StyleReferenceRepository(session), book_id)
    assert _stats(book_id).get("paragraph_root_sha256") is None
    assert ensure_paragraph_root(session, book_id) == fast
    session.commit()
    stored = _stats(book_id)
    assert (stored["paragraph_root_sha256"], stored["paragraph_count"]) == fast
    assert compute_paragraph_root_fast(session, "win_missing") == ("", 0)


def test_stats_patch_merges_keys_instead_of_overwriting(session) -> None:
    """同一行 stats_json 还有分类作业在写:窗口索引只原子合并自己的键,别人的写不丢。"""
    book_id = seed_book(session, "win_patch", synthetic_rows("patch", chapters=1, per_chapter=5), stats={"a": 1})
    book = session.get(StyleReferenceBook, book_id)
    assert book.stats_json == {"a": 1}
    with SessionLocal() as other:  # 另一个会话(分类作业)先写了自己的键
        row = other.get(StyleReferenceBook, book_id)
        row.stats_json = {**row.stats_json, "paragraph_types_revision": 3}
        other.commit()
    patch_book_stats(session, book_id, {"window_index": {"version": "x"}})
    session.commit()
    assert _stats(book_id) == {"a": 1, "paragraph_types_revision": 3, "window_index": {"version": "x"}}
    # 会话里的书对象已重读
    assert session.get(StyleReferenceBook, book_id).stats_json["paragraph_types_revision"] == 3


# ---------------------------------------------------------------------------
# 建索引
# ---------------------------------------------------------------------------


def test_ensure_builds_windows_with_kernel_features_and_marker(session) -> None:
    rows = synthetic_rows("build")
    book_id = seed_book(session, "win_build", rows)
    windows = W.ensure_window_index(session, book_id)
    session.commit()
    assert len(windows) >= 8
    stats = _stats(book_id)
    root = stats["paragraph_root_sha256"]
    assert root == compute_paragraph_root(StyleReferenceRepository(session), book_id)[0]
    marker = stats["window_index"]
    assert marker["version"] == W.WINDOW_INDEX_VERSION == "exemplar_windows_v3"
    assert marker["root"] == root and marker["types_revision"] == 0 and marker["kernel_version"] == KERNEL_VERSION
    assert marker["window_count"] == len(windows)
    chapters, _markers = split_book_chapters(rows)
    card = compute_structure_card(rows)
    assert card["chapter_count"] == len(chapters) == 8
    previous_end = -1
    for number, window in enumerate(windows, start=1):
        assert window.window_no == number and window.index_version == W.WINDOW_INDEX_VERSION
        assert window.root_sha256 == root
        assert window.start_index > previous_end and window.end_index >= window.start_index
        previous_end = window.end_index
        assert 1 <= window.chapter_no <= card["chapter_count"]
        assert window.position in {"opening", "closing", "middle", "whole"}
        assert 600 <= window.chars <= 5000 and 1 <= window.paragraph_count <= 75
        assert tuple(window.features_json) == FEATURE_NAMES
        # 特征就是在 window_text 那段文字上测的;对白占比取测量核的唯一定义
        text = W.window_text(session, window)
        assert kernel_features(measure_text(text)) == window.features_json
        assert window.dialogue_share == window.features_json["dialogue_char_share"]
        assert set(window.type_mix_json) <= {"dialogue", "narration"}
        assert sum(window.type_mix_json.values()) == pytest.approx(1.0, abs=1e-3)
        assert window.typicality <= 0.0
        assert window.tags_json is None
    # 典型度按本书自己的窗口分布排序,不全相同
    assert len({w.typicality for w in windows}) > 1
    batch = W.window_texts(session, windows)
    assert batch == {w.window_no: W.window_text(session, w) for w in windows}
    # 隔得远的窗口各查各的范围,结果一样
    sparse = [windows[0], windows[-1]]
    assert windows[-1].start_index - windows[0].end_index > 200
    assert W.window_texts(session, sparse) == {w.window_no: batch[w.window_no] for w in sparse}
    ref = W.window_ref(windows[0])
    assert ref["window_no"] == 1 and "text" not in ref


def test_warm_calls_reuse_the_rows(session, monkeypatch) -> None:
    book_id = seed_book(session, "win_warm", synthetic_rows("warm", chapters=3))
    first = W.ensure_window_index(session, book_id)
    session.commit()
    monkeypatch.setattr(W, "_build", lambda *a, **k: pytest.fail("warm path must not rebuild"))
    monkeypatch.setattr(W, "_refresh_types", lambda *a, **k: pytest.fail("warm path must not refresh"))
    with SessionLocal() as other:
        again = W.ensure_window_index(other, book_id)
        assert [w.window_id for w in again] == [w.window_id for w in first]
        assert [w.window_id for w in W.load_windows(other, book_id)] == [w.window_id for w in first]


def test_types_revision_refreshes_type_mix_in_place_and_keeps_tags(session, monkeypatch) -> None:
    book_id = seed_book(session, "win_types", synthetic_rows("types", chapters=3))
    windows = W.ensure_window_index(session, book_id)
    assert W.set_window_tags(
        session, book_id, {1: {"situations": ["对峙审问"], "moods": ["紧张"], "gist": "追问"}}, tags_version="t1"
    ) == 1
    session.commit()
    ids = [w.window_id for w in windows]
    first = windows[0]
    # 分类作业:第一窗的段落全部改成心理段,类型版本 +1
    with SessionLocal() as other:
        for paragraph in other.scalars(
            select(StyleReferenceParagraph).where(
                StyleReferenceParagraph.book_id == book_id,
                StyleReferenceParagraph.paragraph_index >= first.start_index,
                StyleReferenceParagraph.paragraph_index <= first.end_index,
            )
        ):
            paragraph.paragraph_type = "psychology"
        book = other.get(StyleReferenceBook, book_id)
        book.stats_json = {**book.stats_json, "paragraph_types_revision": 1}
        other.commit()
    monkeypatch.setattr(W, "_build", lambda *a, **k: pytest.fail("a types-only change must not rebuild"))
    with SessionLocal() as other:
        refreshed = W.ensure_window_index(other, book_id)
        other.commit()
        assert [w.window_id for w in refreshed] == ids
        assert refreshed[0].type_mix_json == {"psychology": 1.0}
        assert refreshed[0].tags_json["situations"] == ["对峙审问"] and refreshed[0].tags_version == "t1"
        assert refreshed[0].features_json == first.features_json
    assert _stats(book_id)["window_index"]["types_revision"] == 1


def test_text_change_rebuilds_and_drops_stale_rows_and_tags(session) -> None:
    book_id = seed_book(session, "win_text", synthetic_rows("text", chapters=3))
    windows = W.ensure_window_index(session, book_id)
    W.set_window_tags(session, book_id, {w.window_no: {"moods": ["平静"]} for w in windows}, tags_version="t1")
    session.commit()
    old_root = windows[0].root_sha256
    # 刷新工具改了段落文本:按契约 pop 根哈希
    with SessionLocal() as other:
        paragraph = other.scalars(
            select(StyleReferenceParagraph).where(
                StyleReferenceParagraph.book_id == book_id,
                StyleReferenceParagraph.paragraph_index == windows[0].start_index,
            )
        ).one()
        paragraph.text = paragraph.text + "他又补了一句。"
        book = other.get(StyleReferenceBook, book_id)
        stats = dict(book.stats_json)
        stats.pop("paragraph_root_sha256")
        stats.pop("paragraph_count")
        book.stats_json = stats
        other.commit()
    with SessionLocal() as other:
        rebuilt = W.ensure_window_index(other, book_id)
        other.commit()
        assert rebuilt and all(w.root_sha256 != old_root for w in rebuilt)
        assert all(w.tags_json is None for w in rebuilt)  # 正文变了,旧标签不再可信
        total = other.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == book_id)).all()
        assert len(total) == len(rebuilt)  # 旧根哈希的行已删除


def test_kernel_change_rebuilds_features_but_keeps_tags(session) -> None:
    book_id = seed_book(session, "win_kernel", synthetic_rows("kernel", chapters=3))
    windows = W.ensure_window_index(session, book_id)
    W.set_window_tags(session, book_id, {2: {"situations": ["日常闲谈"]}}, tags_version="t1")
    session.commit()
    ids = [w.window_id for w in windows]
    marker = dict(_stats(book_id)["window_index"])
    patch_book_stats(session, book_id, {"window_index": {**marker, "kernel_version": "measure_v0"}})
    session.commit()
    with SessionLocal() as other:
        rebuilt = W.ensure_window_index(other, book_id)
        other.commit()
        assert [w.window_id for w in rebuilt] == ids  # 同一段落表:边界与 id 都不变
        assert rebuilt[1].tags_json["situations"] == ["日常闲谈"] and rebuilt[1].tags_version == "t1"
        assert rebuilt[0].tags_json is None
    assert _stats(book_id)["window_index"]["kernel_version"] == KERNEL_VERSION


def test_set_window_tags_keeps_to_the_vocabularies(session) -> None:
    book_id = seed_book(session, "win_tags", synthetic_rows("tags", chapters=2))
    W.ensure_window_index(session, book_id)
    written = W.set_window_tags(
        session,
        book_id,
        {
            1: {"situations": ["对峙审问", "不存在的场面"], "moods": ["紧张", "诙谐", "伤感"], "devices": ["降维比喻", "别的"], "gist": "x" * 80},
            "2": {"situations": ["赶路转场"]},
            999: {"situations": ["日常闲谈"]},
        },
        tags_version="window_tags_v1",
        devices=["降维比喻"],
    )
    session.commit()
    assert written == 2
    windows = W.load_windows(session, book_id)
    first = windows[0].tags_json
    assert first["situations"] == ["对峙审问"]
    assert first["moods"] == ["紧张", "诙谐"]
    assert first["devices"] == ["降维比喻"]
    assert len(first["gist"]) == 40
    assert windows[1].tags_json["situations"] == ["赶路转场"]


def test_front_matter_and_paratext_never_enter_windows(session) -> None:
    body = list(synthetic_rows("front", chapters=2))
    # 正文中间插一条脚注
    body.insert(20, {"text": "[1] 这是一条脚注，不是作者的文字。", "paragraph_type": "narration"})
    front = [
        {"text": "某书合集", "paragraph_type": "transition"},
        {"text": "某某 著", "paragraph_type": "transition"},
        {"text": "内容简介：一个关于雨夜的故事。", "paragraph_type": "narration"},
    ]
    rows = [dict(row, paragraph_index=index) for index, row in enumerate(front + body)]
    book_id = seed_book(session, "win_front", rows)
    windows = W.ensure_window_index(session, book_id)
    assert windows[0].chapter_no == 1 and windows[0].start_index > 3  # 书名页不占章号、不入窗
    assert windows[0].start_index <= 20 <= windows[0].end_index  # 脚注落在第一窗的范围里
    texts = W.window_texts(session, windows)
    assert all("某某 著" not in text and "脚注" not in text for text in texts.values())
    assert max(w.chapter_no for w in windows) == 2


def test_missing_or_empty_books_have_no_windows(session, monkeypatch) -> None:
    assert W.ensure_window_index(session, "win_nobody") == []
    book_id = seed_book(session, "win_empty", [])
    assert W.ensure_window_index(session, book_id) == []
    assert W.load_windows(session, book_id) == []
    # 切不出一窗(每章都不到 600 字)的小书:标记记下 0 窗,之后不再每次重切
    tiny = seed_book(session, "win_tiny", synthetic_rows("tiny", chapters=2, per_chapter=3))
    assert W.ensure_window_index(session, tiny) == []
    session.commit()
    assert _stats(tiny)["window_index"]["window_count"] == 0
    monkeypatch.setattr(W, "_build", lambda *a, **k: pytest.fail("an empty index must not be rebuilt"))
    assert W.ensure_window_index(session, tiny) == []

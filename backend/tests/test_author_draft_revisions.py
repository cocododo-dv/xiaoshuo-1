"""author-draft 修订快照（FE-ALIGN F2：成稿中心版本对比的数据底座）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from novel_system.db.models import AuthorDraftRevision
from novel_system.services.author_drafts import revisions as revisions_module
from tests.support.seed import seed_chapter, seed_scene


class _RevisionClock:
    """修订快照用的时钟（``revisions.utcnow``：合并判断与新行的时间戳都取它）。测试把保存放进指定的 5 分钟时段，
    不会碰巧跨过时段边界（真实时钟下约三百次里有一次）。"""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> str:
        return self.at.isoformat()

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> _RevisionClock:
    # 远离真实时间的一个 5 分钟时段、刚开始 30 秒：后面几次保存都还在这一时段里
    clock = _RevisionClock(datetime(2025, 1, 15, 3, 0, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(revisions_module, "utcnow", clock)
    return clock


def _create_chapter(chapter_id: str) -> None:
    seed_chapter(
        chapter_id,
        planned_scene_count=1,
        chapter_goal=f"目标 {chapter_id}",
        main_plot_push="推进主线",
        emotional_target="情绪转折",
        ending_effect="留下余味",
    )


def _create_scene(scene_id: str, *, chapter_id: str) -> None:
    seed_scene(
        scene_id,
        chapter_id=chapter_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="档案室",
        scene_goal=f"场景目标 {scene_id}",
        beats_json=["发现", "选择"],
        exit_change="关系改变",
        hook="尾钩",
        target_length_band="medium",
        scene_type="reunion",
        is_chapter_last=1,
    )


def _ensure_draft(client, scene_id: str) -> dict:
    response = client.post(
        f"/api/v1/author-drafts/scene/{scene_id}/ensure",
        headers={"X-Idempotency-Key": f"rev-ensure-{scene_id}"},
    )
    assert response.status_code == 200
    return response.json()["data"]["draft"]


def _save(client, draft_id: str, base_revision_no: int, content: str):
    return client.patch(
        f"/api/v1/author-drafts/{draft_id}",
        json={"content": content, "base_revision_no": base_revision_no},
    )


def _age_latest_snapshot(session, draft_id: str, *, minutes: int = 10) -> None:
    """把最近一份快照挪到上一个 5 分钟时段——模拟作者写了一会儿之后再接着写。"""
    row = (
        session.query(AuthorDraftRevision)
        .filter_by(draft_id=draft_id)
        .order_by(AuthorDraftRevision.revision_no.desc())
        .first()
    )
    row.created_at = (datetime.fromisoformat(row.created_at) - timedelta(minutes=minutes)).isoformat()
    session.commit()


def test_autosaves_in_one_window_share_one_snapshot(client, session, clock) -> None:
    """自动保存按 5 分钟时段合并（批准 #8）：同一时段里接连保存只留一份快照，存的是最新的正文。"""
    _create_chapter("CH_REV_1")
    _create_scene("SC_REV_1", chapter_id="CH_REV_1")
    draft = _ensure_draft(client, "SC_REV_1")

    assert _save(client, draft["draft_id"], 1, "第一版正文。").status_code == 200
    clock.advance(seconds=40)
    assert _save(client, draft["draft_id"], 2, "第二版正文，比第一版长一点。").status_code == 200

    response = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["revision_no"] == 3
    # 倒序：v3（这个时段最新的保存）、v1（ensure 建稿那一版，永远留着）
    assert [item["revision_no"] for item in data["items"]] == [3, 1]
    assert all("content" not in item for item in data["items"])  # 列表轻量，不带正文
    latest = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/3").json()["data"]["revision"]
    assert latest["content"] == "第二版正文，比第一版长一点。"
    assert client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/2").status_code == 404


def test_revision_content_is_retrievable(client, session) -> None:
    _create_chapter("CH_REV_2")
    _create_scene("SC_REV_2", chapter_id="CH_REV_2")
    draft = _ensure_draft(client, "SC_REV_2")
    assert _save(client, draft["draft_id"], 1, "潮水在夜里退去。").status_code == 200
    _age_latest_snapshot(session, draft["draft_id"])
    assert _save(client, draft["draft_id"], 2, "潮水在夜里退去，露出一行脚印。").status_code == 200

    response = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/2")
    assert response.status_code == 200
    revision = response.json()["data"]["revision"]
    assert revision["content"] == "潮水在夜里退去。"
    assert revision["origin"] == "edited"
    assert revision["words"] > 0

    missing = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/99")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "AUTHOR_DRAFT_REVISION_NOT_FOUND"


def test_the_promoted_revision_is_never_folded_into_a_later_autosave(client, session, clock) -> None:
    from novel_system.db.models import AuthorDraft

    _create_chapter("CH_REV_4")
    _create_scene("SC_REV_4", chapter_id="CH_REV_4")
    draft = _ensure_draft(client, "SC_REV_4")
    assert _save(client, draft["draft_id"], 1, "晋升成权威正文的那一版。").status_code == 200
    row = session.get(AuthorDraft, draft["draft_id"])
    row.last_promoted_revision_no = 2  # 成稿中心晋升了第 2 版
    session.commit()
    assert _save(client, draft["draft_id"], 2, "晋升之后接着改。").status_code == 200
    assert _save(client, draft["draft_id"], 3, "晋升之后又改了一句。").status_code == 200

    session.expire_all()
    rows = {row.revision_no: row.content for row in session.query(AuthorDraftRevision).filter_by(draft_id=draft["draft_id"])}
    assert rows == {1: "", 2: "晋升成权威正文的那一版。", 4: "晋升之后又改了一句。"}


def test_the_revision_list_pages_when_asked(client, session) -> None:
    _create_chapter("CH_REV_5")
    _create_scene("SC_REV_5", chapter_id="CH_REV_5")
    draft = _ensure_draft(client, "SC_REV_5")
    for revision_no in (1, 2, 3):
        assert _save(client, draft["draft_id"], revision_no, f"第{revision_no}版。").status_code == 200
        _age_latest_snapshot(session, draft["draft_id"])

    first = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions?limit=2").json()["data"]
    assert [item["revision_no"] for item in first["items"]] == [4, 3]
    assert first["pagination"]["has_next"] is True and first["pagination"]["total"] == 4
    rest = client.get(
        f"/api/v1/author-drafts/{draft['draft_id']}/revisions",
        params={"limit": 2, "cursor": first["pagination"]["next_cursor"]},
    ).json()["data"]
    assert [item["revision_no"] for item in rest["items"]] == [2, 1]
    assert rest["pagination"]["has_next"] is False
    # 不给分页参数：照旧一次给全，不带 pagination
    everything = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions").json()["data"]
    assert [item["revision_no"] for item in everything["items"]] == [4, 3, 2, 1]
    assert "pagination" not in everything


def test_conflict_save_does_not_snapshot(client, session) -> None:
    _create_chapter("CH_REV_3")
    _create_scene("SC_REV_3", chapter_id="CH_REV_3")
    draft = _ensure_draft(client, "SC_REV_3")
    assert _save(client, draft["draft_id"], 1, "正式的一版。").status_code == 200

    conflict = _save(client, draft["draft_id"], 1, "基于旧版的改写。")
    assert conflict.status_code == 409

    rows = session.query(AuthorDraftRevision).filter_by(draft_id=draft["draft_id"]).all()
    assert {row.revision_no for row in rows} == {1, 2}


def test_unknown_draft_revisions_404(client, session) -> None:
    response = client.get("/api/v1/author-drafts/author_draft_missing/revisions")
    assert response.status_code == 404


# ------------------------------------------------------------------ 终审 A-1 / A-2：合并不能吃掉作者要找回的那一版

# 合成的正文（公开仓库：只用测试世界里的人和地方）
_HANDWRITTEN = "<p>雨停之后，林昭把那封没寄出的旧信压在灯座下面。</p><p>门外有人敲了三下，她没有应。</p>"
_ADOPTED = "<p>窗外的风把案卷吹得哗哗作响，他终于说出了那个名字。</p>"


def _paragraphs(mark: str, count: int) -> str:
    return "".join(
        f"<p>{mark}第{index}段：雨城的风从河面上压过来，她把折好的旧信塞回口袋，沿着湿滑的石阶一级一级往下走。</p>"
        for index in range(1, count + 1)
    )


def _rows(session, draft_id: str) -> dict[int, tuple[str, str]]:
    session.expire_all()
    return {
        row.revision_no: (row.origin, row.content)
        for row in session.query(AuthorDraftRevision).filter_by(draft_id=draft_id)
    }


def test_an_adoption_keeps_the_handwritten_autosave_of_its_window(client, session, clock) -> None:
    """终审 A-1：AI 起草台「采纳并归档」（确认覆盖作者稿：adopt-current + exact_author_draft）与作者刚手写的那次自动
    保存落在同一个 5 分钟时段——采纳单独一行（``adopted``），手写的那一版留着，版本对比两版都取得到。批准 #8 是
    「自动保存至多一份 + 每次采纳一份」：以前采纳走自动保存的合并，把这一时段的手写稿原地改成了 AI 稿，手写稿在服务器
    上一份都不剩。"""
    _create_chapter("CH_REV_ADOPT")
    _create_scene("SC_REV_ADOPT", chapter_id="CH_REV_ADOPT")
    draft = _ensure_draft(client, "SC_REV_ADOPT")
    assert _save(client, draft["draft_id"], 1, _HANDWRITTEN).status_code == 200
    clock.advance(seconds=60)

    adopted = client.post(
        "/api/v1/scenes/SC_REV_ADOPT/adopt-current",
        json={
            "accepted_warning_codes": [],
            "exact_author_draft": {
                "draft_id": draft["draft_id"],
                "base_revision_no": 2,
                "expected_current_final_scene_row_id": None,
                "content": _ADOPTED,
            },
        },
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["data"]["draft_revision_no"] == 3

    items = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions").json()["data"]["items"]
    assert [(item["revision_no"], item["origin"]) for item in items] == [(3, "adopted"), (2, "edited"), (1, "created")]
    handwritten = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/2").json()["data"]["revision"]
    assert handwritten["content"] == _HANDWRITTEN
    assert client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/3").json()["data"]["revision"]["content"] == _ADOPTED

    # 采纳之后在同一时段里接着改：另起一行，采纳的那一行原样
    clock.advance(seconds=30)
    assert _save(client, draft["draft_id"], 3, _ADOPTED + "<p>补一句。</p>").status_code == 200
    rows = _rows(session, draft["draft_id"])
    assert sorted(rows) == [1, 2, 3, 4]
    assert rows[3] == ("adopted", _ADOPTED)


def test_a_wipe_in_the_window_keeps_the_text_it_threw_away(client, session, clock) -> None:
    """终审 A-2：全选删除（再敲一个字）与上一次自动保存落在同一个 5 分钟时段——不并进这一时段的快照、另起一行，删掉
    之前的整段正文留在上一行，版本对比取得回来；删完接着打字照旧并进新起的这一行。以前它把这一时段的快照原地改成了
    「x」，刚写的正文在服务器上一份都不剩。"""
    _create_chapter("CH_REV_WIPE")
    _create_scene("SC_REV_WIPE", chapter_id="CH_REV_WIPE")
    draft = _ensure_draft(client, "SC_REV_WIPE")
    long_text = _paragraphs("案卷", 8)
    assert _save(client, draft["draft_id"], 1, long_text).status_code == 200
    clock.advance(seconds=20)
    assert _save(client, draft["draft_id"], 2, "<p>x</p>").status_code == 200
    clock.advance(seconds=5)
    assert _save(client, draft["draft_id"], 3, "<p>xy</p>").status_code == 200

    assert _rows(session, draft["draft_id"]) == {
        1: ("created", ""),
        2: ("edited", long_text),
        4: ("edited", "<p>xy</p>"),
    }
    kept = client.get(f"/api/v1/author-drafts/{draft['draft_id']}/revisions/2").json()["data"]["revision"]
    assert kept["content"] == long_text


def test_a_large_rewrite_in_the_window_keeps_the_passage_it_replaced(client, session, clock) -> None:
    """终审 A-2：接受一大段改写（行内改写 / 按诊断改写换掉这一时段刚写的几段）也是整段删改：改写之前的那几段留在这一
    时段原来那一行。改写前后字数差不多，只看「字数少了一半」抓不到它。"""
    from novel_system.services.writing_stats import count_words

    _create_chapter("CH_REV_REWRITE")
    _create_scene("SC_REV_REWRITE", chapter_id="CH_REV_REWRITE")
    draft = _ensure_draft(client, "SC_REV_REWRITE")
    earlier = _paragraphs("旧信", 6)
    assert _save(client, draft["draft_id"], 1, earlier).status_code == 200
    clock.advance(minutes=10)  # 下一个时段接着写
    typed = earlier + _paragraphs("雨夜", 6)
    rewritten = earlier + "".join(
        f"<p>改写稿第{index}段：雨城的风卷着水汽扑上石阶，林昭攥紧口袋里那封旧信，脚步一点也没有放慢，始终不肯回头。</p>"
        for index in range(1, 7)
    )
    assert count_words(rewritten) >= count_words(typed)
    assert _save(client, draft["draft_id"], 2, typed).status_code == 200
    clock.advance(seconds=30)
    assert _save(client, draft["draft_id"], 3, rewritten).status_code == 200

    rows = _rows(session, draft["draft_id"])
    assert sorted(rows) == [1, 2, 3, 4]
    assert rows[3] == ("edited", typed)  # 改写之前的那几段还在
    assert rows[4] == ("edited", rewritten)


def test_ordinary_typing_still_shares_one_snapshot_per_window(client, session, clock) -> None:
    """批准 #8 照旧：打字、退格、删掉半句、接着写两段——同一时段里只留一份快照（整段删改才另起一行）。"""
    _create_chapter("CH_REV_TYPING")
    _create_scene("SC_REV_TYPING", chapter_id="CH_REV_TYPING")
    draft = _ensure_draft(client, "SC_REV_TYPING")
    base = _paragraphs("案卷", 6)
    texts = [
        base,
        base + "<p>她停下。</p>",
        base + "<p>她停下来。</p>",
        base + "<p>她停下来，回头看了一眼。</p>",
        base + "<p>她停下来，回头。</p>",
        base + _paragraphs("雨夜", 2),
    ]
    revision_no = 1
    for text in texts:
        clock.advance(seconds=15)
        response = _save(client, draft["draft_id"], revision_no, text)
        assert response.status_code == 200, response.text
        revision_no = response.json()["data"]["draft"]["revision_no"]

    rows = _rows(session, draft["draft_id"])
    assert sorted(rows) == [1, revision_no]
    assert rows[revision_no] == ("edited", texts[-1])


def _distinct(count: int, *, offset: int = 0) -> str:
    """互不相同的汉字：删改的位置唯一，算出来的删改字数就是切掉的那一段。"""
    return "".join(chr(0x4E00 + offset + index) for index in range(count))


_LONG = _distinct(1000)
_SHORT = _distinct(60)


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        pytest.param(f"<p>{_distinct(30)}</p>", "<p>x</p>", False, id="不到下限的整篇删除照旧并"),
        pytest.param(f"<p>{_SHORT}</p>", f"<p>{_SHORT[:21]}</p>", False, id="短稿删掉39字"),
        pytest.param(f"<p>{_SHORT}</p>", f"<p>{_SHORT[:20]}</p>", True, id="短稿删掉40字"),
        pytest.param(f"<p>{_LONG}</p>", f"<p>{_LONG[:400]}{_LONG[599:]}</p>", False, id="长稿中间删199字"),
        pytest.param(f"<p>{_LONG}</p>", f"<p>{_LONG[:400]}{_LONG[600:]}</p>", True, id="长稿中间删200字"),
        pytest.param(
            f"<p>{_LONG}</p>", f"<p>{_LONG[:300]}</p><p>{_distinct(250, offset=2000)}</p><p>{_LONG[550:]}</p>", True,
            id="同样字数换掉250字",
        ),
        pytest.param(f"<p>{_LONG}</p>", f"<p>{_LONG[:500]}{_distinct(300, offset=2000)}{_LONG[500:]}</p>", False, id="中间插入"),
        pytest.param(f"<p>{_LONG}</p>", f"<p><strong>{_LONG[:500]}</strong></p>\n<p>{_LONG[500:]}</p>", False, id="只改格式与分段"),
    ],
)
def test_destructive_edit_threshold(previous: str, current: str, expected: bool) -> None:
    """「整段删改」按删掉 / 换掉的可见字数算（编辑器口径，去空白）：至少 min(200, max(40, 上一份的一半))。"""
    assert revisions_module.cuts_snapshot(previous, current) is expected

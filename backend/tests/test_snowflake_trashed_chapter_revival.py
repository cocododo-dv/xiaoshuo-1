"""删了旧章再回来重新分章：随旧章进回收站的场景卡跟着这一版回来（2026-09-20 真实故障）。

作者嫌目录里的章乱，先在章节编排里把旧章删了——删章会把章内的场景卡一起送进回收站，盖上和章相同的
``trashed_at``——再回构思里「整理为章节结构」并确认写入。阶段 Y 之后重新分章铸的是新章号，过去只有
「目标章恰好就是被取回的那一章」时随它一起删的卡才回来：于是场景卡被搬进了新章，自己却还躺在回收站里，
目录里 4 章只看得见 1 场，另外 16 场「不见了」。

约定（``services/catalog_trash_cascade.py``）：

1. 删章是整理结构，不是对场景的裁定——这一版章表里有这一场，确认写入就把它的卡取回，不管它落进哪一章；
2. 作者**单独**删掉的场景卡是对那一场的裁定，确认写入不替作者取回；
3. 分章面板在确认之前把这两件事说清楚；
4. 卡已经被上一次（有缺陷的）确认写入搬进了活跃的章、自己还在回收站里，章内序号又被别的卡占了——
   再确认一次照样取回，不撞 ``(chapter_id, scene_seq)`` 唯一索引。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import ChapterGoal, OperationLog, SceneCard
from tests.test_catalog_book_spine import _base, _catalog, _materialized, _preview
from tests.test_snowflake_chaptering_story_order import _confirm


def _snowflake_chapters(client, project_id: str) -> list[dict]:
    return [chapter for chapter in _catalog(client, project_id) if chapter["origin"] == "snowflake"]


def _trash_chapter(client, project_id: str, chapter_id: str) -> None:
    response = client.delete(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        headers={"X-Idempotency-Key": f"trash-{chapter_id}"},
    )
    assert response.status_code == 200, response.text


def _trash_scene(client, project_id: str, scene_id: str) -> None:
    response = client.delete(
        f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}",
        headers={"X-Idempotency-Key": f"trash-{scene_id}"},
    )
    assert response.status_code == 200, response.text


def _cards(session, project_id: str) -> list[SceneCard]:
    session.expire_all()
    return list(session.execute(select(SceneCard).where(SceneCard.project_id == project_id)).scalars())


def _planned_scene_ids(preview: dict) -> list[str]:
    return [scene["scene_id"] for chapter in preview["chapters"] for scene in chapter["scenes"]]


def test_cards_trashed_with_their_old_chapters_come_back_with_the_new_chaptering(client, session) -> None:
    project_id = _materialized(client, "revive-cascade", scenes_per_chapter=3)
    before = _snowflake_chapters(client, project_id)
    kept_chapter, *deleted = before
    for chapter in deleted:
        _trash_chapter(client, project_id, chapter["chapter_id"])
    trashed_ids = {card.scene_id for card in _cards(session, project_id) if int(card.trashed_flag or 0) == 1}
    assert trashed_ids == {scene["scene_id"] for chapter in deleted for scene in chapter["scenes"]} and trashed_ids

    # 换成每章 2 场：有的章沿用旧号（钉着的章在回收站里 → 取回），有的是新铸的号——卡要回来，不管落进哪一种章
    preview = _preview(client, project_id, scenes_per_chapter=2)
    returning = next(w for w in preview["warnings"] if w["kind"] == "catalog_trashed_scenes_return")
    assert set(returning["scene_ids"]) == trashed_ids
    assert not any(w["kind"] == "catalog_trashed_scenes_kept" for w in preview["warnings"])

    result = _confirm(client, project_id, preview, "revive-cascade-2")

    assert set(result["restored_scene_ids"]) == trashed_ids
    cards = _cards(session, project_id)
    assert [card.scene_id for card in cards if int(card.trashed_flag or 0) == 1] == []
    assert all(card.trashed_at is None and card.trashed_by is None for card in cards)
    # 目录里看得见的就是这一版章表：每一章的场数、全书的场序都和面板上确认的一样
    after = _snowflake_chapters(client, project_id)
    assert [len(chapter["scenes"]) for chapter in after] == [len(chapter["scenes"]) for chapter in preview["chapters"]]
    assert [scene["scene_id"] for chapter in after for scene in chapter["scenes"]] == _planned_scene_ids(preview)
    assert kept_chapter["chapter_id"] in {chapter["chapter_id"] for chapter in after}
    # 这正是过去回不来的那一种：随旧章删掉的卡，落进了一个新铸号的章（不是「被取回的那一章」）
    old_ids = {chapter["chapter_id"] for chapter in before}
    assert any(
        chapter["chapter_id"] not in old_ids and trashed_ids & {scene["scene_id"] for scene in chapter["scenes"]}
        for chapter in after
    )
    # 章内序号连续、章末标记在最后一场上
    for chapter in after:
        rows = sorted(
            (card for card in cards if card.chapter_id == chapter["chapter_id"]), key=lambda card: int(card.scene_seq)
        )
        assert [int(card.scene_seq) for card in rows] == list(range(1, len(rows) + 1))
        assert [int(card.is_chapter_last or 0) for card in rows] == [0] * (len(rows) - 1) + [1]
    log = session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_scene_cards_revived")
    ).scalars().one()
    assert set(log.payload_json["scene_ids"]) == trashed_ids
    # 确认之后面板不再提这件事
    again = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    assert not any(w["kind"].startswith("catalog_trashed_scenes") for w in again["warnings"])


def test_a_card_the_author_trashed_on_its_own_stays_in_the_trash(client, session) -> None:
    project_id = _materialized(client, "revive-individual", scenes_per_chapter=3)
    first, second, *_rest = _snowflake_chapters(client, project_id)
    verdict = first["scenes"][1]["scene_id"]
    _trash_scene(client, project_id, verdict)  # 对这一场的裁定
    _trash_chapter(client, project_id, second["chapter_id"])  # 整理结构
    cascade = {scene["scene_id"] for scene in second["scenes"]}

    preview = _preview(client, project_id, scenes_per_chapter=2)
    by_kind = {w["kind"]: w for w in preview["warnings"]}
    assert set(by_kind["catalog_trashed_scenes_return"]["scene_ids"]) == cascade
    assert by_kind["catalog_trashed_scenes_kept"]["scene_ids"] == [verdict]
    assert by_kind["catalog_trashed_scenes_kept"]["severity"] == "advisory"

    result = _confirm(client, project_id, preview, "revive-individual-2")

    assert set(result["restored_scene_ids"]) == cascade
    still_trashed = [card.scene_id for card in _cards(session, project_id) if int(card.trashed_flag or 0) == 1]
    assert still_trashed == [verdict]
    visible = [scene["scene_id"] for chapter in _snowflake_chapters(client, project_id) for scene in chapter["scenes"]]
    assert verdict not in visible
    assert set(visible) == set(_planned_scene_ids(preview)) - {verdict}


def test_cards_already_moved_into_live_chapters_but_left_in_the_trash_are_repaired_by_confirming_again(
    client, session
) -> None:
    """实库的现状：上一次确认写入（修复之前）把卡搬进了新章、却把它们留在回收站里；旧章还在回收站。"""
    project_id = _materialized(client, "revive-moved", scenes_per_chapter=3)
    first, second, *_rest = _snowflake_chapters(client, project_id)
    stamp = "2026-09-20T06:39:05.908809+00:00"
    stranded = [scene["scene_id"] for scene in second["scenes"]]
    # 旧章在回收站里（带着删章那一刻的时间戳）；随它一起删的三张卡已经被搬进了活跃的第 1 章
    session.add(
        ChapterGoal(
            chapter_id=f"{project_id}_CH_OLD", project_id=project_id, planned_scene_count=0, chapter_goal="旧章",
            trashed_flag=1, trashed_at=stamp, trashed_by="operator",
            writer_brief_json={"source": "snowflake_method"},
        )
    )
    session.flush()
    for offset, scene_id in enumerate(stranded, start=1):
        card = session.get(SceneCard, scene_id)
        card.chapter_id = first["chapter_id"]
        card.scene_seq = offset  # 和第 1 章里活跃的卡同号：只在回收站里才不撞唯一索引
        card.trashed_flag, card.trashed_at, card.trashed_by = 1, stamp, "operator"
    session.commit()
    visible = [scene["scene_id"] for chapter in _snowflake_chapters(client, project_id) for scene in chapter["scenes"]]
    assert not set(stranded) & set(visible)

    preview = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    result = _confirm(client, project_id, preview, "revive-moved-2")

    assert set(result["restored_scene_ids"]) == set(stranded)
    after = _snowflake_chapters(client, project_id)
    assert [scene["scene_id"] for chapter in after for scene in chapter["scenes"]] == _planned_scene_ids(preview)
    assert [scene["scene_id"] for scene in after[1]["scenes"]] == stranded
    assert [card.scene_id for card in _cards(session, project_id) if int(card.trashed_flag or 0) == 1] == []

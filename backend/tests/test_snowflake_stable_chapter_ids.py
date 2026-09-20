"""阶段 Y（2026-09-20）：章在目录里的 id 钉在章计划上，不再是「第几章」。

过去物化目标章号 = ``{project}_CH{章序:02d}``。章序一变（拆章、并章、换一个每章场数），每一章的号都跟着
平移：``CH02`` 变成另一组场，章名 / 书签 / 章级状态 / 终审挂在「位置」上而不是挂在那一章上，场景卡成批跨章
搬动。现在：

- ``SnowflakeChapterPlan.catalog_chapter_id`` 铸一次、钉住（迁移 0089）；号是**序列号**，不表示顺序；
- 重新按场景提议时，场至少一半相同的章还是那一章（对半时归故事序上靠前的那一个）；整拆 / 整并而谁都不过半时，
  身份归开头对得上的那一个；
- 目录里的章序由 ``_CatalogPlacement.settle_chapter_order`` 按这一版章表统一落位；
- 手加的场跟着它的锚点场走，哪怕锚点换了章。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    chapter_target_id,
    match_chunks_to_chapters,
)
from tests.test_catalog_book_spine import _base, _catalog, _materialized, _preview
from tests.test_snowflake_chaptering import _create_project, _pass_triage, _seed
from tests.test_snowflake_chaptering_story_order import _confirm, _payload


# ------------------------------------------------------------------ 1. 还是不是同一章


def _old(*groups: tuple[int, ...]) -> tuple[list[SnowflakeChapterPlan], list[SnowflakeScenePlan]]:
    chapters = [SnowflakeChapterPlan(chapter_plan_id=f"cp{index}", chapter_seq=index) for index, _ in enumerate(groups, start=1)]
    scenes = [
        SnowflakeScenePlan(scene_plan_id=f"s{number}", chapter_plan_id=f"cp{index}")
        for index, group in enumerate(groups, start=1) for number in group
    ]
    return chapters, scenes


def _chunks(scenes: list[SnowflakeScenePlan], *groups: tuple[int, ...]) -> list[dict]:
    by_id = {scene.scene_plan_id: scene for scene in scenes}
    return [{"scenes": [by_id.get(f"s{n}") or SnowflakeScenePlan(scene_plan_id=f"s{n}") for n in group]} for group in groups]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # 一场没动：同一章，且「完全相同」
        ([(1, 2), (3, 4)], [(1, 2), (3, 4)], {0: ("cp1", True), 1: ("cp2", True)}),
        # 后面并进来一场：还是 [1,2] 那一章
        ([(1, 2), (3,)], [(1, 2, 3)], {0: ("cp1", False)}),
        # 一章从正中拆开：前半截留着原章（和面板的「从这里另起一章」同一个口径），后半截是新章
        ([(4, 5, 6, 7)], [(4, 5), (6, 7)], {0: ("cp1", False)}),
        # 两章等长地并成一章：靠前的那一章留着（「并入上一章」）
        ([(4, 5), (6, 7)], [(4, 5, 6, 7)], {0: ("cp1", False)}),
        # 09 末尾加了一场：末章还是末章
        ([(1, 2), (3, 4)], [(1, 2), (3, 4, 5)], {0: ("cp1", True), 1: ("cp2", False)}),
        # 整拆：一章拆成几小段、谁都不过半 → 开头对得上的那一段留着原章（「从这里另起一章」连做两次的结果）
        ([(1, 2, 3, 4, 5)], [(1, 2), (3,), (4, 5)], {0: ("cp1", False)}),
        ([(1, 2, 3, 4, 5, 6, 7)], [(1,), (2, 3), (4, 5), (6, 7)], {0: ("cp1", False)}),
        # 整并：几章整个并成一段、谁都不过半 → 开头对得上的那一章留着（「并入上一章」连做两次的结果）
        ([(1,), (2,), (3,)], [(1, 2, 3)], {0: ("cp1", False)}),
        # 开头对不上、内容也不过半：不是同一章——第二章被切碎，哪一段都不是它
        ([(1, 2, 3, 4), (5, 6, 7, 8)], [(1,), (2, 3, 4, 5, 6, 7), (8,)], {1: ("cp1", False)}),
        # 内容过半的认领优先于开头对得上：[1] 的开头对得上，但 [1,2,3] 里过半的是 [2,3] 那一章
        ([(1,), (2, 3)], [(1, 2, 3)], {0: ("cp2", False)}),
        # 一章只被认领一次；并列时取公共场最多的，再并列取靠前的
        ([(1, 2), (3, 4, 5, 6)], [(1, 2, 3, 4), (5, 6)], {0: ("cp1", False), 1: ("cp2", False)}),
    ],
)
def test_a_reproposed_chunk_is_the_old_chapter_when_at_least_half_of_both_is_shared(old, new, expected) -> None:
    chapters, scenes = _old(*old)
    matched = match_chunks_to_chapters(chapters, scenes, _chunks(scenes, *new))
    assert {index: (chapter.chapter_plan_id, identical) for index, (chapter, identical) in matched.items()} == expected


def test_a_chapter_recut_into_small_pieces_keeps_its_row_on_the_opening_piece(session) -> None:
    """17 场、四章（5 / 7 / 3 / 2）换成每章约 2 场：5 场和 7 场的两章被切成谁都不过半的小段——原来的章计划
    （连同作者起的章名、它钉着的目录章）留在开头那一段上，不是整行作废另起一批新章。"""
    from tests.test_snowflake_chaptering_story_order import PROJECT_ID, _seed as seed_story, _uids

    seed_story(session)
    chaptering = SnowflakeChapteringService(session)
    chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 12})
    before = chaptering.chapter_plans(PROJECT_ID)
    assert [len([plan for plan in chaptering.scene_plans(PROJECT_ID) if plan.chapter_plan_id == c.chapter_plan_id]) for c in before] == [5, 7, 3, 2]
    before[0].title, before[1].title = "雨夜来信", "旧案卷宗"
    session.flush()
    pinned = {chapter.row_uid: chapter.catalog_chapter_id for chapter in before}
    assert all(pinned.values())

    preview = chaptering.preview(PROJECT_ID, {"strategy": "from_scenes", "scenes_per_chapter": 2})
    pieces = _uids(preview)
    assert all(len(piece) <= 3 for piece in pieces) and len(pieces) > 4
    by_opening = {piece[0]: chapter for piece, chapter in zip(pieces, preview["chapters"])}
    # 四章都还在：各自留在以它的第一场开头的那一段上
    for old_chapter, opening in zip(before, ("u01", "u06", "u13", "u16")):
        kept = by_opening[opening]
        assert kept["row_uid"] == old_chapter.row_uid and kept["chapter_id"] == pinned[old_chapter.row_uid]
    assert by_opening["u01"]["title"] == "雨夜来信" and by_opening["u06"]["title"] == "旧案卷宗"
    assert preview["replaces_chapter_count"] == 0, "没有一章被替换掉"
    fresh = [chapter for chapter in preview["chapters"] if chapter["row_uid"].startswith("new:")]
    assert len(fresh) == len(pieces) - 4 and all(chapter["chapter_id"] == "" for chapter in fresh)


# ------------------------------------------------------------------ 2. 号是序列号，铸一次、钉住


def test_ids_are_minted_once_in_table_order_and_never_reused(client, session) -> None:
    project_id = _create_project(client, "stable-serial")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    # 手建一章：它的 id 不是序列号的形状，不占号
    hand_made = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters", json={"title": "楔子"},
        headers={"X-Idempotency-Key": "stable-serial-hand"},
    ).json()["data"]["chapter"]["chapter_id"]

    # 没落库的提议：还没有目录章号
    preview = _preview(client, project_id, scenes_per_chapter=3)
    assert [chapter["chapter_id"] for chapter in preview["chapters"]] == ["", "", "", ""]
    # 只保存分章（不物化）就已经铸号：按章表的顺序 CH01…，此后这一章无论排第几都是这个号
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=_payload(preview)).status_code == 200
    chaptering = SnowflakeChapteringService(session)
    pinned = [chapter.catalog_chapter_id for chapter in chaptering.chapter_plans(project_id)]
    assert pinned == [chapter_target_id(project_id, serial) for serial in (1, 2, 3, 4)]
    assert hand_made not in pinned
    saved = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    assert [chapter["chapter_id"] for chapter in saved["chapters"]] == pinned
    # 场景行上的章戳 = 这一章钉着的号
    stamps = {plan.chapter_plan_id: plan.chapter_id for plan in chaptering.scene_plans(project_id)}
    assert stamps == {chapter.chapter_plan_id: chapter.catalog_chapter_id for chapter in chaptering.chapter_plans(project_id)}

    _confirm(client, project_id, saved, "stable-serial-1")
    assert [chapter["chapter_id"] for chapter in _catalog(client, project_id) if chapter["origin"] == "snowflake"] == pinned

    # 换成每章 2 场：[1] 与 [6,7] 是新章 → 下一个没用过的号（CH05、CH06），不去挤别的章的号
    six = _preview(client, project_id, scenes_per_chapter=2)
    _confirm(client, project_id, six, "stable-serial-2")
    session.expire_all()
    by_scenes = {
        tuple(scene["story_index"] for scene in chapter["scenes"]): chapter["chapter_id"]
        for chapter in client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]["chapters"]
    }
    assert by_scenes == {
        (1,): chapter_target_id(project_id, 5), (2, 3): pinned[0], (4, 5): pinned[1],
        (6, 7): chapter_target_id(project_id, 6), (8, 9): pinned[2], (10, 11, 12): pinned[3],
    }

    # 再收回 4 章：CH05 / CH06 空了进回收站。以后再多出来的章拿 CH07——回收站里的号随时可能被作者取回，不复用
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "stable-serial-3")
    seven = _preview(client, project_id, scenes_per_chapter=2)
    _confirm(client, project_id, seven, "stable-serial-4")
    session.expire_all()
    ids = {chapter.catalog_chapter_id for chapter in SnowflakeChapteringService(session).chapter_plans(project_id)}
    assert ids == {*pinned, chapter_target_id(project_id, 7), chapter_target_id(project_id, 8)}
    trashed = set(session.execute(
        select(ChapterGoal.chapter_id).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 1)
    ).scalars())
    assert {chapter_target_id(project_id, 5), chapter_target_id(project_id, 6)} <= trashed


def test_a_scene_stamp_that_is_not_a_chapter_of_this_work_is_not_pinned(client, session) -> None:
    """场景行上的章戳反推章表时：只有目录里真有的章、或本作品序列号形状的章戳才钉；模型随手写的字符串不钉。"""
    project_id = _create_project(client, "stable-derive")
    _seed(client, project_id)
    chaptering = SnowflakeChapteringService(session)
    for chapter in chaptering.chapter_plans(project_id):
        session.delete(chapter)
    scenes = chaptering.scene_plans(project_id)
    for index, plan in enumerate(scenes):
        plan.chapter_plan_id = None
        plan.chapter_id = "ch-one" if index < 6 else f"{project_id}_CH02"
    session.commit()

    derived = chaptering.ensure_chapter_plans(project_id)
    assert [chapter.catalog_chapter_id for chapter in derived] == [None, f"{project_id}_CH02"]
    # 没钉的那一章保存时照常铸号——CH02 已经被占，它拿 CH03
    assert chaptering.catalog_chapter_id(derived[0]) == f"{project_id}_CH03"


# ------------------------------------------------------------------ 3. 目录章序 = 这一版章表的顺序


def _order(client, project_id: str) -> list[str]:
    return [chapter["chapter_id"] for chapter in _catalog(client, project_id)]


def _hand_made_after(client, project_id: str, title: str, after_chapter_id: str) -> str:
    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters", json={"title": title},
        headers={"X-Idempotency-Key": f"hand-{len(title)}-{after_chapter_id[-4:]}"},
    )
    assert created.status_code == 200, created.text
    chapter_id = created.json()["data"]["chapter"]["chapter_id"]
    # 手建的章必须真的动过笔，否则它是空白占位章，物化时会被移走
    scene_id = created.json()["data"]["chapter"]["scenes"][0]["scene_id"]
    assert client.patch(
        f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}", json={"brief": {"conflict": "门锁着"}},
    ).status_code == 200
    order = [item for item in _order(client, project_id) if item != chapter_id]
    order.insert(order.index(after_chapter_id) + 1, chapter_id)
    moved = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapter-order", json={"chapter_ids": order},
        headers={"X-Idempotency-Key": f"order-{chapter_id[-6:]}"},
    )
    assert moved.status_code == 200, moved.text
    return chapter_id


def test_new_chapters_land_where_the_table_puts_them_and_hand_made_chapters_keep_their_neighbour(client, session) -> None:
    project_id = _materialized(client, "stable-order", scenes_per_chapter=3)
    first, second, third, fourth = _order(client, project_id)
    interlude = _hand_made_after(client, project_id, "番外 · 旧照片", first)
    assert _order(client, project_id) == [first, interlude, second, third, fourth]

    # 每章 2 场：[1] 新章、[2,3] = first、[4,5] = second、[6,7] 新章、[8,9] = third、[10–12] = fourth
    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "stable-order-2")
    assert result["chapter_order_held"] is False
    order = _order(client, project_id)
    new_opening, new_middle = order[0], order[4]
    assert order == [new_opening, first, interlude, second, new_middle, third, fourth], "番外还跟在原来那一章后面"
    assert {new_opening, new_middle}.isdisjoint({first, second, third, fourth, interlude})
    assert [chapter["no"] for chapter in _catalog(client, project_id)] == [f"{n:02d}" for n in range(1, 8)]
    rows = session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
    ).scalars().all()
    assert sorted(row.display_order for row in rows) == list(range(1, 8))


def test_an_approved_chapter_is_never_moved_so_new_chapters_wait_at_the_end(client, session) -> None:
    """和章节编排的拖动排序同一条规矩：终审过的章，相对顺序与目录位置都锁着。"""
    project_id = _materialized(client, "stable-order-approved", scenes_per_chapter=3)
    first, second, third, fourth = _order(client, project_id)
    session.get(ChapterGoal, fourth).state = "approved"
    session.commit()

    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "stable-order-approved-2")
    assert result["chapter_order_held"] is True
    order = _order(client, project_id)
    assert order[:4] == [first, second, third, fourth], "一章都没挪"
    assert len(order) == 6 and set(order[4:]).isdisjoint({first, second, third, fourth})

    # 重新打开之后再整理一次：按章表排好
    session.expire_all()
    session.get(ChapterGoal, fourth).state = "planned"
    session.commit()
    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "stable-order-approved-3")
    assert result["chapter_order_held"] is False
    settled = _order(client, project_id)
    assert [settled[i] for i in (1, 2, 4, 5)] == [first, second, third, fourth]


# ------------------------------------------------------------------ 4. 手加的场跟着锚点场走


def test_a_hand_made_scene_follows_its_anchor_scene_even_in_front_of_it(client, session) -> None:
    project_id = _materialized(client, "stable-followers", scenes_per_chapter=3)
    chapters = _catalog(client, project_id)
    second = chapters[1]
    assert len(second["scenes"]) == 4
    sixth_scene = second["scenes"][2]["scene_id"]

    def add(title: str, at: int) -> str:
        created = client.post(
            f"/api/v2/projects/{project_id}/catalog/chapters/{second['chapter_id']}/scenes",
            json={"title": title, "at": at}, headers={"X-Idempotency-Key": f"followers-{at}"},
        )
        assert created.status_code == 200, created.text
        return created.json()["data"]["scene"]["scene_id"]

    leading = add("排在本章最前面的手加场", 0)   # 前面没有计划内的卡 → 跟着第 4 场、排在它前面
    trailing = add("紧跟在第 6 场后面的手加场", 4)  # [手加, 4, 5, 6, 这里, 7]

    # 每章 2 场：第 4、5 场留在原章，第 6、7 场成了一章新章——紧跟第 6 场的手加场跟过去，最前面的那张留下
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "stable-followers-2")
    session.expire_all()
    cards = {card.scene_id: card for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
    ).scalars()}
    assert cards[leading].chapter_id == second["chapter_id"] and cards[leading].scene_seq == 1
    new_chapter = cards[sixth_scene].chapter_id
    assert new_chapter != second["chapter_id"]
    assert cards[trailing].chapter_id == new_chapter
    in_new_chapter = sorted((card.scene_seq, card.scene_id) for card in cards.values() if card.chapter_id == new_chapter)
    assert [scene_id for _seq, scene_id in in_new_chapter][:2] == [sixth_scene, trailing]
    for chapter_id in {card.chapter_id for card in cards.values()}:
        members = sorted(card.scene_seq for card in cards.values() if card.chapter_id == chapter_id)
        assert members == list(range(1, len(members) + 1)), chapter_id
        assert sum(card.is_chapter_last for card in cards.values() if card.chapter_id == chapter_id) == 1

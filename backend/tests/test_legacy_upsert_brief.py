"""v1 建章 / 建场接口的 ``writer_brief_json``（2026-10-16 v2 写作简报退役之后）。

以前两个接口都把简报归一化成 v2 的 11 / 13 个键（其余键丢掉），起草 bundle 再把有内容的 v2 简报整份塞进提示。
那两份简报没有界面能填，实库从没人写过；退役后简报对象原样存（雪花物化的来源与三拍、目录的标题都在这一列），
不是对象就在请求校验处 422，提示里也不再有「Chapter / Scene Writer Brief」两段。
"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.context_budget import SECTION_SPECS


def test_the_legacy_routes_store_the_brief_object_as_given(client, session) -> None:
    chapter = {"chapter_id": "LUB", "planned_scene_count": 1, "chapter_goal": "林昭找到写信的人。"}
    chapter["writer_brief_json"] = {"title": "旧信", "chapter_promise": "不再进提示"}
    assert client.post("/api/v1/chapters", json=chapter).status_code == 200

    scene = {"scene_id": "LUB_SC01", "chapter_id": "LUB", "scene_goal": "林昭拆开旧信。"}
    scene["writer_brief_json"] = {"scene_form": "proactive", "goal": "拿到旧信", "character_desire": "不再进提示"}
    assert client.post("/api/v1/scenes", json=scene).status_code == 200

    session.expire_all()
    assert session.get(ChapterGoal, "LUB").writer_brief_json == {"title": "旧信", "chapter_promise": "不再进提示"}
    stored = session.get(SceneCard, "LUB_SC01").writer_brief_json
    assert stored == {"scene_form": "proactive", "goal": "拿到旧信", "character_desire": "不再进提示"}

    snapshot = BundleBuilder(session).build("LUB_SC01")["snapshot"]
    assert "不再进提示" not in str(snapshot["inline_digests"])
    assert not any("writer_brief" in key for key in snapshot["inline_digests"])
    assert not any("writer_brief" in name for name, _label, _keys in SECTION_SPECS)


def test_a_brief_that_is_not_an_object_is_rejected_before_anything_is_written(client, session) -> None:
    chapter = {"chapter_id": "LUB_BAD", "planned_scene_count": 1, "chapter_goal": "目标", "writer_brief_json": ["旧信"]}
    response = client.post("/api/v1/chapters", json=chapter)
    assert response.status_code == 422, response.text
    assert session.get(ChapterGoal, "LUB_BAD") is None

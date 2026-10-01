"""直接经 ORM 造章与场景（X04-22），不经测试专用的 v1 建章 / 建场景接口。

v1 的 ``POST /api/v1/chapters`` / ``POST /api/v1/scenes`` 是 CLAUDE.md 里「留给测试的原语」：React 从不调用，测试为了
造数据走它们，路由就成了测试基础设施，批次二没法只按产品理由决定它们的去留。这里落的行与那两条路由**新建**时落的
一样（``chapter_upsert`` / ``scene_upsert`` 的新建分支）：

- 作品：``StoryProject``（大纲驱动；``mark`` 取标题首字，其余是 ORM 默认值，与 ``ProjectService.create`` 一样）；
- 章：``ChapterGoal``（写作简报按同一规则规范化）+ 运行时 ``ChapterState``（``ensure_chapter_state``）；
- 场景：``SceneCard``（写作简报同样规范化）+ ``SceneRunState(scene_status="ready")``。

都在自己的会话里提交，和一次请求一样。专测 v1 路由本身的用例（输入边界、幂等、终审锁、场序冲突）照旧走路由。
"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import ChapterGoal, SceneCard, SceneRunState, StoryProject
from novel_system.db.session import SessionLocal
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.writer_briefs import normalize_chapter_writer_brief, normalize_scene_writer_brief


def seed_project(project_id: str, *, title: str, outline_text: str, **fields: Any) -> None:
    """新建一部大纲驱动的作品（v1 ``POST /api/v1/projects`` 落的那一行）。"""
    fields.setdefault("mark", title[:1] or None)
    with SessionLocal() as session:
        session.add(StoryProject(project_id=project_id, title=title, outline_text=outline_text, **fields))
        session.commit()


def seed_chapter(chapter_id: str, **fields: Any) -> None:
    """新建一章：``ChapterGoal`` + ``ChapterState``。``fields`` 是 ``ChapterGoal`` 的列。"""
    payload = {"chapter_id": chapter_id, **fields}
    payload["writer_brief_json"] = normalize_chapter_writer_brief(payload.get("writer_brief_json"))
    with SessionLocal() as session:
        session.add(ChapterGoal(**payload))
        session.flush()
        ensure_chapter_state(session, chapter_id)
        session.commit()


def seed_scene(scene_id: str, *, chapter_id: str, scene_seq: int, **fields: Any) -> None:
    """新建一场：``SceneCard`` + ``SceneRunState(ready)``。``fields`` 是 ``SceneCard`` 的列。"""
    payload = {"scene_id": scene_id, "chapter_id": chapter_id, "scene_seq": scene_seq, **fields}
    payload["writer_brief_json"] = normalize_scene_writer_brief(payload.get("writer_brief_json"))
    with SessionLocal() as session:
        session.add(SceneCard(**payload))
        session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
        session.commit()


__all__ = ["seed_chapter", "seed_project", "seed_scene"]

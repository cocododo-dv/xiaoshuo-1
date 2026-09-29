"""FE-ALIGN Phase 2: v2 作品域聚合（dashboard / writing-stats）。

原型主页（design/ws-works.jsx 的 home 形状）所需的聚合读端点，纯读不新增写路径。
雪花步骤状态映射（简报改动 3）：approved→done、当前步→active、
skipped 或完整度未达（stale/pending 草稿）→warn、未开始→todo。

章节的展示态（state/pct）在 Phase 3 目录统一前暂存于
ChapterGoal.writer_brief_json["fe_display"]（demo seed 写入；真实章节走默认推导），
Phase 3 落正式列后由目录服务接管。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    SceneCard,
    SnowflakeStepRun,
    StoryProject,
)
from novel_system.services.catalog import CatalogService
from novel_system.services.catalog_labels import chapter_title, focus_scene_payload
from novel_system.services.snowflake_steps import list_step_definitions
from novel_system.services.writing_stats import WritingStatsService, count_words
from novel_system.services.snowflake_queries import step_gate_satisfied
from novel_system.services.scene_lookup import require_project

_TAG_BREAK_RE = re.compile(r"</(?:p|div|h\d|li|blockquote)>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
#: 主页「近章」每一行给前端的键（``_row`` / ``_index`` 只在服务里用）
_CHAPTER_VIEW_KEYS = ("chapter_id", "no", "title", "state", "pct", "active")


def _content_lines(content: str | None) -> list[str]:
    if not content:
        return []
    text = _TAG_BREAK_RE.sub("\n", content)
    text = _TAG_RE.sub("", text)
    return [line.strip() for line in text.splitlines() if line.strip()]


class ProjectOverviewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._stats = WritingStatsService(session)
        self._catalog = CatalogService(session)

    # ---- writing-stats ----

    def writing_stats(self, project_id: str) -> dict[str, Any]:
        require_project(self.session, project_id)
        return self._stats.stats_payload(project_id)

    # ---- dashboard ----

    def dashboard(self, project_id: str) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        chapter_views = self._chapter_views(project)
        current = self._current_chapter_view(project, chapter_views)
        resume, brief = self._resume_and_brief(project, current)
        return {
            "resume": resume,
            "brief": brief,
            "snowflake": self._snowflake_board(project_id),
            "chapters_recent": [
                {key: view[key] for key in _CHAPTER_VIEW_KEYS}
                for view in chapter_views[-5:]
            ],
            "stats": self._stats.stats_payload(project_id),
        }

    # ---- internals ----

    def _current_scene_drafts(self, scene_ids: list[str]) -> dict[str, AuthorDraft]:
        if not scene_ids:
            return {}
        rows = self.session.execute(
            select(AuthorDraft).where(
                AuthorDraft.object_type == "scene",
                AuthorDraft.object_id.in_(scene_ids),
                AuthorDraft.status == "current",
            )
        ).scalars().all()
        return {row.object_id: row for row in rows}

    def _chapter_views(self, project: StoryProject) -> list[dict[str, Any]]:
        """主页的章头：与目录 API 同一套章序、章名、状态、字数口径（``chapter_rows`` / ``chapter_title`` /
        Σ 活跃场景的字数）。只读章头要的列——整张目录载荷（每场的设计卡、场景三问……）只给要续写的那一章
        （:meth:`_resume_and_brief`）；过去为了显示五行近章把全书每一章都序列化了一遍（B08-13）。"""
        chapter_rows = self._catalog.chapter_rows(project.project_id)
        words = self._chapter_words([chapter.chapter_id for chapter in chapter_rows])
        views: list[dict[str, Any]] = []
        for index, chapter in enumerate(chapter_rows):
            state = str(chapter.state or "planned")
            target = chapter.words_target
            pct = (
                min(100, round(words.get(chapter.chapter_id, 0) * 100 / target))
                if target
                else (100 if state == "approved" else 0)
            )
            views.append(
                {
                    "chapter_id": chapter.chapter_id,
                    "no": f"{index + 1:02d}",
                    "title": chapter_title(chapter),
                    "state": state,
                    "pct": pct,
                    "active": bool(chapter.chapter_id == project.current_chapter_id or state == "writing"),
                    "_row": chapter,
                    "_index": index,
                }
            )
        return views

    def _chapter_words(self, chapter_ids: list[str]) -> dict[str, int]:
        """每章活跃场景的字数之和（一条 GROUP BY）——与目录载荷的 ``words.cur`` 同一口径。"""
        if not chapter_ids:
            return {}
        return {
            str(chapter_id): int(total or 0)
            for chapter_id, total in self.session.execute(
                select(SceneCard.chapter_id, func.sum(SceneCard.words_current))
                .where(SceneCard.chapter_id.in_(chapter_ids), SceneCard.trashed_flag == 0)
                .group_by(SceneCard.chapter_id)
            ).all()
        }

    def _current_chapter_view(
        self, project: StoryProject, chapter_views: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not chapter_views:
            return None
        for view in chapter_views:
            if view["chapter_id"] == project.current_chapter_id:
                return view
        for view in chapter_views:
            if view["state"] == "writing" or view["active"]:
                return view
        return chapter_views[-1]

    def _resume_and_brief(
        self, project: StoryProject, current: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """与目录 API 同源：场景载荷（slug/title/kind/state/brief）直接取自 CatalogService（只序列化这一章）。"""
        if current is None:
            return None, None
        chapter = current["_row"]
        payload = self._catalog.chapter_payload(
            project,
            chapter,
            current["_index"],
            context=self._catalog.read_context(project.project_id, [chapter.chapter_id]),
        )
        scenes = list(payload.get("scenes") or [])
        if not scenes:
            return None, None
        # 阶段 X：与目录 / 写作台 / AI 起草台同一条「现在该写哪一场」规则（在写 → 第一场没写完的 → 末场）。
        # 过去这里取章里的**最后一场**：雪花刚物化完的 5 场章，主页的「继续写作」指着第 5 场。
        scene = focus_scene_payload(scenes) or scenes[-1]
        draft = self._current_scene_drafts([scene["scene_id"]]).get(scene["scene_id"])
        lines = _content_lines(draft.content if draft else None)
        resume = {
            "chapter_no": current["no"],
            "scene_slug": scene["slug"],
            # 场景 slug 已是稳定的 scene_id（不再含位置）；章内第几场单独给
            "scene_no": scenes.index(scene) + 1,
            "scene_title": scene["title"],
            "last_lines": lines[-2:],
            "scene_words": count_words(draft.content) if draft else int(scene.get("words") or 0),
            "paused_at": draft.updated_at if draft else None,
        }
        return resume, dict(scene["brief"])

    def _latest_by_step(self, project_id: str) -> dict[str, Any]:
        """每一步最新的非 superseded 版本（与 ``snowflake_queries.latest_by_step`` 同一规则）——只取状态三列：
        看板只要状态，过去把每一步的每一个版本连同整份 draft_json 都读了回来（B08-13）。"""
        latest: dict[str, Any] = {}
        for row in self.session.execute(
            select(SnowflakeStepRun.step_key, SnowflakeStepRun.status, SnowflakeStepRun.stale_accepted_at)
            .where(SnowflakeStepRun.project_id == project_id)
            .order_by(SnowflakeStepRun.version.asc(), SnowflakeStepRun.created_at.asc())
        ).all():
            if row.status == "superseded":
                continue
            latest[row.step_key] = row
        return latest

    def _snowflake_board(self, project_id: str) -> list[dict[str, Any]]:
        latest = self._latest_by_step(project_id)
        current_key = next(
            (
                step["step_key"]
                for step in list_step_definitions()
                if not step_gate_satisfied(latest.get(step["step_key"]))
            ),
            None,
        )
        board: list[dict[str, Any]] = []
        for step in list_step_definitions():
            run = latest.get(step["step_key"])
            if run is not None and run.status == "approved":
                status = "done"
            elif step["step_key"] == current_key:
                status = "active"
            elif run is not None:
                status = "warn"  # skipped / stale / 完整度未达的草稿
            else:
                status = "todo"
            board.append({"step_key": step["step_key"], "label": step["label"], "status": status})
        return board

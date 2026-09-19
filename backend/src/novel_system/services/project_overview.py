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

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ReviewItem,
    SnowflakeStepRun,
    StoryProject,
)
from novel_system.services.catalog import CatalogService, focus_scene_payload
from novel_system.services.projects import ProjectService
from novel_system.services.snowflake_steps import list_step_definitions
from novel_system.services.writing_stats import WritingStatsService, count_words

_TAG_BREAK_RE = re.compile(r"</(?:p|div|h\d|li|blockquote)>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _content_lines(content: str | None) -> list[str]:
    if not content:
        return []
    text = _TAG_BREAK_RE.sub("\n", content)
    text = _TAG_RE.sub("", text)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _gate_satisfied(run: SnowflakeStepRun | None) -> bool:
    if run is None:
        return False
    if run.status in {"approved", "skipped"}:
        return True
    return run.status == "stale" and bool(run.stale_accepted_at)


class ProjectOverviewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._projects = ProjectService(session)
        self._stats = WritingStatsService(session)
        self._catalog = CatalogService(session)

    # ---- writing-stats ----

    def writing_stats(self, project_id: str) -> dict[str, Any]:
        self._projects.require_project(project_id)
        return self._stats.stats_payload(project_id)

    # ---- dashboard ----

    def dashboard(self, project_id: str) -> dict[str, Any]:
        project = self._projects.require_project(project_id)
        chapter_views = self._chapter_views(project)
        current = self._current_chapter_view(project, chapter_views)
        resume, brief = self._resume_and_brief(current)
        return {
            "resume": resume,
            "brief": brief,
            "snowflake": self._snowflake_board(project_id),
            "chapters_recent": [
                {key: value for key, value in view.items() if key != "scenes"}
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
        """FE-ALIGN P3：与目录 API 同源（CatalogService 序列化），主页/编排/成稿一致。"""
        views: list[dict[str, Any]] = []
        for index, chapter in enumerate(self._catalog.chapter_rows(project.project_id)):
            payload = self._catalog.chapter_payload(project, chapter, index)
            words = payload["words"]
            pct = (
                min(100, round((words["cur"] or 0) * 100 / words["target"]))
                if words.get("target")
                else (100 if payload["state"] == "approved" else 0)
            )
            views.append(
                {
                    "chapter_id": payload["chapter_id"],
                    "no": payload["no"],
                    "title": payload["title"],
                    "state": payload["state"],
                    "pct": pct,
                    "active": bool(payload["current"] or payload["state"] == "writing"),
                    "scenes": payload["scenes"],
                }
            )
        return views

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
        self, current: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """与目录 API 同源：场景载荷（slug/title/kind/state/brief）直接取自 CatalogService。"""
        if current is None:
            return None, None
        scenes = list(current.get("scenes") or [])
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

    def _latest_by_step(self, project_id: str) -> dict[str, SnowflakeStepRun]:
        rows = self.session.execute(
            select(SnowflakeStepRun)
            .where(SnowflakeStepRun.project_id == project_id)
            .order_by(SnowflakeStepRun.version.asc(), SnowflakeStepRun.created_at.asc())
        ).scalars().all()
        latest: dict[str, SnowflakeStepRun] = {}
        for row in rows:
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
                if not _gate_satisfied(latest.get(step["step_key"]))
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

    def _last_manuscript(
        self, project: StoryProject, *, chapter_views: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        approved_ids = list(project.approved_chapter_ids_json or [])
        if not approved_ids:
            return None
        view_by_id = {view["chapter_id"]: view for view in chapter_views}
        last_id = approved_ids[-1]
        chapter = self.session.get(ChapterGoal, last_id)
        view = view_by_id.get(last_id)
        return {
            "no": view["no"] if view else last_id,
            "title": view["title"] if view else (_fallback_title(chapter) if chapter else last_id),
            "at": chapter.updated_at if chapter else None,
        }


def _fallback_title(chapter: ChapterGoal) -> str:
    text = str(chapter.chapter_goal or "").strip().splitlines()[0] if chapter.chapter_goal else ""
    return text[:24] or chapter.chapter_id

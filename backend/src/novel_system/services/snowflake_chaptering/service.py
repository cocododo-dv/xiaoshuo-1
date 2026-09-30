"""``SnowflakeChapteringService``：分章的门面（路由、雪花工作区与测试都经它）。

实现分在同包的模块里（B07-01，2026-09-30）：``derive``（章表派生与分章现状）、``preview``（预览）、``save``
（落库 / 指名策略 / 按场景提议）、``orphans``（孤儿场）、``llm``（AI 建议分章 / AI 起章名）；纯算法在
``algorithms`` / ``contiguity`` / ``spine`` / ``rhythm``；章表行的读写在包外的叶子 ``snowflake_chapter_table``。
这里每个方法只转一手，名字与签名照旧。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.snowflake_chapter_table import catalog_chapter_id, live_chapter_plans
from novel_system.services.snowflake_chaptering.derive import chapter_plan_status, ensure_chapter_plans
from novel_system.services.snowflake_chaptering.llm import suggest as suggest_chaptering, suggest_titles as suggest_chapter_titles
from novel_system.services.snowflake_chaptering.orphans import ORPHAN_RESOLUTIONS, resolve_orphan as resolve_orphaned_scene
from novel_system.services.snowflake_chaptering.preview import preview as preview_chapters
from novel_system.services.snowflake_chaptering.save import (
    autoassign as autoassign_chapters,
    propose_from_scenes as propose_chapters_from_scenes,
    save as save_chapters,
)
from novel_system.services.snowflake_chaptering.scale import chapter_scale as scale_of_chapters
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order


class SnowflakeChapteringService:
    #: AI 起章名：一次 LLM 调用起几章的名字 / 一次请求最多几批。整本书的章名 + 章摘要一次要不完
    #: （思考型中转的推理 token 也算在输出里），分批还让后面的批看得见前面已经起好的名字，口径一致。
    TITLE_BATCH_SIZE = 12
    TITLE_MAX_BATCHES = 6
    #: 孤儿场的两个合法去向（见 ``orphans``）。
    ORPHAN_RESOLUTIONS = ORPHAN_RESOLUTIONS

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 读

    def chapter_plans(self, project_id: str) -> list[SnowflakeChapterPlan]:
        return live_chapter_plans(self.session, project_id)

    def scene_plans(self, project_id: str) -> list[SnowflakeScenePlan]:
        """活跃场景计划，按**故事序**（09 场景列表的行序，见 ``snowflake_scene_order``）。"""
        return live_scene_plans_in_story_order(self.session, project_id)

    def catalog_chapter_id(self, chapter: SnowflakeChapterPlan, *, mint: bool = True) -> str:
        """这一章在目录里的 id（阶段 Y：钉过就用钉的，没钉过且 ``mint`` 时铸下一个序列号）。"""
        return catalog_chapter_id(self.session, chapter, mint=mint)

    def ensure_chapter_plans(self, project_id: str) -> list[SnowflakeChapterPlan]:
        return ensure_chapter_plans(self.session, project_id)

    def status(self, project_id: str, scene_plans: list[SnowflakeScenePlan]) -> dict[str, Any]:
        return chapter_plan_status(self.session, project_id, scene_plans)

    def chapter_scale(self, project_id: str, body: dict[str, Any] | None, scenes: list[SnowflakeScenePlan]) -> dict[str, Any]:
        return scale_of_chapters(self.session, project_id, body, scenes)

    # ------------------------------------------------------------------ 预览与落库

    def preview(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return preview_chapters(self.session, project_id, payload)

    def save(self, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator") -> dict[str, Any]:
        return save_chapters(self.session, project_id, payload, actor_ref=actor_ref)

    def autoassign(self, project_id: str, strategy: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        return autoassign_chapters(self.session, project_id, strategy, actor_ref=actor_ref)

    def propose_from_scenes(
        self, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator"
    ) -> dict[str, Any]:
        return propose_chapters_from_scenes(self.session, project_id, payload, actor_ref=actor_ref)

    def resolve_orphan(self, project_id: str, scene_plan_id: str, *, action: str, actor_ref: str = "operator") -> dict[str, Any]:
        return resolve_orphaned_scene(self.session, project_id, scene_plan_id, action=action, actor_ref=actor_ref)

    # ------------------------------------------------------------------ AI（只读、fail-closed）

    def suggest(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return suggest_chaptering(self.session, project_id, payload)

    def suggest_titles(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # 批次大小经 self 读：测试按类属性调小它们
        return suggest_chapter_titles(
            self.session,
            project_id,
            payload,
            batch_size=self.TITLE_BATCH_SIZE,
            max_batches=self.TITLE_MAX_BATCHES,
        )

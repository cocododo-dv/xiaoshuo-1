"""章故事架构（章节蓝图，``GenerationPlanningArtifact`` 里 ``artifact_type = chapter_story_architecture`` 的行）的一处定义（B07-11）。

两个写入方共用这里的常量、字段表、读法、落库与归一：章节编排（``chapter_plan_llm``：作者亲手写 / 显式重生成）
与场景运行的规划段（``near_final.ensure_scene_planning``：没有可用蓝图时由模型现做）。两边的提示词仍是两条
（规划台与场景运行各自的上下文），写出来的是同一种行。

作者亲手写的蓝图 = ``llm_call_id`` 为空的那一行：设计 / 绑定变化不作废它（B07-03，见 ``scene_planning_staleness``）。
叶子模块：只依赖 ORM 与别的查询叶子。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import GenerationPlanningArtifact
from novel_system.services.planning_queries import latest_active_planning_artifact

CHAPTER_ARCHITECTURE_ARTIFACT = "chapter_story_architecture"
ARCHITECTURE_FIELDS = (
    "chapter_promise",
    "escalation_path",
    "reveal_plan",
    "payoff_target",
    "character_shift",
    "ending_question",
)
ARCHITECTURE_LIST_FIELDS = ("escalation_path", "reveal_plan")
_SCALAR_FIELDS = tuple(field for field in ARCHITECTURE_FIELDS if field not in ARCHITECTURE_LIST_FIELDS)
# 作者 / 规划台写的蓝图：每个标量字段与每一条的字数上限、每个列表的条数上限
_MAX_FIELD_CHARS = 400
_MAX_LIST_ITEMS = 8


def latest_chapter_architecture(session: Session, chapter_id: str) -> GenerationPlanningArtifact | None:
    """这一章最新的一份可用（active）蓝图。"""
    return latest_active_planning_artifact(
        session,
        artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
        object_type="chapter",
        object_id=chapter_id,
    )


def is_author_architecture(artifact: GenerationPlanningArtifact | None) -> bool:
    """作者亲手写的蓝图（没有经过模型）。"""
    return artifact is not None and artifact.artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT and artifact.llm_call_id is None


def persist_chapter_architecture(
    session: Session,
    chapter_id: str,
    payload: dict[str, Any],
    *,
    llm_call_id: str | None,
    created_by: str,
    source_bundle_id: str | None = None,
    source_bundle_hash: str | None = None,
) -> GenerationPlanningArtifact:
    """这一章的新蓝图：旧的 active 行让位（superseded），新行 active。"""
    for row in session.execute(
        select(GenerationPlanningArtifact).where(
            GenerationPlanningArtifact.artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT,
            GenerationPlanningArtifact.object_type == "chapter",
            GenerationPlanningArtifact.object_id == chapter_id,
            GenerationPlanningArtifact.status == "active",
        )
    ).scalars().all():
        row.status = "superseded"
    artifact = GenerationPlanningArtifact(
        row_id=f"planning_{CHAPTER_ARCHITECTURE_ARTIFACT}_{chapter_id}_{uuid.uuid4().hex[:10]}",
        artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
        object_type="chapter",
        object_id=chapter_id,
        chapter_id=chapter_id,
        scene_id=None,
        payload_json=payload,
        llm_call_id=llm_call_id,
        source_bundle_id=source_bundle_id,
        source_bundle_hash=source_bundle_hash,
        status="active",
        created_by=created_by,
    )
    session.add(artifact)
    session.flush()
    return artifact


def normalize_chapter_architecture(payload: Any, *, strict: bool) -> dict[str, Any]:
    """蓝图 → 六个规范字段。

    - ``strict=False``（作者 / 规划台写的蓝图）：宽容——缺的字段留空、单个值当成一条、空白收拢、字数与条数截到上限；
    - ``strict=True``（场景运行现做的蓝图）：六个字段缺一个、标量是空的、列表是空的或混着空条目都报 ``ValueError``
      （调用方把它翻成 ``CHAPTER_STORY_ARCHITECTURE_OUTPUT_INVALID``）；多余字段忽略（json_object 非严格 schema）。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if strict:
        missing = [field for field in ARCHITECTURE_FIELDS if field not in payload]
        if missing:
            raise ValueError("chapter architecture payload is missing required fields: " + ", ".join(missing))
        normalized: dict[str, Any] = {}
        for field in _SCALAR_FIELDS:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
            normalized[field] = value.strip()
        for field in ARCHITECTURE_LIST_FIELDS:
            value = payload.get(field)
            if not isinstance(value, list) or not value:
                raise ValueError(f"{field} must be a non-empty string array")
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"{field} must contain only non-empty strings")
            normalized[field] = [item.strip() for item in value]
        return {field: normalized[field] for field in ARCHITECTURE_FIELDS}
    lenient: dict[str, Any] = {}
    for field in ARCHITECTURE_FIELDS:
        value = payload.get(field)
        if field in ARCHITECTURE_LIST_FIELDS:
            items = value if isinstance(value, list) else ([value] if value else [])
            lenient[field] = [_clean(item) for item in items if _clean(item)][:_MAX_LIST_ITEMS]
        else:
            lenient[field] = _clean(value)
    return lenient


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())[:_MAX_FIELD_CHARS].strip()

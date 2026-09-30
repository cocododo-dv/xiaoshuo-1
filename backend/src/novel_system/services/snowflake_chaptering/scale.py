"""一章大约装几场（面板要向作者解释这个数的来历）与参考作家的两个可选增强：章长推每章场数、章题画像。

参考书相关的两个读法都是**可选增强**：没有绑定、画像是旧格式、解析失败都只是没有这条建议，分章与起章名不能因此失败。
预期内的失败（领域错误、库读失败、画像字段缺失 / 形状不对）静默返回 None；意料之外的异常记一条带堆栈的警告再返回
None——以前一个 ``except Exception`` 把编程错误也吞成了「没有参考」（B07-16）。
"""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeScenePlan, StoryProject, StyleReferenceProfile
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chaptering.algorithms import hinge_min_chapters
from novel_system.services.style_policy import style_policy_live
from novel_system.services.style_reference.planning_context import reference_titles_payload
from novel_system.services.value_coercion import int_or_default

logger = logging.getLogger(__name__)

# 参考作者无显式场界时,推每章场数所用的「中等场」字数(与场景卡 medium 长度带同量级)
_REFERENCE_DEFAULT_SCENE_CHARS = 1500
_REFERENCE_MAX_SCENES_PER_CHAPTER = 12
#: 可选增强里「预期内」的失败：静默当作没有参考
_EXPECTED_REFERENCE_ERRORS = (DomainError, SQLAlchemyError, KeyError, TypeError, ValueError, AttributeError)


def chapter_scale(
    session: Session,
    project_id: str,
    body: dict[str, Any] | None,
    scenes: list[SnowflakeScenePlan],
) -> dict[str, Any]:
    """一章大约装几场——以及这个数是从哪来的（面板要向作者解释，不能是个黑盒）。

    优先级：作者这一次指名的章数 → 这一次指名的每章场数 → 作品设置的目标章数 →
    参考书的章长（结构画像 chapter_chars 中位 ÷ 场长中位，无显式场界按中等场 1500 字）→ 每章 3 场。
    作者在面板里填「每章约 N 场」必须压过作品设置里的目标章数——那是建项目时随手填的数，
    不该让面板上的输入框形同虚设。铰链优先于这一切：三个灾难各自收束一章，见 ``hinge_min_chapters``。
    """
    payload = body or {}
    total = len(scenes)
    requested_target = int_or_default(payload.get("target_chapter_count"), 0)
    requested_per = int_or_default(payload.get("scenes_per_chapter"), 0)
    project = session.get(StoryProject, project_id)
    project_target = int(getattr(project, "target_chapter_count", 0) or 0)
    reference_hint = None
    target = 0
    if requested_target > 0:
        source, target = "request_target", requested_target
        per_chapter = max(1, math.ceil(total / requested_target)) if total else 1
    elif requested_per > 0:
        source, per_chapter = "request_per_chapter", requested_per
    elif project_target > 0:
        source, target = "project_target", project_target
        per_chapter = max(1, math.ceil(total / project_target)) if total else 1
    else:
        # 2026-09-14 风格保真修补(WP5)：作者什么都没定时，按参考作者的章长推每章场数。
        reference_hint = reference_chapter_scale_hint(session, project_id)
        if reference_hint:
            source, per_chapter = "reference", int(reference_hint["scenes_per_chapter"])
        else:
            source, per_chapter = "default", 3
    return {
        "source": source,
        "target_chapter_count": target,
        "scenes_per_chapter": max(1, per_chapter),
        "scene_count": total,
        "hinge_min_chapters": hinge_min_chapters(scenes),
        "reference_hint": reference_hint,
    }


def reference_chapter_titles(session: Session, project_id: str) -> dict[str, Any] | None:
    """参考作家的章题画像（``planning_context.reference_titles_payload``）；失败 → None（可选增强）。"""
    try:
        return reference_titles_payload(session, project_id)
    except _EXPECTED_REFERENCE_ERRORS:
        return None
    except Exception:  # noqa: BLE001 — 可选增强：不让起章名因参考失败而失败，但留下痕迹
        logger.warning("reference chapter titles failed for %s", project_id, exc_info=True)
        return None


def reference_chapter_scale_hint(session: Session, project_id: str) -> dict[str, Any] | None:
    """项目 / 全局作用域绑定的参考画像 → {chapter_chars_median, scene_chars_median, scenes_per_chapter}。

    绑定只在 ``style_policy_live`` 解析(2026-09-24 S3;轻量路径——这里只要画像 id,不冻结契约);失败都
    返回 None(可选增强,分章提议不能因参考失败而失败)。
    """
    try:
        return _reference_chapter_scale_hint(session, project_id)
    except _EXPECTED_REFERENCE_ERRORS:
        return None
    except Exception:  # noqa: BLE001 — 可选增强：不让分章提议因参考失败而失败，但留下痕迹
        logger.warning("reference chapter scale failed for %s", project_id, exc_info=True)
        return None


def _reference_chapter_scale_hint(session: Session, project_id: str) -> dict[str, Any] | None:
    scope = SimpleNamespace(project_id=str(project_id), scene_id=None, pov_character_id=None, onstage_chars_json=[])
    policy = style_policy_live(session, scope, task_type="scene_generation", freeze_contract=False)
    if not policy.bound or not policy.profile_id:
        return None
    profile = session.get(StyleReferenceProfile, str(policy.profile_id))
    card = (getattr(profile, "profile_json", None) or {}).get("structure_card") if profile is not None else None
    if not isinstance(card, dict) or int(card.get("chapter_count") or 0) <= 1:
        return None
    chapter_median = int((card.get("chapter_chars") or {}).get("median") or 0)
    if chapter_median <= 0:
        return None
    scene_median = 0
    if str(card.get("scene_break_style") or "") == "explicit":
        scene_median = int((card.get("scene_chars") or {}).get("median") or 0)
    typical_scene = scene_median or _REFERENCE_DEFAULT_SCENE_CHARS
    scenes_per_chapter = max(1, min(_REFERENCE_MAX_SCENES_PER_CHAPTER, int(round(chapter_median / typical_scene))))
    return {
        "profile_id": str(getattr(profile, "profile_id", "") or ""),
        "chapter_chars_median": chapter_median,
        "scene_chars_median": scene_median or None,
        "scenes_per_chapter": scenes_per_chapter,
    }

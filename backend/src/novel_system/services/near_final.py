"""准定稿：规划产物、验收评审与评审结果的归一化（B03-21 拆成三个模块，这里转出旧名字）。

- ``near_final_planning``：章架构 / 人物压力两份规划产物（``NearFinalPlanningService``）。
- ``near_final_review``：场景 / 章级准定稿验收评审（``NearFinalAcceptanceService``）。
- ``near_final_payload``：评审结果的归一化、房风门、评审执行失败时的降级结果。

调用方与测试照旧从本模块取这些名字。
"""

from __future__ import annotations

from novel_system.services.near_final_payload import (
    AUTOMATED_REWRITE_FAILURE_CLASSES,
    CHAPTER_ACCEPTANCE_SCORE_KEYS,
    NEAR_FINAL_REWRITE_TYPE,
    NEAR_FINAL_RUBRIC_ID,
    SCENE_ACCEPTANCE_SCORE_KEYS,
    SCENE_FAILURE_CLASSES,
    _apply_scene_near_final_gates,
    _apply_style_bound_rewrite_policy,
    _normalize_acceptance_payload,
)
from novel_system.services.near_final_planning import (
    CHAPTER_ARCHITECTURE_ARTIFACT,
    CHAPTER_ARCHITECTURE_FIELDS,
    CHARACTER_PRESSURE_ARTIFACT,
    CHARACTER_PRESSURE_FIELDS,
    NearFinalPlanningService,
    _planning_user_prompt,
)
from novel_system.services.near_final_review import NearFinalAcceptanceService, _acceptance_user_prompt

__all__ = [
    "AUTOMATED_REWRITE_FAILURE_CLASSES",
    "CHAPTER_ACCEPTANCE_SCORE_KEYS",
    "CHAPTER_ARCHITECTURE_ARTIFACT",
    "CHAPTER_ARCHITECTURE_FIELDS",
    "CHARACTER_PRESSURE_ARTIFACT",
    "CHARACTER_PRESSURE_FIELDS",
    "NEAR_FINAL_REWRITE_TYPE",
    "NEAR_FINAL_RUBRIC_ID",
    "NearFinalAcceptanceService",
    "NearFinalPlanningService",
    "SCENE_ACCEPTANCE_SCORE_KEYS",
    "SCENE_FAILURE_CLASSES",
    "_acceptance_user_prompt",
    "_apply_scene_near_final_gates",
    "_apply_style_bound_rewrite_policy",
    "_normalize_acceptance_payload",
    "_planning_user_prompt",
]

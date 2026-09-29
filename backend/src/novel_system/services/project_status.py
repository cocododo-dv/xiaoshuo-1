"""作品（``StoryProject``）与大纲计划（``OutlinePlan``）的状态词表——叶子模块，不依赖任何服务。

过去这些常量住在 ``projects.py`` 里，目录服务为了一个「本章终审」常量就得连带导入章节运行、系统配置、
提示词构建……（B08-23）。``projects`` 照旧把它们原样再导出。
"""
from __future__ import annotations

PROJECT_STATUS_OUTLINE_DRAFT = "outline_draft"
PROJECT_STATUS_CHAPTER_READY = "chapter_ready"
PROJECT_STATUS_CHAPTER_RUNNING = "chapter_running"
PROJECT_STATUS_CHAPTER_BLOCKED = "chapter_blocked"
PROJECT_STATUS_CHAPTER_FINAL_REVIEW = "chapter_final_review"
PROJECT_STATUS_COMPLETED = "completed"

PLAN_STATUS_PENDING_REVIEW = "pending_review"
PLAN_STATUS_APPROVED = "approved"

REFERENCE_SAFETY_RULES = [
    "参考书只进入抽象风格画像，不复制原文表达。",
    "不得复刻参考书人物、设定、桥段、特殊意象或标志性句式。",
    "运行时只使用节奏、句法、叙事手法、结构技巧和禁复刻规则。",
]

#: 作品状态 → v1 看板 / 运行本章回包的下一步（``outline_draft`` 另看大纲计划）
NEXT_ACTION_BY_STATUS = {
    PROJECT_STATUS_COMPLETED: "completed",
    PROJECT_STATUS_CHAPTER_FINAL_REVIEW: "approve_chapter_final",
    PROJECT_STATUS_CHAPTER_RUNNING: "view_chapter_progress",
    PROJECT_STATUS_CHAPTER_READY: "run_current_chapter",
    PROJECT_STATUS_CHAPTER_BLOCKED: "resolve_blocker",
}

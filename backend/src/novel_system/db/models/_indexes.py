"""外键查找索引：要等全部模型模块都把表登记进 ``Base.metadata`` 之后才建（``db/models/__init__`` 最后导入本模块）。"""

from __future__ import annotations

from sqlalchemy import Index

from novel_system.db.base import Base


# Keep ``Base.metadata.create_all`` test databases aligned with Alembic 0081.
# Every declared foreign key used for parent lookup/deletion must have an index
# whose first column is that foreign-key column.  Existing composite indexes are
# left in their domain models; these are the previously uncovered lookups.
_FOREIGN_KEY_LOOKUP_INDEXES: tuple[tuple[str, str], ...] = (
    ("attempt_tracker", "chapter_id"),
    ("chapter_goals", "outline_plan_id"),
    ("chapter_run_jobs", "chapter_id"),
    ("human_review_events", "chapter_id"),
    ("outline_plans", "project_id"),
    ("qc_reports", "chapter_id"),
    ("scene_blueprints", "chapter_id"),
    ("scene_blueprints", "scene_id"),
    ("scene_bundles", "chapter_id"),
    ("scene_cards", "outline_plan_id"),
    ("scene_drafts", "chapter_id"),
    ("scene_execution_contracts", "project_id"),
    ("scene_execution_contracts", "chapter_id"),
    ("scene_execution_contracts", "scene_id"),
    ("snowflake_artifacts", "project_id"),
    ("snowflake_assistant_turns", "project_id"),
    ("snowflake_character_plans", "project_id"),
    ("snowflake_revision_links", "project_id"),
    ("snowflake_scene_triage_items", "scene_plan_id"),
    ("snowflake_scene_triage_items", "project_id"),
    ("snowflake_step_runs", "project_id"),
    ("story_characters", "project_id"),
    ("style_reference_evidences", "quote_id"),
    ("style_reference_findings", "run_id"),
    ("style_reference_profiles", "run_id"),
    ("style_reference_quotes", "paragraph_id"),
)

for _table_name, _column_name in _FOREIGN_KEY_LOOKUP_INDEXES:
    Index(
        f"ix_{_table_name}_{_column_name}",
        Base.metadata.tables[_table_name].c[_column_name],
    )

"""LLM ledger indexes for the cost dashboard; the active models snapshot keeps only the author's choices.

Revision ID: 20260929_0096
Revises: 20260929_0094
Create Date: 2026-09-30

两件事，一份迁移（2026-09-29 全系统重构 P06）：

1. 成本看板的三个索引（审计 B09-15）。看板与用量读数按 ``llm_calls.project_id`` / ``chapter_id`` 与
   ``llm_call_attempts.created_at`` 过滤，原来都是全表扫描：``ix_llm_calls_project_created(project_id, created_at)``、
   ``ix_llm_calls_chapter(chapter_id)``、``ix_llm_call_attempts_created(created_at)``。普通 CREATE INDEX，可降级。

2. 活动 models 快照瘦身（批准#5a / 重评 R4）。以前「一键补齐」/ 分工把节点 spec 的整份默认参数（温度、输出预算、
   响应格式、推理档位、解码惩罚、model_profile 标签）连同仓库 models.yaml 的 task_routing / model_profiles /
   retry_budget / job_runtime 一起抄进快照，代码里修好的默认值于是永远到不了已配置过的安装（3200 盖住 8192 一类）。
   现在快照只存作者为每个节点选的服务与模型（provider / provider_id / model / api_mode / credential_mode /
   account_id，以及别的显式覆盖如 provider_options / timeout_seconds），其余参数解析时取节点 spec。这一步把
   **当前活动**的 models 快照改写成这个形状，另存一个新版本激活，旧版本标 superseded 留在历史里：
   - 本迁移时注册表里的 32 个节点（下面冻结的清单）：去掉抄进去的默认参数，保留作者的选择；
   - 老快照 ``task_routing`` 里 node_routing 没有的条目并进 node_routing（解析结果不变），然后整张表不再存；
     ``stylize`` 别名没有调用方，不再存；
   - 不在清单里的退役节点原样留着（设置页照旧列为 stale_routes，下一次一键补齐 / 分工剪掉）；
   - ``model_profiles`` / ``role_assignments``（没人读）、``retry_budget`` / ``job_runtime``（以仓库 models.yaml 为准）
     不再存。
   已经是这个形状（没有要去掉的东西）就什么都不做，所以可以重复跑。

降级：删掉三个索引；本迁移另存的快照若仍是活动版本，就重新激活它的来源快照并删掉它（作者之后又保存过路由的，
以作者的为准，不动）。历史迁移是冻结的显式 SQL，不导入应用 ORM 与节点注册表。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import yaml
from alembic import op


revision = "20260929_0096"
down_revision = "20260929_0094"
branch_labels = None
depends_on = None

SNAPSHOTS = "system_config_snapshots"
MIGRATION_ACTOR = "migration:20260929_0096"

INDEXES = (
    ("ix_llm_calls_project_created", "llm_calls", ("project_id", "created_at")),
    ("ix_llm_calls_chapter", "llm_calls", ("chapter_id",)),
    ("ix_llm_call_attempts_created", "llm_call_attempts", ("created_at",)),
)

# 本迁移时节点注册表里的全部节点（冻结：以后注册表增删节点不影响这份迁移）
ACTIVE_NODE_IDS = frozenset(
    {
        "extraction",
        "snowflake_step_candidates",
        "style_ref_paragraph_classify_anchor",
        "style_ref_paragraph_classify_bulk",
        "style_ref_extract_language",
        "style_ref_extract_narrative",
        "style_ref_extract_scene",
        "style_ref_extract_theme",
        "style_ref_synthesize_profile",
        "style_ref_protected_terms",
        "style_ref_tag_windows",
        "snowflake_step_generate",
        "snowflake_workspace_assistant",
        "snowflake_scene_triage",
        "snowflake_chapter_plan",
        "scene_blueprint",
        "character_pressure_blueprint",
        "chapter_story_architecture",
        "chapter_scene_plan_candidates",
        "chapter_scene_plan_fill",
        "chapter_plan_review",
        "neutral_draft",
        "style_draft",
        "style_patch",
        "scene_literary_rewrite",
        "hard_qc",
        "soft_qc",
        "near_final_acceptance_review",
        "chapter_near_final_review",
        "writer_passage_patch",
        "writer_deep_review",
        "author_proposal_generate",
    }
)
# 以前从节点 spec 抄进快照的默认参数：去掉后解析时取 spec
SPEC_COPIED_FIELDS = (
    "temperature",
    "max_output_tokens",
    "response_format",
    "reasoning_level",
    "frequency_penalty",
    "presence_penalty",
    "top_p",
    "model_profile",
)
LEGACY_TASK_ALIASES = frozenset({"stylize"})
DROPPED_SECTIONS = ("task_routing", "model_profiles", "role_assignments", "retry_budget", "job_runtime")


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def lean_models_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    """一份 models 快照内容 → 只存作者选择的形状（纯函数，迁移测试直接调）。"""
    result = {key: value for key, value in parsed.items() if key not in DROPPED_SECTIONS and key != "node_routing"}
    raw_node_routing = parsed.get("node_routing")
    node_routing: dict[str, Any] = dict(raw_node_routing) if isinstance(raw_node_routing, dict) else {}
    legacy = parsed.get("task_routing")
    if isinstance(legacy, dict):
        for key, route in legacy.items():
            if key not in LEGACY_TASK_ALIASES and key not in node_routing:
                node_routing[key] = route
    lean: dict[str, Any] = {}
    for node_id, route in node_routing.items():
        if node_id in ACTIVE_NODE_IDS and isinstance(route, dict):
            route = {key: value for key, value in route.items() if key not in SPEC_COPIED_FIELDS}
        lean[node_id] = route
    result["node_routing"] = lean
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _active_models_snapshot(bind: sa.engine.Connection) -> sa.engine.Row | None:
    return bind.execute(
        sa.text(
            f"SELECT snapshot_id, version, parsed_json FROM {SNAPSHOTS} "
            "WHERE category = 'models' AND active_flag = 1 "
            "ORDER BY version DESC, created_at DESC LIMIT 1"
        )
    ).first()


def _slim_active_models_snapshot(bind: sa.engine.Connection) -> None:
    active = _active_models_snapshot(bind)
    if active is None:
        return
    parsed = _load_json(active.parsed_json)
    lean = lean_models_payload(parsed)
    if lean == parsed:
        return
    next_version = int(
        bind.execute(sa.text(f"SELECT COALESCE(MAX(version), 0) FROM {SNAPSHOTS} WHERE category = 'models'")).scalar()
        or 0
    ) + 1
    now = _now()
    bind.execute(
        sa.text(f"UPDATE {SNAPSHOTS} SET active_flag = 0, status = 'superseded' WHERE category = 'models' AND active_flag = 1")
    )
    bind.execute(
        sa.text(
            f"INSERT INTO {SNAPSHOTS} "
            "(snapshot_id, category, version, yaml_raw, parsed_json, validation_json, status, active_flag, "
            "created_by, created_at, activated_at) "
            "VALUES (:snapshot_id, 'models', :version, :yaml_raw, :parsed_json, :validation_json, 'active', 1, "
            ":created_by, :created_at, :activated_at)"
        ),
        {
            "snapshot_id": f"config_models_{uuid.uuid4().hex[:12]}",
            "version": next_version,
            "yaml_raw": yaml.safe_dump(lean, allow_unicode=True, sort_keys=False),
            "parsed_json": json.dumps(lean),
            "validation_json": json.dumps(
                {"ok": True, "message": "models config is valid", "migrated_from": active.snapshot_id}
            ),
            "created_by": MIGRATION_ACTOR,
            "created_at": now,
            "activated_at": now,
        },
    )


def _restore_slimmed_models_snapshot(bind: sa.engine.Connection) -> None:
    active = bind.execute(
        sa.text(
            f"SELECT snapshot_id, validation_json FROM {SNAPSHOTS} "
            "WHERE category = 'models' AND active_flag = 1 AND created_by = :actor "
            "ORDER BY version DESC, created_at DESC LIMIT 1"
        ),
        {"actor": MIGRATION_ACTOR},
    ).first()
    if active is None:
        return
    source_id = _load_json(active.validation_json).get("migrated_from")
    if not source_id:
        return
    source = bind.execute(
        sa.text(f"SELECT snapshot_id FROM {SNAPSHOTS} WHERE snapshot_id = :snapshot_id"), {"snapshot_id": source_id}
    ).first()
    if source is None:
        return
    bind.execute(sa.text(f"DELETE FROM {SNAPSHOTS} WHERE snapshot_id = :snapshot_id"), {"snapshot_id": active.snapshot_id})
    bind.execute(
        sa.text(f"UPDATE {SNAPSHOTS} SET active_flag = 1, status = 'active' WHERE snapshot_id = :snapshot_id"),
        {"snapshot_id": source_id},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, columns in INDEXES:
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)})"))
    _slim_active_models_snapshot(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _restore_slimmed_models_snapshot(bind)
    for name, _table, _columns in INDEXES:
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))

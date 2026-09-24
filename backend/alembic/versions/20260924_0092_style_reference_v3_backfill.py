"""Style reference v3 backfill: bindings to the v3 config keys, legacy profiles archived, legacy review cards removed.

Revision ID: 20260924_0092
Revises: 20260923_0091
Create Date: 2026-09-24

2026-09-24 风格参考 v3 清理（docs/style-reference-v3-2026-09-23.md §8.1 S1）——只改数据，不改结构：
- (a) ``style_reference_injection_bindings``：每条绑定的 ``config_json`` 回填成 v3 键——``reference_mode``（旧策略 A →
  ``card_only``，其余 → ``full``；已有合法值不动）、``sample_windows``（缺失时由旧 ``intensity`` 换算 round(3 + 9·i/100)、
  夹到 0–16；没有 intensity → 12）、``dimension_states``（缺失 → ``{}``，读时补 normal）；``draft_mode`` 只在原来就有
  合法值时保留、**不回填**（缺键仍在契约构建期取 yaml 默认）；旧键（intensity / sub_dimensions / include_*）不再写；
  ``strategy`` 列统一写 ``mixed``。此后代码里不再解释任何旧键（``binding_config.normalize_binding_config`` 只认四键）。
- (b) ``style_reference_profiles``：``profile_json.profile_version != "style_profile_v3"``（缺失也算）且未归档的画像
  → ``status = "archived"``（旧画像没有文风卡；界面对这本书显示「没学过」，学习文风时就地更新同一份画像并复活）。
- (c) ``review_items``：删掉旧版风格参考写下的三种待办行（``review_style_ref_finding_*`` / ``review_style_ref_apply_*`` /
  ``review_style_ref_calib_*``，与 ``style_reference/cleanup.py`` 按书清理时用的前缀相同）；写它们的物化 / 校准模块
  与「应用画像」待办卡的 effect 都已删除。

降级是空操作：改写前的旧键与旧状态没有留副本（旧键的语义已由新键完整表达；被归档的旧画像重新学习即复活；
删掉的待办行无处可回）。历史迁移是冻结的显式 SQL，不导入应用 ORM。
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260924_0092"
down_revision = "20260923_0091"
branch_labels = None
depends_on = None

BINDINGS = "style_reference_injection_bindings"
PROFILES = "style_reference_profiles"
REVIEW_ITEMS = "review_items"

PROFILE_VERSION_V3 = "style_profile_v3"
REFERENCE_MODES = ("full", "samples_only", "card_only")
DRAFT_MODES = ("style_first", "neutral_first")
DEFAULT_SAMPLE_WINDOWS = 12
MIN_SAMPLE_WINDOWS = 0
MAX_SAMPLE_WINDOWS = 16
# 与 cleanup.py 按书清理时的三种前缀相同（``_`` 在 LIKE 里是通配符，逐字转义）
LEGACY_REVIEW_ID_PREFIXES = (
    "review_style_ref_finding_",
    "review_style_ref_apply_",
    "review_style_ref_calib_",
)


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


def _clamp_windows(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_WINDOWS
    return max(MIN_SAMPLE_WINDOWS, min(MAX_SAMPLE_WINDOWS, number))


def _intensity_to_windows(intensity: Any) -> int:
    """旧强度 i（0–100）→ 窗数 round(3 + 9·i/100)：与被删的 ``binding_config.legacy_intensity_to_windows`` 同一公式。"""
    try:
        value = int(round(float(intensity)))
    except (TypeError, ValueError):
        value = 100
    value = max(0, min(100, value))
    return int(round(3 + 9 * value / 100))


def v3_binding_config(strategy: Any, config: dict[str, Any]) -> dict[str, Any]:
    """一条旧绑定 → v3 键（已有合法值不动；``draft_mode`` 只保留、不回填）。"""
    mode = str(config.get("reference_mode") or "").strip().lower()
    if mode not in REFERENCE_MODES:
        mode = "card_only" if str(strategy or "") == "A" else "full"
    if "sample_windows" in config:
        windows = _clamp_windows(config.get("sample_windows"))
    elif "intensity" in config:
        windows = _clamp_windows(_intensity_to_windows(config.get("intensity")))
    else:
        windows = DEFAULT_SAMPLE_WINDOWS
    states = config.get("dimension_states")
    out: dict[str, Any] = {
        "reference_mode": mode,
        "sample_windows": windows,
        "dimension_states": dict(states) if isinstance(states, dict) else {},
    }
    draft_mode = str(config.get("draft_mode") or "").strip().lower()
    if draft_mode in DRAFT_MODES:
        out["draft_mode"] = draft_mode
    return out


def _backfill_bindings(bind: Any) -> None:
    rows = bind.execute(sa.text(f"SELECT binding_id, strategy, config_json FROM {BINDINGS}")).fetchall()
    update = sa.text(f"UPDATE {BINDINGS} SET config_json = :config_json, strategy = 'mixed' WHERE binding_id = :binding_id")
    for binding_id, strategy, raw_config in rows:
        before = _load_json(raw_config)
        after = v3_binding_config(strategy, before)
        if after == before and str(strategy or "") == "mixed":
            continue
        bind.execute(
            update,
            {"binding_id": binding_id, "config_json": json.dumps(after, ensure_ascii=False, sort_keys=True)},
        )


def _archive_legacy_profiles(bind: Any) -> None:
    rows = bind.execute(sa.text(f"SELECT profile_id, status, profile_json FROM {PROFILES}")).fetchall()
    update = sa.text(f"UPDATE {PROFILES} SET status = 'archived' WHERE profile_id = :profile_id")
    for profile_id, status, raw_json in rows:
        if str(status or "") == "archived":
            continue
        version = str(_load_json(raw_json).get("profile_version") or "")
        if version == PROFILE_VERSION_V3:
            continue
        bind.execute(update, {"profile_id": profile_id})


def _delete_legacy_review_items(bind: Any) -> None:
    for prefix in LEGACY_REVIEW_ID_PREFIXES:
        pattern = prefix.replace("_", r"\_") + "%"
        bind.execute(
            sa.text(f"DELETE FROM {REVIEW_ITEMS} WHERE review_id LIKE :pattern ESCAPE '\\'"),
            {"pattern": pattern},
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if BINDINGS in tables:
        _backfill_bindings(bind)
    if PROFILES in tables:
        _archive_legacy_profiles(bind)
    if REVIEW_ITEMS in tables:
        _delete_legacy_review_items(bind)


def downgrade() -> None:
    """空操作：这次迁移只改数据，改写前的旧键 / 旧状态 / 删掉的待办行没有副本，也不需要回来（见模块说明）。"""

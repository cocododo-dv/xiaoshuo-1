"""雪花十步：步骤目录、写作指引、草稿形状、规则诊断的门面（2026-09-30 按职责拆成四个模块，B06-09）。

- ``snowflake_step_catalog``：目录与常量、只读访问器；
- ``snowflake_step_guidance``：写作指引、检查清单、编辑器提示语；
- ``snowflake_step_drafts``：默认稿、合并与归一化、第 10 步的场景种子；
- ``snowflake_step_diagnosis``：完整度与规则诊断。

这里原样转出四个模块的全部名字（含测试引用的私有名），调用方的 import 不必改。
"""

from __future__ import annotations

from novel_system.services.snowflake_step_catalog import (  # noqa: F401
    CHARACTER_STEPS,
    SCENE_FORMS,
    SYNOPSIS_PARAGRAPHS,
    coerce_scene_form,
    CONFIRMED_STEP_STATUSES,
    LONG_SYNOPSIS_PARAGRAPHS,
    MATERIALIZATION_REQUIRED_STEPS,
    MATERIALIZATION_REQUIREMENTS,
    MATERIALIZATION_WARNING_STEPS,
    QUALITY_POLICY,
    RENDERING_MODES,
    SNOWFLAKE_METHOD_VERSION,
    SNOWFLAKE_STEP_CATALOG,
    STEP_ORDER,
    SUMMARY_LENGTH_BAND,
    effective_rendering_mode,
    get_step_definition,
    list_step_definitions,
    step_definition_view,
    step_definition_views,
    step_label,
)
from novel_system.services.snowflake_step_guidance import (  # noqa: F401
    _DEFAULT_GUIDANCE,
    _FIELD_HELP,
    _GUIDANCE_CHECKLIST,
    _GUIDANCE_RUBRIC,
    _REFERENCE_GUIDANCE_TIMEBOX,
    _REFERENCE_STEP_INSTRUCTIONS,
    _STEP_GUIDANCE,
    _enrich_editor_fields,
    editor_payload,
    step_guidance,
)
from novel_system.services.snowflake_step_drafts import (  # noqa: F401
    _coerce_positional_list,
    _merge_dicts,
    _normalize_character_bible,
    _normalize_scene_item,
    _normalize_step_draft,
    _scene_detail_seed,
    default_step_draft,
    derive_three_act,
    merge_step_draft,
)
from novel_system.services.snowflake_step_diagnosis import (  # noqa: F401
    SCENE_FIELD_EXAMPLES,
    _FIELD_DISPLAY_LABELS,
    _GENERIC_FRAGMENTS,
    _LEAD_ROLE_MARKERS,
    _PLACEHOLDER_MARKERS,
    _SCENE_PLACEHOLDER_TEXTS,
    _character_pressure_text,
    _collect_scene_placeholder_texts,
    _diagnose_scene_step_pressure,
    _diagnostic_fix_steps,
    _field_display_label,
    _flag_key,
    _hard_blockers_from_flags,
    _has_cost_or_reversal,
    _has_escalating_conflict,
    _has_pressure_turn,
    _has_true_choice_cost,
    _looks_generic,
    _missing_fields_for_step,
    _missing_scene_detail_fields,
    _normalize_placeholder_text,
    _points_to_next_goal,
    _pressure_result,
    _repeated_crucible_advice,
    _scene_field_placeholder_like,
    _text,
    _total_fields_for_step,
    _unique,
    _weak_scene_pressure_flags,
    diagnose_scene_detail,
    diagnose_step_pressure,
    is_lead_role,
    is_protagonist_role,
    step_completeness,
)

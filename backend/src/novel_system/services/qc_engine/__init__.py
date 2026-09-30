"""硬 / 软质检引擎与管线里的原文重合门（B04-02：原来 2,700 行的 qc_engine.py 按职责拆成这个包）。

- ``base``：结论类型、报告号、尝试记录、引擎骨架（构造、提示词装配与 LLM 运行器、开人工复核事件）；
- ``degradation``：质检节点的 LLM 调用与三级受控降级（只在账本证据成立时降级）；
- ``issues``：质检意见的确定性加工（约束冲突注解、确定性连续性 issue、代词意见过滤、修改简报合并）；
- ``scores``：软质检分数的刻度换算与参考评审记录；
- ``styled_gate``：中性步位稿与风格稿的原文重合门（唯一抄袭门 + 受保护专名，MetricEvent 审计）；
- ``hard`` / ``soft``：两个引擎。

调用方与测试从旧模块路径取的名字在这里原样再导出（含几个测试直接用的私有名）。测试替身要换掉引擎用的
``LLMNodeRunner`` 时改 ``qc_engine.base.LLMNodeRunner``——引擎在那里取它。
"""

from novel_system.services.qc_engine.base import (
    HardQcDecision,
    QcDecision,
    QcEngineBase,
    SoftQcDecision,
    _build_qc_report_id,
)
from novel_system.services.qc_engine.degradation import (
    CONTINUITY_BUDGET_ISSUE_KEY,
    CONTINUITY_BUDGET_MESSAGE,
    _qc_build_user_prompt,
    _qc_run_node_with_degradation,
)
from novel_system.services.qc_engine.hard import HardQcEngine
from novel_system.services.qc_engine.issues import LLM_PRONOUN_ISSUE_KEYS, _deterministic_quality_issues
from novel_system.services.qc_engine.scores import SOFT_QC_SCORE_FIELDS, _normalize_soft_qc_scores
from novel_system.services.qc_engine.soft import SoftQcEngine
from novel_system.services.qc_engine.styled_gate import (
    HARD_QC_GATE_EVENT_KIND,
    STYLE_BANNED_TERM_ISSUE_KEY,
    STYLE_GATE_UNAVAILABLE_ISSUE_KEY,
    STYLE_PLAGIARISM_ISSUE_KEY,
    STYLE_VALIDATION_PLAGIARISM_TRIGGER,
    STYLED_DRAFT_GATE_EVENT_KIND,
    STYLED_DRAFT_GATE_STAGES,
    STYLED_GATE_UNAVAILABLE_VERDICT,
    _STYLED_GATE_MAX_HITS,
    _styled_gate_report,
    _styled_gate_result,
    run_styled_draft_style_gate,
    scene_gate_style_policy,
    styled_gate_unavailable_result,
)

__all__ = [
    "CONTINUITY_BUDGET_ISSUE_KEY",
    "CONTINUITY_BUDGET_MESSAGE",
    "HARD_QC_GATE_EVENT_KIND",
    "LLM_PRONOUN_ISSUE_KEYS",
    "SOFT_QC_SCORE_FIELDS",
    "STYLED_DRAFT_GATE_EVENT_KIND",
    "STYLED_DRAFT_GATE_STAGES",
    "STYLED_GATE_UNAVAILABLE_VERDICT",
    "STYLE_BANNED_TERM_ISSUE_KEY",
    "STYLE_GATE_UNAVAILABLE_ISSUE_KEY",
    "STYLE_PLAGIARISM_ISSUE_KEY",
    "STYLE_VALIDATION_PLAGIARISM_TRIGGER",
    "HardQcDecision",
    "HardQcEngine",
    "QcDecision",
    "QcEngineBase",
    "SoftQcDecision",
    "SoftQcEngine",
    "_STYLED_GATE_MAX_HITS",
    "_build_qc_report_id",
    "_deterministic_quality_issues",
    "_normalize_soft_qc_scores",
    "_qc_build_user_prompt",
    "_qc_run_node_with_degradation",
    "_styled_gate_report",
    "_styled_gate_result",
    "run_styled_draft_style_gate",
    "scene_gate_style_policy",
    "styled_gate_unavailable_result",
]

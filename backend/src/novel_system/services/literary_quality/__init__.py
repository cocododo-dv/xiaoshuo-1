"""文学质量：规则引擎与文学质量视图（B04-01：原来 2,900 行的 literary_quality.py 按职责拆成这个包）。

- ``dimensions``：规则维度的词汇（维度、权重、中文名与「问题 / 改法」、严重度、文本层、signal id、统一形状）；
- ``lexicons``：词表；``text``：文本助手；
- ``calibration``：按参考书校准；``scoring``：证据充分度与自动诊断分的上限；
- ``rules``：规则维度与 ``analyze_literary_quality``；
- ``fingerprint``：质量指纹；``candidates``：候选稿的对抗排名分与离散度；
- ``report``：视图的报告层（统一形状的发现、定位、风险簇、跨场复用、推荐动作、整维已忽略）；
- ``chapter_set``：章组复审；``service``：``LiteraryQualityService``（包里唯一读库的模块）。

依赖只朝下：text / lexicons ← dimensions / calibration ← scoring ← rules ← candidates / report ← service
（fingerprint、chapter_set 只靠词表与文本助手）。调用方从旧模块路径取的名字在这里原样再导出。
"""

from novel_system.services.literary_quality.calibration import (
    DEFAULT_RULE_CALIBRATION,
    RULE_CALIBRATION_CONFIDENCE_Z,
    RULE_DIMENSION_COMMON_SHARE,
    RULE_DIMENSION_HABIT_SHARE,
    RULE_ENDING_DIMENSIONS,
    RULE_JUDGE_WINDOW_CHARS,
    RULE_NEEDLE_TAIL_ALPHA,
    RULE_REPETITION_DIMENSIONS,
    RuleCalibration,
    calibrate_lexicons,
    calibrated_lexicons,
    dimension_level,
    poisson_tail,
    wilson_lower_bound,
)
from novel_system.services.literary_quality.candidates import (
    ADVERSARIAL_DIMS,
    adversarial_rank_score,
    candidate_dispersion,
)
from novel_system.services.literary_quality.dimensions import (
    AUTOMATED_DIAGNOSTIC_CEILING,
    AUTOMATED_EVIDENCE_SIGNAL,
    AUTOMATED_EVIDENCE_TARGET_CHARS,
    AUTOMATED_EVIDENCE_TARGET_SENTENCES,
    DIMENSION_LABELS,
    DIMENSION_NOTES,
    DIMENSION_WEIGHTS,
    FINDING_ANCHORS,
    QUALITY_DIMENSIONS,
    QUALITY_TEXT_LAYERS,
    RULE_SIGNAL_SOURCE,
    SEVERITY_RANK,
    describe_rule_finding,
    dimension_label,
    get_dimension_weights,
    rule_signal_id,
    unify_rule_finding,
)
from novel_system.services.literary_quality.fingerprint import fingerprint_literary_quality
from novel_system.services.literary_quality.lexicons import FAULT_LEXICONS
from novel_system.services.literary_quality.report import (
    ignored_dimensions_from_findings,
    ignored_rule_dimensions,
)
from novel_system.services.literary_quality.rules import analyze_literary_quality
from novel_system.services.literary_quality.scoring import (
    automated_diagnostic_assessment,
    automated_evidence_sufficiency,
)
from novel_system.services.literary_quality.service import (
    LiteraryQualityService,
    RuleCalibrationResolver,
)

__all__ = [
    "ADVERSARIAL_DIMS",
    "AUTOMATED_DIAGNOSTIC_CEILING",
    "AUTOMATED_EVIDENCE_SIGNAL",
    "AUTOMATED_EVIDENCE_TARGET_CHARS",
    "AUTOMATED_EVIDENCE_TARGET_SENTENCES",
    "DEFAULT_RULE_CALIBRATION",
    "DIMENSION_LABELS",
    "DIMENSION_NOTES",
    "DIMENSION_WEIGHTS",
    "FAULT_LEXICONS",
    "FINDING_ANCHORS",
    "QUALITY_DIMENSIONS",
    "QUALITY_TEXT_LAYERS",
    "RULE_CALIBRATION_CONFIDENCE_Z",
    "RULE_DIMENSION_COMMON_SHARE",
    "RULE_DIMENSION_HABIT_SHARE",
    "RULE_ENDING_DIMENSIONS",
    "RULE_JUDGE_WINDOW_CHARS",
    "RULE_NEEDLE_TAIL_ALPHA",
    "RULE_REPETITION_DIMENSIONS",
    "RULE_SIGNAL_SOURCE",
    "SEVERITY_RANK",
    "LiteraryQualityService",
    "RuleCalibration",
    "RuleCalibrationResolver",
    "adversarial_rank_score",
    "analyze_literary_quality",
    "automated_diagnostic_assessment",
    "automated_evidence_sufficiency",
    "calibrate_lexicons",
    "calibrated_lexicons",
    "candidate_dispersion",
    "describe_rule_finding",
    "dimension_label",
    "dimension_level",
    "fingerprint_literary_quality",
    "get_dimension_weights",
    "ignored_dimensions_from_findings",
    "ignored_rule_dimensions",
    "poisson_tail",
    "rule_signal_id",
    "unify_rule_finding",
    "wilson_lower_bound",
]

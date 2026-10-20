"""文学质量：规则引擎（B04-01：原来 2,900 行的 literary_quality.py 按职责拆成这个包）。规则发现进场景诊断
（写作台深改的「规则」来源）与成稿门的 Q3 警告；单独的文学质量视图与它的三个接口（巡检 / 即时扫描 / 章组复审）
2026-10-20 退役，``service`` / ``chapter_set`` 两个模块随之删掉。

- ``dimensions``：规则维度的词汇（维度、权重、中文名与「问题 / 改法」、严重度、文本层、signal id、统一形状）；
- ``lexicons``：词表；``text``：文本助手；
- ``calibration``：按参考书校准；``scoring``：证据充分度与自动诊断分的上限；
- ``rules``：规则维度与 ``analyze_literary_quality``；
- ``fingerprint``：质量指纹；``candidates``：候选稿的对抗排名分；
- ``report``：「整维已忽略」的判定（成稿门用）；
- ``calibration_source``：参考书那一侧的校准读数（读参考书的段落表与绑定的画像），不从这里再导出——
  要用的地方按模块导入，导入这个包不拉起它。

依赖只朝下：text / lexicons ← dimensions / calibration ← scoring ← rules ← candidates / report
（fingerprint 只靠词表与文本助手；calibration_source 在 rules 之上）。调用方从旧模块路径取的名字在这里原样再导出。
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
    weighted_score,
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
    "RuleCalibration",
    "adversarial_rank_score",
    "analyze_literary_quality",
    "automated_diagnostic_assessment",
    "automated_evidence_sufficiency",
    "calibrate_lexicons",
    "calibrated_lexicons",
    "describe_rule_finding",
    "dimension_label",
    "dimension_level",
    "fingerprint_literary_quality",
    "ignored_dimensions_from_findings",
    "ignored_rule_dimensions",
    "poisson_tail",
    "rule_signal_id",
    "unify_rule_finding",
    "weighted_score",
    "wilson_lower_bound",
]

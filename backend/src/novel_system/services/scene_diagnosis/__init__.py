"""场景诊断（2026-09-22）：一场正文「哪里有问题」只有一份记录。

之前有四套引擎各算各的——文学质量视图的规则维度、写作台深改姿态里三条浏览器本地正则、
后端从没被任何界面调用过的 LLM 深评（``writer_deep_review``）、起草管线在去模板门 / 成稿门
里再跑一遍的同一批规则——三套词汇、三种严重度，页面之间只有跳转、没有数据。这里把它们
合成**一种**发现形状，写作台的深改面板是唯一的展示处：

    {signal_id, source, dimension, label, lens, severity, issue, recommendation, why,
     evidence: {excerpt, paragraph_index, start, end} | None, context,
     ignored, stale, house_taste, origin, patch, opinion}

* ``source``：``rules``（规则维度，``literary_quality`` 包）/ ``craft``（段落节奏：贴邻叠句、
  段落偏长、句首重复——从浏览器本地规则搬到服务端）/ ``review``（起草台的准定稿评审）/
  ``ai``（写作台的 AI 深评：整场一次、「AI 看这一处」的局部深评、成稿中心「AI 通读本章」
  里落到这一场的发现——``origin.kind`` 区分 ``scene`` / ``passage`` / ``chapter``）。
* ``signal_id`` 稳定：规则按「命中了什么」取 id（`literary_quality.rule_signal_id`），节奏按
  段落开头与命中文本，评审 / 深评按维度 + 证据。作者的「忽略」记在 ``SceneCard`` 的
  ``deep_review_ignored_keys_json`` 里，按 id 生效，并反向作用到文学质量视图与成稿门。
* ``evidence`` 钉到**编辑器段落**：``paragraph_index`` 是写作台 ``p, blockquote`` 的序号，
  ``start / end`` 是这一段可见文字里的偏移；正文是作者稿 HTML，这里按同一规则拆段。
* ``stale``：评审 / 深评的证据在当前正文里已找不到（多半改掉了）；``ai.status`` /
  ``review.status`` 另说整份评审是不是改前的。
* ``opinion``：「AI 看这一处」对这条发现的判断（成立 / 部分成立 / 不成立）与改法。局部深评看的是
  焦点段（一段、一段范围或几条发现所在的段）加**整场正文**（``passage_scope``：焦点段与前后段标出，
  其余段全文给到 ``PASSAGE_SCENE_FULL_CHARS`` 字，再长的远段只留开头），所以它能指出焦点段与本场
  另一段的矛盾——那样的发现带 ``related``（另一段的原话与段号）。
* 有风格绑定的场，检查按参考作者校准（``craft_calibration``）：节奏三条（段落偏长的阈值取参考书
  段长的长尾，参考作者常用的贴邻叠句 / 句首重复不再提示）+ 规则维度的词表与维度
  （``literary_quality.RuleCalibration``：参考作者每万字用到一次以上的词表词不当毛病，在参考书一半
  以上的场级窗口上都会响的规则降为提示并带 ``calibrated``）；规则与节奏发现标 ``house_taste``。
* 计数随写回传：``scene_rollup`` / ``chapter_rollup`` 是作者稿保存、深评动作、通读的响应里带的
  ``diagnosis_rollup``——主页 / 成稿中心的角标不必再拉整本书的汇总。

包的分工（依赖只朝下）：``vocabulary``（口径、来源、维度中文名）← ``text``（诊断看的正文、定位、局部深评的
范围）← ``calibration``（节奏检查的参考书读数；规则维度那一半在 ``literary_quality.calibration_source``）←
``findings``（各来源 → 统一发现、计数、进程缓存）← ``serialize``（改写候选 / 局部深评的序列化）← ``service``
（``SceneDiagnosisService``：读库、拼载荷、计数）。只依赖 ``literary_quality``、``manuscript_html``、两个纯函数的
段型判断、模型与风格绑定解析；``writer_deep_review`` / ``api.routes`` 从这里取载荷。调用方从这个包取的名字在这里
原样再导出。
"""

from novel_system.services.literary_quality.calibration_source import (
    RULE_CALIBRATION_MAX_ENDINGS,
    RULE_CALIBRATION_MAX_WINDOWS,
    RULE_CALIBRATION_MIN_ENDINGS,
    RULE_CALIBRATION_MIN_WINDOWS,
    RULE_CALIBRATION_WINDOW_CHARS,
    TRANSITION_PARAGRAPH_TYPE,
    BoundProfile,
    compute_reference_rules,
    rule_calibration_from_reference,
)
from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.scene_diagnosis.calibration import (
    CRAFT_ECHO_HABIT_PER_1K,
    CRAFT_LONG_PARAGRAPH_CHARS,
    CRAFT_SAME_OPENING_HABIT_PER_1K,
    DEFAULT_CRAFT_CALIBRATION,
    CraftCalibration,
    calibration_from_reference,
    compute_reference_craft,
    craft_calibration_for,
)
from novel_system.services.scene_diagnosis.findings import (
    cached_text_findings,
    craft_findings,
    evaluation_findings,
    finding_counts,
    finding_sort_key,
    rule_findings,
)
from novel_system.services.scene_diagnosis.serialize import (
    serialize_passage_review,
    serialize_patch_candidate,
)
from novel_system.services.scene_diagnosis.service import (
    STYLE_TASK_TYPE,
    SceneDiagnosisService,
    summarize_counts,
)
from novel_system.services.scene_diagnosis.text import (
    PASSAGE_FAR_PARAGRAPH_HEAD,
    PASSAGE_SCENE_FULL_CHARS,
    DiagnosisText,
    locate,
    locate_in_paragraphs,
    passage_scope,
)
from novel_system.services.scene_diagnosis.vocabulary import (
    AI_DIMENSION_LABELS,
    CRAFT_LABELS,
    DIAGNOSIS_SOURCES,
    LENS_LABELS,
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    NEAR_FINAL_RUBRIC_ID,
    PASSAGE_RELATION_KINDS,
    PASSAGE_RELATION_LABELS,
    PASSAGE_VERDICT_LABELS,
    PASSAGE_VERDICTS,
    PATCH_CATEGORIES,
    REVIEW_DIMENSION_LABELS,
    SEVERITIES,
    SOURCE_LABELS,
    candidate_category_for_dimension,
)

__all__ = [
    "AI_DIMENSION_LABELS",
    "CRAFT_ECHO_HABIT_PER_1K",
    "CRAFT_LABELS",
    "CRAFT_LONG_PARAGRAPH_CHARS",
    "CRAFT_SAME_OPENING_HABIT_PER_1K",
    "DEFAULT_CRAFT_CALIBRATION",
    "DIAGNOSIS_SOURCES",
    "LENS_LABELS",
    "LITERARY_REVISION_PASSAGE_RUBRIC_ID",
    "LITERARY_REVISION_RUBRIC_ID",
    "NEAR_FINAL_RUBRIC_ID",
    "PASSAGE_FAR_PARAGRAPH_HEAD",
    "PASSAGE_RELATION_KINDS",
    "PASSAGE_RELATION_LABELS",
    "PASSAGE_SCENE_FULL_CHARS",
    "PASSAGE_VERDICTS",
    "PASSAGE_VERDICT_LABELS",
    "PATCH_CATEGORIES",
    "REVIEW_DIMENSION_LABELS",
    "RULE_CALIBRATION_MAX_ENDINGS",
    "RULE_CALIBRATION_MAX_WINDOWS",
    "RULE_CALIBRATION_MIN_ENDINGS",
    "RULE_CALIBRATION_MIN_WINDOWS",
    "RULE_CALIBRATION_WINDOW_CHARS",
    "SEVERITIES",
    "SOURCE_LABELS",
    "STYLE_TASK_TYPE",
    "TRANSITION_PARAGRAPH_TYPE",
    "BoundProfile",
    "CraftCalibration",
    "DiagnosisText",
    "SceneDiagnosisService",
    "cached_text_findings",
    "calibration_from_reference",
    "candidate_category_for_dimension",
    "compute_reference_craft",
    "compute_reference_rules",
    "craft_calibration_for",
    "craft_findings",
    "evaluation_findings",
    "finding_counts",
    "finding_sort_key",
    "locate",
    "locate_in_paragraphs",
    "manuscript_paragraphs",
    "passage_scope",
    "rule_calibration_from_reference",
    "rule_findings",
    "serialize_passage_review",
    "serialize_patch_candidate",
    "summarize_counts",
]

"""场景诊断的词汇：评审口径、来源、严重度、各来源的维度与中文名、局部深评的判定与关系、修补类别（纯常量，叶子）。"""

from __future__ import annotations

# 评审来源的 rubric id。深评的在这里定义（writer_deep_review 从这里取）；准定稿评审的
# 与 near_final.NEAR_FINAL_RUBRIC_ID 相同——测试钉住两者相等，这里不 import near_final
# （它牵着整条管线，会把这个叶子拖进环）。
LITERARY_REVISION_RUBRIC_ID = "literary_revision_v1"
LITERARY_REVISION_PASSAGE_RUBRIC_ID = "literary_revision_passage_v1"
NEAR_FINAL_RUBRIC_ID = "near_final_acceptance_v1"

DIAGNOSIS_SOURCES: tuple[str, ...] = ("rules", "craft", "review", "ai")
SOURCE_LABELS: dict[str, str] = {
    "rules": "规则体检",
    "craft": "节奏",
    "review": "起草评审",
    "ai": "AI 深评",
}
SEVERITIES: tuple[str, ...] = ("blocking", "revision", "taste", "info")
PASSAGE_VERDICTS: tuple[str, ...] = ("holds", "partly", "does_not_hold", "no_finding")
PASSAGE_VERDICT_LABELS: dict[str, str] = {
    "holds": "成立",
    "partly": "部分成立",
    "does_not_hold": "不成立",
    "no_finding": "没有要改的",
}

# 深评（literary_revision_v1）的十维与五个镜头
AI_DIMENSION_LABELS: dict[str, str] = {
    "character_contradiction": "人物自相矛盾",
    "choice_pressure": "抉择压力",
    "relationship_tension": "关系张力",
    "dialogue_subtext": "对白潜台词",
    "information_rhythm": "信息节奏",
    "voice_distinction": "声音辨识度",
    "image_necessity": "意象必要性",
    "repetitive_expression": "表达重复",
    "ending_drive": "收束驱动",
    "theme_pressure": "主题压力",
}
LENS_LABELS: dict[str, str] = {"story": "故事", "character": "人物", "prose": "文字", "reader": "读者", "theme": "主题"}
# 准定稿评审（near_final）里确定性门产出的维度
REVIEW_DIMENSION_LABELS: dict[str, str] = {
    "model_voice_risk": "模型腔",
    "forced_choice": "被逼的选择",
    "price_paid": "付出的代价",
    "ending_action": "收尾动作",
    "structure": "结构",
    "scene_form": "场景形态",
    "source_text": "正文",
}
CRAFT_LABELS: dict[str, str] = {
    "adjacent_echo": "贴邻叠句",
    "long_paragraph": "段落偏长",
    "same_opening": "句首重复",
}
PATCH_CATEGORIES: tuple[str, ...] = (
    "dialogue_rewrite",
    "action_replace",
    "ending_pressure",
    "information_reorder",
    "de_model_voice",
    "local_patch",
)
PASSAGE_RELATION_KINDS: tuple[str, ...] = ("contradiction", "repetition", "continuity")
PASSAGE_RELATION_LABELS: dict[str, str] = {"contradiction": "矛盾", "repetition": "重复", "continuity": "承接"}


def candidate_category_for_dimension(dimension: str) -> str:
    """一条发现的维度 → 局部修补的类别（偏好画像按类别学）。规则维度与深评十维都认。"""

    value = str(dimension or "")
    if value in {"dialogue_subtext", "dialogue_edge", "relationship_tension", "expository_dialogue", "dialogue_as_report"}:
        return "dialogue_rewrite"
    if value in {"image_necessity", "repetitive_expression", "template_action_reuse", "repetitive_action", "self_repetition", "adjacent_echo", "same_opening"}:
        return "action_replace"
    if value in {"ending_drive", "summary_ending", "false_poetic_closure", "ending_action"}:
        return "ending_pressure"
    if value in {"information_rhythm", "false_clarity", "over_explained_motive", "long_paragraph"}:
        return "information_reorder"
    if value in {"model_voice", "prose_model_voice", "model_voice_risk", "image_homogeneity", "syntax_monotony", "decorative_imagery", "image_field_reuse", "perception_filter", "voice_distinction"}:
        return "de_model_voice"
    return "local_patch"

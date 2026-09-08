"""Style Reference v2 — 跨场景声音一致性（规格 §1.6 / §2.W6）。

两端：

- **写端** ``observe_style_drift(session, scene, final_text, contract)``：归档期用
  W3 的 ``compute_voice_signature_for_text`` 对成稿做**确定性**漂移读数——以冻结契约
  里 ``profile_json.voice_signature.features``（整书特征）为基线均值、以
  ``voice_baseline.yaml`` 的块级 std 为尺度（场景级读数 ``block_count=1``），再对
  ``metrics_baseline`` 的既有量化指标做同样的 z；``|z| ≥ 1.5`` 的特征按 ``|z|`` 降序取前
  5 条 ``deviations``，渲染 ≤3 行**方向性、无阿拉伯数字**的中文校准句与 few-shot 段型优先级
  ``drift_ptype_priority``，写 1 行 ``StyleReferenceMetricEvent(event_kind=
  "style_drift_observed")``（context 形状见 §1.6）。
- **读端** ``latest_drift_event`` / ``latest_drift_calibration``：下一场 bundle
  （``bundle_builder._style_drift_calibration``）读取本章更早场景最近一次事件，渲染成
  ``style_drift_calibration`` section（只对 style_draft 可见）。同章没有时退到上一章
  （与「前文声音锚」同一回退约定——归档管线目前只在章末场景触发读数，见
  ``scene_archive_checkpoint``，若不回退则校准几乎永远为空）。

全部路径 fail-open 为 no-op：无契约 / 无画像 / 画像无 ``voice_signature`` / 文本过短 /
基线文件缺失时返回 ``{"outcome": "no_op", "reason": ...}`` 且**不写事件**；读端查不到返回
``None`` / ``[]``。本模块不调用 LLM，不接触参考原文，校准句只含方向与闭类词。
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, StyleReferenceMetricEvent
from novel_system.services.style_reference.metrics_recorder import MetricsRecorder
from novel_system.services.style_reference.voice_signature import (
    LEXICAL_WINDOW_CHARS,
    compute_voice_signature_for_text,
    feature_z_scores,
    load_voice_baseline,
)

_LOGGER = logging.getLogger(__name__)

STYLE_DRIFT_EVENT_KIND = "style_drift_observed"
STYLE_DRIFT_TARGET_KIND = "scene"
# 少于这么多可见字（CJK / 字母 / 数字）的成稿不做读数——块级 std 对几百字的样本没有意义。
MIN_DRIFT_TEXT_CHARS = 300
DRIFT_Z_THRESHOLD = 1.5
MAX_DEVIATIONS = 5
MAX_CALIBRATION_LINES = 3
_Z_CLIP = 8.0
_CJK = "㐀-鿿"
_NON_VISIBLE_RE = re.compile(rf"[^A-Za-z0-9{_CJK}]+")
_DIGIT_RE = re.compile(r"[0-9]")

# 分类器标签依赖的比例指标：生成文本全部归 narration，对照无意义（见 validation.quantitative）。
_TYPE_RATIO_METRICS: frozenset[str] = frozenset(
    {
        "dialogue_ratio",
        "psychology_ratio",
        "description_env_ratio",
        "description_char_ratio",
        "action_ratio",
        "narration_ratio",
        "transition_ratio",
        "flashback_ratio",
    }
)
_DIALOGUE_GUIDE_FEATURES: tuple[str, ...] = (
    "dialogue_guide_pre_share",
    "dialogue_guide_post_share",
    "dialogue_guide_none_share",
)
_SPEECH_VERB_FEATURES: tuple[str, ...] = tuple(
    f"speech_verb_{key}_share" for key in ("shuodao", "shuo", "dao", "wen", "da", "other")
)
_PERSON_FEATURES: tuple[str, ...] = (
    "person_first_share",
    "person_second_share",
    "person_third_share",
)
_LEXICAL_FEATURES: tuple[str, ...] = ("lexical_char_ttr", "lexical_bigram_hapax_ratio")

# ---------------------------------------------------------------------------
# feature → (族, 偏高观察, 偏高校正, 偏低观察, 偏低校正)
# 「偏高 / 偏低」指本场成稿相对参考画像；校正句朝参考方向说。全部中文、无阿拉伯数字。
# ---------------------------------------------------------------------------
_FAMILY_RHYTHM = "rhythm"
_FAMILY_FUNCTION_WORDS = "function_words"
_FAMILY_PUNCTUATION = "punctuation"
_FAMILY_PARAGRAPH = "paragraph"
_FAMILY_DIALOGUE = "dialogue"
_FAMILY_LEXICAL = "lexical"
_FAMILY_PERSON = "person"

_FEATURE_GUIDE: dict[str, tuple[str, str, str, str, str]] = {
    # --- 句子节奏 ---
    "sent_len_mean": (_FAMILY_RHYTHM, "句子偏长", "回到参考的短句节奏", "句子偏短", "让句子恢复参考的长度，多用短语连缀成句"),
    "avg_sentence_length": (_FAMILY_RHYTHM, "句子偏长", "回到参考的短句节奏", "句子偏短", "让句子恢复参考的长度，多用短语连缀成句"),
    "sent_len_std": (_FAMILY_RHYTHM, "长短句起落过大", "句长回到参考的均匀节奏", "句长过于均匀", "恢复参考的长短句交错"),
    "sentence_length_std": (_FAMILY_RHYTHM, "长短句起落过大", "句长回到参考的均匀节奏", "句长过于均匀", "恢复参考的长短句交错"),
    "sent_len_p90": (_FAMILY_RHYTHM, "长句偏多", "收短最长的几句", "长句偏少", "允许几处更长的句子"),
    "long_sentence_ratio": (_FAMILY_RHYTHM, "长句偏多", "收短最长的几句", "长句偏少", "允许几处更长的句子"),
    "sent_len_p10": (_FAMILY_RHYTHM, "极短句变少", "补回参考里的极短句", "极短句偏多", "极短句收敛到参考的频率"),
    "short_sentence_ratio": (_FAMILY_RHYTHM, "短句偏多", "短句收敛到参考的比重", "短句偏少", "补回参考的短句"),
    "sent_short_run_ratio": (_FAMILY_RHYTHM, "短句连打过多", "短句连打收敛到参考的频率", "短句连打变少", "恢复参考里几个短句紧接推进的写法"),
    "sent_short_run_mean": (_FAMILY_RHYTHM, "短句连打拖得过长", "短句连打收敛到参考的长度", "短句连打变短", "恢复参考里几个短句紧接推进的写法"),
    "sent_len_lag1_autocorr": (_FAMILY_RHYTHM, "句长成段相近、缺少起落", "长句之后接短句，恢复起落", "句长逐句交替过于频繁", "让相近句长成串，少逐句交替"),
    "sent_pauses_mean": (_FAMILY_RHYTHM, "逗号停顿变多", "减少句内停顿", "逗号停顿变少", "回到密集停顿"),
    "punct_comma_per_1k": (_FAMILY_RHYTHM, "逗号停顿变多", "减少句内停顿", "逗号停顿变少", "回到密集停顿"),
    "punctuation_density_per_1k": (_FAMILY_RHYTHM, "标点偏密", "减少句内停顿", "标点偏疏", "回到参考的停顿密度"),
    "clause_len_mean": (_FAMILY_RHYTHM, "停顿之间的短语偏长", "把长短语切成参考那样的短停顿", "停顿之间的短语过短", "让短语稍长再停"),
    "punct_period_per_1k": (_FAMILY_RHYTHM, "句号偏密", "少落句号，多用逗号连缀", "句号偏疏", "多落句号，缩短句子"),
    # --- 虚词 ---
    "fw_particle_per_1k": (_FAMILY_FUNCTION_WORDS, "「的」等结构助词偏多", "少叠定语，省用「的」", "结构助词偏少", "定语层次恢复参考的密度"),
    "fw_aspect_per_1k": (_FAMILY_FUNCTION_WORDS, "「了、着」等体标记偏多", "动作少带状态尾巴", "体标记偏少", "动作恢复参考的「了、着」尾巴"),
    "fw_connective_per_1k": (_FAMILY_FUNCTION_WORDS, "连接词偏多", "少靠连接词点明关系，多用并置", "连接词偏少", "句间关系恢复参考的连接词"),
    "fw_adverb_per_1k": (_FAMILY_FUNCTION_WORDS, "副词偏多", "少加程度修饰", "副词偏少", "程度和转折恢复参考的副词点出"),
    "fw_preposition_per_1k": (_FAMILY_FUNCTION_WORDS, "介词框架偏多", "方位和对象多直接并置", "介词框架偏少", "恢复参考的介词铺展"),
    "fw_pronoun_per_1k": (_FAMILY_FUNCTION_WORDS, "代词偏多", "少用代词回指", "代词偏少", "恢复参考的代词密度"),
    "fw_modal_per_1k": (_FAMILY_FUNCTION_WORDS, "语气词偏多", "句末少带语气词", "语气词偏少", "口气松下来，句末带上参考的语气词"),
    "sentence_final_modal_ratio": (_FAMILY_FUNCTION_WORDS, "句末语气词偏多", "句末少带语气词", "句末语气词偏少", "口气松下来，句末带上参考的语气词"),
    "colloquial_marker_ratio": (_FAMILY_FUNCTION_WORDS, "口语语气词偏多", "句末少带语气词", "口语语气词偏少", "口气松下来，句末带上参考的语气词"),
    "fw_classical_per_1k": (_FAMILY_FUNCTION_WORDS, "文言虚词偏多", "白话回来，少夹文言", "文言虚词偏少", "恢复参考里夹用的文言骨架"),
    "sentence_final_classical_ratio": (_FAMILY_FUNCTION_WORDS, "文言句末语气偏多", "白话回来，少夹文言", "文言句末语气偏少", "恢复参考里夹用的文言骨架"),
    "classical_word_ratio": (_FAMILY_FUNCTION_WORDS, "文言词偏多", "白话回来，少夹文言", "文言词偏少", "恢复参考里夹用的文言骨架"),
    "fw_total_per_1k": (_FAMILY_FUNCTION_WORDS, "虚词总量偏多", "句子做减法，少用虚词", "虚词总量偏少", "虚词恢复参考的密度"),
    # --- 其它标点 ---
    "punct_enumeration_per_1k": (_FAMILY_PUNCTUATION, "顿号并举偏多", "少排列并举", "顿号偏少", "恢复参考的并举排列"),
    "punct_colon_per_1k": (_FAMILY_PUNCTUATION, "冒号偏多", "少用冒号引出", "冒号偏少", "恢复参考用冒号引出下文的写法"),
    "punct_semicolon_per_1k": (_FAMILY_PUNCTUATION, "分号偏多", "并列分句直接断开", "分号偏少", "恢复参考用分号挂并列分句"),
    "semicolon_density_per_1k": (_FAMILY_PUNCTUATION, "分号偏多", "并列分句直接断开", "分号偏少", "恢复参考用分号挂并列分句"),
    "punct_exclamation_per_1k": (_FAMILY_PUNCTUATION, "感叹号偏多", "情绪压回句里", "感叹号偏少", "恢复参考外露的感叹语气"),
    "punct_question_per_1k": (_FAMILY_PUNCTUATION, "问句偏多", "少设问反问", "问句偏少", "恢复参考的设问反问"),
    "question_density_per_1k": (_FAMILY_PUNCTUATION, "问句偏多", "少设问反问", "问句偏少", "恢复参考的设问反问"),
    "punct_ellipsis_per_1k": (_FAMILY_PUNCTUATION, "省略号偏多", "话说尽即止，少留白", "省略号偏少", "恢复参考的省略号留白"),
    "ellipsis_density_per_1k": (_FAMILY_PUNCTUATION, "省略号偏多", "话说尽即止，少留白", "省略号偏少", "恢复参考的省略号留白"),
    "punct_dash_per_1k": (_FAMILY_PUNCTUATION, "破折号偏多", "少插补语和急转", "破折号偏少", "恢复参考的破折号插入"),
    "dash_em_density_per_1k": (_FAMILY_PUNCTUATION, "破折号偏多", "少插补语和急转", "破折号偏少", "恢复参考的破折号插入"),
    # --- 段落 ---
    "para_len_mean": (_FAMILY_PARAGRAPH, "段落偏长", "多换段", "段落偏短", "让一段承载更多动作再换段"),
    "paragraph_mean_chars": (_FAMILY_PARAGRAPH, "段落偏长", "多换段", "段落偏短", "让一段承载更多动作再换段"),
    "paragraph_length_std_chars": (_FAMILY_PARAGRAPH, "段落长短起落过大", "段长回到参考的均匀节奏", "段落长度过于均匀", "恢复参考的长短段交错"),
    "paragraphs_per_1k": (_FAMILY_PARAGRAPH, "换段过频", "让一段承载更多动作再换段", "换段太少", "多换段"),
    "para_single_sentence_ratio": (_FAMILY_PARAGRAPH, "一句成段偏多", "少让单句独立成段", "一句成段偏少", "恢复参考的单句成段"),
    "single_sentence_paragraph_ratio": (_FAMILY_PARAGRAPH, "一句成段偏多", "少让单句独立成段", "一句成段偏少", "恢复参考的单句成段"),
    # --- 对白 ---
    "para_dialogue_ratio": (_FAMILY_DIALOGUE, "对白段偏多", "叙述占回参考的比重", "对白段偏少", "对白恢复参考的比重"),
    "quote_led_paragraph_ratio": (_FAMILY_DIALOGUE, "引语起头的段落偏多", "叙述占回参考的比重", "引语起头的段落偏少", "对白恢复参考的比重"),
    "punct_quote_pair_per_1k": (_FAMILY_DIALOGUE, "引语偏多", "叙述占回参考的比重", "引语偏少", "对白恢复参考的比重"),
    "dialogue_guide_pre_share": (_FAMILY_DIALOGUE, "对白引导词前置偏多", "引导词按参考挪到引语后或省去", "前置引导词偏少", "恢复参考先点出谁说再引话的写法"),
    "dialogue_guide_post_share": (_FAMILY_DIALOGUE, "对白引导词后置偏多", "引导词按参考前置或省去", "后置引导词偏少", "恢复参考话说完再补谁说的写法"),
    "dialogue_guide_none_share": (_FAMILY_DIALOGUE, "无引导词的对白偏多", "对白按参考补上引导词", "无引导词的对白偏少", "对白多省引导词，靠上下文辨认"),
    "speech_verb_shuodao_share": (_FAMILY_DIALOGUE, "引导动词偏用「说道」", "换回参考的引导动词", "「说道」用得偏少", "引导动词恢复参考的用法"),
    "speech_verb_shuo_share": (_FAMILY_DIALOGUE, "引导动词偏用「说」", "换回参考的引导动词", "「说」用得偏少", "引导动词恢复参考的用法"),
    "speech_verb_dao_share": (_FAMILY_DIALOGUE, "引导动词偏用「道」", "换回参考的引导动词", "「道」用得偏少", "引导动词恢复参考的用法"),
    "speech_verb_wen_share": (_FAMILY_DIALOGUE, "引导动词偏用「问」", "换回参考的引导动词", "「问」用得偏少", "引导动词恢复参考的用法"),
    "speech_verb_da_share": (_FAMILY_DIALOGUE, "引导动词偏用「答」", "换回参考的引导动词", "「答」用得偏少", "引导动词恢复参考的用法"),
    "speech_verb_other_share": (_FAMILY_DIALOGUE, "引导动词花样偏多", "换回参考的引导动词", "引导动词过于单一", "引导动词恢复参考的用法"),
    # --- 词汇 ---
    "four_char_segment_per_1k": (_FAMILY_LEXICAL, "四字格偏多", "不堆成语", "四字格偏少", "恢复参考的四字短语收束"),
    "redup_aa_per_1k": (_FAMILY_LEXICAL, "叠字偏多", "少用叠词", "叠字偏少", "恢复参考的叠词"),
    "redup_aabb_per_1k": (_FAMILY_LEXICAL, "叠词偏多", "少用叠词", "叠词偏少", "恢复参考的叠词"),
    "redup_abab_per_1k": (_FAMILY_LEXICAL, "叠词偏多", "少用叠词", "叠词偏少", "恢复参考的叠词"),
    "redup_total_per_1k": (_FAMILY_LEXICAL, "叠词偏多", "少用叠词", "叠词偏少", "恢复参考的叠词"),
    "lexical_char_ttr": (_FAMILY_LEXICAL, "用字过于铺张", "回到参考克制的用字", "用字重复偏多", "换字，别反复用同一批字"),
    "lexical_bigram_hapax_ratio": (_FAMILY_LEXICAL, "用词过于求新", "回到参考克制的用字", "用词重复偏多", "换字，别反复用同一批字"),
    "metaphor_density_per_1k": (_FAMILY_LEXICAL, "比喻偏多", "少设比喻", "比喻偏少", "恢复参考的比喻密度"),
    "personification_density_per_1k": (_FAMILY_LEXICAL, "拟人偏多", "少用拟人", "拟人偏少", "恢复参考的拟人写法"),
    "sensory_visual_per_1k": (_FAMILY_LEXICAL, "视觉词偏多", "少堆视觉描写", "视觉词偏少", "恢复参考的视觉描写"),
    "sensory_auditory_per_1k": (_FAMILY_LEXICAL, "听觉词偏多", "少堆声音描写", "听觉词偏少", "恢复参考的声音描写"),
    "sensory_olfactory_per_1k": (_FAMILY_LEXICAL, "嗅觉词偏多", "少写气味", "嗅觉词偏少", "恢复参考的气味描写"),
    "sensory_tactile_per_1k": (_FAMILY_LEXICAL, "触觉词偏多", "少写触感", "触觉词偏少", "恢复参考的触感描写"),
    "sensory_gustatory_per_1k": (_FAMILY_LEXICAL, "味觉词偏多", "少写滋味", "味觉词偏少", "恢复参考的滋味描写"),
    # --- 人称 ---
    "person_first_share": (_FAMILY_PERSON, "第一人称偏多", "少用「我」", "第一人称偏少", "恢复参考「我」的比重"),
    "person_second_share": (_FAMILY_PERSON, "第二人称偏多", "少用「你」的呼告", "第二人称偏少", "恢复参考「你」的呼告"),
    "person_third_share": (_FAMILY_PERSON, "第三人称偏多", "少用「他、她」回指", "第三人称偏少", "恢复参考「他、她」的比重"),
}

# 族 → few-shot 样例段型优先级（与 scene_samples_index / 启发式分类器的段型词表一致）。
_FAMILY_PTYPES: dict[str, tuple[str, ...]] = {
    _FAMILY_DIALOGUE: ("dialogue", "narration"),
    _FAMILY_RHYTHM: ("narration", "action", "description_env"),
    _FAMILY_FUNCTION_WORDS: ("narration", "psychology", "dialogue"),
    _FAMILY_PUNCTUATION: ("narration", "dialogue"),
    _FAMILY_PARAGRAPH: ("narration", "action"),
    _FAMILY_LEXICAL: ("description_env", "description_char", "narration"),
    _FAMILY_PERSON: ("psychology", "narration"),
}
_LINE_PREFIX = "前一场"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float) -> float:
    return round(float(value), 4)


def visible_char_count(text: str) -> int:
    """可见字数（CJK / 字母 / 数字），与 voice_signature 的口径一致。"""
    return len(_NON_VISIBLE_RE.sub("", str(text or "")))


def _no_op(reason: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": "no_op", "reason": reason, **extra}


def _contract_layers(contract: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """契约层按 ``order`` 升序（泛 → 具体）；坏形状退化为空。"""
    if not isinstance(contract, Mapping):
        return []
    raw_layers = contract.get("layers")
    if not isinstance(raw_layers, Sequence) or isinstance(raw_layers, (str, bytes)):
        return []
    layers = [layer for layer in raw_layers if isinstance(layer, Mapping)]

    def order_of(layer: Mapping[str, Any]) -> int:
        value = _finite(layer.get("order"))
        return int(value) if value is not None else 0

    return [dict(layer) for layer in sorted(layers, key=order_of)]


def _layer_profile_json(layer: Mapping[str, Any]) -> dict[str, Any]:
    profile = layer.get("profile")
    if not isinstance(profile, Mapping):
        return {}
    profile_json = profile.get("profile_json")
    return dict(profile_json) if isinstance(profile_json, Mapping) else {}


def _layer_voice_features(layer: Mapping[str, Any]) -> dict[str, float]:
    voice = _layer_profile_json(layer).get("voice_signature")
    if not isinstance(voice, Mapping):
        return {}
    features = voice.get("features")
    if not isinstance(features, Mapping):
        return {}
    result: dict[str, float] = {}
    for name, raw in features.items():
        value = _finite(raw)
        if value is not None:
            result[str(name)] = value
    return result


def contract_voice_reference(contract: Mapping[str, Any] | None) -> dict[str, float]:
    """契约各层 ``voice_signature.features`` 的加权均值（泛 → 具体权重 1..n）。

    与 ``runtime_contract.blend_profile_metric_baselines`` 同一口径：越具体的层权重越大。
    没有任何层带 voice_signature 时返回 ``{}``。
    """
    layers = _contract_layers(contract)
    weighted: list[tuple[float, dict[str, float]]] = []
    for index, layer in enumerate(layers):
        features = _layer_voice_features(layer)
        if features:
            weighted.append((float(index + 1), features))
    if not weighted:
        return {}
    names: set[str] = set()
    for _weight, features in weighted:
        names.update(features)
    blended: dict[str, float] = {}
    for name in sorted(names):
        total = 0.0
        weight_sum = 0.0
        for weight, features in weighted:
            if name in features:
                total += weight * features[name]
                weight_sum += weight
        if weight_sum > 0:
            blended[name] = total / weight_sum
    return blended


def contract_deliberate_repetition(contract: Mapping[str, Any] | None) -> bool:
    """任一层画像标记 ``voice_signature.deliberate_repetition=true`` → True。"""
    for layer in _contract_layers(contract):
        voice = _layer_profile_json(layer).get("voice_signature")
        if isinstance(voice, Mapping) and voice.get("deliberate_repetition") is True:
            return True
    return False


def _most_specific_layer_ids(layers: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    for layer in reversed(list(layers)):
        profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
        binding = layer.get("binding") if isinstance(layer.get("binding"), Mapping) else {}
        profile_id = str(profile.get("profile_id") or "") or None
        binding_id = str(binding.get("binding_id") or "") or None
        if profile_id or binding_id:
            return profile_id, binding_id
    return None, None


def _z_score(value: float, mean: float, std: float) -> float:
    floor = max(1e-6, 0.05 * abs(mean))
    effective_std = max(std, floor)
    z = (value - mean) / effective_std
    return max(-_Z_CLIP, min(_Z_CLIP, z))


def _excluded_voice_features(
    signature: Mapping[str, Any],
    reference: Mapping[str, float],
) -> set[str]:
    """本场或参考上退化为全零的特征组不参与漂移判定（否则 0 对 0.5 会被误判为漂移）。"""
    excluded: set[str] = set()
    features = signature.get("features") if isinstance(signature.get("features"), Mapping) else {}
    stats = signature.get("stats") if isinstance(signature.get("stats"), Mapping) else {}

    def group_is_degenerate(names: Sequence[str]) -> bool:
        scene_total = sum(abs(_finite(features.get(name)) or 0.0) for name in names)
        reference_total = sum(abs(reference.get(name, 0.0)) for name in names)
        return scene_total <= 0.0 or reference_total <= 0.0

    quote_count = _finite(stats.get("quote_count")) or 0.0
    if quote_count <= 0 or group_is_degenerate(_DIALOGUE_GUIDE_FEATURES):
        excluded.update(_DIALOGUE_GUIDE_FEATURES)
        excluded.update(_SPEECH_VERB_FEATURES)
    elif group_is_degenerate(_SPEECH_VERB_FEATURES):
        excluded.update(_SPEECH_VERB_FEATURES)
    if group_is_degenerate(_PERSON_FEATURES):
        excluded.update(_PERSON_FEATURES)
    char_count = _finite(stats.get("char_count")) or 0.0
    if char_count < LEXICAL_WINDOW_CHARS:
        # 字级 TTR / 二元组 hapax 在不足一个词汇窗口的文本上系统性偏高，不可对照。
        excluded.update(_LEXICAL_FEATURES)
    return excluded


# ---------------------------------------------------------------------------
# 校准句与段型优先级
# ---------------------------------------------------------------------------


def render_calibration_lines(
    deviations: Sequence[Mapping[str, Any]],
    *,
    max_lines: int = MAX_CALIBRATION_LINES,
) -> list[str]:
    """把 deviations 按特征族合成 ≤``max_lines`` 行方向性校准句（无阿拉伯数字）。

    形如「前一场句子偏长、逗号停顿变少：回到短句与密集停顿」；同族多条偏离合并到一行，
    观察与校正短语各自去重；不在映射表里的特征跳过。
    """
    grouped: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    for item in deviations:
        if not isinstance(item, Mapping):
            continue
        guide = _FEATURE_GUIDE.get(str(item.get("feature") or ""))
        if guide is None:
            continue
        family, high_obs, high_fix, low_obs, low_fix = guide
        direction = str(item.get("direction") or "")
        if direction == "high":
            observation, fix = high_obs, high_fix
        elif direction == "low":
            observation, fix = low_obs, low_fix
        else:
            continue
        bucket = grouped.setdefault(family, {"obs": [], "fix": []})
        if family not in order:
            order.append(family)
        if observation not in bucket["obs"]:
            bucket["obs"].append(observation)
        if fix not in bucket["fix"]:
            bucket["fix"].append(fix)
    lines: list[str] = []
    for family in order[:max(0, int(max_lines))]:
        bucket = grouped[family]
        observations = "、".join(bucket["obs"][:3])
        fixes = "，".join(bucket["fix"][:2])
        line = f"{_LINE_PREFIX}{observations}：{fixes}"
        if _DIGIT_RE.search(line):  # 映射表全中文；防御性兜底
            line = _DIGIT_RE.sub("", line)
        lines.append(line)
    return lines


def drift_ptype_priority(deviations: Sequence[Mapping[str, Any]]) -> list[str]:
    """按偏离幅度给 few-shot 样例段型排序（对白偏离 → dialogue 优先，等）。"""
    scores: dict[str, float] = {}
    for item in deviations:
        if not isinstance(item, Mapping):
            continue
        guide = _FEATURE_GUIDE.get(str(item.get("feature") or ""))
        if guide is None:
            continue
        preferred = _FAMILY_PTYPES.get(guide[0], ())
        weight = abs(_finite(item.get("z")) or 0.0)
        for rank, ptype in enumerate(preferred):
            scores[ptype] = scores.get(ptype, 0.0) + weight * (len(preferred) - rank)
    return sorted(scores, key=lambda ptype: (-scores[ptype], ptype))


# ---------------------------------------------------------------------------
# 写端
# ---------------------------------------------------------------------------


def _metric_baseline_z(
    text: str,
    contract: Mapping[str, Any],
) -> dict[str, dict[str, float]]:
    """既有量化指标（metrics_baseline）的 z；坏契约 / 缺基线退化为空。"""
    try:
        from novel_system.services.style_reference.runtime_contract import (
            blend_profile_metric_baselines,
            contract_profile_objects,
        )
        from novel_system.services.style_reference.validation.quantitative import (
            compute_generated_metrics,
        )

        blended = blend_profile_metric_baselines(contract_profile_objects(contract))
        if not blended:
            return {}
        generated = compute_generated_metrics(text)
    except Exception:  # noqa: BLE001 — 兼容层，失败只影响附加读数
        _LOGGER.debug("metrics baseline drift reading skipped", exc_info=True)
        return {}
    result: dict[str, dict[str, float]] = {}
    for name, stats in blended.items():
        if name in _TYPE_RATIO_METRICS or name not in generated:
            continue
        mean = _finite(stats.get("mean")) if isinstance(stats, Mapping) else None
        std = _finite(stats.get("std")) if isinstance(stats, Mapping) else None
        value = _finite(generated.get(name))
        if mean is None or value is None:
            continue
        result[str(name)] = {
            "value": value,
            "baseline_mean": mean,
            "baseline_std": max(0.0, std or 0.0),
            "z": _z_score(value, mean, max(0.0, std or 0.0)),
        }
    return result


def observe_style_drift(
    session: Session,
    scene: Any,
    final_text: str,
    contract: Mapping[str, Any] | None,
    *,
    record_event: bool = True,
) -> dict[str, Any]:
    """归档期确定性漂移读数（规格 §1.6）。

    返回：
    - ``{"outcome": "observed", "event_id", "profile_id", "deviations",
      "calibration_lines", "drift_ptype_priority", "feature_count",
      "deviation_count", "text_chars", "scene_seq"}``——已写 MetricEvent；
    - ``{"outcome": "no_op", "reason": ...}``——无契约 / 无画像 / 无 voice_signature /
      文本不足 ``MIN_DRIFT_TEXT_CHARS`` 可见字 / 没有任何可对照基线，不写事件；
    - ``{"outcome": "degraded", "error_code": ...}``——事件写入失败（MetricsRecorder 已吞异常）。
    不抛业务异常之外的错误由调用方（scene_archive_effects）兜底。
    """
    started = time.perf_counter()
    text = str(final_text or "")
    text_chars = visible_char_count(text)
    scene_id = str(getattr(scene, "scene_id", "") or "")
    chapter_id = str(getattr(scene, "chapter_id", "") or "")
    scene_seq_raw = _finite(getattr(scene, "scene_seq", None))
    scene_seq = int(scene_seq_raw) if scene_seq_raw is not None else None

    if text_chars < MIN_DRIFT_TEXT_CHARS:
        return _no_op("text_too_short", text_chars=text_chars)
    if not isinstance(contract, Mapping):
        return _no_op("no_contract", text_chars=text_chars)
    layers = _contract_layers(contract)
    if not layers:
        return _no_op("no_profile", text_chars=text_chars)
    reference = contract_voice_reference(contract)
    if not reference:
        return _no_op("no_voice_signature", text_chars=text_chars)
    profile_id, binding_id = _most_specific_layer_ids(layers)

    baseline = load_voice_baseline()
    baseline_features = baseline.get("features") if isinstance(baseline, Mapping) else None
    readings: dict[str, dict[str, float]] = {}
    if isinstance(baseline_features, Mapping) and baseline_features:
        signature = compute_voice_signature_for_text(text, baseline=baseline)
        scene_features = signature.get("features") if isinstance(signature.get("features"), Mapping) else {}
        excluded = _excluded_voice_features(signature, reference)
        comparable: dict[str, dict[str, float]] = {}
        for name, mean in reference.items():
            if name in excluded or name not in scene_features:
                continue
            entry = baseline_features.get(name)
            std = _finite(entry.get("std")) if isinstance(entry, Mapping) else None
            if std is None:
                continue
            comparable[name] = {"mean": mean, "std": max(0.0, std)}
        z_scores = feature_z_scores(
            {name: scene_features[name] for name in comparable},
            comparable,
            block_count=1,
        )
        for name, z in z_scores.items():
            readings[name] = {
                "value": float(scene_features[name]),
                "baseline_mean": comparable[name]["mean"],
                "baseline_std": comparable[name]["std"],
                "z": float(z),
            }
    else:
        _LOGGER.warning("voice_baseline.yaml missing; style drift reading falls back to metrics_baseline only")

    for name, reading in _metric_baseline_z(text, contract).items():
        readings.setdefault(name, reading)
    if not readings:
        return _no_op("no_baseline", text_chars=text_chars)

    deviations = [
        {
            "feature": name,
            "direction": "high" if reading["z"] > 0 else "low",
            "z": _round(reading["z"]),
        }
        for name, reading in readings.items()
        if abs(reading["z"]) >= DRIFT_Z_THRESHOLD
    ]
    deviations.sort(key=lambda item: (-abs(item["z"]), item["feature"]))
    deviations = deviations[:MAX_DEVIATIONS]
    calibration_lines = render_calibration_lines(deviations)
    ptype_priority = drift_ptype_priority(deviations)

    context = {
        "chapter_id": chapter_id,
        "scene_seq": scene_seq,
        "features": {
            name: {key: _round(value) for key, value in reading.items()}
            for name, reading in sorted(readings.items())
        },
        "deviations": deviations,
        "calibration_lines": calibration_lines,
        "drift_ptype_priority": ptype_priority,
        "text_chars": text_chars,
        "z_threshold": DRIFT_Z_THRESHOLD,
        "contract_hash": str(contract.get("contract_hash") or "") or None,
        "layer_profile_ids": [
            str((layer.get("profile") or {}).get("profile_id") or "")
            for layer in layers
            if isinstance(layer.get("profile"), Mapping)
        ],
    }
    result: dict[str, Any] = {
        "outcome": "observed",
        "event_id": None,
        "profile_id": profile_id,
        "deviations": deviations,
        "calibration_lines": calibration_lines,
        "drift_ptype_priority": ptype_priority,
        "feature_count": len(readings),
        "deviation_count": len(deviations),
        "text_chars": text_chars,
        "scene_seq": scene_seq,
    }
    if not record_event:
        return result
    event_id = MetricsRecorder.record(
        session,
        STYLE_DRIFT_EVENT_KIND,
        target_kind=STYLE_DRIFT_TARGET_KIND,
        target_ref_id=scene_id or None,
        profile_id=profile_id,
        binding_id=binding_id,
        outcome="drift" if deviations else "in_band",
        latency_ms=int((time.perf_counter() - started) * 1000),
        context=context,
    )
    if event_id is None:
        return {
            "outcome": "degraded",
            "error_code": "STYLE_DRIFT_EVENT_WRITE_FAILED",
            "profile_id": profile_id,
            "deviation_count": len(deviations),
            "text_chars": text_chars,
        }
    result["event_id"] = event_id
    return result


# ---------------------------------------------------------------------------
# 读端
# ---------------------------------------------------------------------------


def _event_scene_seq(session: Session, event: StyleReferenceMetricEvent) -> int | None:
    context = event.context_json if isinstance(event.context_json, Mapping) else {}
    seq = _finite(context.get("scene_seq"))
    if seq is not None:
        return int(seq)
    if event.target_ref_id:
        scene = session.get(SceneCard, event.target_ref_id)
        if scene is not None and scene.scene_seq is not None:
            return int(scene.scene_seq)
    return None


def _event_view(event: StyleReferenceMetricEvent, *, source_scope: str) -> dict[str, Any]:
    context = event.context_json if isinstance(event.context_json, Mapping) else {}
    raw_lines = context.get("calibration_lines")
    lines = [
        " ".join(str(line or "").split())
        for line in (raw_lines if isinstance(raw_lines, Sequence) and not isinstance(raw_lines, str) else [])
    ]
    raw_priority = context.get("drift_ptype_priority")
    priority = [
        str(ptype)
        for ptype in (raw_priority if isinstance(raw_priority, Sequence) and not isinstance(raw_priority, str) else [])
        if str(ptype or "").strip()
    ]
    seq = _finite(context.get("scene_seq"))
    return {
        "event_id": event.event_id,
        "scene_id": event.target_ref_id,
        "chapter_id": str(context.get("chapter_id") or ""),
        "scene_seq": int(seq) if seq is not None else None,
        "profile_id": event.profile_id,
        "created_at": event.created_at,
        "calibration_lines": [line for line in lines if line],
        "drift_ptype_priority": priority,
        "source_scope": source_scope,
    }


def _latest_event_in_chapter(
    session: Session,
    chapter_id: str,
    before_scene_seq: int | None,
) -> StyleReferenceMetricEvent | None:
    stmt = (
        select(StyleReferenceMetricEvent)
        .where(
            StyleReferenceMetricEvent.event_kind == STYLE_DRIFT_EVENT_KIND,
            StyleReferenceMetricEvent.target_kind == STYLE_DRIFT_TARGET_KIND,
            StyleReferenceMetricEvent.context_json["chapter_id"].as_string() == chapter_id,
        )
        .order_by(
            StyleReferenceMetricEvent.created_at.desc(),
            StyleReferenceMetricEvent.event_id.desc(),
        )
    )
    for event in session.scalars(stmt):
        if before_scene_seq is not None:
            seq = _event_scene_seq(session, event)
            if seq is None or seq >= int(before_scene_seq):
                continue
        if event.target_ref_id:
            scene = session.get(SceneCard, event.target_ref_id)
            if scene is not None and int(scene.trashed_flag or 0) != 0:
                continue
        return event
    return None


def _previous_chapter_id(session: Session, chapter_id: str) -> str | None:
    current = session.get(ChapterGoal, chapter_id)
    if current is None:
        return None
    if current.display_order is not None:
        stmt = (
            select(ChapterGoal.chapter_id)
            .where(
                ChapterGoal.project_id == current.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.display_order < current.display_order,
            )
            .order_by(ChapterGoal.display_order.desc())
        )
    else:
        stmt = (
            select(ChapterGoal.chapter_id)
            .where(
                ChapterGoal.project_id == current.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.chapter_id < chapter_id,
            )
            .order_by(ChapterGoal.chapter_id.desc())
        )
    return session.execute(stmt).scalars().first()


def latest_drift_event(
    session: Session,
    chapter_id: str,
    before_scene_seq: int | None,
    *,
    fallback_previous_chapter: bool = True,
) -> dict[str, Any] | None:
    """本章 ``scene_seq < before_scene_seq`` 最近一场的 ``style_drift_observed`` 事件。

    返回 ``{"event_id", "scene_id", "chapter_id", "scene_seq", "profile_id", "created_at",
    "calibration_lines", "drift_ptype_priority", "source_scope"}``；``source_scope`` 为
    ``"chapter"``（同章）或 ``"previous_chapter"``（同章没有、退到上一章最近一次，与
    「前文声音锚」同一回退约定）。找不到 → ``None``。``before_scene_seq`` 为 None 时不限 seq。
    """
    if not chapter_id:
        return None
    event = _latest_event_in_chapter(session, str(chapter_id), before_scene_seq)
    if event is not None:
        return _event_view(event, source_scope="chapter")
    if not fallback_previous_chapter:
        return None
    previous = _previous_chapter_id(session, str(chapter_id))
    if not previous:
        return None
    event = _latest_event_in_chapter(session, previous, None)
    if event is None:
        return None
    return _event_view(event, source_scope="previous_chapter")


def latest_drift_calibration(
    session: Session,
    chapter_id: str,
    before_scene_seq: int | None,
) -> list[str]:
    """W5 约定的读取端：返回 :func:`latest_drift_event` 的 ``calibration_lines``（list[str]）。

    调用方（bundle_builder）负责按 ``drift_calibration_max_lines`` 截行并登记 section；
    返回空时不登记。
    """
    event = latest_drift_event(session, chapter_id, before_scene_seq)
    if event is None:
        return []
    return list(event.get("calibration_lines") or [])


__all__ = [
    "DRIFT_Z_THRESHOLD",
    "MAX_CALIBRATION_LINES",
    "MAX_DEVIATIONS",
    "MIN_DRIFT_TEXT_CHARS",
    "STYLE_DRIFT_EVENT_KIND",
    "STYLE_DRIFT_TARGET_KIND",
    "contract_deliberate_repetition",
    "contract_voice_reference",
    "drift_ptype_priority",
    "latest_drift_calibration",
    "latest_drift_event",
    "observe_style_drift",
    "render_calibration_lines",
    "visible_char_count",
]

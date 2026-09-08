"""Content-restrained style signatures for StyleReference RAG.

The signature intentionally excludes nouns, named entities, topic words, raw
character n-grams and embeddings of source prose.  It keeps only bounded,
explainable shape/rhythm/function-word features.  Raw text remains payload for
the final prompt, never the positive retrieval key.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Mapping

from novel_system.services.style_reference.text_utils import split_sentences


STYLE_SIGNATURE_VERSION = "zh_content_restrained_style_signature_v2"
_BINS = 8
_SEARCH_CODEPOINT_BASE = 0xE000
_HAS_VISIBLE_CHAR = re.compile(r"[\w㐀-鿿]")
_TRAILING_CLOSERS = "”’」』\"'）)】〕］]"

_PUNCTUATION = {
    "comma": "，,",
    "period": "。.",
    "semicolon": "；;",
    "colon": "：:",
    "question": "？?",
    "exclamation": "！!",
    "enumeration": "、",
    "quote": "“”‘’「」『』\"'",
    "parenthesis": "（）()【】",
    "dash": "—",
    "ellipsis": "…",
}
_FUNCTION_GROUPS = {
    "particle": ("的", "地", "得", "所"),
    "aspect": ("了", "着", "过", "将", "把", "被"),
    "connective": (
        "而",
        "但",
        "却",
        "便",
        "就",
        "又",
        "仍",
        "于是",
        "所以",
        "因为",
        "虽然",
        "然而",
        "可是",
    ),
    "modal": ("呢", "吧", "啊", "吗", "么", "呀", "罢"),
    "classical": (
        "之",
        "乎",
        "者",
        "也",
        "焉",
        "矣",
        "其",
        "于",
        "乃",
        "故",
        "且",
        "若",
    ),
}
_PRONOUN_GROUPS = {
    "first": ("我", "我们"),
    "second": ("你", "你们"),
    "third": ("他", "她", "它", "他们", "她们", "它们"),
}

FEATURE_NAMES: tuple[str, ...] = (
    "shape.unit_length",
    "shape.sentence_mean",
    "shape.sentence_std",
    "shape.sentence_q25",
    "shape.sentence_median",
    "shape.sentence_q75",
    "shape.short_sentence_ratio",
    "shape.long_sentence_ratio",
    "shape.sentence_density",
    "shape.paragraph_mean",
    "shape.paragraph_std",
    "shape.paragraph_density",
    "shape.single_sentence_paragraph_ratio",
    "shape.dialogue_paragraph_ratio",
    "punct.density",
    *tuple(f"punct.{name}_share" for name in _PUNCTUATION),
    *tuple(f"function.{name}_density" for name in _FUNCTION_GROUPS),
    *tuple(f"pronoun.{name}_density" for name in _PRONOUN_GROUPS),
    "script.cjk_ratio",
    "script.latin_ratio",
    "script.digit_ratio",
)

_GROUP_WEIGHTS = {
    "shape": 0.35,
    "punct": 0.30,
    "function": 0.20,
    "pronoun": 0.10,
    "script": 0.05,
}
_UNIT_LENGTH_SCALE = {"sentence": 80.0, "paragraph": 400.0, "scene": 800.0}


def _clip01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _visible_length(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text or ""))


def _quantile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _looks_like_dialogue(paragraph: str) -> bool:
    stripped = paragraph.lstrip()
    return bool(
        stripped.startswith(("“", "‘", "「", "『", '"', "'"))
        or sum(stripped.count(char) for char in "“”‘’「」『』") >= 2
    )


def _marker_density(
    text: str, markers: tuple[str, ...], visible_chars: int, scale: float
) -> float:
    count = sum(text.count(marker) for marker in markers)
    return _clip01((count / max(1, visible_chars)) * scale)


@dataclass(frozen=True, slots=True)
class StyleSignature:
    granularity: str
    features: Mapping[str, float]
    version: str = STYLE_SIGNATURE_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "granularity": self.granularity,
                "features": {
                    name: round(float(self.features[name]), 8) for name in FEATURE_NAMES
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def search_text(self) -> str:
        """Encode nearby numeric bins as private-use characters for ANN shortlist.

        The generic vector stores only see these style-bin tokens.  They never see
        source prose or query topic words.  Exact continuous distance is recomputed
        after the shortlist, so this representation is only a recall accelerator.
        """
        tokens: list[str] = []
        for feature_index, name in enumerate(FEATURE_NAMES):
            value = _clip01(float(self.features.get(name, 0.0)))
            bucket = min(_BINS - 1, int(value * _BINS))
            for neighbor in range(max(0, bucket - 1), min(_BINS - 1, bucket + 1) + 1):
                tokens.append(
                    chr(_SEARCH_CODEPOINT_BASE + feature_index * _BINS + neighbor)
                )
        return "".join(tokens)


def extract_style_signature(text: str, *, granularity: str) -> StyleSignature:
    if granularity not in _UNIT_LENGTH_SCALE:
        raise ValueError(f"unsupported style signature granularity: {granularity}")
    normalized = str(text or "").strip()
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n|\r?\n", normalized) if part.strip()
    ]
    if not paragraphs:
        paragraphs = [normalized] if normalized else [""]
    sentences = [
        sentence.strip() for sentence in split_sentences(normalized) if sentence.strip()
    ]
    if not sentences and normalized:
        sentences = [normalized]
    sentence_lengths = [_visible_length(sentence) for sentence in sentences] or [0]
    paragraph_lengths = [_visible_length(paragraph) for paragraph in paragraphs] or [0]
    visible_chars = max(1, _visible_length(normalized))

    punctuation_counts = {
        name: sum(normalized.count(char) for char in set(chars))
        for name, chars in _PUNCTUATION.items()
    }
    punctuation_total = sum(punctuation_counts.values())
    features: dict[str, float] = {
        "shape.unit_length": _clip01(visible_chars / _UNIT_LENGTH_SCALE[granularity]),
        "shape.sentence_mean": _clip01(statistics.fmean(sentence_lengths) / 80.0),
        "shape.sentence_std": _clip01(
            (statistics.pstdev(sentence_lengths) if len(sentence_lengths) > 1 else 0.0)
            / 40.0
        ),
        "shape.sentence_q25": _clip01(_quantile(sentence_lengths, 0.25) / 80.0),
        "shape.sentence_median": _clip01(_quantile(sentence_lengths, 0.5) / 80.0),
        "shape.sentence_q75": _clip01(_quantile(sentence_lengths, 0.75) / 80.0),
        "shape.short_sentence_ratio": sum(
            1 for length in sentence_lengths if length <= 10
        )
        / len(sentence_lengths),
        "shape.long_sentence_ratio": sum(
            1 for length in sentence_lengths if length >= 30
        )
        / len(sentence_lengths),
        "shape.sentence_density": _clip01(len(sentences) * 20.0 / visible_chars),
        "shape.paragraph_mean": _clip01(statistics.fmean(paragraph_lengths) / 500.0),
        "shape.paragraph_std": _clip01(
            (
                statistics.pstdev(paragraph_lengths)
                if len(paragraph_lengths) > 1
                else 0.0
            )
            / 300.0
        ),
        "shape.paragraph_density": _clip01(len(paragraphs) * 500.0 / visible_chars),
        "shape.single_sentence_paragraph_ratio": sum(
            1 for paragraph in paragraphs if len(split_sentences(paragraph)) <= 1
        )
        / len(paragraphs),
        "shape.dialogue_paragraph_ratio": sum(
            1 for paragraph in paragraphs if _looks_like_dialogue(paragraph)
        )
        / len(paragraphs),
        "punct.density": _clip01(punctuation_total * 5.0 / visible_chars),
        "script.cjk_ratio": _clip01(
            sum(1 for char in normalized if "\u3400" <= char <= "\u9fff")
            / visible_chars
        ),
        "script.latin_ratio": _clip01(
            sum(1 for char in normalized if char.isascii() and char.isalpha())
            / visible_chars
        ),
        "script.digit_ratio": _clip01(
            sum(1 for char in normalized if char.isdigit()) / visible_chars
        ),
    }
    for name, count in punctuation_counts.items():
        features[f"punct.{name}_share"] = count / max(1, punctuation_total)
    for name, markers in _FUNCTION_GROUPS.items():
        features[f"function.{name}_density"] = _marker_density(
            normalized, markers, visible_chars, 10.0
        )
    for name, markers in _PRONOUN_GROUPS.items():
        features[f"pronoun.{name}_density"] = _marker_density(
            normalized, markers, visible_chars, 20.0
        )
    return StyleSignature(
        granularity=granularity,
        features={
            name: _clip01(float(features.get(name, 0.0))) for name in FEATURE_NAMES
        },
    )


def parse_style_signature(payload: str | Mapping[str, Any]) -> StyleSignature | None:
    try:
        raw = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if raw.get("version") != STYLE_SIGNATURE_VERSION:
        return None
    granularity = str(raw.get("granularity") or "")
    raw_features = raw.get("features")
    if granularity not in _UNIT_LENGTH_SCALE or not isinstance(raw_features, Mapping):
        return None
    features: dict[str, float] = {}
    for name in FEATURE_NAMES:
        try:
            value = float(raw_features[name])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        features[name] = _clip01(value)
    return StyleSignature(granularity=granularity, features=features)


def style_signature_similarity(left: StyleSignature, right: StyleSignature) -> float:
    if left.granularity != right.granularity:
        return 0.0
    group_distances: dict[str, list[float]] = {}
    for name in FEATURE_NAMES:
        group = name.split(".", 1)[0]
        group_distances.setdefault(group, []).append(
            abs(float(left.features[name]) - float(right.features[name]))
        )
    weighted_distance = 0.0
    weight_sum = 0.0
    for group, distances in group_distances.items():
        weight = _GROUP_WEIGHTS.get(group, 0.0)
        if not distances or weight <= 0:
            continue
        weighted_distance += weight * statistics.fmean(distances)
        weight_sum += weight
    if weight_sum <= 0:
        return 0.0
    return _clip01(math.exp(-3.0 * (weighted_distance / weight_sum)))


def content_shingle_overlap(left: str, right: str, *, width: int = 2) -> float:
    """Topic/content overlap diagnostic used only as a negative safety penalty."""

    def _shingles(text: str) -> set[str]:
        normalized = "".join(
            char.lower()
            for char in str(text or "")
            if char.isalnum() or "\u3400" <= char <= "\u9fff"
        )
        if len(normalized) < width:
            return {normalized} if normalized else set()
        return {
            normalized[index : index + width]
            for index in range(len(normalized) - width + 1)
        }

    left_items = _shingles(left)
    right_items = _shingles(right)
    if not left_items or not right_items:
        return 0.0
    return len(left_items & right_items) / len(left_items | right_items)


def query_view_for_granularity(
    query_text: str,
    granularity: str,
    *,
    paragraph_chars: int = 400,
    scene_chars: int = 800,
) -> str:
    normalized = str(query_text or "").strip()
    if not normalized:
        return ""
    if granularity == "sentence":
        # 分句复用 text_utils.split_sentences(W2 统一修闭引号);丢弃无可见字符的残片
        # (闭引号 / 纯标点),再从原文取最后一句起始到结尾——保留句末标点与闭引号,
        # 不再把孤立的「”」当成最后一句。
        sentences = [
            sentence
            for sentence in split_sentences(normalized)
            if _HAS_VISIBLE_CHAR.search(sentence)
        ]
        if not sentences:
            return normalized[-80:]
        last = sentences[-1]
        # split_sentences 会剥掉句末标点、并把闭引号归并进片段,片段未必是原文子串;
        # 用去掉尾部闭引号的「句核」定位起点,再取原文到结尾。
        core = last.rstrip(_TRAILING_CLOSERS) or last
        tail_start = normalized.rfind(core)
        if tail_start < 0:
            return last
        return normalized[tail_start:].strip()
    if granularity == "paragraph":
        paragraphs = [
            part.strip()
            for part in re.split(r"\n\s*\n|\r?\n", normalized)
            if part.strip()
        ]
        return (paragraphs[-1] if paragraphs else normalized)[
            -max(1, paragraph_chars) :
        ]
    if granularity == "scene":
        return normalized[-max(1, scene_chars) :]
    raise ValueError(f"unsupported style signature granularity: {granularity}")


__all__ = [
    "FEATURE_NAMES",
    "STYLE_SIGNATURE_VERSION",
    "StyleSignature",
    "content_shingle_overlap",
    "extract_style_signature",
    "parse_style_signature",
    "query_view_for_granularity",
    "style_signature_similarity",
]

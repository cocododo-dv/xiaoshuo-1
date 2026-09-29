"""4 layers × 16 sub-dimensions 分层维度常量。

依据《风格参考模块重构执行手册 v1.1》§3 dimensions.py 与 §6.5 抽取层划分。
PR-2 之后的 ingest / extractor / synthesizer 与前端 DimensionMatrix 均消费本模块。
"""

from __future__ import annotations

from enum import Enum


class Layer(str, Enum):
    LANGUAGE = "language"
    NARRATIVE = "narrative"
    SCENE = "scene"
    THEME = "theme"


class SubDimension(str, Enum):
    LANGUAGE_SENTENCE_STRUCTURE = "language.sentence_structure"
    LANGUAGE_VOCABULARY = "language.vocabulary"
    LANGUAGE_RHETORIC = "language.rhetoric"
    LANGUAGE_PUNCTUATION = "language.punctuation"

    NARRATIVE_PERSPECTIVE = "narrative.perspective"
    NARRATIVE_PACING = "narrative.pacing"
    NARRATIVE_TIME_HANDLING = "narrative.time_handling"
    NARRATIVE_INFORMATION_DENSITY = "narrative.information_density"

    SCENE_ENVIRONMENT = "scene.environment"
    SCENE_CHARACTER_PORTRAYAL = "scene.character_portrayal"
    SCENE_DIALOGUE = "scene.dialogue"
    SCENE_SENSORY_PRIORITY = "scene.sensory_priority"

    THEME_EMOTIONAL_TONE = "theme.emotional_tone"
    THEME_VALUES = "theme.values"
    THEME_MOTIFS = "theme.motifs"
    THEME_NARRATIVE_PHILOSOPHY = "theme.narrative_philosophy"


LAYER_TO_SUB_DIMS: dict[Layer, list[SubDimension]] = {
    Layer.LANGUAGE: [
        SubDimension.LANGUAGE_SENTENCE_STRUCTURE,
        SubDimension.LANGUAGE_VOCABULARY,
        SubDimension.LANGUAGE_RHETORIC,
        SubDimension.LANGUAGE_PUNCTUATION,
    ],
    Layer.NARRATIVE: [
        SubDimension.NARRATIVE_PERSPECTIVE,
        SubDimension.NARRATIVE_PACING,
        SubDimension.NARRATIVE_TIME_HANDLING,
        SubDimension.NARRATIVE_INFORMATION_DENSITY,
    ],
    Layer.SCENE: [
        SubDimension.SCENE_ENVIRONMENT,
        SubDimension.SCENE_CHARACTER_PORTRAYAL,
        SubDimension.SCENE_DIALOGUE,
        SubDimension.SCENE_SENSORY_PRIORITY,
    ],
    Layer.THEME: [
        SubDimension.THEME_EMOTIONAL_TONE,
        SubDimension.THEME_VALUES,
        SubDimension.THEME_MOTIFS,
        SubDimension.THEME_NARRATIVE_PHILOSOPHY,
    ],
}

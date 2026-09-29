"""候选稿的两把尺子（Best-of-N / 终选页）：对抗维度的诊断排名分，与一组候选的指纹离散度。"""

from __future__ import annotations

from novel_system.services.literary_quality.dimensions import DIMENSION_WEIGHTS
from novel_system.services.literary_quality.fingerprint import fingerprint_literary_quality
from novel_system.services.literary_quality.rules import analyze_literary_quality
from novel_system.services.literary_quality.scoring import automated_diagnostic_assessment


ADVERSARIAL_DIMS: tuple[str, ...] = (
    "model_voice",
    "false_clarity",
    "over_explained_motive",
    "template_action_reuse",
    "syntax_monotony",
    "repetitive_action",
    "image_homogeneity",
    "image_field_reuse",
    "decorative_imagery",
    "false_poetic_closure",
    "expository_dialogue",
    "dialogue_as_report",
    "perception_filter",
    "self_repetition",
    # §4/§8: structural cost & conflict dimensions
    "painless_scene",
    "no_choice_scene",
    "choice_pressure",
    "conflict_too_clean",
)


def adversarial_rank_score(
    text: str,
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Diagnostic floor score across adversarial dimensions.

    Higher means fewer *known deterministic failure signals*; it does not mean
    better literature.  The score is evidence-adjusted and capped below 1.0 so
    a keyword/action-word bundle cannot masquerade as a human upper-bound
    judgment.  Candidate terminal selection remains a human decision.
    """
    if not text or not text.strip():
        return 0.0
    effective = weights if weights is not None else DIMENSION_WEIGHTS
    signals, _ = analyze_literary_quality(text)
    total_w = sum(effective.get(d, 0.0) for d in ADVERSARIAL_DIMS)
    raw_score = 1.0 if total_w <= 0.0 else round(
        sum(signals.get(d, {}).get("score", 1.0) * effective.get(d, 0.0) for d in ADVERSARIAL_DIMS) / total_w,
        4,
    )
    return automated_diagnostic_assessment(
        text,
        raw_diagnostic_score=raw_score,
    )["score"]


def candidate_dispersion(texts: list[str]) -> float:
    """Average pairwise Jaccard distance between fingerprint token sets. 0 = identical."""
    if len(texts) < 2:
        return 0.0
    fingerprints: list[set[str]] = []
    for text in texts:
        fp = fingerprint_literary_quality(text)
        tokens: set[str] = set()
        for key in ("action_templates", "image_fields", "syntax_shapes"):
            for row in fp.get(key, []):
                tokens.add(f"{key}:{row.get('value', '')}")
        fingerprints.append(tokens)
    distances: list[float] = []
    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            union = fingerprints[i] | fingerprints[j]
            intersection = fingerprints[i] & fingerprints[j]
            distances.append(1.0 - (len(intersection) / len(union)) if union else 0.0)
    return round(sum(distances) / len(distances), 4) if distances else 0.0

"""场景长度策略：场景卡的计划长度带 + 作者手笔直起时的放宽 + 参考作者的场尺度，以及由它们写成的长度指引。

2026-09-12 风格直起：style_first 下场景卡的数字长度带两侧各放宽 ``style_first_length_slack``（作者自己的场景尺度
优先于系统的长度带，越界才触发长度补丁）；概述场（``rendering_mode=summary``）与 neutral_first 不放宽。
2026-09-22 结构跟随参考书：bundle 冻结的「参考作者一场多长」（bundle_builder 按参考章长 ÷ 本章场数推算，
``inline_digests["_style_reference_scene_scale"]``）只在放宽生效时随行——硬范围上限抬到这个尺度 × (1 + slack)
（封顶 ceiling），长度指引把它说成写作目标。

放宽比例与参考尺度以前放在两个上下文变量里：几处公共入口设、约 20 处解析长度带的地方隐式读，忘了进上下文的调用方
会悄悄拿到没放宽的带。现在每个入口按 (bundle, 场景) 建一个 :class:`LengthPolicy`，显式传给验收、长度指引与补丁。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from novel_system.services.scene_form import rendering_mode
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference.config_loader import load_yaml_config

_STYLE_FIRST_LENGTH_SLACK_DEFAULT = 0.5
_REFERENCE_SCENE_SCALE_KEY = "_style_reference_scene_scale"


_NUMERIC_LENGTH_BAND_RE = re.compile(
    r"(?P<minimum>\d{2,6})\s*(?:-|–|—|~|～|至|到)\s*(?P<maximum>\d{2,6})"
)


@dataclass(frozen=True, slots=True)
class LengthPolicy:
    """一场的长度带：``band`` 是场景卡的 ``target_length_band``；``slack`` 是作者手笔直起的两侧放宽（0 = 不放宽）；
    ``reference_scale`` 是参考作者的场尺度（只在放宽生效时随行）。"""

    band: str | None = None
    slack: float = 0.0
    reference_scale: Mapping[str, Any] | None = None

    @classmethod
    def for_scene(cls, bundle: Mapping[str, Any] | None, scene: Any) -> LengthPolicy:
        """按 bundle 的 StylePolicy 与场景的呈现方式定放宽；参考尺度只在放宽生效（style_first 且非概述场）时随行。"""
        slack = _style_first_length_slack(bundle, scene)
        return cls(
            band=_scene_band(scene),
            slack=slack,
            reference_scale=_reference_scene_scale_from_bundle(bundle) if slack > 0 else None,
        )

    @classmethod
    def plain(cls, scene: Any) -> LengthPolicy:
        """不放宽：场景卡的带原样（neutral_first / 未绑定）。"""
        return cls(band=_scene_band(scene))

    def hard_range(self) -> tuple[int, int] | None:
        """验收与补丁用的硬范围（放宽、参考尺度都算上）；带不是数字范围 → ``None``。"""
        return _parse_numeric_length_band(self.band, slack=self.slack, scale=self.reference_scale)

    def planned_range(self) -> tuple[int, int] | None:
        """场景卡原样的计划带（不放宽、不看参考尺度）。"""
        return _parse_numeric_length_band(self.band)


def _scene_band(scene: Any) -> str | None:
    return getattr(scene, "target_length_band", None) if scene is not None else None


def _bundle_inline_digests(bundle: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """bundle 既可能是 BundleBuilder 返回的外壳(``{"snapshot": {...}}``)也可能是快照本身。"""
    if not isinstance(bundle, Mapping):
        return {}
    digests = bundle.get("inline_digests")
    if isinstance(digests, Mapping):
        return digests
    snapshot = bundle.get("snapshot")
    if isinstance(snapshot, Mapping) and isinstance(snapshot.get("inline_digests"), Mapping):
        return snapshot["inline_digests"]
    return {}


def _reference_scene_scale_from_bundle(bundle: Mapping[str, Any] | None) -> dict[str, Any] | None:
    raw = _bundle_inline_digests(bundle).get(_REFERENCE_SCENE_SCALE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        derived = int(payload.get("derived_scene_chars") or 0)
    except (TypeError, ValueError):
        return None
    if derived <= 0:
        return None
    return payload


def _reference_scale_sentence(scale: Mapping[str, Any] | None) -> str:
    """长度指引里说明参考尺度的一句(没有尺度 → 空串)。"""
    if not scale:
        return ""
    derived = int(scale.get("derived_scene_chars") or 0)
    if derived <= 0:
        return ""
    if str(scale.get("basis") or "") == "explicit_scene_breaks":
        return (
            f" Measured on the reference book: this author's scenes run about {derived:,} visible characters — "
            "write at that scale; the card's band is the plan's floor, not a ceiling."
        )
    chapter = scale.get("chapter_chars") if isinstance(scale.get("chapter_chars"), Mapping) else {}
    median = int(chapter.get("median") or 0)
    p10 = int(chapter.get("p10") or 0)
    p90 = int(chapter.get("p90") or 0)
    scenes = int(scale.get("scenes_in_chapter") or 1)
    spread = f" ({p10:,}–{p90:,} is normal)" if p10 and p90 else ""
    return (
        f" Measured on the reference book: a chapter runs about {median:,} characters{spread} and this chapter has "
        f"{scenes} scene{'s' if scenes != 1 else ''}, so a scene of this author's is about {derived:,} visible characters — "
        "write at that scale; the card's band is the plan's floor, not a ceiling."
    )


def _style_first_length_slack(bundle: Mapping[str, Any] | None, scene: Any = None) -> float:
    if not style_policy_for_bundle(bundle).defers_house_taste():
        return 0.0
    # 阶段 L：「概述两段」的场是作者的呈现决定（200–500 字），风格直起也不把它放宽成整场。
    # 风格参考 v3：读场景卡上结构化的 rendering_mode，不再在结构简报的渲染文本里找「Rendering mode: summary」。
    if rendering_mode(scene) == "summary":
        return 0.0
    try:
        budget = load_yaml_config("injection_budget")
    except FileNotFoundError:
        budget = {}
    try:
        slack = float(budget.get("style_first_length_slack", _STYLE_FIRST_LENGTH_SLACK_DEFAULT))
    except (TypeError, ValueError):
        slack = _STYLE_FIRST_LENGTH_SLACK_DEFAULT
    return max(0.0, min(slack, 0.9))


def _parse_numeric_length_band(
    value: str | None,
    *,
    slack: float = 0.0,
    scale: Mapping[str, Any] | None = None,
) -> tuple[int, int] | None:
    """解析数字长度带（「1200-1800」之类）；``slack`` > 0 时两侧放宽，``scale``（参考作者的场尺度）只在放宽时抬上限。"""
    match = _NUMERIC_LENGTH_BAND_RE.search(value or "")
    if match is None:
        return None
    minimum = int(match.group("minimum"))
    maximum = int(match.group("maximum"))
    if minimum <= 0 or maximum < minimum:
        return None
    if slack > 0:
        minimum = max(1, int(round(minimum * (1.0 - slack))))
        maximum = max(minimum, int(round(maximum * (1.0 + slack))))
        # 2026-09-22 结构跟随参考书:硬范围上限至少抬到参考作者的场尺度 × (1 + slack)(封顶 ceiling),
        # 作者(或第 10 步的模型)定的带不再把一场压在参考尺度之下;下限不动。计划值(slack 0)不看参考尺度。
        if scale:
            derived = int(scale.get("derived_scene_chars") or 0)
            ceiling = int(scale.get("ceiling") or 0) or derived
            if derived > 0:
                maximum = max(maximum, min(int(round(derived * (1.0 + slack))), max(ceiling, derived)))
    return minimum, maximum


def _length_fitness(length: int, target: tuple[int, int] | None) -> float:
    if target is None:
        return 1.0
    minimum, maximum = target
    if minimum <= length <= maximum:
        return 1.0
    if length < minimum:
        return length / minimum
    return maximum / length


def _safe_length_window(minimum: int, maximum: int) -> tuple[int, int, int]:
    width = maximum - minimum
    margin = min(50, max(10, width // 10)) if width >= 40 else 0
    safe_minimum = minimum + margin
    safe_maximum = maximum - margin
    if safe_minimum > safe_maximum:
        safe_minimum, safe_maximum = minimum, maximum
    target = round((safe_minimum + safe_maximum) / 2)
    return safe_minimum, safe_maximum, target


def _style_repair_working_window(
    minimum: int,
    maximum: int,
    *,
    source_length: int,
) -> tuple[int, int, int]:
    """长度不合格时贴近最近安全边界修，不把局部校正变成整篇伸缩。"""

    safe_minimum, safe_maximum, safe_target = _safe_length_window(
        minimum,
        maximum,
    )
    if minimum <= source_length <= maximum:
        local_minimum = max(minimum, source_length * 9 // 10)
        local_maximum = min(maximum, (source_length * 11 + 9) // 10)
        if local_minimum <= local_maximum:
            return local_minimum, local_maximum, source_length
        return minimum, maximum, min(max(source_length, minimum), maximum)

    safe_width = max(0, safe_maximum - safe_minimum)
    correction_span = min(120, max(80, safe_width // 8))
    if source_length < minimum:
        local_minimum = safe_minimum
        local_maximum = min(safe_maximum, safe_minimum + correction_span)
    else:
        local_maximum = safe_maximum
        local_minimum = max(safe_minimum, safe_maximum - correction_span)
    target = round((local_minimum + local_maximum) / 2)
    if local_minimum > local_maximum:
        return safe_minimum, safe_maximum, safe_target
    return local_minimum, local_maximum, target


# ---------------------------------------------------------------------------
# 长度指引（接在各步 user prompt 末尾）
# ---------------------------------------------------------------------------


def _neutral_length_instruction(
    lengths: LengthPolicy,
    *,
    previous_length: int | None = None,
    retry: bool = False,
) -> str:
    length_range = lengths.hard_range()
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    prior = (
        f" The previous attempt was about {previous_length} visible characters and was rejected."
        if previous_length is not None
        else ""
    )
    retry_rule = (
        " Edit the labeled rejected draft directly and return one complete replacement scene, not commentary, a continuation, or a synopsis. Preserve every required fact, causal step, and ending function."
        if retry
        else ""
    )
    delta_rule = ""
    if retry and previous_length is not None:
        if previous_length < safe_minimum:
            delta_rule = (
                f" Add at least {safe_minimum - previous_length} visible characters inside existing action-reaction, blocking, perception, or consequence; do not add a new event."
            )
        elif previous_length > safe_maximum:
            delta_rule = (
                f" Remove at least {previous_length - safe_maximum} visible characters by compressing repetition and decorative description only; do not remove a required fact."
            )
        else:
            local_minimum = max(safe_minimum, previous_length * 9 // 10)
            local_maximum = min(
                safe_maximum,
                (previous_length * 11 + 9) // 10,
            )
            delta_rule = (
                f" The previous length already passed. Keep the repaired scene within {local_minimum}-{local_maximum} visible characters, make the smallest localized edits needed, and do not restage or broadly rewrite unchanged paragraphs."
            )
    return (
        "\n\n[Deterministic Scene Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters."
        f" Aim near {target}; use {safe_minimum}-{safe_maximum} as the working window so minor counting differences cannot cross the hard boundary."
        f"{prior}{retry_rule}{delta_rule} Before returning, count once and compress or expand existing action-reaction beats; preserve every required fact and do not add a new event."
    )


def _style_first_length_instruction(
    lengths: LengthPolicy,
    *,
    previous_length: int | None = None,
    retry: bool = False,
) -> str:
    """style_first 首稿的长度指引:场景卡的带是计划值,作者自己的尺度在放宽后的硬范围内优先。"""
    planned = lengths.planned_range()
    length_range = lengths.hard_range()
    if planned is None or length_range is None:
        return ""
    minimum, maximum = length_range
    prior = (
        f" The previous attempt was about {previous_length} visible characters and was rejected."
        if previous_length is not None
        else ""
    )
    retry_rule = (
        " Edit the labeled rejected draft directly and return one complete replacement scene, not commentary, a continuation, or a synopsis. Preserve every required fact, causal step, and ending function, and keep the reference author's manner."
        if retry
        else ""
    )
    delta_rule = ""
    if retry and previous_length is not None:
        if previous_length < minimum:
            delta_rule = (
                f" Add at least {minimum - previous_length} visible characters with this author's own means; do not add a new event."
            )
        elif previous_length > maximum:
            delta_rule = (
                f" Remove at least {previous_length - maximum} visible characters; do not remove a required fact."
            )
    scale_note = _reference_scale_sentence(lengths.reference_scale)
    return (
        "\n\n[Scene Length Guide]\n"
        f"The scene card planned {planned[0]}-{planned[1]} visible non-whitespace Chinese prose characters. "
        f"The reference author's own scale for a scene like this takes precedence inside the hard range {minimum}-{maximum}: "
        "the scene may run shorter or longer the way that author's scenes do, but must stay inside the hard range."
        f"{scale_note} "
        "Fill or compress with this author's own means — summary, digression, dialogue, description, reflection — "
        f"not only action-reaction beats; never drop a required fact and never add a new event.{prior}{retry_rule}{delta_rule}"
    )


def _style_length_instruction(
    lengths: LengthPolicy,
    *,
    source_length: int,
    style_first: bool = False,
) -> str:
    length_range = lengths.hard_range()
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    if style_first:
        planned = lengths.planned_range() or length_range
        scale_note = _reference_scale_sentence(lengths.reference_scale)
        return (
            "\n\n[Style Revision Length Guide]\n"
            f"The first draft is about {source_length} visible characters; the scene card planned {planned[0]}-{planned[1]}. "
            f"The complete revision must stay inside the hard range {minimum}-{maximum}; within it, the reference author's own scale wins."
            f"{scale_note} "
            "Count once before returning. Fill or compress with this author's own means — summary, digression, dialogue, description, reflection — "
            "never by dropping a required beat."
        )
    return (
        "\n\n[Deterministic Style Rewrite Length Guard]\n"
        f"The approved source is about {source_length} visible characters. The complete final rewrite must be "
        f"{minimum}-{maximum}; aim near {target} and keep {safe_minimum}-{safe_maximum} as the working window. "
        "Count once before returning. Style compression is not permission to drop a required beat or fall below "
        "the lower bound; expand or compress only existing action-reaction, blocking, perception, and consequence."
    )


def _style_repair_length_instruction(
    lengths: LengthPolicy,
    *,
    source_length: int,
) -> str:
    """二改使用局部长度窗，防止修一个问题却把合格稿整体扩写或压缩。"""

    length_range = lengths.hard_range()
    if length_range is None:
        if source_length <= 0:
            return ""
        local_minimum = max(20, source_length * 9 // 10)
        local_maximum = max(
            local_minimum,
            (source_length * 11 + 9) // 10,
        )
        return (
            "\n\n[Deterministic Style Repair Length Guard]\n"
            f"Keep the complete repaired scene within {local_minimum}-{local_maximum} visible non-whitespace "
            f"characters (the source is about {source_length}). Make the smallest localized edits needed; "
            "do not restage, summarize, or broadly rewrite unchanged paragraphs."
        )

    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if minimum <= source_length <= maximum:
        local_rule = (
            f"The source already passes at about {source_length}; keep the repaired scene within "
            f"the local {local_minimum}-{local_maximum} window and make the smallest localized edits needed."
        )
    else:
        if source_length < minimum:
            delta_rule = (
                f"add {local_minimum - source_length}-{local_maximum - source_length} visible characters"
            )
        else:
            delta_rule = (
                f"remove {source_length - local_maximum}-{source_length - local_minimum} visible characters"
            )
        local_rule = (
            f"The source is about {source_length} and is outside the hard range; {delta_rule}, finish inside "
            f"the narrow {local_minimum}-{local_maximum} correction window, and aim near {target}. Change only "
            "existing action-reaction, blocking, perception, consequence, or removable repetition."
        )
    return (
        "\n\n[Deterministic Style Repair Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters. "
        f"{local_rule} Preserve every required fact, causal step, and ending function; count once before returning."
    )

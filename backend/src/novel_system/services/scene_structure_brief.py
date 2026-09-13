"""场景结构简报（Scene Structure）——把雪花第 9、10 步的场景设计带标签地送进写作管线。

2026-09-13 对雪花模块的评估核实了一条断链：雪花物化把 ``scene_form / scene_crucible /
goal / conflict / setback | reaction / dilemma / decision / cost_requirement / must_reveal /
must_withhold`` 写进 ``SceneCard.writer_brief_json``，但写作侧一律经
``writer_briefs.normalize_scene_writer_brief`` 归一化，而那份 v2 schema 只认另外 13 个键，
于是起草 bundle、蓝图快照、近终稿快照、预检看到的场景简报全部为空。起草模型只剩
``scene_digest.scene_card_digest`` 里无标签的 ``Beats`` 和一行 ``Scene type: proactive``，
而起草提示词从未解释主动 / 反应场景该怎么写。

本模块**直读原始键**，渲染成带标签的事实段，作为与 ``scene_card`` 同级的事实 section
（``context_budget.SECTION_SPECS`` 里的 ``scene_structure_brief``）进入起草 bundle、蓝图快照
与近终稿快照。它只陈述作者设计的事实；「主动 / 反应场景该怎么写」的指令在提示词模板里
（``neutral_draft`` / ``style_first_draft`` / ``scene_blueprint`` / ``hard_qc`` / ``soft_qc`` /
``near_final_acceptance_review``）。

不改 v2 schema：作者草稿、生命周期等 v2 消费者行为不变。章节编排本来就按
``catalog.SCENE_BRIEF_GCS / SCENE_BRIEF_RDD`` 读原始键，所以作者在编排台手填的三拍同样会
从这里到达写作——判据只看「有没有三拍或坩埚」，不看 ``source``。

来源：Ingermanson《How to Write a Dynamite Scene》——主动场景 = 目标 → 冲突 → 挫折，反应场景
= 反应 → 两难 → 决定；每一场都要能说出坩埚是什么，说不出就是坏场景。

``NOVEL_SYSTEM_SCENE_STRUCTURE_BRIEF=false`` 可整体关闭注入（默认开，只作回滚用）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, StoryCharacter
from novel_system.settings import get_settings

SCENE_STRUCTURE_SECTION_KEY = "scene_structure_brief"
SCENE_STRUCTURE_SECTION_LABEL = "Scene Structure (Snowflake)"

PROACTIVE_BEATS: tuple[str, ...] = ("goal", "conflict", "setback")
REACTIVE_BEATS: tuple[str, ...] = ("reaction", "dilemma", "decision")
SCENE_FORMS: tuple[str, ...] = ("proactive", "reactive")

_BEAT_LABELS: dict[str, str] = {
    "goal": "Goal (目标)",
    "conflict": "Conflict (冲突)",
    "setback": "Setback (挫折)",
    "reaction": "Reaction (反应)",
    "dilemma": "Dilemma (两难)",
    "decision": "Decision (决定)",
}
_FORM_LINES: dict[str, str] = {
    "proactive": "proactive scene (主动场景) — Goal → Conflict → Setback",
    "reactive": "reactive scene (反应场景) — Reaction → Dilemma → Decision",
}
_UNPLANNED = "未规划"


def structure_brief_enabled() -> bool:
    """环境开关；不读库内配置快照（这不是作者在界面上配的东西）。"""
    settings = get_settings(include_runtime_config=False)
    return bool(getattr(settings, "scene_structure_brief_enabled", True))


def scene_structure_form(scene: SceneCard) -> str | None:
    """这一场的形态。显式声明优先（简报 ``scene_form`` / ``primary_form``、场景卡 ``scene_type``），
    否则按填了哪一组三拍推断；两组都空返回 ``None``。"""
    brief = _brief(scene)
    for candidate in (brief.get("scene_form"), brief.get("primary_form"), getattr(scene, "scene_type", None)):
        text = _text(candidate).lower()
        if text in SCENE_FORMS:
            return text
    has_proactive = any(_text(brief.get(key)) for key in PROACTIVE_BEATS)
    has_reactive = any(_text(brief.get(key)) for key in REACTIVE_BEATS)
    if has_reactive and not has_proactive:
        return "reactive"
    if has_proactive:
        return "proactive"
    return None


def scene_has_structure(scene: SceneCard) -> bool:
    """简报里有没有场景结构：任一三拍或坩埚非空即算。v2-only 简报与空简报都返回 False。"""
    brief = _brief(scene)
    return any(
        _text(brief.get(key))
        for key in (*PROACTIVE_BEATS, *REACTIVE_BEATS, "scene_crucible", "crucible")
    )


def missing_structure_fields(scene: SceneCard) -> list[str]:
    """按形态列出还缺的必填结构字段（坩埚 + 本形态三拍）。没有结构的场返回空表——
    那是 v2 简报的事，由预检的另一条规则处理。"""
    if not scene_has_structure(scene):
        return []
    brief = _brief(scene)
    form = scene_structure_form(scene) or "proactive"
    beats = REACTIVE_BEATS if form == "reactive" else PROACTIVE_BEATS
    missing: list[str] = []
    if not (_text(brief.get("scene_crucible")) or _text(brief.get("crucible"))):
        missing.append("scene_crucible")
    missing.extend(key for key in beats if not _text(brief.get(key)))
    return missing


def render_scene_structure_brief(scene: SceneCard, session: Session | None = None) -> str | None:
    """渲染事实段；没有结构、或开关关闭时返回 ``None``（调用方不注入任何东西）。

    只陈述作者写下的事实，不带写作指令。字段缺席时明确写「未规划」而不是省略——
    起草模型要知道「作者没给挫折」与「作者给了挫折但我没看到」是两回事。
    """
    if not structure_brief_enabled() or not scene_has_structure(scene):
        return None
    brief = _brief(scene)
    form = scene_structure_form(scene) or "proactive"
    primary = REACTIVE_BEATS if form == "reactive" else PROACTIVE_BEATS
    secondary = PROACTIVE_BEATS if form == "reactive" else REACTIVE_BEATS

    pov_id = _text(getattr(scene, "pov_character_id", None))
    onstage_ids = [_text(item) for item in (getattr(scene, "onstage_chars_json", None) or []) if _text(item)]
    names = _character_names(session, [pov_id, *onstage_ids])

    lines = [f"Scene form: {_FORM_LINES[form]}"]
    if pov_id:
        lines.append(f"POV character: {names.get(pov_id) or pov_id}")
    if onstage_ids:
        onstage = list(dict.fromkeys(names.get(item) or item for item in onstage_ids))
        lines.append("Onstage characters: " + ", ".join(onstage))
    crucible = _text(brief.get("scene_crucible")) or _text(brief.get("crucible"))
    lines.append(f"Scene crucible (坩埚): {crucible or _UNPLANNED}")
    primary_values: list[str] = []
    for key in primary:
        value = _text(brief.get(key))
        primary_values.append(value)
        lines.append(f"{_BEAT_LABELS[key]}: {value or _UNPLANNED}")
    cost = _text(brief.get("cost_requirement"))
    if cost:
        lines.append(f"Cost paid (代价): {cost}")
    # 雪花物化把 must_reveal 设成挫折 / 决定本身；与三拍重复时不再赘述一遍。
    must_reveal = _text(brief.get("must_reveal"))
    if must_reveal and must_reveal not in primary_values:
        lines.append(f"Must reveal: {must_reveal}")
    must_withhold = _text(brief.get("must_withhold"))
    if must_withhold:
        lines.append(f"Must withhold: {must_withhold}")
    exit_change = _text(getattr(scene, "exit_change", None))
    if exit_change:
        lines.append(f"Exit change: {exit_change}")
    pull = _text(brief.get("next_scene_pull")) or _text(getattr(scene, "hook", None))
    if pull:
        lines.append(f"Next-scene pull (钩子): {pull}")
    band = _text(getattr(scene, "target_length_band", None))
    if band:
        lines.append(f"Target length band: {band}")
    follow_up = [f"{_BEAT_LABELS[key]}: {_text(brief.get(key))}" for key in secondary if _text(brief.get(key))]
    if follow_up:
        lines.append("Follow-up beats (secondary form, keep them subordinate): " + "; ".join(follow_up))
    return "\n".join(lines)


def _brief(scene: SceneCard) -> dict[str, Any]:
    raw = getattr(scene, "writer_brief_json", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _character_names(session: Session | None, character_ids: Iterable[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    if session is None:
        return names
    for character_id in character_ids:
        key = _text(character_id)
        if not key or key in names:
            continue
        row = session.get(StoryCharacter, key)
        if row is not None and _text(row.display_name):
            names[key] = _text(row.display_name)
    return names

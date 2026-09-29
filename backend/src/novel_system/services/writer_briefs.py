from __future__ import annotations

from typing import Any

from novel_system.services.errors import DomainError


WRITER_BRIEF_SCHEMA_VERSION = "writer_brief_v2"

CHAPTER_WRITER_BRIEF_FIELDS: tuple[tuple[str, str], ...] = (
    ("core_promise", "核心承诺"),
    ("plot_movement", "主线推进"),
    ("character_shift", "人物变化"),
    ("chapter_question", "章节问题"),
    ("ending_aftertaste", "结尾余味"),
    ("chapter_promise", "chapter promise"),
    ("escalation_path", "escalation path"),
    ("relationship_delta", "relationship delta"),
    ("reveal_or_reversal", "reveal or reversal"),
    ("payoff_target", "payoff target"),
    ("ending_question", "ending question"),
)

SCENE_WRITER_BRIEF_FIELDS: tuple[tuple[str, str], ...] = (
    ("character_desire", "人物欲望"),
    ("obstacle", "阻碍"),
    ("stakes", "风险/代价"),
    ("secret_or_misunderstanding", "秘密/误解"),
    ("subtext", "潜台词"),
    ("irreversible_change", "不可逆变化"),
    ("reader_question", "读者问题"),
    ("choice_under_pressure", "choice under pressure"),
    ("power_shift", "power shift"),
    ("new_information", "new information"),
    ("emotional_turn", "emotional turn"),
    ("image_anchor", "image anchor"),
    ("reader_aftertaste", "reader aftertaste"),
)


def empty_scene_writer_brief() -> dict[str, str]:
    return {"schema_version": WRITER_BRIEF_SCHEMA_VERSION, **{key: "" for key, _label in SCENE_WRITER_BRIEF_FIELDS}}


def normalize_chapter_writer_brief(value: Any) -> dict[str, str]:
    return _normalize_writer_brief(value, CHAPTER_WRITER_BRIEF_FIELDS, "chapter")


def normalize_scene_writer_brief(value: Any) -> dict[str, str]:
    return _normalize_writer_brief(value, SCENE_WRITER_BRIEF_FIELDS, "scene")


def writer_brief_has_content(brief: dict[str, Any]) -> bool:
    return any(str(value or "").strip() for key, value in brief.items() if key != "schema_version")


def _normalize_writer_brief(
    value: Any,
    fields: tuple[tuple[str, str], ...],
    object_type: str,
) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DomainError(
            "WRITER_BRIEF_INVALID",
            f"{object_type} writer_brief_json must be an object",
            status_code=400,
        )
    normalized: dict[str, str] = {"schema_version": WRITER_BRIEF_SCHEMA_VERSION}
    for key, _label in fields:
        raw = value.get(key, "")
        if raw is None:
            normalized[key] = ""
        elif isinstance(raw, (str, int, float, bool)):
            normalized[key] = str(raw).strip()
        else:
            raise DomainError(
                "WRITER_BRIEF_INVALID",
                f"{object_type} writer brief field {key} must be a scalar value",
                status_code=400,
            )
    return normalized

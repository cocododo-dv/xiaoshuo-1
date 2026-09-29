"""场景形态 / 呈现方式 / 场景卡文本取值（叶子模块：不 import ``novel_system`` 的任何东西）。

以前四处各推一遍：结构简报认 ``scene_form`` / ``primary_form`` / ``scene_type`` 再按填了哪组三拍推断；执行契约多认
``scene_mode`` 与 ``reaction`` / ``goal`` 两个别名；关键度只看 ``scene_form or scene_type``；起草单独读
``rendering_mode``。两个 ``_text`` 还不一样——一个把列表当空、一个把列表的 repr 写进提示词。现在都从这里取。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROACTIVE_BEATS: tuple[str, ...] = ("goal", "conflict", "setback")
REACTIVE_BEATS: tuple[str, ...] = ("reaction", "dilemma", "decision")
SCENE_FORMS: tuple[str, ...] = ("proactive", "reactive")
# 执行契约沿用的写法（契约 payload 与来源快照哈希不能因清理而变）：reaction → reactive，goal → proactive
_FORM_ALIASES: dict[str, str] = {
    "proactive": "proactive",
    "goal": "proactive",
    "reactive": "reactive",
    "reaction": "reactive",
}


def text(value: Any) -> str:
    """场景卡 / 简报里的一个标量字段 → 去掉两端空白的字符串；``None``、列表、字典都算空（不把 repr 当正文）。"""
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def writer_brief(scene: Any) -> dict[str, Any]:
    """场景卡的原始写作简报（``writer_brief_json``）的一份拷贝；没有就是空字典。"""
    raw = getattr(scene, "writer_brief_json", None) if scene is not None else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def form_alias(value: Any) -> str:
    """认得的形态写法（含 ``reaction`` / ``goal`` 两个别名）→ ``proactive`` / ``reactive``；认不出 → 空串。"""
    return _FORM_ALIASES.get(text(value).lower(), "")


def scene_form(scene: Any) -> str | None:
    """这一场的形态。显式声明优先（简报 ``scene_form`` / ``primary_form``、场景卡 ``scene_type``），
    否则按填了哪一组三拍推断；两组都空返回 ``None``。"""
    brief = writer_brief(scene)
    for candidate in (brief.get("scene_form"), brief.get("primary_form"), getattr(scene, "scene_type", None)):
        form = text(candidate).lower()
        if form in SCENE_FORMS:
            return form
    has_proactive = any(text(brief.get(key)) for key in PROACTIVE_BEATS)
    has_reactive = any(text(brief.get(key)) for key in REACTIVE_BEATS)
    if has_reactive and not has_proactive:
        return "reactive"
    if has_proactive:
        return "proactive"
    return None


def rendering_mode(scene: Any) -> str:
    """场景卡上结构化的呈现方式（``writer_brief_json.rendering_mode``：full / summary / skip），缺省 full。"""
    return text(writer_brief(scene).get("rendering_mode")).lower() or "full"

"""雪花角色 id 的两个口径：库里（实体口径）带作品前缀，交给前端的草稿（草稿口径）不带。

B06-01（2026-09-30 作者批准 #16c）：React 的角色表按 c1 / c2 / … 给手加的角色编号，服务端原样把它写进
``story_characters.character_id``——那是**全局**主键。第二部作品确认角色表时，``session.get(StoryCharacter, "c1")``
拿到的是第一部作品的人，于是把它改成了第二部作品的名字；按 id 解析视角 / 在场人物的读者（结构简报、章规划、
正史核对）随之把别的作品的人名带进提示词。

现在的纪律（叶子模块：纯函数，只处理字符串与草稿字典）：
- **写入即规范**：进库的角色 id 一律是 ``f"{project_id}_{raw}"``；已经带着本作品前缀的原样保留；缺 id 的角色
  铸一个 ``f"{project_id}_CHAR_{hex8}"``（不再按位置编号——删掉中间一个角色，按位置编的号会落到下一个人身上）。
  视角（``pov_character_id``）、在场人物（``onstage_chars_json``）、全书主角（``protagonist_character_id``）的引用
  同一口径。
- **交给前端时剥前缀**：前端本机缓存、写穿的 ``fe_scaffold`` 与规范字段里的 id 永远是同一个字符串——保真合并
  （``mergeCanon``）按 id 对位，才对得上；前端给新角色编号（c1 / c2 / …）也不会撞上一个它从没见过的带前缀的 id。
  ``fe_*`` 写穿键是前端自己的数据，服务端从不改写。
- 两个方向互为逆运算：``canonical(present(x)) == x``（x 带本作品前缀），``present(canonical(y)) == y``（y 不带）。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

#: 草稿里承载角色 id 的键：集合成员的身份、全书主角、场景的视角与在场人物
_MEMBER_ID_KEY = "character_id"
_PROTAGONIST_KEY = "protagonist_character_id"
_SCENE_POV_KEY = "pov_character_id"
_SCENE_ONSTAGE_KEY = "onstage_chars_json"


def character_prefix(project_id: str) -> str:
    return f"{project_id}_"


def canonical_character_id(project_id: str, raw: Any) -> str:
    """库里的角色 id：已带本作品前缀的原样保留，其余加前缀；空 → 空串（调用方决定要不要铸新的）。
    不知道是哪部作品（project_id 为空）时原样返回——前缀只能是真实的作品 id。"""
    text = str(raw or "").strip()
    if not text or not str(project_id or "").strip():
        return text
    prefix = character_prefix(project_id)
    return text if text.startswith(prefix) else prefix + text


def present_character_id(project_id: str, value: Any) -> str:
    """交给前端的角色 id：剥掉一层本作品前缀（剥完还带前缀的历史 id 原样给，保证来回可逆）。"""
    text = str(value or "").strip()
    if not str(project_id or "").strip():
        return text
    prefix = character_prefix(project_id)
    if text.startswith(prefix):
        rest = text[len(prefix):]
        if rest and not rest.startswith(prefix):
            return rest
    return text


def mint_character_id(project_id: str) -> str:
    suffix = f"CHAR_{uuid.uuid4().hex[:8]}"
    return f"{project_id}_{suffix}" if str(project_id or "").strip() else suffix


def canonicalize_draft(project_id: str, draft: Any, *, mint_missing: bool = False) -> Any:
    """一步草稿（或教练补丁）的规范口径副本；``mint_missing`` 给没有 id 的角色铸一个（写库之前用）。"""
    return _map_draft(draft, lambda value: canonical_character_id(project_id, value), mint=(lambda: mint_character_id(project_id)) if mint_missing else None)


def present_draft(project_id: str, draft: Any) -> Any:
    """交给前端的草稿副本：角色 id 剥前缀。"""
    return _map_draft(draft, lambda value: present_character_id(project_id, value), mint=None)


def _map_draft(draft: Any, mapper: Callable[[Any], str], *, mint: Callable[[], str] | None) -> Any:
    if not isinstance(draft, dict):
        return draft
    result = dict(draft)
    characters = draft.get("characters")
    if isinstance(characters, list):
        mapped: list[Any] = []
        for item in characters:
            if not isinstance(item, dict):
                mapped.append(item)
                continue
            member = dict(item)
            character_id = mapper(item.get(_MEMBER_ID_KEY))
            if not character_id and mint is not None and _has_identity(item):
                character_id = mint()
            if character_id or _MEMBER_ID_KEY in item:
                member[_MEMBER_ID_KEY] = character_id
            mapped.append(member)
        result["characters"] = mapped
    if _PROTAGONIST_KEY in draft:
        result[_PROTAGONIST_KEY] = mapper(draft.get(_PROTAGONIST_KEY))
    scenes = draft.get("scenes")
    if isinstance(scenes, list):
        result["scenes"] = [_map_scene(item, mapper) for item in scenes]
    return result


def _map_scene(item: Any, mapper: Callable[[Any], str]) -> Any:
    if not isinstance(item, dict):
        return item
    scene = dict(item)
    if _SCENE_POV_KEY in item:
        scene[_SCENE_POV_KEY] = mapper(item.get(_SCENE_POV_KEY))
    onstage = item.get(_SCENE_ONSTAGE_KEY)
    if isinstance(onstage, list):
        scene[_SCENE_ONSTAGE_KEY] = [mapper(value) for value in onstage if str(value or "").strip()]
    return scene


def _has_identity(item: dict[str, Any]) -> bool:
    """值得铸 id 的成员：除 id 之外至少写了一样东西——完全空的成员不铸 id（它不会进名册）。"""
    return any(_has_text(value) for key, value in item.items() if key != _MEMBER_ID_KEY)


def _has_text(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_text(item) for item in value)
    return bool(str(value or "").strip())

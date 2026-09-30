"""雪花角色 id 的两个口径：库里（实体口径）带作品前缀，交给前端的草稿（草稿口径）剥掉服务端加上的那一层。

B06-01（2026-09-30 作者批准 #16c）：React 的角色表按 c1 / c2 / … 给手加的角色编号，服务端原样把它写进
``story_characters.character_id``——那是**全局**主键。第二部作品确认角色表时，``session.get(StoryCharacter, "c1")``
拿到的是第一部作品的人，于是把它改成了第二部作品的名字；按 id 解析视角 / 在场人物的读者（结构简报、章规划、
正史核对）随之把别的作品的人名带进提示词。

现在的纪律（叶子模块：纯函数，只处理字符串与草稿字典）：
- **成员 id 写入即规范**：角色表成员（``characters[].character_id``）进库一律是 ``f"{project_id}_{raw}"``；已经带着
  本作品前缀的原样保留；缺 id 的成员铸一个 ``f"{project_id}_CHAR_{hex8}"``（不再按位置编号——删掉中间一个角色，
  按位置编的号会落到下一个人身上）。
- **引用只认角色 id**：视角（``pov_character_id``）、在场人物（``onstage_chars_json``）、全书主角
  （``protagonist_character_id``）可以是角色 id，也可以是手填的姓名——04 名册还空时视角是自由文本框，模型也会回
  姓名。只有指着角色的引用才补前缀：本作品名册（``roster``：角色计划与角色步最新草稿的成员，库里口径）里有它，
  或者它长着前端给手加角色的编号（c1 / c2 / …：名册那一步还没上行，别的步已经引用了它）。其余原样——姓名、
  资料库 / 章节编排铸的全局 ``CHAR_<HEX>``。给姓名补前缀会把 ``<作品>_林昭`` 写进起草提示词，还会让按姓名
  判「视角是不是全书主角」的简报判反。迁移 20260929_0093 用的是同一条规则。
- **交给前端时只剥服务端补上的那层前缀**：前端本机缓存、写穿的 ``fe_scaffold`` 与规范字段里的 id 必须是同一个
  字符串——保真合并（``mergeCanon``）按 id 对位，对不上就把服务端才有的字段（人物小传的嵌套栏、故事线）丢掉。
  前端编的号（c1）和模型起的别名，前缀是服务端补的，剥掉；服务端自己铸的号（``<作品>_CHAR01`` /
  ``<作品>_CHAR_<hex>``）一向带着前缀交给前端，原样给。``fe_*`` 写穿键是前端自己的数据，服务端从不改写。
- 两个方向互为逆运算：``canonical(present(x)) == x``（x 是库里的 id），``present(canonical(y)) == y``（y 是前端手里的
  id；前端手里不会有不带前缀的 CHAR 号——服务端铸号从来都是带着前缀交出去的）。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Iterable

#: 草稿里承载角色 id 的键：集合成员的身份、全书主角、场景的视角与在场人物
_MEMBER_ID_KEY = "character_id"
_PROTAGONIST_KEY = "protagonist_character_id"
_SCENE_POV_KEY = "pov_character_id"
_SCENE_ONSTAGE_KEY = "onstage_chars_json"

#: 前端给手加角色的编号（``ws-snow-editors-cast.jsx``：c1 / c2 / …）
_FRONTEND_MINTED = re.compile(r"c\d+")
#: 服务端铸号去掉作品前缀后的样子：旧清洗器 / v1 规划器的 CHAR01，v1 规划器与现在的 CHAR_<hex8>
_SERVER_MINTED = re.compile(r"CHAR(?:\d+|_[0-9A-Fa-f]+)")

#: 按需给出本作品名册（库里口径的角色 id）；只在认不出一个引用时才调用
RosterSource = Callable[[], Iterable[str]]


def character_prefix(project_id: str) -> str:
    return f"{project_id}_"


def canonical_character_id(project_id: str, raw: Any) -> str:
    """库里的成员 id：已带本作品前缀的原样保留，其余加前缀；空 → 空串（调用方决定要不要铸新的）。
    不知道是哪部作品（project_id 为空）时原样返回——前缀只能是真实的作品 id。"""
    text = str(raw or "").strip()
    if not text or not str(project_id or "").strip():
        return text
    prefix = character_prefix(project_id)
    return text if text.startswith(prefix) else prefix + text


def canonical_character_ref(project_id: str, value: Any, is_known: Callable[[str], bool]) -> str:
    """库里的引用（视角 / 在场 / 全书主角）：指着角色才补前缀，姓名与全局 id 原样（见模块说明）。
    ``is_known`` 判一个库里口径的 id 在不在本作品名册里。"""
    text = str(value or "").strip()
    if not text or not str(project_id or "").strip():
        return text
    prefix = character_prefix(project_id)
    if text.startswith(prefix):
        return text
    if _FRONTEND_MINTED.fullmatch(text) or is_known(prefix + text):
        return prefix + text
    return text


def present_character_id(project_id: str, value: Any) -> str:
    """交给前端的 id：剥掉服务端补上的那层作品前缀；服务端自己铸的号（前缀之后是 CHAR01 / CHAR_<hex>）原样给，
    剥完还带前缀的历史 id 也原样给——保证来回可逆。"""
    text = str(value or "").strip()
    if not str(project_id or "").strip():
        return text
    prefix = character_prefix(project_id)
    if text.startswith(prefix):
        rest = text[len(prefix):]
        if rest and not rest.startswith(prefix) and not _SERVER_MINTED.fullmatch(rest):
            return rest
    return text


def mint_character_id(project_id: str) -> str:
    suffix = f"CHAR_{uuid.uuid4().hex[:8]}"
    return f"{project_id}_{suffix}" if str(project_id or "").strip() else suffix


def canonicalize_draft(
    project_id: str,
    draft: Any,
    *,
    roster: RosterSource | None = None,
    mint_missing: bool = False,
) -> Any:
    """一步草稿（或教练补丁、前端的 draft_override）的规范口径副本。

    成员 id 一律规范，``mint_missing`` 给没有 id 的成员铸一个（写库之前用）。引用只有指着角色才规范：这份草稿自己的
    成员、``roster`` 给的本作品名册——名册只在一个引用既不带前缀、又不像前端编号时才去取（一次）。"""
    if not isinstance(draft, dict):
        return draft
    mint = (lambda: mint_character_id(project_id)) if mint_missing else None
    result = _map_members(draft, lambda value: canonical_character_id(project_id, value), mint=mint)
    members = {
        str(item.get(_MEMBER_ID_KEY) or "").strip()
        for item in (result.get("characters") if isinstance(result.get("characters"), list) else [])
        if isinstance(item, dict)
    }
    known: set[str] | None = None

    def is_known(candidate: str) -> bool:
        nonlocal known
        if candidate in members:
            return True
        if known is None:
            known = {str(value or "").strip() for value in (roster() if roster is not None else ())}
        return candidate in known

    return _map_refs(result, lambda value: canonical_character_ref(project_id, value, is_known))


def present_draft(project_id: str, draft: Any) -> Any:
    """交给前端的草稿副本：成员 id 与引用都剥掉服务端补上的前缀。"""
    if not isinstance(draft, dict):
        return draft

    def mapper(value: Any) -> str:
        return present_character_id(project_id, value)

    return _map_refs(_map_members(draft, mapper, mint=None), mapper)


def _map_members(draft: dict[str, Any], mapper: Callable[[Any], str], *, mint: Callable[[], str] | None) -> dict[str, Any]:
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
    return result


def _map_refs(draft: dict[str, Any], mapper: Callable[[Any], str]) -> dict[str, Any]:
    result = dict(draft)
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

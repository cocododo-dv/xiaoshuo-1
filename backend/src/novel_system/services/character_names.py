"""一部作品里人物与资料库实体的名字：显示名与别名，按作品一次查齐（叶子模块：只依赖表模型）。

正史事实按角色 id 存（``StoryCharacter.character_id``），正文与提示词里写的却是名字。以前连续性检查拿 id 去
正文里找，一条正史事实也查不出矛盾，提示词摘要也把 ``### CHAR_xxx`` 这样的 id 当人名印出来（B11-01）。
名字只有这一处来源：人物的 ``display_name`` 与人物表 / 小传 / 人物圣经里写的别名，资料库实体的 ``name`` 与
``aliases_json``。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LibraryEntity, StoryCharacter

# 人物资料里记别名的键（不分大小写）：值可以是一串逗号分隔的名字，也可以是列表。
ALIAS_KEYS = frozenset(
    {
        "alias",
        "aliases",
        "aka",
        "nickname",
        "nicknames",
        "other_names",
        "former_names",
        "别名",
        "昵称",
        "曾用名",
    }
)

# 正文匹配只用两个字以上的名字：单字的名字 / 别名在中文正文里几乎处处命中。
MIN_MATCH_NAME_CHARS = 2


def normalized_name(value: Any) -> str:
    """比较名字用的形式：NFKC、去首尾空白、大小写折叠、去掉中间空白。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "".join(normalized.split())


def _collect_scalar_values(value: Any, output: list[str]) -> None:
    if isinstance(value, str):
        output.extend(part.strip() for part in value.replace("，", ",").split(",") if part.strip())
    elif isinstance(value, list):
        output.extend(str(part).strip() for part in value if str(part).strip())


def _collect_alias_values(value: Any, output: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().casefold() in ALIAS_KEYS:
                _collect_scalar_values(child, output)
            elif isinstance(child, (dict, list)):
                _collect_alias_values(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_alias_values(child, output)


def character_alias_values(row: StoryCharacter) -> list[str]:
    """人物表 / 小传 / 人物圣经里写下的别名（原样，按出现先后，去重）。"""
    values: list[str] = []
    for payload in (row.summary_json, row.synopsis_json, row.bible_json):
        _collect_alias_values(payload, values)
    return list(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class EntityNames:
    entity_id: str
    display_name: str
    aliases: tuple[str, ...] = ()

    def match_names(self) -> tuple[str, ...]:
        """正文里指这个实体的写法：显示名 + 别名（够长的、去重）。角色 id 本身由调用方兜底加上。"""
        names: list[str] = []
        for name in (self.display_name, *self.aliases):
            clean = str(name or "").strip()
            if len(clean) >= MIN_MATCH_NAME_CHARS and clean not in names:
                names.append(clean)
        return tuple(names)


def project_entity_names(session: Session, project_id: str) -> dict[str, EntityNames]:
    """这部作品的人物与资料库实体，按 id 索引（两条语句）。"""
    names: dict[str, EntityNames] = {}
    for row in session.execute(
        select(StoryCharacter).where(StoryCharacter.project_id == project_id)
    ).scalars():
        names[row.character_id] = EntityNames(
            entity_id=row.character_id,
            display_name=str(row.display_name or "").strip(),
            aliases=tuple(character_alias_values(row)),
        )
    for row in session.execute(
        select(LibraryEntity).where(LibraryEntity.project_id == project_id)
    ).scalars():
        names.setdefault(
            row.entity_id,
            EntityNames(
                entity_id=row.entity_id,
                display_name=str(row.name or "").strip(),
                aliases=tuple(str(alias).strip() for alias in (row.aliases_json or []) if str(alias).strip()),
            ),
        )
    return names


def display_name_of(names: dict[str, EntityNames], entity_id: str) -> str:
    """提示词里叫这个实体的名字：有显示名用显示名，没有就是 id 本身。"""
    known = names.get(entity_id)
    return known.display_name if known is not None and known.display_name else entity_id


def labelled_name_of(names: dict[str, EntityNames], entity_id: str) -> str:
    """段落标题里用的「名字 (id)」：让模型把名字和人物契约里的 id 对上；没有显示名时就是 id。"""
    name = display_name_of(names, entity_id)
    return f"{name} ({entity_id})" if name != entity_id else entity_id

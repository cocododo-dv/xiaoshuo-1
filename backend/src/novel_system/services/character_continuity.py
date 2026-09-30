from __future__ import annotations

import json
import re
from typing import Any, Iterable

from novel_system.services.qc_constraints import constraint_terms


CHARACTER_CONTRACT_VERSION = "CHARACTER_CONTRACT_v1"


def build_character_contract_digest(
    *,
    pov_character_id: str | None,
    onstage_character_ids: Iterable[str] | None,
    voice_profile_content: str | None = None,
    relation_profile_content: str | None = None,
    display_names: dict[str, str] | None = None,
) -> str:
    """本场角色的身份契约（CHARACTER_CONTRACT_v1）：id 与权威显示名，按 POV 在前、在场角色依次、同名去重。

    声线卡 / 关系卡已退役（批准 #15，重评 R8）：产品里没有任何地方能写它们，实库两张表都是空的。
    ``voice_profile_content`` / ``relation_profile_content`` 只为还在传它们的调用方留着、一律不读；
    没有卡的场（所有真实作品）契约逐字节与以前相同。
    """
    del voice_profile_content, relation_profile_content
    character_ids = _ordered_character_ids(pov_character_id, onstage_character_ids)
    if not character_ids:
        return ""

    names = display_names or {}
    characters: list[dict[str, Any]] = []
    seen_identity_keys: set[str] = set()
    for character_id in character_ids:
        # 用 StoryCharacter 的权威 display_name；没有才退化到 id。
        # 否则裸 character_id 会被当成角色名写进提示词 → 模型把 id 当人名/线索写进正文。
        display_name = (names.get(character_id) or "").strip() or character_id
        character = {
            "character_id": character_id,
            "display_name": display_name,
            "pronouns": [],
            "role": "",
            "aliases": [],
        }
        identity_keys = _character_identity_keys(character)
        if seen_identity_keys.intersection(identity_keys):
            continue
        characters.append(character)
        seen_identity_keys.update(identity_keys)

    payload: dict[str, Any] = {
        "contract_version": CHARACTER_CONTRACT_VERSION,
        "characters": characters,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def detect_character_pronoun_drift(content: str, contract_digest: str | None) -> list[dict[str, Any]]:
    """已退役（批准 #15，重评 R8）：代词只来自声线卡，卡没有地方能写，人物契约也不再带代词——检查一次都不会命中。

    质检与成稿门那一侧的调用由质量闸门包删掉；这个空壳只为还没合并那一侧的检出能导入而留着，合并后连同调用一起删。
    """
    del content, contract_digest
    return []


def detect_mechanical_required_beat_listing(
    *,
    content: str,
    must_include_text: str | None,
) -> dict[str, Any] | None:
    terms = list(dict.fromkeys(constraint_terms(must_include_text or "")))
    if len(terms) < 2 or not content.strip():
        return None

    tail = content.strip()[-260:]
    matched_terms = [term for term in terms if term in tail]
    if len(matched_terms) < min(3, len(terms)):
        return None

    checklist_markers = (
        "必须包含",
        "需要包含",
        "最后需要包含",
        "以下",
        "清单",
        "required text",
        "must include",
    )
    lower_tail = tail.lower()
    has_marker = any(marker in lower_tail for marker in checklist_markers)
    has_bullet_list = bool(re.search(r"(?m)^\s*[-*•]\s*\S+", tail))
    compact_listing = _terms_appear_in_order(matched_terms, tail) and len(matched_terms) >= 3
    if not (has_marker or has_bullet_list or compact_listing):
        return None

    return {
        "issue_key": "mechanical_required_beat_listing",
        "message": "Required beats appear as a tail-loaded checklist instead of being woven into scene action.",
        "matched_terms": matched_terms,
    }


def _ordered_character_ids(pov_character_id: str | None, onstage_character_ids: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    if isinstance(pov_character_id, str) and pov_character_id.strip():
        values.append(pov_character_id.strip())
    for character_id in onstage_character_ids or []:
        if isinstance(character_id, str) and character_id.strip():
            values.append(character_id.strip())
    return list(dict.fromkeys(values))


def _character_names(character: dict[str, Any]) -> list[str]:
    raw_names = [character.get("display_name"), character.get("character_id")]
    aliases = character.get("aliases")
    if isinstance(aliases, list):
        raw_names.extend(aliases)
    names = [str(name).strip() for name in raw_names if isinstance(name, str) and len(name.strip()) >= 2]
    return list(dict.fromkeys(names))


def _character_identity_keys(character: dict[str, Any]) -> set[str]:
    return {name.casefold() for name in _character_names(character)}


def _terms_appear_in_order(terms: list[str], text: str) -> bool:
    position = -1
    for term in terms:
        next_position = text.find(term, position + 1)
        if next_position < 0:
            return False
        position = next_position
    return True

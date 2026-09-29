from __future__ import annotations

import json
import re
from typing import Any, Iterable


CHARACTER_CONTRACT_VERSION = "CHARACTER_CONTRACT_v1"


def build_character_contract_digest(
    *,
    pov_character_id: str | None,
    onstage_character_ids: Iterable[str] | None,
    voice_profile_content: str | None,
    relation_profile_content: str | None,
    display_names: dict[str, str] | None = None,
) -> str:
    character_ids = _ordered_character_ids(pov_character_id, onstage_character_ids)
    if not character_ids:
        return ""

    names = display_names or {}
    voice_metadata = _extract_voice_metadata(voice_profile_content or "")
    characters: list[dict[str, Any]] = []
    seen_identity_keys: set[str] = set()
    for character_id in character_ids:
        is_pov = character_id == pov_character_id
        metadata = voice_metadata if is_pov else {}
        # 优先用 StoryCharacter 的权威 display_name；其次声线卡元数据；最后才退化到 id。
        # 否则裸 character_id 会被当成角色名写进提示词 → 模型把 id 当人名/线索写进正文。
        display_name = (names.get(character_id) or "").strip() or metadata.get("display_name") or character_id
        character = {
            "character_id": character_id,
            "display_name": display_name,
            "pronouns": metadata.get("pronouns") or [],
            "role": metadata.get("role") or "",
            "aliases": metadata.get("aliases") or [],
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
    relationship_stance = _single_line(relation_profile_content or "")
    if relationship_stance:
        payload["relationship_stance"] = relationship_stance
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def detect_character_pronoun_drift(content: str, contract_digest: str | None) -> list[dict[str, Any]]:
    contract = parse_character_contract_digest(contract_digest)
    if not content.strip() or not contract:
        return []

    issues: list[dict[str, Any]] = []
    character_entries: list[tuple[dict[str, Any], list[str]]] = []
    all_names: list[str] = []
    for character in contract.get("characters", []):
        if not isinstance(character, dict):
            continue
        names = _character_names(character)
        character_entries.append((character, names))
        all_names.extend(names)

    for character, names in character_entries:
        expected = _expected_chinese_pronoun(character.get("pronouns"))
        if expected is None:
            continue
        found = "他" if expected == "她" else "她"
        if not names:
            continue
        own_names = set(names)
        other_names = [name for name in dict.fromkeys(all_names) if name not in own_names]
        if _wrong_pronoun_near_name(content, names, found, other_names):
            display_name = str(character.get("display_name") or names[0])
            issues.append(
                {
                    "issue_key": "character_pronoun_drift",
                    "message": f"{display_name} expects pronoun {expected} but nearby text uses {found}.",
                    "character_id": str(character.get("character_id") or ""),
                    "display_name": display_name,
                    "expected_pronoun": expected,
                    "found_pronoun": found,
                }
            )
    return issues


def detect_mechanical_required_beat_listing(
    *,
    content: str,
    must_include_text: str | None,
) -> dict[str, Any] | None:
    terms = _constraint_terms(must_include_text or "")
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


def parse_character_contract_digest(contract_digest: str | None) -> dict[str, Any] | None:
    if not isinstance(contract_digest, str) or not contract_digest.strip():
        return None
    try:
        payload = json.loads(contract_digest)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("contract_version") != CHARACTER_CONTRACT_VERSION:
        return None
    characters = payload.get("characters")
    if not isinstance(characters, list):
        return None
    return payload


def _ordered_character_ids(pov_character_id: str | None, onstage_character_ids: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    if isinstance(pov_character_id, str) and pov_character_id.strip():
        values.append(pov_character_id.strip())
    for character_id in onstage_character_ids or []:
        if isinstance(character_id, str) and character_id.strip():
            values.append(character_id.strip())
    return list(dict.fromkeys(values))


def _extract_voice_metadata(content: str) -> dict[str, Any]:
    return {
        "display_name": _extract_labeled_value(content, ("角色名", "姓名", "display_name", "name")),
        "pronouns": _extract_pronouns(content),
        "role": _extract_labeled_value(content, ("角色职责", "职责", "身份", "role")),
        "aliases": _extract_aliases(content),
    }


def _extract_labeled_value(content: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[:：]\s*([^\n\r;；,，]+)"
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()
    return ""


def _extract_pronouns(content: str) -> list[str]:
    raw = _extract_labeled_value(content, ("称谓/代词", "代词", "pronouns", "pronoun"))
    if not raw:
        return []
    tokens = [token.strip() for token in re.split(r"[,，、/;；\s]+", raw) if token.strip()]
    allowed = {"她", "他", "TA", "ta", "其", "she", "her", "he", "him"}
    return list(dict.fromkeys(token for token in tokens if token in allowed))


def _extract_aliases(content: str) -> list[str]:
    raw = _extract_labeled_value(content, ("别名", "aliases", "alias"))
    if not raw:
        return []
    return list(dict.fromkeys(token.strip() for token in re.split(r"[,，、/;；\s]+", raw) if token.strip()))


def _single_line(value: str) -> str:
    return " ".join(part.strip() for part in value.splitlines() if part.strip())


def _expected_chinese_pronoun(pronouns: Any) -> str | None:
    if not isinstance(pronouns, list):
        return None
    normalized = {str(pronoun).strip().lower() for pronoun in pronouns if str(pronoun).strip()}
    if "她" in normalized or "she" in normalized or "her" in normalized:
        return "她"
    if "他" in normalized or "he" in normalized or "him" in normalized:
        return "他"
    return None


def _character_names(character: dict[str, Any]) -> list[str]:
    raw_names = [character.get("display_name"), character.get("character_id")]
    aliases = character.get("aliases")
    if isinstance(aliases, list):
        raw_names.extend(aliases)
    names = [str(name).strip() for name in raw_names if isinstance(name, str) and len(name.strip()) >= 2]
    return list(dict.fromkeys(names))


def _character_identity_keys(character: dict[str, Any]) -> set[str]:
    return {name.casefold() for name in _character_names(character)}


def _wrong_pronoun_near_name(content: str, names: list[str], wrong_pronoun: str, other_names: list[str]) -> bool:
    pronoun_pattern = re.compile(rf"{re.escape(wrong_pronoun)}(?!们)")
    for name in names:
        for match in re.finditer(re.escape(name), content):
            window = content[match.end() : match.end() + 80]
            pronoun_match = pronoun_pattern.search(window)
            if pronoun_match is None:
                continue
            if _other_character_name_before(window, pronoun_match.start(), other_names):
                continue
            return True
    return False


def _other_character_name_before(window: str, pronoun_position: int, other_names: list[str]) -> bool:
    prefix = window[:pronoun_position]
    return any(name and name in prefix for name in other_names)


def _constraint_terms(text: str) -> list[str]:
    terms = [term.strip() for term in re.split(r"[,，、;；\n\r]+", text) if len(term.strip()) >= 2]
    return list(dict.fromkeys(terms))


def _terms_appear_in_order(terms: list[str], text: str) -> bool:
    position = -1
    for term in terms:
        next_position = text.find(term, position + 1)
        if next_position < 0:
            return False
        position = next_position
    return True

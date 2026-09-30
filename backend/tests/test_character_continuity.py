from __future__ import annotations

import json

from novel_system.services.character_continuity import (
    build_character_contract_digest,
    detect_mechanical_required_beat_listing,
)

CARD_LESS_CONTRACT = (
    '{"contract_version":"CHARACTER_CONTRACT_v1","characters":['
    '{"character_id":"CHAR_LINYUAN","display_name":"林远","pronouns":[],"role":"","aliases":[]},'
    '{"character_id":"CHAR_SUWAN","display_name":"苏晚","pronouns":[],"role":"","aliases":[]},'
    '{"character_id":"CHAR_GUZHOU","display_name":"CHAR_GUZHOU","pronouns":[],"role":"","aliases":[]}]}'
)


def test_card_less_contract_json_is_unchanged() -> None:
    """声线卡 / 关系卡退役前后，没有卡的场（所有真实作品）契约逐字节相同（重评 R8 的 golden）。"""
    digest = build_character_contract_digest(
        pov_character_id="CHAR_LINYUAN",
        onstage_character_ids=["CHAR_LINYUAN", "CHAR_SUWAN", "林远", "CHAR_GUZHOU"],
        display_names={"CHAR_LINYUAN": "林远", "CHAR_SUWAN": "苏晚"},
    )
    assert digest == CARD_LESS_CONTRACT
    assert build_character_contract_digest(pov_character_id=None, onstage_character_ids=[]) == ""


def test_voice_and_relation_card_content_is_ignored() -> None:
    """还在传卡片内容的调用方拿到的契约与不传时相同：不再从卡里解析代词 / 职责 / 别名 / 关系立场（批准 #15）。"""
    with_cards = build_character_contract_digest(
        pov_character_id="CHAR_LINYUAN",
        onstage_character_ids=["CHAR_LINYUAN", "CHAR_SUWAN", "林远", "CHAR_GUZHOU"],
        voice_profile_content="角色名：林岑\n代词：她\n角色职责：档案修复师\n别名：小林",
        relation_profile_content="林岑与许望互相信任，但在公开真相的时机上有分歧。",
        display_names={"CHAR_LINYUAN": "林远", "CHAR_SUWAN": "苏晚"},
    )
    assert with_cards == CARD_LESS_CONTRACT
    assert "relationship_stance" not in json.loads(with_cards)


def test_build_character_contract_digest_dedupes_by_display_name() -> None:
    """POV 的权威显示名与在场名单里直接写的名字相同 → 只留一个角色。"""
    digest = build_character_contract_digest(
        pov_character_id="CHAR_LINCEN",
        onstage_character_ids=["林岑", "许望", "幸存者阿砚"],
        display_names={"CHAR_LINCEN": "林岑"},
    )

    payload = json.loads(digest)

    assert [character["character_id"] for character in payload["characters"]] == ["CHAR_LINCEN", "许望", "幸存者阿砚"]
    assert [character["display_name"] for character in payload["characters"]] == ["林岑", "许望", "幸存者阿砚"]


def test_detect_mechanical_required_beat_listing_flags_tail_loaded_checklist() -> None:
    issue = detect_mechanical_required_beat_listing(
        content=(
            "林岑先听见雾堤下的回声，随后把证据封进纸袋。\n\n"
            "最后需要包含：盐钟残片、潮汐记录、幸存者名单。"
        ),
        must_include_text="盐钟残片；潮汐记录；幸存者名单",
    )

    assert issue == {
        "issue_key": "mechanical_required_beat_listing",
        "message": "Required beats appear as a tail-loaded checklist instead of being woven into scene action.",
        "matched_terms": ["盐钟残片", "潮汐记录", "幸存者名单"],
    }


def test_build_character_contract_digest_uses_authoritative_display_names_over_raw_id() -> None:
    """修复裸 id 泄漏：用 StoryCharacter 权威名而非 character_id。"""
    digest = build_character_contract_digest(
        pov_character_id="CHAR_2457AE17E4",
        onstage_character_ids=None,
        display_names={"CHAR_2457AE17E4": "林深"},
    )
    payload = json.loads(digest)
    assert payload["characters"][0]["display_name"] == "林深"
    assert payload["characters"][0]["character_id"] == "CHAR_2457AE17E4"
    # 裸 id 不得当作角色名（否则模型把 id 写进正文）
    assert payload["characters"][0]["display_name"] != "CHAR_2457AE17E4"


def test_build_character_contract_digest_falls_back_to_id_without_name_source() -> None:
    """没有权威名时退化到 id（对照：修复仅在有名源时生效）。"""
    digest = build_character_contract_digest(
        pov_character_id="CHAR_NONAME",
        onstage_character_ids=None,
    )
    payload = json.loads(digest)
    assert payload["characters"][0]["display_name"] == "CHAR_NONAME"

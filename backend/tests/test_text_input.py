"""写入口的正文输入校验（``services/text_input.py``，B04-32）：像乱码 / 没解码的文字 400 ``TEXT_ENCODING_INVALID``，
旧的 ``{{backfill …}}`` 占位只留可见文字。"""

from __future__ import annotations

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.text_input import clean_backfill_markers, validate_user_text_payload


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("林昭把旧信�放回案卷", "replacement_character"),
        ("雨城的钟响了???下", "question_mark_placeholder"),
        ("案卷\u0085编号", "mojibake_control_character"),
        ("Ã©Ã¨Ã  Ã§a", "mojibake_marker"),
    ],
)
def test_undecoded_text_is_rejected_with_the_field_path(value: str, reason: str) -> None:
    with pytest.raises(DomainError) as error:
        validate_user_text_payload({"beats_json": ["雨城的钟响了三下", value]}, field_prefix="scene")
    assert error.value.code == "TEXT_ENCODING_INVALID"
    assert error.value.status_code == 400
    assert error.value.details == {"field": "scene.beats_json[1]", "reason": reason}


def test_ordinary_text_passes() -> None:
    validate_user_text_payload(
        {"scene_goal": "林昭把旧信放回案卷。", "link": "https://example.com/Ã©Ã¨Ã", "count": 3, "empty": "  "},
        field_prefix="scene",
    )


def test_backfill_markers_keep_only_the_visible_text() -> None:
    assert clean_backfill_markers('雨城{{backfill id=F001 text="旧信"}}还在。') == "雨城旧信还在。"
    assert clean_backfill_markers(None) is None


def test_chapter_route_rejects_undecoded_text(client) -> None:
    response = client.post(
        "/api/v1/chapters",
        json={"chapter_id": "CH930", "planned_scene_count": 1, "chapter_goal": "旧信???"},
        headers={"X-Idempotency-Key": "chapter-corrupted-text"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TEXT_ENCODING_INVALID"

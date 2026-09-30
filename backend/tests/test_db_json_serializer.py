"""JSON 列存原样 UTF-8（B12-13 / X01-07）。

以前 JSON 列按 ``json.dumps`` 的默认写法存：中文全写成 ``\\uXXXX``，同样的内容约大 1.6 倍。现在新写入存原样的
UTF-8；这些用例钉住「读的人什么都察觉不到」：两种写法读回同一个 Python 对象（键的顺序也一样）、哈希相同、
``json_extract`` 取到同一个值，编不成 UTF-8 的单个代理项照旧转义存。
"""

from __future__ import annotations

import json

import pytest

from novel_system.db import session as db_session
from novel_system.db.models import IdempotencyKey
from novel_system.services.hash_engine import sha256_json_normalized, sha256_json_plain

PAYLOAD = {
    "ok": True,
    "data": {
        "title": "旧信",
        "scenes": [{"place": "雨城", "words": 1200, "ratio": 0.5}],
        "note": "林昭说：“到了。”\n\t案卷 第二行 😀",
        "空键": None,
    },
    "error": None,
}


def _raw_response_json(key: str) -> str:
    with db_session.engine().connect() as connection:
        return connection.exec_driver_sql(
            "SELECT response_json FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        ).scalar_one()


def _store(key: str, value) -> None:
    with db_session.SessionLocal() as session:
        session.add(IdempotencyKey(idempotency_key=key, request_hash="request-hash", response_json=value))
        session.commit()


def _load(key: str):
    with db_session.SessionLocal() as session:
        return session.get(IdempotencyKey, key).response_json


def test_new_rows_store_raw_utf8_and_read_back_the_same_value() -> None:
    _store("key-new", PAYLOAD)

    raw = _raw_response_json("key-new")
    assert "旧信" in raw and "雨城" in raw
    assert "\\u" not in raw
    assert len(raw.encode("utf-8")) < len(json.dumps(PAYLOAD).encode("utf-8"))
    assert json.loads(raw) == PAYLOAD
    assert _load("key-new") == PAYLOAD


def test_rows_in_the_old_escaped_form_read_back_to_the_same_value_and_hash() -> None:
    with db_session.engine().begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO idempotency_keys "
            "(idempotency_key, request_hash, status, response_json, attempt_no, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("key-old", "request-hash", "completed", json.dumps(PAYLOAD), 1, "2026-09-01", "2026-09-01"),
        )
    _store("key-new", PAYLOAD)
    assert "\\u65e7\\u4fe1" in _raw_response_json("key-old")
    assert "\\u" not in _raw_response_json("key-new")

    old, new = _load("key-old"), _load("key-new")

    assert old == new == PAYLOAD
    assert list(old["data"]) == list(new["data"]) == list(PAYLOAD["data"])
    assert sha256_json_plain(old) == sha256_json_plain(new)
    assert sha256_json_normalized(old) == sha256_json_normalized(new)
    with db_session.engine().connect() as connection:
        titles = connection.exec_driver_sql(
            "SELECT json_extract(response_json, '$.data.title') FROM idempotency_keys ORDER BY idempotency_key"
        ).scalars().all()
    assert titles == ["旧信", "旧信"]


def test_a_lone_surrogate_is_still_stored_escaped() -> None:
    """JSON 里合法的单个代理项（模型输出或请求体里的 ``"\\ud83d"``）编不成 UTF-8：照旧转义存，事务不失败。"""
    value = {"text": json.loads('"雨城\\ud83d"')}

    _store("key-lone", value)

    assert "\\ud83d" in _raw_response_json("key-lone")
    assert _load("key-lone") == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        "中",
        1.25,
        [1, "二", {"三": [None, True, False]}],
        {"控制符": "\x00\x1f", "引号": '"\\'},
        {"b": 1, "a": 2, "中": 3},
    ],
)
def test_serializer_parses_back_to_what_the_default_serializer_gives(value) -> None:
    ours = json.loads(db_session.json_column_dumps(value))
    default = json.loads(json.dumps(value))

    assert ours == default
    if isinstance(default, dict):
        assert list(ours) == list(default)

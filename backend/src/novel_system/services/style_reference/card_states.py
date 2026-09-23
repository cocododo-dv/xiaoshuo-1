"""风格参考 v3 — 文风卡行状态：矩阵里的 ✓（pinned，永远带上）/ ✗（excluded，不再用）（台账 U3 / E8）。

以前在矩阵里对一条发现点 ✗ 会把整份画像打回 draft（绑定它的每个作品随即没了风格参考），✓ 什么也不做，
👍/👎 单作者永远到不了调档阈值。现在只有一件事：原子地改 ``profile_json.card_line_states`` 里这一句的状态——
不重新合成、不改画像状态、不写待办。渲染（P4）按状态取舍卡片行。

写法用 SQLite 的 ``json_set`` / ``json_remove`` 只改这一个键（学习作业的定稿可能同时在写这一行的
``profile_json``：定稿先拿写锁再重读行状态合并，这里是单条 UPDATE，互不覆盖）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceProfile, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.card import (
    LINE_STATES,
    card_from_profile_json,
    line_states_from_profile_json,
)

LINE_ID_RE = re.compile(r"^cl_[0-9a-f]{12}$")
CARD_LINE_NOT_FOUND_CODE = "STYLE_REFERENCE_CARD_LINE_NOT_FOUND"
CARD_LINE_STATE_INVALID_CODE = "STYLE_REFERENCE_CARD_LINE_STATE_INVALID"
PROFILE_HAS_NO_CARD_CODE = "STYLE_REFERENCE_PROFILE_HAS_NO_CARD"


def set_card_line_state(session: Session, profile_id: str, line_id: str, state: str | None) -> dict[str, Any]:
    """把文风卡一句的状态设为 ``pinned`` / ``excluded``，或清掉（``None``）。只 flush。

    画像不存在 404；状态不合法 400；画像还没有文风卡（v3 之前的画像）409；要设状态的句子不在卡上 404
    （清状态不要求句子还在——重新学习后消失的句子也能清掉它残留的状态）。
    """
    if state is not None and state not in LINE_STATES:
        raise DomainError(
            CARD_LINE_STATE_INVALID_CODE,
            f"state must be one of {LINE_STATES} or null",
            status_code=400,
            details={"state": state},
        )
    profile = session.execute(
        select(StyleReferenceProfile)
        .where(StyleReferenceProfile.profile_id == profile_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    card = card_from_profile_json(profile.profile_json)
    if card is None:
        raise DomainError(
            PROFILE_HAS_NO_CARD_CODE,
            "这份画像还没有文风卡:先对它的参考书「学习文风」,再逐句标记。",
            status_code=409,
            details={
                "profile_id": profile_id,
                "author_action": {"action": "learn_style", "view": "styleref", "book_id": profile.book_id},
            },
        )
    known = {line.line_id for _dim, line in card.all_lines()}
    if not LINE_ID_RE.match(str(line_id or "")) or (state is not None and line_id not in known):
        raise DomainError(
            CARD_LINE_NOT_FOUND_CODE,
            f"card line {line_id!r} is not on this profile's style card",
            status_code=404,
            details={"profile_id": profile_id, "line_id": line_id},
        )
    path = f'$."{line_id}"'
    column = func.coalesce(StyleReferenceProfile.profile_json, "{}")
    if state is None:
        value = func.json_remove(column, f"$.card_line_states.{path[2:]}")
    else:
        states = func.coalesce(func.json_extract(column, "$.card_line_states"), func.json("{}"))
        value = func.json_set(column, "$.card_line_states", func.json_set(states, path, state))
    session.execute(
        update(StyleReferenceProfile)
        .where(StyleReferenceProfile.profile_id == profile_id)
        .values(profile_json=value, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire(profile, ["profile_json", "updated_at"])
    return {
        "profile_id": profile_id,
        "line_id": line_id,
        "state": state,
        "card_line_states": line_states_from_profile_json(profile.profile_json),
        "status": profile.status,
    }


__all__ = [
    "CARD_LINE_NOT_FOUND_CODE",
    "CARD_LINE_STATE_INVALID_CODE",
    "LINE_ID_RE",
    "PROFILE_HAS_NO_CARD_CODE",
    "set_card_line_state",
]

"""风格参考 · 画像的禁用词（``style_reference_banned_terms``）：列出、登记、删除的业务规则。

- ``generation``：起草时不许出现（进红线、抄袭门的受保护专名提醒）；``extraction``：学习时滤掉含这个词的段落；
- 来源 ``user``（作者录入）/ ``preset``（预置，不能删）/ ``protected_auto``（学习作业识别的本书专名）：作者删掉一个
  自动专名（多半是误收的日常词）时记下来，重新学习不再把它加回来；
- 同一画像 + 同一个词 + 同一个作用域只有一行：重复登记返回既有的那行（幂等友好，给了替换提示就更新提示）。

路由（``api/routes/style_reference/profiles.py``）只做 HTTP 与幂等包装；这里 flush 不 commit。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import profile_not_found
from novel_system.services.style_reference.protected_terms import PROTECTED_SOURCE, dismiss_protected_term
from novel_system.services.style_reference.repository import StyleReferenceRepository

BANNED_TERM_SCOPES = ("generation", "extraction")
BANNED_TERM_INVALID_CODE = "STYLE_REFERENCE_BANNED_TERM_INVALID"
BANNED_TERM_NOT_FOUND_CODE = "STYLE_REFERENCE_BANNED_TERM_NOT_FOUND"
BANNED_TERM_PROTECTED_CODE = "STYLE_REFERENCE_BANNED_TERM_PROTECTED"
SOURCE_USER = "user"
SOURCE_PRESET = "preset"


def serialize_banned_term(term: Any) -> dict[str, Any]:
    return {
        "term_id": term.term_id,
        "profile_id": term.profile_id,
        "term": term.term,
        "replacement_hint": term.replacement_hint,
        "source": term.source,
        "scope": term.scope,
        "created_at": term.created_at,
    }


def _require_profile(repo: StyleReferenceRepository, profile_id: str) -> None:
    if repo.get_profile(profile_id) is None:
        raise profile_not_found(profile_id)


def list_banned_terms(session: Session, profile_id: str, *, scope: str | None = None) -> list[dict[str, Any]]:
    """一份画像的禁用词（``scope`` 缺省列全部）；画像不存在 404。"""
    repo = StyleReferenceRepository(session)
    _require_profile(repo, profile_id)
    return [serialize_banned_term(term) for term in repo.list_banned_terms(profile_id, scope=scope)]


def create_banned_term(
    session: Session,
    profile_id: str,
    *,
    term: str,
    scope: str,
    replacement_hint: str | None = None,
) -> dict[str, Any]:
    """登记一个作者禁用词：空词 / 不认识的作用域 400，画像不存在 404；已有同一行 → 返回它（``created=False``）。"""
    term_text = str(term or "").strip()
    scope_name = str(scope or "").strip()
    if not term_text:
        raise DomainError(BANNED_TERM_INVALID_CODE, "term must be non-empty", status_code=400)
    if scope_name not in BANNED_TERM_SCOPES:
        raise DomainError(BANNED_TERM_INVALID_CODE, f"scope must be one of {BANNED_TERM_SCOPES}", status_code=400)
    repo = StyleReferenceRepository(session)
    _require_profile(repo, profile_id)
    existing = repo.find_banned_term(profile_id, term_text, scope_name)
    if existing is not None:
        if replacement_hint is not None:
            existing.replacement_hint = replacement_hint
        return {"term": serialize_banned_term(existing), "created": False}
    row = repo.create_banned_term(
        term_id=f"sr_term_{uuid.uuid4().hex[:12]}",
        profile_id=profile_id,
        term=term_text,
        replacement_hint=replacement_hint,
        source=SOURCE_USER,
        scope=scope_name,
    )
    return {"term": serialize_banned_term(row), "created": True}


def delete_banned_term(session: Session, term_id: str) -> dict[str, Any]:
    """删一个禁用词：不存在 404；预置的不能删 400；删的是自动识别的专名时记下「作者删过」（重新学习不再加回）。"""
    repo = StyleReferenceRepository(session)
    row = repo.get_banned_term(term_id)
    if row is None:
        raise DomainError(BANNED_TERM_NOT_FOUND_CODE, f"banned term {term_id!r} not found", status_code=404)
    if row.source == SOURCE_PRESET:
        raise DomainError(BANNED_TERM_PROTECTED_CODE, "preset banned terms cannot be deleted", status_code=400)
    if row.source == PROTECTED_SOURCE and row.profile_id:
        dismiss_protected_term(session, str(row.profile_id), str(row.term))
    repo.delete_banned_term(term_id)
    return {"term_id": term_id, "deleted": True}


__all__ = [
    "BANNED_TERM_SCOPES",
    "create_banned_term",
    "delete_banned_term",
    "list_banned_terms",
    "serialize_banned_term",
]

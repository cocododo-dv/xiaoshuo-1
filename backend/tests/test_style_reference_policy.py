from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.services.style_reference import errors
from novel_system.services.style_reference.errors import CloudPolicyBlockedError
from novel_system.services.style_reference.policy import (
    cloud_llm_allowed,
    ensure_cloud_llm_allowed,
)


def _book(*, cloud_policy: str, rights_declaration: object) -> SimpleNamespace:
    return SimpleNamespace(
        book_id="sr_book_policy",
        cloud_policy=cloud_policy,
        stats_json={"rights_declaration": rights_declaration},
    )


@pytest.mark.parametrize(
    "rights_declaration",
    [
        None,
        {},
        {"declared": False, "send_rights": True},
        {"declared": True, "send_rights": False},
        {"declared": "true", "send_rights": True},
        {"declared": True, "send_rights": "true"},
        {"declared": 1, "send_rights": True},
        {"declared": True, "send_rights": 1},
    ],
    ids=(
        "missing",
        "empty",
        "declared-false",
        "send-false",
        "declared-string",
        "send-string",
        "declared-int",
        "send-int",
    ),
)
def test_nonlocal_policy_requires_strict_declared_send_rights(
    rights_declaration: object,
) -> None:
    book = _book(
        cloud_policy="segments_only",
        rights_declaration=rights_declaration,
    )

    assert cloud_llm_allowed(book) is False

    expected_error = errors.CloudSendRightsBlockedError
    with pytest.raises(expected_error) as caught:
        ensure_cloud_llm_allowed(book, operation="synthesize_profile")

    err = caught.value
    assert err.code == "STYLE_REFERENCE_SEND_RIGHTS_REQUIRED"
    assert err.status_code == 409
    assert err.details["book_id"] == "sr_book_policy"
    assert err.details["operation"] == "synthesize_profile"
    assert err.details["cloud_policy"] == "segments_only"
    assert err.details["author_action"]["action"] == "redeclare_send_rights"


@pytest.mark.parametrize("cloud_policy", ["segments_only", "allow_full_cloud"])
def test_nonlocal_policy_allows_explicit_declared_send_rights(
    cloud_policy: str,
) -> None:
    book = _book(
        cloud_policy=cloud_policy,
        rights_declaration={"declared": True, "send_rights": True},
    )

    assert cloud_llm_allowed(book) is True
    ensure_cloud_llm_allowed(book, operation="preview")


def test_local_only_uses_cloud_policy_error_even_with_send_rights() -> None:
    book = _book(
        cloud_policy="local_only",
        rights_declaration={"declared": True, "send_rights": True},
    )

    assert cloud_llm_allowed(book) is False

    with pytest.raises(CloudPolicyBlockedError) as caught:
        ensure_cloud_llm_allowed(book, operation="start_extract_run")

    err = caught.value
    assert err.code == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert err.details["book_id"] == "sr_book_policy"
    assert err.details["operation"] == "start_extract_run"
    assert err.details["cloud_policy"] == "local_only"


@pytest.mark.parametrize("cloud_policy", ["", "legacy_cloud", " segments_only"])
def test_unknown_cloud_policy_is_fail_closed_even_with_send_rights(
    cloud_policy: str,
) -> None:
    book = _book(
        cloud_policy=cloud_policy,
        rights_declaration={"declared": True, "send_rights": True},
    )

    assert cloud_llm_allowed(book) is False

    expected_error = errors.CloudPolicyInvalidError
    with pytest.raises(expected_error) as caught:
        ensure_cloud_llm_allowed(book, operation="generate_preview")

    err = caught.value
    assert err.code == "STYLE_REFERENCE_CLOUD_POLICY_INVALID"
    assert err.status_code == 409
    assert err.details["book_id"] == "sr_book_policy"
    assert err.details["operation"] == "generate_preview"
    assert err.details["cloud_policy"] == cloud_policy
    assert err.details["author_action"]["action"] == "review_cloud_policy"


def test_none_book_keeps_caller_not_found_behavior() -> None:
    assert cloud_llm_allowed(None) is True
    ensure_cloud_llm_allowed(None, operation="caller_handles_not_found")


# ---------------------------------------------------------------------------
# 2026-09-24 清理 C7：轻量现解析里，绑定指向非 active 画像 → 降级（不是未绑定）
# ---------------------------------------------------------------------------


def _scope(project_id: str, *, scene_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(project_id=project_id, scene_id=scene_id, pov_character_id=None, onstage_chars_json=[])


def test_light_live_policy_degrades_when_the_bound_profile_is_not_active(session) -> None:
    from novel_system.db.models import StoryProject, StyleReferenceProfile
    from novel_system.services.style_policy import (
        MODE_DEGRADED,
        MODE_LIVE,
        PROFILE_NOT_ACTIVE_CODE,
        style_policy_live,
    )
    from tests.style_reference_factories import make_binding, make_book, make_profile, synthetic_paragraphs

    project_id = "PRJ_C7"
    session.add(StoryProject(project_id=project_id, title="C7", outline_text=""))
    book_id = make_book(session, "book_c7", paragraphs=synthetic_paragraphs(6))
    archived = make_profile(session, book_id, profile_id="profile_c7_archived", status="archived")
    binding = make_binding(session, archived, binding_id="bind_c7_project", scope_ref_id=project_id)
    session.commit()

    policy = style_policy_live(session, _scope(project_id), freeze_contract=False)
    assert policy.bound is False and policy.mode == MODE_DEGRADED
    assert policy.error_code == PROFILE_NOT_ACTIVE_CODE
    # 带上是哪份画像 / 哪条绑定 / 哪本书：界面能说「画像未启用」，抄袭门能报 unavailable
    assert (policy.profile_id, policy.binding_id, policy.book_id) == (archived, binding.binding_id, book_id)
    assert policy.audit()["error_code"] == PROFILE_NOT_ACTIVE_CODE

    # 同一目标上另有指向 active 画像的绑定：与冻结路径一样只在可用的绑定里选，不降级
    active = make_profile(session, book_id, profile_id="profile_c7_active", status="active")
    usable = make_binding(session, active, binding_id="bind_c7_global", scope="global", scope_ref_id=None)
    session.commit()
    bound = style_policy_live(session, _scope(project_id), freeze_contract=False)
    assert bound.bound and bound.mode == MODE_LIVE and bound.binding_id == usable.binding_id

    # 画像启用后同一条绑定就是生效的那条
    session.get(StyleReferenceProfile, archived).status = "active"
    session.commit()
    revived = style_policy_live(session, _scope(project_id), freeze_contract=False)
    assert revived.bound and revived.binding_id == binding.binding_id and revived.profile_id == archived

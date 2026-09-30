from __future__ import annotations

import pytest


def _validation_issues(response) -> list[dict[str, str]]:
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    return payload["error"]["details"]["issues"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "patch",
            "/api/v1/author-drafts/missing",
            {"content": "正文", "base_revision_no": 1, "server_owned": True},
        ),
        (
            "post",
            "/api/v1/author-drafts/missing/promote-canonical",
            {"base_revision_no": 1, "unknown_reconciliation": "skip"},
        ),
        (
            "post",
            "/api/v1/author-drafts/missing/proposals/generate-set",
            {"instruction": "给三种方案", "count": 1000},
        ),
        (
            "post",
            "/api/v1/author-drafts/missing/proposals/generate-set",
            {"instruction": "续写", "target_range": {"unit": "text", "source_excerpt": "正文"}},
        ),
    ],
)
def test_author_draft_write_routes_forbid_unknown_fields(
    client,
    method: str,
    path: str,
    payload: dict,
) -> None:
    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 422, response.text
    assert any(item["type"] == "extra_forbidden" for item in _validation_issues(response))


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/v1/author-drafts/missing/promote-canonical",
            {
                "base_revision_no": 1,
                "accepted_warning_codes": [f"warning_{index}" for index in range(65)],
            },
        ),
        (
            "/api/v1/author-drafts/missing/promote-canonical",
            {
                "base_revision_no": 1,
                "accepted_warning_codes": ["w" * 129],
            },
        ),
        (
            "/api/v1/author-drafts/missing/promote-canonical",
            {
                "base_revision_no": 1,
                "accepted_warning_codes": [1],
            },
        ),
    ],
)
def test_canonical_promotion_bounds_accepted_warning_codes(
    client,
    path: str,
    payload: dict,
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422, response.text
    assert _validation_issues(response)
    assert "warning_64" not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "patch",
            "/api/v1/author-drafts/missing",
            {"content": "x" * 2_000_001, "base_revision_no": 1},
        ),
        (
            "patch",
            "/api/v1/author-drafts/missing",
            {"content": "正文", "base_revision_no": True},
        ),
        (
            "post",
            "/api/v1/author-drafts/missing/proposals/generate-set",
            {"instruction": "x" * 8_001},
        ),
    ],
)
def test_author_draft_write_routes_enforce_type_and_text_bounds(
    client,
    method: str,
    path: str,
    payload: dict,
) -> None:
    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 422, response.text
    assert _validation_issues(response)
    # The validation envelope must not echo a rejected manuscript-sized input.
    assert "x" * 256 not in response.text

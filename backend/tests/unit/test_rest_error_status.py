import pytest

from backend.app.errors import rest_error_status


@pytest.mark.parametrize(
    "error_code",
    (
        "legacy_media_cutover_conflict",
        "media_editing_not_open",
        "media_install_in_progress",
        "media_policy_changed",
        "media_state_changed",
    ),
)
def test_rest_error_status_matches_mcp_conflicts_for_context_media(
    error_code: str,
) -> None:
    assert rest_error_status(error_code) == "conflict"


@pytest.mark.parametrize(
    "error_code",
    (
        "media_owner_mismatch",
        "media_quota_exceeded",
        "media_markdown_policy_invalid",
    ),
)
def test_rest_error_status_matches_mcp_policy_for_context_media(
    error_code: str,
) -> None:
    assert rest_error_status(error_code) == "rejected_by_policy"

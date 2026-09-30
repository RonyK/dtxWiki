from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.conversion_failure_policy import (
    LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES,
    decide_conversion_manual_retry,
    is_conversion_timeout_error,
    is_fail_closed_conversion_error,
)
from backend.services.domain_read import _library_conversion_state_payload


@pytest.mark.parametrize(
    "error_code",
    (
        "converter_timeout",
        "converter_timed_out",
        "conversion_total_timeout",
    ),
)
def test_timeout_errors_are_shared_fail_closed_policy(error_code: str) -> None:
    assert is_conversion_timeout_error(error_code) is True
    assert is_fail_closed_conversion_error(error_code) is True


@pytest.mark.parametrize(
    "error_code",
    (
        "active_content_denied",
        "encrypted_document",
        "external_relationship_denied",
        "invalid_format",
        "invalid_ooxml",
        "container_ratio_exceeded",
        "xml_depth_exceeded",
    ),
)
def test_security_errors_are_shared_fail_closed_policy(error_code: str) -> None:
    assert is_conversion_timeout_error(error_code) is False
    assert is_fail_closed_conversion_error(error_code) is True


@pytest.mark.parametrize(
    ("status", "error_code"),
    (
        ("timed_out", "converter_timed_out"),
        ("timed_out", "conversion_total_timeout"),
        ("failed", "container_ratio_exceeded"),
        ("failed", "xml_depth_exceeded"),
        ("failed", "invalid_ooxml"),
    ),
)
def test_read_projection_never_marks_fail_closed_error_retryable(
    status: str,
    error_code: str,
) -> None:
    payload = _library_conversion_state_payload(
        SimpleNamespace(processing_status=status),
        attempt=SimpleNamespace(
            error_code=error_code,
            conversion_detail=SimpleNamespace(
                manual_retry_disposition="denied",
                disposition_reason="policy_denied",
            ),
        ),
    )

    assert payload.retryable is False


def test_read_projection_uses_durable_allowed_disposition_as_authority() -> None:
    payload = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            # The same code is retryable for a worker infrastructure failure
            # and non-retryable when a converter process reports it. The
            # durable decision, not this ambiguous code, is authoritative.
            error_code="converter_process_failed",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition="allowed",
                disposition_reason="error_retryable",
            ),
        ),
    )

    assert payload.retryable is True


def test_read_projection_uses_durable_denied_disposition_for_input_limit() -> None:
    payload = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            error_code="input_limit_exceeded",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition="denied",
                disposition_reason="error_not_retryable",
            ),
        ),
    )

    assert payload.retryable is False


@pytest.mark.parametrize(
    ("disposition", "reason"),
    (("allowed", None), (None, "error_retryable"), ("unknown", "legacy")),
)
def test_read_projection_fails_closed_for_incomplete_durable_decision(
    disposition: str | None,
    reason: str | None,
) -> None:
    payload = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            error_code="converter_transport_failed",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition=disposition,
                disposition_reason=reason,
            ),
        ),
    )

    assert payload.retryable is False


def test_legacy_null_disposition_uses_closed_allowlist() -> None:
    assert "converter_transport_failed" in LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES
    allowed = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            error_code="converter_transport_failed",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition=None,
                disposition_reason=None,
            ),
        ),
    )
    ambiguous = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            error_code="converter_process_failed",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition=None,
                disposition_reason=None,
            ),
        ),
    )
    unknown = _library_conversion_state_payload(
        SimpleNamespace(processing_status="failed"),
        attempt=SimpleNamespace(
            error_code="new_unregistered_failure",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition=None,
                disposition_reason=None,
            ),
        ),
    )
    status_mismatch = _library_conversion_state_payload(
        SimpleNamespace(processing_status="timed_out"),
        attempt=SimpleNamespace(
            error_code="converter_transport_failed",
            conversion_detail=SimpleNamespace(
                manual_retry_disposition=None,
                disposition_reason=None,
            ),
        ),
    )

    assert allowed.retryable is True
    assert ambiguous.retryable is False
    assert unknown.retryable is False
    assert status_mismatch.retryable is False


@pytest.mark.parametrize(
    ("error_retryable", "policy_stops", "expected"),
    (
        (True, False, ("allowed", "error_retryable")),
        (False, False, ("denied", "error_not_retryable")),
        (True, True, ("denied", "policy_denied")),
    ),
)
def test_failure_time_decision_combines_error_provenance_and_policy(
    error_retryable: bool,
    policy_stops: bool,
    expected: tuple[str, str],
) -> None:
    decision = decide_conversion_manual_retry(
        error_retryable=error_retryable,
        policy_stops=policy_stops,
    )

    assert (decision.disposition, decision.reason) == expected


def test_read_projection_fails_closed_without_attempt_error_provenance() -> None:
    payload = _library_conversion_state_payload(
        SimpleNamespace(processing_status="timed_out"),
        attempt=None,
    )

    assert payload.retryable is False

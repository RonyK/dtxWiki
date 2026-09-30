"""Shared fail-closed classification for Library conversion failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_TIMEOUT_ERROR_CODES = frozenset(
    {
        "converter_timeout",
        "converter_timed_out",
        "conversion_total_timeout",
    }
)
_SECURITY_HARD_STOP_ERROR_CODES = frozenset(
    {
        "active_content_denied",
        "encrypted_document",
        "external_relationship_denied",
        "invalid_format",
        "invalid_ooxml",
    }
)
_SECURITY_HARD_STOP_ERROR_PREFIXES = ("container_", "xml_")
LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES = frozenset(
    {
        # These codes are emitted by the orchestrator with retryable=True.
        # They are the only historical NULL-disposition failures for which the
        # retry provenance can be recovered without guessing. Provider error
        # codes are deliberately absent: the same code can be emitted with
        # different retryability, which is why new rows persist the decision.
        "converter_materialization_failed",
        "converter_transport_failed",
        "worker_failed",
    }
)

ManualRetryDisposition = Literal["allowed", "denied"]


@dataclass(frozen=True, slots=True)
class ConversionManualRetryDecision:
    disposition: ManualRetryDisposition
    reason: str


def is_conversion_timeout_error(error_code: str) -> bool:
    """Return whether an error represents an indeterminate timeout."""

    return error_code in _TIMEOUT_ERROR_CODES


def is_fail_closed_conversion_error(error_code: str) -> bool:
    """Return whether retry and provider fallback must both be denied."""

    return (
        is_conversion_timeout_error(error_code)
        or error_code in _SECURITY_HARD_STOP_ERROR_CODES
        or error_code.startswith(_SECURITY_HARD_STOP_ERROR_PREFIXES)
    )


def decide_conversion_manual_retry(
    *,
    error_retryable: bool,
    policy_stops: bool,
) -> ConversionManualRetryDecision:
    """Capture the manual-retry decision while failure provenance is present."""

    if policy_stops:
        return ConversionManualRetryDecision("denied", "policy_denied")
    if error_retryable:
        return ConversionManualRetryDecision("allowed", "error_retryable")
    return ConversionManualRetryDecision("denied", "error_not_retryable")


def is_retryable_conversion_failure(
    *,
    status: str,
    error_code: str | None,
    manual_retry_disposition: str | None,
    disposition_reason: str | None,
) -> bool:
    """Read the durable retry decision, with a narrow legacy fallback.

    New terminal failures always persist both disposition fields.  NULL is
    reserved for pre-migration rows; those rows use a closed allowlist so an
    unknown or ambiguously emitted error code is denied instead of guessed.
    """

    if status not in {"failed", "timed_out"}:
        return False
    if manual_retry_disposition is not None or disposition_reason is not None:
        return (
            manual_retry_disposition == "allowed"
            and isinstance(disposition_reason, str)
            and 0 < len(disposition_reason) <= 128
        )
    return (
        status == "failed"
        and error_code is not None
        and error_code in LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES
        and not is_fail_closed_conversion_error(error_code)
    )


__all__ = [
    "ConversionManualRetryDecision",
    "LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES",
    "decide_conversion_manual_retry",
    "is_conversion_timeout_error",
    "is_fail_closed_conversion_error",
    "is_retryable_conversion_failure",
]

from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse

from backend.schemas.common import (
    EnvelopeWarning,
    RESTConflictErrorBody,
    RESTErrorBody,
    RESTResultEnvelope,
)


def build_rest_error_response(
    error_code: str,
    message: str,
    *,
    status_code: int,
    request_id: str | UUID | None = None,
    warnings: list[EnvelopeWarning] | list[dict[str, str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    envelope_kwargs: dict[str, Any] = {
        "result_status": rest_error_status(error_code),
        "error_code": error_code,
        "warnings": warnings or [],
    }
    if request_id is not None:
        envelope_kwargs["request_id"] = request_id
    envelope = RESTResultEnvelope(**envelope_kwargs)
    payload: dict[str, Any] = {
        "envelope": envelope,
        "error_code": error_code,
        "message": message,
        "request_id": envelope.request_id,
        "warnings": warnings or [],
    }
    if extra:
        payload.update(extra)

    body_model = RESTConflictErrorBody if extra else RESTErrorBody
    body = body_model(**payload)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
    )


def rest_error_status(error_code: str) -> str:
    if error_code in {
        "disambiguation_required",
        "legacy_media_cutover_conflict",
        "media_editing_not_open",
        "media_install_in_progress",
        "media_policy_changed",
        "media_state_changed",
        "page_name_occupied",
        "stale_base_version",
        "stale_context_draft",
    }:
        return "conflict"
    if error_code in {
        "bundle_expired",
        "media_markdown_policy_invalid",
        "media_owner_mismatch",
        "media_quota_exceeded",
        "rejected_by_policy",
        "scope_denied",
        "unsupported_retrieval_mode",
    }:
        return "rejected_by_policy"
    return "failed"

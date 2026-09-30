"""Stable REST mappings for Shared Media admission failures."""

from fastapi.responses import JSONResponse

from backend.app.errors import build_rest_error_response
from backend.media.domain import (
    MediaStagingBytesExceededError,
    MediaStagingError,
    MediaStagingLimitExceededError,
)


def media_staging_error_response(exc: MediaStagingError) -> JSONResponse:
    if isinstance(exc, MediaStagingLimitExceededError):
        status_code = 429
        message = "Media staging is temporarily rate limited."
        retry_after_seconds = 60
    elif isinstance(exc, MediaStagingBytesExceededError):
        status_code = 413
        message = "Media staging exceeds the active byte limit."
        retry_after_seconds = None
    else:  # pragma: no cover - closed taxonomy guard
        raise TypeError("Unsupported media staging failure")
    response = build_rest_error_response(
        error_code=exc.error_code,
        message=message,
        status_code=status_code,
    )
    if retry_after_seconds is not None:
        response.headers["Retry-After"] = str(retry_after_seconds)
    return response


__all__ = ["media_staging_error_response"]

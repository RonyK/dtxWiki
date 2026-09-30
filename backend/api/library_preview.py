"""Typed Library Preview manifest and selected-artifact transport routes."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.api.middleware.security_headers import (
    CSP_SANDBOXED_DOCUMENT_STATE_KEY,
)
from backend.app.errors import build_rest_error_response
from backend.media.application import if_none_match_matches
from backend.schemas.common import RESTErrorBody
from backend.schemas.preview import LibraryPreviewManifestResponse
from backend.services.library_content_service import LibraryByteRangeError
from backend.services.read_errors import ReadServiceError

router = APIRouter(prefix="/workspaces", tags=["library-preview"])

_REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


@router.get(
    "/{workspace_id}/library/versions/{version_id}/preview",
    response_model=LibraryPreviewManifestResponse,
    responses={
        **_REST_ERROR_RESPONSES,
        304: {"description": "The authorized Preview manifest is unchanged."},
    },
)
def get_library_preview_manifest(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    request: Request,
):
    try:
        with session_factory() as session:
            manifest = svc.library_preview_manifest.resolve_manifest(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
            )
    except ReadServiceError as exc:
        return read_service_error_response(exc)

    headers = _preview_headers(etag=manifest.etag)
    if if_none_match_matches(request.headers.get("if-none-match"), manifest.etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=LibraryPreviewManifestResponse(
            data=manifest.to_payload()
        ).model_dump_json(),
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


@router.head(
    "/{workspace_id}/library/versions/{version_id}/preview/source",
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/library/versions/{version_id}/preview/source",
    responses={
        **_REST_ERROR_RESPONSES,
        206: {"description": "A single authorized Preview source byte range."},
        304: {"description": "The authorized Preview source is unchanged."},
        416: {"model": RESTErrorBody},
    },
)
def read_library_preview_source(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    request: Request,
):
    _mark_preview_sandbox(request)
    try:
        with session_factory.begin() as session:
            content = svc.library_preview_manifest.resolve_source_content(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
                range_header=request.headers.get("range"),
                if_range_header=request.headers.get("if-range"),
            )
            not_modified = content.byte_range is None and if_none_match_matches(
                request.headers.get("if-none-match"),
                content.etag,
            )
            if not not_modified:
                svc.library.audit_service.emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    action="library.preview.source.read",
                    target_kind="library_item_version",
                    target_id=version_id,
                    payload={
                        "library_item_id": str(content.library_item_id),
                        "range": content.content_range,
                    },
                )
    except LibraryByteRangeError as exc:
        return _range_error_response(exc)
    except ReadServiceError as exc:
        return _preview_read_error_response(exc)

    return _preview_content_response(
        request=request,
        content=content,
        not_modified=not_modified,
    )


def _preview_content_response(*, request: Request, content, not_modified: bool):
    # The global security middleware normally owns CSP. Mark this response as
    # inert so it applies the stricter Preview policy instead of the
    # application-shell policy.
    _mark_preview_sandbox(request)
    headers = {
        **_preview_headers(etag=content.etag),
        **_preview_document_headers(),
        "Content-Disposition": _content_disposition(
            content.filename,
            disposition="inline",
        ),
    }
    if not_modified:
        return Response(status_code=304, headers=headers)
    if content.content_range is not None:
        headers["Content-Range"] = content.content_range
    headers["Content-Length"] = str(content.content_length)
    status_code = 206 if content.byte_range is not None else 200
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=content.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _stream_preview_content(content),
        status_code=status_code,
        media_type=content.media_type,
        headers=headers,
    )


def _range_error_response(exc):
    response = build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc),
        status_code=416,
    )
    response.headers["Content-Range"] = f"bytes */{exc.complete_length}"
    response.headers.update(_preview_document_headers())
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _preview_read_error_response(exc: ReadServiceError):
    response = read_service_error_response(exc)
    response.headers.update(_preview_document_headers())
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _stream_preview_content(content):
    with content.blob.open_binary() as source:
        remaining = content.content_length
        if content.byte_range is not None:
            source.seek(content.byte_range.start)
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Stored Library Preview content ended unexpectedly")
            remaining -= len(chunk)
            yield chunk


def _preview_headers(*, etag: str) -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }


def _preview_document_headers() -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
    }


def _mark_preview_sandbox(request: Request) -> None:
    setattr(request.state, CSP_SANDBOXED_DOCUMENT_STATE_KEY, True)


def _content_disposition(
    filename: str,
    *,
    disposition: Literal["attachment", "inline"],
) -> str:
    fallback = filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = fallback.replace('"', "_").replace("\\", "_") or "preview.bin"
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


__all__ = ["router"]

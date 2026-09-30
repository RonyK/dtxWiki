"""Library semantic Markdown editor and isolated overlay Preview transport."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, Response

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    WorkspaceEditorAccessDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.api.middleware.security_headers import CSP_SANDBOXED_DOCUMENT_STATE_KEY
from backend.media.application import if_none_match_matches
from backend.schemas.common import RESTErrorBody
from backend.schemas.library_semantic import (
    LibrarySemanticMarkdownResponse,
    LibrarySemanticMarkdownUpdateRequest,
)
from backend.services.read_errors import ReadServiceError

router = APIRouter(prefix="/workspaces", tags=["library-semantic"])

_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


@router.get(
    "/{workspace_id}/library/versions/{version_id}/semantic-markdown",
    response_model=LibrarySemanticMarkdownResponse,
    responses=_ERROR_RESPONSES,
)
def get_library_semantic_markdown(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    response: Response,
):
    try:
        with session_factory() as session:
            projection = svc.library_semantic_overlay.get_current(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
            )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return {"data": projection.to_payload()}


@router.put(
    "/{workspace_id}/library/versions/{version_id}/semantic-markdown",
    response_model=LibrarySemanticMarkdownResponse,
    responses=_ERROR_RESPONSES,
)
def update_library_semantic_markdown(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    request_body: LibrarySemanticMarkdownUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    response: Response,
):
    try:
        with session_factory.begin() as session:
            projection = svc.library_semantic_overlay.update_current(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
                body_markdown=request_body.body_markdown,
                expected_revision=request_body.expected_revision,
                expected_base_artifact_id=request_body.expected_base_artifact_id,
                expected_base_content_sha256=(
                    request_body.expected_base_content_sha256
                ),
            )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return {"data": projection.to_payload()}


@router.head(
    "/{workspace_id}/library/semantic-overlays/{overlay_id}/content",
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/library/semantic-overlays/{overlay_id}/content",
    responses={
        **_ERROR_RESPONSES,
        304: {"description": "The authorized overlay content is unchanged."},
    },
)
def read_library_semantic_overlay_content(
    svc: ServicesDep,
    workspace_id: UUID,
    overlay_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    request: Request,
):
    _mark_sandboxed(request)
    try:
        with session_factory.begin() as session:
            content = svc.library_semantic_overlay.resolve_content(
                session=session,
                workspace_id=workspace_id,
                overlay_id=overlay_id,
                actor=actor,
            )
            not_modified = if_none_match_matches(
                request.headers.get("if-none-match"),
                content.etag,
            )
            if not not_modified:
                svc.library_semantic_overlay.audit_service.emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor.actor_kind,
                    actor_id=str(actor.actor_id),
                    action="library.semantic_overlay.preview.read",
                    target_kind="library_semantic_overlay",
                    target_id=overlay_id,
                    payload={
                        "library_item_id": str(content.library_item_id),
                        "library_item_version_id": str(
                            content.library_item_version_id
                        ),
                    },
                )
    except ReadServiceError as exc:
        response = read_service_error_response(exc)
        response.headers.update(_content_headers(etag=None, filename=None))
        return response

    headers = _content_headers(etag=content.etag, filename=content.filename)
    if not_modified:
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(content.body))
    if request.method == "HEAD":
        return Response(status_code=200, media_type=content.media_type, headers=headers)
    return Response(
        content=content.body,
        status_code=200,
        media_type=content.media_type,
        headers=headers,
    )


def _content_headers(*, etag: str | None, filename: str | None) -> dict[str, str]:
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if etag is not None:
        headers["ETag"] = etag
    if filename is not None:
        fallback = (
            filename.encode("ascii", errors="ignore")
            .decode("ascii")
            .replace('"', "_")
            .replace("\\", "_")
            or "edited.md"
        )
        headers["Content-Disposition"] = (
            f'inline; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
    return headers


def _mark_sandboxed(request: Request) -> None:
    setattr(request.state, CSP_SANDBOXED_DOCUMENT_STATE_KEY, True)


__all__ = ["router"]

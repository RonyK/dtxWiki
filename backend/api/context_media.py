"""Router factory for Context-owned media associations and exact content reads."""

from collections.abc import Callable
from hashlib import sha256
from typing import Annotated, Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse

from backend.api.context_media_upload_limits import (
    ContextMediaUploadRoute,
)
from backend.api.deps import RestActorDep, ServicesDep, TargetResolvedRestActorDep
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.context_media.application import ContextMediaService
from backend.context_media.domain import (
    CONTEXT_MEDIA_UPLOAD_POLICY,
    ActorIdentity,
    ContentCapability,
    ContextMediaAssetProjection,
    ContextMediaEditingNotOpenError,
    ContextMediaError,
    ContextMediaNotFoundError,
    ContextMediaOwnershipError,
    ContextMediaUnavailableError,
    InvalidLogicalReferenceError,
    MediaInUseError,
    MediaUsedInHistoryError,
    StaleUsageTokenError,
    UsageState,
)
from backend.media.application import (
    DEFAULT_MAX_HTTP_RANGE_BYTES,
    ByteRangeNotSatisfiable,
    if_none_match_matches,
    if_range_allows_range,
    parse_http_byte_range,
)
from backend.policy.auth import ActorContext
from backend.policy.rest_scopes import scope_actor_to_resolved_workspace
from backend.schemas.common import RESTErrorBody
from backend.schemas.context_media import (
    ContextMediaAssetListResponse,
    ContextMediaCapabilitiesResponse,
    ContextMediaDeleteResponse,
    ContextMediaUploadResponse,
)
from backend.services.context_image_ingest import (
    CONTEXT_IMAGE_INGEST_ERROR_CODES,
    ContextImageIngestCommand,
    ContextImageIngestError,
)
from backend.services.context_media_access import (
    ContextMediaAccessError,
    ContextMediaAccessPolicy,
)
from backend.services.library_blob_storage import LibraryBlobStorageError

CONTEXT_MEDIA_UPLOAD_RESPONSES = {
    status_code: {"model": RESTErrorBody}
    for status_code in (400, 401, 403, 404, 409, 410, 413, 415, 422, 503)
}
CONTEXT_MEDIA_UPLOAD_RESPONSES[429] = {
    "model": RESTErrorBody,
    "headers": {
        "Retry-After": {
            "description": "Seconds until the same upload operation may be retried.",
            "schema": {"type": "string"},
        }
    },
}
_CONTEXT_IMAGE_INGEST_STATUS_BY_CODE = {
    "validation_failed": 422,
    "media_type_unsupported": 415,
    "media_content_mismatch": 422,
    "media_too_large": 413,
    "not_found": 404,
    "media_policy_changed": 409,
    "media_owner_mismatch": 403,
    "media_editing_not_open": 409,
    "media_unavailable": 409,
    "media_quota_exceeded": 422,
    "media_staging_limit_exceeded": 429,
    "media_staging_bytes_exceeded": 413,
    "media_install_in_progress": 429,
    "media_install_unavailable": 503,
    "media_state_changed": 409,
}
_context_image_ingest_codes_missing_from_error_codes = set(
    _CONTEXT_IMAGE_INGEST_STATUS_BY_CODE
) - set(CONTEXT_IMAGE_INGEST_ERROR_CODES)
_context_image_ingest_codes_missing_from_status_mapping = set(
    CONTEXT_IMAGE_INGEST_ERROR_CODES
) - set(_CONTEXT_IMAGE_INGEST_STATUS_BY_CODE)
if (
    _context_image_ingest_codes_missing_from_error_codes
    or _context_image_ingest_codes_missing_from_status_mapping
):
    raise RuntimeError(
        "Context image ingest status-code contract mismatch: "
        "codes in status mapping but missing from error codes: "
        f"{sorted(_context_image_ingest_codes_missing_from_error_codes)}; "
        "codes in error codes but missing from status mapping: "
        f"{sorted(_context_image_ingest_codes_missing_from_status_mapping)}"
    )
CONTEXT_MEDIA_CONTENT_RESPONSES = {
    206: {"description": "A single authorized byte range."},
    304: {"description": "The authorized media content is unchanged."},
    404: {"model": RESTErrorBody},
    416: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}
CONTEXT_MEDIA_PRIVATE_CONTENT_RESPONSES = {
    **CONTEXT_MEDIA_CONTENT_RESPONSES,
    401: {"model": RESTErrorBody},
}


class ContextMediaAccessPort(Protocol):
    """Outer authorization seam; content resolution never grants membership."""

    def require_workspace(
        self, actor: ActorContext, workspace_id: UUID, *, role: str
    ) -> None: ...

    def require_page(
        self,
        actor: ActorContext,
        page_id: UUID,
        *,
        expected_workspace_id: UUID,
        role: str,
    ) -> UUID: ...

    def require_page_version(
        self,
        actor: ActorContext,
        page_id: UUID,
        page_version_id: UUID,
        *,
        expected_workspace_id: UUID,
        role: str,
    ) -> UUID: ...

    def require_workspace_asset(
        self, actor: ActorContext, workspace_id: UUID, asset_id: UUID, *, role: str
    ) -> UUID: ...


ContentResponder = Callable[[ContentCapability, Request], Response]


def create_context_media_router(
    get_service: Callable[..., ContextMediaService],
    get_access_policy: Callable[..., ContextMediaAccessPort],
    *,
    content_responder: ContentResponder,
) -> APIRouter:
    """Create routes without importing a concrete repository or byte store."""

    router = APIRouter(
        tags=["context-media"],
        route_class=ContextMediaUploadRoute,
    )
    ServiceDep = Annotated[ContextMediaService, Depends(get_service)]
    AccessDep = Annotated[ContextMediaAccessPort, Depends(get_access_policy)]

    @router.get(
        "/workspaces/{workspace_id}/context/media/capabilities",
        response_model=ContextMediaCapabilitiesResponse,
    )
    def context_media_capabilities(
        workspace_id: UUID,
        actor: RestActorDep,
        access: AccessDep,
    ):
        try:
            access.require_workspace(actor, workspace_id, role="viewer")
        except ContextMediaAccessError as exc:
            return _access_error_response(exc)
        return {
            "data": {
                "policy_revision": CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision,
                "uploadable_media_types": (
                    CONTEXT_MEDIA_UPLOAD_POLICY.uploadable_media_types
                ),
                "max_upload_bytes": CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes,
                "max_files_per_batch": (
                    CONTEXT_MEDIA_UPLOAD_POLICY.max_files_per_batch
                ),
                "max_batch_bytes": CONTEXT_MEDIA_UPLOAD_POLICY.max_batch_bytes,
                "max_parallel_uploads": (
                    CONTEXT_MEDIA_UPLOAD_POLICY.max_parallel_uploads
                ),
            }
        }

    @router.post(
        "/workspaces/{workspace_id}/context/pages/{page_id}/media",
        response_model=ContextMediaUploadResponse,
        responses=CONTEXT_MEDIA_UPLOAD_RESPONSES,
    )
    async def upload_page_media(
        workspace_id: UUID,
        page_id: UUID,
        request: Request,
        actor: TargetResolvedRestActorDep,
        services: ServicesDep,
        access: AccessDep,
        file: Annotated[UploadFile, File()],
        marker_uuid: Annotated[UUID, Form()],
        editing_version_id: Annotated[UUID, Form()],
        policy_revision: Annotated[
            str | None,
            Header(alias="X-DTX-Media-Policy-Revision"),
        ] = None,
    ):
        try:
            true_workspace_id = access.require_page(
                actor,
                page_id,
                expected_workspace_id=workspace_id,
                role="editor",
            )
            if true_workspace_id != workspace_id:
                raise ContextMediaAccessError(
                    "not_found", "page not found", status_code=404
                )
            actor = _target_scoped_route_actor(request, actor, workspace_id)
        except ContextMediaAccessError as exc:
            return _access_error_response(exc)
        # Context media is workspace-owned data, so the write goes to the same plane
        # as the page/editing version it is registered against.
        session_factory = get_container_from_app(request).db.session_for(workspace_id)
        return await _register_upload(
            file=file,
            marker_uuid=marker_uuid,
            workspace_id=workspace_id,
            page_id=page_id,
            editing_version_id=editing_version_id,
            actor=actor,
            services=services,
            session_factory=session_factory,
            policy_revision=(
                policy_revision or CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision
            ),
        )

    @router.get(
        "/workspaces/{workspace_id}/context/pages/{page_id}/media",
        response_model=ContextMediaAssetListResponse,
    )
    def list_page_media(
        workspace_id: UUID,
        page_id: UUID,
        request: Request,
        actor: TargetResolvedRestActorDep,
        service: ServiceDep,
        access: AccessDep,
        usage: Annotated[str, Query(pattern="^(used|unused)$")] = "used",
    ):
        try:
            true_workspace_id = access.require_page(
                actor,
                page_id,
                expected_workspace_id=workspace_id,
                role="editor",
            )
            if true_workspace_id != workspace_id:
                raise ContextMediaAccessError(
                    "not_found", "page not found", status_code=404
                )
            actor = _target_scoped_route_actor(request, actor, workspace_id)
        except ContextMediaAccessError as exc:
            return _access_error_response(exc)
        usage_filter = UsageState.UNUSED if usage == "unused" else UsageState.ACTIVE
        projections = service.list_page_assets(
            workspace_id=workspace_id,
            page_id=page_id,
            usage=usage_filter,
            viewer=_actor_identity(actor),
        )
        return {
            "data": [
                _asset_payload(item, workspace_id=workspace_id) for item in projections
            ]
        }

    @router.delete(
        "/workspaces/{workspace_id}/context/media/assets/{asset_id}",
        response_model=ContextMediaDeleteResponse,
    )
    def delete_context_media_asset(
        workspace_id: UUID,
        asset_id: UUID,
        request: Request,
        actor: TargetResolvedRestActorDep,
        service: ServiceDep,
        access: AccessDep,
        usage_token: Annotated[str, Header(alias="If-Match", min_length=1)],
    ):
        # P3.1/§5.3: `(workspace_id, asset_id)` names the command -- page_id
        # is derived from the asset, never a caller-supplied input, so a
        # request cannot name one page while the asset actually belongs to
        # another.
        try:
            page_id = access.require_workspace_asset(
                actor, workspace_id, asset_id, role="editor"
            )
            actor = _target_scoped_route_actor(request, actor, workspace_id)
        except ContextMediaAccessError as exc:
            return _access_error_response(exc)
        try:
            service.delete_unused(
                workspace_id=workspace_id,
                page_id=page_id,
                asset_id=asset_id,
                usage_token=usage_token.strip('"'),
                actor=_actor_identity(actor),
            )
        except ContextMediaError as exc:
            return _error_response(exc)
        return {
            "data": {
                "id": asset_id,
                "association_state": "tombstoned",
                "gc_state": "queued",
            }
        }

    @router.head(
        "/workspaces/{workspace_id}/context/pages/{page_id}/versions/{page_version_id}/media/{asset_id}/content",
        include_in_schema=False,
    )
    @router.get(
        "/workspaces/{workspace_id}/context/pages/{page_id}/versions/{page_version_id}/media/{asset_id}/content",
        operation_id="read_context_media_version_content",
        responses=CONTEXT_MEDIA_PRIVATE_CONTENT_RESPONSES,
    )
    def read_version_media(
        request: Request,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        asset_id: UUID,
        actor: TargetResolvedRestActorDep,
        service: ServiceDep,
        access: AccessDep,
    ):
        try:
            true_workspace_id = access.require_page_version(
                actor,
                page_id,
                page_version_id,
                expected_workspace_id=workspace_id,
                role="viewer",
            )
            if true_workspace_id != workspace_id:
                raise ContextMediaAccessError(
                    "not_found", "page version not found", status_code=404
                )
            actor = _target_scoped_route_actor(request, actor, workspace_id)
        except ContextMediaAccessError:
            return _opaque_content_access_error_response()
        try:
            capability = service.resolve_version_content(
                workspace_id=workspace_id,
                page_id=page_id,
                page_version_id=page_version_id,
                asset_id=asset_id,
                actor=_actor_identity(actor),
            )
        except ContextMediaError as exc:
            return _error_response(exc, content_read=True)
        return _private_content_response(content_responder(capability, request))

    return router


def _target_scoped_route_actor(
    request: Request,
    actor: ActorContext,
    workspace_id: UUID,
) -> ActorContext:
    """Apply this late-resolved target's exact grant and route capability."""

    try:
        return scope_actor_to_resolved_workspace(request, actor, workspace_id)
    except PermissionError as exc:
        error_code = str(exc).split(":", 1)[0] or "token_workspace_denied"
        raise ContextMediaAccessError(
            error_code,
            str(exc),
            status_code=403,
        ) from exc
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise ContextMediaAccessError(
            str(detail.get("error_code") or "scope_denied"),
            str(detail.get("message") or "insufficient token scope"),
            status_code=exc.status_code,
        ) from exc


async def _register_upload(
    *,
    file: UploadFile,
    marker_uuid: UUID,
    workspace_id: UUID,
    page_id: UUID,
    editing_version_id: UUID,
    actor: ActorContext,
    services,
    session_factory,
    policy_revision: str,
):
    declared_mime = (file.content_type or "").split(";", 1)[0].strip().lower()
    # Multipart parsing and bounded transport reads remain adapter concerns.
    # Canonical format/size/decode/digest/policy admission lives in the shared
    # ContextImageIngestService used by both REST and MCP.
    content = await file.read(CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes + 1)
    try:
        result = services.context_image_ingest.ingest(
            session_factory=session_factory,
            command=ContextImageIngestCommand(
                workspace_id=workspace_id,
                marker_uuid=marker_uuid,
                filename=file.filename or "image",
                declared_media_type=declared_mime,
                content=content,
                page_id=page_id,
                editing_version_id=editing_version_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                policy_revision=policy_revision,
            ),
        )
    except ContextImageIngestError as exc:
        return _context_image_ingest_error_response(exc)
    return {
        "data": {
            "asset_id": result.asset_id,
            "logical_ref": result.logical_ref,
            "content_url": result.content_url,
            "mime": result.canonical_media_type,
            "bytes": result.byte_size,
            "sha256": result.sha256,
        }
    }


def _context_image_ingest_error_response(exc: ContextImageIngestError):
    response = build_rest_error_response(
        exc.error_code,
        str(exc),
        status_code=_CONTEXT_IMAGE_INGEST_STATUS_BY_CODE[exc.error_code],
    )
    if exc.error_code in {
        "media_staging_limit_exceeded",
        "media_install_in_progress",
    }:
        response.headers["Retry-After"] = "60"
    return response


def get_context_media_service(request: Request) -> ContextMediaService:
    return get_container_from_app(request).get_services().context_media


def get_context_media_access_policy(request: Request) -> ContextMediaAccessPolicy:
    container = get_container_from_app(request)
    return ContextMediaAccessPolicy(
        container.db, container.get_services().workspace_directory
    )


def context_media_content_response(
    capability: ContentCapability,
    request: Request,
    *,
    etag_scope: object | None = None,
) -> Response:
    # Route by the capability's own blob workspace_id rather than the removed
    # library_blob_store singleton, so a workspace with its own store/bucket
    # is actually read from -- capability.blob is the authorized BlobReference
    # this response is serving, so its workspace_id is exactly the one to ask
    # store_for for.
    store = (
        get_container_from_app(request)
        .get_services()
        .blob_store_provider.store_for(capability.blob.workspace_id)
    )
    try:
        handle = store.open_handle(
            capability.storage_key,
            byte_size=capability.byte_size,
        )
        stream = handle.open_binary()
    except LibraryBlobStorageError:
        return build_rest_error_response(
            "not_found",
            "media not found",
            status_code=404,
        )
    etag_facts = "|".join(
        (
            request.url.path,
            str(capability.asset_id),
            str(capability.blob.blob_id),
            str(capability.blob.epoch.value),
            str(etag_scope or ""),
        )
    )
    etag = f'"ctx-{sha256(etag_facts.encode()).hexdigest()[:32]}"'
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Disposition": (
            "inline; filename*=utf-8''" + quote(capability.original_filename)
        ),
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if if_none_match_matches(request.headers.get("if-none-match"), etag):
        stream.close()
        return Response(status_code=304, headers=headers)

    range_header = request.headers.get("range")
    if not if_range_allows_range(request.headers.get("if-range"), etag):
        range_header = None
    try:
        byte_range = parse_http_byte_range(
            range_header,
            complete_length=capability.byte_size,
            max_range_bytes=DEFAULT_MAX_HTTP_RANGE_BYTES,
        )
    except ByteRangeNotSatisfiable as exc:
        stream.close()
        response = build_rest_error_response(
            "media_range_invalid",
            "The requested media byte range is invalid.",
            status_code=416,
        )
        response.headers.update(
            {
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Content-Range": f"bytes */{exc.complete_length}",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    content_length = (
        byte_range.length if byte_range is not None else capability.byte_size
    )
    headers["Content-Length"] = str(content_length)
    status_code = 206 if byte_range is not None else 200
    if byte_range is not None:
        headers["Content-Range"] = byte_range.content_range
    if request.method == "HEAD":
        stream.close()
        return Response(
            status_code=status_code,
            media_type=capability.canonical_media_type,
            headers=headers,
        )
    return StreamingResponse(
        _stream_chunks(
            stream,
            start=byte_range.start if byte_range is not None else 0,
            length=content_length,
        ),
        status_code=status_code,
        media_type=capability.canonical_media_type,
        headers=headers,
    )


def _actor_identity(actor: ActorContext) -> ActorIdentity:
    return ActorIdentity(kind=actor.actor_kind, id=actor.actor_id)


def _asset_payload(
    projection: ContextMediaAssetProjection, *, workspace_id: UUID
) -> dict[str, object]:
    asset = projection.asset
    return {
        "id": asset.asset_id,
        "logical_ref": asset.logical_reference,
        "original_filename": asset.original_filename,
        "media_type": asset.canonical_media_type,
        "bytes": asset.logical_bytes,
        "association_state": asset.association_state.value,
        "usage_state": projection.usage_state.value,
        "latest_draft_reference_count": projection.latest_draft_reference_count,
        "current_published_reference_count": (
            projection.current_published_reference_count
        ),
        "historical_reference_count": projection.historical_reference_count,
        "can_delete": projection.can_delete,
        "usage_token": projection.usage_token,
        "content_url": (
            f"/api/v1/workspaces/{workspace_id}/context/pages/{asset.page_id}"
            f"/versions/{asset.created_for_version_id}/media/{asset.asset_id}/content"
        ),
    }


def _private_content_response(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _error_response(exc: ContextMediaError, *, content_read: bool = False):
    if content_read or isinstance(exc, ContextMediaNotFoundError):
        return build_rest_error_response(
            "not_found", "media not found", status_code=404
        )
    if isinstance(exc, ContextMediaOwnershipError):
        return build_rest_error_response(exc.error_code, str(exc), status_code=403)
    if isinstance(exc, ContextMediaUnavailableError):
        return build_rest_error_response(exc.error_code, str(exc), status_code=409)
    if isinstance(exc, ContextMediaEditingNotOpenError):
        return build_rest_error_response(exc.error_code, str(exc), status_code=409)
    if isinstance(exc, StaleUsageTokenError):
        return build_rest_error_response(exc.error_code, str(exc), status_code=412)
    if isinstance(exc, (MediaInUseError, MediaUsedInHistoryError)):
        return build_rest_error_response(exc.error_code, str(exc), status_code=409)
    if isinstance(exc, InvalidLogicalReferenceError):
        return build_rest_error_response(exc.error_code, str(exc), status_code=422)
    return build_rest_error_response(exc.error_code, str(exc), status_code=400)


def _access_error_response(exc: ContextMediaAccessError):
    return build_rest_error_response(
        exc.error_code,
        str(exc),
        status_code=exc.status_code,
    )


def _opaque_content_access_error_response():
    """Hide every authenticated content-target authorization failure."""

    return build_rest_error_response("not_found", "media not found", status_code=404)


def _stream_chunks(stream, *, start: int, length: int):
    try:
        stream.seek(start)
        remaining = length
        while remaining > 0:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Stored media content ended unexpectedly")
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


router = create_context_media_router(
    get_context_media_service,
    get_context_media_access_policy,
    content_responder=context_media_content_response,
)


__all__ = [
    "CONTEXT_MEDIA_CONTENT_RESPONSES",
    "CONTEXT_MEDIA_PRIVATE_CONTENT_RESPONSES",
    "CONTEXT_MEDIA_UPLOAD_RESPONSES",
    "context_media_content_response",
    "create_context_media_router",
    "router",
]

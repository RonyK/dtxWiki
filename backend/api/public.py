"""Unauthenticated public read surface (T16-B2).

Security-critical and structurally RBAC-isolated: this router has NO
RestActorDep and NO require_* dependency, so it never enters the session/auth/
role code path and cannot escalate. Only a complete visibility-compatible
scope/version grant resolves; ``is_public`` alone is not authority. Everything
else returns a uniform 404 (no 403, no info leak). ``unlisted_web`` page
responses receive noindex/nofollow/no-store; public Workspace profiles,
llms.txt, and ``indexed_web`` page responses remain discoverable. Page-path
errors get the unlisted-safe policy in PublicSurfaceHeadersMiddleware,
including validation failures that never reach a handler.
"""

from uuid import UUID

from fastapi import APIRouter, Path, Request
from fastapi.responses import Response

from backend.api.context_media import (
    CONTEXT_MEDIA_CONTENT_RESPONSES,
    context_media_content_response,
)
from backend.api.middleware.public_headers import UNLISTED_PUBLIC_SURFACE_HEADERS
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.context_media.domain import ActorIdentity, ContextMediaError
from backend.schemas.common import RESTErrorBody
from backend.schemas.page import PAGE_SLUG_PATTERN, SLUG_PATTERN, PublicPageResponse
from backend.schemas.public_workspace import PublicWorkspaceProfileResponse
from backend.services.context_media_access import resolve_public_context_media_scope
from backend.services.llms_txt import render_scoped_llms_txt
from backend.services.page_service import (
    PageNotFoundError,
    PageService,
    PageServiceError,
)
from backend.services.public_page_exposure import UNLISTED_WEB
from backend.services.public_workspace_service import (
    PublicWorkspaceNotFoundError,
    read_public_workspace_profile,
)

router = APIRouter(prefix="/public", tags=["public"])

# This router is unauthenticated (see module docstring), so there is no real
# actor to attribute a content read to. resolve_version_content()'s actor
# check only ever matters for its uninserted-preview fallback (no usage row
# yet); a sentinel that can never equal a real uploader's identity makes that
# fallback correctly deny anonymous visitors rather than crash for lack of
# one, while the ordinary published-content path (a usage row exists) is
# unaffected by which actor is passed.
_PUBLIC_ANONYMOUS_ACTOR = ActorIdentity("anonymous", "public_context_media")

# 404 = private/missing/draft-only (uniform); 422 = a slug/workspace_slug that
# fails the Path bounds/pattern (the project envelope, not FastAPI's default).
PUBLIC_ERROR_RESPONSES = {
    404: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


def _apply_unlisted_headers(response: Response) -> None:
    response.headers.update(UNLISTED_PUBLIC_SURFACE_HEADERS)


@router.get(
    "/w/{workspace_slug}",
    response_model=PublicWorkspaceProfileResponse,
    responses=PUBLIC_ERROR_RESPONSES,
)
def read_public_workspace(
    request: Request,
    workspace_slug: str = Path(min_length=1, max_length=80, pattern=SLUG_PATTERN),
):
    session_factory = get_container_from_app(request).db.control_session_factory()
    try:
        with session_factory() as session:
            profile = read_public_workspace_profile(
                session,
                workspace_slug=workspace_slug,
            )
    except PublicWorkspaceNotFoundError:
        return build_rest_error_response(
            error_code="not_found",
            message="workspace not found",
            status_code=404,
        )
    return {"data": profile.__dict__}


@router.get(
    "/w/{workspace_slug}/llms.txt",
    responses=PUBLIC_ERROR_RESPONSES,
)
def read_public_workspace_llms_txt(
    request: Request,
    workspace_slug: str = Path(min_length=1, max_length=80, pattern=SLUG_PATTERN),
):
    container = get_container_from_app(request)
    configured = getattr(request.app.state.settings, "public_base_url", None)
    base_url = (configured or str(request.base_url)).rstrip("/")
    content = render_scoped_llms_txt(
        container.db,
        workspace_slug=workspace_slug,
        base_url=base_url,
    )
    if content is None:
        return build_rest_error_response(
            error_code="not_found",
            message="workspace not found",
            status_code=404,
        )
    return Response(content=content, media_type="text/markdown")


def get_page_service(request: Request, workspace_slug: str) -> PageService:
    """Resolve the public workspace before opening its data-plane session."""
    container = get_container_from_app(request)
    services = container.get_services()
    control_factory = container.db.control_session_factory()
    with control_factory() as session:
        workspace_id = services.workspace_directory.workspace_id_by_slug(
            session, workspace_slug
        )
    if workspace_id is None:
        raise PageNotFoundError("workspace not found")
    return PageService(
        container.db.session_for(workspace_id),
        job_queue=services.job_queue,
        body_preparer=services.body_preparer,
        lint_registry=services.lint_registry,
    )


@router.get(
    "/w/{workspace_slug}/pages/{slug}",
    response_model=PublicPageResponse,
    responses=PUBLIC_ERROR_RESPONSES,
)
def read_public_page(
    request: Request,
    response: Response,
    # Bounded like the authenticated by-slug route (max_length=64): the regex
    # alone is unbounded, and this endpoint is unauthenticated + unthrottled.
    workspace_slug: str = Path(min_length=1, max_length=80, pattern=SLUG_PATTERN),
    slug: str = Path(min_length=1, max_length=64, pattern=PAGE_SLUG_PATTERN),
):
    try:
        page_service = get_page_service(request, workspace_slug)
        page = page_service.read_public_page(
            workspace_slug=workspace_slug,
            slug=slug,
        )
    except PageServiceError as exc:
        # Uniform 404 for private/missing/draft-only — never 403, never a leak.
        return build_rest_error_response(
            error_code=exc.error_code,
            message="page not found",
            status_code=404,
        )
    if page.exposure_scope == UNLISTED_WEB:
        _apply_unlisted_headers(response)
    return {"data": page}


@router.head(
    "/w/{workspace_slug}/pages/{slug}/media/{asset_id}",
    include_in_schema=False,
)
@router.get(
    "/w/{workspace_slug}/pages/{slug}/media/{asset_id}",
    operation_id="read_public_context_media",
    responses={**PUBLIC_ERROR_RESPONSES, **CONTEXT_MEDIA_CONTENT_RESPONSES},
)
def read_public_context_media(
    request: Request,
    asset_id: UUID,
    workspace_slug: str = Path(min_length=1, max_length=80, pattern=SLUG_PATTERN),
    slug: str = Path(min_length=1, max_length=64, pattern=PAGE_SLUG_PATTERN),
):
    container = get_container_from_app(request)
    services = container.get_services()
    scope = resolve_public_context_media_scope(
        container.db,
        workspace_slug=workspace_slug,
        page_slug=slug,
    )
    if scope is None:
        return build_rest_error_response(
            "not_found",
            "media not found",
            status_code=404,
        )
    try:
        capability = services.context_media.resolve_version_content(
            workspace_id=scope.workspace_id,
            page_id=scope.page_id,
            page_version_id=scope.page_version_id,
            asset_id=asset_id,
            actor=_PUBLIC_ANONYMOUS_ACTOR,
        )
    except ContextMediaError:
        return build_rest_error_response(
            "not_found",
            "media not found",
            status_code=404,
        )
    response = context_media_content_response(
        capability,
        request,
        etag_scope=scope.page_version_id,
    )
    if scope.exposure_scope == UNLISTED_WEB:
        _apply_unlisted_headers(response)
    return response

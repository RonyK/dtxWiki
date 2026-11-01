"""Validate editor source authority before resolving any stored link pins."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext
from backend.schemas.link_preview import LinkPreviewPayload, LinkPreviewRequest
from backend.services.actor_visible_read import (
    context_page_visible,
    resolve_visible_context_version,
)
from backend.services.context_link_targets import markdown_link_targets
from backend.services.read_errors import ReadNotFoundError


def preview_link_targets(
    session: Session,
    *,
    workspace_id: UUID,
    request: LinkPreviewRequest,
    actor: ActorContext,
    self_origins: frozenset[str] | None = None,
) -> LinkPreviewPayload:
    page = version = None
    if request.source_page_id is not None or request.source_version_id is not None:
        page, version = resolve_visible_context_version(
            session,
            workspace_id=workspace_id,
            page_id=request.source_page_id,
            version_id=request.source_version_id,
            path=None,
            version_preference="visible",
            actor=actor,
        )
        if (
            page is None
            or version is None
            or not context_page_visible(page, actor, workspace_id=workspace_id)
            or (
                request.source_page_id is not None
                and page.id != request.source_page_id
            )
        ):
            raise ReadNotFoundError("Context source not found")

    return LinkPreviewPayload(
        workspace_id=workspace_id,
        source_page_id=request.source_page_id,
        source_version_id=request.source_version_id,
        request_id=request.request_id,
        link_targets=markdown_link_targets(
            session,
            body=request.body_md,
            workspace_id=workspace_id,
            actor=actor,
            source_page_id=page.id if page is not None else None,
            source_version_id=version.id if version is not None else None,
            source_is_published=(
                page is not None and version.id == page.current_published_version_id
            ),
            allow_pin_fallback=False,
            self_origins=self_origins,
        ),
    )

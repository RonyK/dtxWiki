"""ContextPage version visibility projections shared by reads and repositories."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, case, null, true
from sqlalchemy.orm import aliased, object_session

from backend.models.context import ContextPage, ContextPageVersion
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    select_in_workspace_by_ids,
)

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


PUBLISHED_CONTEXT_VERSION_STATUSES = frozenset(
    {"published_snapshot", "superseded_snapshot"}
)


def editable_context_version_id(page: ContextPage) -> UUID | None:
    """Return the single Context version on which a new edit must be based."""

    return page.latest_draft_version_id or page.current_version_id


def visible_context_version_id(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID | None, bool]:
    """Return the published default, or the latest saved unpublished version."""

    if page.current_published_version_id is not None:
        return page.current_published_version_id, False
    version_id = latest_saved_context_draft_version_id(page, actor)
    return version_id, version_id is not None


def latest_saved_context_draft_version_id(
    page: ContextPage, actor: ActorContext | None
) -> UUID | None:
    """Project the latest visible open draft without consulting retired slots."""

    if not actor_can_read_drafts(actor):
        return None
    session = object_session(page)
    if session is not None:
        return session.scalar(_latest_saved_draft_statement(page.id, page.workspace_id))
    # Detached/constructed pages can only inspect relationships already loaded.
    versions = list(page.__dict__.get("versions", ()))
    versions.extend(
        version
        for name in ("current_version", "latest_draft_version")
        if (version := page.__dict__.get(name)) is not None
    )
    candidates = [version for version in versions if version.version_status == "draft"]
    if not candidates:
        return None
    version = min(
        candidates,
        key=lambda row: (
            -(
                row.last_saved_at or row.created_at or datetime(1970, 1, 1, tzinfo=UTC)
            ).timestamp(),
            str(row.id),
        ),
    )
    return version.id


def visible_context_version_ids(
    session,
    *,
    workspace_id: UUID,
    pages: Iterable[ContextPage],
    actor: ActorContext | None,
) -> dict[UUID, tuple[UUID | None, bool]]:
    """Resolve ordinary read defaults in workspace-scoped, bounded batches."""
    page_ids = list(dict.fromkeys(page.id for page in pages))
    result = {page_id: (None, False) for page_id in page_ids}
    visible_version = visible_context_version_expression(actor)
    for start in range(0, len(page_ids), 400):
        statement = select_in_workspace_by_ids(
            ContextPage,
            workspace_id=workspace_id,
            resource_ids=page_ids[start : start + 400],
        ).with_only_columns(
            ContextPage.id,
            visible_version.label("visible_version_id"),
            ContextPage.current_published_version_id,
        )
        for page_id, version_id, published_id in session.execute(statement):
            result[page_id] = (
                version_id,
                version_id is not None and published_id is None,
            )
    return result


def _latest_saved_draft_statement(page_id, workspace_id):
    version = aliased(ContextPageVersion)
    return (
        select_in_workspace(
            version,
            workspace_id=workspace_id,
            conditions=(
                version.page_id == page_id,
                version.version_status == "draft",
            ),
        )
        .with_only_columns(version.id)
        .order_by(version.last_saved_at.desc(), version.id.asc())
        .limit(1)
        .correlate_except(version)
    )


def searchable_context_version_id(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID | None, bool]:
    """Keep search on its existing latest-slot selection and published copy."""

    return editable_head_context_version_id(page, actor)


def editable_head_context_version_id(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID | None, bool]:
    """The writer's latest-slot base under the ordinary draft-read rule."""

    return visible_context_version_id(page, actor)


def visible_search_version_ids(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID, ...]:
    """Return searchable version ids for ``actor``, visible head first."""

    ordered = []
    if page.current_published_version_id is not None:
        ordered.append(page.current_published_version_id)
    if actor_can_read_drafts(actor):
        session = object_session(page)
        drafts = (
            session.scalars(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=page.workspace_id,
                    conditions=(
                        ContextPageVersion.page_id == page.id,
                        ContextPageVersion.version_status == "draft",
                    ),
                ).order_by(
                    ContextPageVersion.last_saved_at.desc(), ContextPageVersion.id
                )
            ).all()
            if session
            else page.__dict__.get("versions", ())
        )
        ordered.extend(
            version.id
            for version in drafts
            if version.version_status == "draft" and version.id not in ordered
        )
    return tuple(ordered)


def visible_context_version_expression(actor: ActorContext | None):
    """SQL counterpart of the published-first ordinary read default."""

    if not actor_can_read_drafts(actor):
        return ContextPage.current_published_version_id
    return case(
        (
            ContextPage.current_published_version_id.is_not(None),
            ContextPage.current_published_version_id,
        ),
        else_=latest_saved_context_draft_version_expression(actor),
    )


def latest_saved_context_draft_version_expression(actor: ActorContext | None):
    """SQL counterpart of latest saved draft discovery for lists and badges."""

    if not actor_can_read_drafts(actor):
        return null()
    return _latest_saved_draft_statement(
        ContextPage.id, ContextPage.workspace_id
    ).scalar_subquery()


def searchable_context_version_expression(actor: ActorContext | None):
    """SQL counterpart of the latest-slot search projection."""

    return editable_head_context_version_expression(actor)


def editable_head_context_version_expression(actor: ActorContext | None):
    """SQL counterpart of the writer's unchanged latest-slot base."""

    return visible_context_version_expression(actor)


def visible_context_page_expression(actor: ActorContext | None):
    """SQL equivalent of ordinary context_page_visible for a joined page."""
    return and_(
        ContextPage.lifecycle_status != "archived",
        visible_context_version_expression(actor).is_not(None),
    )


def explicit_context_version_visible_expression(version, actor: ActorContext | None):
    """SQL equivalent of context_version_is_visible for an aliased version."""
    published = version.version_status.in_(
        ("published_snapshot", "superseded_snapshot")
    )
    if not actor_can_read_drafts(actor):
        return published
    return true()

"""ContextPage version visibility projections shared by reads and repositories."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, case, not_
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


def _actor_identity(actor: ActorContext | None) -> tuple[str, str] | None:
    actor_kind = getattr(actor, "actor_kind", None)
    actor_id = getattr(actor, "actor_id", None)
    if actor_kind is None or actor_id is None:
        return None
    return actor_kind, actor_id


# Unsubmitted rows: also used by retention and account cleanup. Reading an
# editing row is governed separately so exposing it cannot change its lifetime.
PRIVATE_VERSION_STATUSES = frozenset({"editing", "discarded"})


def discarded_row_hidden_from_actor(
    version: ContextPageVersion | None, actor: ActorContext | None
) -> bool:
    """Hide discarded work from other actors, independently of draft access."""

    if version is None or version.version_status != "discarded":
        return False
    identity = _actor_identity(actor)
    if identity is None:
        return True
    actor_kind, actor_id = identity
    return not (
        version.created_by_actor_kind == actor_kind
        and version.created_by_actor_id == actor_id
    )


def foreign_editing_version_exists(pointer_column, identity: tuple[str, str] | None):
    """SQL for an unsubmitted row owned by someone else.

    A correlated EXISTS rather than a join: callers embed this expression
    against queries that may (context_page_repo.py) or may not already join
    `context_page_version` under its own name, so an unaliased reference here
    would collide/ambiguously correlate with that unrelated join instead of
    naming a fresh row to inspect.

    Retention and account cleanup retain this rule even though editing rows
    are readable by other editors.
    """

    return _foreign_version_exists(pointer_column, identity, PRIVATE_VERSION_STATUSES)


def foreign_discarded_version_exists(pointer_column, identity: tuple[str, str] | None):
    """SQL equivalent of ``discarded_row_hidden_from_actor`` for read surfaces."""

    return _foreign_version_exists(pointer_column, identity, ("discarded",))


def _foreign_version_exists(pointer_column, identity, statuses):
    editing_row = aliased(ContextPageVersion)
    return (
        select_in_workspace(
            editing_row,
            workspace_id=pointer_column.class_.workspace_id,
            conditions=(
                editing_row.id == pointer_column,
                editing_row.version_status.in_(tuple(statuses)),
                (
                    not_(
                        and_(
                            editing_row.created_by_actor_kind == identity[0],
                            editing_row.created_by_actor_id == identity[1],
                        )
                    )
                    if identity is not None
                    else True
                ),
            ),
        )
        .with_only_columns(editing_row.id)
        .correlate_except(editing_row)
        .exists()
    )


def _editable_context_version(page: ContextPage) -> ContextPageVersion | None:
    """The row `editable_context_version_id` names, via the matching relationship.

    A page that has never published can have `current_version_id` name an
    `editing` row directly, with `latest_draft_version_id` staying `None`
    the whole time (page-first creation, before any Save draft) -- so this
    cannot assume `latest_draft_version` is always the right object to
    inspect just because `latest_draft_version_id` is usually where an open
    session's id comes from.
    """

    if page.latest_draft_version_id is not None:
        return page.latest_draft_version
    return page.current_version


def visible_context_version_id(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID | None, bool]:
    """Return the published default, or the latest saved unpublished version."""

    if page.current_published_version_id is not None:
        return page.current_published_version_id, False
    if not actor_can_read_drafts(actor):
        return None, False
    session = object_session(page)
    if session is not None:
        version_id = session.scalar(
            _latest_saved_draft_statement(page.id, page.workspace_id)
        )
        return version_id, version_id is not None
    # Detached/constructed pages can only inspect relationships already loaded.
    versions = list(page.__dict__.get("versions", ()))
    versions.extend(
        version
        for name in ("current_version", "latest_draft_version")
        if (version := page.__dict__.get(name)) is not None
    )
    candidates = [
        version
        for version in versions
        if version.version_status in {"editing", "draft"}
    ]
    if not candidates:
        return None, False
    version = min(
        candidates,
        key=lambda row: (
            -(
                row.last_saved_at or row.created_at or datetime(1970, 1, 1, tzinfo=UTC)
            ).timestamp(),
            str(row.id),
        ),
    )
    return version.id, True


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
                version.version_status.in_(("editing", "draft")),
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
    """Preserve the writer's latest-slot base, including its own open work."""

    identity = _actor_identity(actor)

    def eligible(version):
        return version is not None and (
            version.version_status not in PRIVATE_VERSION_STATUSES
            or identity == (version.created_by_actor_kind, version.created_by_actor_id)
        )

    if actor_can_read_drafts(actor):
        editable_id = editable_context_version_id(page)
        if (
            editable_id is not None
            and editable_id != page.current_published_version_id
            and eligible(_editable_context_version(page))
        ):
            return editable_id, True
        if (
            page.current_version_id is not None
            and page.current_version_id != page.current_published_version_id
            and eligible(page.current_version)
        ):
            return page.current_version_id, True
    return page.current_published_version_id, False


def visible_search_version_ids(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID, ...]:
    """Return searchable version ids for ``actor``, visible head first."""

    head_id, _is_draft = searchable_context_version_id(page, actor)
    ordered: list[UUID] = [] if head_id is None else [head_id]
    if (
        actor_can_read_drafts(actor)
        and page.current_published_version_id is not None
        and page.current_published_version_id != head_id
    ):
        ordered.append(page.current_published_version_id)
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
        else_=_latest_saved_draft_statement(
            ContextPage.id, ContextPage.workspace_id
        ).scalar_subquery(),
    )


def searchable_context_version_expression(actor: ActorContext | None):
    """SQL counterpart of the latest-slot search projection."""

    return editable_head_context_version_expression(actor)


def editable_head_context_version_expression(actor: ActorContext | None):
    """SQL counterpart of the writer's unchanged latest-slot base."""

    if not actor_can_read_drafts(actor):
        return ContextPage.current_published_version_id
    identity = _actor_identity(actor)
    return case(
        (
            ContextPage.latest_draft_version_id.is_not(None)
            & ~foreign_editing_version_exists(
                ContextPage.latest_draft_version_id, identity
            ),
            ContextPage.latest_draft_version_id,
        ),
        (
            ContextPage.current_version_id.is_not(None)
            & (
                ContextPage.current_published_version_id.is_(None)
                | (
                    ContextPage.current_version_id
                    != ContextPage.current_published_version_id
                )
            )
            & ~foreign_editing_version_exists(ContextPage.current_version_id, identity),
            ContextPage.current_version_id,
        ),
        else_=ContextPage.current_published_version_id,
    )


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
    identity = _actor_identity(actor)
    own = (
        False
        if identity is None
        else and_(
            version.created_by_actor_kind == identity[0],
            version.created_by_actor_id == identity[1],
        )
    )
    return published | (version.version_status != "discarded") | own

"""ContextPage version visibility projections shared by reads and repositories."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, case, exists, not_, select
from sqlalchemy.orm import aliased

from backend.models.context import ContextPage, ContextPageVersion
from backend.policy.trust_boundaries import actor_can_read_drafts

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


# Version statuses whose rows only their author may read.
PRIVATE_VERSION_STATUSES = frozenset({"editing", "discarded"})


def editing_row_hidden_from_actor(
    version: ContextPageVersion | None, actor: ActorContext | None
) -> bool:
    """P2.2/P2.6: an ``editing`` row is private to its own author.

    So is a ``discarded`` one: that status is only ever given to an editing
    row (discard_context_edit, or a page-first row superseded by its first
    publish), so its text was never shared, and discarding it must not publish
    it to every reader who can see drafts. A submitted ``draft`` (or any other
    status) keeps the existing role-based ``actor_can_read_drafts`` threshold
    unchanged.
    """

    if version is None or version.version_status not in PRIVATE_VERSION_STATUSES:
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
    """SQL for "``pointer_column`` names an ``editing`` row someone else owns".

    A correlated EXISTS rather than a join: callers embed this expression
    against queries that may (context_page_repo.py) or may not already join
    `context_page_version` under its own name, so an unaliased reference here
    would collide/ambiguously correlate with that unrelated join instead of
    naming a fresh row to inspect.

    This is the single SQL form of `editing_row_hidden_from_actor`; routers
    and adapters must embed it rather than copy the predicate.
    """

    editing_row = aliased(ContextPageVersion)
    return exists(
        select(1)
        .select_from(editing_row)
        .where(
            editing_row.id == pointer_column,
            editing_row.version_status.in_(tuple(PRIVATE_VERSION_STATUSES)),
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
        )
        .correlate_except(editing_row)
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
    """Return ``(version_id, is_draft)`` for the version visible to ``actor``."""

    if actor_can_read_drafts(actor):
        editable_id = editable_context_version_id(page)
        if (
            editable_id is not None
            and editable_id != page.current_published_version_id
            and not editing_row_hidden_from_actor(
                _editable_context_version(page), actor
            )
        ):
            return editable_id, True
        if (
            page.current_version_id is not None
            and page.current_version_id != page.current_published_version_id
            and not editing_row_hidden_from_actor(page.current_version, actor)
        ):
            return page.current_version_id, True
    if page.current_published_version_id is not None:
        return page.current_published_version_id, False
    return None, False


def visible_search_version_ids(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID, ...]:
    """Return searchable version ids for ``actor``, visible head first."""

    head_id, _is_draft = visible_context_version_id(page, actor)
    ordered: list[UUID] = [] if head_id is None else [head_id]
    if (
        actor_can_read_drafts(actor)
        and page.current_published_version_id is not None
        and page.current_published_version_id != head_id
    ):
        ordered.append(page.current_published_version_id)
    return tuple(ordered)


def visible_context_version_expression(actor: ActorContext | None):
    """Return the SQL expression for the single Context head visible to actor."""

    if actor_can_read_drafts(actor):
        identity = _actor_identity(actor)

        def _pointer_is_someone_elses_editing(pointer_column):
            # A fresh alias per pointer (see foreign_editing_version_exists)
            # -- `current_version_id` can name an `editing` row directly on a
            # page that has never published, with `latest_draft_version_id`
            # staying NULL the whole time (page-first creation), so this
            # cannot check only one pointer's row and assume the other is the
            # same one.
            return foreign_editing_version_exists(pointer_column, identity)

        return case(
            (
                ContextPage.latest_draft_version_id.is_not(None)
                & ~_pointer_is_someone_elses_editing(
                    ContextPage.latest_draft_version_id
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
                & ~_pointer_is_someone_elses_editing(ContextPage.current_version_id),
                ContextPage.current_version_id,
            ),
            else_=ContextPage.current_published_version_id,
        )
    return ContextPage.current_published_version_id

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from backend.context_media.application import SyncVersionUsage
from backend.context_media.domain import validate_markdown_media_policy
from backend.context_media.ports import ContextMediaRepository
from backend.domain.prepared_body import PreparedBody
from backend.models.context import ContextPage, ContextPageVersion
from backend.repositories.context_visibility import visible_context_version_expression
from backend.utils.actor_display import resolve_actor_display_name
from backend.utils.block_identity import (
    BlockOperationProvenance,
    block_map_for_version,
    compute_block_map_with_ids,
)
from backend.utils.content_hash import content_hash as calculate_content_hash
from backend.utils.page_frontmatter import (
    derive_nav_description,
    validate_page_markdown_input,
)
from backend.utils.uuid import parse_uuid_or_none

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


def current_page_versions_statement(
    workspace_id: UUID,
    actor: ActorContext | None = None,
):
    visible_version_id = visible_context_version_expression(actor)
    published_version = aliased(ContextPageVersion)
    return (
        # The published instant is selected here, not fetched per row later: a
        # page list is one version query, and "modified" means last published.
        select(ContextPage, ContextPageVersion, published_version.created_at)
        # Explicit left side: `visible_version_id` (an EXISTS-bearing CASE,
        # see context_visibility.py) references its own aliased
        # ContextPageVersion internally, which otherwise leaves the ORM
        # unable to infer whether this join's implicit left is ContextPage
        # or that unrelated nested entity.
        .select_from(ContextPage)
        .join(
            ContextPageVersion,
            ContextPageVersion.id == visible_version_id,
        )
        .outerjoin(
            published_version,
            published_version.id == ContextPage.current_published_version_id,
        )
        .where(ContextPage.workspace_id == workspace_id)
        .order_by(
            func.coalesce(
                published_version.created_at,
                ContextPageVersion.created_at,
                ContextPage.updated_at,
            ).desc(),
            ContextPage.slug.asc(),
        )
    )


def list_pages(
    session: Session,
    workspace_id: UUID,
    limit: int,
    offset: int = 0,
) -> list[ContextPage]:
    statement = (
        select(ContextPage)
        .where(ContextPage.workspace_id == workspace_id)
        .order_by(ContextPage.updated_at.desc(), ContextPage.slug.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(session.scalars(statement))


def list_pages_with_current_versions(
    session: Session,
    workspace_id: UUID,
    limit: int,
    offset: int = 0,
    actor: ActorContext | None = None,
) -> list[tuple[ContextPage, ContextPageVersion, datetime | None]]:
    """Each row is (page, visible version, published instant or None)."""

    statement = (
        current_page_versions_statement(workspace_id, actor=actor)
        .offset(offset)
        .limit(limit)
    )
    return list(session.execute(statement).tuples())


def list_editing_versions_for_actor(
    session: Session,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    limit: int,
    offset: int = 0,
) -> list[tuple[ContextPageVersion, ContextPage]]:
    """This actor's own open "editing" rows in ``workspace_id`` (P8.3/P8.6).

    Filters on ``created_by_actor_kind``/``created_by_actor_id`` in the WHERE
    clause itself (not a post-fetch filter) -- the dominant bug class this
    session has found is a query that fetches broadly and trusts a caller to
    filter afterward. Ordered by most-recently-saved first, with ``id`` as a
    stable tiebreaker since two autosaves can land the same ``last_saved_at``
    at second/microsecond storage granularity.
    """

    statement = (
        select(ContextPageVersion, ContextPage)
        .join(ContextPage, ContextPage.id == ContextPageVersion.page_id)
        .where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.version_status == "editing",
            ContextPageVersion.created_by_actor_kind == actor_kind,
            ContextPageVersion.created_by_actor_id == actor_id,
        )
        .order_by(
            ContextPageVersion.last_saved_at.desc(),
            ContextPageVersion.id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.execute(statement).tuples())


def iter_version_block_maps(session: Session, *, batch_size: int = 200):
    """Yield (version_status, body_md, block_map) for every version row.

    Operator audit only (``dtxwiki context block-map-audit``): it spans every
    workspace on purpose and never writes.
    """

    statement = select(
        ContextPageVersion.version_status,
        ContextPageVersion.body_md,
        ContextPageVersion.block_map,
    ).execution_options(yield_per=batch_size)
    yield from session.execute(statement)


def get_open_edit_for_actor(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    actor_kind: str,
    actor_id: str,
) -> ContextPageVersion | None:
    """This actor's own open "editing" row on one page, or None.

    At most one exists (``uq_cpv_current_editing_per_author``). The actor
    filter is in the WHERE clause, so another author's private row is never
    fetched at all.
    """

    return session.scalar(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.page_id == page_id,
            ContextPageVersion.version_status == "editing",
            ContextPageVersion.created_by_actor_kind == actor_kind,
            ContextPageVersion.created_by_actor_id == actor_id,
        )
    )


def get_page_by_id(session: Session, page_id: UUID) -> ContextPage | None:
    return session.get(ContextPage, page_id)


def get_page_by_id_for_update(
    session: Session,
    page_id: UUID,
) -> ContextPage | None:
    return session.scalar(context_page_by_id_for_update_statement(page_id))


def get_page_by_slug(
    session: Session,
    workspace_id: UUID,
    slug: str,
) -> ContextPage | None:
    return session.scalar(
        select(ContextPage).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.slug == slug,
        )
    )


def get_page_by_slug_for_update(
    session: Session,
    workspace_id: UUID,
    slug: str,
) -> ContextPage | None:
    return session.scalar(context_page_by_slug_for_update_statement(workspace_id, slug))


def context_page_by_id_for_update_statement(page_id: UUID):
    return select(ContextPage).where(ContextPage.id == page_id).with_for_update()


def context_page_by_slug_for_update_statement(workspace_id: UUID, slug: str):
    return (
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.slug == slug,
        )
        .with_for_update()
    )


def context_page_by_id_for_version_insert_statement(page_id: UUID):
    return (
        select(ContextPage.id, ContextPage.workspace_id)
        .where(ContextPage.id == page_id)
        .with_for_update()
    )


def get_version(
    session: Session,
    version_id: UUID | None,
) -> ContextPageVersion | None:
    if version_id is None:
        return None
    return session.get(ContextPageVersion, version_id)


def list_versions(
    session: Session,
    page_id: UUID,
    limit: int,
    *,
    visible_statuses: Collection[str] | None = None,
    before_version_no: int | None = None,
) -> list[ContextPageVersion]:
    stmt = select(ContextPageVersion).where(ContextPageVersion.page_id == page_id)
    if visible_statuses is not None:
        stmt = stmt.where(ContextPageVersion.version_status.in_(visible_statuses))
    if before_version_no is not None:
        stmt = stmt.where(ContextPageVersion.version_no < before_version_no)
    return list(
        session.scalars(
            stmt.order_by(ContextPageVersion.version_no.desc()).limit(limit)
        )
    )


def insert_page(
    session: Session,
    workspace_id: UUID,
    slug: str,
    title: str,
    page_type: str = "overview",
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
) -> ContextPage:
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type=page_type,
        tags=tags or [],
        aliases=aliases or [],
    )
    session.add(page)
    session.flush()
    return page


def insert_page_or_get_locked(
    session: Session,
    workspace_id: UUID,
    slug: str,
    title: str,
    page_type: str = "overview",
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
) -> tuple[ContextPage, bool]:
    """Insert an exact slug or converge on a concurrent first writer."""

    values = {
        "workspace_id": workspace_id,
        "slug": slug,
        "title": title,
        "page_type": page_type,
        "tags": tags or [],
        "aliases": aliases or [],
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(ContextPage)
    elif dialect == "postgresql":
        statement = postgresql_insert(ContextPage)
    else:
        raise RuntimeError(f"unsupported context-page dialect: {dialect}")
    inserted_id = session.scalar(
        statement.values(**values)
        .on_conflict_do_nothing(index_elements=["workspace_id", "slug"])
        .returning(ContextPage.id)
    )
    if inserted_id is not None:
        page = session.get(ContextPage, inserted_id, populate_existing=True)
        if page is None:
            raise RuntimeError("inserted context page is not visible")
        return page, True
    existing = get_page_by_slug_for_update(session, workspace_id, slug)
    if existing is None:
        raise RuntimeError("context page conflict left no visible row")
    return existing, False


def insert_version(
    session: Session,
    page_id: UUID,
    parent_version_id: UUID | None,
    body: PreparedBody,
    actor_kind: str,
    actor_id: str,
    version_status: str = "published_snapshot",
    graph_status: str = "linear",
    change_set_id: UUID | None = None,
    context_media_repository: ContextMediaRepository | None = None,
    operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
    | None = None,
    identity_seed: str | None = None,
    explicit_block_replacements: dict[str, str] | None = None,
    prepared_block_map: list[dict] | None = None,
) -> ContextPageVersion:
    """Insert new authored bytes only after the shared body preparation gate."""

    if not isinstance(body, PreparedBody):
        raise TypeError("insert_version requires a PreparedBody")
    return _insert_version_raw(
        session,
        page_id=page_id,
        parent_version_id=parent_version_id,
        body_md=body.body,
        content_hash=body.content_hash,
        actor_kind=actor_kind,
        actor_id=actor_id,
        version_status=version_status,
        graph_status=graph_status,
        change_set_id=change_set_id,
        context_media_repository=context_media_repository,
        operation_provenance=operation_provenance,
        identity_seed=identity_seed,
        explicit_block_replacements=explicit_block_replacements,
        prepared_block_map=prepared_block_map,
    )


def insert_version_copy(
    session: Session,
    page_id: UUID,
    parent_version_id: UUID | None,
    source_version: ContextPageVersion | None,
    actor_kind: str,
    actor_id: str,
    version_status: str = "published_snapshot",
    graph_status: str = "linear",
    change_set_id: UUID | None = None,
    context_media_repository: ContextMediaRepository | None = None,
    operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
    | None = None,
    identity_seed: str | None = None,
    explicit_block_replacements: dict[str, str] | None = None,
    prepared_block_map: list[dict] | None = None,
) -> ContextPageVersion:
    """Copy stored bytes and their hash, or seed an empty editing session."""

    if source_version is not None and not isinstance(
        source_version, ContextPageVersion
    ):
        raise TypeError("insert_version_copy requires a stored ContextPageVersion")
    return _insert_version_raw(
        session,
        page_id=page_id,
        parent_version_id=parent_version_id,
        body_md=source_version.body_md if source_version is not None else "",
        content_hash=(
            source_version.content_hash
            if source_version is not None
            else calculate_content_hash("")
        ),
        actor_kind=actor_kind,
        actor_id=actor_id,
        version_status=version_status,
        graph_status=graph_status,
        change_set_id=change_set_id,
        context_media_repository=context_media_repository,
        operation_provenance=operation_provenance,
        identity_seed=identity_seed,
        explicit_block_replacements=explicit_block_replacements,
        prepared_block_map=prepared_block_map,
    )


def _insert_version_raw(
    session: Session,
    page_id: UUID,
    parent_version_id: UUID | None,
    body_md: str,
    content_hash: str,
    actor_kind: str,
    actor_id: str,
    version_status: str = "published_snapshot",
    graph_status: str = "linear",
    change_set_id: UUID | None = None,
    context_media_repository: ContextMediaRepository | None = None,
    operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
    | None = None,
    identity_seed: str | None = None,
    explicit_block_replacements: dict[str, str] | None = None,
    prepared_block_map: list[dict] | None = None,
) -> ContextPageVersion:
    """Single constructor for ContextPageVersion rows.

    Every write path (REST page service, MCP context write, ingress accept)
    must create versions here so cross-writer invariants hold: the page
    row-lock plus shared version_no allocation, actor_display_name
    resolution, and write-time nav_description derivation.

    The row-lock half of that invariant needs help on SQLite, where
    ``FOR UPDATE`` compiles away and pysqlite defers ``BEGIN`` until the first
    DML: two writers would otherwise both read the same ``max(version_no)``
    and collide on ``context_page_version_no_uq``.  This layer cannot import
    ``acquire_write_intent`` (repositories must not import services), so every
    calling transaction is responsible for holding write intent before it gets
    here.
    """
    # The lexical half of the media contract must run before row locks, version
    # allocation, or any PageVersion/Usage DML. SyncVersionUsage repeats the
    # pure validation while atomically authorizing and replacing occurrences.
    validate_page_markdown_input(body_md)
    validate_markdown_media_policy(body_md)
    # Every Context-media writer uses one lock order: Workspace media intent,
    # then Page, then association/Usage.  Looking up the immutable Workspace id
    # does not lock the Page; the repository port acquires the shared intent
    # before this constructor takes the Page row lock below.  This closes the
    # PostgreSQL inversion where a version writer formerly held Page while
    # ``replace_version_usage`` waited for the Workspace media intent, opposite
    # to upload/reconciliation/delete flows.
    if context_media_repository is not None:
        workspace_id = session.scalar(
            select(ContextPage.workspace_id).where(ContextPage.id == page_id)
        )
        if workspace_id is None:
            raise ValueError("Context Page was not found")
        context_media_repository.lock_workspace_for_usage(workspace_id)
    locked_page = session.execute(
        context_page_by_id_for_version_insert_statement(page_id)
    ).one_or_none()
    if locked_page is None:
        raise ValueError("Context Page was not found")
    workspace_id = locked_page.workspace_id
    parent_version = None
    if parent_version_id is not None:
        parent_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == page_id,
                ContextPageVersion.id == parent_version_id,
            )
        )
        if parent_version is None:
            raise ValueError("Context parent PageVersion was not found")
    next_version_no = (
        session.scalar(
            select(func.coalesce(func.max(ContextPageVersion.version_no), 0) + 1).where(
                ContextPageVersion.page_id == page_id
            )
        )
        or 1
    )
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page_id,
        parent_version_id=parent_version_id,
        version_no=next_version_no,
        body_md=body_md,
        content_hash=content_hash,
        graph_status=graph_status,
        actor_type=actor_kind,
        actor_id=parse_uuid_or_none(actor_id),
        actor_display_name=resolve_actor_display_name(
            session,
            actor_kind=actor_kind,
            actor_id=actor_id,
        ),
        created_by_actor_kind=actor_kind,
        created_by_actor_id=actor_id,
        version_status=version_status,
        change_set_id=change_set_id,
        nav_description=derive_nav_description(body_md),
        block_map=(
            prepared_block_map
            if prepared_block_map is not None
            else compute_block_map_with_ids(
                body_md,
                previous_body_md=parent_version.body_md if parent_version else None,
                previous_block_map=(
                    block_map_for_version(
                        parent_version.block_map,
                        parent_version.body_md,
                    )
                    if parent_version
                    else None
                ),
                operation_provenance=operation_provenance,
                identity_seed=identity_seed,
                explicit_replacements=explicit_block_replacements,
            )
        ),
    )
    session.add(version)
    session.flush()
    page = session.scalar(
        select(ContextPage).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id == page_id,
        )
    )
    if page is not None:
        if context_media_repository is not None:
            SyncVersionUsage().execute(
                context_media_repository,
                workspace_id=page.workspace_id,
                page_id=page.id,
                page_version_id=version.id,
                body_markdown=body_md,
            )
        elif "dtx-media://asset/" in body_md:
            raise ValueError(
                "Context media references require a configured Usage writer"
            )
    return version

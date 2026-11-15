from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import LargeBinary, and_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from backend.context_media.application import SyncVersionUsage
from backend.context_media.domain import validate_markdown_media_policy
from backend.context_media.ports import ContextMediaRepository
from backend.domain.prepared_body import PreparedBody
from backend.models.base import utc_now
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.tree import WorkspaceTreeNode
from backend.repositories.context_visibility import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
    visible_context_version_expression,
)
from backend.repositories.page_links import active_page_link_condition
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    join_in_workspace,
    select_in_workspace,
)
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


@dataclass(frozen=True, slots=True)
class ContextLinkAdvisoryRow:
    source_is_advisory_eligible: bool
    has_other_graph_visible_context: bool
    has_graph_visible_outbound_link: bool


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


def visible_page_body_totals(
    session: Session,
    workspace_id: UUID,
    actor: ActorContext | None = None,
) -> tuple[int, int]:
    """(pages, UTF-8 body bytes) of the live pages ``actor`` can see.

    The same visible-version rule the whole-workspace bundle uses, summed in
    one query rather than rendered, so the guide can say whether the bundle is
    worth reading whole. Stored bodies only: the rendered bundle drops stored
    frontmatter and adds an index, so this is an estimate.
    """

    body = ContextPageVersion.body_md
    body_bytes = (
        func.length(cast(body, LargeBinary))
        if session.get_bind().dialect.name == "sqlite"
        else func.octet_length(body)
    )
    pages, total = session.execute(
        select(func.count(), func.coalesce(func.sum(body_bytes), 0))
        .select_from(ContextPage)
        .join(
            ContextPageVersion,
            ContextPageVersion.id == visible_context_version_expression(actor),
        )
        .where(ContextPage.workspace_id == workspace_id)
        .where(ContextPage.lifecycle_status != "archived")
    ).one()
    return int(pages), int(total)


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


def page_slugs_by_ids(
    session: Session,
    workspace_id: UUID,
    page_ids: Collection[UUID],
) -> dict[UUID, str]:
    """Return current slugs for Context pages scoped to one workspace."""

    ids = tuple(set(page_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(ContextPage.id, ContextPage.slug).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id.in_(ids),
        )
    )
    return {page_id: slug for page_id, slug in rows}


def context_page_targets_by_navigation_id(
    session: Session,
    workspace_id: UUID,
    target_ids: Collection[UUID],
) -> dict[UUID, tuple[UUID, str]]:
    """Resolve `/context?target=` page or tree-node ids to Context pages.

    The Context screen treats a matching tree-node id before falling back to a
    page id. Mirror that order so authored links describe the page a reader
    actually opens. Archived rows remain resolvable because PageLink stores
    authored history; viewer read boundaries decide whether to expose them.
    """

    ids = tuple(set(target_ids))
    if not ids:
        return {}
    resolved = {
        page_id: (page_id, slug)
        for page_id, slug in page_slugs_by_ids(session, workspace_id, ids).items()
    }
    node_rows = session.execute(
        select(
            WorkspaceTreeNode.id,
            WorkspaceTreeNode.target_kind,
            ContextPage.id,
            ContextPage.slug,
        )
        .outerjoin(
            ContextPage,
            and_(
                ContextPage.workspace_id == WorkspaceTreeNode.workspace_id,
                ContextPage.id == WorkspaceTreeNode.target_id,
            ),
        )
        .where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.id.in_(ids),
        )
    )
    for node_id, target_kind, page_id, slug in node_rows:
        # ContextScreen resolves any same-workspace node id before attempting a
        # page-id fallback. A folder or another entity kind therefore blocks a
        # coincidentally equal ContextPage id instead of opening that page.
        resolved.pop(node_id, None)
        if target_kind == "context_page" and page_id is not None and slug is not None:
            resolved[node_id] = (page_id, slug)
    return resolved


def live_page_link_version_ids(
    session: Session,
    page: ContextPage,
) -> set[UUID]:
    """Return versions whose authored-link projections remain live."""

    return set(
        session.scalars(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=page.workspace_id,
                conditions=(
                    ContextPageVersion.page_id == page.id,
                    or_(
                        ContextPageVersion.version_status == "draft",
                        ContextPageVersion.id == page.current_published_version_id,
                    ),
                ),
            ).with_only_columns(ContextPageVersion.id)
        )
    )


def context_link_advisory_row(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    version_id: UUID,
) -> ContextLinkAdvisoryRow:
    """Load the workspace-scoped authored-link advisory facts in one query."""

    source_page = ContextPage
    source_version = ContextPageVersion
    candidate_page = aliased(ContextPage, name="advisory_candidate_page")
    candidate_head = aliased(ContextPageVersion, name="advisory_candidate_head")
    linked_page = aliased(ContextPage, name="advisory_linked_page")
    linked_head = aliased(ContextPageVersion, name="advisory_linked_head")

    source_query = select_in_workspace(
        source_page,
        workspace_id=workspace_id,
        conditions=(
            source_page.id == page_id,
            source_page.lifecycle_status != "archived",
            source_version.version_status != "discarded",
            or_(
                source_version.version_status == "draft",
                source_page.current_published_version_id == version_id,
            ),
        ),
    )
    source_query = join_in_workspace(
        source_query,
        source_page,
        source_version,
        workspace_id=workspace_id,
        on=and_(
            source_version.id == version_id, source_version.page_id == source_page.id
        ),
    )
    source_is_advisory_eligible = exists(source_query.with_only_columns(literal(1)))

    has_other_graph_visible_context = exists(
        select(1)
        .select_from(candidate_page)
        .join(
            candidate_head,
            and_(
                candidate_head.id == candidate_page.current_published_version_id,
                candidate_head.page_id == candidate_page.id,
                candidate_head.workspace_id == candidate_page.workspace_id,
            ),
        )
        .where(
            candidate_page.workspace_id == workspace_id,
            candidate_page.id != page_id,
            candidate_page.lifecycle_status != "archived",
            candidate_head.version_status.in_(PUBLISHED_CONTEXT_VERSION_STATUSES),
        )
    )

    has_graph_visible_outbound_link = exists(
        select(1)
        .select_from(PageLink)
        .join(
            linked_page,
            and_(
                linked_page.id == PageLink.target_page_id,
                linked_page.workspace_id == PageLink.workspace_id,
            ),
        )
        .join(
            linked_head,
            and_(
                linked_head.id == linked_page.current_published_version_id,
                linked_head.page_id == linked_page.id,
                linked_head.workspace_id == linked_page.workspace_id,
            ),
        )
        .where(
            PageLink.workspace_id == workspace_id,
            active_page_link_condition(),
            PageLink.source_page_id == page_id,
            PageLink.source_version_id == version_id,
            PageLink.target_page_id.is_not(None),
            PageLink.target_page_id != page_id,
            linked_page.lifecycle_status != "archived",
            linked_head.version_status.in_(PUBLISHED_CONTEXT_VERSION_STATUSES),
        )
    )

    row = session.execute(
        select(
            source_is_advisory_eligible.label("source_is_advisory_eligible"),
            has_other_graph_visible_context.label("has_other_graph_visible_context"),
            has_graph_visible_outbound_link.label("has_graph_visible_outbound_link"),
        )
    ).one()
    return ContextLinkAdvisoryRow(
        source_is_advisory_eligible=bool(row.source_is_advisory_eligible),
        has_other_graph_visible_context=bool(row.has_other_graph_visible_context),
        has_graph_visible_outbound_link=bool(row.has_graph_visible_outbound_link),
    )


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
    reference_source_version: ContextPageVersion | None = None,
) -> ContextPageVersion:
    """Insert new authored bytes only after the shared body preparation gate."""

    if not isinstance(body, PreparedBody):
        raise TypeError("insert_version requires a PreparedBody")
    version = _insert_version_raw(
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
    if reference_source_version is not None or parent_version_id is not None:
        source = reference_source_version or get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=version.workspace_id,
            resource_id=parent_version_id,
        )
        if source is not None:
            if source.workspace_id != version.workspace_id or source.page_id != page_id:
                raise ValueError("Reference source must belong to the destination page")
            from backend.repositories.document_reference import copy_document_references

            copy_document_references(session, source=source, target=version)
    return version


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
    if prepared_block_map is None and source_version is not None:
        from copy import deepcopy

        from backend.utils.block_identity import heal_block_map

        # A restore copies the selected snapshot's identities, even when its
        # new lineage parent is a different, newer version.
        source_map = source_version.block_map
        if source_map is None:
            source_map, _ = heal_block_map(
                source_version.body_md, None, identity_seed=str(source_version.id)
            )
        prepared_block_map = deepcopy(source_map)
    version = _insert_version_raw(
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
        validate_authored_body=False,
    )
    if source_version is not None:
        from backend.repositories.document_reference import copy_document_references

        copy_document_references(session, source=source_version, target=version)
    return version


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
    validate_authored_body: bool = True,
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
    if validate_authored_body:
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
        last_saved_at=utc_now(),
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

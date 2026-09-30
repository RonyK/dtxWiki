"""Edge-projection extractor registry (design §19.5.4, G0).

Every edge-bearing relation in the schema must be listed in
EDGE_BEARING_RELATIONS and either have a registered extractor or an explicit
defer gate. rebuild_edge_projection is idempotent: it replaces the workspace's
rows for every registered edge kind in one pass (uq_edge_projection_identity
guards against duplicates).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
    PageProvenance,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.mcp_chat import (
    CONSUMPTION_VERBS,
    McpChatArtifactLink,
    McpChatSession,
)
from backend.models.tree import WorkspaceTreeNode
from backend.services.entity_ref import EntityKind
from backend.services.graph_cache import bump_edge_version

CO_ACCESS_MAX_ARTIFACTS_PER_SESSION = 50


class EdgeExtractor(Protocol):
    def rebuild_rows(
        self, session: Session, workspace_id: UUID
    ) -> list[dict[str, object]]:
        """Return edge_projection row dicts for one workspace."""
        ...


PROVENANCE_BY_KIND: dict[str, str] = {
    # design 19.3: curated (human-curated links) > derived (version/lineage)
    # > usage (behavioral). Fixed map — extractors may not improvise.
    "wikilink": "curated",
    "tree": "curated",
    "citation_page": "curated",
    "citation_source": "curated",
    "promoted_from": "derived",
    "page_provenance": "derived",
    "library_sync_version": "derived",
    "version_parent": "derived",
    "version_merge": "derived",
    "ask_citation": "usage",
    "co_access": "usage",
}


def _edge(
    *,
    workspace_id: UUID,
    source_kind: EntityKind,
    source_id: UUID,
    target_kind: EntityKind,
    target_id: UUID,
    edge_kind: str,
    source_table: str,
    weight: float = 1.0,
    occurred_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "source_kind": source_kind.value,
        "source_id": source_id,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "edge_kind": edge_kind,
        "provenance": PROVENANCE_BY_KIND[edge_kind],
        "weight": weight,
        "occurred_at": occurred_at,
        "source_ref": {"table": source_table},
    }


def _ask_citation_rows(session: Session, workspace_id: UUID) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            AskTurnCitation.ask_turn_id,
            AskTurnCitation.target_kind,
            AskTurnCitation.target_id,
        )
        .join(AskTurn, AskTurnCitation.ask_turn_id == AskTurn.id)
        .join(AskSession, AskTurn.ask_session_id == AskSession.id)
        .where(AskSession.workspace_id == workspace_id)
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.ASK_TURN,
            source_id=turn_id,
            target_kind=EntityKind(target_kind),
            target_id=target_id,
            edge_kind="ask_citation",
            source_table="ask_turn_citation",
        )
        for turn_id, target_kind, target_id in rows
    ]


def _co_access_rows(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Aggregate session-level co-access without retaining session identity."""

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    rows = session.execute(
        select(
            McpChatArtifactLink.chat_session_id,
            McpChatArtifactLink.resource_kind,
            McpChatArtifactLink.resource_id,
            McpChatArtifactLink.occurrence_count,
            McpChatArtifactLink.last_occurred_at,
        )
        .join(
            McpChatSession,
            McpChatArtifactLink.chat_session_id == McpChatSession.id,
        )
        .where(
            McpChatSession.workspace_id == workspace_id,
            McpChatArtifactLink.verb.in_(CONSUMPTION_VERBS),
            McpChatArtifactLink.last_occurred_at >= cutoff,
        )
    ).all()
    per_session: dict[UUID, dict[tuple[str, UUID], tuple[int, datetime]]] = defaultdict(
        dict
    )
    for session_id, kind, entity_id, count, occurred_at in rows:
        try:
            EntityKind(kind)
        except ValueError:
            continue
        previous = per_session[session_id].get((kind, entity_id))
        if previous is None:
            per_session[session_id][(kind, entity_id)] = (count, occurred_at)
        else:
            per_session[session_id][(kind, entity_id)] = (
                previous[0] + count,
                max(previous[1], occurred_at),
            )

    aggregates: dict[
        tuple[tuple[str, UUID], tuple[str, UUID]], tuple[float, datetime]
    ] = {}
    for artifacts in per_session.values():
        ordered = sorted(
            artifacts,
            key=lambda item: (
                -artifacts[item][0],
                -artifacts[item][1].timestamp(),
                item[0],
                str(item[1]),
            ),
        )[:CO_ACCESS_MAX_ARTIFACTS_PER_SESSION]
        for left, right in combinations(ordered, 2):
            left, right = sorted(
                (left, right),
                key=lambda ref: (ref[0], str(ref[1])),
            )
            left_count, left_at = artifacts[left]
            right_count, right_at = artifacts[right]
            occurred_at = max(left_at, right_at)
            age_days = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
            weight = min(left_count, right_count) * (0.5 ** (age_days / 30.0))
            previous = aggregates.get((left, right))
            aggregates[(left, right)] = (
                weight + (previous[0] if previous else 0.0),
                max(occurred_at, previous[1]) if previous else occurred_at,
            )

    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind(left[0]),
            source_id=left[1],
            target_kind=EntityKind(right[0]),
            target_id=right[1],
            edge_kind="co_access",
            source_table="mcp_chat_artifact_link",
            weight=weight,
            occurred_at=occurred_at,
        )
        for (left, right), (weight, occurred_at) in aggregates.items()
    ]


def _promoted_from_rows(
    session: Session, workspace_id: UUID
) -> list[dict[str, object]]:
    rows = session.execute(
        select(AskTurn.promoted_context_page_id, AskTurn.id)
        .join(AskSession, AskTurn.ask_session_id == AskSession.id)
        .where(AskSession.workspace_id == workspace_id)
        .where(AskTurn.promoted_context_page_id.is_not(None))
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=page_id,
            target_kind=EntityKind.ASK_TURN,
            target_id=turn_id,
            edge_kind="promoted_from",
            source_table="ask_turn",
        )
        for page_id, turn_id in rows
    ]


def _page_provenance_rows(
    session: Session, workspace_id: UUID
) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            PageProvenance.page_id,
            PageProvenance.source_kind,
            PageProvenance.source_id,
        )
        .join(ContextPage, PageProvenance.page_id == ContextPage.id)
        .where(ContextPage.workspace_id == workspace_id)
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=page_id,
            target_kind=EntityKind(source_kind),
            target_id=source_id,
            edge_kind="page_provenance",
            source_table="page_provenance",
        )
        for page_id, source_kind, source_id in rows
    ]


def _library_sync_version_rows(
    session: Session, workspace_id: UUID
) -> list[dict[str, object]]:
    rows = session.execute(
        select(LibraryItemVersion.id, LibraryItemVersion.sync_run_id)
        .join(LibraryItem, LibraryItemVersion.library_item_id == LibraryItem.id)
        .where(LibraryItem.workspace_id == workspace_id)
        .where(LibraryItemVersion.sync_run_id.is_not(None))
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.LIBRARY_ITEM_VERSION,
            source_id=version_id,
            target_kind=EntityKind.LIBRARY_SYNC_RUN,
            target_id=run_id,
            edge_kind="library_sync_version",
            source_table="library_item_version",
        )
        for version_id, run_id in rows
    ]


def _wikilink_rows(
    session: Session,
    workspace_id: UUID,
    citing_page_id: UUID | None = None,
) -> list[dict[str, object]]:
    # Published-head grain (design 3.3): draft-only links must not leak into
    # viewer-visible neighbors, so only PageLink rows whose source version is
    # the page's current published head project. Weight = total occurrences.
    statement = (
        select(
            PageLink.source_page_id,
            PageLink.target_page_id,
            func.sum(PageLink.occurrence_count),
        )
        .join(ContextPage, PageLink.source_page_id == ContextPage.id)
        .where(PageLink.workspace_id == workspace_id)
        .where(PageLink.target_page_id.is_not(None))
        .where(PageLink.source_version_id == ContextPage.current_published_version_id)
        .group_by(PageLink.source_page_id, PageLink.target_page_id)
    )
    if citing_page_id is not None:
        statement = statement.where(PageLink.source_page_id == citing_page_id)
    rows = session.execute(statement).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=source_page_id,
            target_kind=EntityKind.CONTEXT_PAGE,
            target_id=target_page_id,
            edge_kind="wikilink",
            source_table="page_links",
            weight=float(total or 1),
        )
        for source_page_id, target_page_id, total in rows
    ]


def _tree_rows(session: Session, workspace_id: UUID) -> list[dict[str, object]]:
    """Curated hierarchy edges: folder->folder (structure) and
    folder->entity (placement) from visible tree nodes.

    Ask-section nodes are operational per-user chat structure (day folders,
    session refs titled with question excerpts), not knowledge hierarchy —
    they never project, so no generic graph surface can serve them."""
    nodes = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.visibility_status == "visible",
            WorkspaceTreeNode.section != "ask",
        )
    ).all()
    by_id = {node.id: node for node in nodes}
    rows: list[dict[str, object]] = []
    # Collapse identity duplicates (same edge dedupe as the incremental
    # refresh path): two visible ref nodes with the same target under one
    # parent must not emit two rows or the rebuild aborts on
    # uq_edge_projection_identity.
    seen: set[tuple] = set()
    for node in nodes:
        parent = by_id.get(node.parent_id) if node.parent_id else None
        row = _tree_edge_row(workspace_id, node, parent)
        if row is None:
            continue
        identity = (
            row["source_kind"],
            row["source_id"],
            row["target_kind"],
            row["target_id"],
            row["edge_kind"],
        )
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


def _tree_edge_row(
    workspace_id: UUID,
    node: WorkspaceTreeNode,
    parent: WorkspaceTreeNode | None,
) -> dict[str, object] | None:
    if (
        parent is None
        or node.visibility_status != "visible"
        or parent.visibility_status != "visible"
        or node.section == "ask"
    ):
        return None
    if node.node_kind == "folder":
        target_kind = EntityKind.WORKSPACE_TREE_NODE
        target_id = node.id
    elif node.target_id is not None and node.target_kind in (
        "context_page",
        "library_item",
        "agent_page",
    ):
        target_kind = EntityKind(node.target_kind)
        target_id = node.target_id
    else:
        return None
    return _edge(
        workspace_id=workspace_id,
        source_kind=EntityKind.WORKSPACE_TREE_NODE,
        source_id=parent.id,
        target_kind=target_kind,
        target_id=target_id,
        edge_kind="tree",
        source_table="workspace_tree_node",
        occurred_at=node.created_at,
    )


def _citation_scope(statement, citing_page_id: UUID | None):
    """Restrict citations to one page's current published version.

    ``CitationInput`` stores claim ids for claim-scoped citations and version
    ids for whole/section citations. Legacy page/version target kinds remain
    readable, but draft and superseded-version evidence never projects into
    the viewer-visible graph.
    """
    if citing_page_id is None:
        return statement
    published_version_ids = select(ContextPage.current_published_version_id).where(
        ContextPage.id == citing_page_id,
        ContextPage.current_published_version_id.is_not(None),
    )
    claim_ids = select(ContextClaim.id).where(
        ContextClaim.page_version_id.in_(published_version_ids)
    )
    return statement.where(
        or_(
            and_(
                ContextCitation.target_kind == "context_page",
                ContextCitation.target_id == citing_page_id,
            ),
            and_(
                ContextCitation.target_kind.in_(
                    (
                        "context_page_version",
                        "context_page_whole",
                        "context_page_section",
                    )
                ),
                ContextCitation.target_id.in_(published_version_ids),
            ),
            and_(
                ContextCitation.target_kind == "context_claim",
                ContextCitation.target_id.in_(claim_ids),
            ),
        )
    )


def _aggregate_citation_edges(
    session: Session,
    workspace_id: UUID,
    edge_kind: str,
    per_citation: list[tuple[ContextCitation, EntityKind, UUID]],
) -> list[dict[str, object]]:
    """Collapse citation instances onto identity edges.

    A page can cite the same target more than once (two claims in one draft,
    or citations carried across revisions — ContextCitation has no
    uniqueness), and uq_edge_projection_identity would reject duplicate rows
    mid-publish. Weight = instance count, occurred_at = latest evidence.
    """
    citing_by_citation = _citing_page_map(
        session, [citation for citation, _kind, _id in per_citation]
    )
    workspace_pages = _workspace_page_ids(
        session, set(citing_by_citation.values()), workspace_id
    )
    aggregated: dict[tuple[UUID, EntityKind, UUID], list] = {}
    for citation, target_kind, target_id in per_citation:
        citing_page_id = citing_by_citation.get(citation.id)
        if citing_page_id is None or citing_page_id not in workspace_pages:
            continue
        key = (citing_page_id, target_kind, target_id)
        entry = aggregated.get(key)
        if entry is None:
            aggregated[key] = [1, citation.created_at]
        else:
            entry[0] += 1
            if citation.created_at and (
                entry[1] is None or citation.created_at > entry[1]
            ):
                entry[1] = citation.created_at
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE,
            source_id=citing_page_id,
            target_kind=target_kind,
            target_id=target_id,
            edge_kind=edge_kind,
            source_table="context_citation",
            weight=float(count),
            occurred_at=latest,
        )
        for (citing_page_id, target_kind, target_id), (
            count,
            latest,
        ) in aggregated.items()
    ]


def _citation_page_rows(
    session: Session,
    workspace_id: UUID,
    citing_page_id: UUID | None = None,
) -> list[dict[str, object]]:
    """Curated page->page citations (ContextCitation XOR: cited page arm)."""
    statement = (
        select(
            ContextCitation,
            ContextPageVersion.page_id,
        )
        .join(
            ContextPageVersion,
            ContextCitation.cited_context_page_version_id == ContextPageVersion.id,
        )
        .where(ContextCitation.cited_context_page_version_id.is_not(None))
    )
    rows = session.execute(_citation_scope(statement, citing_page_id)).all()
    return _aggregate_citation_edges(
        session,
        workspace_id,
        "citation_page",
        [
            (citation, EntityKind.CONTEXT_PAGE, cited_page_id)
            for citation, cited_page_id in rows
        ],
    )


def _citation_source_rows(
    session: Session,
    workspace_id: UUID,
    citing_page_id: UUID | None = None,
) -> list[dict[str, object]]:
    """Curated page->library citations (artifact / item-version arms)."""
    statement = select(ContextCitation).where(
        (ContextCitation.library_artifact_id.is_not(None))
        | (ContextCitation.library_item_version_id.is_not(None))
    )
    citations = session.scalars(_citation_scope(statement, citing_page_id)).all()
    per_citation: list[tuple[ContextCitation, EntityKind, UUID]] = []
    for citation in citations:
        if citation.library_artifact_id is not None:
            target_kind = EntityKind.LIBRARY_ARTIFACT
            target_id = citation.library_artifact_id
        else:
            target_kind = EntityKind.LIBRARY_ITEM_VERSION
            target_id = citation.library_item_version_id
        per_citation.append((citation, target_kind, target_id))
    return _aggregate_citation_edges(
        session, workspace_id, "citation_source", per_citation
    )


def _version_parent_rows(
    session: Session, workspace_id: UUID
) -> list[dict[str, object]]:
    """Derived version-lineage edges (context page versions)."""
    rows = session.execute(
        select(
            ContextPageVersion.id,
            ContextPageVersion.parent_version_id,
            ContextPageVersion.created_at,
        )
        .join(ContextPage, ContextPageVersion.page_id == ContextPage.id)
        .where(ContextPage.workspace_id == workspace_id)
        .where(ContextPageVersion.parent_version_id.is_not(None))
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE_VERSION,
            source_id=version_id,
            target_kind=EntityKind.CONTEXT_PAGE_VERSION,
            target_id=parent_id,
            edge_kind="version_parent",
            source_table="context_page_version",
            occurred_at=created_at,
        )
        for version_id, parent_id, created_at in rows
    ]


def _version_merge_rows(
    session: Session, workspace_id: UUID
) -> list[dict[str, object]]:
    """Derived reconcile-merge edges: a diverged branch folded cleanly into a
    linear version -- the git-merge "second parent", distinct from
    version_parent's "branched from" edge above so a graph query can tell
    lineage from reconciliation."""
    rows = session.execute(
        select(
            ContextPageVersion.id,
            ContextPageVersion.merged_into_version_id,
            ContextPageVersion.created_at,
        )
        .join(ContextPage, ContextPageVersion.page_id == ContextPage.id)
        .where(ContextPage.workspace_id == workspace_id)
        .where(ContextPageVersion.merged_into_version_id.is_not(None))
    ).all()
    return [
        _edge(
            workspace_id=workspace_id,
            source_kind=EntityKind.CONTEXT_PAGE_VERSION,
            source_id=version_id,
            target_kind=EntityKind.CONTEXT_PAGE_VERSION,
            target_id=merged_into_id,
            edge_kind="version_merge",
            source_table="context_page_version",
            occurred_at=created_at,
        )
        for version_id, merged_into_id, created_at in rows
    ]


def _citing_page_map(
    session: Session, citations: list[ContextCitation]
) -> dict[UUID, UUID]:
    """Map citations to a page only when they belong to its published head."""
    version_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind
        in (
            "context_page_version",
            "context_page_whole",
            "context_page_section",
        )
    }
    version_owners: dict[UUID, tuple[UUID, UUID]] = {}
    if version_targets:
        version_owners = {
            version_id: (page_id, version_id)
            for version_id, page_id in session.execute(
                select(ContextPageVersion.id, ContextPageVersion.page_id).where(
                    ContextPageVersion.id.in_(version_targets)
                )
            ).all()
        }

    claim_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind == "context_claim"
    }
    claim_owners: dict[UUID, tuple[UUID, UUID]] = {}
    if claim_targets:
        claim_owners = {
            claim_id: (page_id, version_id)
            for claim_id, version_id, page_id in session.execute(
                select(
                    ContextClaim.id,
                    ContextPageVersion.id,
                    ContextPageVersion.page_id,
                )
                .join(
                    ContextPageVersion,
                    ContextClaim.page_version_id == ContextPageVersion.id,
                )
                .where(ContextClaim.id.in_(claim_targets))
            ).all()
        }

    page_targets = {
        citation.target_id
        for citation in citations
        if citation.target_kind == "context_page"
    }
    owner_by_citation: dict[UUID, tuple[UUID, UUID | None]] = {}
    for citation in citations:
        if citation.target_kind in (
            "context_page_version",
            "context_page_whole",
            "context_page_section",
        ):
            owner = version_owners.get(citation.target_id)
        elif citation.target_kind == "context_claim":
            owner = claim_owners.get(citation.target_id)
        elif citation.target_kind == "context_page":
            owner = (citation.target_id, None)
        else:
            owner = None
        if owner is not None:
            owner_by_citation[citation.id] = owner

    page_ids = page_targets | {
        page_id for page_id, _version_id in owner_by_citation.values()
    }
    published_by_page: dict[UUID, UUID | None] = {}
    if page_ids:
        published_by_page = dict(
            session.execute(
                select(ContextPage.id, ContextPage.current_published_version_id).where(
                    ContextPage.id.in_(page_ids)
                )
            ).all()
        )

    mapping: dict[UUID, UUID] = {}
    for citation_id, (page_id, version_id) in owner_by_citation.items():
        published_version_id = published_by_page.get(page_id)
        if published_version_id is not None and (
            version_id is None or version_id == published_version_id
        ):
            mapping[citation_id] = page_id
    return mapping


def _workspace_page_ids(
    session: Session, page_ids: set[UUID], workspace_id: UUID
) -> set[UUID]:
    if not page_ids:
        return set()
    return set(
        session.scalars(
            select(ContextPage.id)
            .where(ContextPage.id.in_(page_ids))
            .where(ContextPage.workspace_id == workspace_id)
        ).all()
    )


# Registry: edge_kind -> extractor. New edge-bearing relations must register
# here (the completeness test fails otherwise).
EDGE_EXTRACTORS: dict[str, EdgeExtractor] = {}


def _register(edge_kind: str, rebuild):
    class _FnExtractor:
        def __init__(self, fn):
            self._fn = fn

        def rebuild_rows(self, session: Session, workspace_id: UUID):
            return self._fn(session, workspace_id)

    EDGE_EXTRACTORS[edge_kind] = _FnExtractor(rebuild)


_register("ask_citation", _ask_citation_rows)
_register("promoted_from", _promoted_from_rows)
_register("page_provenance", _page_provenance_rows)
_register("library_sync_version", _library_sync_version_rows)
_register("wikilink", _wikilink_rows)
_register("tree", _tree_rows)
_register("citation_page", _citation_page_rows)
_register("citation_source", _citation_source_rows)
_register("version_parent", _version_parent_rows)
_register("version_merge", _version_merge_rows)
_register("co_access", _co_access_rows)


# Roster of schema relations that bear edges (design §19.2/§19.5.4). Values are
# metadata-derived source signatures; the completeness test compares these with
# Base.metadata so this list cannot be satisfied merely by copying extractor
# names. Multiple source columns may form one logical edge relation.
EDGE_BEARING_RELATIONS: dict[str, tuple[str, ...]] = {
    "ask_citation": (
        "ask_turn_citation.ask_turn_id->ask_turn.id",
        "ask_turn_citation.target_kind+target_id",
    ),
    "promoted_from": ("ask_turn.promoted_context_page_id->context_page.id",),
    "page_provenance": (
        "page_provenance.page_id->context_page.id",
        "page_provenance.source_kind+source_id",
    ),
    "library_sync_version": ("library_item_version.sync_run_id->library_sync_run.id",),
    "wikilink": (
        "page_links.source_page_id->context_page.id",
        "page_links.source_version_id->context_page_version.id",
        "page_links.target_page_id->context_page.id",
    ),
    "tree": (
        "workspace_tree_node.parent_id->workspace_tree_node.id",
        "workspace_tree_node.target_kind+target_id",
        "ask_session.default_context_parent_tree_node_id->workspace_tree_node.id",
        "ask_session.placement_tree_node_id->workspace_tree_node.id",
        "ask_turn.recommended_placement_tree_node_id->workspace_tree_node.id",
        "context_ingress_proposal.proposed_parent_tree_node_id->workspace_tree_node.id",
    ),
    "citation_owner": ("context_citation.target_kind+target_id",),
    "citation_page": (
        "context_citation.cited_context_page_version_id->context_page_version.id",
    ),
    "citation_source": (
        "context_citation.library_artifact_id->library_artifact.id",
        "context_citation.library_item_version_id->library_item_version.id",
    ),
    "version_parent": (
        "context_page_version.parent_version_id->context_page_version.id",
        "library_item_version.previous_version_id->library_item_version.id",
    ),
    "version_merge": (
        "context_page_version.merged_into_version_id->context_page_version.id",
    ),
    "version_membership": (
        "agent_page_version.agent_page_id->agent_page.id",
        "context_page_version.page_id->context_page.id",
        "library_artifact.library_item_version_id->library_item_version.id",
        "library_item_version.library_item_id->library_item.id",
    ),
    "context_claim_version": (
        "context_claim.page_version_id->context_page_version.id",
    ),
    "co_access": (
        "mcp_chat_artifact_link.chat_session_id->mcp_chat_session.id",
        "mcp_chat_artifact_link.last_mcp_tool_call_id->mcp_tool_call.id",
        "mcp_chat_artifact_link.resource_kind+resource_id",
    ),
    "tool_call_artifact": (
        "mcp_chat_tool_call_artifact.chat_session_id->mcp_chat_session.id",
        "mcp_chat_tool_call_artifact.mcp_tool_call_id->mcp_tool_call.id",
        "mcp_chat_tool_call_artifact.resource_kind+resource_id",
    ),
    "change_lineage": (
        "change_operation.change_set_id->change_set.id",
        "change_operation.target_type+target_id",
        "change_set.origin_chat_session_id->mcp_chat_session.id",
        "context_ingress_proposal.accepted_change_set_id->change_set.id",
        "review_decision.review_item_id->review_item.id",
        "review_item.change_set_id->change_set.id",
    ),
    "review_routing": ("review_decision.audience_kind+audience_user_id",),
    "proposal_source": (
        "context_ingress_proposal.source_ask_turn_id->ask_turn.id",
        "proposal_source_item.library_item_id->library_item.id",
        "proposal_source_item.proposal_id->context_ingress_proposal.id",
    ),
    "bundle_membership": (
        "agent_input_bundle_item.agent_page_version_id->agent_page_version.id",
        "agent_input_bundle_item.bundle_id->agent_input_bundle.id",
        "agent_input_bundle_item.context_page_version_id->context_page_version.id",
        "agent_input_bundle_item.library_artifact_id->library_artifact.id",
        "ask_turn.used_agent_input_bundle_id->agent_input_bundle.id",
    ),
    "ask_session_turn": ("ask_turn.ask_session_id->ask_session.id",),
    "feedback_target": ("feedback_item.target_type+target_id",),
    "feedback_resolution": ("feedback_item.resolved_by_change_set_id->change_set.id",),
    "tool_call_output": (
        "mcp_tool_call.agent_input_bundle_id->agent_input_bundle.id",
        "mcp_tool_call.audit_event_id->audit_event.id",
        "mcp_tool_call.change_set_id->change_set.id",
    ),
    "audit_target": ("audit_event.target_kind+target_id",),
    "discovery_lineage": (
        "library_discovery_candidate.accepted_library_item_id->library_item.id",
        "library_discovery_candidate.discovery_run_id->library_discovery_run.id",
        "library_discovery_run.job_id->job.id",
        "library_sync_run.job_id->job.id",
        "library_sync_run.library_item_id->library_item.id",
    ),
}

EDGE_RELATION_SOURCES: dict[str, str] = {
    source: relation
    for relation, sources in EDGE_BEARING_RELATIONS.items()
    for source in sources
}

# Relations knowingly not yet projected. Each reason pins the implementation
# gate so an omission cannot masquerade as completeness.
DEFERRED_EDGE_RELATIONS: dict[str, str] = {
    relation: "Deferred to Sprint-3 G1 (design §19.5/§19.6)."
    for relation in {
        "ask_session_turn",
        "audit_target",
        "bundle_membership",
        "change_lineage",
        "citation_owner",
        "context_claim_version",
        "discovery_lineage",
        "feedback_resolution",
        "feedback_target",
        "proposal_source",
        "review_routing",
        "tool_call_output",
        "version_membership",
    }
}
DEFERRED_EDGE_RELATIONS.update(
    {
        "tool_call_artifact": (
            "Consumed as the call-level audit source; session-level co_access "
            "is projected separately. A call-level graph edge waits for "
            "measured demand."
        ),
    }
)


def refresh_page_edge_projection(
    session: Session, workspace_id: UUID, page_id: UUID
) -> None:
    """Write-through refresh of one page's outgoing curated edges.

    Publish must not wait for a reindex to update wikilink/citation
    neighbors; scoped delete + re-extract keeps the projection fresh in the
    same transaction (failure fails the publish, like other projections).
    """
    for edge_kind, extractor in (
        ("wikilink", _wikilink_rows),
        ("citation_page", _citation_page_rows),
        ("citation_source", _citation_source_rows),
    ):
        session.execute(
            delete(EdgeProjection)
            .where(EdgeProjection.workspace_id == workspace_id)
            .where(EdgeProjection.edge_kind == edge_kind)
            .where(EdgeProjection.source_kind == EntityKind.CONTEXT_PAGE.value)
            .where(EdgeProjection.source_id == page_id)
        )
        now = datetime.now(UTC)
        # Scoped extraction: a publish must not pay O(workspace) citation
        # scans; the extractors restrict to this page's link/citation rows.
        for row in extractor(session, workspace_id, citing_page_id=page_id):
            if row["source_id"] != page_id:
                continue
            session.add(EdgeProjection(**row, last_seen_at=now))
    session.flush()
    bump_edge_version(session, workspace_id)


def refresh_tree_node_edge_projection(
    session: Session,
    workspace_id: UUID,
    node_ids: set[UUID],
    *,
    remove_outgoing: bool = False,
) -> None:
    """Incrementally refresh placement edges owned by changed tree nodes.

    Entity targets can have non-primary placements, so target edges are
    replaced as a small group. Moving a folder preserves its outgoing child
    edges; archive cascades explicitly request their removal.
    """
    if not node_ids:
        return
    nodes = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.id.in_(node_ids),
        )
    ).all()
    folder_ids = {node.id for node in nodes if node.node_kind == "folder"}
    targets_by_kind: dict[str, set[UUID]] = {}
    for node in nodes:
        if node.target_kind is not None and node.target_id is not None:
            targets_by_kind.setdefault(node.target_kind, set()).add(node.target_id)

    target_conditions = []
    if folder_ids:
        target_conditions.append(
            and_(
                EdgeProjection.target_kind == EntityKind.WORKSPACE_TREE_NODE.value,
                EdgeProjection.target_id.in_(folder_ids),
            )
        )
    target_conditions.extend(
        and_(
            EdgeProjection.target_kind == target_kind,
            EdgeProjection.target_id.in_(target_ids),
        )
        for target_kind, target_ids in targets_by_kind.items()
    )
    if target_conditions:
        session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "tree",
                or_(*target_conditions),
            )
        )
    if remove_outgoing:
        session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "tree",
                EdgeProjection.source_kind == EntityKind.WORKSPACE_TREE_NODE.value,
                EdgeProjection.source_id.in_(node_ids),
            )
        )

    candidate_conditions = []
    if folder_ids:
        candidate_conditions.append(WorkspaceTreeNode.id.in_(folder_ids))
    candidate_conditions.extend(
        and_(
            WorkspaceTreeNode.target_kind == target_kind,
            WorkspaceTreeNode.target_id.in_(target_ids),
        )
        for target_kind, target_ids in targets_by_kind.items()
    )
    if not candidate_conditions:
        session.flush()
        bump_edge_version(session, workspace_id)
        return
    candidates = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            or_(*candidate_conditions),
        )
    ).all()
    parent_ids = {node.parent_id for node in candidates if node.parent_id is not None}
    parents = (
        {
            parent.id: parent
            for parent in session.scalars(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.id.in_(parent_ids),
                )
            ).all()
        }
        if parent_ids
        else {}
    )
    seen: set[tuple[object, ...]] = set()
    now = datetime.now(UTC)
    for node in candidates:
        row = _tree_edge_row(workspace_id, node, parents.get(node.parent_id))
        if row is None:
            continue
        identity = (
            row["source_kind"],
            row["source_id"],
            row["target_kind"],
            row["target_id"],
            row["edge_kind"],
        )
        if identity in seen:
            continue
        seen.add(identity)
        session.add(EdgeProjection(**row, last_seen_at=now))
    session.flush()
    bump_edge_version(session, workspace_id)


def refresh_tree_edge_projection(session: Session, workspace_id: UUID) -> None:
    """Whole-workspace re-extract of the tree edges (reindex/test helper).

    NOT the production write-through seam: tree mutations call the scoped
    ``refresh_tree_node_edge_projection`` so a single node change never pays
    an O(workspace) rebuild. A tripwire test asserts production paths do not
    reach here; keep it that way.
    """
    session.execute(
        delete(EdgeProjection)
        .where(EdgeProjection.workspace_id == workspace_id)
        .where(EdgeProjection.edge_kind == "tree")
    )
    now = datetime.now(UTC)
    for row in _tree_rows(session, workspace_id):
        session.add(EdgeProjection(**row, last_seen_at=now))
    session.flush()
    bump_edge_version(session, workspace_id)


def refresh_co_access_edge_projection(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
) -> int:
    rows = _co_access_rows(session, workspace_id, now=now)
    projected = {
        (
            str(row["source_kind"]),
            row["source_id"],
            str(row["target_kind"]),
            row["target_id"],
            float(row["weight"]),
        )
        for row in rows
    }
    existing = {
        (
            str(source_kind),
            source_id,
            str(target_kind),
            target_id,
            float(weight),
        )
        for source_kind, source_id, target_kind, target_id, weight in session.execute(
            select(
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
                EdgeProjection.weight,
            ).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "co_access",
            )
        ).all()
    }
    if existing == projected:
        return len(rows)
    session.execute(
        delete(EdgeProjection).where(
            EdgeProjection.workspace_id == workspace_id,
            EdgeProjection.edge_kind == "co_access",
        )
    )
    seen_at = now or datetime.now(UTC)
    for row in rows:
        session.add(EdgeProjection(**row, last_seen_at=seen_at))
    session.flush()
    bump_edge_version(session, workspace_id)
    return len(rows)


def rebuild_edge_projection(session: Session, workspace_id: UUID) -> dict[str, int]:
    """Replace the workspace's edge_projection rows for all registered kinds.

    Returns per-edge-kind row counts. Idempotent: repeated runs produce the
    same rows (uq_edge_projection_identity would fail loudly otherwise).
    """

    session.execute(
        delete(EdgeProjection)
        .where(EdgeProjection.workspace_id == workspace_id)
        .where(EdgeProjection.edge_kind.in_(EDGE_EXTRACTORS.keys()))
    )
    now = datetime.now(UTC)
    counts: dict[str, int] = {}
    for edge_kind, extractor in EDGE_EXTRACTORS.items():
        rows = extractor.rebuild_rows(session, workspace_id)
        counts[edge_kind] = len(rows)
        for row in rows:
            session.add(EdgeProjection(**row, last_seen_at=now))
    session.flush()
    bump_edge_version(session, workspace_id)
    return counts

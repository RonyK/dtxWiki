"""Actor-visible, document-grain Workspace Knowledge Map projection.

The service intentionally does not share a scene cache. Every call derives the
published Context and canonical current Library universe for the current actor,
then filters authored relations before metrics or aggregation. This order is a
privacy boundary: hidden rows never enter a count, cluster, revision, cursor, or
label.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import hmac
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased, load_only

from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    PageLink,
)
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.schemas.workspace_graph import (
    WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT,
    WorkspaceGraphCapabilities,
    WorkspaceGraphClusterNode,
    WorkspaceGraphDocumentNode,
    WorkspaceGraphEdge,
    WorkspaceGraphFacetCounts,
    WorkspaceGraphFacetValue,
    WorkspaceGraphFocus,
    WorkspaceGraphFolder,
    WorkspaceGraphFolderEdgeSlice,
    WorkspaceGraphFolderKey,
    WorkspaceGraphFolderMembership,
    WorkspaceGraphFolderTopology,
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphPlacementRef,
    WorkspaceGraphSceneCounts,
    WorkspaceGraphScenePayload,
)
from backend.services.actor_visible_read import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
    library_item_visible,
)
from backend.services.read_errors import ReadValidationError

ALGORITHM_VERSION = "visible-component-pack.v3"
VISIBILITY_POLICY_VERSION = "published-document-visibility.v1"
DEFAULT_PLACEMENT_LABEL = "No visible placement"
_EMPTY_GENERATED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_CURSOR_VERSION = 1

_DocumentKey = tuple[str, UUID]


@dataclass(frozen=True)
class _VisibleFolder:
    id: UUID
    label: str
    parent_folder_id: UUID | None


@dataclass
class _Placement:
    tree_node_id: UUID
    folder_id: UUID | None
    folder_label: str
    path: str
    ancestor_ids: tuple[UUID, ...]
    folder_ancestors: tuple[_VisibleFolder, ...] = ()


@dataclass
class _Document:
    key: _DocumentKey
    entity_id: UUID
    entity_kind: str
    label: str
    path: str
    head_id: UUID
    updated_at: datetime
    primary: _Placement | None = None
    secondary: list[_Placement] = field(default_factory=list)
    secondary_revision: tuple[_Placement, ...] = ()
    secondary_capped: bool = False
    reference_count: int = 0
    evidence_count: int = 0

    @property
    def node_id(self) -> str:
        return f"document:{self.entity_kind}:{self.entity_id}"

    @property
    def folder_id(self) -> UUID | None:
        return self.primary.folder_id if self.primary is not None else None

    @property
    def folder_label(self) -> str:
        return (
            self.primary.folder_label
            if self.primary is not None
            else DEFAULT_PLACEMENT_LABEL
        )


@dataclass(frozen=True)
class _Relation:
    source: _DocumentKey
    target: _DocumentKey
    kind: str
    layer: str
    occurred_at: datetime
    occurrence_weight: int = 1


@dataclass(frozen=True, eq=False)
class _Group:
    members: tuple[_DocumentKey, ...]
    cluster_kind: str | None = None
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        digest = (
            _key_token(self.members[0])
            if self.is_document
            else _group_member_digest(self.members)
        )
        object.__setattr__(
            self,
            "identity",
            f"{self.cluster_kind or 'document'}:{digest}",
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Group):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)

    @property
    def is_document(self) -> bool:
        return len(self.members) == 1 and self.cluster_kind is None


class _MappedRelationPartition:
    """Maintain exact kind/layer identities while rendered groups are merged.

    A merge only remaps identities incident to its two groups. Unrelated
    relations are neither rescanned nor reindexed, and duplicate identities
    collapse exactly as they do in final materialization.
    """

    def __init__(self, groups: list[_Group], relations: list[_Relation]) -> None:
        group_index = {
            member: index
            for index, group in enumerate(groups)
            for member in group.members
        }
        self._relations: set[tuple[int, int, str, str]] = set()
        self._incident: dict[int, set[tuple[int, int, str, str]]] = defaultdict(set)
        for relation in relations:
            self._add(
                (
                    group_index[relation.source],
                    group_index[relation.target],
                    relation.kind,
                    relation.layer,
                )
            )

    @property
    def count(self) -> int:
        return len(self._relations)

    def merge(self, first: int, second: int, merged: int) -> None:
        affected = self._incident.pop(first, set()) | self._incident.pop(second, set())
        for identity in affected:
            self._relations.remove(identity)
            source, target, _kind, _layer = identity
            if source not in (first, second):
                self._incident[source].discard(identity)
            if target not in (first, second):
                self._incident[target].discard(identity)
        for source, target, kind, layer in affected:
            self._add(
                (
                    merged if source in (first, second) else source,
                    merged if target in (first, second) else target,
                    kind,
                    layer,
                )
            )

    def _add(self, identity: tuple[int, int, str, str]) -> None:
        self._relations.add(identity)
        self._incident[identity[0]].add(identity)
        self._incident[identity[1]].add(identity)


@dataclass
class _RenderedScene:
    groups: list[_Group]
    nodes: list[WorkspaceGraphDocumentNode | WorkspaceGraphClusterNode]
    edges: list[WorkspaceGraphEdge]
    representation: str
    omission_reasons: list[str]
    expansion_cursor: str | None = None


class WorkspaceGraphService:
    """Build a bounded Workspace scene from one captured visible universe."""

    def __init__(
        self,
        *,
        cursor_secret: str,
    ) -> None:
        if not cursor_secret:
            raise ValueError("cursor_secret must not be empty")
        self._cursor_secret = cursor_secret.encode("utf-8")

    def query(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: WorkspaceGraphOverviewRequest,
        actor: ActorContext,
    ) -> WorkspaceGraphScenePayload:
        # Capture the complete actor-visible published document/head universe
        # before applying any scene controls. Later relation reads use canonical
        # head joins and are checked against these captured IDs. A concurrent
        # publication can therefore omit a relation for this response, but it
        # cannot mix an old-head document with a new-head relation.
        documents = self._load_documents(session, workspace_id, actor)
        self._attach_tree_placements(session, workspace_id, documents)
        # Revision and freshness describe the whole actor-visible published
        # document/relation universe, not one lens/filter scene. Applying a
        # filter therefore preserves viewport/pin state while any visible
        # content or authored relation change invalidates expansion cursors.
        revision_relations = self._load_relation_universe(
            session, workspace_id, documents
        )
        graph_revision = self._graph_revision(documents, revision_relations)
        generated_at = self._generated_at(documents, revision_relations)

        # Folder facets are self-excluding: retain the scene produced by every
        # other data filter, but before folder and quick-filter selection. This
        # keeps unselected folders available after Apply so the drawer can be
        # reopened to add or switch folders without issuing a broader read.
        folder_facet_documents = self._apply_document_filters(
            documents,
            request,
            apply_folder_filters=False,
        )
        folder_facet_relations = self._scene_relations(
            revision_relations,
            folder_facet_documents,
            lens=request.lens,
        )
        folder_facet_documents, _folder_facet_relations = (
            self._apply_relation_time_filter(
                folder_facet_documents,
                folder_facet_relations,
                request,
            )
        )

        documents = self._apply_document_filters(documents, request)

        relations = self._scene_relations(
            revision_relations,
            documents,
            lens=request.lens,
        )
        documents, relations = self._apply_relation_time_filter(
            documents,
            relations,
            request,
        )

        # Metrics are defined on the actor-visible universe after applied data
        # filters but before quick-filter selection. Quick filters select from
        # that universe; they do not redefine a node's displayed reference or
        # evidence count on the induced result subgraph.
        self._apply_metrics(documents, relations)
        quick_filter_counts = self._quick_filter_counts(documents, relations)
        documents, relations = self._apply_quick_filters(
            documents,
            relations,
            request.quick_filters,
        )

        request_fingerprint = self._request_fingerprint(request)
        actor_fingerprint = self._actor_fingerprint(actor)
        rendered = self._render(
            documents,
            relations,
            request,
            workspace_id=workspace_id,
            graph_revision=graph_revision,
            request_fingerprint=request_fingerprint,
            actor_fingerprint=actor_fingerprint,
        )

        warnings: list[str] = []
        if any(document.secondary_capped for document in documents.values()):
            warnings.append("secondary_placements_capped")
        projection_partial = request.lens in {"compare", "growth", "provenance"}
        if projection_partial:
            warnings.append(f"{request.lens}_projection_not_available")
            if "projection_partial" not in rendered.omission_reasons:
                rendered.omission_reasons.append("projection_partial")

        truncated = bool(rendered.omission_reasons)
        completeness = (
            "partial"
            if projection_partial
            else "capped"
            if rendered.representation == "aggregated"
            else "complete"
        )
        facet_counts = self._facet_counts(
            documents,
            relations,
            folder_documents=folder_facet_documents,
            quick_filter_counts=quick_filter_counts,
        )
        folder_topology = self._folder_topology(documents, relations, rendered)
        if folder_topology is None:
            # This is an explicit optional-feature fallback, not a clipped or
            # invented membership distribution. The authored scene is intact.
            warnings.append("folder_topology_row_budget_exceeded")
        return WorkspaceGraphScenePayload(
            graph_revision=graph_revision,
            generated_at=generated_at,
            algorithm_version=ALGORITHM_VERSION,
            metric_basis=self._metric_basis(request.lens),
            representation=rendered.representation,
            completeness=completeness,
            truncated=truncated,
            counts=WorkspaceGraphSceneCounts(
                candidate_documents=len(documents),
                candidate_relations=len(relations),
                rendered_documents=sum(
                    node.grain == "document" for node in rendered.nodes
                ),
                rendered_clusters=sum(
                    node.grain == "cluster" for node in rendered.nodes
                ),
                rendered_relations=len(rendered.edges),
            ),
            omission_reasons=rendered.omission_reasons,
            nodes=rendered.nodes,
            edges=rendered.edges,
            folder_topology=folder_topology,
            facet_counts=facet_counts,
            expansion_cursor=rendered.expansion_cursor,
            warnings=warnings,
            capabilities=WorkspaceGraphCapabilities(
                expand=rendered.expansion_cursor is not None,
                evidence=any(relation.layer == "evidence" for relation in relations),
                # Export is a client-side serialization of this exact scene;
                # it performs no broader fetch and therefore preserves ID parity.
                export=True,
                directory=True,
                compare=False,
                growth=False,
                provenance=False,
            ),
            focus=_focus_result(request.focus_entity_id, rendered.groups, documents),
        )

    @staticmethod
    def _load_documents(
        session: Session,
        workspace_id: UUID,
        actor: ActorContext,
    ) -> dict[_DocumentKey, _Document]:
        documents: dict[_DocumentKey, _Document] = {}
        page_heads = session.execute(
            select(
                ContextPage.id,
                ContextPage.title,
                ContextPageVersion.id,
                ContextPageVersion.created_at,
            )
            .join(
                ContextPageVersion,
                and_(
                    ContextPageVersion.id == ContextPage.current_published_version_id,
                    ContextPageVersion.workspace_id == ContextPage.workspace_id,
                ),
            )
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPageVersion.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
                ContextPageVersion.page_id == ContextPage.id,
                ContextPageVersion.version_status.in_(
                    PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
            )
            .order_by(ContextPage.id)
        ).all()
        for page_id, title, head_id, head_created_at in page_heads:
            key = ("context", page_id)
            documents[key] = _Document(
                key=key,
                entity_id=page_id,
                entity_kind="context",
                label=title,
                path=f"/context?target={page_id}",
                head_id=head_id,
                updated_at=_aware(head_created_at),
            )

        # The canonical Library head is the ready pointer; only an item that
        # has never been projected falls back to its legacy current pointer
        # (mirrors get_canonical_library_version, which library_item_visible
        # below then re-checks per actor).
        item_heads = session.execute(
            select(LibraryItem, LibraryItemVersion)
            .options(
                load_only(
                    LibraryItem.id,
                    LibraryItem.workspace_id,
                    LibraryItem.lifecycle_status,
                    LibraryItem.trust_level,
                    LibraryItem.current_ready_version_id,
                    LibraryItem.current_version_id,
                    LibraryItem.title,
                    LibraryItem.uri,
                    raiseload=True,
                ),
                load_only(
                    LibraryItemVersion.id,
                    LibraryItemVersion.library_item_id,
                    LibraryItemVersion.processing_status,
                    LibraryItemVersion.updated_at,
                    raiseload=True,
                ),
            )
            .join(
                LibraryItemVersion,
                LibraryItemVersion.id
                == func.coalesce(
                    LibraryItem.current_ready_version_id,
                    LibraryItem.current_version_id,
                ),
            )
            .where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItemVersion.library_item_id == LibraryItem.id,
            )
            .order_by(LibraryItem.id)
        ).all()
        for item, head in item_heads:
            if not library_item_visible(
                item,
                actor,
                workspace_id=workspace_id,
                version=head,
            ):
                continue
            key = ("library", item.id)
            documents[key] = _Document(
                key=key,
                entity_id=item.id,
                entity_kind="library",
                label=item.title or item.uri,
                path=f"/library?target={item.id}",
                head_id=head.id,
                updated_at=_aware(head.updated_at),
            )
        return documents

    def _attach_tree_placements(
        self,
        session: Session,
        workspace_id: UUID,
        documents: dict[_DocumentKey, _Document],
    ) -> None:
        if not documents:
            return
        node_rows = session.execute(
            select(
                WorkspaceTreeNode,
                LibraryArtifact.library_item_version_id.label(
                    "artifact_library_item_version_id"
                ),
            )
            .outerjoin(
                LibraryArtifact,
                and_(
                    WorkspaceTreeNode.target_kind == "library_artifact",
                    LibraryArtifact.id == WorkspaceTreeNode.target_id,
                    LibraryArtifact.workspace_id == WorkspaceTreeNode.workspace_id,
                    LibraryArtifact.workspace_id == workspace_id,
                ),
            )
            .where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
            .order_by(
                WorkspaceTreeNode.section,
                WorkspaceTreeNode.sort_key,
                WorkspaceTreeNode.title,
                WorkspaceTreeNode.id,
            )
        ).all()
        nodes = [node for node, _artifact_version_id in node_rows]
        nodes_by_id = {node.id: node for node in nodes}
        item_by_head = {
            document.head_id: document.entity_id
            for document in documents.values()
            if document.entity_kind == "library"
        }
        artifact_items = {
            node.target_id: item_by_head[artifact_version_id]
            for node, artifact_version_id in node_rows
            if node.target_kind == "library_artifact"
            and node.target_id is not None
            and artifact_version_id in item_by_head
        }

        initially_visible: set[UUID] = set()
        for node in nodes:
            if node.target_id is None:
                initially_visible.add(node.id)
            elif (
                node.target_kind == "context_page"
                and (
                    "context",
                    node.target_id,
                )
                in documents
            ):
                initially_visible.add(node.id)
            elif (
                node.target_kind == "library_item"
                and (
                    "library",
                    node.target_id,
                )
                in documents
            ):
                initially_visible.add(node.id)
            elif (
                node.target_kind == "library_artifact"
                and node.target_id in artifact_items
            ):
                initially_visible.add(node.id)

        closure_cache: dict[UUID, bool] = {}

        def chain_visible(node_id: UUID) -> bool:
            cached = closure_cache.get(node_id)
            if cached is not None:
                return cached
            closure_cache[node_id] = False
            node = nodes_by_id.get(node_id)
            visible = (
                node is not None
                and node_id in initially_visible
                and (node.parent_id is None or chain_visible(node.parent_id))
            )
            closure_cache[node_id] = visible
            return visible

        visible_ids = {node.id for node in nodes if chain_visible(node.id)}

        placements: dict[_DocumentKey, list[tuple[int, _Placement]]] = defaultdict(list)
        for node in nodes:
            if node.id not in visible_ids or node.target_id is None:
                continue
            if node.target_kind == "context_page":
                key = ("context", node.target_id)
                priority = 0
            elif node.target_kind == "library_item":
                key = ("library", node.target_id)
                priority = 0
            elif node.target_kind == "library_artifact":
                item_id = artifact_items.get(node.target_id)
                if item_id is None:
                    continue
                key = ("library", item_id)
                priority = 1
            else:
                continue
            if key not in documents:
                continue
            placement = self._placement_for_node(nodes_by_id, node)
            placements[key].append((priority, placement))

        for key, candidates in placements.items():
            document = documents[key]
            candidates.sort(
                key=lambda pair: (
                    not nodes_by_id[pair[1].tree_node_id].primary_placement,
                    pair[0],
                    str(pair[1].tree_node_id),
                )
            )
            primary_index = next(
                (
                    index
                    for index, (_priority, placement) in enumerate(candidates)
                    if nodes_by_id[placement.tree_node_id].primary_placement
                ),
                None,
            )
            if primary_index is not None:
                _priority, document.primary = candidates.pop(primary_index)
            secondary = [placement for _priority, placement in candidates]
            document.secondary_capped = len(secondary) > 32
            document.secondary = secondary[:32]
            document.secondary_revision = tuple(secondary)

    @staticmethod
    def _placement_for_node(
        nodes: dict[UUID, WorkspaceTreeNode],
        node: WorkspaceTreeNode,
    ) -> _Placement:
        ancestors: list[WorkspaceTreeNode] = []
        current = nodes.get(node.parent_id) if node.parent_id is not None else None
        seen: set[UUID] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            ancestors.append(current)
            current = (
                nodes.get(current.parent_id) if current.parent_id is not None else None
            )
        ancestors.reverse()
        folder_ancestors = [item for item in ancestors if item.node_kind == "folder"]
        folder = folder_ancestors[-1] if folder_ancestors else None
        route = " / ".join([item.title for item in ancestors] + [node.title])
        return _Placement(
            tree_node_id=node.id,
            folder_id=folder.id if folder is not None else None,
            folder_label=folder.title
            if folder is not None
            else DEFAULT_PLACEMENT_LABEL,
            path=route or node.title,
            ancestor_ids=tuple(item.id for item in ancestors),
            folder_ancestors=tuple(
                _VisibleFolder(
                    id=item.id,
                    label=item.title,
                    parent_folder_id=(
                        folder_ancestors[index - 1].id if index else None
                    ),
                )
                for index, item in enumerate(folder_ancestors)
            ),
        )

    @staticmethod
    def _apply_document_filters(
        documents: dict[_DocumentKey, _Document],
        request: WorkspaceGraphOverviewRequest,
        *,
        apply_folder_filters: bool = True,
    ) -> dict[_DocumentKey, _Document]:
        search = request.search.casefold() if request.search else None
        include = set(request.folders.include)
        exclude = set(request.folders.exclude)
        only = set(request.folders.only)
        filtered: dict[_DocumentKey, _Document] = {}
        for key, document in documents.items():
            if document.entity_kind not in request.document_kinds:
                continue
            if search:
                # Navigation URLs contain shared route words and entity UUIDs,
                # not authored search metadata. Placements have already passed
                # actor-visible ancestor closure; only the primary path defines
                # this document's map location, never a secondary placement.
                primary_path = document.primary.path if document.primary else ""
                if search not in f"{document.label} {primary_path}".casefold():
                    continue
            placement_ids: set[WorkspaceGraphFolderKey] = (
                set(document.primary.ancestor_ids) | {document.primary.tree_node_id}
                if document.primary is not None
                else set()
            )
            # Match the emitted folder facet: root placements and documents
            # without a visible primary folder both belong to "unplaced".
            # Invisible ancestor IDs never enter the membership set.
            if document.folder_id is None:
                placement_ids.add("unplaced")
            if apply_folder_filters:
                if only and not placement_ids.intersection(only):
                    continue
                if include and not placement_ids.intersection(include):
                    continue
                if exclude and placement_ids.intersection(exclude):
                    continue
            if request.time.basis == "document_updated" and not _in_time_window(
                document.updated_at,
                request.time.from_,
                request.time.to,
            ):
                continue
            filtered[key] = document
        return filtered

    @staticmethod
    def _load_relation_universe(
        session: Session,
        workspace_id: UUID,
        documents: dict[_DocumentKey, _Document],
    ) -> list[_Relation]:
        """Load canonical-head relations that agree with the captured heads.

        The mutable ``EdgeProjection`` table is deliberately not consulted.
        PageLink and ContextCitation are the authoritative rows and carry the
        authoritative occurrence time. SQL joins bound each source to its
        canonical head, then Python compares that source with the document
        capture. Under READ COMMITTED, a concurrent publication may therefore
        cause a transient omission; it cannot attach a new-head relation to a
        captured old-head document.
        """

        context_by_id = {
            document.entity_id: document
            for document in documents.values()
            if document.entity_kind == "context"
        }
        library_by_head = {
            document.head_id: document.key
            for document in documents.values()
            if document.entity_kind == "library"
        }
        if not context_by_id:
            return []

        relations: dict[tuple[_DocumentKey, _DocumentKey, str], _Relation] = {}

        def add_relation(
            source: _DocumentKey,
            target: _DocumentKey,
            kind: str,
            occurred_at: datetime | None,
            occurrence_weight: int = 1,
        ) -> None:
            if source not in documents or target not in documents:
                return
            if kind == "wikilink" and source == target:
                return
            occurrence = _aware(occurred_at or documents[source].updated_at)
            identity = (source, target, kind)
            previous = relations.get(identity)
            relations[identity] = _Relation(
                source=source,
                target=target,
                kind=kind,
                layer="knowledge" if kind == "wikilink" else "evidence",
                occurred_at=max(occurrence, previous.occurred_at)
                if previous is not None
                else occurrence,
                # Several authored syntaxes/aliases can resolve to the same
                # page pair. It remains one logical relation, but every link
                # occurrence contributes to the browser's layout input.
                # Each accepted citation row contributes its unit occurrence.
                occurrence_weight=(
                    occurrence_weight
                    + (previous.occurrence_weight if previous is not None else 0)
                ),
            )

        page_links = session.execute(
            select(
                PageLink.source_page_id,
                PageLink.source_version_id,
                PageLink.target_page_id,
                PageLink.occurrence_count,
                PageLink.created_at,
            )
            .join(
                ContextPage,
                and_(
                    ContextPage.id == PageLink.source_page_id,
                    ContextPage.workspace_id == PageLink.workspace_id,
                    ContextPage.current_published_version_id
                    == PageLink.source_version_id,
                ),
            )
            .where(
                PageLink.workspace_id == workspace_id,
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
                PageLink.target_page_id.is_not(None),
            )
            .with_hint(
                PageLink,
                "INDEXED BY idx_page_links_graph_head",
                dialect_name="sqlite",
            )
        ).all()
        for (
            source_page_id,
            source_version_id,
            target_page_id,
            occurrence_count,
            created_at,
        ) in page_links:
            source_document = context_by_id.get(source_page_id)
            if (
                source_document is None
                or source_document.head_id != source_version_id
                or target_page_id is None
            ):
                continue
            add_relation(
                source_document.key,
                ("context", target_page_id),
                "wikilink",
                created_at,
                occurrence_weight=occurrence_count,
            )

        version_target_kinds = (
            "context_page_version",
            "context_page_whole",
            "context_page_section",
        )
        version_source_page = aliased(
            ContextPage,
            name="citation_version_source_page",
        )
        claim_source_page = aliased(
            ContextPage,
            name="citation_claim_source_page",
        )
        claim_source_version = aliased(
            ContextPageVersion,
            name="citation_claim_source_version",
        )
        cited_context_version = aliased(
            ContextPageVersion,
            name="cited_context_version",
        )
        cited_context_page = aliased(
            ContextPage,
            name="cited_context_page",
        )
        cited_library_artifact = aliased(
            LibraryArtifact,
            name="cited_library_artifact",
        )

        citation_target_columns = (
            ContextCitation.cited_context_page_version_id,
            cited_context_page.id.label("cited_context_page_id"),
            ContextCitation.library_item_version_id,
            ContextCitation.library_artifact_id,
            cited_library_artifact.library_item_version_id.label(
                "artifact_library_item_version_id"
            ),
            ContextCitation.created_at,
        )

        def with_citation_targets(statement):
            return (
                statement.outerjoin(
                    cited_context_version,
                    and_(
                        cited_context_version.id
                        == ContextCitation.cited_context_page_version_id,
                        cited_context_version.workspace_id
                        == ContextCitation.workspace_id,
                        cited_context_version.workspace_id == workspace_id,
                        cited_context_version.version_status.in_(
                            PUBLISHED_CONTEXT_VERSION_STATUSES
                        ),
                    ),
                )
                .outerjoin(
                    cited_context_page,
                    and_(
                        cited_context_page.id == cited_context_version.page_id,
                        cited_context_page.workspace_id
                        == cited_context_version.workspace_id,
                        cited_context_page.workspace_id == workspace_id,
                    ),
                )
                .outerjoin(
                    cited_library_artifact,
                    and_(
                        cited_library_artifact.id
                        == ContextCitation.library_artifact_id,
                        cited_library_artifact.workspace_id
                        == ContextCitation.workspace_id,
                        cited_library_artifact.workspace_id == workspace_id,
                    ),
                )
                .with_hint(
                    ContextCitation,
                    "INDEXED BY idx_context_citation_graph_head",
                    dialect_name="sqlite",
                )
            )

        version_citations = with_citation_targets(
            select(
                version_source_page.id.label("source_page_id"),
                ContextCitation.target_id.label("source_head_id"),
                *citation_target_columns,
            ).join(
                version_source_page,
                and_(
                    version_source_page.current_published_version_id
                    == ContextCitation.target_id,
                    version_source_page.workspace_id == ContextCitation.workspace_id,
                    version_source_page.workspace_id == workspace_id,
                ),
            )
        ).where(
            ContextCitation.workspace_id == workspace_id,
            ContextCitation.target_kind.in_(version_target_kinds),
            version_source_page.lifecycle_status != "archived",
        )
        claim_citations = with_citation_targets(
            select(
                claim_source_page.id.label("source_page_id"),
                ContextClaim.page_version_id.label("source_head_id"),
                *citation_target_columns,
            )
            .join(
                ContextClaim,
                and_(
                    ContextClaim.id == ContextCitation.target_id,
                    ContextClaim.workspace_id == ContextCitation.workspace_id,
                    ContextClaim.workspace_id == workspace_id,
                ),
            )
            .join(
                claim_source_version,
                and_(
                    claim_source_version.id == ContextClaim.page_version_id,
                    claim_source_version.workspace_id == ContextClaim.workspace_id,
                    claim_source_version.workspace_id == workspace_id,
                ),
            )
            .join(
                claim_source_page,
                and_(
                    claim_source_page.id == claim_source_version.page_id,
                    claim_source_page.workspace_id == claim_source_version.workspace_id,
                    claim_source_page.workspace_id == workspace_id,
                    claim_source_page.current_published_version_id
                    == claim_source_version.id,
                ),
            )
        ).where(
            ContextCitation.workspace_id == workspace_id,
            ContextCitation.target_kind == "context_claim",
            claim_source_version.version_status.in_(PUBLISHED_CONTEXT_VERSION_STATUSES),
            claim_source_page.lifecycle_status != "archived",
        )

        # Version/claim statements have disjoint target kinds. Every source
        # and target join is to a unique primary key, and captured canonical
        # source heads are rechecked below. Each accepted authored citation
        # row therefore appears once; selecting its UUID for defensive dedup
        # would unnecessarily defeat the graph-head covering index.
        for citation_statement in (version_citations, claim_citations):
            for citation in session.execute(citation_statement).mappings():
                source_document = context_by_id.get(citation["source_page_id"])
                if (
                    source_document is None
                    or source_document.head_id != citation["source_head_id"]
                ):
                    continue
                source = source_document.key
                if citation["cited_context_page_version_id"] is not None:
                    cited_page_id = citation["cited_context_page_id"]
                    target = (
                        ("context", cited_page_id)
                        if cited_page_id is not None
                        and ("context", cited_page_id) in documents
                        else None
                    )
                    kind = "citation_page"
                elif citation["library_artifact_id"] is not None:
                    target = library_by_head.get(
                        citation["artifact_library_item_version_id"]
                    )
                    kind = "citation_source"
                elif citation["library_item_version_id"] is not None:
                    target = library_by_head.get(citation["library_item_version_id"])
                    kind = "citation_source"
                else:
                    target = None
                    kind = "citation_source"
                if target is not None:
                    add_relation(source, target, kind, citation["created_at"])

        return [relations[key] for key in sorted(relations, key=_relation_sort_key)]

    @staticmethod
    def _scene_relations(
        relations: list[_Relation],
        documents: dict[_DocumentKey, _Document],
        *,
        lens: str,
    ) -> list[_Relation]:
        relation_kinds = {
            "combined": {"wikilink", "citation_page", "citation_source"},
            "links": {"wikilink"},
            "evidence": {"citation_page", "citation_source"},
            "compare": {"wikilink", "citation_page", "citation_source"},
            "growth": {"wikilink", "citation_page", "citation_source"},
            "directory": set(),
            "provenance": set(),
        }[lens]
        return [
            relation
            for relation in relations
            if relation.kind in relation_kinds
            and relation.source in documents
            and relation.target in documents
        ]

    @staticmethod
    def _time_is_active(request: WorkspaceGraphOverviewRequest) -> bool:
        return request.time.from_ is not None or request.time.to is not None

    @staticmethod
    def _filter_relations_by_time(
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
    ) -> list[_Relation]:
        return [
            relation
            for relation in relations
            if _in_time_window(
                relation.occurred_at,
                request.time.from_,
                request.time.to,
            )
        ]

    @classmethod
    def _apply_relation_time_filter(
        cls,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
    ) -> tuple[dict[_DocumentKey, _Document], list[_Relation]]:
        if not (
            cls._time_is_active(request) and request.time.basis == "relation_observed"
        ):
            return documents, relations
        relations = cls._filter_relations_by_time(relations, request)
        incident = {relation.source for relation in relations} | {
            relation.target for relation in relations
        }
        documents = {key: value for key, value in documents.items() if key in incident}
        return documents, [
            relation
            for relation in relations
            if relation.source in documents and relation.target in documents
        ]

    @classmethod
    def _quick_filter_counts(
        cls,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
    ) -> dict[str, int]:
        return {
            name: len(cls._keys_for_quick_filter(documents, relations, name))
            for name in (
                "orphans",
                "highly_referenced",
                "cross_folder",
                "recently_connected",
            )
        }

    @classmethod
    def _apply_quick_filters(
        cls,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        filters: Iterable[str],
    ) -> tuple[dict[_DocumentKey, _Document], list[_Relation]]:
        selected = set(documents)
        for filter_name in filters:
            selected &= cls._keys_for_quick_filter(
                documents,
                relations,
                filter_name,
            )
        filtered_documents = {
            key: documents[key] for key in sorted(selected, key=_document_key_sort)
        }
        filtered_relations = [
            relation
            for relation in relations
            if relation.source in selected and relation.target in selected
        ]
        return filtered_documents, filtered_relations

    @staticmethod
    def _keys_for_quick_filter(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        filter_name: str,
    ) -> set[_DocumentKey]:
        if filter_name == "orphans":
            incident = {relation.source for relation in relations} | {
                relation.target for relation in relations
            }
            return set(documents) - incident
        if filter_name == "highly_referenced":
            inbound_sources: dict[_DocumentKey, set[_DocumentKey]] = defaultdict(set)
            for relation in relations:
                if relation.kind == "wikilink":
                    inbound_sources[relation.target].add(relation.source)
            return {
                key for key, sources in inbound_sources.items() if len(sources) >= 3
            }
        if filter_name == "cross_folder":
            selected: set[_DocumentKey] = set()
            for relation in relations:
                source_folder = documents[relation.source].folder_id
                target_folder = documents[relation.target].folder_id
                if (
                    source_folder is not None
                    and target_folder is not None
                    and source_folder != target_folder
                ):
                    selected.update((relation.source, relation.target))
            return selected
        if filter_name == "recently_connected":
            if not relations:
                return set()
            newest = max(relation.occurred_at for relation in relations)
            cutoff = newest.timestamp() - 30 * 24 * 60 * 60
            selected = set()
            for relation in relations:
                if relation.occurred_at.timestamp() >= cutoff:
                    selected.update((relation.source, relation.target))
            return selected
        raise ValueError(f"unsupported workspace graph quick filter: {filter_name}")

    @staticmethod
    def _apply_metrics(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
    ) -> None:
        reference_sources: dict[_DocumentKey, set[_DocumentKey]] = defaultdict(set)
        evidence_sources: dict[_DocumentKey, set[_DocumentKey]] = defaultdict(set)
        for relation in relations:
            if relation.kind == "wikilink":
                reference_sources[relation.target].add(relation.source)
            elif relation.layer == "evidence":
                evidence_sources[relation.target].add(relation.source)
        for key, document in documents.items():
            document.reference_count = len(reference_sources.get(key, set()))
            document.evidence_count = len(evidence_sources.get(key, set()))

    def _render(
        self,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
        *,
        workspace_id: UUID,
        graph_revision: str,
        request_fingerprint: str,
        actor_fingerprint: str,
    ) -> _RenderedScene:
        if (
            len(documents) <= request.budget.documents
            and len(relations) <= request.budget.relations
            and request.expansion is None
        ):
            groups = [
                _Group((key,)) for key in sorted(documents, key=_document_key_sort)
            ]
            return self._materialize_groups(
                groups,
                documents,
                relations,
                omission_reasons=[],
            )

        omission_reasons: list[str] = []
        if len(documents) > request.budget.documents:
            omission_reasons.append("document_budget")
        if len(relations) > request.budget.relations:
            omission_reasons.append("relation_budget")

        groups = self._groups_with_pinned_focus(
            documents,
            relations,
            request,
        ) or self._aggregate_groups(
            documents,
            relations,
            document_budget=request.budget.documents,
            relation_budget=request.budget.relations,
            lens=request.lens,
        )
        expansion_cursor = None
        if request.expansion is not None:
            groups, expansion_cursor = self._expand_groups(
                groups,
                documents,
                relations,
                request,
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
            )
        rendered = self._materialize_groups(
            groups,
            documents,
            relations,
            omission_reasons=omission_reasons or ["document_budget"],
            expansion_cursor=expansion_cursor,
        )
        identity_floor = len(
            {(relation.kind, relation.layer) for relation in relations}
        )
        if identity_floor > request.budget.relations:
            # Even a single packed node has one self-edge per real kind/layer.
            # If that irreducible identity floor exceeds a tiny budget, keep
            # deterministic real edges, explicitly omit the rest, and retain
            # node coverage. Do not invent a mixed relationship or claim the
            # omitted relationships are accounted for by retained edge counts.
            rendered.edges = rendered.edges[: request.budget.relations]
            if "relation_budget" not in rendered.omission_reasons:
                rendered.omission_reasons.append("relation_budget")
        clusters = [group for group in groups if not group.is_document]
        if clusters and rendered.expansion_cursor is None and request.expansion is None:
            pinned = _pinned_focus(groups, documents, request.focus_entity_id)
            first_cluster = next(
                (
                    cluster
                    for cluster in clusters
                    if self._can_expand_cluster(
                        groups,
                        cluster,
                        relations,
                        offset=0,
                        document_budget=request.budget.documents,
                        relation_budget=request.budget.relations,
                        pinned=pinned,
                    )
                ),
                None,
            )
            if first_cluster is not None:
                rendered.expansion_cursor = self._encode_cursor(
                    workspace_id=workspace_id,
                    graph_revision=graph_revision,
                    request_fingerprint=request_fingerprint,
                    actor_fingerprint=actor_fingerprint,
                    cluster_id=_cluster_id(first_cluster),
                    offset=0,
                )
        return rendered

    def _groups_with_pinned_focus(
        self,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
    ) -> list[_Group] | None:
        """Reserve one document slot for a deep link's target.

        The remaining documents are aggregated within one fewer slot and a
        relation budget reduced by the target's own relations, which bounds the
        edges the target can add. ``None`` means the budgets cannot hold the
        target on its own; the caller then aggregates normally and the focus
        resolves to the cluster that contains it.
        """

        focus_key = _focus_key(documents, request.focus_entity_id)
        document_budget = request.budget.documents
        relation_budget = request.budget.relations
        if focus_key is None or document_budget < 2:
            return None
        focus_relation_count = sum(
            focus_key in (relation.source, relation.target) for relation in relations
        )
        if focus_relation_count > relation_budget:
            return None
        remaining = {key: value for key, value in documents.items() if key != focus_key}
        groups = [
            *self._aggregate_groups(
                remaining,
                [
                    relation
                    for relation in relations
                    if focus_key not in (relation.source, relation.target)
                ],
                document_budget=document_budget - 1,
                relation_budget=relation_budget - focus_relation_count,
                lens=request.lens,
            ),
            _Group((focus_key,)),
        ]
        if (
            len(groups) > document_budget
            or _mapped_relation_count(groups, relations) > relation_budget
        ):
            return None
        return sorted(groups, key=lambda group: _group_sort_key(group, documents))

    @staticmethod
    def _aggregate_groups(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        *,
        document_budget: int,
        relation_budget: int,
        lens: str,
    ) -> list[_Group]:
        if not documents:
            return []
        partitions = _semantic_partitions(documents, relations, lens=lens)
        # One honest group per visible folder (Directory) or connected authored
        # component (Links/Evidence). Singleton partitions remain exact docs.
        groups = [
            _Group(
                tuple(partition),
                cluster_kind=(
                    None
                    if len(partition) == 1
                    else "folder_cluster"
                    if lens == "directory"
                    else "link_cluster"
                ),
            )
            for partition in partitions
        ]

        # Disconnected singletons must not consume all slots ahead of linked
        # documents. Keep them represented by one honest residual; exact
        # members are revealed only after the connected prefix, using otherwise
        # idle slots without adding relations. A self-link is still authored.
        incident = {
            key for relation in relations for key in (relation.source, relation.target)
        }
        isolated = [
            group
            for group in groups
            if group.is_document and group.members[0] not in incident
        ]
        if len(isolated) > 1:
            isolated_set = set(isolated)
            groups = [group for group in groups if group not in isolated_set]
            groups.append(
                _Group(
                    tuple(group.members[0] for group in isolated),
                    cluster_kind="other_cluster",
                )
            )

        if len(groups) >= document_budget and len(groups) > 1:
            # A budget full of tiny component clusters reveals no documents.
            # Retain only dominant semantic groups, reserving at least half
            # the slots for exact members. Smaller components share an honest
            # other_cluster residual, never a falsely named linked component.
            semantic_slots = max(1, document_budget // 2)
            dominant_size = len(documents) // semantic_slots + 1
            ranked_groups = sorted(
                groups,
                key=lambda group: (
                    -len(group.members),
                    group.identity,
                ),
            )
            keep = [
                group for group in ranked_groups if len(group.members) >= dominant_size
            ]
            kept = set(keep)
            packed_members = tuple(
                sorted(
                    (
                        member
                        for group in ranked_groups
                        if group not in kept
                        for member in group.members
                    ),
                    key=_document_key_sort,
                )
            )
            groups = [*keep, _Group(packed_members, cluster_kind="other_cluster")]

        # If even the unsplit semantic groups exceed the relation budget,
        # explicitly pack them rather than claiming one folder/component.
        if _mapped_relation_count(groups, relations) > relation_budget:
            groups = [
                _Group(
                    tuple(sorted(documents, key=_document_key_sort)),
                    cluster_kind="other_cluster",
                )
            ]

        # Rank connected members only once. Splitting a fixed prefix refines
        # the partition, so neither its node nor relation count can decrease.
        # This permits O(log budget) feasibility scans rather than a hash/sort
        # of the entire residual for every newly revealed singleton. It also
        # retains the largest informative prefix instead of discarding all
        # exact documents when just one relation crosses the cap.
        ranked_members = sorted(
            (
                member
                for group in groups
                if not group.is_document
                for member in group.members
                if member in incident
            ),
            key=lambda key: (
                -documents[key].reference_count,
                -documents[key].evidence_count,
                *_document_key_sort(key),
            ),
        )

        def build(reveal_count: int) -> list[_Group] | None:
            selected = set(ranked_members[:reveal_count])
            candidate = [group for group in groups if group.is_document]
            candidate.extend(
                _Group((member,)) for member in ranked_members[:reveal_count]
            )
            for group in groups:
                if group.is_document:
                    continue
                residual = tuple(
                    member for member in group.members if member not in selected
                )
                if residual:
                    candidate.append(_Group(residual, cluster_kind=group.cluster_kind))
            if len(candidate) > document_budget:
                return None
            if _mapped_relation_count(candidate, relations) > relation_budget:
                return None
            return candidate

        capacity = min(len(ranked_members), document_budget)
        best = groups
        full = build(capacity)
        if full is not None:
            best = full
        else:
            low, high = 1, capacity - 1
            while low <= high:
                midpoint = (low + high) // 2
                candidate = build(midpoint)
                if candidate is None:
                    high = midpoint - 1
                else:
                    best = candidate
                    low = midpoint + 1
        best = WorkspaceGraphService._reveal_unlinked_documents(
            best, incident, document_budget=document_budget
        )
        return sorted(best, key=lambda group: _group_sort_key(group, documents))

    @staticmethod
    def _reveal_unlinked_documents(
        groups: list[_Group],
        incident: set[_DocumentKey],
        *,
        document_budget: int,
    ) -> list[_Group]:
        """Use spare slots for unrelated documents without changing relation count.

        Such documents have no source or target identity in the visible relation
        universe, so revealing them cannot consume the relation budget. A
        residual still reserves its own slot unless every member is revealed;
        singleton residuals are always real documents, never one-member clusters.
        """

        available_slots = document_budget - len(groups)
        revealed: list[_Group] = []
        for group in groups:
            if group.is_document:
                revealed.append(group)
                continue
            unlinked = sorted(
                (member for member in group.members if member not in incident),
                key=_document_key_sort,
            )
            if (
                len(unlinked) == len(group.members)
                and len(unlinked) - 1 <= available_slots
            ):
                # Replacing the whole cluster frees its existing slot as well.
                selected = unlinked
                available_slots -= len(selected) - 1
            else:
                selected = unlinked[: max(0, available_slots)]
                available_slots -= len(selected)
            if not selected and len(group.members) > 1:
                revealed.append(group)
                continue
            selected_set = set(selected)
            revealed.extend(_Group((member,)) for member in selected)
            residual = tuple(
                member for member in group.members if member not in selected_set
            )
            if residual:
                revealed.append(
                    _Group(
                        residual,
                        cluster_kind=group.cluster_kind if len(residual) > 1 else None,
                    )
                )
        return revealed

    def _expand_groups(
        self,
        groups: list[_Group],
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
        *,
        workspace_id: UUID,
        graph_revision: str,
        request_fingerprint: str,
        actor_fingerprint: str,
    ) -> tuple[list[_Group], str | None]:
        assert request.expansion is not None
        cluster = next(
            (
                group
                for group in groups
                if not group.is_document
                and _cluster_id(group) == request.expansion.cluster_id
            ),
            None,
        )
        if cluster is None:
            raise ReadValidationError(
                "expansion cluster is not present in the current visible revision"
            )
        offset = 0
        if request.expansion.cursor is not None:
            cursor = self._decode_cursor(request.expansion.cursor)
            expected = {
                "workspace_id": str(workspace_id),
                "graph_revision": graph_revision,
                "request_fingerprint": request_fingerprint,
                "actor_fingerprint": actor_fingerprint,
                "cluster_id": request.expansion.cluster_id,
            }
            if any(cursor.get(key) != value for key, value in expected.items()):
                raise ReadValidationError(
                    "expansion cursor does not match the current actor, "
                    "query, or revision"
                )
            offset_value = cursor.get("offset")
            if not isinstance(offset_value, int) or offset_value < 0:
                raise ReadValidationError("expansion cursor offset is invalid")
            offset = offset_value

        members = list(cluster.members)
        if offset >= len(members):
            raise ReadValidationError("expansion cursor is exhausted")
        pinned = _pinned_focus(groups, documents, request.focus_entity_id)
        candidate = self._expansion_candidate(
            groups,
            cluster,
            documents,
            relations,
            offset=offset,
            document_budget=request.budget.documents,
            relation_budget=request.budget.relations,
            pinned=pinned,
        )
        if candidate is None:
            # A dense component may require more rendered relation identities
            # than the request budget even when only one member is exact. A
            # no-progress page would create an endless cursor; terminate the
            # sequence cleanly and retain the bounded aggregate instead.
            return groups, None
        expanded, revealed_count = candidate

        next_cursor = None
        next_offset = offset + revealed_count
        if next_offset < len(members) and self._can_expand_cluster(
            groups,
            cluster,
            relations,
            offset=next_offset,
            document_budget=request.budget.documents,
            relation_budget=request.budget.relations,
            pinned=pinned,
        ):
            next_cursor = self._encode_cursor(
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
                cluster_id=request.expansion.cluster_id,
                offset=next_offset,
            )
        return expanded, next_cursor

    @staticmethod
    def _can_expand_cluster(
        groups: list[_Group],
        cluster: _Group,
        relations: list[_Relation],
        *,
        offset: int,
        document_budget: int,
        relation_budget: int,
        pinned: _DocumentKey | None,
    ) -> bool:
        """Prove one-member progress without constructing an expansion page.

        The builder protects the selected member and an exact focused document;
        every other group can ultimately merge into one residual. That quotient
        is the exact relation-count floor: further merges cannot lower it, and
        the greedy builder reaches it if earlier partitions do not fit. Cursor
        discovery therefore needs one bounded identity scan, not all candidate
        prefixes and their intermediate merge/sort/hash work.
        """

        if not 0 <= offset < len(cluster.members):
            return False
        reserved = int(
            pinned is not None
            and any(
                group.is_document and group.members[0] == pinned for group in groups
            )
        )
        if document_budget - reserved < 2:
            return False
        protected = {cluster.members[offset]: 0}
        if reserved:
            assert pinned is not None
            protected[pinned] = 1
        identities: set[tuple[int, int, str, str]] = set()
        for relation in relations:
            identities.add(
                (
                    protected.get(relation.source, 2),
                    protected.get(relation.target, 2),
                    relation.kind,
                    relation.layer,
                )
            )
            if len(identities) > relation_budget:
                return False
        return True

    @staticmethod
    def _merge_expansion_groups(
        groups: list[_Group],
        relations: list[_Relation],
        *,
        selected: set[_DocumentKey],
        pinned: _DocumentKey | None,
        relation_budget: int,
    ) -> list[_Group] | None:
        """Apply the existing smallest-group-first packing without full rescans."""

        partition = _MappedRelationPartition(groups, relations)
        if partition.count <= relation_budget:
            return groups
        by_index = dict(enumerate(groups))
        protected = selected | {pinned}
        mergeable = [
            (len(group.members), group.identity, index)
            for index, group in by_index.items()
            if not group.is_document or group.members[0] not in protected
        ]
        heapq.heapify(mergeable)
        next_index = len(groups)
        while partition.count > relation_budget:
            if len(mergeable) < 2:
                return None
            first_index = heapq.heappop(mergeable)[2]
            second_index = heapq.heappop(mergeable)[2]
            first = by_index.pop(first_index)
            second = by_index.pop(second_index)
            merged = _Group(
                tuple(
                    sorted((*first.members, *second.members), key=_document_key_sort)
                ),
                cluster_kind="other_cluster",
            )
            by_index[next_index] = merged
            partition.merge(first_index, second_index, next_index)
            heapq.heappush(
                mergeable, (len(merged.members), merged.identity, next_index)
            )
            next_index += 1
        return list(by_index.values())

    @staticmethod
    def _expansion_candidate(
        groups: list[_Group],
        cluster: _Group,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        *,
        offset: int,
        document_budget: int,
        relation_budget: int,
        pinned: _DocumentKey | None,
    ) -> tuple[list[_Group], int] | None:
        """Return the largest deterministic reveal prefix satisfying both caps.

        ``pinned`` is a deep link's target the scene reserved a slot for. It is
        never displaced into the residual aggregate or merged under the
        relation cap, so expanding a cluster cannot take the selection away.
        """

        def is_pinned(group: _Group) -> bool:
            return group.is_document and group.members[0] == pinned

        other_groups = [group for group in groups if group is not cluster]
        displaced_members: list[_DocumentKey] = []
        available_slots = document_budget - len(other_groups)
        displaceable = [group for group in other_groups if not is_pinned(group)]
        while available_slots < 2 and displaceable:
            displaced = min(
                displaceable,
                key=lambda group: (
                    max(
                        documents[key].reference_count + documents[key].evidence_count
                        for key in group.members
                    ),
                    len(group.members),
                    group.identity,
                ),
            )
            other_groups.remove(displaced)
            displaceable.remove(displaced)
            displaced_members.extend(displaced.members)
            available_slots += 1
        if available_slots < 2:
            return None

        members = list(cluster.members)
        remaining_from_offset = len(members) - offset
        if remaining_from_offset <= 0:
            return None
        if remaining_from_offset <= available_slots and not displaced_members:
            capacity = remaining_from_offset
        else:
            # Reserve one slot for the residual aggregate so every candidate
            # document remains represented in this standalone response.
            capacity = min(remaining_from_offset, available_slots - 1)
        if capacity < 1:
            return None

        def build(reveal_count: int) -> list[_Group] | None:
            window = members[offset : offset + reveal_count]
            selected = set(window)
            residual = [member for member in members if member not in selected]
            residual.extend(displaced_members)
            candidate = [
                *other_groups,
                *(_Group((member,)) for member in window),
            ]
            if residual:
                candidate.append(
                    _Group(
                        tuple(sorted(residual, key=_document_key_sort)),
                        cluster_kind=(
                            None
                            if len(residual) == 1
                            else "other_cluster"
                            if displaced_members
                            else cluster.cluster_kind
                        ),
                    )
                )
            candidate = WorkspaceGraphService._merge_expansion_groups(
                candidate,
                relations,
                selected=selected,
                pinned=pinned,
                relation_budget=relation_budget,
            )
            if candidate is None or len(candidate) > document_budget:
                return None
            return sorted(
                candidate,
                key=lambda group: _group_sort_key(group, documents),
            )

        # Splitting more prefix members out of an aggregate cannot reduce the
        # number of mapped relation identities or increase merge freedom.
        # Try full capacity once, then binary-search the largest feasible
        # prefix to avoid O(document_budget * relation_count) dense scans.
        full = build(capacity)
        if full is not None:
            return full, capacity
        smallest = build(1)
        if smallest is None:
            return None
        best_groups = smallest
        best_count = 1
        low = 2
        high = capacity - 1
        while low <= high:
            midpoint = (low + high) // 2
            midpoint_groups = build(midpoint)
            if midpoint_groups is None:
                high = midpoint - 1
            else:
                best_groups = midpoint_groups
                best_count = midpoint
                low = midpoint + 1
        return best_groups, best_count

    def _materialize_groups(
        self,
        groups: list[_Group],
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        *,
        omission_reasons: list[str],
        expansion_cursor: str | None = None,
    ) -> _RenderedScene:
        group_by_member: dict[_DocumentKey, _Group] = {
            member: group for group in groups for member in group.members
        }
        # Cluster IDs hash every member. Compute each once: a large residual
        # cluster may receive thousands of relations, and re-sorting/hashing
        # all of its members per edge turns materialization quadratic in
        # practice.
        id_by_group = {group: _group_id(group, documents) for group in groups}
        group_by_id = {group_id: group for group, group_id in id_by_group.items()}
        incident_by_group: Counter[_Group] = Counter()
        mapped: dict[tuple[str, str, str, str], list[object]] = {}
        for relation in relations:
            source_group = group_by_member[relation.source]
            target_group = group_by_member[relation.target]
            source_id = id_by_group[source_group]
            target_id = id_by_group[target_group]
            identity = (source_id, target_id, relation.kind, relation.layer)
            cross_folder = _relation_cross_folder(relation, documents)
            existing = mapped.get(identity)
            if existing is None:
                mapped[identity] = [1, cross_folder, relation.occurrence_weight]
            else:
                existing[0] = int(existing[0]) + 1
                existing[1] = bool(existing[1]) or cross_folder
                existing[2] = int(existing[2]) + relation.occurrence_weight
            incident_by_group[source_group] += 1
            if source_group is not target_group:
                incident_by_group[target_group] += 1

        nodes: list[WorkspaceGraphDocumentNode | WorkspaceGraphClusterNode] = []
        for group in sorted(
            groups, key=lambda value: _group_sort_key(value, documents)
        ):
            if group.is_document:
                document = documents[group.members[0]]
                nodes.append(
                    WorkspaceGraphDocumentNode(
                        id=document.node_id,
                        entity_kind=document.entity_kind,
                        entity_id=document.entity_id,
                        label=document.label[:200],
                        path=document.path[:1024],
                        primary_folder_id=document.folder_id,
                        primary_folder_label=document.folder_label[:200],
                        reference_count=document.reference_count,
                        evidence_count=document.evidence_count,
                        also_appears_in=[
                            WorkspaceGraphPlacementRef(
                                tree_node_id=placement.tree_node_id,
                                label=placement.folder_label[:200],
                                path=placement.path[:1024],
                            )
                            for placement in document.secondary
                        ],
                    )
                )
            else:
                nodes.append(
                    WorkspaceGraphClusterNode(
                        id=_cluster_id(group),
                        cluster_kind=group.cluster_kind,
                        label=_cluster_label(group, documents)[:200],
                        member_count=len(group.members),
                        relation_count=incident_by_group[group],
                    )
                )

        edges = [
            WorkspaceGraphEdge(
                id=_aggregate_edge_id(source, target, kind, layer),
                source=source,
                target=target,
                relation_kind=kind,
                label=_relation_label(kind),
                layer=layer,
                direction="directed",
                aggregate=(
                    count > 1
                    or not group_by_id[source].is_document
                    or not group_by_id[target].is_document
                ),
                relation_count=int(count),
                occurrence_weight=int(occurrence_weight),
                cross_folder=bool(cross_folder),
            )
            for (source, target, kind, layer), (
                count,
                cross_folder,
                occurrence_weight,
            ) in sorted(mapped.items())
        ]
        representation = (
            "aggregated" if any(not group.is_document for group in groups) else "exact"
        )
        return _RenderedScene(
            groups=groups,
            nodes=nodes,
            edges=edges,
            representation=representation,
            omission_reasons=list(dict.fromkeys(omission_reasons)),
            expansion_cursor=expansion_cursor,
        )

    @staticmethod
    def _folder_topology(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        rendered: _RenderedScene,
    ) -> WorkspaceGraphFolderTopology | None:
        """Emit lossless folder inputs after filters, grouping, and edge caps.

        Complexity is linear in represented documents, relations, and their
        primary folder ancestor closure. No secondary placement or facet-only
        folder enters this input. A row-budget overflow disables the entire
        optional topology, rather than truncating correlated distributions.
        """

        folders: dict[UUID, _VisibleFolder] = {}
        direct_counts: Counter[UUID] = Counter()
        memberships: Counter[tuple[str, UUID | None, str]] = Counter()
        node_by_member: dict[_DocumentKey, str] = {}
        for group in rendered.groups:
            node_id = _group_id(group, documents)
            for member in group.members:
                document = documents[member]
                node_by_member[member] = node_id
                memberships[(node_id, document.folder_id, document.entity_kind)] += 1
                if document.folder_id is not None:
                    direct_counts[document.folder_id] += 1
                if document.primary is not None:
                    for folder in document.primary.folder_ancestors:
                        folders[folder.id] = folder
                if (
                    len(folders) + len(memberships)
                    > WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
                ):
                    return None

        emitted_edges = {edge.id for edge in rendered.edges}
        slices: dict[tuple[str, UUID | None, str, UUID | None, str], list[int]] = {}
        for relation in relations:
            edge_id = _aggregate_edge_id(
                node_by_member[relation.source],
                node_by_member[relation.target],
                relation.kind,
                relation.layer,
            )
            if edge_id not in emitted_edges:
                # The existing irreducible kind-floor fallback may omit a
                # whole edge. Never assign its counts to another visible edge.
                continue
            source = documents[relation.source]
            target = documents[relation.target]
            identity = (
                edge_id,
                source.folder_id,
                source.entity_kind,
                target.folder_id,
                target.entity_kind,
            )
            counts = slices.setdefault(identity, [0, 0])
            counts[0] += 1
            counts[1] += relation.occurrence_weight
            if (
                len(folders) + len(memberships) + len(slices)
                > WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
            ):
                return None

        return WorkspaceGraphFolderTopology(
            folders=[
                WorkspaceGraphFolder(
                    id=folder.id,
                    label=folder.label[:200],
                    parent_folder_id=folder.parent_folder_id,
                    direct_member_count=direct_counts[folder.id],
                )
                for folder in sorted(folders.values(), key=lambda item: str(item.id))
            ],
            memberships=[
                WorkspaceGraphFolderMembership(
                    node_id=node_id,
                    folder_id=folder_id,
                    entity_kind=entity_kind,
                    member_count=count,
                )
                for (node_id, folder_id, entity_kind), count in sorted(
                    memberships.items(),
                    key=lambda item: (
                        item[0][0],
                        str(item[0][1]) if item[0][1] is not None else "",
                        item[0][2],
                    ),
                )
            ],
            edge_slices=[
                WorkspaceGraphFolderEdgeSlice(
                    edge_id=edge_id,
                    source_folder_id=source_folder_id,
                    source_entity_kind=source_entity_kind,
                    target_folder_id=target_folder_id,
                    target_entity_kind=target_entity_kind,
                    relation_count=count,
                    occurrence_weight=weight,
                )
                for (
                    edge_id,
                    source_folder_id,
                    source_entity_kind,
                    target_folder_id,
                    target_entity_kind,
                ), (count, weight) in sorted(
                    slices.items(),
                    key=lambda item: tuple(
                        str(value) if value is not None else "" for value in item[0]
                    ),
                )
            ],
        )

    @staticmethod
    def _graph_revision(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
    ) -> str:
        def placement_state(placement: _Placement | None) -> dict | None:
            if placement is None:
                return None
            return {
                "tree_node_id": str(placement.tree_node_id),
                "folder_id": (
                    str(placement.folder_id)
                    if placement.folder_id is not None
                    else None
                ),
                "folder_label": placement.folder_label,
                "path": placement.path,
                "ancestor_ids": [str(value) for value in placement.ancestor_ids],
                "folder_ancestors": [
                    {
                        "id": str(folder.id),
                        "label": folder.label,
                        "parent_folder_id": (
                            str(folder.parent_folder_id)
                            if folder.parent_folder_id is not None
                            else None
                        ),
                    }
                    for folder in placement.folder_ancestors
                ],
            }

        visible_state = {
            "algorithm": ALGORITHM_VERSION,
            "visibility_policy": VISIBILITY_POLICY_VERSION,
            "documents": [
                {
                    "kind": document.entity_kind,
                    "id": str(document.entity_id),
                    "head_id": str(document.head_id),
                    "head_updated_at": document.updated_at.isoformat(),
                    "label": document.label,
                    "path": document.path,
                    "primary_placement": placement_state(document.primary),
                    # Payload caps secondary placements at 32, but revision
                    # invalidation must include every visible placement so a
                    # metadata change outside that cap cannot reuse a cursor
                    # derived from a different actor-visible universe.
                    "secondary_placements": [
                        placement_state(value) for value in document.secondary_revision
                    ],
                }
                for document in sorted(
                    documents.values(), key=lambda value: _document_key_sort(value.key)
                )
            ],
            "relations": [
                {
                    "source": _key_token(relation.source),
                    "target": _key_token(relation.target),
                    "kind": relation.kind,
                    "occurred_at": relation.occurred_at.isoformat(),
                    "occurrence_weight": relation.occurrence_weight,
                }
                for relation in sorted(relations, key=_relation_sort_key)
            ],
        }
        digest = hashlib.sha256(_canonical_json(visible_state)).hexdigest()
        return f"wgr1:{digest}"

    @staticmethod
    def _generated_at(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
    ) -> datetime:
        values = [document.updated_at for document in documents.values()]
        values.extend(relation.occurred_at for relation in relations)
        return max(values, default=_EMPTY_GENERATED_AT)

    @staticmethod
    def _request_fingerprint(request: WorkspaceGraphOverviewRequest) -> str:
        # Expansion is transport pagination state, not part of the underlying
        # scene query. Excluding it lets a base-scene cursor validate on the
        # follow-up request while every data filter/lens/budget remains bound.
        payload = request.model_dump(mode="json", by_alias=True)
        payload.pop("expansion", None)
        # A focus regroups the scene, so it binds cursors when present. An
        # absent focus keeps the fingerprint of requests that predate it.
        if payload.get("focus_entity_id") is None:
            payload.pop("focus_entity_id", None)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    @staticmethod
    def _actor_fingerprint(actor: ActorContext) -> str:
        payload = {
            "actor_kind": actor.actor_kind,
            "actor_id": actor.actor_id,
            "user_id": str(actor.user_id) if actor.user_id is not None else None,
            "workspace_id": (
                str(actor.workspace_id) if actor.workspace_id is not None else None
            ),
            "role": actor.role or actor.workspace_role,
            "scopes": sorted(actor.scopes),
            "token_kind": actor.token_kind,
            "token_id": str(actor.token_id) if actor.token_id is not None else None,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _encode_cursor(
        self,
        *,
        workspace_id: UUID,
        graph_revision: str,
        request_fingerprint: str,
        actor_fingerprint: str,
        cluster_id: str,
        offset: int,
    ) -> str:
        payload = {
            "v": _CURSOR_VERSION,
            "workspace_id": str(workspace_id),
            "graph_revision": graph_revision,
            "request_fingerprint": request_fingerprint,
            "actor_fingerprint": actor_fingerprint,
            "cluster_id": cluster_id,
            "offset": offset,
        }
        body = _urlsafe_encode(_canonical_json(payload))
        signature = hmac.new(
            self._cursor_secret,
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{body}.{_urlsafe_encode(signature)}"

    def _decode_cursor(self, token: str) -> dict[str, object]:
        try:
            body, encoded_signature = token.split(".", 1)
            decoded_body = _urlsafe_decode(body)
            signature = _urlsafe_decode(encoded_signature)
            expected = hmac.new(
                self._cursor_secret,
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            payload = json.loads(decoded_body)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ReadValidationError(
                "expansion cursor is malformed or tampered"
            ) from exc
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ReadValidationError("expansion cursor version is unsupported")
        return payload

    @staticmethod
    def _facet_counts(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        *,
        folder_documents: dict[_DocumentKey, _Document],
        quick_filter_counts: dict[str, int],
    ) -> WorkspaceGraphFacetCounts:
        document_counts = Counter(
            document.entity_kind for document in documents.values()
        )
        relation_counts = Counter(relation.kind for relation in relations)
        folder_counts: Counter[str] = Counter()
        folder_labels: dict[str, str] = {}
        for document in folder_documents.values():
            value = (
                str(document.folder_id)
                if document.folder_id is not None
                else "unplaced"
            )
            folder_counts[value] += 1
            folder_labels[value] = document.folder_label
        return WorkspaceGraphFacetCounts(
            document_kinds=[
                WorkspaceGraphFacetValue(value=value, label=value.title(), count=count)
                for value, count in sorted(document_counts.items())
            ],
            relation_kinds=[
                WorkspaceGraphFacetValue(
                    value=value,
                    label=_relation_label(value),
                    count=count,
                )
                for value, count in sorted(relation_counts.items())
            ],
            folders=[
                WorkspaceGraphFacetValue(
                    value=value,
                    label=folder_labels[value][:200],
                    count=count,
                )
                for value, count in sorted(folder_counts.items())
            ],
            quick_filters=[
                WorkspaceGraphFacetValue(
                    value=value,
                    label=value.replace("_", " ").title(),
                    count=count,
                )
                for value, count in sorted(quick_filter_counts.items())
            ],
        )

    @staticmethod
    def _metric_basis(lens: str) -> str:
        return {
            "combined": "visible_published_links_and_citations.pre_quick.v1",
            "links": "distinct_visible_published_wikilink_sources.pre_quick.v1",
            "evidence": "visible_published_citation_relations.pre_quick.v1",
            "directory": "visible_primary_tree_placement.v1",
            "compare": "partial_union_of_authored_relations.v1",
            "growth": "unavailable_temporal_projection.v1",
            "provenance": "unavailable_gated_provenance.v1",
        }[lens]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _in_time_window(
    value: datetime,
    from_: datetime | None,
    to: datetime | None,
) -> bool:
    current = _aware(value)
    return not (
        (from_ is not None and current < from_) or (to is not None and current > to)
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    # Cursor wire format is *unpadded canonical* base64url. Python's permissive
    # urlsafe decoder accepts padding, non-URL alphabet characters, and textual
    # variants whose non-zero unused bits decode to the same bytes. Accepting
    # those variants makes a signed token textually malleable even though its
    # HMAC bytes still compare equal.
    if not value or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise ValueError("base64url value contains invalid characters")
    if len(value) % 4 == 1:
        raise ValueError("base64url value has invalid length")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except ValueError as exc:
        raise ValueError("base64url value is malformed") from exc
    if _urlsafe_encode(decoded) != value:
        raise ValueError("base64url value is not canonical")
    return decoded


def _document_key_sort(key: _DocumentKey) -> tuple[str, str]:
    return key[0], str(key[1])


def _relation_sort_key(
    relation: _Relation | tuple[_DocumentKey, _DocumentKey, str],
) -> tuple[str, str, str, str, str]:
    if isinstance(relation, _Relation):
        source, target, kind = relation.source, relation.target, relation.kind
    else:
        source, target, kind = relation
    return source[0], str(source[1]), target[0], str(target[1]), kind


def _key_token(key: _DocumentKey) -> str:
    return f"{key[0]}:{key[1]}"


def _group_member_digest(members: Iterable[_DocumentKey]) -> str:
    return hashlib.sha256(
        "|".join(
            _key_token(key) for key in sorted(members, key=_document_key_sort)
        ).encode("utf-8")
    ).hexdigest()


def _cluster_id(group: _Group) -> str:
    digest = group.identity.rsplit(":", 1)[1]
    return f"cluster:{group.cluster_kind}:{digest[:24]}"


def _focus_key(
    documents: dict[_DocumentKey, _Document],
    entity_id: UUID | None,
) -> _DocumentKey | None:
    """Resolve a deep-link entity among the documents in this scene only."""

    if entity_id is None:
        return None
    for kind in ("context", "library"):
        key = (kind, entity_id)
        if key in documents:
            return key
    return None


def _pinned_focus(
    groups: list[_Group],
    documents: dict[_DocumentKey, _Document],
    entity_id: UUID | None,
) -> _DocumentKey | None:
    """The focus document when the scene shows it on its own, else ``None``."""

    key = _focus_key(documents, entity_id)
    if key is None:
        return None
    if any(group.is_document and group.members[0] == key for group in groups):
        return key
    return None


def _focus_result(
    entity_id: UUID | None,
    groups: list[_Group],
    documents: dict[_DocumentKey, _Document],
) -> WorkspaceGraphFocus | None:
    if entity_id is None:
        return None
    key = _focus_key(documents, entity_id)
    group = (
        next((group for group in groups if key in group.members), None)
        if key is not None
        else None
    )
    if key is None or group is None:
        return WorkspaceGraphFocus(status="unavailable")
    if group.is_document:
        return WorkspaceGraphFocus(status="document", node_id=documents[key].node_id)
    return WorkspaceGraphFocus(status="cluster", node_id=_cluster_id(group))


def _group_id(
    group: _Group,
    documents: dict[_DocumentKey, _Document],
) -> str:
    return (
        documents[group.members[0]].node_id if group.is_document else _cluster_id(group)
    )


def _group_sort_key(
    group: _Group,
    documents: dict[_DocumentKey, _Document],
) -> tuple[int, str]:
    return (0 if group.is_document else 1, _group_id(group, documents))


def _cluster_label(
    group: _Group,
    documents: dict[_DocumentKey, _Document],
) -> str:
    if group.cluster_kind == "folder_cluster":
        labels = {documents[key].folder_label for key in group.members}
        if len(labels) == 1:
            return next(iter(labels))
        return f"{len(group.members)} visible documents across folders"
    if group.cluster_kind == "link_cluster":
        return f"Linked component · {len(group.members)} visible documents"
    return f"Other visible documents · {len(group.members)}"


def _mapped_relation_count(
    groups: list[_Group],
    relations: list[_Relation],
) -> int:
    group_index = {
        member: index for index, group in enumerate(groups) for member in group.members
    }
    return len(
        {
            (
                group_index[relation.source],
                group_index[relation.target],
                relation.kind,
                relation.layer,
            )
            for relation in relations
        }
    )


def _semantic_partitions(
    documents: dict[_DocumentKey, _Document],
    relations: list[_Relation],
    *,
    lens: str,
) -> list[tuple[_DocumentKey, ...]]:
    if lens == "directory":
        by_folder: dict[str, list[_DocumentKey]] = defaultdict(list)
        for key, document in documents.items():
            folder_key = (
                str(document.folder_id)
                if document.folder_id is not None
                else "unplaced"
            )
            by_folder[folder_key].append(key)
        return [
            tuple(sorted(members, key=_document_key_sort))
            for _folder, members in sorted(by_folder.items())
        ]

    adjacency: dict[_DocumentKey, set[_DocumentKey]] = {key: set() for key in documents}
    for relation in relations:
        adjacency[relation.source].add(relation.target)
        adjacency[relation.target].add(relation.source)
    unseen = set(documents)
    partitions: list[tuple[_DocumentKey, ...]] = []
    # A precomputed seed order avoids scanning all remaining isolated nodes
    # once per component in a sparse/orphan-heavy workspace.
    for seed in sorted(documents, key=_document_key_sort):
        if seed not in unseen:
            continue
        stack = [seed]
        members: set[_DocumentKey] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            unseen.discard(current)
            stack.extend(adjacency[current] - members)
        partitions.append(tuple(sorted(members, key=_document_key_sort)))
    return sorted(
        partitions, key=lambda members: (-len(members), _group_member_digest(members))
    )


def _relation_cross_folder(
    relation: _Relation,
    documents: dict[_DocumentKey, _Document],
) -> bool:
    source_folder = documents[relation.source].folder_id
    target_folder = documents[relation.target].folder_id
    return (
        source_folder is not None
        and target_folder is not None
        and source_folder != target_folder
    )


def _relation_label(kind: str) -> str:
    return {
        "wikilink": "links to",
        "citation_page": "cites",
        "citation_source": "cites source",
    }.get(kind, kind.replace("_", " "))


def _aggregate_edge_id(source: str, target: str, kind: str, layer: str) -> str:
    digest = hashlib.sha256(f"{source}|{target}|{kind}|{layer}".encode()).hexdigest()
    return f"relation:{kind}:{digest[:24]}"

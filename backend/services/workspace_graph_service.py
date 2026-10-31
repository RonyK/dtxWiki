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
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import batched
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

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
    WorkspaceGraphExpansionOutcome,
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
from backend.services.workspace_urls import context_page_url, library_item_url

ALGORITHM_VERSION = "visible-component-pack.v3"
VISIBILITY_POLICY_VERSION = "published-document-visibility.v1"
DEFAULT_PLACEMENT_LABEL = "No visible placement"
_EMPTY_GENERATED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_CURSOR_VERSION = 1
_REVISION_BATCH_SIZE = 256

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


@dataclass(frozen=True, slots=True)
class _LibraryVisibilityItem:
    workspace_id: UUID
    lifecycle_status: str
    trust_level: str
    current_ready_version_id: UUID | None
    current_version_id: UUID | None


@dataclass(frozen=True, slots=True)
class _LibraryVisibilityVersion:
    id: UUID
    processing_status: str


@dataclass(frozen=True, slots=True)
class _TreeNode:
    id: UUID
    parent_id: UUID | None
    node_kind: str
    title: str
    target_kind: str | None
    target_id: UUID | None
    primary_placement: bool


@dataclass(frozen=True, slots=True)
class _TreeIndex:
    """Request-local scalar capture, not expanded per-document presentation."""

    nodes: dict[UUID, _TreeNode]

    def ancestors(self, node_id: UUID) -> list[_TreeNode]:
        node = self.nodes[node_id]
        current = self.nodes.get(node.parent_id)
        ancestors: list[_TreeNode] = []
        seen: set[UUID] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            ancestors.append(current)
            current = self.nodes.get(current.parent_id)
        ancestors.reverse()
        return ancestors


def _visible_folders(ancestors: list[_TreeNode]) -> tuple[_VisibleFolder, ...]:
    folders = [node for node in ancestors if node.node_kind == "folder"]
    return tuple(
        _VisibleFolder(node.id, node.title, folders[index - 1].id if index else None)
        for index, node in enumerate(folders)
    )


@dataclass(frozen=True, slots=True)
class _CapturedPlacement:
    tree_node_id: UUID
    index: _TreeIndex
    folder: _TreeNode | None

    @property
    def folder_id(self) -> UUID | None:
        return self.folder.id if self.folder else None

    @property
    def folder_label(self) -> str:
        return self.folder.title if self.folder else DEFAULT_PLACEMENT_LABEL

    @property
    def ancestor_ids(self) -> tuple[UUID, ...]:
        return tuple(node.id for node in self.index.ancestors(self.tree_node_id))

    @property
    def folder_ancestors(self) -> tuple[_VisibleFolder, ...]:
        return _visible_folders(self.index.ancestors(self.tree_node_id))

    @property
    def path(self) -> str:
        return " / ".join(
            [node.title for node in self.index.ancestors(self.tree_node_id)]
            + [self.index.nodes[self.tree_node_id].title]
        )

    def values(self) -> dict:
        # Hashing needs every placement, but discards these temporary values.
        # Expanded routes are retained only in admitted singleton detail.
        ancestors = self.index.ancestors(self.tree_node_id)
        return {
            "tree_node_id": self.tree_node_id,
            "folder_id": self.folder_id,
            "folder_label": self.folder_label,
            "path": " / ".join(
                [node.title for node in ancestors]
                + [self.index.nodes[self.tree_node_id].title]
            ),
            "ancestor_ids": tuple(node.id for node in ancestors),
            "folder_ancestors": _visible_folders(ancestors),
        }


@dataclass
class _Document:
    key: _DocumentKey
    entity_id: UUID
    entity_kind: str
    label: str
    path: str
    head_id: UUID
    updated_at: datetime
    selected_conversion_attempt_id: UUID | None = None
    primary: _Placement | _CapturedPlacement | None = None
    secondary: list[_Placement | _CapturedPlacement] = field(default_factory=list)
    secondary_revision: tuple[_Placement | _CapturedPlacement, ...] = ()
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


class _RevealProbe:
    """Decide whether revealing a ranked prefix fits both budgets.

    Revealing the first ``reveal_count`` ranked members splits them out of
    their groups. The result fits when it has at most ``document_budget``
    groups and its relations map to at most ``relation_budget`` distinct
    (source, target, kind, layer) identities. Both follow from one integer
    label per endpoint (its rank once revealed, else its group), so a probe is
    a single pass over labels prepared once. It neither rebuilds and rehashes
    the residual groups nor reads the relation objects again.
    """

    def __init__(
        self,
        groups: list[_Group],
        ranked_members: list[_DocumentKey],
        relations: list[_Relation],
        *,
        document_budget: int,
        relation_budget: int,
    ) -> None:
        self._document_budget = document_budget
        self._relation_budget = relation_budget
        self._relations = relations
        self._rank = {member: rank for rank, member in enumerate(ranked_members)}
        # No probe reveals this many members, so it is the rank of a member
        # that stays in its group.
        self._never = len(ranked_members)
        self._span = self._never + len(groups)
        self._group_of: dict[_DocumentKey, int] = {}
        self._exact = 0
        emptied_at: list[int] = []
        for index, group in enumerate(groups):
            for member in group.members:
                self._group_of[member] = self._never + index
            if group.is_document:
                self._exact += 1
                continue
            ranks = [self._rank.get(member, self._never) for member in group.members]
            # A residual group survives until its last member is revealed.
            emptied_at.append(max(ranks, default=-1) + 1)
        self._emptied_at = sorted(emptied_at)
        self._labelled: list[tuple[int, int, int, int, int]] | None = None
        self._kinds = 1

    def fits(self, reveal_count: int) -> bool:
        residuals = len(self._emptied_at) - bisect_right(self._emptied_at, reveal_count)
        if self._exact + reveal_count + residuals > self._document_budget:
            return False
        if self._labelled is None:
            self._labelled = self._label_relations()
        span, kinds = self._span, self._kinds
        identities = {
            (
                (src if src < reveal_count else src_group) * span
                + (dst if dst < reveal_count else dst_group)
            )
            * kinds
            + kind
            for src, src_group, dst, dst_group, kind in self._labelled
        }
        return len(identities) <= self._relation_budget

    def _label_relations(self) -> list[tuple[int, int, int, int, int]]:
        # (source rank, source group, target rank, target group, kind/layer).
        kinds: dict[tuple[str, str], int] = {}
        rank, group_of, never = self._rank, self._group_of, self._never
        labelled = [
            (
                rank.get(relation.source, never),
                group_of[relation.source],
                rank.get(relation.target, never),
                group_of[relation.target],
                kinds.setdefault((relation.kind, relation.layer), len(kinds)),
            )
            for relation in self._relations
        ]
        self._kinds = max(1, len(kinds))
        return labelled


@dataclass
class _RenderedScene:
    groups: list[_Group]
    nodes: list[WorkspaceGraphDocumentNode | WorkspaceGraphClusterNode]
    edges: list[WorkspaceGraphEdge]
    representation: str
    omission_reasons: list[str]
    expansion_cursor: str | None = None
    expansions: list[WorkspaceGraphExpansionOutcome] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ReplayStep:
    """One requested expansion; the active one's offset comes from its cursor."""

    cluster_id: str
    pinned: bool
    offset: int
    active: bool


@dataclass(frozen=True, slots=True)
class _ReplayResult:
    """What one step did: no cluster means missing, no window means no room."""

    step: _ReplayStep
    cluster: _Group | None = None
    revealed: frozenset[_DocumentKey] | None = None
    folded_count: int = 0


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
            expansions=rendered.expansions,
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
        )
        for page_id, title, head_id, head_created_at in page_heads:
            key = ("context", page_id)
            documents[key] = _Document(
                key=key,
                entity_id=page_id,
                entity_kind="context",
                label=title,
                path=context_page_url(workspace_id, page_id),
                head_id=head_id,
                updated_at=_aware(head_created_at),
            )

        # The canonical Library head is the ready pointer; only an item that
        # has never been projected falls back to its legacy current pointer
        # (mirrors get_canonical_library_version, which library_item_visible
        # below then re-checks per actor).
        item_heads = session.execute(
            select(
                LibraryItem.id,
                LibraryItem.workspace_id,
                LibraryItem.lifecycle_status,
                LibraryItem.trust_level,
                LibraryItem.current_ready_version_id,
                LibraryItem.current_version_id,
                LibraryItem.title,
                LibraryItem.uri,
                LibraryItemVersion.id,
                LibraryItemVersion.processing_status,
                LibraryItemVersion.updated_at,
                LibraryItemVersion.selected_conversion_attempt_id,
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
        )
        for (
            item_id,
            item_workspace_id,
            lifecycle_status,
            trust_level,
            ready_version_id,
            current_version_id,
            title,
            uri,
            head_id,
            processing_status,
            updated_at,
            selected_conversion_attempt_id,
        ) in item_heads:
            if not library_item_visible(
                _LibraryVisibilityItem(
                    item_workspace_id,
                    lifecycle_status,
                    trust_level,
                    ready_version_id,
                    current_version_id,
                ),
                actor,
                workspace_id=workspace_id,
                version=_LibraryVisibilityVersion(head_id, processing_status),
            ):
                continue
            key = ("library", item_id)
            documents[key] = _Document(
                key=key,
                entity_id=item_id,
                entity_kind="library",
                label=title or uri,
                path=library_item_url(workspace_id, item_id),
                head_id=head_id,
                updated_at=_aware(updated_at),
                selected_conversion_attempt_id=selected_conversion_attempt_id,
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
                WorkspaceTreeNode.id,
                WorkspaceTreeNode.parent_id,
                WorkspaceTreeNode.node_kind,
                WorkspaceTreeNode.title,
                WorkspaceTreeNode.target_kind,
                WorkspaceTreeNode.target_id,
                WorkspaceTreeNode.primary_placement,
                LibraryArtifact.library_item_version_id.label(
                    "artifact_library_item_version_id"
                ),
                LibraryArtifact.attempt_id.label("artifact_attempt_id"),
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
        )
        # A retained artifact from another conversion attempt is not a visible
        # placement, even when its source version remains the ready head.
        item_by_artifact_head = {
            (
                document.head_id,
                document.selected_conversion_attempt_id,
            ): document.entity_id
            for document in documents.values()
            if document.entity_kind == "library"
            and document.selected_conversion_attempt_id is not None
        }
        nodes_by_id: dict[UUID, _TreeNode] = {}
        artifact_items: dict[UUID, UUID] = {}
        for *node_values, artifact_version_id, artifact_attempt_id in node_rows:
            node = _TreeNode(*node_values)
            nodes_by_id[node.id] = node
            if (
                node.target_kind == "library_artifact"
                and node.target_id is not None
                and (artifact_version_id, artifact_attempt_id) in item_by_artifact_head
            ):
                artifact_items[node.target_id] = item_by_artifact_head[
                    (artifact_version_id, artifact_attempt_id)
                ]
        index = _TreeIndex(nodes_by_id)

        initially_visible: set[UUID] = set()
        for node in nodes_by_id.values():
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

        visible_ids = {
            node.id for node in nodes_by_id.values() if chain_visible(node.id)
        }

        placements: dict[_DocumentKey, list[tuple[int, _CapturedPlacement]]] = (
            defaultdict(list)
        )
        for node in nodes_by_id.values():
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
            folder = next(
                (
                    ancestor
                    for ancestor in reversed(index.ancestors(node.id))
                    if ancestor.node_kind == "folder"
                ),
                None,
            )
            placement = _CapturedPlacement(node.id, index, folder)
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
            if apply_folder_filters and (only or include or exclude):
                placement_ids: set[WorkspaceGraphFolderKey] = (
                    set(document.primary.ancestor_ids) | {document.primary.tree_node_id}
                    if document.primary is not None
                    else set()
                )
                # Match the emitted facet, including root/unplaced documents.
                # Invisible ancestors never enter the membership set.
                if document.folder_id is None:
                    placement_ids.add("unplaced")
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
        library_by_artifact_head = {
            (document.head_id, document.selected_conversion_attempt_id): document.key
            for document in documents.values()
            if document.entity_kind == "library"
            and document.selected_conversion_attempt_id is not None
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
            cited_library_artifact.attempt_id.label("artifact_attempt_id"),
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
                    target = library_by_artifact_head.get(
                        (
                            citation["artifact_library_item_version_id"],
                            citation["artifact_attempt_id"],
                        )
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
        if request.lens == "directory":
            return self._render_directory(
                documents,
                relations,
                request,
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
            )
        if (
            len(documents) <= request.budget.documents
            and len(relations) <= request.budget.relations
            and request.expansion is None
            and not request.retained_expansions
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
        rendered = self._replay_and_materialize(
            groups,
            documents,
            relations,
            request,
            omission_reasons=omission_reasons,
            grouped=False,
            workspace_id=workspace_id,
            graph_revision=graph_revision,
            request_fingerprint=request_fingerprint,
            actor_fingerprint=actor_fingerprint,
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
        return rendered

    def _replay_and_materialize(
        self,
        groups: list[_Group],
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
        *,
        omission_reasons: list[str],
        grouped: bool,
        workspace_id: UUID,
        graph_revision: str,
        request_fingerprint: str,
        actor_fingerprint: str,
    ) -> _RenderedScene:
        """Replay the requested expansions, then offer the next cluster to open."""

        expansion_cursor = None
        outcomes: list[WorkspaceGraphExpansionOutcome] = []
        if request.expansion is not None or request.retained_expansions:
            groups, expansion_cursor, outcomes = self._expand_groups(
                groups,
                documents,
                relations,
                request,
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
            )
        # Within budget only a replay leaves budget clusters; one that applied
        # nothing (a missing step, or data that shrank) yields an exact scene.
        if (
            not grouped
            and not omission_reasons
            and any(not group.is_document for group in groups)
        ):
            omission_reasons = ["document_budget"]
        rendered = self._materialize_groups(
            groups,
            documents,
            relations,
            omission_reasons=omission_reasons,
            expansion_cursor=expansion_cursor,
            grouped=grouped,
        )
        rendered.expansions = outcomes
        if rendered.expansion_cursor is None and request.expansion is None:
            rendered.expansion_cursor = self._first_expansion_cursor(
                groups,
                documents,
                relations,
                request,
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
            )
        return rendered

    def _first_expansion_cursor(
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
    ) -> str | None:
        """Offer the first cluster that can reveal at least one member."""

        protected_keys = _protected_singletons(
            _singleton_keys(groups), _focus_key(documents, request.focus_entity_id), []
        )
        first_cluster = next(
            (
                cluster
                for cluster in groups
                if not cluster.is_document
                and self._can_expand_cluster(
                    groups,
                    cluster,
                    relations,
                    offset=0,
                    document_budget=request.budget.documents,
                    relation_budget=request.budget.relations,
                    protected_keys=protected_keys,
                )
            ),
            None,
        )
        if first_cluster is None:
            return None
        return self._encode_cursor(
            workspace_id=workspace_id,
            graph_revision=graph_revision,
            request_fingerprint=request_fingerprint,
            actor_fingerprint=actor_fingerprint,
            cluster_id=_cluster_id(first_cluster),
            offset=0,
        )

    def _render_directory(
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
        """Group documents by primary folder whatever the budget.

        Directory exists to show the folder structure, so grouping is the
        intended representation rather than a budget fallback: every document
        stays represented and nothing is omitted. Folder groups open through the
        ordinary cluster expansion.
        """

        groups = self._directory_groups(
            documents,
            document_budget=request.budget.documents,
            focus_key=_focus_key(documents, request.focus_entity_id),
        )
        return self._replay_and_materialize(
            groups,
            documents,
            relations,
            request,
            omission_reasons=[],
            grouped=True,
            workspace_id=workspace_id,
            graph_revision=graph_revision,
            request_fingerprint=request_fingerprint,
            actor_fingerprint=actor_fingerprint,
        )

    @staticmethod
    def _directory_groups(
        documents: dict[_DocumentKey, _Document],
        *,
        document_budget: int,
        focus_key: _DocumentKey | None,
    ) -> list[_Group]:
        # A deep link's target keeps its own slot, as in the relation lenses.
        reserved = focus_key if document_budget >= 2 else None
        grouped = documents
        if reserved is not None:
            grouped = {
                key: value for key, value in documents.items() if key != reserved
            }
            document_budget -= 1
        groups = [
            _Group(
                tuple(partition),
                cluster_kind=None if len(partition) == 1 else "folder_cluster",
            )
            for partition in _semantic_partitions(grouped, [], lens="directory")
        ]
        if len(groups) > document_budget:
            # Keep the largest folders and represent the rest in one honest
            # residual instead of dropping folders from the scene.
            ranked = sorted(
                groups, key=lambda group: (-len(group.members), group.identity)
            )
            kept = ranked[: max(document_budget - 1, 0)]
            rest = tuple(
                sorted(
                    (
                        member
                        for group in ranked[len(kept) :]
                        for member in group.members
                    ),
                    key=_document_key_sort,
                )
            )
            groups = [
                *kept,
                _Group(rest, cluster_kind=None if len(rest) == 1 else "other_cluster"),
            ]
        if reserved is not None:
            groups.append(_Group((reserved,)))
        return sorted(groups, key=lambda group: _group_sort_key(group, documents))

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
        # One honest group per connected authored component. Directory groups
        # by folder in _directory_groups instead. Singleton partitions remain
        # exact docs.
        groups = [
            _Group(
                tuple(partition),
                cluster_kind=None if len(partition) == 1 else "link_cluster",
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

        def build(reveal_count: int) -> list[_Group]:
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
            return candidate

        # A scan only counts: just the winning prefix is ever built as groups.
        probe = _RevealProbe(
            groups,
            ranked_members,
            relations,
            document_budget=document_budget,
            relation_budget=relation_budget,
        )
        capacity = min(len(ranked_members), document_budget)
        revealed: int | None = None
        if probe.fits(capacity):
            revealed = capacity
        else:
            low, high = 1, capacity - 1
            while low <= high:
                midpoint = (low + high) // 2
                if probe.fits(midpoint):
                    revealed = midpoint
                    low = midpoint + 1
                else:
                    high = midpoint - 1
        best = groups if revealed is None else build(revealed)
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
    ) -> tuple[list[_Group], str | None, list[WorkspaceGraphExpansionOutcome]]:
        """Replay retained expansions in order, then the active one.

        Each step reveals what fits and lets everything unprotected merge,
        so an unpinned earlier step folds before anything pinned. Pinned steps
        and the focus document stay protected; the oldest pinned step is
        released only when that is what makes room for a later step. The
        replay is deterministic for one visible revision and request, which is
        what lets a step name a cluster that only an earlier step created.
        """

        steps = [
            _ReplayStep(step.cluster_id, step.pinned, step.offset, active=False)
            for step in request.retained_expansions
        ]
        if request.expansion is not None:
            # A cursor must match this actor, query and revision even when its
            # cluster is gone, so validate it before replaying anything.
            offset = self._expansion_offset(
                request,
                workspace_id=workspace_id,
                graph_revision=graph_revision,
                request_fingerprint=request_fingerprint,
                actor_fingerprint=actor_fingerprint,
            )
            expansion = request.expansion
            steps.append(
                _ReplayStep(expansion.cluster_id, expansion.pinned, offset, active=True)
            )
        focus_key = _focus_key(documents, request.focus_entity_id)
        pinned_windows: list[frozenset[_DocumentKey]] = []
        results: list[_ReplayResult] = []
        next_cursor = None
        for step in steps:
            cluster = next(
                (
                    group
                    for group in groups
                    if not group.is_document and _cluster_id(group) == step.cluster_id
                ),
                None,
            )
            if cluster is not None and step.offset >= len(cluster.members):
                if step.active:
                    raise ReadValidationError("expansion cursor is exhausted")
                cluster = None
            if cluster is None:
                # Changed pins or a removed earlier step can regroup what this
                # step named. Report it with the scene the request now yields.
                results.append(_ReplayResult(step))
                continue
            candidate, pinned_windows = self._reveal_step(
                groups,
                cluster,
                documents,
                relations,
                request,
                offset=step.offset,
                focus_key=focus_key,
                pinned_windows=pinned_windows,
            )
            if candidate is None:
                # A dense component may require more rendered relation
                # identities than the request budget even when only one member
                # is exact. A no-progress page would create an endless cursor;
                # retain the bounded aggregate instead.
                results.append(_ReplayResult(step, cluster))
                continue
            start_groups = groups
            groups, revealed_count = candidate
            revealed = frozenset(
                cluster.members[step.offset : step.offset + revealed_count]
            )
            results.append(
                _ReplayResult(
                    step,
                    cluster,
                    revealed,
                    len(_singleton_keys(start_groups) - _singleton_keys(groups)),
                )
            )
            if step.pinned:
                pinned_windows = [*pinned_windows, revealed]
            next_offset = step.offset + revealed_count
            # The next page replays this step from the same starting groups,
            # and its release loop can free every pinned window, so only the
            # focus binds it.
            if (
                step.active
                and next_offset < len(cluster.members)
                and self._can_expand_cluster(
                    start_groups,
                    cluster,
                    relations,
                    offset=next_offset,
                    document_budget=request.budget.documents,
                    relation_budget=request.budget.relations,
                    protected_keys=_protected_singletons(
                        _singleton_keys(start_groups), focus_key, []
                    ),
                )
            ):
                next_cursor = self._encode_cursor(
                    workspace_id=workspace_id,
                    graph_revision=graph_revision,
                    request_fingerprint=request_fingerprint,
                    actor_fingerprint=actor_fingerprint,
                    cluster_id=step.cluster_id,
                    offset=next_offset,
                )

        # Later steps may fold what an earlier one revealed; report what is
        # still on its own in the final scene.
        singles = _singleton_keys(groups)
        return (
            groups,
            next_cursor,
            [_expansion_outcome(result, documents, singles) for result in results],
        )

    def _reveal_step(
        self,
        groups: list[_Group],
        cluster: _Group,
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
        request: WorkspaceGraphOverviewRequest,
        *,
        offset: int,
        focus_key: _DocumentKey | None,
        pinned_windows: list[frozenset[_DocumentKey]],
    ) -> tuple[tuple[list[_Group], int] | None, list[frozenset[_DocumentKey]]]:
        """Reveal one page, releasing the oldest pinned windows only if that helps.

        Returns the candidate and the pinned windows still protected. When no
        release makes room, every pin is kept for the later steps.
        """

        singles = _singleton_keys(groups)
        windows = list(pinned_windows)
        while True:
            candidate = self._expansion_candidate(
                groups,
                cluster,
                documents,
                relations,
                offset=offset,
                document_budget=request.budget.documents,
                relation_budget=request.budget.relations,
                protected_keys=_protected_singletons(singles, focus_key, windows),
            )
            if candidate is not None:
                return candidate, windows
            if not windows:
                return None, pinned_windows
            windows.pop(0)

    def _expansion_offset(
        self,
        request: WorkspaceGraphOverviewRequest,
        *,
        workspace_id: UUID,
        graph_revision: str,
        request_fingerprint: str,
        actor_fingerprint: str,
    ) -> int:
        assert request.expansion is not None
        if request.expansion.cursor is None:
            return 0
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
                "expansion cursor does not match the current actor, query, or revision"
            )
        offset_value = cursor.get("offset")
        if not isinstance(offset_value, int) or offset_value < 0:
            raise ReadValidationError("expansion cursor offset is invalid")
        return offset_value

    @staticmethod
    def _can_expand_cluster(
        groups: list[_Group],
        cluster: _Group,
        relations: list[_Relation],
        *,
        offset: int,
        document_budget: int,
        relation_budget: int,
        protected_keys: frozenset[_DocumentKey],
    ) -> bool:
        """Prove one-member progress without constructing an expansion page.

        The builder protects the selected member and the protected singletons
        (the focus and pinned windows exact when the step starts); every other
        group can ultimately merge into one residual. That quotient
        is the exact relation-count floor: further merges cannot lower it, and
        the greedy builder reaches it if earlier partitions do not fit. Cursor
        discovery therefore needs one bounded identity scan, not all candidate
        prefixes and their intermediate merge/sort/hash work.
        """

        if not 0 <= offset < len(cluster.members):
            return False
        reserved = sorted(
            (
                group.members[0]
                for group in groups
                if group.is_document and group.members[0] in protected_keys
            ),
            key=_document_key_sort,
        )
        if document_budget - len(reserved) < 2:
            return False
        protected = {cluster.members[offset]: 0}
        for index, key in enumerate(reserved, start=1):
            protected.setdefault(key, index)
        identities: set[tuple[int, int, str, str]] = set()
        for relation in relations:
            identities.add(
                (
                    protected.get(relation.source, -1),
                    protected.get(relation.target, -1),
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
        protected_keys: frozenset[_DocumentKey],
        relation_budget: int,
    ) -> list[_Group] | None:
        """Apply the existing smallest-group-first packing without full rescans."""

        partition = _MappedRelationPartition(groups, relations)
        if partition.count <= relation_budget:
            return groups
        by_index = dict(enumerate(groups))
        protected = selected | protected_keys
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
        protected_keys: frozenset[_DocumentKey],
    ) -> tuple[list[_Group], int] | None:
        """Return the largest deterministic reveal prefix satisfying both caps.

        ``protected_keys`` holds a deep link's target and the members of pinned
        expansions that are exact documents. They are never displaced into the
        residual aggregate or merged under the relation cap, so expanding a
        cluster cannot take the selection or a pinned group away.
        """

        def is_protected(group: _Group) -> bool:
            return group.is_document and group.members[0] in protected_keys

        other_groups = [group for group in groups if group is not cluster]
        displaced_members: list[_DocumentKey] = []
        available_slots = document_budget - len(other_groups)
        displaceable = [group for group in other_groups if not is_protected(group)]
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
                protected_keys=protected_keys,
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
        grouped: bool = False,
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
                nodes.append(self._materialize_document_detail(document))
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
        has_clusters = any(not group.is_document for group in groups)
        representation = (
            "exact" if not has_clusters else "grouped" if grouped else "aggregated"
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
    def _materialize_document_detail(document: _Document) -> WorkspaceGraphDocumentNode:
        """Expand presentation only for final, admitted singleton documents."""

        return WorkspaceGraphDocumentNode(
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
        # Rows sort by the text of a folder id (unplaced first); render it once
        # per folder instead of once per row.
        folder_text: dict[UUID | None, str] = {None: ""}
        # What a relation needs from each endpoint, found with one lookup.
        endpoint: dict[_DocumentKey, tuple[str, UUID | None, str]] = {}
        for group in rendered.groups:
            node_id = _group_id(group, documents)
            for member in group.members:
                document = documents[member]
                folder_id = document.folder_id
                membership = (node_id, folder_id, document.entity_kind)
                endpoint[member] = membership
                memberships[membership] += 1
                if folder_id is not None:
                    direct_counts[folder_id] += 1
                    if folder_id not in folder_text:
                        folder_text[folder_id] = str(folder_id)
                if document.primary is not None:
                    for folder in document.primary.folder_ancestors:
                        folders[folder.id] = folder
                if (
                    len(folders) + len(memberships)
                    > WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT
                ):
                    return None

        emitted_edges = {edge.id for edge in rendered.edges}
        edge_ids: dict[tuple[str, str, str, str], str] = {}
        slices: dict[tuple[str, UUID | None, str, UUID | None, str], list[int]] = {}
        for relation in relations:
            source_node, source_folder, source_kind = endpoint[relation.source]
            target_node, target_folder, target_kind = endpoint[relation.target]
            # Relations that fold into one rendered edge share its id.
            edge_key = (source_node, target_node, relation.kind, relation.layer)
            edge_id = edge_ids.get(edge_key)
            if edge_id is None:
                edge_id = edge_ids[edge_key] = _aggregate_edge_id(*edge_key)
            if edge_id not in emitted_edges:
                # The existing irreducible kind-floor fallback may omit a
                # whole edge. Never assign its counts to another visible edge.
                continue
            identity = (
                edge_id,
                source_folder,
                source_kind,
                target_folder,
                target_kind,
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
                        folder_text[item[0][1]],
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
                    key=lambda item: (
                        item[0][0],
                        folder_text[item[0][1]],
                        item[0][2],
                        folder_text[item[0][3]],
                        item[0][4],
                    ),
                )
            ],
        )

    @staticmethod
    def _graph_revision(
        documents: dict[_DocumentKey, _Document],
        relations: list[_Relation],
    ) -> str:
        # Each document can occur in thousands of relations. Keep canonical
        # identity strings local to this captured universe instead of formatting
        # the same UUID again for every endpoint and relation sort key. This is
        # not a scene cache: every query still hashes all visible source state.
        document_ids = {key: str(key[1]) for key in documents}
        document_tokens = {
            key: f"{key[0]}:{entity_id}" for key, entity_id in document_ids.items()
        }

        def relation_order(relation: _Relation) -> tuple[str, str, str, str, str]:
            return (
                relation.source[0],
                document_ids[relation.source],
                relation.target[0],
                document_ids[relation.target],
                relation.kind,
            )

        def placement_state(
            placement: _Placement | _CapturedPlacement | None,
        ) -> dict | None:
            if placement is None:
                return None
            values = (
                placement.values()
                if isinstance(placement, _CapturedPlacement)
                else vars(placement)
            )
            return {
                "tree_node_id": str(values["tree_node_id"]),
                "folder_id": (
                    str(values["folder_id"])
                    if values["folder_id"] is not None
                    else None
                ),
                "folder_label": values["folder_label"],
                "path": values["path"],
                "ancestor_ids": [str(value) for value in values["ancestor_ids"]],
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
                    for folder in values["folder_ancestors"]
                ],
            }

        # Preserve the exact sorted canonical JSON byte stream, but don't
        # duplicate the whole universe as dictionaries and one giant UTF-8
        # buffer. Fixed-size batches retain the C encoder's throughput without
        # one encoder call per relation. This bounds digest scratch space,
        # not the complete visible scan or the size of an individual record.
        digest = hashlib.sha256()

        def update_array(records: Iterable[dict]) -> None:
            digest.update(b"[")
            for index, batch in enumerate(batched(records, _REVISION_BATCH_SIZE)):
                if index:
                    digest.update(b",")
                # The batch's brackets are replaced by the outer array's.
                digest.update(_canonical_json(batch)[1:-1])
            digest.update(b"]")

        digest.update(b'{"algorithm":')
        digest.update(_canonical_json(ALGORITHM_VERSION))
        digest.update(b',"documents":')
        update_array(
            {
                "kind": document.entity_kind,
                "id": document_ids[document.key],
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
                documents.values(),
                key=lambda value: (value.key[0], document_ids[value.key]),
            )
        )
        digest.update(b',"relations":')
        update_array(
            {
                "source": document_tokens[relation.source],
                "target": document_tokens[relation.target],
                "kind": relation.kind,
                "occurred_at": relation.occurred_at.isoformat(),
                "occurrence_weight": relation.occurrence_weight,
            }
            for relation in sorted(relations, key=relation_order)
        )
        digest.update(b',"visibility_policy":')
        digest.update(_canonical_json(VISIBILITY_POLICY_VERSION))
        digest.update(b"}")
        return f"wgr1:{digest.hexdigest()}"

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
        # Retained steps bind a cursor by cluster and page. Pins may change
        # between pages; if that regroups the paged cluster, the replay reports
        # it missing. No steps keep the fingerprint of requests that predate
        # them.
        retained = payload.pop("retained_expansions", None)
        if retained:
            payload["retained_expansions"] = [
                {"cluster_id": step["cluster_id"], "offset": step["offset"]}
                for step in retained
            ]
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


def _singleton_keys(groups: list[_Group]) -> frozenset[_DocumentKey]:
    return frozenset(group.members[0] for group in groups if group.is_document)


def _protected_singletons(
    singles: frozenset[_DocumentKey],
    focus_key: _DocumentKey | None,
    pinned_windows: list[frozenset[_DocumentKey]],
) -> frozenset[_DocumentKey]:
    """The focus and pinned documents a step must not fold.

    Only documents exact when the step starts count: that is all the
    one-member feasibility floor can reserve, so a residual that becomes a lone
    document during the step stays mergeable.
    """

    keys = frozenset[_DocumentKey]().union(*pinned_windows)
    if focus_key is not None:
        keys |= {focus_key}
    return keys & singles


def _expansion_outcome(
    result: _ReplayResult,
    documents: dict[_DocumentKey, _Document],
    singles: frozenset[_DocumentKey],
) -> WorkspaceGraphExpansionOutcome:
    step = result.step
    if result.cluster is None:
        # A missing step names nothing visible; its label could leak.
        return WorkspaceGraphExpansionOutcome(
            cluster_id=step.cluster_id, pinned=step.pinned, status="missing"
        )
    found = {
        "cluster_id": step.cluster_id,
        "label": _cluster_label(result.cluster, documents)[:200],
        "member_count": len(result.cluster.members),
        "offset": step.offset,
        "pinned": step.pinned,
    }
    if result.revealed is None:
        return WorkspaceGraphExpansionOutcome(**found, status="no_capacity")
    visible = len(result.revealed & singles)
    return WorkspaceGraphExpansionOutcome(
        **found,
        revealed_count=len(result.revealed),
        visible_count=visible,
        folded_count=result.folded_count,
        status=(
            "applied"
            if visible == len(result.revealed)
            else "partial"
            if visible
            else "folded"
        ),
    )


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

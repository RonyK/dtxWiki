"""Operator diagnostics for current Context relationship supply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from statistics import median
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.repositories.graph_density_repo import (
    GraphDensitySnapshotInputs,
    load_graph_density_snapshot_inputs,
    load_graph_density_workspace_row,
)

RELATION_DENSITY_REPORT_VERSION = "graph-degree-report.v1"
RELATION_DENSITY_VISIBILITY_SCOPE = "operator_full"
RELATION_DENSITY_PERCENTILE_METHOD = "nearest_rank"
RELATION_SUPPLY_MEDIAN_DEGREE_GATE = 2


class GraphDensityWorkspaceNotFoundError(LookupError):
    """The selected workspace disappeared before its report could be read."""


@dataclass(frozen=True, slots=True)
class RelationDensityReport:
    report_version: str
    workspace_id: UUID
    workspace_slug: str
    visibility_scope: str
    percentile_method: str
    context_pages: int
    degree_0_pages: int
    degree_1_2_pages: int
    degree_3_plus_pages: int
    pages_with_links: int
    orphan_ratio: float | None
    p25_degree: float | None
    median_degree: float | None
    p75_degree: float | None
    wikilink_edges: int
    citation_page_edges: int
    library_items: int
    library_items_cited: int
    library_items_cited_ratio: float | None
    citation_source_edges: int
    citation_source_records: int
    restricted_library_items: int
    restricted_library_items_cited: int
    restricted_citation_source_edges: int
    restricted_citation_source_records: int
    gate_threshold: int
    gate_passed: bool
    guidance_code: str
    measured_at: datetime
    edge_projection_version_before: int
    edge_projection_version_after: int | None
    edge_projection_version_stable: bool

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON shape owned by the operator CLI."""

        return {
            "report_version": self.report_version,
            "workspace_id": str(self.workspace_id),
            "workspace_slug": self.workspace_slug,
            "visibility_scope": self.visibility_scope,
            "percentile_method": self.percentile_method,
            "context_pages": self.context_pages,
            "degree_0_pages": self.degree_0_pages,
            "degree_1_2_pages": self.degree_1_2_pages,
            "degree_3_plus_pages": self.degree_3_plus_pages,
            "pages_with_links": self.pages_with_links,
            "orphan_ratio": self.orphan_ratio,
            "p25_degree": self.p25_degree,
            "median_degree": self.median_degree,
            "p75_degree": self.p75_degree,
            "wikilink_edges": self.wikilink_edges,
            "citation_page_edges": self.citation_page_edges,
            "library_items": self.library_items,
            "library_items_cited": self.library_items_cited,
            "library_items_cited_ratio": self.library_items_cited_ratio,
            "citation_source_edges": self.citation_source_edges,
            "citation_source_records": self.citation_source_records,
            "restricted_library_items": self.restricted_library_items,
            "restricted_library_items_cited": self.restricted_library_items_cited,
            "restricted_citation_source_edges": (
                self.restricted_citation_source_edges
            ),
            "restricted_citation_source_records": (
                self.restricted_citation_source_records
            ),
            "gate_threshold": self.gate_threshold,
            "gate_passed": self.gate_passed,
            "guidance_code": self.guidance_code,
            "measured_at": self.measured_at.isoformat().replace("+00:00", "Z"),
            "edge_projection_version_before": (
                self.edge_projection_version_before
            ),
            "edge_projection_version_after": self.edge_projection_version_after,
            "edge_projection_version_stable": (
                self.edge_projection_version_stable
            ),
        }


def relation_density_report(
    session: Session,
    workspace_id: UUID,
) -> RelationDensityReport:
    """Measure current authoritative relations in one operator-visible workspace.

    PageLink and ContextCitation are the source of truth. The mutable edge
    projection contributes only a coarse before/after change marker; callers
    must not interpret that revision as a complete snapshot token for lifecycle
    or trust-policy changes.
    """

    snapshot = load_graph_density_snapshot_inputs(session, workspace_id)
    if snapshot is None:
        raise GraphDensityWorkspaceNotFoundError(str(workspace_id))
    version_after_row = load_graph_density_workspace_row(session, workspace_id)
    version_after = (
        version_after_row.edge_projection_version
        if version_after_row is not None
        else None
    )
    return _build_report(
        snapshot,
        measured_at=utc_now(),
        edge_projection_version_after=version_after,
    )


def _build_report(
    snapshot: GraphDensitySnapshotInputs,
    *,
    measured_at: datetime,
    edge_projection_version_after: int | None,
) -> RelationDensityReport:
    workspace = snapshot.workspace
    context_ids = {row.page_id for row in snapshot.context_heads}
    neighbors = {page_id: set() for page_id in context_ids}

    wikilink_edges: set[tuple[UUID, UUID]] = set()
    for row in snapshot.page_links:
        source_id = row.source_page_id
        target_id = row.target_page_id
        if (
            source_id not in context_ids
            or target_id not in context_ids
            or source_id == target_id
        ):
            continue
        wikilink_edges.add((source_id, target_id))
        neighbors[source_id].add(target_id)
        neighbors[target_id].add(source_id)

    citation_page_edges: set[tuple[UUID, UUID]] = set()
    visible_library = {row.item_id: row for row in snapshot.library_heads}
    citation_source_edges: set[tuple[UUID, UUID]] = set()
    citation_source_records: set[UUID] = set()
    restricted_citation_source_edges: set[tuple[UUID, UUID]] = set()
    restricted_citation_source_records: set[UUID] = set()
    for row in snapshot.citations:
        source_id = row.source_page_id
        if source_id not in context_ids:
            continue
        if (
            row.cited_context_page_id in context_ids
            and row.cited_context_page_id != source_id
        ):
            target_id = row.cited_context_page_id
            assert target_id is not None
            citation_page_edges.add((source_id, target_id))
            neighbors[source_id].add(target_id)
            neighbors[target_id].add(source_id)

        item_id = row.cited_library_item_id
        library = visible_library.get(item_id) if item_id is not None else None
        if library is None:
            continue
        edge = (source_id, library.item_id)
        citation_source_edges.add(edge)
        citation_source_records.add(row.citation_id)
        if library.trust_level == "restricted":
            restricted_citation_source_edges.add(edge)
            restricted_citation_source_records.add(row.citation_id)

    degrees = sorted(len(neighbors[page_id]) for page_id in context_ids)
    context_pages = len(degrees)
    degree_0_pages = sum(value == 0 for value in degrees)
    degree_1_2_pages = sum(0 < value < 3 for value in degrees)
    degree_3_plus_pages = sum(value >= 3 for value in degrees)
    median_degree = float(median(degrees)) if degrees else None

    library_ids = set(visible_library)
    cited_library_ids = {item_id for _source_id, item_id in citation_source_edges}
    restricted_library_ids = {
        item_id
        for item_id, row in visible_library.items()
        if row.trust_level == "restricted"
    }
    restricted_cited_library_ids = cited_library_ids & restricted_library_ids
    version_before = workspace.edge_projection_version
    return RelationDensityReport(
        report_version=RELATION_DENSITY_REPORT_VERSION,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        visibility_scope=RELATION_DENSITY_VISIBILITY_SCOPE,
        percentile_method=RELATION_DENSITY_PERCENTILE_METHOD,
        context_pages=context_pages,
        degree_0_pages=degree_0_pages,
        degree_1_2_pages=degree_1_2_pages,
        degree_3_plus_pages=degree_3_plus_pages,
        pages_with_links=context_pages - degree_0_pages,
        orphan_ratio=_ratio(degree_0_pages, context_pages),
        p25_degree=_nearest_rank(degrees, 0.25),
        median_degree=median_degree,
        p75_degree=_nearest_rank(degrees, 0.75),
        wikilink_edges=len(wikilink_edges),
        citation_page_edges=len(citation_page_edges),
        library_items=len(library_ids),
        library_items_cited=len(cited_library_ids),
        library_items_cited_ratio=_ratio(len(cited_library_ids), len(library_ids)),
        citation_source_edges=len(citation_source_edges),
        citation_source_records=len(citation_source_records),
        restricted_library_items=len(restricted_library_ids),
        restricted_library_items_cited=len(restricted_cited_library_ids),
        restricted_citation_source_edges=len(restricted_citation_source_edges),
        restricted_citation_source_records=len(restricted_citation_source_records),
        gate_threshold=RELATION_SUPPLY_MEDIAN_DEGREE_GATE,
        gate_passed=(
            median_degree is not None
            and median_degree >= RELATION_SUPPLY_MEDIAN_DEGREE_GATE
        ),
        guidance_code=_guidance_code(context_pages, median_degree),
        measured_at=measured_at,
        edge_projection_version_before=version_before,
        edge_projection_version_after=edge_projection_version_after,
        edge_projection_version_stable=(
            edge_projection_version_after is not None
            and version_before == edge_projection_version_after
        ),
    )


def _nearest_rank(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    index = max(0, ceil(percentile * len(values)) - 1)
    return float(values[index])


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _guidance_code(context_pages: int, median_degree: float | None) -> str:
    if context_pages == 0 or median_degree is None:
        return "no_context_pages"
    if median_degree == 0:
        return "degree_0"
    if median_degree < 3:
        return "degree_1_2"
    return "degree_3_plus"

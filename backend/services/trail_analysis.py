from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import ContextPage, EdgeProjection
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.graph import KnowledgeNodeStat
from backend.models.library import LibraryItem
from backend.models.lint import LintFinding, LintRun
from backend.models.mcp_chat import (
    CONSUMPTION_VERBS,
    McpChatArtifactLink,
    McpChatSession,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_restricted_library,
    actor_can_read_sensitive_agent_pages,
)
from backend.services.actor_visible_read import agent_page_requires_sensitive_access
from backend.services.context_visibility import visible_context_version_id
from backend.services.delegated_tasks import create_task
from backend.services.entity_ref import (
    EntityKind,
    context_concept_path,
    library_concept_path,
)
from backend.services.indexing.edge_registry import (
    clear_co_access_edge_projection,
    co_access_pairs,
    co_read_source,
    refresh_co_access_edge_projection,
)

TRAIL_WINDOW_DAYS = 30
HOT_CONCEPT_LIMIT = 10
HOT_PATH_MIN_SESSIONS = 5
HOT_PATH_MIN_TREE_DISTANCE = 3


def run_trail_tick(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
    usage_signals_enabled: bool,
) -> dict[str, int]:
    """One maintenance pass over the workspace's usage trail.

    ``usage_signals_enabled`` is the owner's switch and has no default. Off,
    the co_access projection is removed and no hot path is staged: hot paths
    are the page-to-page co_access pairs, so they are the same signal.
    """

    now = now or datetime.now(UTC)
    if usage_signals_enabled:
        hot_paths = _stage_hot_paths(session, workspace_id, now=now)
        co_access = refresh_co_access_edge_projection(
            session, workspace_id, now=now
        )
    else:
        hot_paths = 0
        co_access = clear_co_access_edge_projection(session, workspace_id)
    authority = recompute_authority(session, workspace_id, now=now)
    overview_tasks = _produce_folder_overview_tasks(session, workspace_id)
    return {
        "hot_path_findings": hot_paths,
        "co_access_edges": co_access,
        "authority_nodes": authority,
        "folder_overview_tasks": overview_tasks,
    }


def list_hot_concepts(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    now: datetime | None = None,
    limit: int = HOT_CONCEPT_LIMIT,
) -> list[dict[str, object]]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=TRAIL_WINDOW_DAYS)
    rows = session.execute(
        select(
            McpChatArtifactLink.resource_kind,
            McpChatArtifactLink.resource_id,
            func.sum(McpChatArtifactLink.occurrence_count).label("score"),
        )
        .join(McpChatSession, McpChatArtifactLink.chat_session_id == McpChatSession.id)
        .where(
            McpChatSession.workspace_id == workspace_id,
            McpChatArtifactLink.verb.in_(CONSUMPTION_VERBS),
            McpChatArtifactLink.last_occurred_at >= cutoff,
        )
        .group_by(
            McpChatArtifactLink.resource_kind,
            McpChatArtifactLink.resource_id,
        )
        .order_by(func.sum(McpChatArtifactLink.occurrence_count).desc())
        .limit(max(limit, limit * 10))
    ).all()
    identities = _hot_concept_identities(session, workspace_id, actor, rows)
    result: list[dict[str, object]] = []
    for kind, entity_id, score in rows:
        hydrated = identities.get((kind, entity_id))
        if hydrated is None:
            continue
        title, path = hydrated
        result.append(
            {
                "kind": kind,
                "id": entity_id,
                "title": title,
                "path": path,
                "score": int(score or 0),
            }
        )
        if len(result) >= limit:
            break
    return result


def recompute_authority(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    # The frozen core migration also invokes this function. Select only its
    # released version columns, since later Workspace settings do not exist yet.
    versions = session.execute(
        select(
            Workspace.edge_projection_version,
            Workspace.authority_projection_version,
        ).where(Workspace.id == workspace_id)
    ).one_or_none()
    if versions is None:
        raise ValueError("workspace not found while computing authority")
    projection_version, authority_version = versions
    if authority_version == projection_version:
        return int(
            session.scalar(
                select(func.count(KnowledgeNodeStat.id)).where(
                    KnowledgeNodeStat.workspace_id == workspace_id
                )
            )
            or 0
        )
    edges = list(
        session.execute(
            select(
                EdgeProjection.source_kind,
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
                EdgeProjection.edge_kind,
                EdgeProjection.weight,
            ).where(EdgeProjection.workspace_id == workspace_id)
        ).all()
    )
    stat_rows = _authority_stat_rows(
        edges,
        workspace_id=workspace_id,
        now=now,
    )
    session.execute(
        delete(KnowledgeNodeStat).where(KnowledgeNodeStat.workspace_id == workspace_id)
    )
    if stat_rows:
        session.connection().execute(KnowledgeNodeStat.__table__.insert(), stat_rows)
    session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(authority_projection_version=projection_version)
    )
    session.flush()
    return len(stat_rows)


def _authority_stat_rows(
    edges,
    *,
    workspace_id: UUID,
    now: datetime,
) -> list[dict[str, object]]:
    """Pure PageRank calculation; persistence is deliberately outside."""

    node_indexes: dict[tuple[str, UUID], int] = {}
    nodes: list[tuple[str, UUID]] = []
    indexed_edges: list[tuple[int, int, float]] = []
    in_degree: list[int] = []

    def node_index(node: tuple[str, UUID]) -> int:
        existing = node_indexes.get(node)
        if existing is not None:
            return existing
        index = len(nodes)
        node_indexes[node] = index
        nodes.append(node)
        in_degree.append(0)
        return index

    for source_kind, source_id, target_kind, target_id, edge_kind, weight in edges:
        source = node_index((source_kind, source_id))
        target = node_index((target_kind, target_id))
        bounded_weight = max(weight, 0.0)
        indexed_edges.append((source, target, bounded_weight))
        in_degree[target] += 1
        if edge_kind == "co_access":
            indexed_edges.append((target, source, bounded_weight))
            in_degree[source] += 1
    if not nodes:
        return []
    count = len(nodes)
    out_weights = [0.0] * count
    for source, _target, weight in indexed_edges:
        out_weights[source] += weight
    damping = 0.85
    transitions = [
        (source, target, damping * weight / out_weights[source])
        for source, target, weight in indexed_edges
        if out_weights[source] > 0
    ]
    dangling_indexes = [index for index, total in enumerate(out_weights) if total <= 0]
    ranks = [1.0 / count] * count
    for _ in range(20):
        dangling = sum(ranks[index] for index in dangling_indexes)
        base = (1.0 - damping) / count + damping * dangling / count
        updated = [base] * count
        for source, target, transition_weight in transitions:
            updated[target] += ranks[source] * transition_weight
        if (
            max(
                abs(current - previous)
                for current, previous in zip(updated, ranks, strict=True)
            )
            < 1e-9
        ):
            ranks = updated
            break
        ranks = updated
    return [
        {
            "workspace_id": workspace_id,
            "kind": kind,
            "entity_id": entity_id,
            "authority": ranks[index],
            "in_degree": in_degree[index],
            "created_at": now,
            "updated_at": now,
        }
        for index, (kind, entity_id) in enumerate(nodes)
    ]


def _stage_hot_paths(session: Session, workspace_id: UUID, *, now: datetime) -> int:
    pair_counts = _context_pair_session_counts(session, workspace_id, now=now)
    candidates = [
        pair for pair, count in pair_counts.items() if count >= HOT_PATH_MIN_SESSIONS
    ]
    if not candidates:
        return 0
    linked = {
        frozenset((source_id, target_id))
        for source_id, target_id in session.execute(
            select(EdgeProjection.source_id, EdgeProjection.target_id).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "wikilink",
                EdgeProjection.source_kind == EntityKind.CONTEXT_PAGE.value,
                EdgeProjection.target_kind == EntityKind.CONTEXT_PAGE.value,
            )
        ).all()
    }
    distances = _tree_distance_lookup(session, workspace_id)
    existing = {
        fingerprint
        for fingerprint in session.scalars(
            select(LintFinding.fingerprint).where(
                LintFinding.workspace_id == workspace_id,
                LintFinding.rule_id == "kb.hot_path_unmaterialized",
                LintFinding.status.in_(("open", "acknowledged", "dismissed")),
            )
        ).all()
    }
    run = None
    created = 0
    for left, right in candidates:
        if frozenset((left, right)) in linked:
            continue
        distance = distances(left, right)
        if distance is None or distance < HOT_PATH_MIN_TREE_DISTANCE:
            continue
        ordered = sorted((left, right), key=str)
        fingerprint = f"hot-path:{ordered[0]}:{ordered[1]}"
        if fingerprint in existing:
            continue
        if run is None:
            run = _maintenance_run(session, workspace_id, now, "trail_tick")
        session.add(
            LintFinding(
                lint_run_id=run.id,
                workspace_id=workspace_id,
                target_type="context_page",
                target_id=left,
                rule_id="kb.hot_path_unmaterialized",
                severity="info",
                status="open",
                suggested_action=json.dumps(
                    {
                        "proposal": "add_cross_link",
                        "related_page_id": str(right),
                        "session_count": pair_counts[(left, right)],
                        "tree_distance": distance,
                    },
                    sort_keys=True,
                ),
                fingerprint=fingerprint,
            )
        )
        existing.add(fingerprint)
        created += 1
    if run is not None:
        run.finding_count_by_severity = {"info": created}
    session.flush()
    return created


def _context_pair_session_counts(
    session: Session, workspace_id: UUID, *, now: datetime
) -> dict[tuple[UUID, UUID], int]:
    """Count the sessions that read each pair of context pages together.

    A hot path is a co_access pair between two context pages: the same
    streamed source, the same pairing, then only the page-to-page pairs. The
    pairing runs over every kind first, so a Library item read between two
    pages separates them here exactly as it does on the graph. The rollup
    this used to read keeps no per-read instant, which forced a whole-session
    clique: one session of 200 reads became 19,900 pairs.
    """

    page = EntityKind.CONTEXT_PAGE.value
    reads = co_read_source(
        session,
        workspace_id,
        since=now - timedelta(days=TRAIL_WINDOW_DAYS),
    )
    # A pair is yielded at most once per session, so this counts sessions.
    counts: defaultdict[tuple[UUID, UUID], int] = defaultdict(int)
    for pair in co_access_pairs(reads):
        if pair.left[0] == page and pair.right[0] == page:
            counts[(pair.left[1], pair.right[1])] += 1
    return dict(counts)


def _tree_distance_lookup(session: Session, workspace_id: UUID):
    nodes = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.visibility_status == "visible",
        )
    ).all()
    parents = {node.id: node.parent_id for node in nodes}
    placements = {
        node.target_id: node.id
        for node in nodes
        if node.target_kind == EntityKind.CONTEXT_PAGE.value
        and node.target_id is not None
        and node.primary_placement
    }

    def distance(left_page: UUID, right_page: UUID) -> int | None:
        left = placements.get(left_page)
        right = placements.get(right_page)
        if left is None or right is None:
            return None
        left_depths: dict[UUID, int] = {}
        cursor = left
        depth = 0
        while cursor is not None and cursor not in left_depths:
            left_depths[cursor] = depth
            cursor = parents.get(cursor)
            depth += 1
        cursor = right
        depth = 0
        seen: set[UUID] = set()
        while cursor is not None and cursor not in seen:
            if cursor in left_depths:
                return left_depths[cursor] + depth
            seen.add(cursor)
            cursor = parents.get(cursor)
            depth += 1
        return None

    return distance


def _produce_folder_overview_tasks(session: Session, workspace_id: UUID) -> int:
    nodes = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.node_kind == "folder",
            WorkspaceTreeNode.visibility_status == "visible",
        )
    ).all()
    child_counts = dict(
        session.execute(
            select(WorkspaceTreeNode.parent_id, func.count(WorkspaceTreeNode.id))
            .where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.visibility_status == "visible",
                WorkspaceTreeNode.parent_id.is_not(None),
            )
            .group_by(WorkspaceTreeNode.parent_id)
        ).all()
    )
    overview_parents = set(
        session.scalars(
            select(WorkspaceTreeNode.parent_id)
            .join(
                ContextPage,
                (WorkspaceTreeNode.target_kind == EntityKind.CONTEXT_PAGE.value)
                & (WorkspaceTreeNode.target_id == ContextPage.id),
            )
            .where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.visibility_status == "visible",
                ContextPage.page_type == "overview",
                ContextPage.lifecycle_status != "archived",
            )
        ).all()
    )
    gaps = {
        node.id
        for node in nodes
        if child_counts.get(node.id, 0) >= 5 and node.id not in overview_parents
    }
    existing_tasks = session.scalars(
        select(ClientDelegatedTask).where(
            ClientDelegatedTask.workspace_id == workspace_id,
            ClientDelegatedTask.task_type == "folder_overview_draft",
            ClientDelegatedTask.status.in_(
                ("pending", "leased", "answered", "dismissed")
            ),
        )
    ).all()
    for task in existing_tasks:
        if task.target_id not in gaps or task.fingerprint != "missing_overview":
            task.status = "expired"
            task.lease_expires_at = None
    session.flush()

    created = 0
    for node in nodes:
        if node.id not in gaps:
            continue
        task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="folder_overview_draft",
            target_kind=EntityKind.WORKSPACE_TREE_NODE.value,
            target_id=node.id,
            source="lint",
            fingerprint="missing_overview",
            payload={"child_count": child_counts[node.id], "title": node.title},
            priority=80,
        )
        created += int(task is not None)
    return created


def _maintenance_run(
    session: Session, workspace_id: UUID, now: datetime, trigger_type: str
) -> LintRun:
    run = LintRun(
        workspace_id=workspace_id,
        lint_scope={"kind": "maintenance"},
        trigger_type=trigger_type,
        status="completed",
        finding_count_by_severity={},
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    session.flush()
    return run


def _hot_concept_identities(
    session: Session,
    workspace_id: UUID,
    actor: ActorContext | None,
    rows: list,
) -> dict[tuple[str, UUID], tuple[str, str]]:
    ids_by_kind: dict[str, set[UUID]] = defaultdict(set)
    for kind, entity_id, _score in rows:
        ids_by_kind[kind].add(entity_id)
    hydrated: dict[tuple[str, UUID], tuple[str, str]] = {}

    context_ids = ids_by_kind.get(EntityKind.CONTEXT_PAGE.value, set())
    if context_ids:
        pages = session.scalars(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id.in_(context_ids),
            )
        ).all()
        for page in pages:
            if (
                page.lifecycle_status == "archived"
                or visible_context_version_id(page, actor)[0] is None
            ):
                continue
            hydrated[(EntityKind.CONTEXT_PAGE.value, page.id)] = (
                page.title,
                context_concept_path(page.slug),
            )

    library_ids = ids_by_kind.get(EntityKind.LIBRARY_ITEM.value, set())
    if library_ids:
        items = session.scalars(
            select(LibraryItem).where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItem.id.in_(library_ids),
            )
        ).all()
        for item in items:
            if item.lifecycle_status in {"archived", "hidden"} or (
                item.trust_level == "restricted"
                and not actor_can_read_restricted_library(actor)
            ):
                continue
            hydrated[(EntityKind.LIBRARY_ITEM.value, item.id)] = (
                item.title or item.uri,
                library_concept_path(item.id),
            )

    agent_ids = ids_by_kind.get(EntityKind.AGENT_PAGE.value, set())
    if agent_ids:
        pages = session.scalars(
            select(AgentPage).where(
                AgentPage.workspace_id == workspace_id,
                AgentPage.id.in_(agent_ids),
            )
        ).all()
        for page in pages:
            if (
                page.lifecycle_status != "active"
                or page.current_active_version_id is None
                or (
                    agent_page_requires_sensitive_access(page)
                    and not actor_can_read_sensitive_agent_pages(actor)
                )
            ):
                continue
            hydrated[(EntityKind.AGENT_PAGE.value, page.id)] = (
                page.title,
                f"/agent?target={page.id}",
            )
    return hydrated

"""Lifetime co-view counts, maintained with the read trace transaction.

The raw per-call history is the replay source. Normal writes inspect only
the changed conversation; no conversation identity is copied into aggregates.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from backend.models.graph import CoAccessPair
from backend.models.mcp_chat import McpChatArtifactVerb, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.policy.mcp_scopes import FOCUSED_READ_TOOLS
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    join_in_workspace,
    select_in_workspace,
)
from backend.services.graph_cache import lock_edge_projection_workspace
from backend.services.indexing.co_read import CoReadPair, EntityRef, Read
from backend.services.indexing.co_read_policy import (
    CO_ACCESS_MIN_SESSIONS,
    CO_ACCESS_SOURCE_BATCH_ROWS,
    co_access_pairs,
)
from backend.services.indexing.edge_projection import (
    EdgeProjectionScope,
    build_edge_row,
    reconcile_edge_projection_rows,
)

PairKey = tuple[EntityRef, EntityRef]


def lock_co_access_workspace(session: Session, workspace_id: UUID) -> None:
    """Serialize counter and projection changes, including a first pair insert.

    A no-op UPDATE also acquires SQLite's writer lock before the read/diff.
    The public graph revision changes only if visible relations change.
    """
    lock_edge_projection_workspace(session, workspace_id)


def _reads(
    session: Session,
    workspace_id: UUID,
    *,
    chat_session_id: UUID | None = None,
    since: datetime | None = None,
) -> Iterable[Read]:
    from backend.services.entity_ref import EntityKind

    conditions = [McpChatToolCallArtifact.verb == McpChatArtifactVerb.VIEWED]
    if chat_session_id is not None:
        conditions.append(McpChatToolCallArtifact.chat_session_id == chat_session_id)
    if since is not None:
        conditions.append(McpChatToolCallArtifact.occurred_at >= since)
    statement = select_in_workspace(
        McpChatToolCallArtifact, workspace_id=workspace_id, conditions=conditions
    )
    statement = join_in_workspace(
        statement,
        McpChatToolCallArtifact,
        MCPToolCall,
        workspace_id=workspace_id,
        on=(MCPToolCall.id == McpChatToolCallArtifact.mcp_tool_call_id)
        & (MCPToolCall.result_status == "succeeded")
        & MCPToolCall.tool_name.in_(FOCUSED_READ_TOOLS),
    )
    kinds = {kind.value for kind in EntityKind}
    rows = session.scalars(
        statement.order_by(
            McpChatToolCallArtifact.chat_session_id,
            McpChatToolCallArtifact.occurred_at,
            McpChatToolCallArtifact.mcp_tool_call_id,
            McpChatToolCallArtifact.id,
        ).execution_options(yield_per=CO_ACCESS_SOURCE_BATCH_ROWS)
    )
    try:
        for row in rows:
            if row.resource_kind in kinds:
                yield Read(
                    session_id=row.chat_session_id,
                    entity=(row.resource_kind, row.resource_id),
                    occurred_at=row.occurred_at,
                    mcp_tool_call_id=row.mcp_tool_call_id,
                    artifact_id=row.id,
                )
    finally:
        rows.close()


def _pairs(reads: Iterable[Read]) -> dict[PairKey, CoReadPair]:

    return {(pair.left, pair.right): pair for pair in co_access_pairs(reads)}


def _identity(pair: PairKey) -> tuple[str, UUID, str, UUID]:
    return (*pair[0], *pair[1])


def _scope(model, keys: Iterable[PairKey]):
    return tuple_(
        model.source_kind, model.source_id, model.target_kind, model.target_id
    ).in_([_identity(key) for key in keys])


def _usage_enabled(session: Session, workspace_id: UUID) -> bool:
    from backend.app.container import SINGLE_DATABASE_TOPOLOGY
    from backend.services.workspace_graph_capability import (
        usage_relation_signals_enabled,
    )

    provider = session.info.get("database_provider")
    if provider is None or provider.storage_topology == SINGLE_DATABASE_TOPOLOGY:
        return usage_relation_signals_enabled(session, workspace_id)
    with provider.control_session_factory()() as control_session:
        return usage_relation_signals_enabled(control_session, workspace_id)


def _recount_pair(session: Session, workspace_id: UUID, key: PairKey):
    """Repair an affected pair after an out-of-order read changes segmentation.

    Only conversations containing both endpoints can contribute. This rare
    repair preserves exact first/last times without a per-conversation ledger.
    """

    def conversations(entity):
        return select_in_workspace(
            McpChatToolCallArtifact,
            workspace_id=workspace_id,
            conditions=(
                McpChatToolCallArtifact.verb == McpChatArtifactVerb.VIEWED,
                McpChatToolCallArtifact.resource_kind == entity[0],
                McpChatToolCallArtifact.resource_id == entity[1],
            ),
        ).with_only_columns(McpChatToolCallArtifact.chat_session_id)

    ids = session.scalars(conversations(key[0]).intersect(conversations(key[1])))
    count, first, last = 0, None, None
    for chat_id in ids.all():
        pair = _pairs(_reads(session, workspace_id, chat_session_id=chat_id)).get(key)
        if pair is not None:
            count += 1
            first = min(first, pair.first_pair_at) if first else pair.first_pair_at
            last = max(last, pair.last_pair_at) if last else pair.last_pair_at
    return count, first, last


def record_co_access_read(
    session: Session,
    *,
    workspace_id: UUID,
    chat_session_id: UUID,
    artifact_id: UUID,
) -> None:
    """Apply one newly inserted VIEWED row exactly once in its transaction.

    The caller locks the workspace before inserting the row. A duplicate
    per-call artifact never enters this function. Repeated views in one chat
    update dates but cannot add a second supporting conversation.
    """
    reads = list(_reads(session, workspace_id, chat_session_id=chat_session_id))
    after = _pairs(reads)
    before = _pairs(read for read in reads if read.artifact_id != artifact_id)
    changed = {
        key
        for key in before.keys() | after.keys()
        if key not in before
        or key not in after
        or (before[key].first_pair_at, before[key].last_pair_at)
        != (after[key].first_pair_at, after[key].last_pair_at)
    }
    if not changed:
        return

    for key in sorted(changed):
        row = session.scalar(
            select_in_workspace(
                CoAccessPair,
                workspace_id=workspace_id,
                conditions=(_scope(CoAccessPair, [key]),),
            )
        )
        old, new = before.get(key), after.get(key)
        # A late insertion may remove a pair or move an extremal timestamp
        # inward. Recount its actual contributors instead of guessing dates.
        repair = (
            (row is None and old is not None)
            or new is None
            or (
                old is not None
                and row is not None
                and (
                    old.first_pair_at == row.first_observed_at
                    and new.first_pair_at > old.first_pair_at
                    or old.last_pair_at == row.occurred_at
                    and new.last_pair_at < old.last_pair_at
                )
            )
        )
        if repair:
            count, first, last = _recount_pair(session, workspace_id, key)
        else:
            count = (row.support_session_count if row else 0) + (old is None)
            first = (
                min(row.first_observed_at, new.first_pair_at)
                if row
                else new.first_pair_at
            )
            last = max(row.occurred_at, new.last_pair_at) if row else new.last_pair_at
        if not count:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(
                CoAccessPair(
                    workspace_id=workspace_id,
                    source_kind=key[0][0],
                    source_id=key[0][1],
                    target_kind=key[1][0],
                    target_id=key[1][1],
                    support_session_count=count,
                    first_observed_at=first,
                    occurred_at=last,
                )
            )
        else:
            row.support_session_count = count
            row.first_observed_at = first
            row.occurred_at = last
    session.flush()
    sync_co_access_projection(
        session,
        workspace_id,
        keys=changed,
        usage_signals_enabled=_usage_enabled(session, workspace_id),
    )


def sync_co_access_projection(
    session: Session,
    workspace_id: UUID,
    *,
    usage_signals_enabled: bool,
    keys: set[PairKey] | None = None,
) -> int:
    """Restore/toggle the read model from counters without reading MCP history."""
    from backend.models.context import EdgeProjection
    from backend.services.entity_ref import EntityKind
    from backend.services.graph_cache import bump_edge_version

    lock_co_access_workspace(session, workspace_id)
    pairs_statement = select_in_workspace(
        CoAccessPair,
        workspace_id=workspace_id,
        conditions=(_scope(CoAccessPair, keys),) if keys is not None else (),
    )
    edges_statement = select_in_workspace(
        EdgeProjection,
        workspace_id=workspace_id,
        conditions=(
            EdgeProjection.edge_kind == "co_access",
            *((_scope(EdgeProjection, keys),) if keys is not None else ()),
        ),
    )
    desired = [
        build_edge_row(
            workspace_id=workspace_id,
            source_kind=EntityKind(pair.source_kind),
            source_id=pair.source_id,
            target_kind=EntityKind(pair.target_kind),
            target_id=pair.target_id,
            edge_kind="co_access",
            provenance="usage",
            source_table="co_access_pair",
            weight=float(pair.support_session_count),
            occurred_at=pair.occurred_at,
            support_session_count=pair.support_session_count,
            first_observed_at=pair.first_observed_at,
        )
        for pair in session.scalars(pairs_statement)
        if usage_signals_enabled
        and pair.support_session_count >= CO_ACCESS_MIN_SESSIONS
    ]
    delta = reconcile_edge_projection_rows(
        session,
        scope=EdgeProjectionScope(workspace_id, frozenset({"co_access"})),
        existing_rows=list(session.scalars(edges_statement)),
        desired_rows=desired,
    )
    if delta.changed:
        bump_edge_version(session, workspace_id)
    return delta.desired_count


def co_access_projection_needs_repair(session: Session, workspace_id: UUID) -> bool:
    """Recover a missed policy-toggle job without replaying the read history."""
    from backend.models.context import EdgeProjection

    matches = (
        (EdgeProjection.workspace_id == CoAccessPair.workspace_id)
        & (EdgeProjection.edge_kind == "co_access")
        & (EdgeProjection.source_kind == CoAccessPair.source_kind)
        & (EdgeProjection.source_id == CoAccessPair.source_id)
        & (EdgeProjection.target_kind == CoAccessPair.target_kind)
        & (EdgeProjection.target_id == CoAccessPair.target_id)
        & (EdgeProjection.support_session_count == CoAccessPair.support_session_count)
        & (EdgeProjection.weight == CoAccessPair.support_session_count)
        & (EdgeProjection.first_observed_at == CoAccessPair.first_observed_at)
        & (EdgeProjection.occurred_at == CoAccessPair.occurred_at)
    )
    mismatch = (
        select_in_workspace(
            CoAccessPair,
            workspace_id=workspace_id,
            conditions=(
                CoAccessPair.support_session_count >= CO_ACCESS_MIN_SESSIONS,
                ~select_in_workspace(
                    EdgeProjection, workspace_id=workspace_id, conditions=(matches,)
                )
                .with_only_columns(EdgeProjection.id)
                .exists(),
            ),
        )
        .with_only_columns(CoAccessPair.id)
        .limit(1)
    )
    return session.scalar(mismatch) is not None


def rebuild_co_access_pairs(
    session: Session, workspace_id: UUID, *, usage_signals_enabled: bool = True
) -> int:
    """Explicit recovery/reindex replay; normal graph reads never call this."""

    lock_co_access_workspace(session, workspace_id)
    aggregates = {}
    for pair in co_access_pairs(_reads(session, workspace_id)):
        key = (pair.left, pair.right)
        previous = aggregates.get(key)
        aggregates[key] = (
            (
                previous[0] + 1,
                min(previous[1], pair.first_pair_at),
                max(previous[2], pair.last_pair_at),
            )
            if previous
            else (1, pair.first_pair_at, pair.last_pair_at)
        )
    session.execute(delete_in_workspace(CoAccessPair, workspace_id=workspace_id))
    for key, (count, first, last) in aggregates.items():
        session.add(
            CoAccessPair(
                workspace_id=workspace_id,
                source_kind=key[0][0],
                source_id=key[0][1],
                target_kind=key[1][0],
                target_id=key[1][1],
                support_session_count=count,
                first_observed_at=first,
                occurred_at=last,
            )
        )
    session.flush()
    return sync_co_access_projection(
        session, workspace_id, usage_signals_enabled=usage_signals_enabled
    )


def co_access_projection_rows(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Project lifetime counters; this never scans raw MCP history."""
    from sqlalchemy import inspect
    from sqlalchemy.engine import Connection

    from backend.services.entity_ref import EntityKind

    # Frozen core upgrade invokes this seam before the new counters exist.
    # Its read model is superseded by the new revision's frozen backfill.
    if (
        session.join_transaction_mode == "rollback_only"
        and isinstance(session.get_bind(), Connection)
        and not inspect(session.connection()).has_table("co_access_pair")
    ):
        return []
    return [
        build_edge_row(
            workspace_id=workspace_id,
            source_kind=EntityKind(pair.source_kind),
            source_id=pair.source_id,
            target_kind=EntityKind(pair.target_kind),
            target_id=pair.target_id,
            edge_kind="co_access",
            provenance="usage",
            source_table="co_access_pair",
            weight=float(pair.support_session_count),
            occurred_at=pair.occurred_at,
            support_session_count=pair.support_session_count,
            first_observed_at=pair.first_observed_at,
        )
        for pair in session.scalars(
            select_in_workspace(
                CoAccessPair,
                workspace_id=workspace_id,
                conditions=(
                    CoAccessPair.support_session_count >= CO_ACCESS_MIN_SESSIONS,
                ),
            )
        )
    ]


def co_access_reads(session: Session, workspace_id: UUID, *, since: datetime):
    """The same successful-body source for bounded hot-path analysis."""
    return _reads(session, workspace_id, since=since)

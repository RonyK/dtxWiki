"""Released reference-based usage semantics for historical migrations.

This snapshot keeps v0.1 data repair and downgrade independent of runtime
verb, counter, ranking and projection changes introduced by later releases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .co_read_v1 import Read, co_read_pairs

HOT_PATH_MIN_SESSIONS = 5
ENTITY_KINDS = frozenset(
    {
        "context_page",
        "library_item",
        "library_artifact",
        "agent_page",
        "review_item",
        "content_chunk",
        "context_claim",
        "feedback",
        "change_set",
        "ask_turn",
        "library_item_version",
        "library_sync_run",
        "workspace_tree_node",
        "context_page_version",
        "context_media_asset",
    }
)


def _utc(value):
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _tables(connection):
    metadata = sa.MetaData()
    tables = tuple(
        sa.Table(name, metadata, autoload_with=connection)
        for name in (
            "mcp_chat_tool_call_artifact",
            "edge_projection",
            "workspace",
            "knowledge_node_stat",
        )
    )
    # SQLite reflection cannot recover SQLAlchemy's UUID processors from CHAR.
    for table in tables:
        for column in table.c:
            if column.name == "id" or column.name.endswith("_id"):
                column.type = sa.Uuid()
    return tables


def _evidence(connection, source, workspace_id, now):
    rows = connection.execute(
        sa.select(source)
        .where(
            source.c.workspace_id == workspace_id,
            source.c.verb == "referenced",
            source.c.occurred_at >= now - timedelta(days=30),
        )
        .order_by(
            source.c.chat_session_id,
            source.c.occurred_at,
            source.c.mcp_tool_call_id,
            source.c.id,
        )
        .execution_options(yield_per=5000)
    )
    try:
        reads = (
            Read(
                row["chat_session_id"],
                (row["resource_kind"], row["resource_id"]),
                _utc(row["occurred_at"]),
                row["mcp_tool_call_id"],
                row["id"],
            )
            for row in rows.mappings()
            if row["resource_kind"] in ENTITY_KINDS
        )
        aggregate = {}
        for pair in co_read_pairs(
            reads, max_gap=timedelta(minutes=30), window=5, max_per_segment=12
        ):
            age = max(0.0, (now - pair.last_pair_at).total_seconds() / 86400)
            weight = min(pair.left_reads, pair.right_reads) * 0.5 ** (age / 30)
            key = (*pair.left, *pair.right)
            value = aggregate.setdefault(
                key, [0.0, 0, pair.first_pair_at, pair.last_pair_at]
            )
            value[0] += weight
            value[1] += 1
            value[2] = min(value[2], pair.first_pair_at)
            value[3] = max(value[3], pair.last_pair_at)
        return aggregate
    finally:
        rows.close()


def restore_legacy_usage(
    connection: Connection,
    workspace_id: UUID,
    *,
    now: datetime,
    usage_signals_enabled: bool = True,
):
    """Rebuild old decayed edges and authority; return valid Context pair counts."""
    source, edges, workspace, stats = _tables(connection)
    now = _utc(now)
    aggregate = _evidence(connection, source, workspace_id, now)
    desired = []
    for (source_kind, source_id, target_kind, target_id), value in aggregate.items():
        weight, count, first, last = value
        if count < 2 or not usage_signals_enabled:
            continue
        row = dict(
            source_kind=source_kind,
            source_id=source_id,
            target_kind=target_kind,
            target_id=target_id,
            edge_kind="co_access",
            provenance="usage",
            weight=weight,
            occurred_at=last,
            source_ref={"table": "mcp_chat_tool_call_artifact"},
        )
        if "support_session_count" in edges.c:
            row.update(support_session_count=count, first_observed_at=first)
        desired.append(row)
    existing = list(
        connection.execute(
            sa.select(edges).where(
                edges.c.workspace_id == workspace_id, edges.c.edge_kind == "co_access"
            )
        ).mappings()
    )
    fields = (
        "source_kind",
        "source_id",
        "target_kind",
        "target_id",
        "edge_kind",
        "provenance",
        "weight",
        "occurred_at",
        "source_ref",
    )
    if "support_session_count" in edges.c:
        fields += ("support_session_count", "first_observed_at")

    def identity(row):
        return tuple(
            _utc(row[field])
            if isinstance(row[field], datetime)
            else repr(row[field])
            if isinstance(row[field], dict)
            else row[field]
            for field in fields
        )

    if {identity(row) for row in existing} != {identity(row) for row in desired}:
        connection.execute(
            edges.delete().where(
                edges.c.workspace_id == workspace_id, edges.c.edge_kind == "co_access"
            )
        )
        if desired:
            connection.execute(
                edges.insert(),
                [
                    dict(
                        id=uuid4(),
                        created_at=now,
                        workspace_id=workspace_id,
                        last_seen_at=now,
                        **row,
                    )
                    for row in desired
                ],
            )
        connection.execute(
            workspace.update()
            .where(workspace.c.id == workspace_id)
            .values(edge_projection_version=workspace.c.edge_projection_version + 1)
        )
    _restore_authority(connection, workspace, edges, stats, workspace_id, now)
    return {
        (key[1], key[3]): value[1]
        for key, value in aggregate.items()
        if key[0] == key[2] == "context_page"
    }


def _restore_authority(connection, workspace, edges, stats, workspace_id, now):
    versions = connection.execute(
        sa.select(
            workspace.c.edge_projection_version,
            workspace.c.authority_projection_version,
        ).where(workspace.c.id == workspace_id)
    ).one()
    if versions[0] == versions[1]:
        return
    edge_rows = connection.execute(
        sa.select(
            edges.c.source_kind,
            edges.c.source_id,
            edges.c.target_kind,
            edges.c.target_id,
            edges.c.edge_kind,
            edges.c.weight,
        ).where(edges.c.workspace_id == workspace_id)
    ).all()
    rows = _authority_stat_rows(edge_rows, workspace_id=workspace_id, now=now)
    connection.execute(stats.delete().where(stats.c.workspace_id == workspace_id))
    if rows:
        connection.execute(stats.insert(), [dict(id=uuid4(), **row) for row in rows])
    connection.execute(
        workspace.update()
        .where(workspace.c.id == workspace_id)
        .values(authority_projection_version=versions[0])
    )


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


def legacy_migration_session(session) -> bool:
    """Use released semantics only in its connection-owned migration context."""
    if session.join_transaction_mode != "rollback_only" or not isinstance(
        session.get_bind(), Connection
    ):
        return False
    connection = session.connection()
    return bool(
        connection.get_execution_options().get("dtxwiki_legacy_usage_projection")
        or not sa.inspect(connection).has_table("co_access_pair")
    )


def legacy_context_pair_counts(session, workspace_id, *, now):
    source, _edges, _workspace, _stats = _tables(session.connection())
    return {
        (key[1], key[3]): value[1]
        for key, value in _evidence(
            session.connection(), source, workspace_id, _utc(now)
        ).items()
        if key[0] == key[2] == "context_page"
    }


def recompute_legacy_authority(session, workspace_id, *, now):
    connection = session.connection()
    _source, edges, workspace, stats = _tables(connection)
    _restore_authority(connection, workspace, edges, stats, workspace_id, _utc(now))
    return int(
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(stats)
            .where(stats.c.workspace_id == workspace_id)
        )
        or 0
    )

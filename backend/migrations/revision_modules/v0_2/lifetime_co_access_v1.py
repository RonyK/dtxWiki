"""One-time body-view reclassification and lifetime pair backfill.

Every fact comes from persisted per-call provenance. Rollups without that
provenance remain references; an editing or outline touch never becomes a view.
This revision owns its pairing snapshot and does not call runtime services.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from backend.migrations.revision_modules.v0_2.co_read_v1 import Read, co_read_pairs

FOCUSED_READ_TOOLS = ("fetch", "read", "read_agent_page", "read_concept", "read_page")
DOCUMENT_KINDS = ("context_page", "library_item", "agent_page")

tool_call = sa.table(
    "mcp_tool_call",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("tool_name", sa.Text()),
    sa.column("result_status", sa.Text()),
)
per_call = sa.table(
    "mcp_chat_tool_call_artifact",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("chat_session_id", sa.Uuid()),
    sa.column("mcp_tool_call_id", sa.Uuid()),
    sa.column("resource_kind", sa.Text()),
    sa.column("resource_id", sa.Uuid()),
    sa.column("verb", sa.Text()),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
)
rollup = sa.table(
    "mcp_chat_artifact_link",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("chat_session_id", sa.Uuid()),
    sa.column("resource_kind", sa.Text()),
    sa.column("resource_id", sa.Uuid()),
    sa.column("verb", sa.Text()),
    sa.column("first_occurred_at", sa.DateTime(timezone=True)),
    sa.column("last_occurred_at", sa.DateTime(timezone=True)),
    sa.column("occurrence_count", sa.Integer()),
    sa.column("last_mcp_tool_call_id", sa.Uuid()),
    sa.column("last_read_version_id", sa.Uuid()),
    sa.column("last_read_root_hash", sa.Text()),
    sa.column("last_read_at", sa.DateTime(timezone=True)),
)
workspace = sa.table(
    "workspace",
    sa.column("id", sa.Uuid()),
    sa.column("usage_relation_signals_enabled", sa.Boolean()),
    sa.column("edge_projection_version", sa.Integer()),
)
pair_table = sa.table(
    "co_access_pair",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("source_kind", sa.Text()),
    sa.column("source_id", sa.Uuid()),
    sa.column("target_kind", sa.Text()),
    sa.column("target_id", sa.Uuid()),
    sa.column("support_session_count", sa.Integer()),
    sa.column("first_observed_at", sa.DateTime(timezone=True)),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
)
edges = sa.table(
    "edge_projection",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("source_kind", sa.Text()),
    sa.column("source_id", sa.Uuid()),
    sa.column("target_kind", sa.Text()),
    sa.column("target_id", sa.Uuid()),
    sa.column("edge_kind", sa.Text()),
    sa.column("provenance", sa.Text()),
    sa.column("weight", sa.Float()),
    sa.column("support_session_count", sa.Integer()),
    sa.column("first_observed_at", sa.DateTime(timezone=True)),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("source_ref", sa.JSON()),
    sa.column("last_seen_at", sa.DateTime(timezone=True)),
)


def upgrade(connection: Connection) -> None:
    _move_body_views(connection)
    _backfill_pairs(connection)


def downgrade(connection: Connection) -> None:
    """Keep old runtimes able to read the trace after dropping the new model."""

    groups = connection.execute(
        sa.select(rollup).where(rollup.c.verb == "viewed")
    ).mappings()
    for viewed in list(groups):
        _merge_viewed_rollup_back(connection, viewed)
    connection.execute(
        per_call.update().where(per_call.c.verb == "viewed").values(verb="referenced")
    )


def _identity(table, key):
    return (
        table.c.workspace_id == key[0],
        table.c.chat_session_id == key[1],
        table.c.resource_kind == key[2],
        table.c.resource_id == key[3],
    )


def _move_body_views(connection: Connection) -> None:
    eligible = (
        sa.select(per_call)
        .select_from(
            per_call.join(
                tool_call,
                sa.and_(
                    per_call.c.workspace_id == tool_call.c.workspace_id,
                    per_call.c.mcp_tool_call_id == tool_call.c.id,
                ),
            )
        )
        .where(
            per_call.c.verb == "referenced",
            tool_call.c.tool_name.in_(FOCUSED_READ_TOOLS),
            tool_call.c.result_status == "succeeded",
        )
    )
    grouped = {}
    for row in connection.execute(
        eligible.execution_options(yield_per=5_000)
    ).mappings():
        key = (
            row["workspace_id"],
            row["chat_session_id"],
            row["resource_kind"],
            row["resource_id"],
        )
        group = grouped.setdefault(
            key,
            {
                "count": 0,
                "first": row["occurred_at"],
                "last": dict(row),
            },
        )
        group["count"] += 1
        group["first"] = min(group["first"], row["occurred_at"])
        if _read_order(row) > _read_order(group["last"]):
            group["last"] = dict(row)
    if not grouped:
        return

    eligible_ids = eligible.with_only_columns(per_call.c.id)
    connection.execute(
        per_call.update().where(per_call.c.id.in_(eligible_ids)).values(verb="viewed")
    )
    for key, changed in grouped.items():
        reference = (
            connection.execute(
                sa.select(rollup).where(
                    *_identity(rollup, key), rollup.c.verb == "referenced"
                )
            )
            .mappings()
            .first()
        )
        viewed = (
            connection.execute(
                sa.select(rollup).where(
                    *_identity(rollup, key), rollup.c.verb == "viewed"
                )
            )
            .mappings()
            .first()
        )
        values = {
            "occurrence_count": changed["count"]
            + (viewed["occurrence_count"] if viewed else 0),
            "first_occurred_at": changed["first"],
            "last_occurred_at": changed["last"]["occurred_at"],
            "last_mcp_tool_call_id": changed["last"]["mcp_tool_call_id"],
        }
        if viewed:
            values["first_occurred_at"] = min(
                values["first_occurred_at"], viewed["first_occurred_at"]
            )
            if _rollup_order(viewed) > (
                values["last_occurred_at"],
                values["last_mcp_tool_call_id"],
            ):
                values.update(
                    last_occurred_at=viewed["last_occurred_at"],
                    last_mcp_tool_call_id=viewed["last_mcp_tool_call_id"],
                )

        remaining = []
        remaining_count = 0
        if reference:
            remaining = list(
                connection.execute(
                    sa.select(per_call).where(
                        *_identity(per_call, key), per_call.c.verb == "referenced"
                    )
                ).mappings()
            )
            remaining_count = max(
                len(remaining), reference["occurrence_count"] - changed["count"]
            )
        # Read-base pointers are independent correctness state. Keep a pointer
        # with a surviving reference, or transfer it before removing that row.
        pointer = reference if reference and remaining_count == 0 else None
        if viewed and (
            pointer is None or _pointer_time(viewed) >= _pointer_time(pointer)
        ):
            pointer = viewed
        if pointer:
            values.update({field: pointer[field] for field in _POINTER_FIELDS})
        if viewed:
            connection.execute(
                rollup.update().where(rollup.c.id == viewed["id"]).values(**values)
            )
        else:
            connection.execute(
                rollup.insert().values(
                    id=uuid4(),
                    workspace_id=key[0],
                    chat_session_id=key[1],
                    resource_kind=key[2],
                    resource_id=key[3],
                    verb="viewed",
                    **values,
                )
            )
        if not reference:
            continue
        if remaining_count == 0:
            connection.execute(rollup.delete().where(rollup.c.id == reference["id"]))
        else:
            values = {"occurrence_count": remaining_count}
            # A rollup can contain older touches with no per-call provenance;
            # do not pretend their first/last times are known after splitting.
            if len(remaining) == remaining_count:
                ordered = sorted(remaining, key=_read_order)
                values.update(
                    first_occurred_at=ordered[0]["occurred_at"],
                    last_occurred_at=ordered[-1]["occurred_at"],
                    last_mcp_tool_call_id=ordered[-1]["mcp_tool_call_id"],
                )
            connection.execute(
                rollup.update().where(rollup.c.id == reference["id"]).values(**values)
            )


_POINTER_FIELDS = ("last_read_version_id", "last_read_root_hash", "last_read_at")


def _pointer_time(row):
    return row["last_read_at"] or datetime.min.replace(
        tzinfo=row["last_occurred_at"].tzinfo
    )


def _read_order(row):
    return row["occurred_at"], row["mcp_tool_call_id"], row["id"]


def _rollup_order(row):
    return row["last_occurred_at"], row["last_mcp_tool_call_id"]


def _merge_viewed_rollup_back(connection: Connection, viewed) -> None:
    key = tuple(
        viewed[field]
        for field in ("workspace_id", "chat_session_id", "resource_kind", "resource_id")
    )
    reference = (
        connection.execute(
            sa.select(rollup).where(
                *_identity(rollup, key), rollup.c.verb == "referenced"
            )
        )
        .mappings()
        .first()
    )
    if reference is None:
        connection.execute(
            rollup.update().where(rollup.c.id == viewed["id"]).values(verb="referenced")
        )
        return
    latest = max((reference, viewed), key=_rollup_order)
    pointer = max((reference, viewed), key=_pointer_time)
    connection.execute(
        rollup.update()
        .where(rollup.c.id == reference["id"])
        .values(
            occurrence_count=reference["occurrence_count"] + viewed["occurrence_count"],
            first_occurred_at=min(
                reference["first_occurred_at"], viewed["first_occurred_at"]
            ),
            last_occurred_at=latest["last_occurred_at"],
            last_mcp_tool_call_id=latest["last_mcp_tool_call_id"],
            **{field: pointer[field] for field in _POINTER_FIELDS},
        )
    )
    connection.execute(rollup.delete().where(rollup.c.id == viewed["id"]))


def _backfill_pairs(connection: Connection) -> None:
    now = datetime.now(UTC)
    for workspace_id, enabled in connection.execute(
        sa.select(workspace.c.id, workspace.c.usage_relation_signals_enabled)
    ):
        source = connection.execute(
            sa.select(per_call)
            .where(
                per_call.c.workspace_id == workspace_id,
                per_call.c.verb == "viewed",
                per_call.c.resource_kind.in_(DOCUMENT_KINDS),
            )
            .order_by(
                per_call.c.chat_session_id,
                per_call.c.occurred_at,
                per_call.c.mcp_tool_call_id,
                per_call.c.id,
            )
            .execution_options(yield_per=5_000)
        ).mappings()
        reads = (
            Read(
                row["chat_session_id"],
                (row["resource_kind"], row["resource_id"]),
                row["occurred_at"],
                row["mcp_tool_call_id"],
                row["id"],
            )
            for row in source
        )
        aggregate = {}
        for pair in co_read_pairs(
            reads, max_gap=timedelta(minutes=30), window=5, max_per_segment=12
        ):
            key = (*pair.left, *pair.right)
            value = aggregate.setdefault(
                key, [0, pair.first_pair_at, pair.last_pair_at]
            )
            value[0] += 1
            value[1] = min(value[1], pair.first_pair_at)
            value[2] = max(value[2], pair.last_pair_at)
        rows = (
            dict(
                id=uuid4(),
                workspace_id=workspace_id,
                source_kind=key[0],
                source_id=key[1],
                target_kind=key[2],
                target_id=key[3],
                support_session_count=value[0],
                first_observed_at=value[1],
                occurred_at=value[2],
            )
            for key, value in aggregate.items()
        )
        _insert_batches(connection, pair_table, rows)

        desired = {
            key: (
                float(count),
                count,
                first,
                last,
                "usage",
                {"table": "co_access_pair"},
            )
            for key, (count, first, last) in aggregate.items()
            if enabled and count >= 2
        }
        existing = {
            (
                row["source_kind"],
                row["source_id"],
                row["target_kind"],
                row["target_id"],
            ): (
                row["weight"],
                row["support_session_count"],
                row["first_observed_at"],
                row["occurred_at"],
                row["provenance"],
                row["source_ref"],
            )
            for row in connection.execute(
                sa.select(edges).where(
                    edges.c.workspace_id == workspace_id,
                    edges.c.edge_kind == "co_access",
                )
            ).mappings()
        }
        if existing == desired:
            continue
        connection.execute(
            edges.delete().where(
                edges.c.workspace_id == workspace_id, edges.c.edge_kind == "co_access"
            )
        )
        visible = (
            dict(
                id=uuid4(),
                created_at=now,
                workspace_id=workspace_id,
                source_kind=key[0],
                source_id=key[1],
                target_kind=key[2],
                target_id=key[3],
                edge_kind="co_access",
                provenance=value[4],
                weight=value[0],
                support_session_count=value[1],
                first_observed_at=value[2],
                occurred_at=value[3],
                source_ref=value[5],
                last_seen_at=now,
            )
            for key, value in desired.items()
        )
        _insert_batches(connection, edges, visible)
        connection.execute(
            workspace.update()
            .where(workspace.c.id == workspace_id)
            .values(edge_projection_version=workspace.c.edge_projection_version + 1)
        )


def _insert_batches(connection, table, rows):
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) == 1_000:
            connection.execute(table.insert(), batch)
            batch = []
    if batch:
        connection.execute(table.insert(), batch)

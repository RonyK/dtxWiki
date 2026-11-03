"""One-time body-view reclassification and lifetime pair/cursor backfill.

Every fact comes from persisted per-call provenance. Rollups without that
provenance remain references; an editing or outline touch never becomes a view.
This revision owns its pairing implementation independently of runtime services.
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
cursor_per_call = sa.table(
    "mcp_chat_tool_call_artifact",
    sa.column("id", sa.Uuid()),
    sa.column("co_access_segment", sa.Integer()),
    sa.column("co_access_position", sa.Integer()),
)
chat_session = sa.table(
    "mcp_chat_session",
    sa.column("id", sa.Uuid()),
    sa.column("co_access_state", sa.JSON()),
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
    sa.column("authority_projection_version", sa.Integer()),
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
    # Stored authority must be recalculated with bounded lifetime weights.
    connection.execute(workspace.update().values(authority_projection_version=-1))


def downgrade(connection: Connection) -> None:
    """Restore reference traces and the old rolling, decayed projection."""
    # Invalidate before legacy restore even when its edge rows compare equal.
    connection.execute(workspace.update().values(authority_projection_version=-1))
    groups = list(
        connection.execute(
            sa.select(rollup).where(rollup.c.verb == "viewed")
        ).mappings()
    )
    updates, deletes = [], []
    for batch in _batches(groups):
        keys = [_key(row) for row in batch]
        references = {
            _key(row): row
            for row in connection.execute(
                sa.select(rollup).where(
                    _identities(rollup, keys), rollup.c.verb == "referenced"
                )
            ).mappings()
        }
        for viewed in batch:
            reference = references.get(_key(viewed))
            if reference is None:
                updates.append((viewed["id"], {"verb": "referenced"}))
                continue
            latest = max((reference, viewed), key=_rollup_order)
            pointer = max((reference, viewed), key=_pointer_time)
            updates.append(
                (
                    reference["id"],
                    dict(
                        occurrence_count=reference["occurrence_count"]
                        + viewed["occurrence_count"],
                        first_occurred_at=min(
                            reference["first_occurred_at"], viewed["first_occurred_at"]
                        ),
                        last_occurred_at=latest["last_occurred_at"],
                        last_mcp_tool_call_id=latest["last_mcp_tool_call_id"],
                        **{field: pointer[field] for field in _POINTER_FIELDS},
                    ),
                )
            )
            deletes.append(viewed["id"])
    _update_rollups(connection, updates)
    _delete_rollups(connection, deletes)
    connection.execute(
        per_call.update().where(per_call.c.verb == "viewed").values(verb="referenced")
    )
    # A pre-lifetime runtime treats weight as an already-decayed read sum.
    # Never leave cumulative counts behind after their backing table is gone.
    from . import legacy_usage_v1

    now = datetime.now(UTC)
    for workspace_id, enabled in connection.execute(
        sa.select(workspace.c.id, workspace.c.usage_relation_signals_enabled)
    ):
        legacy_usage_v1.restore_legacy_usage(
            connection, workspace_id, now=now, usage_signals_enabled=enabled
        )


def _focused_reads(verb):
    """Shared eligibility for reclassification and source replay."""
    return (
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
            per_call.c.verb == verb,
            per_call.c.resource_kind.in_(DOCUMENT_KINDS),
            tool_call.c.tool_name.in_(FOCUSED_READ_TOOLS),
            tool_call.c.result_status == "succeeded",
        )
    )


def _move_body_views(connection: Connection) -> None:
    eligible = _focused_reads("referenced")
    grouped = {}
    source = connection.execute(eligible.execution_options(yield_per=5_000)).mappings()
    try:
        for row in source:
            key = _key(row)
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
    finally:
        source.close()
    if not grouped:
        return

    eligible_ids = eligible.with_only_columns(per_call.c.id)
    connection.execute(
        per_call.update().where(per_call.c.id.in_(eligible_ids)).values(verb="viewed")
    )
    for key_batch in _batches(list(grouped)):
        existing = {
            (_key(row), row["verb"]): row
            for row in connection.execute(
                sa.select(rollup).where(
                    _identities(rollup, key_batch),
                    rollup.c.verb.in_(("referenced", "viewed")),
                )
            ).mappings()
        }
        remaining_by_key = {}
        for row in connection.execute(
            sa.select(per_call).where(
                _identities(per_call, key_batch), per_call.c.verb == "referenced"
            )
        ).mappings():
            summary = remaining_by_key.setdefault(
                _key(row),
                {
                    "count": 0,
                    "first": row["occurred_at"],
                    "last": row,
                },
            )
            summary["count"] += 1
            summary["first"] = min(summary["first"], row["occurred_at"])
            if _read_order(row) > _read_order(summary["last"]):
                summary["last"] = row
        updates, inserts, deletes = [], [], []
        for key in key_batch:
            changed = grouped[key]
            reference = existing.get((key, "referenced"))
            viewed = existing.get((key, "viewed"))
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
            remaining = remaining_by_key.get(key)
            known_remaining_count = remaining["count"] if remaining else 0
            remaining_count = (
                max(
                    known_remaining_count,
                    reference["occurrence_count"] - changed["count"],
                )
                if reference
                else 0
            )
            # Preserve read-base correctness independently of display verbs.
            pointer = reference if reference and remaining_count == 0 else None
            if viewed and (
                pointer is None or _pointer_time(viewed) >= _pointer_time(pointer)
            ):
                pointer = viewed
            values.update(
                {
                    field: pointer[field] if pointer else None
                    for field in _POINTER_FIELDS
                }
            )
            if viewed:
                updates.append((viewed["id"], values))
            else:
                inserts.append(
                    dict(
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
                deletes.append(reference["id"])
                continue
            values = {"occurrence_count": remaining_count}
            # Rollup-only history has no proven replacement first/last time.
            if remaining and known_remaining_count == remaining_count:
                values.update(
                    first_occurred_at=remaining["first"],
                    last_occurred_at=remaining["last"]["occurred_at"],
                    last_mcp_tool_call_id=remaining["last"]["mcp_tool_call_id"],
                )
            updates.append((reference["id"], values))
        _insert_batches(connection, rollup, inserts)
        _update_rollups(connection, updates)
        _delete_rollups(connection, deletes)


def _key(row):
    return tuple(
        row[field]
        for field in ("workspace_id", "chat_session_id", "resource_kind", "resource_id")
    )


def _batches(values, size=500):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _identities(table, keys):
    return sa.tuple_(
        table.c.workspace_id,
        table.c.chat_session_id,
        table.c.resource_kind,
        table.c.resource_id,
    ).in_(keys)


def _update_rollups(connection, updates):
    _update_rows(connection, rollup, updates)


def _update_rows(connection, table, updates):
    groups = {}
    for row_id, values in updates:
        groups.setdefault(tuple(sorted(values)), []).append((row_id, values))
    for columns, rows in groups.items():
        for batch in _batches(rows):
            values = {
                column: sa.case(
                    *(
                        (
                            sa.literal(row_id, type_=sa.Uuid()),
                            sa.literal(row[column], type_=table.c[column].type),
                        )
                        for row_id, row in batch
                    ),
                    value=table.c.id,
                    else_=table.c[column],
                )
                for column in columns
            }
            connection.execute(
                table.update()
                .where(table.c.id.in_([row_id for row_id, _row in batch]))
                .values(**values)
            )


def _delete_rollups(connection, ids):
    for batch in _batches(ids):
        connection.execute(rollup.delete().where(rollup.c.id.in_(batch)))


_POINTER_FIELDS = ("last_read_version_id", "last_read_root_hash", "last_read_at")


def _pointer_time(row):
    return row["last_read_at"] or datetime.min.replace(
        tzinfo=row["last_occurred_at"].tzinfo
    )


def _read_order(row):
    return row["occurred_at"], row["mcp_tool_call_id"], row["id"]


def _rollup_order(row):
    return row["last_occurred_at"], row["last_mcp_tool_call_id"]


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _annotated_reads(connection, source):
    """Persist bounded cursors while streaming the same reads into pair counts."""
    session_id = None
    state = None
    previous_at = None
    positions, cursors = [], []
    for row in source:
        at = _utc(row["occurred_at"])
        if row["chat_session_id"] != session_id:
            if state is not None:
                cursors.append((session_id, {"co_access_state": state}))
            session_id = row["chat_session_id"]
            state = {"version": 1, "segment": 0, "last_key": None, "runs": []}
            previous_at = None
        runs = state["runs"]
        entity = [row["resource_kind"], str(row["resource_id"])]
        if previous_at is not None and at - previous_at > timedelta(minutes=30):
            state["segment"] += 1
            runs = state["runs"] = []
        if runs and runs[-1][:2] == entity:
            runs[-1][3] = at.isoformat()
        else:
            if len(runs) == 12:
                state["segment"] += 1
                runs = state["runs"] = []
            runs.append([*entity, at.isoformat(), at.isoformat()])
        state["last_key"] = [
            at.isoformat(),
            str(row["mcp_tool_call_id"]),
            str(row["id"]),
        ]
        positions.append(
            (
                row["id"],
                {
                    "co_access_segment": state["segment"],
                    "co_access_position": len(runs) - 1,
                },
            )
        )
        previous_at = at
        if len(positions) >= 1000:
            _update_rows(connection, cursor_per_call, positions)
            positions = []
        if len(cursors) >= 500:
            _update_rows(connection, chat_session, cursors)
            cursors = []
        yield Read(
            session_id,
            (row["resource_kind"], row["resource_id"]),
            at,
            row["mcp_tool_call_id"],
            row["id"],
        )
    if state is not None:
        cursors.append((session_id, {"co_access_state": state}))
    _update_rows(connection, cursor_per_call, positions)
    _update_rows(connection, chat_session, cursors)


def _backfill_pairs(connection: Connection) -> None:
    now = datetime.now(UTC)
    for workspace_id, enabled in connection.execute(
        sa.select(workspace.c.id, workspace.c.usage_relation_signals_enabled)
    ):
        source = connection.execute(
            _focused_reads("viewed")
            .where(per_call.c.workspace_id == workspace_id)
            .order_by(
                per_call.c.chat_session_id,
                per_call.c.occurred_at,
                per_call.c.mcp_tool_call_id,
                per_call.c.id,
            )
            .execution_options(yield_per=5_000)
        ).mappings()
        aggregate = {}
        try:
            for pair in co_read_pairs(
                _annotated_reads(connection, source),
                max_gap=timedelta(minutes=30),
                window=5,
                max_per_segment=12,
            ):
                key = (*pair.left, *pair.right)
                value = aggregate.setdefault(
                    key, [0, pair.first_pair_at, pair.last_pair_at]
                )
                value[0] += 1
                value[1] = min(value[1], pair.first_pair_at)
                value[2] = max(value[2], pair.last_pair_at)
        finally:
            source.close()
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
                _utc(row["first_observed_at"]),
                _utc(row["occurred_at"]),
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
            connection.execute(table.insert().values(batch))
            batch = []
    if batch:
        connection.execute(table.insert().values(batch))

"""One-time bounded read cursor backfill for already recorded body views."""

from datetime import UTC, timedelta

import sqlalchemy as sa

from .co_read_v1 import Read
from .lifetime_co_access_v1 import (
    DOCUMENT_KINDS,
    FOCUSED_READ_TOOLS,
    _update_rows,
    per_call,
    tool_call,
)

chat_session = sa.table(
    "mcp_chat_session",
    sa.column("id", sa.Uuid()),
    sa.column("co_access_state", sa.JSON()),
)
cursor_per_call = sa.table(
    "mcp_chat_tool_call_artifact",
    sa.column("id", sa.Uuid()),
    sa.column("co_access_segment", sa.Integer()),
    sa.column("co_access_position", sa.Integer()),
)
workspace = sa.table(
    "workspace",
    sa.column("id", sa.Uuid()),
    sa.column("authority_projection_version", sa.Integer()),
)


def upgrade(connection):
    source = connection.execute(
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
            per_call.c.verb == "viewed",
            per_call.c.resource_kind.in_(DOCUMENT_KINDS),
            tool_call.c.result_status == "succeeded",
            tool_call.c.tool_name.in_(FOCUSED_READ_TOOLS),
        )
        .order_by(
            per_call.c.workspace_id,
            per_call.c.chat_session_id,
            per_call.c.occurred_at,
            per_call.c.mcp_tool_call_id,
            per_call.c.id,
        )
        .execution_options(yield_per=5000)
    ).mappings()
    try:
        for _read in _annotated_reads(connection, source):
            pass
    finally:
        source.close()
    # Existing lifetime projections used unbounded weights in authority. A
    # negative watermark forces the next authority tick onto the new law.
    connection.execute(workspace.update().values(authority_projection_version=-1))


def downgrade(connection):
    # An older runtime must not accept cached authority from the bounded law.
    connection.execute(workspace.update().values(authority_projection_version=-1))


def _annotated_reads(connection, source):
    """Seed bounded append cursors and position provenance during one replay."""
    session_id = None
    state = None
    previous_at = None
    positions, cursors = [], []
    for row in source:
        at = row["occurred_at"]
        at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
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

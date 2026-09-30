"""Reclassify historical MCP search results without inventing read activity."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)
SEARCH_TOOLS = ("search", "grep_wiki")


tool_call = sa.table(
    "mcp_tool_call",
    sa.column("id", sa.Uuid()),
    sa.column("tool_name", sa.Text()),
)
per_call_link = sa.table(
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
rollup_link = sa.table(
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


@dataclass(frozen=True)
class SearchMatchGroup:
    workspace_id: UUID
    chat_session_id: UUID
    resource_kind: str
    resource_id: UUID
    count: int
    first_occurred_at: datetime
    last_occurred_at: datetime
    last_mcp_tool_call_id: UUID

    @property
    def key(self) -> tuple[UUID, UUID, str, UUID]:
        return (
            self.workspace_id,
            self.chat_session_id,
            self.resource_kind,
            self.resource_id,
        )


def upgrade() -> None:
    reclassify_search_matches(op.get_bind())


def downgrade() -> None:
    # Data repair is intentionally forward-only. Conflating exposure with a
    # read again would be less accurate than retaining the new verb.
    pass


def reclassify_search_matches(
    connection: Connection, *, now: datetime | None = None
) -> dict[str, int]:
    """Move exact search-derived counts from referenced to search_match.

    The changed per-call rows are the sole provenance for this repair. If no
    row changes, the function returns before touching rollups or projection
    versions, which makes reruns byte-for-byte idempotent.
    """

    rows = list(
        connection.execute(
            sa.select(
                per_call_link.c.id,
                per_call_link.c.workspace_id,
                per_call_link.c.chat_session_id,
                per_call_link.c.mcp_tool_call_id,
                per_call_link.c.resource_kind,
                per_call_link.c.resource_id,
                per_call_link.c.occurred_at,
            )
            .select_from(
                per_call_link.join(
                    tool_call,
                    per_call_link.c.mcp_tool_call_id == tool_call.c.id,
                )
            )
            .where(
                tool_call.c.tool_name.in_(SEARCH_TOOLS),
                per_call_link.c.verb == "referenced",
            )
        ).mappings()
    )
    _log_unclassified_rollups(connection)
    if not rows:
        return {
            "per_call_links": 0,
            "rollup_groups": 0,
            "workspaces": 0,
            "dismissed_hot_paths": 0,
        }

    groups = _group_search_matches(rows)
    search_derived_link_ids = (
        sa.select(per_call_link.c.id)
        .select_from(
            per_call_link.join(
                tool_call,
                per_call_link.c.mcp_tool_call_id == tool_call.c.id,
            )
        )
        .where(
            tool_call.c.tool_name.in_(SEARCH_TOOLS),
            per_call_link.c.verb == "referenced",
        )
    )
    connection.execute(
        per_call_link.update()
        .where(per_call_link.c.id.in_(search_derived_link_ids))
        .values(verb="search_match")
    )
    for group in groups:
        _move_rollup_count(connection, group)

    changed_workspaces = {group.workspace_id for group in groups}
    timestamp = now or datetime.now(UTC)
    dismissed = 0
    session = Session(bind=connection, join_transaction_mode="rollback_only")
    try:
        # Runtime projection functions are intentionally reused: this phase
        # runs after tenancy_schema, when their complete table contract is
        # present, and must produce exactly the same graph as the next tick.
        from backend.services.indexing.edge_registry import (
            refresh_co_access_edge_projection,
        )
        from backend.services.trail_analysis import (
            recompute_authority,
        )

        for workspace_id in changed_workspaces:
            refresh_co_access_edge_projection(
                session, workspace_id, now=timestamp
            )
            recompute_authority(session, workspace_id, now=timestamp)
            dismissed += _dismiss_invalid_hot_paths(
                session, workspace_id, now=timestamp
            )
        session.flush()
    finally:
        session.close()

    return {
        "per_call_links": len(rows),
        "rollup_groups": len(groups),
        "workspaces": len(changed_workspaces),
        "dismissed_hot_paths": dismissed,
    }


def _group_search_matches(rows) -> list[SearchMatchGroup]:
    grouped: dict[tuple[UUID, UUID, str, UUID], list] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row["workspace_id"],
                row["chat_session_id"],
                row["resource_kind"],
                row["resource_id"],
            )
        ].append(row)
    result: list[SearchMatchGroup] = []
    for key, matches in grouped.items():
        ordered = sorted(
            matches,
            key=lambda row: (row["occurred_at"], str(row["mcp_tool_call_id"])),
        )
        result.append(
            SearchMatchGroup(
                workspace_id=key[0],
                chat_session_id=key[1],
                resource_kind=key[2],
                resource_id=key[3],
                count=len(matches),
                first_occurred_at=ordered[0]["occurred_at"],
                last_occurred_at=ordered[-1]["occurred_at"],
                last_mcp_tool_call_id=ordered[-1]["mcp_tool_call_id"],
            )
        )
    return result


def _move_rollup_count(connection: Connection, group: SearchMatchGroup) -> None:
    identity = (
        rollup_link.c.workspace_id == group.workspace_id,
        rollup_link.c.chat_session_id == group.chat_session_id,
        rollup_link.c.resource_kind == group.resource_kind,
        rollup_link.c.resource_id == group.resource_id,
    )
    referenced = (
        connection.execute(
            sa.select(rollup_link).where(
                *identity,
                rollup_link.c.verb == "referenced",
            )
        )
        .mappings()
        .first()
    )
    search_match = (
        connection.execute(
            sa.select(rollup_link.c.id).where(
                *identity,
                rollup_link.c.verb == "search_match",
            )
        )
        .mappings()
        .first()
    )
    search_values = {
        "occurrence_count": group.count,
        "first_occurred_at": group.first_occurred_at,
        "last_occurred_at": group.last_occurred_at,
        "last_mcp_tool_call_id": group.last_mcp_tool_call_id,
    }
    if search_match is None:
        connection.execute(
            rollup_link.insert().values(
                id=uuid4(),
                workspace_id=group.workspace_id,
                chat_session_id=group.chat_session_id,
                resource_kind=group.resource_kind,
                resource_id=group.resource_id,
                verb="search_match",
                last_read_version_id=None,
                last_read_root_hash=None,
                last_read_at=None,
                **search_values,
            )
        )
    else:
        connection.execute(
            rollup_link.update()
            .where(rollup_link.c.id == search_match["id"])
            .values(**search_values)
        )

    if referenced is None:
        return
    remaining = list(
        connection.execute(
            sa.select(
                per_call_link.c.mcp_tool_call_id,
                per_call_link.c.occurred_at,
            ).where(
                per_call_link.c.workspace_id == group.workspace_id,
                per_call_link.c.chat_session_id == group.chat_session_id,
                per_call_link.c.resource_kind == group.resource_kind,
                per_call_link.c.resource_id == group.resource_id,
                per_call_link.c.verb == "referenced",
            )
        ).mappings()
    )
    floor = max(len(remaining), 1 if referenced["last_read_version_id"] else 0)
    next_count = max(referenced["occurrence_count"] - group.count, floor)
    if next_count <= 0:
        connection.execute(
            rollup_link.delete().where(rollup_link.c.id == referenced["id"])
        )
        return
    values: dict[str, object] = {"occurrence_count": next_count}
    if remaining:
        latest = max(
            remaining,
            key=lambda row: (row["occurred_at"], str(row["mcp_tool_call_id"])),
        )
        values.update(
            last_occurred_at=latest["occurred_at"],
            last_mcp_tool_call_id=latest["mcp_tool_call_id"],
        )
    connection.execute(
        rollup_link.update()
        .where(rollup_link.c.id == referenced["id"])
        .values(**values)
    )


def _dismiss_invalid_hot_paths(
    session: Session, workspace_id: UUID, *, now: datetime
) -> int:
    from backend.models.lint import LintFinding
    from backend.services.trail_analysis import (
        HOT_PATH_MIN_SESSIONS,
        _context_pair_session_counts,
    )

    valid_pairs = {
        frozenset(pair)
        for pair, count in _context_pair_session_counts(
            session, workspace_id, now=now
        ).items()
        if count >= HOT_PATH_MIN_SESSIONS
    }
    dismissed = 0
    findings = session.scalars(
        sa.select(LintFinding).where(
            LintFinding.workspace_id == workspace_id,
            LintFinding.rule_id == "kb.hot_path_unmaterialized",
            LintFinding.status.in_(("open", "acknowledged")),
        )
    ).all()
    for finding in findings:
        pair = _hot_path_pair(finding.fingerprint)
        if pair is None or pair in valid_pairs:
            continue
        try:
            action = json.loads(finding.suggested_action or "{}")
        except (TypeError, ValueError):
            action = {}
        if not isinstance(action, dict):
            action = {}
        action["dismissed_by"] = "search_match_reclassification"
        finding.suggested_action = json.dumps(action, sort_keys=True)
        finding.status = "dismissed"
        finding.resolved_at = now
        dismissed += 1
    return dismissed


def _hot_path_pair(fingerprint: str) -> frozenset[UUID] | None:
    prefix = "hot-path:"
    if not fingerprint.startswith(prefix):
        return None
    parts = fingerprint[len(prefix) :].split(":")
    if len(parts) != 2:
        return None
    try:
        return frozenset((UUID(parts[0]), UUID(parts[1])))
    except ValueError:
        return None


def _log_unclassified_rollups(connection: Connection) -> None:
    per_call_exists = sa.exists(
        sa.select(1).where(
            per_call_link.c.workspace_id == rollup_link.c.workspace_id,
            per_call_link.c.chat_session_id == rollup_link.c.chat_session_id,
            per_call_link.c.resource_kind == rollup_link.c.resource_kind,
            per_call_link.c.resource_id == rollup_link.c.resource_id,
            per_call_link.c.verb == "referenced",
        )
    )
    count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(rollup_link)
        .where(rollup_link.c.verb == "referenced", ~per_call_exists)
    )
    if count:
        LOGGER.info(
            "left %d referenced MCP chat rollups unchanged because no per-call "
            "provenance exists",
            count,
        )

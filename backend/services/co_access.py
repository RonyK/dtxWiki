"""Lifetime co-view evidence with bounded append work and exact late recovery.

Raw views carry their segment/position; the conversation keeps only twelve
active runs. Aggregate pairs contain counts/dates, never conversation IDs.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, case, select, tuple_
from sqlalchemy.orm import Session

from backend.models.graph import CoAccessPair
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    select_in_workspace,
    update_in_workspace,
)
from backend.services.graph_cache import (
    bump_edge_version,
    lock_edge_projection_workspace,
    try_edge_projection_reader,
)
from backend.services.indexing.co_read import EntityRef, Read
from backend.services.indexing.co_read_policy import (
    CO_ACCESS_ADJACENCY_WINDOW,
    CO_ACCESS_MIN_SESSIONS,
    co_access_pairs,
)
from backend.services.indexing.co_read_source import (
    co_read_rows,
    co_read_source,
    co_read_statement,
)
from backend.services.indexing.co_read_state import advance_co_read_state
from backend.services.indexing.edge_projection import (
    EdgeProjectionScope,
    build_edge_row,
    reconcile_edge_projection_rows,
)

PairKey = tuple[EntityRef, EntityRef]


class CoAccessRepairRequired(RuntimeError):
    """Pending history needs worker recovery rather than inline read work."""


def lock_co_access_workspace(session: Session, workspace_id: UUID) -> None:
    """Administrative projection/recovery lock; never taken for ordinary views."""
    lock_edge_projection_workspace(session, workspace_id)


def _identity(pair: PairKey):
    return (*pair[0], *pair[1])


def _scope(model, keys: Iterable[PairKey]):
    return tuple_(
        model.source_kind, model.source_id, model.target_kind, model.target_id
    ).in_([_identity(k) for k in keys])


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


def _key(row):
    return row.occurred_at, row.mcp_tool_call_id, row.id


def _read(row):
    return Read(
        row.chat_session_id,
        (row.resource_kind, row.resource_id),
        row.occurred_at,
        row.mcp_tool_call_id,
        row.id,
    )


def _annotate_locked_reads(rows, chats):
    """Yield only evidence annotated for the captured, locked conversation set.

    A workspace lock does not prevent a new conversation's first view. Its
    rows may appear in a later source snapshot, so never consume an unlocked
    conversation for either cursor state or aggregate support.
    """
    states = {}
    for row in rows:
        if row.chat_session_id not in chats:
            continue
        read = _read(row)
        state, _, row.co_access_segment, row.co_access_position = advance_co_read_state(
            states.get(row.chat_session_id), read
        )
        states[row.chat_session_id] = state
        yield read
    for chat_id, chat in chats.items():
        chat.co_access_state = states.get(chat_id)


def _has_other_pending_read(session, workspace_id, chat_id, row, *, after_key=None):
    return (
        session.scalar(
            co_read_statement(
                workspace_id,
                chat_session_id=chat_id,
                after_key=after_key,
                conditions=(McpChatToolCallArtifact.id != row.id,),
            )
            .with_only_columns(McpChatToolCallArtifact.id)
            .limit(1)
        )
        is not None
    )


def _conversation_has_pair(session, workspace_id, chat_id, key):
    # Source-level ordinal metadata makes this indexed existence check exact
    # even when this pair was already seen in an earlier closed segment.
    def endpoint(entity):
        return (
            co_read_statement(
                workspace_id,
                chat_session_id=chat_id,
                conditions=(
                    McpChatToolCallArtifact.resource_kind == entity[0],
                    McpChatToolCallArtifact.resource_id == entity[1],
                    McpChatToolCallArtifact.co_access_segment.is_not(None),
                ),
            )
            .with_only_columns(
                McpChatToolCallArtifact.id,
                McpChatToolCallArtifact.co_access_segment.label("segment"),
                McpChatToolCallArtifact.co_access_position.label("position"),
            )
            .subquery()
        )

    left, right = endpoint(key[0]), endpoint(key[1])
    gap = CO_ACCESS_ADJACENCY_WINDOW - 1
    match = and_(
        left.c.segment == right.c.segment,
        left.c.position != right.c.position,
        left.c.position.between(right.c.position - gap, right.c.position + gap),
    )
    statement = select(left.c.id).select_from(left.join(right, match)).limit(1)
    return session.scalar(statement) is not None


def _upsert_pair(session, workspace_id, key, *, delta, first, last):
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    values = dict(
        id=uuid4(),
        workspace_id=workspace_id,
        source_kind=key[0][0],
        source_id=key[0][1],
        target_kind=key[1][0],
        target_id=key[1][1],
        support_session_count=1,
        first_observed_at=first,
        occurred_at=last,
    )
    statement = insert(CoAccessPair).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[
            CoAccessPair.workspace_id,
            CoAccessPair.source_kind,
            CoAccessPair.source_id,
            CoAccessPair.target_kind,
            CoAccessPair.target_id,
        ],
        set_={
            "support_session_count": CoAccessPair.support_session_count + delta,
            "first_observed_at": case(
                (CoAccessPair.first_observed_at > first, first),
                else_=CoAccessPair.first_observed_at,
            ),
            "occurred_at": case(
                (CoAccessPair.occurred_at < last, last), else_=CoAccessPair.occurred_at
            ),
        },
    )
    session.execute(statement)


def _recount_pair(session, workspace_id, key):
    """Rare late-delivery repair, scoped to conversations containing both ends."""

    def conversations(entity):
        return co_read_statement(
            workspace_id,
            conditions=(
                McpChatToolCallArtifact.resource_kind == entity[0],
                McpChatToolCallArtifact.resource_id == entity[1],
                McpChatToolCallArtifact.co_access_segment.is_not(None),
            ),
        ).with_only_columns(McpChatToolCallArtifact.chat_session_id)

    ids = session.scalars(conversations(key[0]).intersect(conversations(key[1]))).all()
    count, first, last = 0, None, None
    for chat_id in ids:
        for pair in co_access_pairs(
            co_read_source(
                session,
                workspace_id,
                chat_session_id=chat_id,
                conditions=(McpChatToolCallArtifact.co_access_segment.is_not(None),),
            )
        ):
            if (pair.left, pair.right) == key:
                count += 1
                first = min(first, pair.first_pair_at) if first else pair.first_pair_at
                last = max(last, pair.last_pair_at) if last else pair.last_pair_at
                break
    return count, first, last


def _replay_conversation(session, workspace_id, chat):
    """Rebuild only a cold/late conversation, then repair its affected pairs."""
    source = co_read_rows(session, workspace_id, chat_session_id=chat.id)
    try:
        rows = list(source)
    finally:
        source.close()
    before = {
        (p.left, p.right): (p.first_pair_at, p.last_pair_at)
        for p in co_access_pairs(
            _read(row) for row in rows if row.co_access_segment is not None
        )
    }
    with session.no_autoflush:
        after = {
            (p.left, p.right): (p.first_pair_at, p.last_pair_at)
            for p in co_access_pairs(_annotate_locked_reads(rows, {chat.id: chat}))
        }
        keys = {
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }
        if keys:
            try_edge_projection_reader(session, workspace_id)
    session.flush()
    for key in sorted(keys):
        # Obtain/create the canonical pair lock before querying contributors.
        stamp = rows[-1].occurred_at
        _upsert_pair(session, workspace_id, key, delta=0, first=stamp, last=stamp)
        count, first, last = _recount_pair(session, workspace_id, key)
        if count:
            session.execute(
                update_in_workspace(
                    CoAccessPair,
                    workspace_id=workspace_id,
                    conditions=(_scope(CoAccessPair, [key]),),
                    values=dict(
                        support_session_count=count,
                        first_observed_at=first,
                        occurred_at=last,
                    ),
                )
            )
        else:
            session.execute(
                delete_in_workspace(
                    CoAccessPair,
                    workspace_id=workspace_id,
                    conditions=(_scope(CoAccessPair, [key]),),
                )
            )
    return keys


def record_co_access_read(
    session: Session,
    *,
    workspace_id: UUID,
    chat_session_id: UUID,
    artifact_id: UUID,
    allow_replay: bool = False,
) -> None:
    """Append one view inline; workers may recover deferred history explicitly."""
    chat = session.scalar(
        select_in_workspace(
            McpChatSession,
            workspace_id=workspace_id,
            conditions=(McpChatSession.id == chat_session_id,),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = session.scalar(
        co_read_statement(
            workspace_id,
            chat_session_id=chat_session_id,
            conditions=(McpChatToolCallArtifact.id == artifact_id,),
        ).execution_options(populate_existing=True)
    )
    if chat is None or row is None or row.co_access_segment is not None:
        return
    state = chat.co_access_state
    if state is None:
        if not _has_other_pending_read(session, workspace_id, chat.id, row):
            state, _, row.co_access_segment, row.co_access_position = (
                advance_co_read_state(None, _read(row))
            )
            chat.co_access_state = state
            session.flush()
            return
        if not allow_replay:
            raise CoAccessRepairRequired("cold conversation has pending history")
        changed = _replay_conversation(session, workspace_id, chat)
    else:
        cursor = state["last_key"]
        cursor_key = (
            datetime.fromisoformat(cursor[0]),
            UUID(cursor[1]),
            UUID(cursor[2]),
        )
        if _key(row) <= cursor_key:
            if not allow_replay:
                raise CoAccessRepairRequired("view predates the conversation cursor")
            changed = _replay_conversation(session, workspace_id, chat)
        else:
            if not allow_replay and _has_other_pending_read(
                session, workspace_id, chat.id, row, after_key=cursor_key
            ):
                raise CoAccessRepairRequired("conversation has a pending view backlog")
            changed = set()
            pending = (
                co_read_rows(
                    session, workspace_id, chat_session_id=chat.id, after_key=cursor_key
                )
                if allow_replay
                else (row,)
            )
            try:
                for event in pending:
                    updated, spans, segment, position = advance_co_read_state(
                        state, _read(event)
                    )
                    if spans:
                        try_edge_projection_reader(session, workspace_id)
                    seen = {
                        key: _conversation_has_pair(session, workspace_id, chat.id, key)
                        for key in spans
                    }
                    # Check prior support before annotating this event.
                    event.co_access_segment, event.co_access_position = (
                        segment,
                        position,
                    )
                    for key in sorted(spans):
                        first, last = spans[key]
                        _upsert_pair(
                            session,
                            workspace_id,
                            key,
                            delta=int(not seen[key]),
                            first=first,
                            last=last,
                        )
                    changed.update(spans)
                    state = updated
                    chat.co_access_state = state
                session.flush()
            finally:
                if allow_replay:
                    pending.close()
    if changed:
        sync_co_access_projection(
            session,
            workspace_id,
            keys=changed,
            usage_signals_enabled=_usage_enabled(session, workspace_id),
            incremental=True,
        )


def _projection_row(pair, workspace_id):
    from backend.services.entity_ref import EntityKind

    return build_edge_row(
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


def _pair_statement(workspace_id, keys=None):
    return select_in_workspace(
        CoAccessPair,
        workspace_id=workspace_id,
        conditions=(
            CoAccessPair.support_session_count >= CO_ACCESS_MIN_SESSIONS,
            *((_scope(CoAccessPair, keys),) if keys is not None else ()),
        ),
    ).execution_options(populate_existing=True)


def sync_co_access_projection(
    session: Session,
    workspace_id: UUID,
    *,
    usage_signals_enabled: bool,
    keys=None,
    incremental: bool = False,
) -> int:
    from backend.models.context import EdgeProjection

    if not incremental:
        lock_co_access_workspace(session, workspace_id)
    edges = select_in_workspace(
        EdgeProjection,
        workspace_id=workspace_id,
        conditions=(
            EdgeProjection.edge_kind == "co_access",
            *((_scope(EdgeProjection, keys),) if keys is not None else ()),
        ),
    )
    if incremental:
        edges = edges.with_for_update(nowait=True)
    existing = list(session.scalars(edges.execution_options(populate_existing=True)))
    desired = (
        [
            _projection_row(pair, workspace_id)
            for pair in session.scalars(_pair_statement(workspace_id, keys))
        ]
        if usage_signals_enabled
        else []
    )

    def rank_values(row):
        return (
            row["support_session_count"],
            row["weight"],
            row["provenance"],
            row["source_ref"],
        )

    def identity(row):
        return (
            row["source_kind"],
            row["source_id"],
            row["target_kind"],
            row["target_id"],
        )

    before = {identity(vars(edge)): rank_values(vars(edge)) for edge in existing}
    after = {identity(row): rank_values(row) for row in desired}
    rank_changed = before != after
    if incremental and rank_changed:
        lock_edge_projection_workspace(session, workspace_id, nowait=True)
    reconcile_edge_projection_rows(
        session,
        scope=EdgeProjectionScope(workspace_id, frozenset({"co_access"})),
        existing_rows=existing,
        desired_rows=desired,
    )
    # Dates remain fresh for evidence display, but no recency rank is enabled.
    # Re-reading in one conversation cannot invalidate topology/PPR caches.
    if rank_changed:
        bump_edge_version(session, workspace_id)
    return len(desired)


def co_access_projection_needs_repair(session, workspace_id):
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
    session, workspace_id, *, usage_signals_enabled=True, sync_projection=True
):
    """Rebuild counters and cursors from one source snapshot during reindex."""
    lock_co_access_workspace(session, workspace_id)
    chats = {
        chat.id: chat
        for chat in session.scalars(
            select_in_workspace(McpChatSession, workspace_id=workspace_id)
            .order_by(McpChatSession.id)
            .with_for_update()
        )
    }
    aggregates = {}
    rows = co_read_rows(session, workspace_id)
    try:
        for pair in co_access_pairs(_annotate_locked_reads(rows, chats)):
            key = (pair.left, pair.right)
            value = aggregates.setdefault(
                key, [0, pair.first_pair_at, pair.last_pair_at]
            )
            value[0] += 1
            value[1] = min(value[1], pair.first_pair_at)
            value[2] = max(value[2], pair.last_pair_at)
        session.flush()
    finally:
        rows.close()
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
    if sync_projection:
        return sync_co_access_projection(
            session, workspace_id, usage_signals_enabled=usage_signals_enabled
        )
    return sum(
        1
        for count, _, _ in aggregates.values()
        if usage_signals_enabled and count >= CO_ACCESS_MIN_SESSIONS
    )


def co_access_projection_rows(session, workspace_id):
    """One shared pair-to-edge producer for maintenance and full reindex."""
    return [
        _projection_row(pair, workspace_id)
        for pair in session.scalars(_pair_statement(workspace_id))
    ]

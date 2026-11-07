"""Sessions in Graph View activity history: one contract, one source per kind.

A session is one conversation that touched documents: an external MCP chat or
an Ask session. Each source reduces its own records to ``SessionActivity``, so
the graph draws every kind with one builder: one node per session and one
relation per (session, document), named by the strongest thing the session
did to that document (``SESSION_ACTION_ORDER``) and carrying its count and
time window. Supporting another kind of session means adding a source to
``SESSION_ACTIVITY_SOURCES``; the graph builder does not change.

A source contributes only for an actor who can open that kind of session on
its own screen, so the graph never names a session its reader cannot open.

Every source answers one request with a fixed number of statements, however
many sessions it returns.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Integer, and_, case, cast, func, literal_column, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.models.context import ContextPageVersion
from backend.models.mcp_chat import (
    AUTHORING_VERBS,
    CONSUMPTION_VERBS,
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import _role_allows
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    select_projection_in_workspace,
)
from backend.schemas.graph import GraphQueryRequest
from backend.schemas.mcp_chat import McpChatPresentationDescriptor
from backend.services.actor_visible_read import actor_can_read_audit_records
from backend.services.graph_semantics import (
    NODE_FAMILY_BY_KIND,
    SESSION_ACTION_ORDER,
    session_action_rank,
)
from backend.services.graph_service import HYDRATORS
from backend.utils.actor_display import workspace_plane_actor_display_names

# The most recently active sessions per seed are collected; older ones fold
# into one group node per seed. This bounds the work of one request, not the
# drawing: the scene budget decides how much of what is collected is shown.
SESSIONS_PER_SEED = 100
# Per session, the other visible documents it used, strongest first, then newest.
# Applied by the scene builder after hydration checks actor visibility.
CO_USED_PER_SESSION = 20
# An uploaded image, for one, has no graph node and cannot be a candidate.
_DRAWABLE_KINDS = frozenset(HYDRATORS) & frozenset(NODE_FAMILY_BY_KIND) - {
    "workspace_tree_node"
}


@dataclass(frozen=True)
class SessionResourceActivity:
    """What one session did to one document."""

    resource_kind: str
    resource_id: UUID
    # (graph action kind, occurrences), strongest first.
    counts: tuple[tuple[str, int], ...]
    first_at: datetime
    last_at: datetime

    @property
    def action_kind(self) -> str:
        return self.counts[0][0]

    @property
    def count(self) -> int:
        return sum(occurrences for _kind, occurrences in self.counts)


@dataclass(frozen=True)
class SessionActivity:
    entity_kind: str
    entity_id: UUID
    # Language-neutral name; ``presentation`` lets the client localize it.
    label: str
    status: str
    actor_label: str | None
    presentation: McpChatPresentationDescriptor | None
    # The seeds this session touched, and only those it is collected for.
    seeds: tuple[SessionResourceActivity, ...]
    co_used: tuple[SessionResourceActivity, ...]
    # Context versions of a seed that this session's writes produced.
    authored_versions: tuple[UUID, ...] = ()
    # A person behind actor_label, whose name only the identity plane holds.
    actor_ref: tuple[str, UUID | None] | None = None


@dataclass(frozen=True)
class SessionGroup:
    """The sessions of one kind past ``SESSIONS_PER_SEED`` for one seed."""

    entity_kind: str
    seed_id: UUID
    count: int
    action_kind: str
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class SessionActivityBatch:
    sessions: tuple[SessionActivity, ...] = ()
    groups: tuple[SessionGroup, ...] = ()


@dataclass(frozen=True)
class SessionAccess:
    """Session screens whose opening is decided outside the actor (Labs)."""

    ask_sessions: bool = False


class SessionActivitySource(Protocol):
    entity_kind: str
    group_kind: str

    def collect(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seed_ids: Sequence[UUID],
        request: GraphQueryRequest,
        actor: ActorContext | None,
        access: SessionAccess,
    ) -> SessionActivityBatch: ...


@dataclass(frozen=True)
class _Kept:
    seed_id: UUID
    session_id: UUID


def _rank_sessions(
    session: Session,
    touches: Select,
    *,
    group_kind: str,
) -> tuple[list[_Kept], list[SessionGroup]]:
    """Keep each seed's newest sessions; fold the rest into one group per seed.

    ``touches`` has one row per (seed_id, session_id) with ``strength`` (a
    ``session_action_rank``), ``first_at`` and ``last_at``.
    """

    touched = touches.cte("session_touches")
    ranked = select(
        touched.c.seed_id,
        touched.c.session_id,
        touched.c.strength,
        touched.c.first_at,
        touched.c.last_at,
        func.row_number()
        .over(
            partition_by=touched.c.seed_id,
            order_by=(touched.c.last_at.desc(), touched.c.session_id.desc()),
        )
        .label("position"),
    ).cte("ranked_session_touches")
    kept = [
        _Kept(seed_id=row.seed_id, session_id=row.session_id)
        for row in session.execute(
            select(ranked.c.seed_id, ranked.c.session_id)
            .where(ranked.c.position <= SESSIONS_PER_SEED)
            .order_by(ranked.c.seed_id, ranked.c.position)
        )
    ]
    groups = [
        SessionGroup(
            entity_kind=group_kind,
            seed_id=row.seed_id,
            count=row.count,
            action_kind=SESSION_ACTION_ORDER[
                min(row.strength, len(SESSION_ACTION_ORDER) - 1)
            ],
            first_at=row.first_at,
            last_at=row.last_at,
        )
        for row in session.execute(
            select(
                ranked.c.seed_id,
                func.count().label("count"),
                func.min(ranked.c.strength).label("strength"),
                func.min(ranked.c.first_at).label("first_at"),
                func.max(ranked.c.last_at).label("last_at"),
            )
            .where(ranked.c.position > SESSIONS_PER_SEED)
            .group_by(ranked.c.seed_id)
            .order_by(ranked.c.seed_id)
        )
    ]
    return kept, groups


def _window(column, request: GraphQueryRequest) -> tuple:
    """The request's time window as predicates on ``column``."""

    predicates = []
    if request.time.from_ is not None:
        predicates.append(column >= request.time.from_)
    if request.time.to is not None:
        predicates.append(column <= request.time.to)
    return tuple(predicates)


def _overlapping_window(start_column, end_column, request: GraphQueryRequest) -> tuple:
    """Predicates for intervals that overlap the requested time window."""

    predicates = []
    if request.time.from_ is not None:
        predicates.append(end_column >= request.time.from_)
    if request.time.to is not None:
        predicates.append(start_column <= request.time.to)
    return tuple(predicates)


@dataclass
class _ResourceTally:
    counts: dict[str, int]
    first_at: datetime
    last_at: datetime

    def add(self, action_kind: str, count: int, first: datetime, last: datetime):
        self.counts[action_kind] = self.counts.get(action_kind, 0) + count
        self.first_at = min(self.first_at, first)
        self.last_at = max(self.last_at, last)


def _tally(
    rows: Iterable[tuple[str, UUID, str, int, datetime, datetime]],
) -> dict[tuple[str, UUID], SessionResourceActivity]:
    """Fold (kind, id, action, count, first, last) rows into one per document."""

    tallies: dict[tuple[str, UUID], _ResourceTally] = {}
    for kind, resource_id, action_kind, count, first, last in rows:
        tally = tallies.get((kind, resource_id))
        if tally is None:
            tallies[(kind, resource_id)] = _ResourceTally(
                {action_kind: count}, first, last
            )
        else:
            tally.add(action_kind, count, first, last)
    return {
        key: SessionResourceActivity(
            resource_kind=key[0],
            resource_id=key[1],
            counts=tuple(
                sorted(
                    tally.counts.items(),
                    key=lambda item: (session_action_rank(item[0]), item[0]),
                )
            ),
            first_at=tally.first_at,
            last_at=tally.last_at,
        )
        for key, tally in tallies.items()
    }


def _split(
    resources: dict[tuple[str, UUID], SessionResourceActivity],
    *,
    kept_seed_ids: set[UUID],
    all_seed_ids: set[UUID],
) -> tuple[tuple[SessionResourceActivity, ...], tuple[SessionResourceActivity, ...]]:
    seeds = tuple(
        activity
        for (kind, resource_id), activity in sorted(
            resources.items(), key=lambda item: str(item[0][1])
        )
        if kind == "context_page" and resource_id in kept_seed_ids
    )
    others = sorted(
        (
            activity
            for (kind, resource_id), activity in resources.items()
            if kind in _DRAWABLE_KINDS
            and not (kind == "context_page" and resource_id in all_seed_ids)
        ),
        key=lambda activity: (
            session_action_rank(activity.action_kind),
            -activity.last_at.timestamp(),
            activity.resource_kind,
            str(activity.resource_id),
        ),
    )
    return seeds, tuple(others)


def _kept_by_session(kept: list[_Kept]) -> dict[UUID, set[UUID]]:
    by_session: dict[UUID, set[UUID]] = defaultdict(set)
    for row in kept:
        by_session[row.session_id].add(row.seed_id)
    return by_session


class McpChatActivitySource:
    """External MCP chats, from the chat-by-artifact rollup.

    Chats are an editor surface whose rows come from the audit trail, so the
    source follows the Chats screen: editors with audit read only.
    The time window selects whole conversations by their recorded activity
    interval; their document relations retain every occurrence in the chat.
    """

    entity_kind = "mcp_chat_session"
    group_kind = "mcp_chat_session_group"
    # Search matches only exposed a document; they never count as using it.
    _VERBS = tuple(sorted(str(verb) for verb in CONSUMPTION_VERBS | AUTHORING_VERBS))

    def collect(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seed_ids: Sequence[UUID],
        request: GraphQueryRequest,
        actor: ActorContext | None,
        access: SessionAccess,
    ) -> SessionActivityBatch:
        if not _mcp_chats_visible(actor):
            return SessionActivityBatch()
        from backend.services.mcp_chat_service import McpChatService

        overlapping_chats = select_projection_in_workspace(
            McpChatSession,
            workspace_id=workspace_id,
            columns=(McpChatSession.id,),
            predicates=_overlapping_window(
                McpChatSession.started_at, McpChatSession.last_activity_at, request
            ),
        )
        link = McpChatArtifactLink
        seed_links = select_projection_in_workspace(
            link,
            workspace_id=workspace_id,
            columns=(
                link.resource_id,
                link.chat_session_id,
                link.verb,
                link.first_occurred_at,
                link.last_occurred_at,
            ),
            predicates=(
                link.resource_kind == "context_page",
                link.resource_id.in_(seed_ids),
                link.verb.in_(self._VERBS),
                link.chat_session_id.in_(overlapping_chats),
            ),
        ).cte("seed_chat_links")
        # Typed, so every dialect compares ranks as integers, not as text.
        strength = cast(
            case(
                {verb: session_action_rank(verb) for verb in self._VERBS},
                value=seed_links.c.verb,
                else_=len(SESSION_ACTION_ORDER),
            ),
            Integer,
        )
        touches = select(
            seed_links.c.resource_id.label("seed_id"),
            seed_links.c.chat_session_id.label("session_id"),
            func.min(strength).label("strength"),
            func.min(seed_links.c.first_occurred_at).label("first_at"),
            func.max(seed_links.c.last_occurred_at).label("last_at"),
        ).group_by(seed_links.c.resource_id, seed_links.c.chat_session_id)
        kept, groups = _rank_sessions(session, touches, group_kind=self.group_kind)
        if not kept:
            return SessionActivityBatch(groups=tuple(groups))
        kept_seeds = _kept_by_session(kept)
        chats = session.scalars(
            select_in_workspace(
                McpChatSession,
                workspace_id=workspace_id,
                conditions=(McpChatSession.id.in_(list(kept_seeds)),),
            ).order_by(McpChatSession.last_activity_at.desc(), McpChatSession.id)
        ).all()
        cards = McpChatService().chat_cards(session, workspace_id, list(chats), actor)
        versions = self._authored_versions(
            session, workspace_id, chat_ids=list(kept_seeds), seed_ids=seed_ids
        )
        verbs = set(self._VERBS)
        all_seed_ids = set(seed_ids)
        sessions = []
        for card in cards:
            resources = _tally(
                (
                    row.resource_kind,
                    row.resource_id,
                    row.verb,
                    row.occurrence_count,
                    row.first_occurred_at,
                    row.last_occurred_at,
                )
                for row in card.links
                if row.verb in verbs
            )
            seeds, co_used = _split(
                resources,
                kept_seed_ids=kept_seeds[card.chat.id],
                all_seed_ids=all_seed_ids,
            )
            if not seeds:
                continue
            sessions.append(
                SessionActivity(
                    entity_kind=self.entity_kind,
                    entity_id=card.chat.id,
                    label=card.title,
                    status="archived"
                    if card.chat.archived_at is not None
                    else "active",
                    actor_label=card.client_name,
                    presentation=card.presentation,
                    seeds=seeds,
                    co_used=co_used,
                    authored_versions=versions.get(card.chat.id, ()),
                )
            )
        return SessionActivityBatch(sessions=tuple(sessions), groups=tuple(groups))

    @staticmethod
    def _authored_versions(
        session: Session,
        workspace_id: UUID,
        *,
        chat_ids: list[UUID],
        seed_ids: Sequence[UUID],
    ) -> dict[UUID, tuple[UUID, ...]]:
        """Seed versions a chat's writes produced, by the write's change set.

        A write call records the change set it opened; the version it saved
        carries the same change set. ChangeSet.origin_chat_session_id is not
        used: most edit change sets leave it empty.
        """

        artifact = McpChatToolCallArtifact
        writes = select_projection_in_workspace(
            artifact,
            workspace_id=workspace_id,
            columns=(
                artifact.chat_session_id,
                artifact.mcp_tool_call_id,
                artifact.resource_id,
            ),
            predicates=(
                artifact.chat_session_id.in_(chat_ids),
                artifact.resource_kind == "context_page",
                artifact.resource_id.in_(seed_ids),
                artifact.verb.in_(tuple(str(verb) for verb in AUTHORING_VERBS)),
            ),
        ).cte("chat_seed_writes")
        change_sets = select_projection_in_workspace(
            MCPToolCall,
            workspace_id=workspace_id,
            columns=(MCPToolCall.id, MCPToolCall.change_set_id),
            predicates=(
                MCPToolCall.id.in_(select(writes.c.mcp_tool_call_id)),
                MCPToolCall.change_set_id.is_not(None),
            ),
        ).cte("chat_write_change_sets")
        produced = select_projection_in_workspace(
            ContextPageVersion,
            workspace_id=workspace_id,
            columns=(
                ContextPageVersion.id,
                ContextPageVersion.page_id,
                ContextPageVersion.change_set_id,
            ),
            predicates=(
                ContextPageVersion.page_id.in_(seed_ids),
                ContextPageVersion.change_set_id.in_(
                    select(change_sets.c.change_set_id)
                ),
            ),
        ).cte("chat_produced_versions")
        rows = session.execute(
            select(writes.c.chat_session_id, produced.c.id)
            .select_from(writes)
            .join(change_sets, change_sets.c.id == writes.c.mcp_tool_call_id)
            .join(
                produced,
                and_(
                    produced.c.change_set_id == change_sets.c.change_set_id,
                    produced.c.page_id == writes.c.resource_id,
                ),
            )
            .distinct()
            .order_by(writes.c.chat_session_id, produced.c.id)
        ).all()
        versions: dict[UUID, list[UUID]] = defaultdict(list)
        for chat_id, version_id in rows:
            versions[chat_id].append(version_id)
        return {chat_id: tuple(ids) for chat_id, ids in versions.items()}


class AskSessionActivitySource:
    """Ask sessions, from the documents their answers included as evidence.

    Ask is a Labs experiment opened by workspace owners only, so a session
    and its title appear only where that screen can be opened.
    """

    entity_kind = "ask_session"
    group_kind = "ask_session_group"
    _ACTION = "included"

    def collect(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seed_ids: Sequence[UUID],
        request: GraphQueryRequest,
        actor: ActorContext | None,
        access: SessionAccess,
    ) -> SessionActivityBatch:
        if not access.ask_sessions:
            return SessionActivityBatch()
        citation = AskTurnCitation
        turns = select_projection_in_workspace(
            AskTurn,
            workspace_id=workspace_id,
            columns=(AskTurn.id, AskTurn.ask_session_id),
        ).cte("ask_turns")
        seed_citations = select_projection_in_workspace(
            citation,
            workspace_id=workspace_id,
            columns=(citation.ask_turn_id, citation.target_id, citation.created_at),
            predicates=(
                citation.target_kind == "context_page",
                citation.target_id.in_(seed_ids),
                *_window(citation.created_at, request),
            ),
        ).cte("ask_seed_citations")
        touches = (
            select(
                seed_citations.c.target_id.label("seed_id"),
                turns.c.ask_session_id.label("session_id"),
                func.min(
                    literal_column(str(session_action_rank(self._ACTION)), Integer)
                ).label("strength"),
                func.min(seed_citations.c.created_at).label("first_at"),
                func.max(seed_citations.c.created_at).label("last_at"),
            )
            .select_from(seed_citations)
            .join(turns, turns.c.id == seed_citations.c.ask_turn_id)
            .group_by(seed_citations.c.target_id, turns.c.ask_session_id)
        )
        kept, groups = _rank_sessions(session, touches, group_kind=self.group_kind)
        if not kept:
            return SessionActivityBatch(groups=tuple(groups))
        kept_seeds = _kept_by_session(kept)
        ask_sessions = session.scalars(
            select_in_workspace(
                AskSession,
                workspace_id=workspace_id,
                conditions=(AskSession.id.in_(list(kept_seeds)),),
            ).order_by(AskSession.started_at.desc(), AskSession.id)
        ).all()
        session_turns = select_projection_in_workspace(
            AskTurn,
            workspace_id=workspace_id,
            columns=(AskTurn.id, AskTurn.ask_session_id),
            predicates=(AskTurn.ask_session_id.in_(list(kept_seeds)),),
        ).cte("kept_ask_turns")
        session_citations = select_projection_in_workspace(
            citation,
            workspace_id=workspace_id,
            columns=(
                citation.ask_turn_id,
                citation.target_kind,
                citation.target_id,
                citation.created_at,
            ),
            predicates=(
                citation.ask_turn_id.in_(select(session_turns.c.id)),
                *_window(citation.created_at, request),
            ),
        ).cte("kept_ask_citations")
        cited = (
            select(
                session_turns.c.ask_session_id,
                session_citations.c.target_kind,
                session_citations.c.target_id,
                func.count().label("count"),
                func.min(session_citations.c.created_at).label("first_at"),
                func.max(session_citations.c.created_at).label("last_at"),
            )
            .select_from(session_citations)
            .join(session_turns, session_turns.c.id == session_citations.c.ask_turn_id)
            .group_by(
                session_turns.c.ask_session_id,
                session_citations.c.target_kind,
                session_citations.c.target_id,
            )
        )
        rows_by_session: dict[UUID, list] = defaultdict(list)
        for row in session.execute(cited):
            rows_by_session[row.ask_session_id].append(
                (
                    row.target_kind,
                    row.target_id,
                    self._ACTION,
                    row.count,
                    row.first_at,
                    row.last_at,
                )
            )
        # A person's name lives on the identity plane: this read writes the
        # workspace-plane label and the route fills the name in afterwards.
        actor_labels = workspace_plane_actor_display_names(
            (ask.actor_kind, ask.actor_id) for ask in ask_sessions
        )
        all_seed_ids = set(seed_ids)
        sessions = []
        for ask in ask_sessions:
            seeds, co_used = _split(
                _tally(rows_by_session.get(ask.id, ())),
                kept_seed_ids=kept_seeds[ask.id],
                all_seed_ids=all_seed_ids,
            )
            if not seeds:
                continue
            sessions.append(
                SessionActivity(
                    entity_kind=self.entity_kind,
                    entity_id=ask.id,
                    label=(ask.title or "").strip()[:200] or "Ask",
                    status="ended" if ask.ended_at is not None else "active",
                    actor_label=actor_labels[(ask.actor_kind, ask.actor_id)],
                    actor_ref=(ask.actor_kind, ask.actor_id),
                    presentation=None,
                    seeds=seeds,
                    co_used=co_used,
                )
            )
        return SessionActivityBatch(sessions=tuple(sessions), groups=tuple(groups))


def _mcp_chats_visible(actor: ActorContext | None) -> bool:
    if actor is None or not actor_can_read_audit_records(actor):
        return False
    if actor.actor_kind == "system":
        return True
    try:
        return _role_allows(actor.workspace_role, "editor")
    except ValueError:
        return False


SESSION_ACTIVITY_SOURCES: tuple[SessionActivitySource, ...] = (
    McpChatActivitySource(),
    AskSessionActivitySource(),
)
SESSION_ENTITY_KINDS = frozenset(
    source.entity_kind for source in SESSION_ACTIVITY_SOURCES
)

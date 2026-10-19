from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any, NamedTuple, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import and_, case, desc, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, load_only

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.base import utc_now
from backend.models.context import ContextClaim, ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.mcp_chat import (
    AUTHORING_VERBS,
    CONSUMPTION_VERBS,
    EXPOSURE_VERBS,
    McpChatArtifactKind,
    McpChatArtifactLink,
    McpChatArtifactVerb,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_restricted_library,
    actor_can_read_sensitive_agent_pages,
)
from backend.repositories.context_visibility import (
    PRIVATE_VERSION_STATUSES,
    editing_row_hidden_from_actor,
    visible_context_version_expression,
)
from backend.repositories.mcp_chat_repo import (
    list_call_artifact_edges,
    list_calls_naming_page,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES
from backend.schemas.mcp_chat import (
    McpChatAction,
    McpChatArchiveResult,
    McpChatArtifactLinkPayload,
    McpChatArtifactLinksPayload,
    McpChatClientRef,
    McpChatDetail,
    McpChatDisplayItem,
    McpChatEditOperation,
    McpChatEditReceipt,
    McpChatEditSummary,
    McpChatErrorDetail,
    McpChatExportPayload,
    McpChatListPayload,
    McpChatPresentationDescriptor,
    McpChatPreviewPurgeResult,
    McpChatProvenanceEntry,
    McpChatProvenancePayload,
    McpChatResultDetail,
    McpChatSessionDiff,
    McpChatSessionRef,
    McpChatTextPreview,
    McpChatTimelineArtifact,
    McpChatTimelineItem,
)
from backend.services.actor_visible_read import (
    VISIBLE_AGENT_SENSITIVITIES,
    agent_page_visible,
    agent_version_is_visible,
    context_page_visible,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.audit_service import AuditService
from backend.services.mcp_display_text import (
    sanitize_display_text as _sanitize_display_text,
)
from backend.services.mcp_display_text import (
    truncate_graphemes as _truncate_graphemes,
)
from backend.services.mcp_trace_redaction import (
    PATCH_OPERATION_KINDS,
    PREVIEW_BUDGET_EXHAUSTED_FIELD,
    PREVIEW_SUFFIX,
    null_previews,
)
from backend.services.read_errors import ReadNotFoundError
from backend.spi.tool_presentation import TOOL_ACTIONS

ERROR_STATUSES = {"failed", "rejected_by_policy", "conflict"}
PRESENTATION_ELIGIBLE_STATUSES = frozenset(
    {"accepted", "succeeded", "partial", "needs_review"}
)
# The P8.6 Context editing commands. Each one creates, changes, submits or
# closes the caller's own editing session, so each counts as a write.
CONTEXT_EDIT_TOOLS = frozenset(
    {
        "create_context_page",
        "begin_context_edit",
        "update_context_edit",
        "patch_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
    }
)
WRITE_TOOLS = {
    "upload_image",
    "cite",
    "write_agent_page_draft",
    "build_agent_input_bundle",
    *CONTEXT_EDIT_TOOLS,
}
IDLE_CLOSE_MINUTES = 30

# Caps on text the client authored. The summary is also the `q` search target,
# so it stays short enough that a verbose agent cannot dominate a match.
TITLE_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 200
CLIENT_DETAIL_NAME_MAX_GRAPHEMES = 200
CLIENT_LIST_NAME_MAX_GRAPHEMES = 80
ACTOR_SEARCH_CANDIDATE_BUDGET = 20
_CHAT_SEARCH_CURSOR_KIND = "chat-search-v1"

# Session-title evidence is intentionally narrower than the complete tool
# registry. These sets are the privacy boundary for values promoted from a
# captured input into the list/detail presentation descriptor.
_SEARCH_INPUT_FIELDS = {
    "search": "query",
    "grep_wiki": "pattern",
}
_FOCUSED_READ_TOOLS = frozenset(
    {"fetch", "read", "read_agent_page", "read_concept", "read_page"}
)
_BOOTSTRAP_TOOLS = frozenset(
    {
        "get_index",
        "guide",
        "list_agent_categories",
        "list_my_workspaces",
        "list_pages",
    }
)
_SAFE_TEXT_INPUT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "cite": (("claim_text", "claim"),),
    "write_agent_page_draft": (("title", "agent_page"),),
    "write_draft": (("title", "context_page"),),
    "write_page": (("title", "context_page"),),
}
_SAFE_KIND_INPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "fetch": ("type",),
    "list_changes": ("target_type",),
    "read": ("target_type",),
    "submit_feedback": ("target_type",),
}
# Detail-view-only input fields of the Context editing commands. Kept apart
# from _SAFE_TEXT_INPUT_FIELDS on purpose: these label one timeline row and
# never feed a chat's title, summary or list search.
_DISPLAY_TEXT_INPUT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "create_context_page": (("title", "context_page"), ("page_type", "page_type")),
    "update_context_edit": (("title", "context_page"), ("page_type", "page_type")),
}

# Edit projection: which calls describe a change, and where it landed.
_EDIT_SUMMARY_TOOLS = frozenset(
    {
        "create_context_page",
        "update_context_edit",
        "patch_context_edit",
        "write_agent_page_draft",
        "submit_revision_for_review",
    }
)
_SESSION_EDIT_TOOLS = frozenset(
    {"create_context_page", "update_context_edit", "patch_context_edit"}
)
# Commands whose input names the editing row they act on.
_VERSION_NAMING_EDIT_TOOLS = frozenset(
    {"patch_context_edit", "submit_context_edit_for_review", "discard_context_edit"}
)
_INPUT_EDIT_MODES = {
    "patch_context_edit": "patch",
    "create_context_page": "create",
    "update_context_edit": "whole_body",
}
_BODY_PREVIEW_FIELDS = {
    "create_context_page": "body_md",
    "write_agent_page_draft": "body_markdown",
    "submit_revision_for_review": "body_markdown",
}
_PREVIEWED_OPERATION_FIELDS = frozenset({"old_text", "new_text", "content", "title"})
_RESULT_DETAIL_PURGED_FIELD = "previews_purged"
_MULTIPLICATION_SIGN = "\N{MULTIPLICATION SIGN}"
_MINUS_SIGN = "\N{MINUS SIGN}"
_SECTION_SEPARATOR = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
_INPUT_FIELD_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Failed calls have no artifact edge (capture links successful outcomes only),
# so their targets are read back out of the captured input. These tables say
# what an input UUID can name. Anything UUID-shaped under a field not listed
# here -- or in _NON_TARGET_ID_FIELDS -- is an unknown target, which the
# reader can never be shown to see.
_UNKNOWN_TARGET = "unknown"
_CONTEXT_PAGE_ID_FIELDS = frozenset({"page_id", "target_page_id"})
_CONTEXT_VERSION_ID_FIELDS = frozenset(
    {
        "version_id",
        "base_version_id",
        "editing_version_id",
        "cited_context_page_version_id",
    }
)
# write_agent_page_draft's base_version_id is an AgentPageVersion.
_AGENT_VERSION_TOOLS = frozenset({"write_agent_page_draft"})
_ID_FIELD_TARGET_KINDS = {
    "agent_page_id": "agent_page",
    "library_item_id": "library_item",
    "library_item_version_id": "library_item_version",
    "library_artifact_id": "library_artifact",
}
# UUIDs that name no readable artifact: the call's own workspace, tree
# placement, client-generated markers and transport identifiers.
_NON_TARGET_ID_FIELDS = frozenset(
    {
        "workspace_id",
        "marker_uuid",
        "placement_parent_node_id",
        "destination_node_id",
        "parent_node_id",
        "client_id",
        "idempotency_key",
        "conversation_token",
    }
)
# (id field, kind field) pairs, e.g. read's target_id/target_type.
_TYPED_TARGET_FIELDS = (("target_id", "target_type"), ("id", "type"))
_TYPED_TARGET_KINDS = {
    "context_page": "context_page",
    "context_page_version": "context_version",
    "context_claim": "context_claim",
    "library_item": "library_item",
    "library_item_version": "library_item_version",
    "library_artifact": "library_artifact",
    "agent_page": "agent_page",
}
_TARGET_SCAN_MAX_DEPTH = 8
_TARGET_SCAN_MAX_ITEMS = 500
_TARGET_SCAN_MAX_REFS = 200


@dataclass(frozen=True)
class ArtifactLinkCandidate:
    resource_kind: McpChatArtifactKind
    resource_id: UUID
    verb: McpChatArtifactVerb
    # The conversation base ledger (design doc section 9): exact version
    # evidence from a versioned read or successful non-replayed patch. It is
    # carried by that call's own CONTEXT_PAGE relation (REFERENCED for a read,
    # EDITED for a patch) and is None when no exact version can be resolved.
    version_id: UUID | None = None
    root_hash: str | None = None


class McpChatService:
    """Aggregates raw MCP tool traces into external chat provenance."""

    def __init__(self, audit_service: AuditService | None = None) -> None:
        self.audit_service = audit_service or AuditService()

    def upsert_session_from_tool_call(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        client_id: UUID,
        mcp_session_id: str,
        tool_call: MCPToolCall,
        owner_user_id: UUID | None = None,
    ) -> McpChatSession:
        chat = session.scalar(
            select(McpChatSession).where(
                McpChatSession.workspace_id == workspace_id,
                McpChatSession.mcp_session_id == mcp_session_id,
            )
        )
        activity_at = tool_call.completed_at or tool_call.started_at or utc_now()
        if chat is None:
            chat = McpChatSession(
                workspace_id=workspace_id,
                client_id=client_id,
                mcp_session_id=mcp_session_id,
                owner_user_id=owner_user_id,
                started_at=tool_call.started_at or activity_at,
                last_activity_at=activity_at,
                tool_call_count=0,
                write_count=0,
                error_count=0,
                title_user_edited=False,
            )
            try:
                with session.begin_nested():
                    session.add(chat)
                    session.flush()
            except IntegrityError:
                # Concurrent first-touch already inserted this session row
                # (uq_mcp_chat_session_per_workspace; Postgres READ COMMITTED —
                # SQLite IMMEDIATE serializes writers). Re-read it so the losing
                # trace still records its tool call onto the surviving session
                # instead of aborting the whole trace-capture transaction.
                if chat in session:
                    session.expunge(chat)
                chat = session.scalar(
                    select(McpChatSession).where(
                        McpChatSession.workspace_id == workspace_id,
                        McpChatSession.mcp_session_id == mcp_session_id,
                    )
                )
                if chat is None:
                    raise
                if chat.owner_user_id is None and owner_user_id is not None:
                    chat.owner_user_id = owner_user_id
        elif chat.owner_user_id is None and owner_user_id is not None:
            chat.owner_user_id = owner_user_id
        # The chat keeps the client that started the conversation. The same
        # user may legitimately continue one conversation token through a
        # different client identity; reassigning here would re-point the
        # chat's provenance on every call. Per-call attribution stays on the
        # MCPToolCall rows themselves.
        chat.last_activity_at = _later(chat.last_activity_at, activity_at)
        # SQL-expression increments (count = count + 1) so concurrent traces on
        # the same conversation each land their +1 under Postgres READ COMMITTED
        # — a Python-computed value would write back a stale read and lose one.
        # Flush immediately: the expression UPDATE expires the counters, so the
        # label/summary refresh below re-reads the post-increment values.
        chat.tool_call_count = McpChatSession.tool_call_count + 1
        if tool_call.tool_name in WRITE_TOOLS:
            chat.write_count = McpChatSession.write_count + 1
        if tool_call.result_status in ERROR_STATUSES:
            chat.error_count = McpChatSession.error_count + 1
            if tool_call.result_status == "rejected_by_policy":
                chat.rejected_count = McpChatSession.rejected_count + 1
            else:
                chat.failed_count = McpChatSession.failed_count + 1
        session.flush()
        self._refresh_chat_labels(session, chat)
        self.freeze_summary_if_ended(chat)
        return chat

    def link_artifact(
        self,
        *,
        session: Session,
        chat_session: McpChatSession,
        tool_call: MCPToolCall,
        resource_kind: McpChatArtifactKind,
        resource_id: UUID,
        verb: McpChatArtifactVerb,
        version_id: UUID | None = None,
        root_hash: str | None = None,
    ) -> McpChatArtifactLink:
        occurred_at = tool_call.completed_at or tool_call.started_at or utc_now()

        def _advance_read_pointer(existing: McpChatArtifactLink) -> None:
            if version_id is None:
                return
            # This pointer is correctness state, not presentation rollup state.
            # An ordinary artifact touch may advance last_occurred_at without
            # carrying a version, so compare version-bearing observations against
            # last_read_at. Keep the predicate in SQL: under PostgreSQL READ
            # COMMITTED, a concurrent updater re-evaluates it after taking the
            # row lock and therefore cannot overwrite a newer committed read
            # with an older trace that arrived late.
            session.execute(
                update(McpChatArtifactLink)
                .where(
                    McpChatArtifactLink.workspace_id == chat_session.workspace_id,
                    McpChatArtifactLink.id == existing.id,
                    or_(
                        McpChatArtifactLink.last_read_at.is_(None),
                        McpChatArtifactLink.last_read_at <= occurred_at,
                    ),
                )
                .values(
                    last_read_version_id=version_id,
                    last_read_root_hash=root_hash,
                    last_read_at=occurred_at,
                )
                .execution_options(synchronize_session=False)
            )
            # The conditional UPDATE may or may not have won. Never let stale
            # identity-map state subsequently flush either outcome back over it.
            session.expire(
                existing,
                ["last_read_version_id", "last_read_root_hash", "last_read_at"],
            )

        def _advance(existing: McpChatArtifactLink) -> None:
            _advance_read_pointer(existing)
            # last_occurred_at and last_mcp_tool_call_id must name the SAME last
            # touch (the 0040 backfill and the timeline headline both assume it).
            # Tool calls can be persisted out of timestamp order, so advance both
            # only when THIS call is at least as recent as the current latest; an
            # older out-of-order call is counted but changes neither pointer.
            if _as_utc(occurred_at) >= _as_utc(existing.last_occurred_at):
                existing.last_occurred_at = occurred_at
                existing.last_mcp_tool_call_id = tool_call.id
            # Symmetric lower bound: a call linked out of order that is EARLIER
            # than the recorded first touch must pull first_occurred_at back.
            # Otherwise occurrence_count grows while first_occurred_at still
            # names a later call, and chat-title anchoring (which orders links
            # by first_occurred_at) and the exposed "first seen" timestamp drift.
            if _as_utc(occurred_at) < _as_utc(existing.first_occurred_at):
                existing.first_occurred_at = occurred_at
            # SQL-expression increment so concurrent links of the same
            # (chat, artifact, verb) each land their +1 (see the session
            # counters above); flush expires the count for re-readers.
            existing.occurrence_count = McpChatArtifactLink.occurrence_count + 1
            session.flush()

        link = session.scalar(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.workspace_id == chat_session.workspace_id,
                McpChatArtifactLink.chat_session_id == chat_session.id,
                McpChatArtifactLink.resource_kind == resource_kind.value,
                McpChatArtifactLink.resource_id == resource_id,
                McpChatArtifactLink.verb == verb.value,
            )
        )
        if link is None:
            link = McpChatArtifactLink(
                workspace_id=chat_session.workspace_id,
                chat_session_id=chat_session.id,
                resource_kind=resource_kind.value,
                resource_id=resource_id,
                verb=verb.value,
                first_occurred_at=occurred_at,
                last_occurred_at=occurred_at,
                occurrence_count=1,
                last_mcp_tool_call_id=tool_call.id,
                last_read_version_id=version_id,
                last_read_root_hash=root_hash,
                last_read_at=occurred_at if version_id is not None else None,
            )
            try:
                with session.begin_nested():
                    session.add(link)
                    session.flush()
            except IntegrityError:
                # Concurrent first-link of the same (chat, artifact, verb)
                # already inserted the rollup (uq_mcp_chat_artifact_link;
                # Postgres only). Re-read and fold this occurrence into it.
                if link in session:
                    session.expunge(link)
                link = session.scalar(
                    select(McpChatArtifactLink).where(
                        McpChatArtifactLink.workspace_id == chat_session.workspace_id,
                        McpChatArtifactLink.chat_session_id == chat_session.id,
                        McpChatArtifactLink.resource_kind == resource_kind.value,
                        McpChatArtifactLink.resource_id == resource_id,
                        McpChatArtifactLink.verb == verb.value,
                    )
                )
                if link is None:
                    raise
                _advance(link)
        else:
            _advance(link)
        # Persist a per-call edge alongside the rollup so the timeline can show
        # EVERY call that touched this artifact (the rollup keeps only the most
        # recent call). Idempotent on (tool_call, resource_kind, resource_id,
        # verb): the same call re-reporting the same touch is a no-op.
        edge = session.scalar(
            select(McpChatToolCallArtifact).where(
                McpChatToolCallArtifact.mcp_tool_call_id == tool_call.id,
                McpChatToolCallArtifact.resource_kind == resource_kind.value,
                McpChatToolCallArtifact.resource_id == resource_id,
                McpChatToolCallArtifact.verb == verb.value,
            )
        )
        if edge is None:
            new_edge = McpChatToolCallArtifact(
                workspace_id=chat_session.workspace_id,
                chat_session_id=chat_session.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind=resource_kind.value,
                resource_id=resource_id,
                verb=verb.value,
                occurred_at=occurred_at,
            )
            try:
                with session.begin_nested():
                    session.add(new_edge)
                    session.flush()
            except IntegrityError:
                # Concurrent insert of the identical per-call edge
                # (uq_mcp_chat_tool_call_artifact); idempotent, so ignore.
                pass
        self._refresh_chat_labels(session, chat_session)
        return link

    def list_chats(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        client_id: UUID | None = None,
        period: str = "all",
        q: str | None = None,
        writes_only: bool = False,
        archived: bool = False,
        sort: str = "last_activity_desc",
        limit: int = 50,
        cursor: str | None = None,
        actor: ActorContext | None = None,
    ) -> McpChatListPayload:
        needle = q.strip().casefold() if q else ""
        actor_search = bool(needle and actor is not None)
        offset = (
            _parse_typed_cursor(cursor, _CHAT_SEARCH_CURSOR_KIND)
            if actor_search
            else _parse_cursor(cursor)
        )
        filters = [McpChatSession.workspace_id == workspace_id]
        if client_id is not None:
            filters.append(McpChatSession.client_id == client_id)
        if writes_only:
            filters.append(McpChatSession.write_count > 0)
        if archived:
            filters.append(McpChatSession.archived_at.is_not(None))
        else:
            filters.append(McpChatSession.archived_at.is_(None))
        period_start = _period_start(period)
        if period_start is not None:
            filters.append(McpChatSession.last_activity_at >= period_start)
        # Ungated/system reads may use the persisted projection directly. For
        # actor-bound search the stored title/summary is not a privacy-safe SQL
        # predicate, because it was derived at capture time without that actor.
        if needle and actor is None:
            like = f"%{q.strip()}%"
            filters.append(
                or_(
                    McpChatSession.title.ilike(like),
                    McpChatSession.summary.ilike(like),
                )
            )
        order_by = (
            desc(McpChatSession.write_count)
            if sort == "write_count_desc"
            else desc(McpChatSession.last_activity_at)
        )
        stmt = (
            select(McpChatSession)
            .where(and_(*filters))
            .order_by(order_by, desc(McpChatSession.id))
        )
        if actor_search:
            assert actor is not None
            items, next_cursor = self._actor_visible_chat_search_page(
                session=session,
                stmt=stmt,
                actor=actor,
                needle=needle,
                offset=offset,
                limit=limit,
            )
        else:
            rows = session.scalars(stmt.offset(offset).limit(limit + 1)).all()
            items = [
                self._session_ref(session, row, actor=actor) for row in rows[:limit]
            ]
            next_cursor = str(offset + limit) if len(rows) > limit else None
        return McpChatListPayload(
            items=items,
            next_cursor=next_cursor,
            clients=self._client_refs(session, workspace_id),
        )

    def _actor_visible_chat_search_page(
        self,
        *,
        session: Session,
        stmt,
        actor: ActorContext,
        needle: str,
        offset: int,
        limit: int,
    ) -> tuple[list[McpChatSessionRef], str | None]:
        """Search the current actor-visible projection before paginating it.

        Scanning the same base chat set for every actor-bound query prevents a
        guessed hidden title from changing either the candidate set or cursor.
        One request projects at most ``ACTOR_SEARCH_CANDIDATE_BUDGET`` rows;
        the typed cursor continues at the next raw chat position. The base chat
        list is already actor-visible as generic activity, so that continuation
        reveals no hidden artifact title or match count.
        """

        candidate_limit = min(limit, ACTOR_SEARCH_CANDIDATE_BUDGET)
        rows = session.scalars(stmt.offset(offset).limit(candidate_limit + 1)).all()
        matches: list[McpChatSessionRef] = []
        for row in rows[:candidate_limit]:
            item = self._session_ref(session, row, actor=actor)
            if (
                needle in item.title.casefold()
                or needle in item.summary.casefold()
                or needle in (item.intent_summary or "").casefold()
            ):
                matches.append(item)
        next_cursor = (
            _encode_typed_cursor(_CHAT_SEARCH_CURSOR_KIND, offset + candidate_limit)
            if len(rows) > candidate_limit
            else None
        )
        return matches, next_cursor

    def read_chat(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
        actor: ActorContext | None = None,
    ) -> McpChatDetail:
        chat = self._get_chat(session, workspace_id, chat_session_id)
        ref = self._session_ref(
            session,
            chat,
            link_limit=100,
            actor=actor,
            include_full_client=True,
            include_search_matches=True,
        )
        return McpChatDetail(
            **ref.model_dump(),
            timeline=self._timeline(session, chat, actor),
            # ``frozen_summary`` was captured without a reader and can contain
            # an artifact title that has since become hidden.  An actor-bound
            # detail/export must project the frozen field through the same
            # current visibility policy as ``summary``.  Keep the original
            # value only for trusted internal reads, and keep an unfrozen chat
            # unfrozen rather than manufacturing a value.
            frozen_summary=(
                ref.summary
                if actor is not None and chat.frozen_summary is not None
                else chat.frozen_summary
            ),
            frozen_at=chat.frozen_at,
        )

    def list_artifact_provenance(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        resource_kind: str,
        resource_id: UUID,
        actor: ActorContext | None = None,
    ) -> McpChatProvenancePayload:
        # The provenance query is keyed by a specific artifact; if the reader
        # cannot see that artifact, disclose no chats touched it (Codex R12).
        title, _path = self._artifact_title_and_path(
            session, resource_kind, resource_id, actor, workspace_id
        )
        if actor is not None and title is None:
            return McpChatProvenancePayload(items=[])
        rows = session.execute(
            select(McpChatArtifactLink, McpChatSession)
            .join(
                McpChatSession,
                McpChatArtifactLink.chat_session_id == McpChatSession.id,
            )
            .where(
                McpChatSession.workspace_id == workspace_id,
                McpChatArtifactLink.resource_kind == resource_kind,
                McpChatArtifactLink.resource_id == resource_id,
            )
            .order_by(
                case(
                    (McpChatArtifactLink.verb.in_(CONSUMPTION_VERBS), 0),
                    (McpChatArtifactLink.verb.in_(AUTHORING_VERBS), 1),
                    (McpChatArtifactLink.verb.in_(EXPOSURE_VERBS), 2),
                    else_=3,
                ),
                desc(McpChatArtifactLink.last_occurred_at),
            )
        ).all()
        return McpChatProvenancePayload(
            items=[
                McpChatProvenanceEntry(
                    verb=link.verb,
                    occurrence_count=link.occurrence_count,
                    last_occurred_at=link.last_occurred_at,
                    chat_session=self._session_ref(session, chat, actor=actor),
                )
                for link, chat in rows
            ]
        )

    def list_artifact_links(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        since=None,
        until=None,
        verb: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        actor: ActorContext | None = None,
    ) -> McpChatArtifactLinksPayload:
        offset = _parse_cursor(cursor)
        filters = [McpChatSession.workspace_id == workspace_id]
        if since is not None:
            filters.append(McpChatArtifactLink.last_occurred_at >= since)
        if until is not None:
            filters.append(McpChatArtifactLink.last_occurred_at <= until)
        if verb:
            filters.append(McpChatArtifactLink.verb == verb)
        if actor is not None:
            filters.append(
                self._actor_visible_artifact_link_predicate(actor, workspace_id)
            )
        stmt = (
            select(McpChatArtifactLink)
            .join(
                McpChatSession,
                McpChatArtifactLink.chat_session_id == McpChatSession.id,
            )
            .where(and_(*filters))
            .order_by(
                desc(McpChatArtifactLink.last_occurred_at),
                desc(McpChatArtifactLink.id),
            )
        )
        rows = session.scalars(stmt.offset(offset).limit(limit + 1)).all()
        items = [
            payload
            for row in rows[:limit]
            if (payload := self._artifact_payload(session, row, actor, workspace_id))
            is not None
        ]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return McpChatArtifactLinksPayload(items=items, next_cursor=next_cursor)

    def _actor_visible_artifact_link_predicate(
        self,
        actor: ActorContext,
        workspace_id: UUID,
    ):
        """Correlated SQL predicate equivalent to ``_artifact_payload`` gates.

        Filtering hidden targets in SQL makes offset/cursor count only visible
        links. A thousand restricted rows therefore neither displace a visible
        link nor create an observable continuation, and query work stays bound
        by the requested visible page size.
        """

        from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES

        context_visible = (
            select(ContextPage.id)
            .where(
                ContextPage.id == McpChatArtifactLink.resource_id,
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
                visible_context_version_expression(actor).is_not(None),
            )
            .exists()
        )

        agent_filters = [
            AgentPage.id == McpChatArtifactLink.resource_id,
            AgentPage.workspace_id == workspace_id,
            AgentPage.lifecycle_status != "archived",
        ]
        if not actor_can_read_drafts(actor):
            agent_filters.append(AgentPage.current_active_version_id.is_not(None))
        if not actor_can_read_sensitive_agent_pages(actor):
            agent_filters.extend(
                [
                    AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
                    AgentPage.sensitivity.in_(VISIBLE_AGENT_SENSITIVITIES),
                ]
            )
        agent_visible = select(AgentPage.id).where(*agent_filters).exists()

        library_filters = [
            LibraryItem.workspace_id == workspace_id,
            LibraryItem.lifecycle_status.notin_(EXCLUDED_LIBRARY_LIFECYCLE_STATUSES),
        ]
        if not actor_can_read_restricted_library(actor):
            library_filters.append(LibraryItem.trust_level != "restricted")
        # P5.1: every producer normalizes a LibraryArtifact id to its parent
        # LibraryItem id before writing a library_item-kind link
        # (mcp_trace_capture.py's _library_item_id_for_artifact), so
        # resource_id under this kind is always a real LibraryItem id --
        # no need to also probe LibraryArtifact ids here.
        direct_library_visible = (
            select(LibraryItem.id)
            .where(
                LibraryItem.id == McpChatArtifactLink.resource_id,
                *library_filters,
            )
            .exists()
        )

        # P3.1/P3.2: every asset is page-owned now (ContextMediaSession and
        # its page-less "session-bound" ownership shape are gone), but that
        # does NOT mean every page-bound asset is as visible as its page.
        # `visible_context_version_expression(actor).is_not(None)` below only
        # proves the PAGE has some head visible to this actor (e.g. its
        # published version) -- it says nothing about whether THIS asset's
        # OWN `created_for_version_id` is that same visible row. Page-first
        # creation can register an asset against a private "editing" row on a
        # page that also has an unrelated visible head (its own published
        # version, or another author's shared draft), so without the extra
        # join below a page-visible-at-all check alone would leak an
        # unshared editing session's upload the same way P2.6
        # (PageNameIndex/FTS) leaked names/body text through an analogous
        # gap -- confirmed reachable now that page-bound-but-private assets
        # are possible, which they were not before P2/P3 existed. The
        # created_for_version join closes it the same way
        # context_visibility.py's editing_row_hidden_from_actor does: an
        # "editing" row is visible only to its own creator, any other status
        # is visible on the same terms as the rest of the page. Excludes a
        # tombstoned asset -- see the matching comment in
        # _artifact_title_and_path for why that mirrors the archived-page and
        # excluded-library-status exclusions above rather than preserving raw
        # history. This predicate always runs with a real actor (only called
        # from the `if actor is not None:` branch above), so the exclusion is
        # unconditional here, unlike the actor-optional Python gate.
        media_created_for_version = aliased(ContextPageVersion)
        media_page_visible = (
            select(ContextMediaAsset.id)
            .join(ContextPage, ContextPage.id == ContextMediaAsset.page_id)
            .join(
                media_created_for_version,
                media_created_for_version.id
                == ContextMediaAsset.created_for_version_id,
            )
            .where(
                ContextMediaAsset.id == McpChatArtifactLink.resource_id,
                ContextMediaAsset.workspace_id == workspace_id,
                ContextMediaAsset.association_state != "tombstoned",
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
                visible_context_version_expression(actor).is_not(None),
                or_(
                    media_created_for_version.version_status != "editing",
                    and_(
                        media_created_for_version.created_by_actor_kind
                        == actor.actor_kind,
                        media_created_for_version.created_by_actor_id
                        == actor.actor_id,
                    ),
                ),
            )
            .exists()
        )

        return or_(
            and_(
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.CONTEXT_PAGE.value,
                context_visible,
            ),
            and_(
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.AGENT_PAGE.value,
                agent_visible,
            ),
            and_(
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.LIBRARY_ITEM.value,
                direct_library_visible,
            ),
            and_(
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.CONTEXT_MEDIA_ASSET.value,
                media_page_visible,
            ),
        )

    def update_title(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
        title: str,
        actor: ActorContext,
    ) -> McpChatDetail:
        chat = self._get_chat(session, workspace_id, chat_session_id)
        previous_title = chat.title
        chat.title = title.strip()
        chat.title_user_edited = True
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="mcp_chat_session.title_updated",
            target_kind="mcp_chat_session",
            target_id=chat.id,
            payload={"previous_title": previous_title, "title": chat.title},
            event_summary="MCP chat title updated.",
        )
        session.flush()
        return self.read_chat(
            session=session,
            workspace_id=workspace_id,
            chat_session_id=chat_session_id,
            actor=actor,
        )

    def archive(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
        actor: ActorContext,
    ) -> McpChatArchiveResult:
        chat = self._get_chat(session, workspace_id, chat_session_id)
        if chat.archived_at is None:
            chat.archived_at = utc_now()
        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="mcp_chat_session.archived",
            target_kind="mcp_chat_session",
            target_id=chat.id,
            payload={"archived_at": chat.archived_at.isoformat()},
            event_summary="MCP chat archived.",
        )
        session.flush()
        return McpChatArchiveResult(
            chat_session_id=chat.id,
            archived_at=chat.archived_at,
            audit_event_id=audit_event.id,
        )

    def unarchive(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
        actor: ActorContext,
    ) -> McpChatArchiveResult:
        chat = self._get_chat(session, workspace_id, chat_session_id)
        chat.archived_at = None
        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="mcp_chat_session.unarchived",
            target_kind="mcp_chat_session",
            target_id=chat.id,
            payload={},
            event_summary="MCP chat unarchived.",
        )
        session.flush()
        return McpChatArchiveResult(
            chat_session_id=chat.id,
            archived_at=chat.archived_at,
            audit_event_id=audit_event.id,
        )

    def export_chats(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        since=None,
        until=None,
        limit: int = 5000,
        cursor: str | None = None,
        actor: ActorContext | None = None,
    ) -> McpChatExportPayload:
        offset = _parse_cursor(cursor)
        filters = [McpChatSession.workspace_id == workspace_id]
        if since is not None:
            filters.append(McpChatSession.last_activity_at >= since)
        if until is not None:
            filters.append(McpChatSession.last_activity_at <= until)
        rows = session.scalars(
            select(McpChatSession)
            .where(and_(*filters))
            .order_by(desc(McpChatSession.last_activity_at))
            .offset(offset)
            .limit(limit + 1)
        ).all()
        chats = [
            self.read_chat(
                session=session,
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                actor=actor,
            )
            for chat in rows[:limit]
        ]
        return McpChatExportPayload(
            exported_at=utc_now(),
            workspace_id=workspace_id,
            range={"since": since, "until": until},
            chats=chats,
            next_cursor=str(offset + limit) if len(rows) > limit else None,
        )

    def freeze_summary_if_ended(self, chat_session: McpChatSession) -> None:
        ended_at = chat_session.last_activity_at + timedelta(minutes=IDLE_CLOSE_MINUTES)
        if chat_session.frozen_at is None and ended_at < utc_now():
            chat_session.frozen_summary = chat_session.summary or self.auto_summary(
                chat_session
            )
            chat_session.frozen_at = utc_now()

    def auto_summary(self, chat_session: McpChatSession) -> str:
        calls = chat_session.tool_call_count
        parts = [f"{calls} tool call{'' if calls == 1 else 's'}"]
        if chat_session.write_count:
            writes = chat_session.write_count
            parts.append(f"{writes} write{'' if writes == 1 else 's'}")
        if chat_session.error_count:
            errors = chat_session.error_count
            parts.append(f"{errors} error{'' if errors == 1 else 's'}")
        return ", ".join(parts)

    def _semantic_summary(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None = None,
        *,
        redacted_call_ids: set[UUID] | None = None,
        shared_only: bool = False,
    ) -> str:
        # What the session DID (primary artifact verb + title, or the first tool
        # action) — not a restatement of the call/write counts, which the UI
        # already shows as chips (item 4). Falls back to counts only for an
        # otherwise-empty chat.
        link = session.scalar(
            select(McpChatArtifactLink)
            .where(
                McpChatArtifactLink.chat_session_id == chat.id,
                McpChatArtifactLink.verb.not_in(
                    (
                        McpChatArtifactVerb.REFERENCED.value,
                        McpChatArtifactVerb.SEARCH_MATCH.value,
                    )
                ),
            )
            .order_by(McpChatArtifactLink.last_occurred_at.desc())
        )
        if link is not None:
            title, _path = self._artifact_title_and_path(
                session,
                link.resource_kind,
                link.resource_id,
                actor,
                chat.workspace_id,
                shared_only=shared_only,
            )
            if title:
                verb = link.verb.replace("_", " ").capitalize()
                return f"{verb}: {title}"
        first_call = self._first_tool_call(
            session, chat, actor, redacted_call_ids=redacted_call_ids
        )
        if first_call is not None:
            # The list response serializes summary even though the current UI
            # treats it as legacy/debug data. Keep that field behind the same
            # per-tool input allowlist as the presentation descriptor; an
            # arbitrary tool's `title`, prompt-like body or nested arguments
            # must not hitch a ride through this fallback.
            return _presentation_anchor(
                _input_presentation(first_call.tool_name, first_call.input_summary)
            )
        return self.auto_summary(chat)

    def _refresh_chat_labels(self, session: Session, chat: McpChatSession) -> None:
        # The stored labels are computed without a reader, so they must not
        # name a page only its author can see: a private page's title lives
        # on the page aggregate until its first submit.
        chat.summary = self._semantic_summary(session, chat, shared_only=True)
        if not chat.title_user_edited:
            chat.title = self.auto_title(session, chat, shared_only=True)

    def auto_title(
        self,
        session: Session,
        chat_session: McpChatSession,
        actor: ActorContext | None = None,
        *,
        shared_only: bool = False,
    ) -> str:
        # Concise, human-readable title = the chat's primary anchor (a linked
        # artifact title or the first tool action). Client identity and the
        # call count are surfaced separately in the UI (client chip + meta),
        # so they are intentionally omitted from the title here.
        return self._title_anchor(
            session, chat_session, actor, shared_only=shared_only
        )

    def _title_anchor(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None = None,
        *,
        redacted_call_ids: set[UUID] | None = None,
        shared_only: bool = False,
    ) -> str:
        if redacted_call_ids is None:
            redacted_call_ids = self._redacted_call_ids(session, chat, actor)
        presentation = self._session_presentation(
            session,
            chat,
            actor,
            redacted_call_ids=redacted_call_ids,
            shared_only=shared_only,
        )
        return _presentation_anchor(presentation)

    def _session_ref(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        link_limit: int = 4,
        actor: ActorContext | None = None,
        include_full_client: bool = False,
        include_search_matches: bool = False,
    ) -> McpChatSessionRef:
        client = get_in_workspace(
            session,
            MCPClient,
            workspace_id=chat.workspace_id,
            resource_id=chat.client_id,
        )
        links_query = select(McpChatArtifactLink).where(
            McpChatArtifactLink.chat_session_id == chat.id
        )
        if not include_search_matches:
            links_query = links_query.where(
                McpChatArtifactLink.verb
                != McpChatArtifactVerb.SEARCH_MATCH.value
            )
        links = session.scalars(
            links_query
            .order_by(desc(McpChatArtifactLink.last_occurred_at))
            .limit(link_limit)
        ).all()
        # Generated labels are always re-derived from the same read-time,
        # actor-visible descriptor. This heals both legacy count titles and the
        # old bootstrap-first titles without persisting localized prose. A
        # user-edited title remains the only immutable stored override.
        redacted_call_ids = self._redacted_call_ids(session, chat, actor)
        presentation = self._session_presentation(
            session,
            chat,
            actor,
            redacted_call_ids=redacted_call_ids,
        )
        stored_title = (
            chat.title
            if chat.title_user_edited and chat.title
            else _presentation_anchor(presentation)
        )
        intent_summary, intent_source = self._intent(
            session, chat, actor, redacted_call_ids=redacted_call_ids
        )
        if actor is not None:
            stored_summary = self._semantic_summary(
                session, chat, actor, redacted_call_ids=redacted_call_ids
            )
        else:
            stored_summary = (
                chat.frozen_summary or chat.summary or self.auto_summary(chat)
            )
        return McpChatSessionRef(
            **presentation.model_dump(),
            id=chat.id,
            workspace_id=chat.workspace_id,
            client=(
                _client_ref(client) if include_full_client else _client_list_ref(client)
            ),
            mcp_session_id=chat.mcp_session_id,
            title=stored_title,
            summary=stored_summary,
            started_at=chat.started_at,
            last_activity_at=chat.last_activity_at,
            tool_call_count=chat.tool_call_count,
            write_count=chat.write_count,
            error_count=chat.error_count,
            failed_count=chat.failed_count,
            rejected_count=chat.rejected_count,
            archived_at=chat.archived_at,
            conversation_group_id=chat.conversation_group_id,
            title_user_edited=chat.title_user_edited,
            intent_summary=intent_summary,
            intent_source=intent_source,
            linked_artifacts=[
                payload
                for link in links
                if (
                    payload := self._artifact_payload(
                        session, link, actor, chat.workspace_id
                    )
                )
                is not None
            ],
        )

    def _session_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None,
        *,
        redacted_call_ids: set[UUID],
        shared_only: bool = False,
    ) -> McpChatPresentationDescriptor:
        """Derive the seven-tier localizable presentation hierarchy.

        Only typed call status, allowlisted input fields and per-call artifact
        edges participate. Result-summary prose, result counts and inferred
        external-client prompts are deliberately outside this contract.
        """

        if chat.title_user_edited:
            safe_tool_name = self._first_eligible_tool_name(
                session, chat, redacted_call_ids=redacted_call_ids
            )
            return McpChatPresentationDescriptor(
                title_source="user",
                action=_tool_action(safe_tool_name),
                subject=chat.title or "",
                subject_kind=None,
                primary_tool_name=safe_tool_name,
            )

        authoring = self._authoring_presentation(
            session, chat, actor, shared_only=shared_only
        )
        if authoring is not None:
            return authoring

        search = self._search_presentation(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if search is not None:
            return search

        focused_read = self._focused_read_presentation(
            session, chat, actor, shared_only=shared_only
        )
        if focused_read is not None:
            return focused_read

        action_input = self._action_input_presentation(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if action_input is not None:
            return action_input

        bootstrap = self._bootstrap_presentation(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if bootstrap is not None:
            return bootstrap

        return _generic_presentation()

    def _eligible_tool_call_stmt(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        redacted_call_ids: set[UUID] | None = None,
    ):
        stmt = self._tool_call_query(session, chat)
        if stmt is None:
            return None
        stmt = stmt.where(MCPToolCall.result_status.in_(PRESENTATION_ELIGIBLE_STATUSES))
        if redacted_call_ids:
            stmt = stmt.where(MCPToolCall.id.notin_(redacted_call_ids))
        return stmt

    def _first_eligible_tool_name(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        redacted_call_ids: set[UUID],
    ) -> str | None:
        stmt = self._eligible_tool_call_stmt(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if stmt is None:
            return None
        return session.scalar(stmt.with_only_columns(MCPToolCall.tool_name).limit(1))

    def _authoring_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None,
        *,
        shared_only: bool = False,
    ) -> McpChatPresentationDescriptor | None:
        rows = session.execute(
            select(
                McpChatToolCallArtifact.verb,
                McpChatToolCallArtifact.resource_kind,
                McpChatToolCallArtifact.resource_id,
                MCPToolCall.tool_name,
            )
            .join(
                MCPToolCall,
                MCPToolCall.id == McpChatToolCallArtifact.mcp_tool_call_id,
            )
            .where(
                McpChatToolCallArtifact.chat_session_id == chat.id,
                McpChatToolCallArtifact.verb.in_(
                    (
                        McpChatArtifactVerb.CREATED.value,
                        McpChatArtifactVerb.EDITED.value,
                    )
                ),
                MCPToolCall.workspace_id == chat.workspace_id,
                MCPToolCall.result_status.in_(PRESENTATION_ELIGIBLE_STATUSES),
            )
            # Latest authoring evidence wins; all remaining columns are stable
            # tie-breakers, so insert/materialization order cannot affect it.
            .order_by(
                McpChatToolCallArtifact.occurred_at.desc(),
                MCPToolCall.started_at.desc(),
                MCPToolCall.id,
                McpChatToolCallArtifact.resource_kind,
                McpChatToolCallArtifact.resource_id,
                McpChatToolCallArtifact.verb,
            )
        ).all()
        for verb, resource_kind, resource_id, tool_name in rows:
            title, _path = self._artifact_title_and_path(
                session,
                resource_kind,
                resource_id,
                actor,
                chat.workspace_id,
                shared_only=shared_only,
            )
            subject = _normalize_presentation_text(title)
            if subject is not None:
                return _artifact_presentation(
                    tool_name=tool_name,
                    verb=verb,
                    resource_kind=resource_kind,
                    title=subject,
                )
        return None

    def _search_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        redacted_call_ids: set[UUID],
    ) -> McpChatPresentationDescriptor | None:
        stmt = self._eligible_tool_call_stmt(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if stmt is None:
            return None
        rows = session.execute(
            stmt.where(
                MCPToolCall.tool_name.in_(_SEARCH_INPUT_FIELDS)
            ).with_only_columns(
                MCPToolCall.tool_name,
                MCPToolCall.input_summary,
            )
        ).all()
        for tool_name, input_summary in rows:
            if not isinstance(input_summary, dict):
                continue
            subject = _normalize_presentation_text(
                input_summary.get(_SEARCH_INPUT_FIELDS[tool_name])
            )
            if subject is not None:
                return McpChatPresentationDescriptor(
                    title_source="query",
                    action="search",
                    subject=subject,
                    subject_kind="query",
                    primary_tool_name=tool_name,
                )
        return None

    def _focused_read_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None,
        *,
        shared_only: bool = False,
    ) -> McpChatPresentationDescriptor | None:
        rows = session.execute(
            select(
                MCPToolCall.id,
                MCPToolCall.tool_name,
                McpChatToolCallArtifact.resource_kind,
                McpChatToolCallArtifact.resource_id,
            )
            .join(
                MCPToolCall,
                MCPToolCall.id == McpChatToolCallArtifact.mcp_tool_call_id,
            )
            .where(
                McpChatToolCallArtifact.chat_session_id == chat.id,
                McpChatToolCallArtifact.verb == McpChatArtifactVerb.REFERENCED.value,
                MCPToolCall.workspace_id == chat.workspace_id,
                MCPToolCall.tool_name.in_(_FOCUSED_READ_TOOLS),
                MCPToolCall.result_status.in_(PRESENTATION_ELIGIBLE_STATUSES),
            )
            .order_by(
                MCPToolCall.started_at,
                MCPToolCall.id,
                McpChatToolCallArtifact.resource_kind,
                McpChatToolCallArtifact.resource_id,
            )
        ).all()
        visible_artifacts: dict[tuple[str, UUID], tuple[str, str]] = {}
        for _call_id, tool_name, resource_kind, resource_id in rows:
            title, _path = self._artifact_title_and_path(
                session,
                resource_kind,
                resource_id,
                actor,
                chat.workspace_id,
                shared_only=shared_only,
            )
            subject = _normalize_presentation_text(title)
            if subject is not None:
                # The query is ordered by call time, so setdefault preserves
                # the first eligible read tool as stable provenance when the
                # same artifact is read repeatedly.
                visible_artifacts.setdefault(
                    (resource_kind, resource_id), (subject, tool_name)
                )

        # Focused reading is a chat-level intent, not a per-call property.  Two
        # one-document calls that read A and then B are discovery, even though
        # each call is individually unambiguous.  Hidden artifacts intentionally
        # do not count; including them would leak their existence through the
        # selected title tier.
        if len(visible_artifacts) != 1:
            return None
        (resource_kind, _resource_id), (subject, tool_name) = next(
            iter(visible_artifacts.items())
        )
        return McpChatPresentationDescriptor(
            title_source="artifact",
            action="read",
            subject=subject,
            subject_kind=resource_kind,
            primary_tool_name=tool_name,
        )

    def _action_input_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        redacted_call_ids: set[UUID],
    ) -> McpChatPresentationDescriptor | None:
        stmt = self._eligible_tool_call_stmt(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if stmt is None:
            return None
        row = session.execute(
            stmt.where(MCPToolCall.tool_name.notin_(_BOOTSTRAP_TOOLS))
            .with_only_columns(MCPToolCall.tool_name, MCPToolCall.input_summary)
            .limit(1)
        ).first()
        if row is None or not isinstance(row.input_summary, dict):
            return None
        return _allowlisted_input_presentation(row.tool_name, row.input_summary)

    def _bootstrap_presentation(
        self,
        session: Session,
        chat: McpChatSession,
        *,
        redacted_call_ids: set[UUID],
    ) -> McpChatPresentationDescriptor | None:
        stmt = self._eligible_tool_call_stmt(
            session, chat, redacted_call_ids=redacted_call_ids
        )
        if stmt is None:
            return None
        tool_names = session.scalars(
            stmt.with_only_columns(MCPToolCall.tool_name)
        ).all()
        if tool_names and all(name in _BOOTSTRAP_TOOLS for name in tool_names):
            return _safe_tool_presentation(tool_names[0])
        return None

    def _timeline(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None = None,
    ) -> list[McpChatTimelineItem]:
        rows = self._tool_calls_for_chat(session, chat)
        # Build per-call links from the per-call edge table (not the rollup's
        # last_mcp_tool_call_id), so an artifact touched by several calls
        # attaches to EVERY call that touched it — each row keeps its own
        # linked_artifacts and "Verb: Title" headline (T9b-FU1).
        link_rows = list_call_artifact_edges(
            session, workspace_id=chat.workspace_id, chat_session_id=chat.id
        )
        gate = _ReaderGate(self, session, chat.workspace_id, actor)
        verbs_by_tool_call: dict[UUID, set[str]] = {}
        artifacts_by_tool_call: dict[UUID, list[McpChatTimelineArtifact]] = {}
        headline_artifact: dict[UUID, tuple[str, str | None, str]] = {}
        # Calls whose primary target (created/edited/referenced/cited) is now
        # hidden from the reader: their captured input can still name that
        # target's title or body, so the whole row is withheld (Codex R13). A
        # hidden search match is only one result among several; it is dropped
        # and the call stays readable.
        target_hidden: set[UUID] = set()
        discarded_edit: set[UUID] = set()
        for tool_call_id, verb, resource_kind, resource_id in link_rows:
            title, path = gate.title_and_path(resource_kind, resource_id)
            # For a gated reader, drop a hidden/dangling target entirely — its id
            # and verb must not leak, and it must not headline the call.
            if actor is not None and title is None:
                if verb == McpChatArtifactVerb.SEARCH_MATCH.value:
                    continue
                if (
                    resource_kind == McpChatArtifactKind.CONTEXT_PAGE.value
                    and gate.is_chat_owner(chat)
                    and gate.is_readers_discarded_page(resource_id)
                ):
                    discarded_edit.add(tool_call_id)
                    continue
                target_hidden.add(tool_call_id)
                continue
            verbs_by_tool_call.setdefault(tool_call_id, set()).add(verb)
            artifacts_by_tool_call.setdefault(tool_call_id, []).append(
                McpChatTimelineArtifact(
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    artifact_title=title,
                    navigation_path=path,
                    verb=verb,
                )
            )
            headline_artifact.setdefault(tool_call_id, (verb, title, resource_kind))
        discarded_edit -= target_hidden
        session_diffs = self._session_diffs(rows, gate, blocked=target_hidden)
        timeline: list[McpChatTimelineItem] = []
        for row in rows:
            linked_artifacts = artifacts_by_tool_call.get(row.id, [])
            common = {
                "id": row.id,
                "tool_name": row.tool_name,
                "schema_version": row.schema_version,
                "result_status": row.result_status,
                "error_code": row.error_code,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
                "latency_ms": row.latency_ms,
            }
            if actor is not None and row.id in target_hidden:
                timeline.append(_target_hidden_item(row, common))
                continue
            if actor is not None and row.result_status in ERROR_STATUSES:
                timeline.append(self._error_call_item(row, common, gate, chat))
                continue
            if row.id in discarded_edit:
                timeline.append(_discarded_edit_item(row, common))
                continue
            timeline.append(
                _visible_call_item(
                    row,
                    common,
                    linked_artifacts=linked_artifacts,
                    linked_verbs=sorted(verbs_by_tool_call.get(row.id, set())),
                    primary_artifact=headline_artifact.get(row.id),
                    content_visible=gate.call_content_visible(row, chat),
                    session_diff=session_diffs.get(row.id),
                )
            )
        # Enrich only after visibility/content projection has populated the
        # request cache. Most edit versions need no additional read at all.
        visible_pairs = [
            (row, item)
            for row, item in zip(rows, timeline, strict=True)
            if item.input_availability != "redacted" and row.id not in discarded_edit
        ]
        gate.prime_input_versions([row for row, _ in visible_pairs])
        for row, item in visible_pairs:
            item.input_display_items.extend(gate.input_version_items(row))
        return timeline

    def _error_call_item(
        self,
        row: MCPToolCall,
        common: dict[str, Any],
        gate: _ReaderGate,
        chat: McpChatSession,
    ) -> McpChatTimelineItem:
        """Project a failed, conflicted or refused call for an actor.

        Trace capture never links these calls to artifacts, so the targets
        are re-resolved from the captured input under the reader's own
        visibility. A UUID in that input is untrusted: one that names a
        hidden page, nothing at all, or another workspace's row yields the
        same shape-only projection, so the row cannot be used to probe for
        existence. The server's message is the caller's alone -- historical
        ownership of the chat is a reason to show a user their own error,
        never a visibility grant for a target.
        """

        targets: list[_ResolvedTarget] = []
        hidden = False
        for kind, target_id in _input_target_refs(row.tool_name, row.input_summary):
            resolved = gate.input_target(kind, target_id)
            if resolved is None:
                hidden = True
                break
            if all(target.resource_id != resolved.resource_id for target in targets):
                targets.append(resolved)
        if hidden:
            return _target_hidden_item(row, common, structural=True)

        is_caller = gate.is_chat_owner(chat)
        content_visible = gate.call_content_visible(row, chat)
        if targets:
            input_summary = (
                row.input_summary
                if content_visible
                else _version_hidden_input(row.input_summary)
            )
            edit_summary = _edit_summary(
                row.tool_name,
                input_summary,
                None,
                withheld=not content_visible,
            )
            input_items = _input_display_items(
                row, input_summary, [], edit_summary=edit_summary
            )
            seen = {item.resource_id for item in input_items if item.resource_id}
            for target in targets[:5]:
                if target.resource_id in seen:
                    continue
                input_items.append(
                    McpChatDisplayItem(
                        key="target_artifact",
                        kind="artifact",
                        text=target.title,
                        resource_kind=target.resource_kind,
                        resource_id=target.resource_id,
                        navigation_path=target.navigation_path,
                    )
                )
            presentation = _input_presentation(row.tool_name, input_summary)
            headline = _call_headline(row, None, input_summary)
        else:
            input_summary = _structural_input_summary(row.input_summary)
            edit_summary = _edit_summary(
                row.tool_name, row.input_summary, None, structural=True
            )
            input_items = _structural_display_items(input_summary, edit_summary)
            presentation = _safe_tool_presentation(row.tool_name)
            headline = row.tool_name.replace("_", " ")

        result_items = (
            _result_display_items(row, [], row.result_summary) if is_caller else []
        )
        return McpChatTimelineItem(
            **presentation.model_dump(),
            **common,
            headline=headline,
            input_summary=input_summary,
            input_availability=_input_availability(input_summary, input_items),
            input_display_items=input_items,
            result_summary=row.result_summary if is_caller else None,
            result_availability=(
                ("available" if result_items else "status_only")
                if is_caller
                else "redacted"
            ),
            result_display_items=result_items,
            redaction_reason=None if is_caller else "error_restricted",
            edit_summary=edit_summary,
            error_details=_stored_error_details(
                row, shape_only=not content_visible
            ),
            linked_verbs=[],
            linked_artifacts=[],
        )

    def _session_diffs(
        self,
        rows: list[MCPToolCall],
        gate: _ReaderGate,
        *,
        blocked: set[UUID],
    ) -> dict[UUID, McpChatSessionDiff]:
        """Which rows offer a cumulative diff, computed now and never stored.

        A submit row diffs the version it submitted against that version's
        base. While an edit session is still open, its latest edit row in
        this chat diffs the open editing row against its base. Only
        successful calls the reader can see qualify.
        """

        candidates: dict[UUID, tuple[UUID, bool]] = {}
        latest_edit: dict[UUID, UUID] = {}
        for row in rows:
            if row.id in blocked or row.result_status in ERROR_STATUSES:
                continue
            version_id = _edited_version_id(row)
            if version_id is None:
                continue
            if row.tool_name == "submit_context_edit_for_review":
                candidates[row.id] = (version_id, False)
            elif row.tool_name in _SESSION_EDIT_TOOLS:
                latest_edit[version_id] = row.id
        for version_id, row_id in latest_edit.items():
            candidates[row_id] = (version_id, True)
        diffs: dict[UUID, McpChatSessionDiff] = {}
        for row_id, (version_id, open_only) in candidates.items():
            diff = gate.session_diff(version_id, open_only=open_only)
            if diff is not None:
                diffs[row_id] = diff
        return diffs

    def purge_chat_previews(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
        actor: ActorContext,
    ) -> McpChatPreviewPurgeResult:
        """Delete the edit previews one chat's calls kept (owner retention).

        Digests, sizes and receipts' counts stay: they are the audit record
        of what happened. Only the text excerpts go, and the deletion itself
        is audited.
        """

        chat = self._get_chat(session, workspace_id, chat_session_id)
        purged = sum(
            1 for row in self._tool_calls_for_chat(session, chat) if _purge_row(row)
        )
        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="mcp_chat_session.previews_purged",
            target_kind="mcp_chat_session",
            target_id=chat.id,
            payload={"purged_call_count": purged},
            event_summary="MCP chat edit previews deleted.",
        )
        session.flush()
        return McpChatPreviewPurgeResult(
            chat_session_id=chat.id,
            purged_call_count=purged,
            audit_event_id=audit_event.id,
        )

    def _tool_call_query(self, session: Session, chat: McpChatSession):
        """Ordered select of this chat's calls, or None when it has none.

        Shared so the timeline and the single-row summary lookup cannot drift
        on which calls belong to a chat.
        """

        mcp_session_uuid = mcp_session_trace_uuid(chat.mcp_session_id)
        # No client_id filter: the conversation token already identifies the
        # chat uniquely per workspace, and a conversation continued through a
        # second client identity must keep the first client's calls in the
        # timeline (tool_call_count includes them).
        filters = [
            MCPToolCall.workspace_id == chat.workspace_id,
        ]
        if mcp_session_uuid is None:
            # Non-UUID session token: recover the chat's calls from the per-call
            # edge table so EVERY call that touched an artifact is included, not
            # just the latest per rollup (T9b-FU1, consistent with _timeline).
            linked_call_ids = session.scalars(
                select(McpChatToolCallArtifact.mcp_tool_call_id)
                .where(McpChatToolCallArtifact.chat_session_id == chat.id)
                .distinct()
            ).all()
            if not linked_call_ids:
                return None
            filters.append(MCPToolCall.id.in_(linked_call_ids))
        else:
            filters.append(MCPToolCall.mcp_session_id == mcp_session_uuid)
        return (
            select(MCPToolCall)
            .where(and_(*filters))
            .order_by(MCPToolCall.started_at, MCPToolCall.id)
        )

    def _tool_calls_for_chat(
        self,
        session: Session,
        chat: McpChatSession,
    ) -> list[MCPToolCall]:
        stmt = self._tool_call_query(session, chat)
        if stmt is None:
            return []
        return session.scalars(stmt).all()

    def _intent(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None,
        *,
        redacted_call_ids: set[UUID] | None = None,
    ) -> tuple[str | None, str | None]:
        """The client's own statement of what it was doing, if it made one.

        Read from the first call this reader is allowed to describe, so a chat
        whose opening move touched a hidden page contributes nothing here.
        """

        first_call = self._first_tool_call(
            session, chat, actor, redacted_call_ids=redacted_call_ids
        )
        if first_call is None:
            return None, None
        if not _ReaderGate(
            self, session, chat.workspace_id, actor
        ).call_content_visible(first_call, chat):
            return None, None
        found = _intent_from_input(first_call.tool_name, first_call.input_summary)
        if found is None:
            return None, None
        return found

    def _redacted_call_ids(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None,
    ) -> set[UUID]:
        """Calls whose primary target this reader may not see.

        A call's arguments were captured verbatim, so they still name the
        target's title, claim text or body. Anything derived from them — the
        timeline row, and equally the list card's title and summary — has to
        consult this first. It used to be decided inline in `_timeline` only,
        which is exactly why the summary path leaked.

        A hidden search match does not count: it is one result among several,
        and the timeline drops just that result. Failed calls are absent here
        because they never carry an edge; every label derivation already
        ignores them by status, and the timeline gates them on the targets
        their input names (``_error_call_item``).
        """

        if actor is None:
            return set()
        hidden_target: dict[tuple[str, UUID], bool] = {}
        hidden: set[UUID] = set()
        for tool_call_id, verb, resource_kind, resource_id in list_call_artifact_edges(
            session, workspace_id=chat.workspace_id, chat_session_id=chat.id
        ):
            if verb == McpChatArtifactVerb.SEARCH_MATCH.value:
                continue
            key = (resource_kind, resource_id)
            if key not in hidden_target:
                title, _path = self._artifact_title_and_path(
                    session, resource_kind, resource_id, actor, chat.workspace_id
                )
                hidden_target[key] = title is None
            if hidden_target[key]:
                hidden.add(tool_call_id)
        return hidden

    def _first_tool_call(
        self,
        session: Session,
        chat: McpChatSession,
        actor: ActorContext | None = None,
        *,
        redacted_call_ids: set[UUID] | None = None,
    ) -> MCPToolCall | None:
        """The earliest call this reader is allowed to describe.

        Selects one row rather than materializing every call in the chat:
        `input_summary` holds the full arguments, `body_markdown` included, so
        loading them all to look at the first was megabytes per chat — and the
        list ran it twice for every chat on the page.

        Only the explicit presentation-status allowlist is eligible. This is a
        positive contract: a future unknown status cannot silently promote raw
        inputs into a list card merely because it is not one of today's errors.
        """

        if redacted_call_ids is None:
            redacted_call_ids = self._redacted_call_ids(session, chat, actor)
        stmt = self._tool_call_query(session, chat)
        if stmt is None:
            return None
        stmt = stmt.where(MCPToolCall.result_status.in_(PRESENTATION_ELIGIBLE_STATUSES))
        if redacted_call_ids:
            stmt = stmt.where(MCPToolCall.id.notin_(redacted_call_ids))
        return session.scalars(stmt.limit(1)).first()

    def _artifact_payload(
        self,
        session: Session,
        link: McpChatArtifactLink,
        actor: ActorContext | None = None,
        workspace_id: UUID | None = None,
    ) -> McpChatArtifactLinkPayload | None:
        title, path = self._artifact_title_and_path(
            session,
            link.resource_kind,
            link.resource_id,
            actor,
            workspace_id,
        )
        # For a gated reader, a redacted (hidden/dangling) target drops the whole
        # entry — the artifact id itself must not leak, not just its title/path.
        if actor is not None and title is None:
            return None
        return McpChatArtifactLinkPayload(
            id=link.id,
            resource_kind=link.resource_kind,
            resource_id=link.resource_id,
            artifact_title=title,
            navigation_path=path,
            verb=link.verb,
            first_occurred_at=link.first_occurred_at,
            last_occurred_at=link.last_occurred_at,
            occurrence_count=link.occurrence_count,
            last_mcp_tool_call_id=link.last_mcp_tool_call_id,
        )

    def _artifact_title_and_path(
        self,
        session: Session,
        resource_kind: str,
        resource_id: UUID,
        actor: ActorContext | None = None,
        workspace_id: UUID | None = None,
        *,
        shared_only: bool = False,
    ) -> tuple[str | None, str | None]:
        # ``shared_only`` (the reader-less stored labels) additionally hides a
        # page only its author can see; see _author_private_page.
        #
        # Chat history stores artifact ids captured under the acting client's
        # permissions; a later reader must be re-checked. Workspace ownership is
        # enforced even for the internal write-time projection, then an actor
        # (when present) applies the stricter lifecycle/version visibility gate.
        if workspace_id is None:
            return None, None
        if resource_kind == McpChatArtifactKind.CONTEXT_PAGE.value:
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=resource_id,
            )
            if page is None:
                return None, None
            if actor is not None and not context_page_visible(
                page, actor, workspace_id=workspace_id
            ):
                return None, None
            if shared_only and _author_private_page(page):
                return None, None
            return page.title, f"/context?target={page.id}"
        if resource_kind == McpChatArtifactKind.AGENT_PAGE.value:
            agent_page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=resource_id,
            )
            if agent_page is None:
                return None, None
            if actor is not None and not agent_page_visible(
                agent_page, actor, workspace_id=workspace_id
            ):
                return None, None
            return agent_page.title, f"/agent?target={agent_page.id}"
        if resource_kind == McpChatArtifactKind.LIBRARY_ITEM.value:
            # P5.1: every producer normalizes a LibraryArtifact id to its
            # parent LibraryItem id before writing a library_item-kind link
            # (mcp_trace_capture.py's _library_item_id_for_artifact), so a
            # resource_id under this kind is always a real LibraryItem id.
            source = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=resource_id,
            )
            if source is None:
                return None, None
            if actor is not None and not library_item_visible(
                source, actor, workspace_id=workspace_id
            ):
                return None, None
            return source.title or source.uri, f"/library?target={source.id}"
        if resource_kind == McpChatArtifactKind.CONTEXT_MEDIA_ASSET.value:
            # ContextMediaAsset has no title column; original_filename is the
            # only author-controlled label, and it is already sanitized at
            # write time (context_media_upload.py's _normalized_filename
            # strips control characters and path separators before the row is
            # created), so it is as trustworthy as page.title/source.title
            # above -- both are also free-text and rendered the same way.
            #
            # ContextMediaAsset's primary key is the COMPOSITE
            # (workspace_id, id) -- unlike every other branch above, `id`
            # alone is not unique across workspaces (its three
            # UniqueConstraints are all workspace-scoped), and it is a
            # caller-supplied value (UploadImageInput.marker_uuid, passed
            # straight through as asset_id in context_image_ingest.py). A
            # bare `WHERE id = :resource_id` can therefore match rows in TWO
            # workspaces and raise MultipleResultsFound, so the workspace
            # scope has to be part of the lookup, not a check applied after
            # it. Without a workspace_id there is no safe scoped lookup to
            # run at all, so this fails closed immediately rather than
            # falling through to an ambiguous query.
            if workspace_id is None:
                return None, None
            asset = session.get(
                ContextMediaAsset, {"workspace_id": workspace_id, "id": resource_id}
            )
            if asset is None:
                return None, None
            # Tombstoned mirrors archived: EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
            # and ContextPage.lifecycle_status != "archived" above both hide
            # a target once its current state says it is no longer live,
            # rather than preserving the raw historical fact that a chat once
            # touched it -- that is this codebase's existing answer to
            # "should provenance survive the target's own removal", and nothing
            # about media makes it a special case. A tombstoned asset is also
            # queued for physical blob deletion (context_media_gc_candidate),
            # so its content_url may already be a dead link by the time this
            # renders. Internal callers (actor is None) still see it, same
            # asymmetry as the archived-page check below.
            if actor is not None and asset.association_state == "tombstoned":
                return None, None
            # Page-bound: the asset is only as visible as the page that
            # carries it (same rule as the CONTEXT_PAGE branch above), AND
            # -- P3.1/P3.2 -- as visible as the specific version it was
            # created for. Every asset is page-owned now (no more page-less
            # "session" ownership), but a page being visible at all does not
            # mean every asset registered against it is: page-first creation
            # can register an asset against a private "editing" row on a
            # page that separately has its own visible
            # head (a published version, or a different author's shared
            # draft). Checking only the page here would leak an unshared
            # editing session's upload the same way P2.6 (PageNameIndex/FTS)
            # leaked names/body text through an analogous gap -- confirmed
            # reachable now that page-bound-but-private assets are possible,
            # which they were not before P2/P3 existed.
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=asset.page_id,
            )
            if page is None:
                return None, None
            if actor is not None and not context_page_visible(
                page, actor, workspace_id=workspace_id
            ):
                return None, None
            if shared_only and _author_private_page(page):
                return None, None
            created_for_version = session.get(
                ContextPageVersion, asset.created_for_version_id
            )
            if actor is not None and editing_row_hidden_from_actor(
                created_for_version, actor
            ):
                return None, None
            return asset.original_filename, f"/context?target={page.id}"
        return None, None

    def _client_refs(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> list[McpChatClientRef]:
        clients = session.scalars(
            select(MCPClient)
            .where(MCPClient.workspace_id == workspace_id)
            .order_by(MCPClient.name)
        ).all()
        return [_client_list_ref(client) for client in clients]

    def _get_chat(
        self,
        session: Session,
        workspace_id: UUID,
        chat_session_id: UUID,
    ) -> McpChatSession:
        chat = get_in_workspace(
            session,
            McpChatSession,
            workspace_id=workspace_id,
            resource_id=chat_session_id,
        )
        if chat is None:
            raise ReadNotFoundError("MCP chat not found")
        return chat


class _ResolvedTarget(NamedTuple):
    resource_kind: str
    resource_id: UUID
    title: str
    navigation_path: str | None


class _ReaderGate:
    """One reader's visibility answers for a single chat projection.

    Every per-call decision in the detail view (link visibility, a failed
    call's named targets, whether edit previews may be shown, the session
    diff) goes through this object, so one request asks each question about
    each row once and the detail and export surfaces cannot disagree.
    ``actor is None`` is the trusted internal read: workspace scope still
    applies, visibility does not.
    """

    def __init__(
        self,
        service: McpChatService,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
    ) -> None:
        self.service = service
        self.session = session
        self.workspace_id = workspace_id
        self.actor = actor
        self._titles: dict[tuple[str, UUID], tuple[str | None, str | None]] = {}
        self._versions: dict[UUID, ContextPageVersion | None] = {}
        self._input_version_candidates: dict[UUID, ContextPageVersion] = {}
        self._targets: dict[tuple[str, UUID], _ResolvedTarget | None] = {}
        self._discarded: dict[UUID, bool] = {}

    def title_and_path(
        self, resource_kind: str, resource_id: UUID
    ) -> tuple[str | None, str | None]:
        key = (resource_kind, resource_id)
        if key not in self._titles:
            self._titles[key] = self.service._artifact_title_and_path(
                self.session, resource_kind, resource_id, self.actor, self.workspace_id
            )
        return self._titles[key]

    def is_chat_owner(self, chat: McpChatSession) -> bool:
        return (
            self.actor is not None
            and self.actor.user_id is not None
            and chat.owner_user_id == self.actor.user_id
        )

    def visible_version(self, version_id: UUID) -> ContextPageVersion | None:
        """The Context version, if the reader may read both it and its page.

        A page can be public while one author's editing row on it is private,
        so a page-level answer is not enough for anything a call wrote into a
        specific version.
        """

        if version_id not in self._versions:
            version = get_in_workspace(
                self.session,
                ContextPageVersion,
                workspace_id=self.workspace_id,
                resource_id=version_id,
            )
            visible = (
                version is not None
                and (
                    self.actor is None
                    or context_version_is_visible(version, self.actor)
                )
                and self.title_and_path(
                    McpChatArtifactKind.CONTEXT_PAGE.value, version.page_id
                )[0]
                is not None
            )
            self._versions[version_id] = version if visible else None
        return self._versions[version_id]

    def prime_input_versions(self, rows: list[MCPToolCall]) -> None:
        """Read missing version labels in bounded batches, without bodies."""
        wanted: set[UUID] = set()
        for row in rows:
            for field in _CONTEXT_VERSION_ID_FIELDS:
                if _target_kind_for_field(row.tool_name, field) != "context_version":
                    continue
                version_id = _parse_uuid_value(row.input_summary.get(field))
                if version_id and version_id not in self._versions:
                    wanted.add(version_id)
        ids = list(wanted)
        for start in range(0, len(ids), 500):
            versions = self.session.scalars(
                select(ContextPageVersion)
                .where(
                    ContextPageVersion.workspace_id == self.workspace_id,
                    ContextPageVersion.id.in_(ids[start : start + 500]),
                )
                .options(
                    load_only(
                        ContextPageVersion.id,
                        ContextPageVersion.page_id,
                        ContextPageVersion.version_no,
                        ContextPageVersion.version_status,
                        ContextPageVersion.created_by_actor_kind,
                        ContextPageVersion.created_by_actor_id,
                    )
                )
            ).all()
            self._input_version_candidates.update(
                (version.id, version) for version in versions
            )

    def input_version_items(self, row: MCPToolCall) -> list[McpChatDisplayItem]:
        """Reuse Context versions already loaded by the reader's gate.

        Only use actual input references, never a created/result version or
        the page's latest version. Editing revisions remain captured inputs;
        reading today's mutable revision would misdescribe a historical call.
        This per-call step never queries. Batched candidates also require a
        previously checked page title and the existing version visibility rule.
        """
        items: list[McpChatDisplayItem] = []
        for field, key in (
            ("version_id", "input_version"),
            ("base_version_id", "base_version"),
            ("editing_version_id", "editing_version"),
            ("cited_context_page_version_id", "cited_version"),
        ):
            if _target_kind_for_field(row.tool_name, field) != "context_version":
                continue
            version_id = _parse_uuid_value(row.input_summary.get(field))
            version = (
                self._versions.get(version_id)
                if version_id in self._versions
                else self._input_version_candidates.get(version_id)
            )
            if version is not None and (
                not self._titles.get(("context_page", version.page_id), (None, None))[0]
                or (
                    self.actor is not None
                    and not context_version_is_visible(version, self.actor)
                )
            ):
                version = None
            if version is not None:
                items.append(
                    McpChatDisplayItem(
                        key=key,
                        kind="text",
                        text=f"v{version.version_no}",
                        resource_id=version.id,
                    )
                )
        return items

    def call_content_visible(self, row: MCPToolCall, chat: McpChatSession) -> bool:
        """Whether the reader may see what a context-edit call wrote.

        A page can be visible while the edit went into its author's private
        editing row, so this is decided by the version the call wrote into.
        When capture did not record that version (older calls, and a failed
        whole-body update, which names none), only the caller -- the chat's
        owner -- may see the content.
        """

        if self.actor is None or row.tool_name not in CONTEXT_EDIT_TOOLS:
            return True
        version_id = _edited_version_id(row)
        if version_id is not None:
            return self.visible_version(version_id) is not None
        return self.is_chat_owner(chat)

    def input_target(self, kind: str, target_id: UUID) -> _ResolvedTarget | None:
        key = (kind, target_id)
        if key not in self._targets:
            self._targets[key] = self._resolve_input_target(kind, target_id)
        return self._targets[key]

    def _resolve_input_target(
        self, kind: str, target_id: UUID
    ) -> _ResolvedTarget | None:
        resource = self._input_target_resource(kind, target_id)
        if resource is None:
            return None
        resource_kind, resource_id = resource
        title, path = self.title_and_path(resource_kind, resource_id)
        if title is None:
            return None
        return _ResolvedTarget(resource_kind, resource_id, title, path)

    def _input_target_resource(
        self, kind: str, target_id: UUID
    ) -> tuple[str, UUID] | None:
        context_page = McpChatArtifactKind.CONTEXT_PAGE.value
        if kind == "context_page":
            return context_page, target_id
        if kind == "context_version":
            version = self.visible_version(target_id)
            return (context_page, version.page_id) if version is not None else None
        if kind == "context_claim":
            claim = get_in_workspace(
                self.session,
                ContextClaim,
                workspace_id=self.workspace_id,
                resource_id=target_id,
            )
            version = (
                self.visible_version(claim.page_version_id)
                if claim is not None
                else None
            )
            return (context_page, version.page_id) if version is not None else None
        if kind == "agent_page":
            return McpChatArtifactKind.AGENT_PAGE.value, target_id
        if kind == "agent_version":
            agent_version = get_in_workspace(
                self.session,
                AgentPageVersion,
                workspace_id=self.workspace_id,
                resource_id=target_id,
            )
            agent_page = (
                get_in_workspace(
                    self.session,
                    AgentPage,
                    workspace_id=self.workspace_id,
                    resource_id=agent_version.agent_page_id,
                )
                if agent_version is not None
                else None
            )
            if agent_page is None or (
                self.actor is not None
                and not agent_version_is_visible(agent_page, agent_version, self.actor)
            ):
                return None
            return McpChatArtifactKind.AGENT_PAGE.value, agent_page.id
        if kind == "library_item":
            return McpChatArtifactKind.LIBRARY_ITEM.value, target_id
        if kind in {"library_item_version", "library_artifact"}:
            version_id: UUID | None = target_id
            if kind == "library_artifact":
                artifact = get_in_workspace(
                    self.session,
                    LibraryArtifact,
                    workspace_id=self.workspace_id,
                    resource_id=target_id,
                )
                version_id = (
                    artifact.library_item_version_id if artifact is not None else None
                )
            library_version = (
                get_in_workspace(
                    self.session,
                    LibraryItemVersion,
                    workspace_id=self.workspace_id,
                    resource_id=version_id,
                )
                if version_id is not None
                else None
            )
            if library_version is None:
                return None
            return (
                McpChatArtifactKind.LIBRARY_ITEM.value,
                library_version.library_item_id,
            )
        # An id this module cannot classify cannot be shown to be visible.
        return None

    def is_readers_discarded_page(self, page_id: UUID) -> bool:
        """Whether this is the reader's own page, never published, discarded.

        Discarding the only editing session of a never-published page archives
        it, which hides it from everyone -- including the author reading their
        own chat. That author (who must also own the chat, so every call on
        the row is theirs) may still see that their calls went somewhere; they
        get a "discarded edit" marker, never the page's title or id.
        """

        if self.actor is None:
            return False
        if page_id not in self._discarded:
            self._discarded[page_id] = self._readers_discarded_page(page_id)
        return self._discarded[page_id]

    def _readers_discarded_page(self, page_id: UUID) -> bool:
        assert self.actor is not None
        page = get_in_workspace(
            self.session,
            ContextPage,
            workspace_id=self.workspace_id,
            resource_id=page_id,
        )
        if (
            page is None
            or page.lifecycle_status != "archived"
            or page.current_published_version_id is not None
        ):
            return False
        return bool(page.versions) and all(
            version.version_status == "discarded"
            and version.created_by_actor_kind == self.actor.actor_kind
            and version.created_by_actor_id == self.actor.actor_id
            for version in page.versions
        )

    def session_diff(
        self, version_id: UUID, *, open_only: bool
    ) -> McpChatSessionDiff | None:
        version = self.visible_version(version_id)
        if version is None or (open_only and version.version_status != "editing"):
            return None
        if version.parent_version_id is None:
            return McpChatSessionDiff(
                page_id=version.page_id,
                from_version_id=None,
                to_version_id=version.id,
            )
        parent = self.visible_version(version.parent_version_id)
        if parent is None or parent.page_id != version.page_id:
            return None
        return McpChatSessionDiff(
            page_id=version.page_id,
            from_version_id=parent.id,
            to_version_id=version.id,
        )


def _author_private_page(page: ContextPage) -> bool:
    """A page whose every version is author-private (open or discarded).

    Until its first submit, a page-first page's title on the aggregate is
    its author's private text. Pages with no versions at all are not
    counted: nothing about them is private to anyone.
    """

    versions = page.versions
    return bool(versions) and all(
        version.version_status in PRIVATE_VERSION_STATUSES for version in versions
    )


def purge_previews_for_page(
    session: Session,
    workspace_id: UUID,
    page_id: UUID,
    *,
    version_id: UUID | None = None,
) -> int:
    """Null the edit previews MCP calls kept for one Context page.

    ``discard_context_edit`` calls this with the discarded editing row: the
    author threw that text away, so the excerpts of it stored beside each
    call's digests go too. Pass ``version_id`` to limit the purge to calls
    that wrote into that version; omit it to purge every edit call on the
    page. A call is matched by the page its input names or, for a create,
    the page its response named (kept in ``result_detail``), so failed calls
    -- which have no artifact edge -- are covered too. Returns how many calls
    changed.
    """

    rows = list_calls_naming_page(
        session,
        workspace_id=workspace_id,
        page_id=page_id,
        tool_names=CONTEXT_EDIT_TOOLS,
    )
    purged = 0
    for row in rows:
        if version_id is not None and _edited_version_id(row) != version_id:
            continue
        if _purge_row(row):
            purged += 1
    session.flush()
    return purged


def _purge_row(row: MCPToolCall) -> bool:
    changed = False
    input_summary, cleared = null_previews(row.input_summary)
    if cleared:
        row.input_summary = input_summary
        changed = True
    detail = row.result_detail
    if isinstance(detail, dict) and isinstance(detail.get("edit_receipt"), dict):
        receipt, receipt_cleared = null_previews(detail["edit_receipt"])
        if receipt_cleared:
            row.result_detail = {
                **detail,
                "edit_receipt": receipt,
                _RESULT_DETAIL_PURGED_FIELD: True,
            }
            changed = True
    return changed


def _edited_version_id(row: MCPToolCall) -> UUID | None:
    """The Context version a context-edit call wrote into, when recorded.

    Commands that name their editing row take it from the input; create and
    whole-body update learn it from the response, which capture keeps in
    ``result_detail``. Calls captured before that existed return ``None``.
    """

    if row.tool_name not in CONTEXT_EDIT_TOOLS:
        return None
    input_summary = row.input_summary if isinstance(row.input_summary, dict) else {}
    detail = row.result_detail if isinstance(row.result_detail, dict) else {}
    from_input = (
        _parse_uuid_value(input_summary.get("version_id"))
        if row.tool_name in _VERSION_NAMING_EDIT_TOOLS
        else None
    )
    return from_input or _parse_uuid_value(detail.get("version_id"))


def _parse_uuid_value(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _is_derived_input_key(key: str) -> bool:
    return (
        key.endswith(("_digest", PREVIEW_SUFFIX))
        or key == "unknown_fields"
        or key == PREVIEW_BUDGET_EXHAUSTED_FIELD
    )


def _input_target_refs(tool_name: str, input_summary: Any) -> list[tuple[str, UUID]]:
    """Every UUID a captured input names, classified by what it could name.

    Scans nested revisions and citation lists too. A UUID under a field this
    module does not know is an ``unknown`` target, which can never be shown
    to be visible -- so an input grows a new id field closed, not open. An
    input too large to scan fully is treated the same way.
    """

    refs: list[tuple[str, UUID]] = []
    overflow = False

    def visit(key: str | None, value: Any, depth: int) -> None:
        nonlocal overflow
        if overflow:
            return
        if depth > _TARGET_SCAN_MAX_DEPTH or len(refs) >= _TARGET_SCAN_MAX_REFS:
            overflow = True
            return
        if isinstance(value, list):
            if len(value) > _TARGET_SCAN_MAX_ITEMS:
                overflow = True
                return
            for item in value:
                visit(key, item, depth + 1)
            return
        if isinstance(value, dict):
            typed_keys: set[str] = set()
            for id_field, type_field in _TYPED_TARGET_FIELDS:
                target_id = _parse_uuid_value(value.get(id_field))
                if target_id is not None:
                    typed_keys.add(id_field)
                    type_value = value.get(type_field)
                    refs.append(
                        (
                            _TYPED_TARGET_KINDS.get(type_value, _UNKNOWN_TARGET)
                            if isinstance(type_value, str)
                            else _UNKNOWN_TARGET,
                            target_id,
                        )
                    )
            for child_key, child in value.items():
                if (
                    not isinstance(child_key, str)
                    or child_key in typed_keys
                    or _is_derived_input_key(child_key)
                ):
                    continue
                visit(child_key, child, depth + 1)
            return
        target_id = _parse_uuid_value(value)
        if target_id is None or key in _NON_TARGET_ID_FIELDS:
            return
        refs.append((_target_kind_for_field(tool_name, key), target_id))

    visit(None, input_summary, 0)
    if overflow:
        return [(_UNKNOWN_TARGET, UUID(int=0))]
    return refs


def _target_kind_for_field(tool_name: str, key: str | None) -> str:
    if key in _CONTEXT_PAGE_ID_FIELDS:
        return "context_page"
    if key == "base_version_id" and tool_name in _AGENT_VERSION_TOOLS:
        return "agent_version"
    if key in _CONTEXT_VERSION_ID_FIELDS:
        return "context_version"
    return _ID_FIELD_TARGET_KINDS.get(key or "", _UNKNOWN_TARGET)


def _input_availability(
    input_summary: dict[str, Any], items: list[McpChatDisplayItem]
) -> str:
    if items:
        return "available"
    return "not_available" if input_summary else "not_applicable"


def _target_hidden_item(
    row: MCPToolCall,
    common: dict[str, Any],
    *,
    structural: bool = False,
) -> McpChatTimelineItem:
    """A row whose target the reader cannot see: its shape, nothing more.

    ``structural`` keeps what a failed call's shape says without naming or
    quoting its target -- the fields it sent, the operation kinds, revision
    numbers -- next to the error code. A successful call on a hidden target
    keeps nothing, as before.
    """

    input_summary = _structural_input_summary(row.input_summary) if structural else {}
    edit_summary = (
        _edit_summary(row.tool_name, row.input_summary, None, structural=True)
        if structural
        else None
    )
    return McpChatTimelineItem(
        **_safe_tool_presentation(row.tool_name).model_dump(),
        **common,
        # The headline must not replay input_summary either; fall back to the
        # bare (non-sensitive) tool name.
        headline=row.tool_name.replace("_", " "),
        input_summary=input_summary,
        input_availability="redacted",
        input_display_items=(
            _structural_display_items(input_summary, edit_summary)
            if structural
            else []
        ),
        result_summary=None,
        result_availability="redacted",
        result_display_items=[],
        redaction_reason="target_hidden",
        edit_summary=edit_summary,
        error_details=(
            _stored_error_details(row, shape_only=True) if structural else []
        ),
        linked_verbs=[],
        linked_artifacts=[],
    )


def _visible_call_item(
    row: MCPToolCall,
    common: dict[str, Any],
    *,
    linked_artifacts: list[McpChatTimelineArtifact],
    linked_verbs: list[str],
    primary_artifact: tuple[str, str | None, str] | None,
    content_visible: bool,
    session_diff: McpChatSessionDiff | None,
) -> McpChatTimelineItem:
    """A call the reader may see. Without ``content_visible`` -- the version
    it wrote into is another author's private row -- every field below is
    built from the allowlist projections, never from the raw capture."""

    if content_visible:
        input_summary = row.input_summary
        receipt = _stored_receipt(row)
        result_summary = row.result_summary
    else:
        input_summary = _version_hidden_input(row.input_summary)
        receipt = _version_hidden_receipt(_stored_receipt(row))
        # Success messages are generic today; anything else could quote the
        # private edit, so only a bare status echo survives.
        result_summary = (
            row.result_summary
            if isinstance(row.result_summary, str)
            and _result_summary_is_status_echo(row.result_summary, row.result_status)
            else None
        )
    edit_summary = _edit_summary(
        row.tool_name,
        input_summary,
        receipt,
        purged=_result_detail_purged(row),
        withheld=not content_visible,
    )
    input_items = _input_display_items(
        row, input_summary, linked_artifacts, edit_summary=edit_summary
    )
    result_items = _result_display_items(row, linked_artifacts, result_summary)
    presentation = _call_presentation(row, primary_artifact, input_summary)
    return McpChatTimelineItem(
        **presentation.model_dump(),
        **common,
        headline=_call_headline(row, primary_artifact, input_summary),
        input_summary=input_summary,
        input_availability=_input_availability(input_summary, input_items),
        input_display_items=input_items,
        result_summary=result_summary,
        result_availability=(
            "available"
            if result_items
            else "status_only"
            if row.result_status
            else "not_available"
        ),
        result_display_items=result_items,
        result_detail=_result_detail_payload(receipt),
        edit_summary=edit_summary,
        session_diff=session_diff,
        linked_verbs=linked_verbs,
        linked_artifacts=linked_artifacts,
    )


def _discarded_edit_item(
    row: MCPToolCall, common: dict[str, Any]
) -> McpChatTimelineItem:
    """The reader's own call on a page they discarded before publishing.

    The author threw the page and its text away, so the row keeps only its
    shape and a "discarded edit" marker -- no title, headings or excerpts, and
    no route to a page that no longer exists for anyone.
    """

    input_summary = _structural_input_summary(row.input_summary)
    edit_summary = _edit_summary(
        row.tool_name, row.input_summary, None, structural=True
    )
    input_items = _structural_display_items(input_summary, edit_summary)
    return McpChatTimelineItem(
        **_safe_tool_presentation(row.tool_name).model_dump(),
        **common,
        headline=row.tool_name.replace("_", " "),
        input_summary=input_summary,
        input_availability=_input_availability(input_summary, input_items),
        input_display_items=input_items,
        # A success message may name the page; it goes with the title.
        result_summary=None,
        result_availability="available",
        result_display_items=[
            McpChatDisplayItem(key="discarded_edit", kind="status", text="discarded")
        ],
        edit_summary=edit_summary,
        linked_verbs=[],
        linked_artifacts=[],
    )


def _structural_input_summary(input_summary: Any) -> dict[str, Any]:
    """What a call sent, without any value that could name or quote a target."""

    if not isinstance(input_summary, dict):
        return {}
    structural: dict[str, Any] = {
        "fields": sorted(
            key
            for key in input_summary
            if isinstance(key, str) and _INPUT_FIELD_NAME_RE.fullmatch(key)
        )
    }
    revision = input_summary.get("expected_editing_revision")
    if type(revision) is int:
        structural["expected_editing_revision"] = revision
    operations = input_summary.get("operations")
    if isinstance(operations, list):
        structural["operations"] = [
            {"op": operation["op"]}
            if isinstance(operation, dict)
            and operation.get("op") in PATCH_OPERATION_KINDS
            else {}
            for operation in operations
        ]
    return structural


def _structural_display_items(
    structural: dict[str, Any], edit_summary: McpChatEditSummary | None
) -> list[McpChatDisplayItem]:
    items: list[McpChatDisplayItem] = []
    if edit_summary is not None and edit_summary.operation_counts:
        items.append(
            McpChatDisplayItem(
                key="edit_operations",
                kind="text",
                text=_operation_counts_text(edit_summary.operation_counts),
            )
        )
    revision = structural.get("expected_editing_revision")
    if type(revision) is int:
        items.append(
            McpChatDisplayItem(key="editing_revision", kind="text", text=str(revision))
        )
    return items


def _stored_error_details(
    row: MCPToolCall, *, shape_only: bool
) -> list[McpChatErrorDetail]:
    """A failed call's structural error details, as capture kept them.

    ``shape_only`` is for a reader who cannot see the target: which codes and
    which operations stay (the call's shape), the schema path and line ranges
    -- positions in the target's text -- do not.
    """

    detail = row.result_detail if isinstance(row.result_detail, dict) else {}
    stored = detail.get("error_details")
    projected: list[McpChatErrorDetail] = []
    for entry in stored if isinstance(stored, list) else []:
        try:
            item = McpChatErrorDetail.model_validate(entry)
        except ValidationError:
            continue
        if shape_only:
            item = item.model_copy(
                update={
                    "field": None,
                    "conflict": (
                        item.conflict.model_copy(
                            update={"left_lines": None, "right_lines": None}
                        )
                        if item.conflict is not None
                        else None
                    ),
                    # Positions in text this reader cannot see.
                    "candidates": [],
                }
            )
        projected.append(item)
    return projected


def _stored_receipt(row: MCPToolCall) -> dict[str, Any] | None:
    detail = row.result_detail if isinstance(row.result_detail, dict) else {}
    receipt = detail.get("edit_receipt")
    return receipt if isinstance(receipt, dict) else None


# What of a context-edit call a reader who cannot see the version it wrote
# into may still see. Positive lists: a field not named here is dropped,
# including every field added later.
_VERSION_HIDDEN_INPUT_UUID_FIELDS = ("workspace_id", "page_id")
_VERSION_HIDDEN_RECEIPT_FIELDS = (
    "edit_mode",
    "editing_revision_before",
    "editing_revision_after",
    "chars_added",
    "chars_removed",
    "lines_added",
    "lines_removed",
)
_VERSION_HIDDEN_RECEIPT_OPERATION_FIELDS = (
    "index",
    "op",
    "chars_added",
    "chars_removed",
)


def _version_hidden_input(input_summary: Any) -> dict[str, Any]:
    """The input of a call whose edited version the reader cannot see.

    The page itself is visible (or the row would be shape-only), so its id
    and the workspace stay, with the revision the call expected, whether it
    was a retry, and each operation's kind. Nothing else: no title, tags,
    aliases, headings, block ids, digests or excerpts.
    """

    if not isinstance(input_summary, dict):
        return {}
    projected: dict[str, Any] = {}
    for field in _VERSION_HIDDEN_INPUT_UUID_FIELDS:
        if _parse_uuid_value(input_summary.get(field)) is not None:
            projected[field] = input_summary[field]
    revision = input_summary.get("expected_editing_revision")
    if type(revision) is int:
        projected["expected_editing_revision"] = revision
    if type(input_summary.get("retry_only")) is bool:
        projected["retry_only"] = input_summary["retry_only"]
    operations = input_summary.get("operations")
    if isinstance(operations, list):
        projected["operations"] = [
            {"op": operation["op"]}
            if isinstance(operation, dict)
            and operation.get("op") in PATCH_OPERATION_KINDS
            else {}
            for operation in operations
        ]
    return projected


def _version_hidden_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """A receipt's counts only: no sections, excerpts, lines or block ids."""

    if receipt is None:
        return None
    projected = {
        field: receipt[field]
        for field in _VERSION_HIDDEN_RECEIPT_FIELDS
        if field in receipt
    }
    operations = receipt.get("operations")
    projected["operations"] = [
        {
            field: operation[field]
            for field in _VERSION_HIDDEN_RECEIPT_OPERATION_FIELDS
            if field in operation
        }
        for operation in (operations if isinstance(operations, list) else [])
        if isinstance(operation, dict)
    ]
    return projected


def _result_detail_purged(row: MCPToolCall) -> bool:
    detail = row.result_detail if isinstance(row.result_detail, dict) else {}
    return detail.get(_RESULT_DETAIL_PURGED_FIELD) is True


def _receipt_model(receipt: dict[str, Any] | None) -> McpChatEditReceipt | None:
    if receipt is None:
        return None
    try:
        return McpChatEditReceipt.model_validate(receipt)
    except ValidationError:
        return None


def _result_detail_payload(
    receipt: dict[str, Any] | None,
) -> McpChatResultDetail | None:
    model = _receipt_model(receipt)
    return McpChatResultDetail(edit_receipt=model) if model is not None else None


def _edit_summary(
    tool_name: str,
    input_summary: Any,
    receipt: dict[str, Any] | None,
    *,
    structural: bool = False,
    purged: bool = False,
    withheld: bool = False,
) -> McpChatEditSummary | None:
    """What a write call changed, from its receipt or else from its input.

    ``structural`` keeps only the operation kinds -- the shape a reader may
    see of a call whose target is hidden from them.
    """

    if tool_name not in _EDIT_SUMMARY_TOOLS:
        return None
    if not isinstance(input_summary, dict):
        input_summary = {}
    measured = None if structural else _receipt_model(receipt)
    measured_operations = (
        {operation.index: operation for operation in measured.operations}
        if measured is not None
        else {}
    )
    counts: dict[str, int] = {}
    operations: list[McpChatEditOperation] = []
    raw_operations = input_summary.get("operations")
    for index, raw in enumerate(
        raw_operations if isinstance(raw_operations, list) else []
    ):
        raw = raw if isinstance(raw, dict) else {}
        op = raw.get("op") if raw.get("op") in PATCH_OPERATION_KINDS else "unknown"
        counts[op] = counts.get(op, 0) + 1
        if structural:
            operations.append(McpChatEditOperation(index=index, op=op))
            continue
        measured_operation = measured_operations.get(index)
        if measured_operation is not None:
            chars_added = measured_operation.chars_added
            chars_removed = measured_operation.chars_removed
        else:
            chars_added, chars_removed = _estimated_operation_chars(op, raw)
        operations.append(
            McpChatEditOperation(
                index=index,
                op=op,
                heading_path=(
                    measured_operation.heading_path
                    if measured_operation is not None
                    and measured_operation.heading_path
                    else _address_heading_path(raw)
                ),
                line_range=(
                    measured_operation.line_range
                    if measured_operation is not None
                    else None
                ),
                chars_added=chars_added,
                chars_removed=chars_removed,
                match_kind=(
                    measured_operation.match_kind
                    if measured_operation is not None
                    else None
                ),
                match_similarity=(
                    measured_operation.match_similarity
                    if measured_operation is not None
                    else None
                ),
                previews=_field_previews(raw),
            )
        )
    if structural:
        return McpChatEditSummary(
            source="input", operation_counts=counts, operations=operations
        )
    exhausted = input_summary.get(PREVIEW_BUDGET_EXHAUSTED_FIELD)
    summary_fields: dict[str, Any] = {
        "operation_counts": counts,
        "operations": operations,
        "body_preview": _body_preview(tool_name, input_summary),
        "preview_budget_exhausted": (
            exhausted if type(exhausted) is int and exhausted > 0 else 0
        ),
        "previews_purged": purged or _has_purged_preview(input_summary),
        "previews_withheld": withheld,
    }
    if measured is not None:
        return McpChatEditSummary(
            source="receipt",
            edit_mode=measured.edit_mode,
            changed_sections=measured.changed_sections,
            chars_added=measured.chars_added,
            chars_removed=measured.chars_removed,
            lines_added=measured.lines_added,
            lines_removed=measured.lines_removed,
            first_change=measured.first_change,
            **summary_fields,
        )
    chars_added, chars_removed = _estimated_call_chars(
        tool_name, input_summary, operations
    )
    return McpChatEditSummary(
        source="input",
        edit_mode=_INPUT_EDIT_MODES.get(tool_name),
        chars_added=chars_added,
        chars_removed=chars_removed,
        **summary_fields,
    )


def _digest_chars(raw: dict[str, Any], field: str) -> int | None:
    digest = raw.get(f"{field}_digest")
    if not isinstance(digest, dict):
        return None
    chars = digest.get("chars")
    return chars if type(chars) is int and chars >= 0 else None


def _estimated_operation_chars(
    op: str, raw: dict[str, Any]
) -> tuple[int | None, int | None]:
    """Sizes a patch operation's input implies; ``None`` where it cannot tell.

    Only what the call sent is known: the text a replace removes is known for
    ``replace_text`` (its ``old_text``), never for a section/block/document
    replacement, whose old content was not part of the input.
    """

    if op == "replace_text":
        occurrences = raw.get("expected_occurrences")
        times = occurrences if type(occurrences) is int and occurrences >= 1 else 1
        added = _digest_chars(raw, "new_text")
        removed = _digest_chars(raw, "old_text")
        return (
            added * times if added is not None else None,
            removed * times if removed is not None else None,
        )
    if op == "insert_text":
        return _digest_chars(raw, "new_text"), 0
    if op in {"append_document", "insert_after", "insert_row"}:
        return _digest_chars(raw, "content"), 0
    if op == "insert_section":
        title = _digest_chars(raw, "title")
        content = _digest_chars(raw, "content")
        if title is None and content is None:
            return None, 0
        return (title or 0) + (content or 0), 0
    if op in {"replace_document", "replace_section", "replace_block"}:
        return _digest_chars(raw, "content"), None
    if op in {"delete_section", "delete_block"}:
        return 0, None
    return None, None


def _estimated_call_chars(
    tool_name: str,
    input_summary: dict[str, Any],
    operations: list[McpChatEditOperation],
) -> tuple[int | None, int | None]:
    if tool_name == "patch_context_edit":
        if not operations:
            return None, None
        added = [operation.chars_added for operation in operations]
        removed = [operation.chars_removed for operation in operations]
        return (
            None if None in added else sum(value or 0 for value in added),
            None if None in removed else sum(value or 0 for value in removed),
        )
    if tool_name == "create_context_page":
        return _digest_chars(input_summary, "body_md"), 0
    return None, None


def _address_heading_path(raw: dict[str, Any]) -> list[str] | None:
    address = raw.get("at")
    if not isinstance(address, dict):
        return None
    heading_path = address.get("heading_path")
    if isinstance(heading_path, str):
        heading_path = [heading_path]
    if not isinstance(heading_path, list):
        return None
    parts = [
        _truncate_graphemes(text, TITLE_MAX_CHARS)
        for part in heading_path
        if (text := _sanitize_display_text(part)) is not None
    ]
    return parts or None


def _preview_model(field: str, value: Any) -> McpChatTextPreview:
    """``None`` in storage means the preview was deleted; keep the field."""

    if not isinstance(value, dict):
        return McpChatTextPreview(field=field, text=None)
    text = value.get("text")
    return McpChatTextPreview(
        field=field,
        text=text if isinstance(text, str) else None,
        truncated=value.get("truncated") is True,
    )


def _field_previews(raw: dict[str, Any]) -> list[McpChatTextPreview]:
    previews: list[McpChatTextPreview] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not key.endswith(PREVIEW_SUFFIX):
            continue
        field = key.removesuffix(PREVIEW_SUFFIX)
        if field in _PREVIEWED_OPERATION_FIELDS:
            previews.append(_preview_model(field, value))
    return previews


def _body_preview(
    tool_name: str, input_summary: dict[str, Any]
) -> McpChatTextPreview | None:
    containers: list[dict[str, Any]] = [input_summary]
    if tool_name == "submit_revision_for_review":
        containers = [
            revision
            for revision in (input_summary.get("update"), input_summary.get("create"))
            if isinstance(revision, dict)
        ]
    body_field = _BODY_PREVIEW_FIELDS.get(tool_name)
    if body_field is None:
        return None
    key = f"{body_field}{PREVIEW_SUFFIX}"
    for container in containers:
        if key in container:
            return _preview_model(body_field, container[key])
    return None


def _has_purged_preview(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_purged_preview(item) for item in value)
    if not isinstance(value, dict):
        return False
    return any(
        (isinstance(key, str) and key.endswith(PREVIEW_SUFFIX) and item is None)
        or _has_purged_preview(item)
        for key, item in value.items()
    )


def _operation_counts_text(counts: dict[str, int]) -> str:
    return ", ".join(
        f"{op} {_MULTIPLICATION_SIGN}{count}" for op, count in counts.items()
    )


def _sections_text(edit_summary: McpChatEditSummary) -> str | None:
    sections = list(edit_summary.changed_sections)
    if not sections:
        for operation in edit_summary.operations:
            if operation.heading_path and operation.heading_path not in sections:
                sections.append(operation.heading_path)
    if not sections:
        return None
    return "; ".join(f" {_SECTION_SEPARATOR} ".join(path) for path in sections)


def _chars_text(edit_summary: McpChatEditSummary) -> str | None:
    parts = []
    if edit_summary.chars_added is not None:
        parts.append(f"+{edit_summary.chars_added}")
    if edit_summary.chars_removed is not None:
        parts.append(f"{_MINUS_SIGN}{edit_summary.chars_removed}")
    return " ".join(parts) or None


def _client_ref(client: MCPClient | None) -> McpChatClientRef:
    if client is None:
        return McpChatClientRef(
            id=UUID("00000000-0000-0000-0000-000000000000"),
            name="Unknown MCP client",
            client_type="unknown",
            brand_hint=None,
            scopes=[],
        )
    return McpChatClientRef(
        id=client.id,
        name=_sanitize_client_name(
            client.name,
            max_graphemes=CLIENT_DETAIL_NAME_MAX_GRAPHEMES,
        ),
        client_type=client.client_type,
        brand_hint=client.brand_hint,
        scopes=list(client.scopes),
    )


def _client_list_ref(client: MCPClient | None) -> McpChatClientRef:
    """Minimized client identity for list/provenance response surfaces."""

    detail = _client_ref(client)
    return McpChatClientRef(
        id=detail.id,
        name=_truncate_graphemes(detail.name, CLIENT_LIST_NAME_MAX_GRAPHEMES),
        client_type=detail.client_type,
        brand_hint=detail.brand_hint,
        scopes=[],
    )


def _period_start(period: str):
    now = utc_now()
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _encode_typed_cursor(kind: str, offset: int) -> str:
    return f"{kind}:{max(0, offset)}"


def _parse_typed_cursor(cursor: str | None, kind: str) -> int:
    if cursor is None:
        return 0
    prefix = f"{kind}:"
    if cursor.startswith(prefix):
        return _parse_cursor(cursor[len(prefix) :])
    # Integer cursors from the previous release remain usable as raw offsets.
    return _parse_cursor(cursor)


def _parse_uuid(value: str | UUID | None) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _as_utc(dt):
    # SQLite round-trips DateTime(timezone=True) as offset-naive; treat a naive
    # value as UTC so comparisons work on both SQLite and Postgres.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _later(left, right):
    # Normalize both sides: SQLite hands the stored value back offset-naive
    # while the incoming trace timestamp is aware, and a bare max() would
    # TypeError. Comparing normalized values keeps ordering, so an
    # out-of-order older trace can never move last_activity_at backwards.
    return max(_as_utc(left), _as_utc(right))


def mcp_session_trace_uuid(value: str | UUID) -> UUID:
    parsed = _parse_uuid(value)
    if parsed is not None:
        return parsed
    return uuid5(NAMESPACE_URL, f"dtxwiki:mcp-session:{value}")


def ensure_chat_session(
    session: Session,
    *,
    workspace_id: UUID,
    client_id: UUID,
    mcp_session_id: str,
    owner_user_id: UUID | None = None,
) -> UUID:
    """Find-or-create the McpChatSession for a conversation token; return its id.

    Used by the write tools so the FIRST write of a brand-new conversation stamps
    a non-NULL ``ChangeSet.origin_chat_session_id`` (chat_owner review routing,
    Codex #10). Trace capture's ``upsert_session_from_tool_call`` later converges
    on the same row (idempotent find-or-create on (workspace_id, mcp_session_id)).
    """
    chat = session.scalar(
        select(McpChatSession).where(
            McpChatSession.workspace_id == workspace_id,
            McpChatSession.mcp_session_id == mcp_session_id,
        )
    )
    if chat is None:
        now = utc_now()
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client_id,
            mcp_session_id=mcp_session_id,
            owner_user_id=owner_user_id,
            started_at=now,
            last_activity_at=now,
            tool_call_count=0,
            write_count=0,
            error_count=0,
            title_user_edited=False,
        )
        try:
            # SAVEPOINT so a unique-violation here rolls back only this INSERT,
            # not the surrounding write tx. add() is inside so the rollback
            # discards the pending row (otherwise autoflush would retry it).
            with session.begin_nested():
                session.add(chat)
                session.flush()
        except IntegrityError:
            # A concurrent first-touch already inserted this
            # (workspace_id, mcp_session_id) row (Postgres READ COMMITTED; SQLite
            # IMMEDIATE serializes writers so this never trips there). Re-read it
            # rather than failing the write (uq_mcp_chat_session_per_workspace).
            if chat in session:
                session.expunge(chat)
            chat = session.scalar(
                select(McpChatSession).where(
                    McpChatSession.workspace_id == workspace_id,
                    McpChatSession.mcp_session_id == mcp_session_id,
                )
            )
            if chat is None:
                raise
            if chat.owner_user_id is None and owner_user_id is not None:
                chat.owner_user_id = owner_user_id
    elif chat.owner_user_id is None and owner_user_id is not None:
        chat.owner_user_id = owner_user_id
    return chat.id


def resolve_effective_principal(
    session: Session,
    *,
    workspace_id: UUID,
    mcp_session_id: str | None,
    fallback_user_id: UUID | None,
) -> UUID | None:
    """Effective principal P for an MCP review caller (T15b §2), tolerant of the
    FIRST call.

    P = owner_user_id of the caller's conversation McpChatSession, used by the
    review MCP surface to evaluate the AI's audience predicate. But the protocol
    runner dispatches BEFORE it persists the trace row, so on the first call of a
    freshly minted conversation the McpChatSession does not exist yet and the
    owner cannot be read — which would make the review queue look empty / a
    revision look not-found even though the caller owns the routed reviews. The
    runner has already proven the token belongs to the caller, and the row will
    shortly be created with ``owner_user_id=fallback_user_id`` (the caller's
    user), so we fall back to it when no row exists yet.

    Once the row EXISTS we honor its recorded owner verbatim: a row with
    ``owner_user_id`` NULL is genuinely owner-less (no audience), so an existing
    owner-less chat never silently adopts a later caller here.
    """
    if not mcp_session_id:
        return fallback_user_id
    row = session.execute(
        select(McpChatSession.owner_user_id).where(
            McpChatSession.workspace_id == workspace_id,
            McpChatSession.mcp_session_id == mcp_session_id,
        )
    ).first()
    if row is None:
        return fallback_user_id
    return row[0]


@dataclass(frozen=True)
class ConversationChatRow:
    owner_user_id: UUID | None
    workspace_id: UUID
    client_id: UUID | None


def conversation_chat_session_id(
    session: Session,
    *,
    workspace_id: UUID,
    mcp_session_id: str,
) -> UUID | None:
    """Resolve a conversation's workspace-scoped chat identity.

    Protocol adapters use this narrow service seam instead of importing the
    persistence model.  The workspace predicate is part of the lookup rather
    than a post-read check so a token can never disclose an ID from another
    workspace plane.
    """

    return session.scalar(
        select(McpChatSession.id).where(
            McpChatSession.workspace_id == workspace_id,
            McpChatSession.mcp_session_id == mcp_session_id,
        )
    )


def fetch_conversation_chats(
    session_factory,
    mcp_session_id: str,
) -> list[ConversationChatRow]:
    """Return ownership/scope fields for chats grouped under a conversation token.

    Keeps McpChatSession model access in the service layer so MCP-layer
    callers (backend.mcp.conversation) never import models directly.
    """
    with session_factory() as session:
        chats = session.scalars(
            select(McpChatSession).where(
                McpChatSession.mcp_session_id == mcp_session_id
            )
        ).all()
        return [
            ConversationChatRow(
                owner_user_id=chat.owner_user_id,
                workspace_id=chat.workspace_id,
                client_id=chat.client_id,
            )
            for chat in chats
        ]


def _anchor_from_input(tool_name: str, input_summary: dict[str, Any]) -> str:
    if tool_name == "search" and input_summary.get("query"):
        return f"Search: {str(input_summary['query'])[:TITLE_MAX_CHARS]}"
    if input_summary.get("title"):
        return str(input_summary["title"])[:TITLE_MAX_CHARS]
    if input_summary.get("claim_text"):
        return str(input_summary["claim_text"])[:TITLE_MAX_CHARS]
    if input_summary.get("target_type"):
        return str(input_summary["target_type"])[:TITLE_MAX_CHARS]
    return tool_name.replace("_", " ")


_ARTIFACT_ACTIONS: dict[str, McpChatAction] = {
    "created": "create",
    "edited": "edit",
    "cited": "cite",
}


def _tool_action(tool_name: str | None) -> McpChatAction:
    if tool_name is None:
        return "other"
    return cast(McpChatAction, TOOL_ACTIONS.get(tool_name, "other"))


def _normalize_presentation_text(value: object) -> str | None:
    safe = _sanitize_display_text(value)
    if safe is None:
        return None
    return _truncate_graphemes(safe, TITLE_MAX_CHARS)


def _sanitize_client_name(value: object, *, max_graphemes: int) -> str:
    safe = _sanitize_display_text(value) or "Unknown MCP client"
    return _truncate_graphemes(safe, max_graphemes)


def _presentation_anchor(
    presentation: McpChatPresentationDescriptor,
) -> str:
    if presentation.subject:
        return presentation.subject
    if presentation.title_source == "target_kind" and presentation.subject_kind:
        return presentation.subject_kind.replace("_", " ")
    if presentation.title_source == "tool" and presentation.primary_tool_name:
        return presentation.primary_tool_name.replace("_", " ")
    return "MCP activity"


def _generic_presentation() -> McpChatPresentationDescriptor:
    return McpChatPresentationDescriptor(
        title_source="generic",
        action="other",
        subject=None,
        subject_kind=None,
        primary_tool_name=None,
    )


def _safe_tool_presentation(tool_name: str) -> McpChatPresentationDescriptor:
    return McpChatPresentationDescriptor(
        # Having an action for a tool is what "we know this tool" means; the
        # descriptor registry itself lives in the MCP layer, which this one
        # may not import.
        title_source="tool" if tool_name in TOOL_ACTIONS else "generic",
        action=_tool_action(tool_name),
        subject=None,
        subject_kind=None,
        primary_tool_name=tool_name,
    )


def _input_presentation(
    tool_name: str, input_summary: dict[str, Any]
) -> McpChatPresentationDescriptor:
    return _allowlisted_input_presentation(
        tool_name, input_summary
    ) or _safe_tool_presentation(tool_name)


def _allowlisted_input_presentation(
    tool_name: str, input_summary: dict[str, Any]
) -> McpChatPresentationDescriptor | None:
    action = _tool_action(tool_name)
    search_field = _SEARCH_INPUT_FIELDS.get(tool_name)
    query = (
        _normalize_presentation_text(input_summary.get(search_field))
        if search_field is not None
        else None
    )
    if query is not None:
        return McpChatPresentationDescriptor(
            title_source="query",
            action=action,
            subject=query,
            subject_kind="query",
            primary_tool_name=tool_name,
        )
    for field_name, subject_kind in _SAFE_TEXT_INPUT_FIELDS.get(tool_name, ()):
        subject = _normalize_presentation_text(input_summary.get(field_name))
        if subject is not None:
            return McpChatPresentationDescriptor(
                title_source="input_text",
                action=action,
                subject=subject,
                subject_kind=subject_kind,
                primary_tool_name=tool_name,
            )
    for field_name in _SAFE_KIND_INPUT_FIELDS.get(tool_name, ()):
        target_kind = _normalize_presentation_text(input_summary.get(field_name))
        if target_kind is not None:
            return McpChatPresentationDescriptor(
                title_source="target_kind",
                action=action,
                subject=None,
                subject_kind=target_kind,
                primary_tool_name=tool_name,
            )
    return None


def _artifact_presentation(
    *,
    tool_name: str | None,
    verb: str,
    resource_kind: str,
    title: str,
) -> McpChatPresentationDescriptor:
    return McpChatPresentationDescriptor(
        title_source="artifact",
        action=_ARTIFACT_ACTIONS.get(verb, _tool_action(tool_name)),
        subject=title,
        subject_kind=resource_kind,
        primary_tool_name=tool_name,
    )


def _call_presentation(
    row: MCPToolCall,
    primary_artifact: tuple[str, str | None, str] | None,
    input_summary: dict[str, Any],
) -> McpChatPresentationDescriptor:
    if primary_artifact is not None:
        verb, title, resource_kind = primary_artifact
        if title:
            return _artifact_presentation(
                tool_name=row.tool_name,
                verb=verb,
                resource_kind=resource_kind,
                title=title,
            )
    return _input_presentation(row.tool_name, input_summary)


def _intent_from_input(
    tool_name: str, input_summary: dict[str, Any]
) -> tuple[str, str] | None:
    """What the client said it was doing, and where that sentence came from.

    MCP delivers a tool name and arguments; the user's own prompt never reaches
    the server, so a chat's intent can never be recovered. What *is* recorded is
    the natural-language field the client had to fill in:
    `submit_context_edit_for_review.reason` is required, `search.query` is the
    literal question asked of the index, and `cite.claim_text` is the sentence
    being supported.

    The source travels with the text because the screen must not let a reader
    mistake an agent's self-description for something they said themselves.
    """

    for field, source in (
        ("reason", "write_reason"),
        ("patch_description", "write_reason"),
    ):
        value = input_summary.get(field)
        normalized = _sanitize_display_text(value)
        if (
            tool_name
            in {
                "write_agent_page_draft",
                "submit_context_edit_for_review",
            }
            and normalized is not None
        ):
            return _truncate_graphemes(normalized, SUMMARY_MAX_CHARS), source
    query = _sanitize_display_text(input_summary.get("query"))
    if tool_name == "search" and query is not None:
        return _truncate_graphemes(query, SUMMARY_MAX_CHARS), "search_query"
    claim = _sanitize_display_text(input_summary.get("claim_text"))
    if tool_name == "cite" and claim is not None:
        return _truncate_graphemes(claim, SUMMARY_MAX_CHARS), "claim_text"
    return None


def _input_display_items(
    row: MCPToolCall,
    input_summary: dict[str, Any],
    linked_artifacts: list[McpChatTimelineArtifact],
    *,
    edit_summary: McpChatEditSummary | None,
) -> list[McpChatDisplayItem]:
    """Project allowlisted call facts; never serialize arbitrary raw arguments.

    ``input_summary`` is the copy this reader may see (edit previews removed
    when the edited version is hidden from them); ``row`` supplies only the
    tool name.
    """

    items: list[McpChatDisplayItem] = []
    seen: set[tuple[str, str]] = set()

    def add_text(key: str, value: Any) -> None:
        text = _sanitize_display_text(value)
        if text is None:
            return
        text = _truncate_graphemes(text, SUMMARY_MAX_CHARS)
        identity = (key, text)
        if identity in seen:
            return
        seen.add(identity)
        items.append(McpChatDisplayItem(key=key, kind="text", text=text))

    search_field = _SEARCH_INPUT_FIELDS.get(row.tool_name)
    if search_field is not None:
        add_text("search_query", input_summary.get(search_field))
    for field_name, subject_kind in _SAFE_TEXT_INPUT_FIELDS.get(row.tool_name, ()):
        add_text(subject_kind, input_summary.get(field_name))
    for field_name, key in _DISPLAY_TEXT_INPUT_FIELDS.get(row.tool_name, ()):
        add_text(key, input_summary.get(field_name))
    for field_name in _SAFE_KIND_INPUT_FIELDS.get(row.tool_name, ()):
        add_text("target_kind", input_summary.get(field_name))
    intent = _intent_from_input(row.tool_name, input_summary)
    if intent is not None:
        text, source = intent
        add_text(source, text)
    if edit_summary is not None:
        if edit_summary.operation_counts:
            add_text(
                "edit_operations",
                _operation_counts_text(edit_summary.operation_counts),
            )
        add_text("edit_sections", _sections_text(edit_summary))
        add_text("edit_chars", _chars_text(edit_summary))

    input_artifact_keys = {
        "referenced": "requested_artifact",
        "edited": "target_artifact",
        "cited": "citation_source",
    }
    for artifact in linked_artifacts[:5]:
        if artifact.artifact_title is None:
            continue
        artifact_key = input_artifact_keys.get(artifact.verb)
        # A newly created artifact is an outcome, not an input. Keeping it out
        # prevents Chats from claiming the new page was requested up front.
        if artifact_key is None:
            continue
        identity = ("artifact", str(artifact.resource_id))
        if identity in seen:
            continue
        seen.add(identity)
        items.append(
            McpChatDisplayItem(
                key=artifact_key,
                kind="artifact",
                text=artifact.artifact_title,
                resource_kind=artifact.resource_kind,
                resource_id=artifact.resource_id,
                navigation_path=artifact.navigation_path,
            )
        )
    return items


def _result_display_items(
    row: MCPToolCall,
    linked_artifacts: list[McpChatTimelineArtifact],
    result_summary: str | None,
) -> list[McpChatDisplayItem]:
    items: list[McpChatDisplayItem] = []
    summary = _sanitize_display_text(result_summary)
    if summary is not None and not _result_summary_is_status_echo(
        summary, row.result_status
    ):
        items.append(
            McpChatDisplayItem(
                key="summary",
                kind="text",
                text=_truncate_graphemes(summary, SUMMARY_MAX_CHARS),
            )
        )
    result_artifact_keys = {
        "created": "created_artifact",
        "edited": "updated_artifact",
        "cited": "cited_artifact",
        "referenced": "read_artifact",
        "search_match": "search_match_artifact",
    }
    for artifact in linked_artifacts[:5]:
        if artifact.artifact_title is None:
            continue
        items.append(
            McpChatDisplayItem(
                key=result_artifact_keys.get(artifact.verb, "result_artifact"),
                kind="artifact",
                text=artifact.artifact_title,
                resource_kind=artifact.resource_kind,
                resource_id=artifact.resource_id,
                navigation_path=artifact.navigation_path,
            )
        )
    return items


def _result_summary_is_status_echo(summary: str, status: str) -> bool:
    normalized = summary.strip().lower().rstrip(".")
    return normalized in {
        "tool call succeeded",
        "tool call failed",
        "tool call completed",
        status.replace("_", " ").strip().lower(),
    }


def _call_headline(
    row: MCPToolCall,
    primary_artifact: tuple[str, str | None, str] | None,
    input_summary: dict[str, Any],
) -> str:
    # Tool-aware, human-readable headline for the timeline summary line (item 20):
    # search -> the query; read/write/cite -> the resolved artifact "Verb: Title"
    # (target_id resolved to a real title); else the generic input anchor.
    # ``input_summary`` is the copy this reader may see, never the raw capture.
    if row.tool_name == "search" and input_summary.get("query"):
        return f"Search: {str(input_summary['query'])[:80]}"
    if primary_artifact is not None:
        verb, title, _resource_kind = primary_artifact
        if title:
            return f"{verb.replace('_', ' ').capitalize()}: {title}"
    return _anchor_from_input(row.tool_name, input_summary)


# Legacy machine title encodings that pre-date the Sprint-6 title fix: a
# leading count restatement ("1 actions", "3 tool calls", "3 tool calls, 1
# write") or the "<client> · N actions · <kind>" dump. These match only the
# machine forms, never a clean human title, so the read-time re-derive in
# _session_ref leaves user/anchor titles untouched.
_LEGACY_COUNT_TITLE_RE = re.compile(
    r"^\d+\s+(?:actions?|tool calls?)\b"  # leading "N actions" / "N tool calls"
    r"|·\s*\d+\s+actions?\s*·",  # embedded "· N actions ·" dump
)


def _is_legacy_count_title(value: str) -> bool:
    return bool(_LEGACY_COUNT_TITLE_RE.search(value.strip()))

"""Private served-read facts and reader-gated MCP activity projections.

The recorder is active only around one MCP dispatch. It snapshots objects at
the point they are served, never reconstructs an old mutable revision, and
keeps no body, path, selector prose, credential, or conversation token.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.audit import AuditEvent
from backend.models.context import ContextPageVersion
from backend.models.library import LibraryItemVersion
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.workspace_scoped import get_in_workspace, list_in_workspace
from backend.services.actor_visible_read import (
    agent_version_is_visible,
    library_item_visible,
)
from backend.spi.tool_presentation import TOOL_ACTIONS

PURE_READ_TOOLS = frozenset(
    {
        "read",
        "fetch",
        "read_concept",
        "read_agent_page",
        "read_context_outline",
        "read_structure_map",
    }
)
_READ_SCOPE_FIELDS = {
    "read": {"target_type", "target_id"},
    "fetch": {"type", "id"},
    "read_concept": {"include_neighbors", "as_of", "format"},
    "read_agent_page": {"version_selector"},
    "read_context_outline": {
        "version",
        "select",
        "max_chars",
        "cursor",
        "max_depth",
        "max_sections",
        "outline_cursor",
    },
    "read_structure_map": {"version", "max_blocks", "max_rows_per_block", "cursor"},
}
_READ_DEFAULTS = {
    "read_concept": {"include_neighbors": True, "as_of": None, "format": "structured"},
    "read_agent_page": {"version_selector": "active"},
    "read_context_outline": {
        "version": "visible",
        "select": None,
        "max_chars": 8000,
        "cursor": None,
        "max_depth": 6,
        "max_sections": 200,
        "outline_cursor": None,
    },
    "read_structure_map": {
        "version": "visible",
        "max_blocks": 200,
        "max_rows_per_block": 200,
        "cursor": None,
    },
}
_VERSION_STATUSES = frozenset(
    {
        "editing",
        "draft",
        "published_snapshot",
        "superseded_snapshot",
        "active",
        "superseded",
        "ready",
        "completed",
        "ready_partial",
    }
)
_PUBLIC_ERRORS = frozenset(
    {
        "not_found",
        "validation_failed",
        "scope_denied",
        "workspace_access_denied",
        "workspace_role_insufficient",
        "restricted_library_denied",
        "conflict",
        "stale_base_version",
        "internal_error",
        "unsupported_retrieval_mode",
    }
)
_KNOWN_RESULTS = frozenset(
    {
        "succeeded",
        "failed",
        "rejected_by_policy",
        "conflict",
        "partial",
        "accepted",
        "needs_review",
    }
)
_capture: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "mcp_served_reads", default=None
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@contextmanager
def capture_served_reads():
    snapshots: list[dict[str, Any]] = []
    token = _capture.set(snapshots)
    try:
        yield snapshots
    finally:
        _capture.reset(token)


def record_served_read(
    resource_kind: str, resource_id: UUID, version: Any, *, content: str | None = None
) -> None:
    """Record only an already-authorized version actually used for a response."""
    snapshots = _capture.get()
    if snapshots is None or version is None:
        return
    status = getattr(version, "version_status", None) or getattr(
        version, "processing_status", None
    )
    body = getattr(version, "body_md", None)
    if body is None:
        body = getattr(version, "body_markdown", None)
    if content is not None:
        body = content
    content_identity = (
        hashlib.sha256(body.encode()).hexdigest()
        if isinstance(body, str)
        else _digest(
            {
                "content_hash": getattr(version, "content_hash", None),
                "conversion_attempt": getattr(
                    version, "selected_conversion_attempt_id", None
                ),
                "preview_attempt": getattr(
                    version, "selected_preview_attempt_id", None
                ),
                "processing_status": status,
            }
        )
    )
    snapshot = {
        "schema": 1,
        "resource_kind": resource_kind,
        "resource_id": str(resource_id),
        "version_id": str(version.id),
        "version_status": status,
        "content_identity": content_identity,
        "editing_revision": getattr(version, "editing_revision", None)
        if status == "editing"
        else None,
    }
    if snapshot not in snapshots:
        snapshots.append(snapshot)


def finish_served_read(
    tool_name: str, arguments: dict[str, Any], snapshots: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if tool_name not in PURE_READ_TOOLS or len(snapshots) != 1:
        return None
    fields = _READ_SCOPE_FIELDS[tool_name]
    scope = {
        **_READ_DEFAULTS.get(tool_name, {}),
        **{key: value for key, value in arguments.items() if key in fields},
    }
    return validate_read_projection({**snapshots[0], "scope_identity": _digest(scope)})


def validate_read_projection(value: Any) -> dict[str, Any] | None:
    """Re-cut the private JSON allowlist, rejecting incomplete/forged shapes."""
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    if value.get("resource_kind") not in {"context_page", "agent_page", "library_item"}:
        return None
    try:
        resource_id, version_id = (
            UUID(str(value["resource_id"])),
            UUID(str(value["version_id"])),
        )
    except (ValueError, TypeError, KeyError):
        return None
    if value.get("version_status") not in _VERSION_STATUSES:
        return None
    for field in ("content_identity", "scope_identity"):
        digest = value.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return None
    revision = value.get("editing_revision")
    if value["version_status"] == "editing" and (
        type(revision) is not int or revision < 1
    ):
        return None
    return {
        "schema": 1,
        "resource_kind": value["resource_kind"],
        "resource_id": str(resource_id),
        "version_id": str(version_id),
        "version_status": value["version_status"],
        "content_identity": value["content_identity"],
        "scope_identity": value["scope_identity"],
        "editing_revision": revision if value["version_status"] == "editing" else None,
    }


@dataclass(frozen=True)
class McpActivityProjection:
    visible: bool
    resource_kind: str | None = None
    resource_id: UUID | None = None
    title: str | None = None
    navigation_path: str | None = None
    operation: str = "tool"
    result: str = "unknown"
    public_error_code: str | None = None
    duration_ms: int | None = None
    grouping_key: tuple[str, ...] | None = None


def project_mcp_activity(
    session: Session, call: MCPToolCall, actor: ActorContext | None
) -> McpActivityProjection:
    return project_mcp_activity_batch(session, [call], actor)[call.id]


def project_mcp_activity_batch(
    session: Session, calls: list[MCPToolCall], actor: ActorContext | None
) -> dict[UUID, McpActivityProjection]:
    """One bounded source batch, with shared policy answers and edge lookups."""
    from backend.services.mcp_chat_service import McpChatService, _ReaderGate

    if not calls:
        return {}
    workspace_ids = {call.workspace_id for call in calls}
    edges_by_call: dict[UUID, list[McpChatToolCallArtifact]] = {}
    chats = {}
    for workspace_id in workspace_ids:
        workspace_edges = list_in_workspace(
            session,
            McpChatToolCallArtifact,
            workspace_id=workspace_id,
            conditions=(
                McpChatToolCallArtifact.mcp_tool_call_id.in_(
                    [call.id for call in calls if call.workspace_id == workspace_id]
                ),
            ),
        )
        for edge in workspace_edges:
            edges_by_call.setdefault(edge.mcp_tool_call_id, []).append(edge)
        chat_ids = {edge.chat_session_id for edge in workspace_edges}
        if chat_ids:
            chats.update(
                (chat.id, chat)
                for chat in list_in_workspace(
                    session,
                    McpChatSession,
                    workspace_id=workspace_id,
                    conditions=(McpChatSession.id.in_(chat_ids),),
                )
            )
    service = McpChatService()
    gates = {
        workspace_id: _ReaderGate(service, session, workspace_id, actor)
        for workspace_id in workspace_ids
    }
    return {
        call.id: _project_mcp_activity(
            session,
            call,
            actor,
            gates[call.workspace_id],
            edges_by_call.get(call.id, []),
            chats,
        )
        for call in calls
    }


def _project_mcp_activity(session, call, actor, gate, edges, chats):
    # Local imports prevent ordinary read services from depending on Chats.
    from backend.services.mcp_chat_service import (
        _input_target_refs,
        mcp_session_trace_uuid,
    )

    operation = TOOL_ACTIONS.get(call.tool_name, "tool")
    common = {
        "operation": operation,
        "result": call.result_status
        if call.result_status in _KNOWN_RESULTS
        else "unknown",
        "public_error_code": call.error_code
        if call.error_code in _PUBLIC_ERRORS
        else None,
        "duration_ms": call.latency_ms
        if type(call.latency_ms) is int and call.latency_ms >= 0
        else None,
    }
    primary_edges = [edge for edge in edges if edge.verb != "search_match"]
    targets: dict[tuple[str, UUID], tuple[str, str | None]] = {}
    for edge in primary_edges:
        title, path = gate.title_and_path(edge.resource_kind, edge.resource_id)
        if title is None:
            return McpActivityProjection(visible=False, **common)
        targets[(edge.resource_kind, edge.resource_id)] = (title, path)
    # Failures and pre-edge legacy calls can still name a private target.
    for kind, target_id in _input_target_refs(call.tool_name, call.input_summary):
        if gate.input_target(kind, target_id) is None:
            return McpActivityProjection(visible=False, **common)
    detail = call.result_detail if isinstance(call.result_detail, dict) else {}
    read = validate_read_projection(detail.get("read_projection"))
    version_id = UUID(read["version_id"]) if read is not None else None
    # Explicit input/receipt versions are checked even if their parent page
    # has a separately public head. The stored snapshot itself is never public.
    if version_id is None and isinstance(detail.get("version_id"), str):
        try:
            version_id = UUID(detail["version_id"])
        except ValueError:
            pass
    if read is not None and not _served_version_visible(
        session, gate, read, actor, call
    ):
        return McpActivityProjection(visible=False, **common)
    if (
        read is None
        and version_id is not None
        and gate.visible_version(version_id) is None
    ):
        return McpActivityProjection(visible=False, **common)
    if len(targets) != 1:
        return McpActivityProjection(visible=True, **common)
    (resource_kind, resource_id), (title, path) = next(iter(targets.items()))
    if read is not None and (
        read["resource_kind"] != resource_kind
        or UUID(read["resource_id"]) != resource_id
    ):
        return McpActivityProjection(visible=True, **common)
    if call.tool_name in PURE_READ_TOOLS and read is None:
        read = _legacy_read_projection(session, call, resource_kind, resource_id)
        if read is None:
            # A public parent edge cannot prove which of its public/private
            # versions an old unqualified read served. Keep only the neutral,
            # workspace-visible call shape, without invented target details.
            return McpActivityProjection(visible=True, **common)
    key = None
    if (
        call.tool_name in PURE_READ_TOOLS
        and call.result_status == "succeeded"
        and call.error_code is None
        and len(primary_edges) == 1
        and primary_edges[0].verb == "referenced"
    ):
        edge = primary_edges[0]
        chat = chats.get(edge.chat_session_id)
        verified = (
            chat is not None
            and chat.workspace_id == call.workspace_id
            and call.mcp_session_id is not None
            and mcp_session_trace_uuid(chat.mcp_session_id) == call.mcp_session_id
        )
        # A conversation retains the client which started it, even when its
        # owner legitimately continues the token through another client.
        # The trace's workspace-scoped client FK proves each call's client;
        # the key below keeps different per-call clients in separate runs.
        if (
            verified
            and read is not None
            and read["resource_kind"] == resource_kind
            and UUID(read["resource_id"]) == resource_id
            and _served_version_visible(session, gate, read, actor, call)
        ):
            key = (
                str(call.workspace_id),
                str(call.client_id),
                str(chat.id),
                str(call.mcp_session_id),
                resource_kind,
                str(resource_id),
                call.tool_name,
                read["version_id"],
                read["version_status"],
                str(read["editing_revision"]),
                read["content_identity"],
                read["scope_identity"],
            )
    return McpActivityProjection(
        visible=True,
        resource_kind=resource_kind,
        resource_id=resource_id,
        title=title,
        navigation_path=path,
        grouping_key=key,
        **common,
    )


def _served_version_visible(
    session: Session,
    gate,
    read: dict[str, Any],
    actor: ActorContext | None,
    call: MCPToolCall,
) -> bool:
    kind, version_id, resource_id = (
        read["resource_kind"],
        UUID(read["version_id"]),
        UUID(read["resource_id"]),
    )
    if kind == "context_page":
        version = gate.visible_version(version_id)
        if version is None or version.page_id != resource_id:
            return False
        # The same Context version id can move from private editing through
        # autosaves to a submitted draft/public snapshot. Sharing its final
        # body does not share earlier, never-submitted revisions that a trace
        # actually served. Preserve that served state's author-only rule even
        # after the mutable database row has become public.
        if read["version_status"] == "editing" and actor is not None:
            return (
                actor_can_read_drafts(actor)
                and bool(actor.actor_kind)
                and bool(actor.actor_id)
                and version.created_by_actor_kind == actor.actor_kind
                and version.created_by_actor_id == actor.actor_id
            )
        return True
    if kind == "agent_page":
        page = get_in_workspace(
            session, AgentPage, workspace_id=gate.workspace_id, resource_id=resource_id
        )
        version = get_in_workspace(
            session,
            AgentPageVersion,
            workspace_id=gate.workspace_id,
            resource_id=version_id,
        )
        return (
            page is not None
            and version is not None
            and version.agent_page_id == page.id
            and agent_version_is_visible(page, version, actor)
            and gate.title_and_path(kind, resource_id)[0] is not None
        )
    from backend.models.library import LibraryItem

    item = get_in_workspace(
        session, LibraryItem, workspace_id=gate.workspace_id, resource_id=resource_id
    )
    version = get_in_workspace(
        session,
        LibraryItemVersion,
        workspace_id=gate.workspace_id,
        resource_id=version_id,
    )
    arguments = call.input_summary if isinstance(call.input_summary, dict) else {}
    explicit_version_metadata = (
        call.tool_name == "read"
        and arguments.get("target_type") == "library_item_version"
        and arguments.get("target_id") == str(version_id)
    )
    return (
        version is not None
        and version.library_item_id == resource_id
        and library_item_visible(
            item,
            actor,
            workspace_id=gate.workspace_id,
            version=None if explicit_version_metadata else version,
        )
    )


def _legacy_read_projection(
    session: Session, call: MCPToolCall, kind: str, resource_id: UUID
) -> dict[str, Any] | None:
    """Only explicit immutable Context versions prove an old served identity.

    Neither current page heads nor a session's last-read rollup prove what a
    previous call saw. Mutable editing/draft rows without a served revision
    therefore remain individual events forever.
    """
    arguments = call.input_summary if isinstance(call.input_summary, dict) else {}
    version_id = None
    if (
        call.tool_name == "read"
        and arguments.get("target_type") == "context_page_version"
    ):
        version_id = arguments.get("target_id")
    elif call.tool_name in {
        "read_context_outline",
        "read_structure_map",
    } and isinstance(arguments.get("target"), dict):
        version_id = arguments["target"].get("version_id")
    try:
        version_id = UUID(str(version_id))
    except (ValueError, TypeError):
        return None
    if kind != "context_page":
        return None
    version = get_in_workspace(
        session,
        ContextPageVersion,
        workspace_id=call.workspace_id,
        resource_id=version_id,
    )
    if (
        version is None
        or version.page_id != resource_id
        or version.version_status not in {"published_snapshot", "superseded_snapshot"}
    ):
        return None
    # A UUID which is immutable *now* may have named an autosaving editing
    # row then. Publication preserves last_saved_at, so that timestamp alone
    # cannot prove the historical state: require its append-only publication
    # audit to precede the read as well. Missing old evidence fails closed.
    saved_at = version.last_saved_at
    called_at = call.started_at
    if saved_at is None or called_at is None:
        return None
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    if called_at.tzinfo is None:
        called_at = called_at.replace(tzinfo=UTC)
    if saved_at > called_at:
        return None
    published = session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.workspace_id == call.workspace_id,
            AuditEvent.target_kind == "context_page",
            AuditEvent.target_id == resource_id,
            AuditEvent.action == "context_page.draft.publish",
            AuditEvent.payload["version_id"].as_string() == str(version.id),
            AuditEvent.created_at <= called_at,
        )
        .limit(1)
    )
    if published is None:
        return None
    scope = {
        **_READ_DEFAULTS.get(call.tool_name, {}),
        **{
            key: value
            for key, value in arguments.items()
            if key in _READ_SCOPE_FIELDS[call.tool_name]
        },
    }
    return validate_read_projection(
        {
            "schema": 1,
            "resource_kind": kind,
            "resource_id": str(resource_id),
            "version_id": str(version.id),
            "version_status": "published_snapshot",
            "content_identity": hashlib.sha256(version.body_md.encode()).hexdigest(),
            "scope_identity": _digest(scope),
        }
    )

"""Batched, scalar evidence for the Chats list and detail header.

The repository selects facts; presentation precedence lives in the service's
pure builder. Trace JSON is read only for ranked label candidates, and only
the per-tool fields that the caller explicitly allows are projected.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Uuid, and_, case, column, func, not_, or_, select, union, values
from sqlalchemy.orm import Session, load_only

from backend.domain.input_policy import fit_generated_title
from backend.models.agent import AgentPage
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.library import LibraryItem
from backend.models.mcp_chat import (
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
    explicit_context_version_visible_expression,
    foreign_editing_version_exists,
    visible_context_page_expression,
)
from backend.repositories.workspace_scoped import select_projection_in_workspace
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES


@dataclass(frozen=True)
class ProjectionPolicy:
    eligible_statuses: frozenset[str]
    bootstrap_tools: frozenset[str]
    search_fields: Mapping[str, str]
    input_fields_by_tool: Mapping[str, tuple[str, ...]]
    context_edit_tools: frozenset[str]
    version_naming_edit_tools: frozenset[str]
    trace_session_uuid: Callable[[str | UUID], UUID]
    artifact_urls: Mapping[str, Callable[[UUID, UUID], str]]
    visible_agent_sensitivities: frozenset[str]
    excluded_library_lifecycle_statuses: frozenset[str]


@dataclass(frozen=True)
class ProjectionLink:
    id: UUID
    chat_session_id: UUID
    resource_kind: str
    resource_id: UUID
    verb: str
    first_occurred_at: datetime
    last_occurred_at: datetime
    occurrence_count: int
    last_mcp_tool_call_id: UUID


@dataclass(frozen=True)
class ProjectionEdge:
    chat_session_id: UUID
    mcp_tool_call_id: UUID
    verb: str
    resource_kind: str
    resource_id: UUID
    occurred_at: datetime
    tool_name: str
    result_status: str
    started_at: datetime


@dataclass(frozen=True)
class ProjectionCall:
    id: UUID
    tool_name: str
    result_status: str
    started_at: datetime
    input_summary: dict[str, Any]
    result_detail: dict[str, Any] | None


@dataclass
class ChatPageProjection:
    clients: dict[UUID, MCPClient] = field(default_factory=dict)
    links_by_chat: dict[UUID, list[ProjectionLink]] = field(default_factory=dict)
    edges_by_chat: dict[UUID, list[ProjectionEdge]] = field(default_factory=dict)
    calls_by_chat: dict[UUID, list[ProjectionCall]] = field(default_factory=dict)
    bootstrap_by_chat: dict[UUID, bool] = field(default_factory=dict)
    first_call_ids_by_chat: dict[UUID, UUID] = field(default_factory=dict)
    first_action_call_ids_by_chat: dict[UUID, UUID] = field(default_factory=dict)
    artifacts: dict[tuple[str, UUID], tuple[str | None, str | None]] = field(
        default_factory=dict
    )
    visible_edit_versions: set[UUID] = field(default_factory=set)
    row_counts: dict[str, int] = field(default_factory=dict)


def _context_visibility(
    actor: ActorContext | None, workspace_id: UUID, *, shared_only: bool
):
    predicates = []
    if actor is not None:
        predicates.append(visible_context_page_expression(actor))
    if shared_only:
        # Preserve bool(versions) and all(private) without loading any bodies.
        has_version = (
            select_projection_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                columns=(ContextPageVersion.id,),
                predicates=(ContextPageVersion.page_id == ContextPage.id,),
            )
            .correlate_except(ContextPageVersion)
            .exists()
        )
        has_shared_version = (
            select_projection_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                columns=(ContextPageVersion.id,),
                predicates=(
                    ContextPageVersion.page_id == ContextPage.id,
                    ContextPageVersion.version_status.notin_(
                        tuple(PRIVATE_VERSION_STATUSES)
                    ),
                ),
            )
            .correlate_except(ContextPageVersion)
            .exists()
        )
        predicates.append(or_(not_(has_version), has_shared_version))
    return and_(True, *predicates)


def _agent_visibility(actor: ActorContext | None, policy: ProjectionPolicy):
    predicates = []
    if actor is not None:
        predicates.append(AgentPage.lifecycle_status != "archived")
        if not actor_can_read_drafts(actor):
            predicates.append(AgentPage.current_active_version_id.is_not(None))
        if not actor_can_read_sensitive_agent_pages(actor):
            predicates.extend(
                (
                    AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
                    AgentPage.sensitivity.in_(policy.visible_agent_sensitivities),
                )
            )
    return and_(True, *predicates)


def _library_visibility(actor: ActorContext | None, policy: ProjectionPolicy):
    if actor is None:
        return and_(True)
    predicates = [
        LibraryItem.lifecycle_status.notin_(policy.excluded_library_lifecycle_statuses)
    ]
    if not actor_can_read_restricted_library(actor):
        predicates.append(LibraryItem.trust_level != "restricted")
    return and_(*predicates)


def _media_visibility(
    actor: ActorContext | None, workspace_id: UUID, *, shared_only: bool
):
    page_visible = (
        select_projection_in_workspace(
            ContextPage,
            workspace_id=workspace_id,
            columns=(ContextPage.id,),
            predicates=(
                ContextPage.id == ContextMediaAsset.page_id,
                _context_visibility(actor, workspace_id, shared_only=shared_only),
            ),
        )
        .correlate_except(ContextPage)
        .exists()
    )
    predicates = [page_visible]
    if actor is not None:
        predicates.extend(
            (
                ContextMediaAsset.association_state != "tombstoned",
                select_projection_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    columns=(ContextPageVersion.id,),
                    predicates=(
                        ContextPageVersion.id
                        == ContextMediaAsset.created_for_version_id,
                    ),
                )
                .correlate_except(ContextPageVersion)
                .exists(),
                not_(
                    foreign_editing_version_exists(
                        ContextMediaAsset.created_for_version_id,
                        (actor.actor_kind, actor.actor_id),
                    )
                ),
            )
        )
    return and_(*predicates)


def artifact_visible_predicate(
    resource_kind,
    resource_id,
    actor: ActorContext | None,
    workspace_id: UUID,
    *,
    policy: ProjectionPolicy,
    shared_only: bool = False,
):
    """Visibility for an arbitrary scoped artifact-reference expression.

    All four branches share their predicates with the metadata batches below,
    including the author-only status of both editing and discarded uploads.
    """

    branches = []
    for kind, model, predicate in (
        (
            "context_page",
            ContextPage,
            _context_visibility(actor, workspace_id, shared_only=shared_only),
        ),
        ("agent_page", AgentPage, _agent_visibility(actor, policy)),
        ("library_item", LibraryItem, _library_visibility(actor, policy)),
        (
            "context_media_asset",
            ContextMediaAsset,
            _media_visibility(actor, workspace_id, shared_only=shared_only),
        ),
    ):
        branches.append(
            and_(
                resource_kind == kind,
                select_projection_in_workspace(
                    model,
                    workspace_id=workspace_id,
                    columns=(model.id,),
                    predicates=(model.id == resource_id, predicate),
                )
                .correlate_except(model)
                .exists(),
            )
        )
    return or_(*branches)


def _resource_keys(workspace_id: UUID, chat_ids: Sequence[UUID], include_search: bool):
    link_predicates = [McpChatArtifactLink.chat_session_id.in_(chat_ids)]
    if not include_search:
        link_predicates.append(
            McpChatArtifactLink.verb != McpChatArtifactVerb.SEARCH_MATCH.value
        )
    links = select_projection_in_workspace(
        McpChatArtifactLink,
        workspace_id=workspace_id,
        columns=(McpChatArtifactLink.resource_kind, McpChatArtifactLink.resource_id),
        predicates=link_predicates,
    )
    edges = select_projection_in_workspace(
        McpChatToolCallArtifact,
        workspace_id=workspace_id,
        columns=(
            McpChatToolCallArtifact.resource_kind,
            McpChatToolCallArtifact.resource_id,
        ),
        predicates=(
            McpChatToolCallArtifact.chat_session_id.in_(chat_ids),
            McpChatToolCallArtifact.verb != McpChatArtifactVerb.SEARCH_MATCH.value,
        ),
    )
    return union(links, edges).cte("projection_resource_keys")


def _load_artifacts(
    session: Session,
    workspace_id: UUID,
    chat_ids: Sequence[UUID],
    kinds: set[str],
    actor: ActorContext | None,
    shared_only: bool,
    include_search: bool,
    policy: ProjectionPolicy,
    result: ChatPageProjection,
) -> None:
    keys = _resource_keys(workspace_id, chat_ids, include_search)
    for kind, model, title, predicate in (
        (
            "context_page",
            ContextPage,
            ContextPage.title,
            _context_visibility(actor, workspace_id, shared_only=shared_only),
        ),
        ("agent_page", AgentPage, AgentPage.title, _agent_visibility(actor, policy)),
        (
            "library_item",
            LibraryItem,
            LibraryItem.title,
            _library_visibility(actor, policy),
        ),
        (
            "context_media_asset",
            ContextMediaAsset,
            ContextMediaAsset.original_filename,
            _media_visibility(actor, workspace_id, shared_only=shared_only),
        ),
    ):
        if kind not in kinds:
            continue
        extra = (
            [LibraryItem.uri]
            if kind == "library_item"
            else [ContextMediaAsset.page_id]
            if kind == "context_media_asset"
            else []
        )
        rows = session.execute(
            select_projection_in_workspace(
                model,
                workspace_id=workspace_id,
                columns=(model.id, title, *extra),
                predicates=(
                    model.id.in_(
                        select(keys.c.resource_id).where(keys.c.resource_kind == kind)
                    ),
                    predicate,
                ),
            )
        ).all()
        result.row_counts["artifacts"] += len(rows)
        for resource_id, raw_title, *extras in rows:
            if kind == "library_item":
                raw_title = raw_title or fit_generated_title(
                    extras[0], fallback="Library item"
                )
            target_id = extras[0] if kind == "context_media_asset" else resource_id
            path = policy.artifact_urls[kind](workspace_id, target_id)
            result.artifacts[(kind, resource_id)] = raw_title, path


def _load_calls(
    session: Session,
    workspace_id: UUID,
    chats: Sequence[McpChatSession],
    actor: ActorContext | None,
    policy: ProjectionPolicy,
    result: ChatPageProjection,
) -> None:
    chat_map = (
        values(column("chat_id", Uuid()), column("trace_uuid", Uuid()))
        .data(
            [
                (chat.id, policy.trace_session_uuid(chat.mcp_session_id))
                for chat in chats
            ]
        )
        .cte("projection_chat_sessions")
    )
    trace_metadata = select_projection_in_workspace(
        MCPToolCall,
        workspace_id=workspace_id,
        columns=(
            MCPToolCall.id,
            MCPToolCall.mcp_session_id,
            MCPToolCall.tool_name,
            MCPToolCall.result_status,
            MCPToolCall.started_at,
        ),
        predicates=(
            MCPToolCall.result_status.in_(policy.eligible_statuses),
            MCPToolCall.mcp_session_id.in_(
                {policy.trace_session_uuid(chat.mcp_session_id) for chat in chats}
            ),
        ),
    ).cte("projection_trace_metadata")
    non_bootstrap = trace_metadata.c.tool_name.notin_(policy.bootstrap_tools)
    metadata_stmt = (
        select(
            chat_map.c.chat_id,
            trace_metadata.c.id,
            trace_metadata.c.tool_name,
            trace_metadata.c.result_status,
            trace_metadata.c.started_at,
            func.row_number()
            .over(
                partition_by=chat_map.c.chat_id,
                order_by=(trace_metadata.c.started_at, trace_metadata.c.id),
            )
            .label("first_rank"),
            func.row_number()
            .over(
                partition_by=chat_map.c.chat_id,
                order_by=(
                    case((non_bootstrap, 0), else_=1),
                    trace_metadata.c.started_at,
                    trace_metadata.c.id,
                ),
            )
            .label("action_rank"),
            func.sum(case((non_bootstrap, 1), else_=0))
            .over(partition_by=chat_map.c.chat_id)
            .label("non_bootstrap_count"),
        )
        .select_from(trace_metadata)
        .join(
            chat_map,
            trace_metadata.c.mcp_session_id == chat_map.c.trace_uuid,
        )
    )
    if actor is not None:
        hidden_primary_edge = (
            select_projection_in_workspace(
                McpChatToolCallArtifact,
                workspace_id=workspace_id,
                columns=(McpChatToolCallArtifact.id,),
                predicates=(
                    McpChatToolCallArtifact.chat_session_id == chat_map.c.chat_id,
                    McpChatToolCallArtifact.mcp_tool_call_id == trace_metadata.c.id,
                    McpChatToolCallArtifact.verb
                    != McpChatArtifactVerb.SEARCH_MATCH.value,
                    not_(
                        artifact_visible_predicate(
                            McpChatToolCallArtifact.resource_kind,
                            McpChatToolCallArtifact.resource_id,
                            actor,
                            workspace_id,
                            policy=policy,
                        )
                    ),
                ),
            )
            .correlate_except(McpChatToolCallArtifact)
            .exists()
        )
        metadata_stmt = metadata_stmt.where(not_(hidden_primary_edge))
    metadata = metadata_stmt.cte("projection_eligible_calls")
    candidates = (
        select(metadata)
        .where(
            or_(
                metadata.c.first_rank == 1,
                and_(
                    metadata.c.action_rank == 1,
                    metadata.c.tool_name.notin_(policy.bootstrap_tools),
                ),
                metadata.c.tool_name.in_(tuple(policy.search_fields)),
            )
        )
        .cte("projection_label_candidates")
    )
    fields_by_tool = {
        name: set(fields) for name, fields in policy.input_fields_by_tool.items()
    }
    for name, search_field in policy.search_fields.items():
        fields_by_tool.setdefault(name, set()).add(search_field)
    for name in policy.version_naming_edit_tools:
        fields_by_tool.setdefault(name, set()).add("version_id")
    field_tools: dict[str, list[str]] = defaultdict(list)
    for name, fields in fields_by_tool.items():
        for input_field in fields:
            field_tools[input_field].append(name)
    field_names = sorted(field_tools)
    # JSON-typed expressions retain numbers/arrays/nulls. Converting to text
    # here would make malformed captured inputs newly eligible as labels.
    leaves = [
        case(
            (
                MCPToolCall.tool_name.in_(field_tools[input_field]),
                MCPToolCall.input_summary[input_field],
            ),
            else_=None,
        ).label(f"input_{input_field}")
        for input_field in field_names
    ]
    result_version = case(
        (
            MCPToolCall.tool_name.in_(policy.context_edit_tools),
            MCPToolCall.result_detail["version_id"],
        ),
        else_=None,
    ).label("result_version_id")
    payload = (
        select_projection_in_workspace(
            MCPToolCall,
            workspace_id=workspace_id,
            columns=(MCPToolCall.id,),
            predicates=(MCPToolCall.id.in_(select(candidates.c.id)),),
        )
        .with_only_columns(MCPToolCall.id, *leaves, result_version)
        .cte("projection_candidate_payload")
    )
    rows = session.execute(
        select(
            candidates,
            *(payload.c[f"input_{name}"] for name in field_names),
            payload.c.result_version_id,
        )
        .join(
            payload,
            payload.c.id == candidates.c.id,
        )
        .order_by(candidates.c.chat_id, candidates.c.started_at, candidates.c.id)
    ).mappings()
    for row in rows:
        chat_id = row["chat_id"]
        name = row["tool_name"]
        inputs = {key: row[f"input_{key}"] for key in fields_by_tool.get(name, ())}
        call = ProjectionCall(
            id=row["id"],
            tool_name=name,
            result_status=row["result_status"],
            started_at=row["started_at"],
            input_summary=inputs,
            result_detail={"version_id": row["result_version_id"]},
        )
        result.calls_by_chat[chat_id].append(call)
        result.row_counts["calls"] += 1
        if row["first_rank"] == 1:
            result.first_call_ids_by_chat[chat_id] = call.id
            result.bootstrap_by_chat[chat_id] = row["non_bootstrap_count"] == 0
        if row["action_rank"] == 1 and name not in policy.bootstrap_tools:
            result.first_action_call_ids_by_chat[chat_id] = call.id


def _load_edit_versions(
    session: Session,
    workspace_id: UUID,
    actor: ActorContext | None,
    policy: ProjectionPolicy,
    result: ChatPageProjection,
) -> None:
    if actor is None:
        return
    version_ids = set()
    for chat_id, calls in result.calls_by_chat.items():
        first_id = result.first_call_ids_by_chat.get(chat_id)
        for call in calls:
            if call.id != first_id or call.tool_name not in policy.context_edit_tools:
                continue
            raw_id = (
                call.input_summary.get("version_id")
                if call.tool_name in policy.version_naming_edit_tools
                else None
            )
            candidates = (raw_id, (call.result_detail or {}).get("version_id"))
            for raw_version_id in candidates:
                if not isinstance(raw_version_id, str):
                    continue
                try:
                    version_ids.add(UUID(raw_version_id))
                except ValueError:
                    continue
                break
    if not version_ids:
        return
    visible_pages = select_projection_in_workspace(
        ContextPage,
        workspace_id=workspace_id,
        columns=(ContextPage.id,),
        predicates=(visible_context_page_expression(actor),),
    )
    result.visible_edit_versions.update(
        session.scalars(
            select_projection_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                columns=(ContextPageVersion.id,),
                predicates=(
                    ContextPageVersion.id.in_(version_ids),
                    ContextPageVersion.page_id.in_(visible_pages),
                    explicit_context_version_visible_expression(
                        ContextPageVersion, actor
                    ),
                ),
            )
        )
    )
    result.row_counts["versions"] = len(result.visible_edit_versions)


def load_projection(
    session: Session,
    workspace_id: UUID,
    chats: Sequence[McpChatSession],
    actor: ActorContext | None,
    *,
    policy: ProjectionPolicy,
    shared_only: bool = False,
    include_search_matches: bool = False,
    load_clients: bool = True,
) -> ChatPageProjection:
    """Load one request's evidence; constructing cards afterwards needs no SQL."""

    result = ChatPageProjection(
        row_counts={
            "links": 0,
            "edges": 0,
            "calls": 0,
            "artifacts": 0,
            "clients": 0,
            "versions": 0,
        }
    )
    if load_clients:
        result.clients = {
            client.id: client
            for client in session.scalars(
                select_projection_in_workspace(MCPClient, workspace_id=workspace_id)
                .order_by(MCPClient.name)
                .options(
                    load_only(
                        MCPClient.id,
                        MCPClient.name,
                        MCPClient.client_type,
                        MCPClient.brand_hint,
                        MCPClient.scopes,
                        raiseload=True,
                    )
                )
            )
        }
        result.row_counts["clients"] = len(result.clients)
    if not chats:
        return result
    if any(chat.workspace_id != workspace_id for chat in chats):
        raise ValueError("all projected chats must belong to the requested workspace")
    chat_ids = [chat.id for chat in chats]
    for chat_id in chat_ids:
        result.links_by_chat[chat_id] = []
        result.edges_by_chat[chat_id] = []
        result.calls_by_chat[chat_id] = []
        result.bootstrap_by_chat[chat_id] = False
    link_columns = (
        "id",
        "chat_session_id",
        "resource_kind",
        "resource_id",
        "verb",
        "first_occurred_at",
        "last_occurred_at",
        "occurrence_count",
        "last_mcp_tool_call_id",
    )
    link_predicates = [McpChatArtifactLink.chat_session_id.in_(chat_ids)]
    if not include_search_matches:
        link_predicates.append(
            McpChatArtifactLink.verb != McpChatArtifactVerb.SEARCH_MATCH.value
        )
    links_stmt = select_projection_in_workspace(
        McpChatArtifactLink,
        workspace_id=workspace_id,
        columns=tuple(getattr(McpChatArtifactLink, name) for name in link_columns),
        predicates=link_predicates,
    ).order_by(
        McpChatArtifactLink.chat_session_id,
        McpChatArtifactLink.last_occurred_at.desc(),
        McpChatArtifactLink.id.desc(),
    )
    links = session.execute(links_stmt).mappings()
    kinds = set()
    for row in links:
        link = ProjectionLink(**row)
        result.links_by_chat[link.chat_session_id].append(link)
        result.row_counts["links"] += 1
        if (
            include_search_matches
            or link.verb != McpChatArtifactVerb.SEARCH_MATCH.value
        ):
            kinds.add(link.resource_kind)
    edge_metadata = select_projection_in_workspace(
        McpChatToolCallArtifact,
        workspace_id=workspace_id,
        columns=(
            McpChatToolCallArtifact.chat_session_id,
            McpChatToolCallArtifact.mcp_tool_call_id,
            McpChatToolCallArtifact.verb,
            McpChatToolCallArtifact.resource_kind,
            McpChatToolCallArtifact.resource_id,
            McpChatToolCallArtifact.occurred_at,
        ),
        predicates=(
            McpChatToolCallArtifact.chat_session_id.in_(chat_ids),
            McpChatToolCallArtifact.verb != McpChatArtifactVerb.SEARCH_MATCH.value,
        ),
    ).cte("projection_edge_metadata")
    edge_calls = select_projection_in_workspace(
        MCPToolCall,
        workspace_id=workspace_id,
        columns=(
            MCPToolCall.id,
            MCPToolCall.tool_name,
            MCPToolCall.result_status,
            MCPToolCall.started_at,
        ),
        predicates=(MCPToolCall.id.in_(select(edge_metadata.c.mcp_tool_call_id)),),
    ).cte("projection_edge_calls")
    edges = session.execute(
        select(
            edge_metadata,
            edge_calls.c.tool_name,
            edge_calls.c.result_status,
            edge_calls.c.started_at,
        )
        .join(
            edge_calls,
            edge_calls.c.id == edge_metadata.c.mcp_tool_call_id,
        )
        .order_by(
            edge_metadata.c.chat_session_id,
            edge_metadata.c.occurred_at,
            edge_metadata.c.resource_id,
        )
    ).mappings()
    for row in edges:
        edge = ProjectionEdge(**row)
        result.edges_by_chat[edge.chat_session_id].append(edge)
        result.row_counts["edges"] += 1
        if (
            include_search_matches
            or edge.verb != McpChatArtifactVerb.SEARCH_MATCH.value
        ):
            kinds.add(edge.resource_kind)
    _load_artifacts(
        session,
        workspace_id,
        chat_ids,
        kinds,
        actor,
        shared_only,
        include_search_matches,
        policy,
        result,
    )
    _load_calls(session, workspace_id, chats, actor, policy, result)
    _load_edit_versions(session, workspace_id, actor, policy, result)
    return result

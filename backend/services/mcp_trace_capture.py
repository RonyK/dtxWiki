from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.context import ContextClaim, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItemVersion
from backend.models.mcp_chat import McpChatArtifactKind, McpChatArtifactVerb
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient
from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import (
    CONTEXT_EDIT_TOOLS,
    ArtifactLinkCandidate,
    McpChatService,
    mcp_session_trace_uuid,
)
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.mcp_read_projection import (
    PURE_READ_TOOLS,
    validate_read_projection,
)
from backend.services.mcp_trace_redaction import (
    redact_edit_receipt,
    redact_error_details,
    redact_tool_capture,
)
from backend.services.retrieval.identity import ranked_result_document_id

ERROR_RESULT_STATUSES = {"failed", "rejected_by_policy", "conflict"}
MCP_CLIENT_LAST_USED_COALESCE = timedelta(minutes=5)


def touch_mcp_client_last_used(client: MCPClient, *, used_at: datetime) -> bool:
    """Touch at most once per five minutes; return whether the row changed."""

    if (
        client.last_used_at is not None
        and client.last_used_at >= used_at - MCP_CLIENT_LAST_USED_COALESCE
    ):
        return False
    client.last_used_at = used_at
    return True


def persist_mcp_tool_trace(
    *,
    session: Session,
    actor: ActorContext,
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    mcp_session_id: str,
    started_at: datetime,
    completed_at: datetime,
    latency_ms: int,
    chat_service: McpChatService | None = None,
    create_chat_session: bool = True,
    read_projection: dict[str, Any] | None = None,
    edit_first_change: dict[str, Any] | None = None,
) -> MCPToolCall | None:
    arguments, payload = redact_tool_capture(tool_name, arguments, payload)
    envelope = payload.get("envelope", {})
    workspace_id = _workspace_id(envelope, actor)
    request_id = _uuid_or_none(envelope.get("request_id"))
    if workspace_id is None or request_id is None:
        return None

    client = resolve_trace_mcp_client(
        session,
        workspace_id=workspace_id,
        actor=actor,
    )
    touch_mcp_client_last_used(client, used_at=completed_at)
    tool_call = MCPToolCall(
        client_id=client.id,
        workspace_id=workspace_id,
        mcp_session_id=mcp_session_trace_uuid(mcp_session_id),
        request_id=request_id,
        tool_name=tool_name,
        input_summary=_jsonable(arguments),
        result_status=str(envelope.get("result_status") or "failed"),
        result_summary=envelope.get("result_summary"),
        result_detail=_result_detail(
            tool_name,
            payload,
            read_projection=read_projection,
            edit_first_change=edit_first_change,
        ),
        warnings=_jsonable(envelope.get("warnings") or []),
        error_code=envelope.get("error_code"),
        audit_event_id=_uuid_or_none(envelope.get("audit_event_id")),
        change_set_id=_uuid_or_none(_data(payload).get("change_set_id")),
        agent_input_bundle_id=_uuid_or_none(_data(payload).get("bundle_id")),
        provider=str(envelope.get("provider") or "unknown"),
        model_id=str(envelope.get("model_id") or "unknown"),
        model_version=envelope.get("model_version"),
        params_summary=_jsonable(envelope.get("params_summary") or {}),
        client_self_reported=bool(envelope.get("client_self_reported") or False),
        outcome=envelope.get("result_status"),
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=latency_ms,
    )
    session.add(tool_call)
    session.flush()

    # A call the server rejected before dispatch (no conversation to attach to)
    # must not open a chat. Everything that actually ran still registers its
    # token and remains visible in the Chats activity surface, including
    # discovery calls, because its trace must stay auditable. Clients can echo
    # that token on later calls to keep related activity in the same chat.
    if not create_chat_session:
        return tool_call

    chat_service = chat_service or McpChatService()
    chat = chat_service.upsert_session_from_tool_call(
        session=session,
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=mcp_session_id,
        tool_call=tool_call,
        owner_user_id=actor.user_id,
    )
    if tool_call.result_status not in ERROR_RESULT_STATUSES:
        candidates = derive_artifact_links(
            session=session,
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
        )
        safe_read = validate_read_projection(read_projection)
        if not candidates and safe_read is not None and tool_name in PURE_READ_TOOLS:
            # Slug-based Agent reads have no request UUID. The served snapshot
            # proves their single artifact without guessing from current heads.
            # Do not change the existing Context conversation-base ledger.
            candidates = [
                ArtifactLinkCandidate(
                    McpChatArtifactKind(safe_read["resource_kind"]),
                    UUID(safe_read["resource_id"]),
                    McpChatArtifactVerb.REFERENCED,
                )
            ]
        for candidate in candidates:
            chat_service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=tool_call,
                resource_kind=candidate.resource_kind,
                resource_id=candidate.resource_id,
                verb=candidate.verb,
                version_id=candidate.version_id,
                root_hash=candidate.root_hash,
            )
    return tool_call


def derive_artifact_links(
    *,
    session: Session,
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> list[ArtifactLinkCandidate]:
    data = _data(payload)
    candidates: list[ArtifactLinkCandidate] = []
    if tool_name == "submit_revision_for_review":
        # The revision writes a new draft version (update OR create) bound to the
        # new change set; resolve the page from new_change_set_id so BOTH revision
        # sources are linked in chat provenance — not just the update path
        # (Codex #11). Fall back to the update.page_id argument if the change set
        # has no resolvable version. A `create` revision links the fresh page as
        # CREATED; an `update` revision as EDITED.
        page_id = None
        change_set_id = _uuid_or_none(data.get("new_change_set_id"))
        if change_set_id is not None:
            page_id = session.scalar(
                select(ContextPageVersion.page_id).where(
                    ContextPageVersion.change_set_id == change_set_id
                )
            )
        if page_id is None:
            page_id = _uuid_or_none((arguments.get("update") or {}).get("page_id"))
        if page_id is not None:
            verb = (
                McpChatArtifactVerb.CREATED
                if arguments.get("create")
                else McpChatArtifactVerb.EDITED
            )
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    page_id,
                    verb,
                )
            )
    elif tool_name == "create_context_page":
        # `data` is a PagePayload (`id`/`version_id`).
        page_id = _uuid_or_none(data.get("id"))
        if page_id is not None:
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    page_id,
                    McpChatArtifactVerb.CREATED,
                    version_id=_uuid_or_none(data.get("version_id")),
                    root_hash=data.get("content_hash"),
                )
            )
    elif tool_name == "begin_context_edit":
        # Opens or resumes the caller's own editing session -- no content
        # changes yet, so this is closer to a read-like touch than a write.
        page_id = _uuid_or_none(data.get("id"))
        if page_id is not None:
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    page_id,
                    McpChatArtifactVerb.REFERENCED,
                    version_id=_uuid_or_none(data.get("version_id")),
                )
            )
    elif tool_name in {
        "update_context_edit",
        "submit_context_edit_for_review",
        "patch_context_edit",
    }:
        page_id = _uuid_or_none(data.get("id"))
        if page_id is not None:
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    page_id,
                    McpChatArtifactVerb.EDITED,
                    version_id=_uuid_or_none(data.get("version_id")),
                    root_hash=data.get("content_hash"),
                )
            )
    elif tool_name == "discard_context_edit":
        # DiscardContextEditResult carries only version_id/page_archived --
        # the page id lives in the request.
        page_id = _uuid_or_none(arguments.get("page_id"))
        if page_id is not None:
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    page_id,
                    McpChatArtifactVerb.EDITED,
                    version_id=_uuid_or_none(data.get("version_id")),
                )
            )
    elif tool_name == "write_agent_page_draft":
        agent_page_id = _uuid_or_none(data.get("agent_page_id"))
        if agent_page_id is not None:
            verb = (
                McpChatArtifactVerb.EDITED
                if arguments.get("base_version_id")
                else McpChatArtifactVerb.CREATED
            )
            candidates.append(
                ArtifactLinkCandidate(
                    McpChatArtifactKind.AGENT_PAGE,
                    agent_page_id,
                    verb,
                )
            )
    elif tool_name in {"read", "read_page", "read_agent_page"}:
        candidates.extend(_read_links(session, tool_name, arguments))
    elif tool_name == "fetch":
        candidates.extend(_fetch_links(session, arguments))
    elif tool_name in {"read_context_outline", "read_structure_map"}:
        # Resolve from the response document, not the request target: the
        # target may be a concept path or a version id, and the document is
        # the page the read actually landed on. The read ledger (design doc
        # section 9) records exactly the version this read served --
        # read_structure_map's document already names it "root_hash" (its
        # own vocabulary); read_context_outline calls the same normalized
        # value "content_hash".
        #
        # Unlike the write tools, these two output schemas are flat (no
        # "data" wrapper -- see ReadContextOutlineOutput/ReadStructureMapOutput
        # in schemas/mcp/read_tools.py), so the document lives at the payload's
        # top level, not under payload["data"].
        document = payload.get("document") if isinstance(payload, dict) else None
        candidates.extend(
            _link_if_uuid(
                McpChatArtifactKind.CONTEXT_PAGE,
                _uuid_or_none(document.get("page_id") if document else None),
                McpChatArtifactVerb.REFERENCED,
                version_id=_uuid_or_none(
                    document.get("version_id") if document else None
                ),
                root_hash=(
                    (document.get("root_hash") or document.get("content_hash"))
                    if document
                    else None
                ),
            )
        )
    elif tool_name == "read_concept":
        candidates.extend(_read_concept_links(payload))
    elif tool_name == "search":
        candidates.extend(_search_links(session, data))
    elif tool_name == "grep_wiki":
        candidates.extend(_grep_links(payload))
    elif tool_name == "cite":
        candidates.extend(_cite_links(session, data))
    elif tool_name == "upload_image":
        # The response always carries asset_id, whether the call was page-bound
        # or session-bound (mutually exclusive owners on ContextMediaAsset) --
        # so a single branch covers both call shapes without inspecting
        # arguments. asset_id also survives a later claim onto a page (Phase
        # 5a moves session_id -> page_id in place, same id), so this edge
        # stays resolvable for Phase 5c's orphan check either way.
        asset_id = _uuid_or_none(data.get("asset_id"))
        candidates.extend(
            _link_if_uuid(
                McpChatArtifactKind.CONTEXT_MEDIA_ASSET,
                asset_id,
                McpChatArtifactVerb.CREATED,
            )
        )
    return _dedupe(candidates)


# Registry contract (design §5.6): every dispatchable tool either derives
# usage edges above or is explicitly declared edge-less here, so a new tool
# cannot silently opt out of the trail. test_mcp_trace_capture pins
# TOOL_HANDLERS == EDGE_DERIVING_TOOLS | EDGE_LESS_TOOLS.
EDGE_DERIVING_TOOLS = frozenset(
    {
        "submit_revision_for_review",
        "write_agent_page_draft",
        "fetch",
        "read",
        "read_page",
        "read_agent_page",
        "read_concept",
        "read_context_outline",
        "read_structure_map",
        "search",
        "grep_wiki",
        "cite",
        "upload_image",
        # P8.6
        "create_context_page",
        "begin_context_edit",
        "update_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
        "patch_context_edit",
    }
)
EDGE_LESS_TOOLS = frozenset(
    {
        # Catalog/enumeration surfaces — no single artifact is "used".
        "guide",
        "get_index",
        "list_my_workspaces",
        "list_changes",
        "what_changed",
        "list_agent_categories",
        "list_requested_changes",
        # P8.6: lists across many of the caller's own pages, like
        # list_changes/list_requested_changes above.
        "list_my_context_edits",
        # The legacy Pages tools are deregistered and their handlers are
        # gone, but these names stay: MCPToolCall rows written before the
        # removal still carry them, and this classification is applied to
        # stored history when a chat is re-rendered. Dropping the names
        # would silently reclassify those old rows, not dead-code-clean.
        "list_pages",
        "write_page",
        "submit_feedback",
        "build_agent_input_bundle",
        # Queue-drain surface: no single knowledge artifact is consumed.
        "wiki_maintenance",
    }
)


def _result_detail(
    tool_name: str,
    payload: dict[str, Any],
    *,
    read_projection: dict[str, Any] | None = None,
    edit_first_change: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Allowlisted edit receipts and separate internal served-read facts.

    Context-edit tools contribute the page and version their edit
    landed on (so a read can offer the session's cumulative diff and a discard
    can find the calls whose previews it must drop) and the server's edit
    receipt,
    re-cut and masked by ``redact_edit_receipt``. Nothing else in the
    response -- in particular no body -- is kept. Pure reads accept only
    the separately captured, revalidated private projection; never a field
    supplied in the response payload. Chats serializes edit receipts only.
    """

    envelope = payload.get("envelope") or {}
    if tool_name in PURE_READ_TOOLS and envelope.get("result_status") == "succeeded":
        safe_read = validate_read_projection(read_projection)
        return {"read_projection": safe_read} if safe_read else None
    if tool_name not in CONTEXT_EDIT_TOOLS:
        return None
    if envelope.get("result_status") in ERROR_RESULT_STATUSES:
        # A refused edit's details say which operation failed and where; keep
        # that structure (never the details' prose) for the failure card.
        error_details = redact_error_details(envelope.get("error_details"))
        return {"error_details": error_details} if error_details else None
    data = _data(payload)
    detail: dict[str, Any] = {}
    # Every context-edit response carries id/version_id (begin's PagePayload,
    # the other commands' ContextEditResult) except discard's small result,
    # which carries only version_id.
    page_id = _uuid_or_none(data.get("id"))
    if page_id is not None:
        detail["page_id"] = str(page_id)
    version_id = _uuid_or_none(data.get("version_id"))
    if version_id is not None:
        detail["version_id"] = str(version_id)
    receipt = redact_edit_receipt(
        data.get("edit_receipt"), first_change=edit_first_change
    )
    if receipt is not None:
        detail["edit_receipt"] = receipt
    return detail or None


def _workspace_id(envelope: dict[str, Any], actor: ActorContext) -> UUID | None:
    workspace_id = _uuid_or_none(envelope.get("workspace_id"))
    return workspace_id or actor.workspace_id


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _read_links(
    session: Session,
    tool_name: str,
    arguments: dict[str, Any],
) -> list[ArtifactLinkCandidate]:
    if tool_name == "read_page":
        page_id = _uuid_or_none(arguments.get("page_id"))
        return _link_if_uuid(
            McpChatArtifactKind.CONTEXT_PAGE,
            page_id,
            McpChatArtifactVerb.REFERENCED,
        )
    if tool_name == "read_agent_page":
        agent_page_id = _uuid_or_none(arguments.get("agent_page_id"))
        return _link_if_uuid(
            McpChatArtifactKind.AGENT_PAGE,
            agent_page_id,
            McpChatArtifactVerb.REFERENCED,
        )

    target_type = arguments.get("target_type")
    target_id = _uuid_or_none(arguments.get("target_id"))
    if target_type == "context_page_version" and target_id is not None:
        target_id = _page_id_for_version(session, target_id)
        target_type = "context_page"
    if target_type == "context_claim" and target_id is not None:
        target_id = _page_id_for_claim(session, target_id)
        target_type = "context_page"
    if target_type == "context_page":
        return _link_if_uuid(
            McpChatArtifactKind.CONTEXT_PAGE,
            target_id,
            McpChatArtifactVerb.REFERENCED,
        )
    if target_type == "library_item_version" and target_id is not None:
        target_id = _library_item_id_for_version(session, target_id)
        target_type = "library_item"
    if target_type == "library_artifact" and target_id is not None:
        target_id = _library_item_id_for_artifact(session, target_id)
        target_type = "library_item"
    if target_type == "library_item":
        return _link_if_uuid(
            McpChatArtifactKind.LIBRARY_ITEM,
            target_id,
            McpChatArtifactVerb.REFERENCED,
        )
    if target_type == "agent_page":
        return _link_if_uuid(
            McpChatArtifactKind.AGENT_PAGE,
            target_id,
            McpChatArtifactVerb.REFERENCED,
        )
    return []


def _fetch_links(
    session: Session,
    arguments: dict[str, Any],
) -> list[ArtifactLinkCandidate]:
    """Derive the artifact a fetch consumed.

    fetch reads one entity, so it belongs on the usage trail exactly like
    ``read`` -- it just names its target as an (id, type) pair instead of
    target_id/target_type. Nested kinds resolve to the artifact that owns
    them, so a chat links the page, not an internal claim id.
    """

    entity_id = _uuid_or_none(arguments.get("id"))
    entity_type = arguments.get("type")
    if entity_id is None:
        return []
    if entity_type == "context_claim":
        entity_id = _page_id_for_claim(session, entity_id)
        entity_type = "context_page"
    if entity_type == "context_page":
        return _link_if_uuid(
            McpChatArtifactKind.CONTEXT_PAGE,
            entity_id,
            McpChatArtifactVerb.REFERENCED,
        )
    if entity_type == "library_artifact":
        entity_id = _library_item_id_for_artifact(session, entity_id)
        entity_type = "library_item"
    if entity_type == "library_item":
        return _link_if_uuid(
            McpChatArtifactKind.LIBRARY_ITEM,
            entity_id,
            McpChatArtifactVerb.REFERENCED,
        )
    if entity_type == "agent_page":
        return _link_if_uuid(
            McpChatArtifactKind.AGENT_PAGE,
            entity_id,
            McpChatArtifactVerb.REFERENCED,
        )
    # feedback and change_set are audit-class, not knowledge artifacts.
    return []


def _read_concept_links(data: dict[str, Any]) -> list[ArtifactLinkCandidate]:
    if data.get("target_kind") == "library_item":
        return _link_if_uuid(
            McpChatArtifactKind.LIBRARY_ITEM,
            _uuid_or_none(data.get("target_id")),
            McpChatArtifactVerb.REFERENCED,
        )
    page_id = _uuid_or_none(data.get("page_id"))
    return _link_if_uuid(
        McpChatArtifactKind.CONTEXT_PAGE,
        page_id,
        McpChatArtifactVerb.REFERENCED,
    )


def _grep_links(data: dict[str, Any]) -> list[ArtifactLinkCandidate]:
    # Preserve the top matches as an exposure/audit trail, but do not treat
    # their co-occurrence as consumption. A later focused read records its own
    # REFERENCED edge if the agent actually opens one of these pages.
    candidates: list[ArtifactLinkCandidate] = []
    seen: set[UUID] = set()
    for match in data.get("matches") or []:
        if len(seen) >= 5:
            break
        if not isinstance(match, dict):
            continue
        page_id = _uuid_or_none(match.get("page_id"))
        if page_id is None or page_id in seen:
            continue
        seen.add(page_id)
        candidates.append(
            ArtifactLinkCandidate(
                McpChatArtifactKind.CONTEXT_PAGE,
                page_id,
                McpChatArtifactVerb.SEARCH_MATCH,
            )
        )
    return candidates


def _search_links(
    session: Session,
    data: dict[str, Any],
) -> list[ArtifactLinkCandidate]:
    ranked_results = data.get("ranked_results")
    if not isinstance(ranked_results, list):
        return []
    candidates: list[ArtifactLinkCandidate] = []
    for result in ranked_results[:5]:
        if not isinstance(result, dict):
            continue
        result_id = ranked_result_document_id(result)
        result_type = result.get("type")
        if result_type == "context_claim" and result_id is not None:
            result_id = _page_id_for_claim(session, result_id)
            result_type = "context_page"
        if result_type == "context_page":
            candidates.extend(
                _link_if_uuid(
                    McpChatArtifactKind.CONTEXT_PAGE,
                    result_id,
                    McpChatArtifactVerb.SEARCH_MATCH,
                )
            )
        elif result_type == "library_artifact" and result_id is not None:
            result_id = _library_item_id_for_artifact(session, result_id)
            result_type = "library_item"
        if result_type == "library_item":
            candidates.extend(
                _link_if_uuid(
                    McpChatArtifactKind.LIBRARY_ITEM,
                    result_id,
                    McpChatArtifactVerb.SEARCH_MATCH,
                )
            )
        elif result_type == "agent_page":
            candidates.extend(
                _link_if_uuid(
                    McpChatArtifactKind.AGENT_PAGE,
                    result_id,
                    McpChatArtifactVerb.SEARCH_MATCH,
                )
            )
    return candidates


def _cite_links(
    session: Session,
    data: dict[str, Any],
) -> list[ArtifactLinkCandidate]:
    candidates: list[ArtifactLinkCandidate] = []
    for candidate in data.get("citation_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        library_artifact_id = _uuid_or_none(candidate.get("library_artifact_id"))
        library_item_version_id = _uuid_or_none(
            candidate.get("library_item_version_id")
        )
        context_version_id = _uuid_or_none(
            candidate.get("cited_context_page_version_id")
        )
        if library_artifact_id is not None:
            library_item_id = _library_item_id_for_artifact(
                session, library_artifact_id
            )
            if library_item_id is not None:
                candidates.append(
                    ArtifactLinkCandidate(
                        McpChatArtifactKind.LIBRARY_ITEM,
                        library_item_id,
                        McpChatArtifactVerb.CITED,
                    )
                )
        elif library_item_version_id is not None:
            library_item_version = session.get(
                LibraryItemVersion, library_item_version_id
            )
            if library_item_version is not None:
                candidates.append(
                    ArtifactLinkCandidate(
                        McpChatArtifactKind.LIBRARY_ITEM,
                        library_item_version.library_item_id,
                        McpChatArtifactVerb.CITED,
                    )
                )
        elif context_version_id is not None:
            version = session.get(ContextPageVersion, context_version_id)
            if version is not None:
                candidates.append(
                    ArtifactLinkCandidate(
                        McpChatArtifactKind.CONTEXT_PAGE,
                        version.page_id,
                        McpChatArtifactVerb.CITED,
                    )
                )
    return candidates


def _page_id_for_version(session: Session, version_id: UUID) -> UUID | None:
    version = session.get(ContextPageVersion, version_id)
    return version.page_id if version is not None else None


def _page_id_for_claim(session: Session, claim_id: UUID) -> UUID | None:
    claim = session.get(ContextClaim, claim_id)
    if claim is None:
        return None
    return _page_id_for_version(session, claim.page_version_id)


def _library_item_id_for_version(session: Session, version_id: UUID) -> UUID | None:
    library_item_version = session.get(LibraryItemVersion, version_id)
    return (
        library_item_version.library_item_id
        if library_item_version is not None
        else None
    )


def _library_item_id_for_artifact(session: Session, resource_id: UUID) -> UUID | None:
    artifact = session.get(LibraryArtifact, resource_id)
    if artifact is None:
        return None
    return _library_item_id_for_version(session, artifact.library_item_version_id)


def _link_if_uuid(
    kind: McpChatArtifactKind,
    resource_id: UUID | None,
    verb: McpChatArtifactVerb,
    *,
    version_id: UUID | None = None,
    root_hash: str | None = None,
) -> list[ArtifactLinkCandidate]:
    if resource_id is None:
        return []
    return [ArtifactLinkCandidate(kind, resource_id, verb, version_id, root_hash)]


def _dedupe(
    candidates: list[ArtifactLinkCandidate],
) -> list[ArtifactLinkCandidate]:
    seen: set[tuple[McpChatArtifactKind, UUID, McpChatArtifactVerb]] = set()
    deduped: list[ArtifactLinkCandidate] = []
    for candidate in candidates:
        key = (candidate.resource_kind, candidate.resource_id, candidate.verb)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped

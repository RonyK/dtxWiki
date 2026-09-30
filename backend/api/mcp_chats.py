from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    WorkspaceEditorAccessDep,
    WorkspaceOwnerAccessDep,
    WorkspaceSessionFactoryDep,
    read_service_error_response,
)
from backend.schemas.common import RESTErrorBody
from backend.schemas.mcp_chat import (
    McpChatArchiveResponse,
    McpChatArtifactKind,
    McpChatArtifactLinksResponse,
    McpChatDetailResponse,
    McpChatExportResponse,
    McpChatListResponse,
    McpChatPreviewPurgeResponse,
    McpChatProvenanceResponse,
    McpChatTitleUpdateRequest,
)
from backend.services.read_errors import ReadServiceError

router = APIRouter(prefix="/workspaces", tags=["mcp-chats"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}



@router.get(
    "/{workspace_id}/mcp-chats",
    response_model=McpChatListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_mcp_chats(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    q: str | None = None,
    client: UUID | None = None,
    period: Literal["today", "week", "month", "all"] = "all",
    writes_only: bool = False,
    archived: bool = False,
    sort: Literal["last_activity_desc", "write_count_desc"] = "last_activity_desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.mcp_chat.list_chats(
                    session=session,
                    workspace_id=workspace_id,
                    client_id=client,
                    period=period,
                    q=q,
                    writes_only=writes_only,
                    archived=archived,
                    sort=sort,
                    limit=limit,
                    cursor=cursor,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)

@router.get(
    "/{workspace_id}/mcp-chats/export",
    response_model=McpChatExportResponse,
    responses=REST_ERROR_RESPONSES,
)
def export_mcp_chats(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    since: datetime | None = None,
    until: datetime | None = None,
    format: Literal["json"] = "json",
    limit: Annotated[int, Query(ge=1, le=5000)] = 5000,
    cursor: str | None = None,
):
    del format
    try:
        with session_factory() as session:
            return {
                "data": svc.mcp_chat.export_chats(
                    session=session,
                    workspace_id=workspace_id,
                    since=since,
                    until=until,
                    limit=limit,
                    cursor=cursor,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/mcp-chats/resource-provenance",
    response_model=McpChatProvenanceResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_artifact_provenance(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    kind: McpChatArtifactKind,
    id: UUID,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.mcp_chat.list_artifact_provenance(
                    session=session,
                    workspace_id=workspace_id,
                    resource_kind=kind,
                    resource_id=id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/mcp-chats/resource-links",
    response_model=McpChatArtifactLinksResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_artifact_links(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    since: datetime | None = None,
    until: datetime | None = None,
    verb: Literal[
        "created", "edited", "cited", "referenced", "search_match"
    ]
    | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.mcp_chat.list_artifact_links(
                    session=session,
                    workspace_id=workspace_id,
                    since=since,
                    until=until,
                    verb=verb,
                    limit=limit,
                    cursor=cursor,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/mcp-chats/{chat_session_id}",
    response_model=McpChatDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def read_mcp_chat(
    svc: ServicesDep,
    workspace_id: UUID,
    chat_session_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.mcp_chat.read_chat(
                    session=session,
                    workspace_id=workspace_id,
                    chat_session_id=chat_session_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.patch(
    "/{workspace_id}/mcp-chats/{chat_session_id}",
    response_model=McpChatDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_mcp_chat_title(
    svc: ServicesDep,
    workspace_id: UUID,
    chat_session_id: UUID,
    request_body: McpChatTitleUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.mcp_chat.update_title(
                    session=session,
                    workspace_id=workspace_id,
                    chat_session_id=chat_session_id,
                    title=request_body.title,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}/mcp-chats/{chat_session_id}",
    response_model=McpChatArchiveResponse,
    responses=REST_ERROR_RESPONSES,
)
def archive_mcp_chat(
    svc: ServicesDep,
    workspace_id: UUID,
    chat_session_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.mcp_chat.archive(
                    session=session,
                    workspace_id=workspace_id,
                    chat_session_id=chat_session_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/mcp-chats/{chat_session_id}/unarchive",
    response_model=McpChatArchiveResponse,
    responses=REST_ERROR_RESPONSES,
)
def unarchive_mcp_chat(
    svc: ServicesDep,
    workspace_id: UUID,
    chat_session_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.mcp_chat.unarchive(
                    session=session,
                    workspace_id=workspace_id,
                    chat_session_id=chat_session_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}/mcp-chats/{chat_session_id}/previews",
    response_model=McpChatPreviewPurgeResponse,
    responses=REST_ERROR_RESPONSES,
)
def purge_mcp_chat_previews(
    svc: ServicesDep,
    workspace_id: UUID,
    chat_session_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    """Delete the edit previews kept for one chat's calls (owner retention).

    Digests, sizes and receipts' counts are kept as the audit record; only the
    stored text excerpts are cleared, and the deletion is itself audited.
    """

    try:
        with session_factory.begin() as session:
            return {
                "data": svc.mcp_chat.purge_chat_previews(
                    session=session,
                    workspace_id=workspace_id,
                    chat_session_id=chat_session_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)

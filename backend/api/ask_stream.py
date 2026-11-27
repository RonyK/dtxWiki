import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.api.deps import (
    AskServiceDep,
    RestActorDep,
    SessionFactoryDep,
    WorkspaceAskAccessDep,
    WorkspaceSessionFactoryDep,
    read_service_error_response,
)
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.domain.input_policy import InputPolicyError
from backend.schemas.ask import (
    AskModelListResponse,
    AskSteerRequest,
    AskStreamTurnCreate,
    AskTurnDetailResponse,
)
from backend.schemas.common import RESTErrorBody
from backend.services.ask_runtime import (
    AskTurnEvent,
    AskTurnIdle,
    AskTurnPreview,
    AskTurnState,
)
from backend.services.ask_service import InvalidUserMessageError
from backend.services.read_errors import ReadServiceError

router = APIRouter(prefix="/workspaces/{workspace_id}/ask", tags=["read"])
_ERRORS = {status: {"model": RESTErrorBody} for status in (401, 403, 404, 409, 422)}


def _runtime(request: Request):
    return get_container_from_app(request).ask_runtime


async def _server_sent_events(events: AsyncIterator[AskTurnEvent]):
    async for event in events:
        yield _frame(event)


def _frame(event: AskTurnEvent) -> str:
    """One Server-Sent Events frame."""

    match event:
        case AskTurnState(turn=turn):
            return f"event: state\ndata: {turn.model_dump_json()}\n\n"
        case AskTurnPreview(text=text, reset=reset):
            data = {"text": text, "reset": True} if reset else {"text": text}
            return f"event: delta\ndata: {json.dumps(data)}\n\n"
        case AskTurnIdle():
            return ": keepalive\n\n"


def _invalid_message(exc):
    return build_rest_error_response(
        error_code="invalid_user_message",
        message=str(exc),
        status_code=422,
    )


@router.get("/models", response_model=AskModelListResponse, responses=_ERRORS)
def list_ask_models(
    request: Request,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    control_factory: SessionFactoryDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    choices = get_container_from_app(request).ask_connections.list_choices(
        control_factory,
        session_factory,
        workspace_id,
        actor,
    )
    return {"data": {"items": choices}}


@router.post(
    "/sessions/{ask_session_id}/turns/stream",
    status_code=202,
    response_model=AskTurnDetailResponse,
    responses=_ERRORS,
)
async def submit_ask_stream_turn(
    request: Request,
    workspace_id: UUID,
    ask_session_id: UUID,
    request_body: AskStreamTurnCreate,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        # Stream submissions join the session's queue; only stop, replace and
        # steer name a running turn.
        detail, _job = await _runtime(request).submit(
            session_factory,
            workspace_id,
            ask_session_id,
            request_body,
            actor,
            replace_turn_id=request_body.replace_turn_id,
        )
    except (InvalidUserMessageError, InputPolicyError) as exc:
        return _invalid_message(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": detail}


@router.get(
    "/turns/{ask_turn_id}/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, **_ERRORS},
)
async def read_ask_stream(
    request: Request,
    workspace_id: UUID,
    ask_turn_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    # Authorized once, when the stream opens; a reconnect is authorized again.
    runtime = _runtime(request)
    try:
        detail = await run_in_threadpool(
            runtime.get_turn,
            session_factory,
            workspace_id,
            ask_turn_id,
            actor,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return StreamingResponse(
        _server_sent_events(
            runtime.events(session_factory, workspace_id, detail.id, actor)
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/turns/{ask_turn_id}/stop", response_model=AskTurnDetailResponse, responses=_ERRORS
)
async def stop_ask_turn(
    request: Request,
    workspace_id: UUID,
    ask_turn_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        detail = await _runtime(request).stop(
            session_factory, workspace_id, ask_turn_id, actor
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": detail}


@router.post(
    "/turns/{ask_turn_id}/steer",
    response_model=AskTurnDetailResponse,
    responses=_ERRORS,
)
async def steer_ask_turn(
    request: Request,
    workspace_id: UUID,
    ask_turn_id: UUID,
    request_body: AskSteerRequest,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        detail = await _runtime(request).steer(
            session_factory,
            workspace_id,
            ask_turn_id,
            request_body,
            actor,
        )
    except (InvalidUserMessageError, InputPolicyError) as exc:
        return _invalid_message(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": detail}


@router.get(
    "/turns/{ask_turn_id}/search",
    response_model=AskTurnDetailResponse,
    responses=_ERRORS,
)
def search_failed_ask_turn(
    workspace_id: UUID,
    ask_turn_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceAskAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    ask_service: AskServiceDep,
):
    try:
        with session_factory() as session:
            detail = ask_service.search_failed_turn(
                session, workspace_id, ask_turn_id, actor
            )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": detail}

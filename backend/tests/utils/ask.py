"""Ask a question inside one test transaction.

The REST route answers through ``AskRuntime``, which commits the
question before the answer is written. Tests about what a turn stores run the
same steps in the caller's session instead.
"""

import asyncio
from uuid import UUID

from sqlalchemy.orm import Session

from backend.llm.binding import ModelBinding
from backend.policy.auth import ActorContext
from backend.schemas.ask import AskTurnCreate, AskTurnDetail
from backend.services.ask_service import AskService


async def _ignore_delta(_text: str) -> None:
    pass


def create_turn_in_session(
    service: AskService,
    *,
    session: Session,
    workspace_id: UUID,
    ask_session_id: UUID,
    request: AskTurnCreate,
    actor_kind: str,
    actor_id: str,
    actor: ActorContext | None = None,
    binding: ModelBinding | None = None,
) -> tuple[AskTurnDetail, list[str]]:
    turn_id = service.submit_turn(
        session,
        workspace_id=workspace_id,
        ask_session_id=ask_session_id,
        request=request,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )
    prepared = service.prepare_turn(
        session, workspace_id, turn_id, actor, binding=binding
    )
    composed = (
        service.compose_evidence(prepared)
        if binding is None
        else asyncio.run(service.stream_turn(prepared, binding, on_delta=_ignore_delta))
    )
    service.finish_turn(session, workspace_id, prepared, composed)
    detail = service.get_turn(session, workspace_id, turn_id, actor)
    return detail, list(detail.warning_codes)

"""Ask a question inside one test transaction.

The REST route answers through ``AskService.run_turn``, which commits the
question before the answer is written. Tests about what a turn stores run the
same steps in the caller's session instead.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext
from backend.schemas.ask import AskTurnCreate, AskTurnDetail
from backend.services.ask_service import AskService


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
) -> tuple[AskTurnDetail, list[str]]:
    turn_id = service.submit_turn(
        session,
        workspace_id=workspace_id,
        ask_session_id=ask_session_id,
        request=request,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )
    prepared = service.prepare_turn(session, workspace_id, turn_id, actor)
    return service.finish_turn(
        session,
        workspace_id,
        prepared,
        service.compose_turn(prepared),
        actor=actor,
    )

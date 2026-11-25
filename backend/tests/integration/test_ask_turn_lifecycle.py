"""A question is kept once it is accepted, whatever happens to its answer."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app import main as app_main
from backend.models.ask import AskTurn
from backend.models.workspace import Workspace
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.schemas.read import SearchPayload
from backend.services.ask_service import AskAccessRevokedError, AskService
from backend.services.read_errors import RejectedByPolicyError

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


class _NoEvidenceSearch:
    def search(self, **_kwargs):
        return SearchPayload(
            ranked_results=[],
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
        )


def test_question_is_kept_when_its_answer_fails_or_the_server_restarts(
    session_factory, monkeypatch
):
    service = AskService(search_service=_NoEvidenceSearch())
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        ask_session_id = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
        ).id

    def ask(message, *, after_turn_id=..., authorize=None):
        request = AskTurnCreate(user_message=message)
        if after_turn_id is not ...:
            request = AskTurnCreate(user_message=message, after_turn_id=after_turn_id)
        return service.run_turn(
            session_factory,
            workspace_id=workspace_id,
            ask_session_id=ask_session_id,
            request=request,
            actor_kind="human",
            actor_id=ACTOR_ID,
            authorize=authorize,
        )

    def turns():
        with session_factory() as session:
            return [
                (turn.turn_no, turn.user_message, turn.status, turn.error_code)
                for turn in session.scalars(
                    select(AskTurn)
                    .where(AskTurn.ask_session_id == ask_session_id)
                    .order_by(AskTurn.turn_no)
                )
            ]

    # Failing after the question was stored keeps the question.
    def broken_search(*_args, **_kwargs):
        raise RuntimeError("search is down")

    monkeypatch.setattr(service, "_retrieve", broken_search)
    with pytest.raises(RuntimeError, match="search is down"):
        ask("first question")
    monkeypatch.undo()

    # Losing access while the answer is written keeps the question, too.
    def revoked():
        raise AskAccessRevokedError

    with pytest.raises(AskAccessRevokedError):
        ask("second question", authorize=revoked)

    # One question at a time per session.
    with session_factory.begin() as session:
        pending_id = service.submit_turn(
            session,
            workspace_id=workspace_id,
            ask_session_id=ask_session_id,
            request=AskTurnCreate(user_message="third question"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
    with pytest.raises(RejectedByPolicyError) as busy:
        ask("fourth question")
    assert busy.value.error_code == "ask_turn_in_progress"

    # A serving process that starts finds nothing still answering the pending
    # question: it ends as an internal error. `make backend-dev` serves through
    # the lazy app; `dtxwiki serve` and Desktop pass create_serving_app to the
    # runtime, which their own tests pin.
    class _PreviousServer:
        state = SimpleNamespace(
            container=SimpleNamespace(
                db=SimpleNamespace(session_factory=session_factory)
            )
        )

        async def __call__(self, scope, receive, send):
            return None

    monkeypatch.setattr(app_main, "create_app", lambda **_kwargs: _PreviousServer())
    asyncio.run(app_main._LazyASGIApp()({"type": "lifespan"}, None, None))

    # A question asked after a turn the asker has not seen is refused.
    with pytest.raises(RejectedByPolicyError) as stale:
        ask("fifth question", after_turn_id=None)
    assert stale.value.error_code == "ask_session_changed"

    detail, _warnings = ask("sixth question", after_turn_id=pending_id)
    assert (detail.status, detail.error_code) == ("completed", None)
    assert turns() == [
        (1, "first question", "failed", "internal_error"),
        (2, "second question", "failed", "access_revoked"),
        (3, "third question", "failed", "server_restarted"),
        (4, "sixth question", "completed", None),
    ]

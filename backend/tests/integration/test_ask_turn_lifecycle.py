"""A question is kept once it is accepted, whatever happens to its answer."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app import main as app_main
from backend.app.container import assemble
from backend.app.db import make_engine
from backend.app.settings import Settings
from backend.llm.streaming import SteerUpdate
from backend.models.ask import AskTurn
from backend.models.workspace import Workspace
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.schemas.read import SearchPayload
from backend.services.ask_service import (
    LLM_COMPOSER_RETRYABLE_WARNING,
    AskAccessRevokedError,
    AskGenerationError,
    AskService,
    ComposedTurn,
)
from backend.services.page_service import PageService
from backend.services.read_errors import RejectedByPolicyError
from backend.tests.conftest import authenticate_test_client_in_workspace

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


def test_ask_stream_accepts_queue_steers_and_replaces_current_turn(
    session_factory, tmp_path, request, monkeypatch
):
    # SSE, stop, and the worker must use independent database connections.
    path = tmp_path / "ask-stream.sqlite"
    with (
        sqlite3.connect(path) as target,
        session_factory.kw["bind"].connect() as source,
    ):
        source.connection.driver_connection.backup(target)
    engine = make_engine(f"sqlite+pysqlite:///{path}")
    request.addfinalizer(engine.dispose)
    session_factory = sessionmaker(
        engine, expire_on_commit=False, info=session_factory.kw["info"]
    )
    page_service = PageService(session_factory)
    container = assemble(
        Settings(),
        session_factory=session_factory,
        page_service=page_service,
    )
    app = app_main.create_app(container=container, bootstrap_on_startup=False)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace.labs_policy = {"ask_enabled": True}
        prefix = f"/api/v1/workspaces/{workspace.id}/ask"
    page_service.write_page(
        workspace_slug="default",
        slug="queue-evidence",
        title="Queue Evidence",
        body_md="# Queue Evidence\n\nAsk queue evidence confirms this example.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    first_started, first_preview, second_started = Event(), Event(), Event()
    started = []

    def resolve(_control, _workspace, _id, _actor, selection):
        return SimpleNamespace(
            steer=selection.model_profile_id == "native",
            context_window=None,
            target={"model": selection.model_profile_id},
        )

    async def generate(prepared, binding, *, on_delta, next_steer, on_steer):
        question = prepared.question
        started.append(question)
        if question.endswith("first"):
            await on_delta("Provisional answer. ")
            first_started.set()
            steer = await next_steer()
            await on_steer(SteerUpdate(steer.id, "applied"))
            return ComposedTurn(
                steer.text, (), {"target": binding.target, "outcome": "completed"}
            )
        if question.endswith("second"):
            second_started.set()
            await asyncio.Event().wait()
        if question.endswith("failure"):
            assert prepared.history == (
                ("Queue evidence second\n\nadd detail", "Replacement answer"),
            )
            raise AskGenerationError(LLM_COMPOSER_RETRYABLE_WARNING)
        return ComposedTurn(
            "Replacement answer", (), {"target": binding.target, "outcome": "completed"}
        )

    runtime = container.ask_runtime
    monkeypatch.setattr(runtime.connections, "resolve", resolve)
    monkeypatch.setattr(runtime.service, "stream_turn", generate)
    events = runtime.events

    async def observed_events(*args):
        async for event in events(*args):
            if event.startswith("event: delta"):
                first_preview.set()
            yield event

    monkeypatch.setattr(runtime, "events", observed_events)
    with TestClient(app) as client:
        authenticate_test_client_in_workspace(
            client,
            session_factory,
            email="ask-stream-queue@example.com",
        )
        assert client.get(f"{prefix}/models").json()["data"] == {"items": []}
        created = client.post(f"{prefix}/sessions", json={})
        assert created.status_code == 201, created.text
        session_url = f"{prefix}/sessions/{created.json()['data']['id']}"
        connection_id = str(uuid4())

        def submit(question, *, profile="plain", replace=None, request_id=None):
            body = {
                "user_message": question,
                "request_id": request_id or str(uuid4()),
                "selection": {
                    "scope": "workspace",
                    "connection_id": connection_id,
                    "model_profile_id": profile,
                },
                "replace_turn_id": replace,
                "after_turn_id": None,
            }
            response = client.post(f"{session_url}/turns/stream", json=body)
            assert response.status_code == 202, response.text
            return response.json()["data"]

        def turn_url(turn):
            return f"{prefix}/turns/{turn['id']}"

        request_id = str(uuid4())
        first = submit("Queue evidence first", profile="native", request_id=request_id)
        assert first["status"] == "queued"
        assert first_started.wait(3)
        legacy_stale = client.post(
            f"{session_url}/turns",
            json={"user_message": "Queue evidence stale", "after_turn_id": None},
        )
        assert legacy_stale.status_code == 409, legacy_stale.text
        assert legacy_stale.json()["error_code"] == "ask_session_changed"
        repeated = submit(
            "Queue evidence first", profile="native", request_id=request_id
        )
        assert repeated["id"] == first["id"]
        second = submit("Queue evidence second")
        cancelled = submit("Queue evidence cancelled")
        failed = submit("Queue evidence failure")
        assert second["status"] == failed["status"] == "queued"
        assert (
            client.post(f"{turn_url(cancelled)}/stop").json()["data"]["status"]
            == "stopped"
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            stream = executor.submit(client.get, f"{turn_url(first)}/stream")
            assert first_preview.wait(3)
            steered = client.post(
                f"{turn_url(first)}/steer",
                json={"request_id": str(uuid4()), "user_message": "Continue briefly"},
            )
            assert steered.status_code == 200, steered.text
            assert steered.json()["data"]["steering_messages"][0]["status"] == "waiting"
            payload = stream.result(timeout=4).text
            assert "event: delta" in payload and "Provisional answer" in payload
            assert '"status":"applied"' in payload and "Continue briefly" in payload, (
                payload
            )

        assert second_started.wait(3)
        replaced = submit("add detail", replace=second["id"])
        assert replaced["status"] == "pending" and replaced["started_at"]
        assert replaced["user_message"] == "Queue evidence second\n\nadd detail"
        terminal = client.get(f"{turn_url(failed)}/stream")
        assert (
            '"status":"failed"' in terminal.text
            and '"can_search":true' in terminal.text
        )
        fallback = client.get(f"{turn_url(failed)}/search")
        assert fallback.status_code == 200, fallback.text
        assert "Queue Evidence" in fallback.json()["data"]["assistant_message"]
        turns = client.get(session_url).json()["data"]["turns"]
        assert [turn["status"] for turn in turns] == [
            "completed",
            "stopped",
            "stopped",
            "failed",
            "completed",
        ]
        assert started == [
            "Queue evidence first",
            "Queue evidence second",
            "Queue evidence second\n\nadd detail",
            "Queue evidence failure",
        ]

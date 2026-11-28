"""A question is kept once it is accepted, whatever happens to its answer."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app import main as app_main
from backend.app.container import assemble
from backend.app.db import make_engine
from backend.app.settings import Settings
from backend.models.ask import AskTurn
from backend.models.workspace import Workspace
from backend.services.ask_runtime import AskTurnPreview
from backend.services.ask_turn_store import fail_interrupted_ask_turns
from backend.services.ask_types import (
    LLM_COMPOSER_RETRYABLE_WARNING,
    AskGenerationError,
    ComposedTurn,
)
from backend.services.page_service import PageService
from backend.spi.llm import GenerationFeatures, SteerUpdate
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def test_ask_questions_queue_stream_steer_and_survive_failures(
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
    steered_preview = Event()
    started = []

    def resolve(_control, _workspace, _id, _actor, selection):
        native = selection.model_profile_id == "native"
        return SimpleNamespace(
            features=GenerationFeatures(streaming=True, steering=native),
            context_window=None,
            target={"model": selection.model_profile_id},
        )

    async def generate(prepared, binding, *, on_delta, next_steer, on_steer):
        question = prepared.question
        started.append(question)
        if question.endswith("first"):
            await on_delta("Provisional answer. ")
            first_started.set()
            # Bounded, so a broken stream fails the test instead of hanging it.
            steer = await asyncio.wait_for(next_steer(), 5)
            await on_steer(SteerUpdate(steer.id, "sent"))
            await on_steer(SteerUpdate(steer.id, "applied"))
            # The response after the instruction replaces the preview.
            await on_delta(steer.text)
            for _ in range(100):
                if steered_preview.is_set():
                    break
                await asyncio.sleep(0.05)
            return ComposedTurn(
                steer.text, (), {"target": binding.target, "outcome": "completed"}
            )
        if question.endswith("broken"):
            raise RuntimeError("composer bug")
        if question.endswith(("second", "unanswered")):
            second_started.set()
            await asyncio.sleep(30)
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
    monkeypatch.setattr(runtime.service.composer, "stream_turn", generate)
    events = runtime.events

    async def observed_events(*args):
        async for event in events(*args):
            if isinstance(event, AskTurnPreview) and event.text:
                first_preview.set()
                if event.reset and event.text == "Continue briefly":
                    steered_preview.set()
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

        def submit(question, *, profile="plain", request_id=None):
            body = {
                "user_message": question,
                "request_id": request_id or str(uuid4()),
                "selection": {
                    "scope": "workspace",
                    "connection_id": connection_id,
                    "model_profile_id": profile,
                },
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
            # A stream's first preview frame replaces whatever was shown, and
            # so does the response that follows an applied instruction.
            assert '{"text": "Provisional answer. ", "reset": true}' in payload
            assert steered_preview.is_set()
            assert '"status":"applied"' in payload and "Continue briefly" in payload, (
                payload
            )

        assert second_started.wait(3)
        # A model that takes no instructions while answering is asked again,
        # with the same selection, ahead of the queued question.
        instruction = {"request_id": str(uuid4()), "user_message": "add detail"}
        replaced = client.post(f"{turn_url(second)}/steer", json=instruction)
        assert replaced.status_code == 200, replaced.text
        replaced = replaced.json()["data"]
        assert replaced["status"] == "pending" and replaced["started_at"]
        assert replaced["user_message"] == "Queue evidence second\n\nadd detail"
        assert replaced["selection"]["model_profile_id"] == "plain"
        # A retry after a lost response gets the same new turn.
        retried = client.post(f"{turn_url(second)}/steer", json=instruction)
        assert retried.json()["data"]["id"] == replaced["id"], retried.text
        terminal = client.get(f"{turn_url(failed)}/stream")
        assert (
            '"status":"failed"' in terminal.text
            and '"can_search":true' in terminal.text
        )
        fallback = client.get(f"{turn_url(failed)}/search")
        assert fallback.status_code == 200, fallback.text
        assert "Queue Evidence" in fallback.json()["data"]["assistant_message"]
        # A failure outside the model keeps the question as an internal error.
        broken = submit("Queue evidence broken")
        assert (
            '"error_code":"internal_error"'
            in client.get(f"{turn_url(broken)}/stream").text
        )
        # Left answering when the server stops.
        second_started.clear()
        unanswered = submit("Queue evidence unanswered")
        assert second_started.wait(3)

    # Serving starts by ending questions nobody is answering any more
    # (bootstrap_core, which every serving path runs before it serves).
    fail_interrupted_ask_turns(session_factory)
    with session_factory() as session:
        stored = session.scalars(
            select(AskTurn)
            .where(AskTurn.ask_session_id == UUID(created.json()["data"]["id"]))
            .order_by(AskTurn.turn_no)
        ).all()
    assert [(turn.status, turn.error_code) for turn in stored] == [
        ("completed", None),
        ("stopped", None),
        ("stopped", None),
        ("failed", LLM_COMPOSER_RETRYABLE_WARNING),
        ("completed", None),
        ("failed", "internal_error"),
        ("failed", "server_restarted"),
    ]
    assert stored[-1].id == UUID(unanswered["id"])
    assert started == [
        "Queue evidence first",
        "Queue evidence second",
        "Queue evidence second\n\nadd detail",
        "Queue evidence failure",
        "Queue evidence broken",
        "Queue evidence unanswered",
    ]

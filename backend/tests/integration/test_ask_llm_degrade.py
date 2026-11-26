"""An unusable default configuration serves evidence with a warning.

Failures during model execution leave a failed turn with its error code.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.container import assemble
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.llm.adapter_registry import LLMAdapterRegistry
from backend.llm.adapters import build_builtin_llm_adapters
from backend.llm.binding import ModelBinding
from backend.llm.legacy import resolve_default_binding
from backend.llm.registry import resolve_embedding_provider, resolve_reranker
from backend.models.workspace import Workspace
from backend.services.ask_service import (
    LLM_ANSWER_REFUSED_WARNING,
    LLM_PROVIDER_UNAVAILABLE_WARNING,
)
from backend.spi.llm import CanonicalTextResult, TextGenerationPort
from backend.tests.conftest import (
    authenticate_test_client_in_workspace,
    captured_log_messages,
)

pytestmark = pytest.mark.integration


def _workspace_id(session_factory):
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _misconfigured_settings() -> Settings:
    # Provider selected but no key: the exact shape of the reported outage.
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="ask-degrade-secret",
        llm_provider="anthropic",
        anthropic_api_key=None,
    )


def test_resolve_default_binding_reports_missing_key():
    binding, reason = resolve_default_binding(
        _misconfigured_settings(), LLMAdapterRegistry(build_builtin_llm_adapters())
    )

    assert binding is None
    assert reason and "API key" in reason


def test_resolve_default_binding_reports_unknown_provider():
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="ask-degrade-secret",
        llm_provider="not-a-provider",
    )

    binding, reason = resolve_default_binding(
        settings, LLMAdapterRegistry(build_builtin_llm_adapters())
    )

    assert binding is None
    assert reason and "DTXWIKI_LLM_PROVIDER" in reason


def test_resolve_embedding_and_rerank_degrade_without_raising():
    """Embedding/rerank misconfiguration reached Ask through the same path."""

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="ask-degrade-secret",
        embedding_provider="openai_compatible",
        embedding_base_url=None,
        rerank_provider="api",
        rerank_base_url=None,
    )

    embedder, embedding_reason = resolve_embedding_provider(settings)
    reranker, rerank_reason = resolve_reranker(settings)

    assert embedder is None
    assert embedding_reason and "DTXWIKI_EMBEDDING_BASE_URL" in embedding_reason
    assert reranker is None
    assert rerank_reason and "DTXWIKI_RERANK_BASE_URL" in rerank_reason


def test_ask_session_list_survives_a_misconfigured_provider(
    session_factory, page_service
):
    """The regression: this endpoint used to 500 before the handler ran."""

    container = assemble(
        _misconfigured_settings(),
        session_factory=session_factory,
        page_service=page_service,
    )
    app = create_app(container=container, bootstrap_on_startup=False)
    client = TestClient(app)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="ask-degrade@example.com",
    )
    workspace_id = _workspace_id(session_factory)
    with session_factory.begin() as session:
        session.get(Workspace, workspace_id).labs_policy = {"ask_enabled": True}

    response = client.get(f"/api/v1/workspaces/{workspace_id}/ask/sessions")

    assert response.status_code == 200, response.text


def test_a_misconfigured_default_model_fails_the_turn_like_any_model(
    session_factory, page_service
):
    container = assemble(
        _misconfigured_settings(),
        session_factory=session_factory,
        page_service=page_service,
    )
    app = create_app(container=container, bootstrap_on_startup=False)
    client = TestClient(app)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="ask-degrade-turn@example.com",
    )
    workspace_id = _workspace_id(session_factory)
    with session_factory.begin() as session:
        session.get(Workspace, workspace_id).labs_policy = {"ask_enabled": True}
    page_service.write_page(
        workspace_slug="default",
        slug="degrade-evidence",
        title="Degrade Evidence",
        body_md="# Degrade Evidence\n\nThe deterministic pack still cites this.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={},
    )
    assert created.status_code == 201, created.text
    ask_session_id = created.json()["data"]["id"]

    turn = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session_id}/turns",
        json={"user_message": "What does the degrade evidence say?"},
    )

    assert turn.status_code == 200, turn.text
    data = turn.json()["data"]
    # The question is kept; a configured default model that cannot be built
    # is a model failure like any other, and the asker can search instead.
    assert (data["status"], data["error_code"], data["can_search"]) == (
        "failed",
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        True,
    )


def _answer_with_default_port(session_factory, page_service, port: TextGenerationPort):
    container = assemble(
        Settings(llm_provider="none"),
        session_factory=session_factory,
        page_service=page_service,
    )
    container.ask_connections.default = ModelBinding(
        port=port,
        target={"kind": "server", "model": "test-default"},
        model="test-default",
        max_output_tokens=1024,
        context_window=None,
        steer=False,
    )
    workspace_id = _workspace_id(session_factory)
    with session_factory.begin() as session:
        session.get(Workspace, workspace_id).labs_policy = {"ask_enabled": True}
    page_service.write_page(
        workspace_slug="default",
        slug="composer-evidence",
        title="Composer Evidence",
        body_md="# Composer Evidence\n\nEvidence for the model to use.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    prefix = f"/api/v1/workspaces/{workspace_id}/ask"
    app = create_app(container=container, bootstrap_on_startup=False)
    with TestClient(app) as client:
        authenticate_test_client_in_workspace(
            client, session_factory, email="ask-default-failure@example.com"
        )
        created = client.post(f"{prefix}/sessions", json={})
        assert created.status_code == 201, created.text
        ask_session_id = created.json()["data"]["id"]
        response = client.post(
            f"{prefix}/sessions/{ask_session_id}/turns",
            json={"user_message": "What does the composer evidence say?"},
        )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_default_model_refusal_fails_the_turn_and_allows_search(
    session_factory, page_service
):
    class RefusingPort:
        async def generate(self, request, context):
            return CanonicalTextResult(text="I can't help.", termination="refused")

    turn = _answer_with_default_port(session_factory, page_service, RefusingPort())

    assert (turn["status"], turn["error_code"], turn["assistant_message"]) == (
        "failed",
        LLM_ANSWER_REFUSED_WARNING,
        None,
    )
    assert turn["selection"] is None and turn["can_search"] is True


def test_unexpected_default_model_exception_fails_the_turn(
    session_factory, page_service
):
    class BuggyPort:
        async def generate(self, request, context):
            raise RuntimeError("composer bug")

    turn = _answer_with_default_port(session_factory, page_service, BuggyPort())

    assert (turn["status"], turn["error_code"]) == (
        "failed",
        LLM_PROVIDER_UNAVAILABLE_WARNING,
    )


def test_a_broken_embedding_config_disables_semantic_indexing_instead_of_the_job(
    session_factory,
):
    """The read path treats this configuration as "semantic is simply off".

    `build_indexer` used the raising builder, and `ValueError` is in the
    worker's terminal set, so the same env var that degraded search killed every
    embed job outright and the queue filled with permanent failures.
    """

    from backend.app.container import DatabaseProvider
    from backend.services.indexing import build_indexer

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="ask-degrade-secret",
        embedding_provider="openai_compatible",
        embedding_base_url=None,
    )
    provider = DatabaseProvider(settings, session_factory=session_factory)

    with captured_log_messages("backend.services.indexing") as records:
        indexer = build_indexer(settings, provider)

    assert indexer is not None
    # Keyword indexing still runs; only the semantic lane drops out.
    assert len(indexer.indexers) == 1
    assert any("DTXWIKI_EMBEDDING_BASE_URL" in message for message in records)


def test_the_reason_an_answer_model_was_skipped_reaches_the_server_log(
    session_factory, page_service
):
    """The UI tells the user to ask an admin to check the LLM settings. Before
    the degrade the reason was in the 500's traceback; without a log line the
    admin they ask finds nothing at all."""

    with captured_log_messages("backend.app.container") as records:
        container = assemble(
            _misconfigured_settings(),
            session_factory=session_factory,
            page_service=page_service,
        )
        _ = container.ask_connections

    assert any(
        "Ask answer model unavailable" in message and "API key" in message
        for message in records
    )

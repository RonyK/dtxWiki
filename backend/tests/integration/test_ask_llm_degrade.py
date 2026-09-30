"""A misconfigured answer model must degrade Ask, not break it.

The LLM provider is built while FastAPI resolves ``AskServiceDep``, i.e. before
any handler body runs, so a raise there escaped every ``try/except`` and turned
the whole Ask surface into a 500. The frontend then wrote an empty workspace id
into its state, and every later call went to ``/workspaces//ask/...`` — which is
the "route not found" 404 users actually reported. These tests pin the backend
half: configuration failures become a served answer plus a warning.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.container import assemble
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.llm.registry import (
    resolve_embedding_provider,
    resolve_llm_provider,
    resolve_reranker,
)
from backend.models.workspace import Workspace
from backend.services.ask_service import (
    LLM_COMPOSER_FAILED_WARNING,
    LLM_PROVIDER_UNAVAILABLE_WARNING,
)
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


def test_resolve_llm_provider_reports_reason_instead_of_raising():
    provider, reason = resolve_llm_provider(_misconfigured_settings())

    assert provider is None
    assert reason and "API key" in reason


def test_resolve_llm_provider_reports_unknown_provider():
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="ask-degrade-secret",
        llm_provider="not-a-provider",
    )

    provider, reason = resolve_llm_provider(settings)

    assert provider is None
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

    response = client.get(f"/api/v1/workspaces/{workspace_id}/ask/sessions")

    assert response.status_code == 200, response.text


def test_ask_turn_answers_and_discloses_why_the_model_was_skipped(
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
    body = turn.json()
    # An answer is served, not an error.
    assert body["data"]["assistant_message"]
    # ...and the user is told why it is the deterministic summary.
    assert LLM_PROVIDER_UNAVAILABLE_WARNING in body["data"]["warning_codes"]
    assert LLM_PROVIDER_UNAVAILABLE_WARNING in [
        warning["code"] for warning in body["envelope"]["warnings"]
    ]


def test_composer_failure_is_disclosed_instead_of_silently_swallowed(
    session_factory, page_service
):
    """A transient provider error used to be `pass`-ed with no user signal."""

    from backend.llm.base import LLMProviderError
    from backend.schemas.ask import AskSessionCreate, AskTurnCreate
    from backend.services.ask_service import AskService

    class _FailingProvider:
        def chat(self, request):
            # `provider` is keyword-only with no default. Omitting it raised
            # TypeError instead, which the service's generic `except Exception`
            # arm also warns on -- so this test passed without ever exercising
            # the LLMProviderError path it names.
            raise LLMProviderError("anthropic API returned 401", provider="anthropic")

    workspace_id = _workspace_id(session_factory)
    page_service.write_page(
        workspace_slug="default",
        slug="composer-failure-evidence",
        title="Composer Failure Evidence",
        body_md="# Composer Failure Evidence\n\nPack text stands in.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    service = AskService(provider=_FailingProvider())

    with session_factory.begin() as session:
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=str(uuid4()),
        )
        _detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace_id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="composer failure evidence"),
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    assert LLM_COMPOSER_FAILED_WARNING in warnings


def test_an_unexpected_composer_exception_does_not_promise_a_retry(
    session_factory, page_service
):
    """`llm_composer_failed` tells the user retrying may compose a full answer.

    That is true of an upstream 401 or timeout and never true of a bug in our
    own composer, so the two arms must not emit the same code — otherwise the
    only advice a user gets for a server-side crash is one that cannot work.
    """

    from backend.schemas.ask import AskSessionCreate, AskTurnCreate
    from backend.services.ask_service import (
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        AskService,
    )

    class _BuggyProvider:
        def chat(self, request):
            raise TypeError("composer bug, not an upstream failure")

    workspace_id = _workspace_id(session_factory)
    page_service.write_page(
        workspace_slug="default",
        slug="composer-bug-evidence",
        title="Composer Bug Evidence",
        body_md="# Composer Bug Evidence\n\nPack text stands in.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    service = AskService(provider=_BuggyProvider())

    with session_factory.begin() as session:
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=str(uuid4()),
        )
        _detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace_id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="composer bug evidence"),
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    assert LLM_PROVIDER_UNAVAILABLE_WARNING in warnings
    assert LLM_COMPOSER_FAILED_WARNING not in warnings


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
        container.get_ask_service()

    assert any("Ask answer model unavailable" in message for message in records)

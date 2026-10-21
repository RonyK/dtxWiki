"""Recency preserves Markdown whitespace even when the shared hash ignores it."""

from datetime import UTC, datetime
from importlib import import_module

import pytest
from sqlalchemy import select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.schemas.agent import AgentPageDraftRequest
from backend.services.agent_page_service import AgentPageService

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
INITIAL_TIME = datetime(2026, 10, 1, 9, tzinfo=UTC)
CHANGED_TIME = datetime(2026, 10, 11, 9, tzinfo=UTC)

SEMANTIC_CHANGES = [
    pytest.param(
        "# Example\n\nFirst line\nSecond line.",
        "# Example\n\nFirst line  \nSecond line.",
        id="soft-break-to-hard-break",
    ),
    pytest.param(
        "# Example\n\n```text\nvalue\n```",
        "# Example\n\n```text\nvalue  \n```",
        id="spaces-inside-fenced-code",
    ),
    pytest.param(
        "# Example\n\n```text\nvalue\n```",
        "# Example\n\n```text\nvalue\t\n```",
        id="tab-inside-fenced-code",
    ),
]

TRANSPORT_CHANGES = [
    pytest.param(
        "# Example\n\nCaf\u00e9.",
        "# Example\n\nCafe\u0301.",
        id="nfc-equivalent",
    ),
    pytest.param(
        "# Example\n\nFirst line\nSecond line.",
        "# Example\r\n\r\nFirst line\r\nSecond line.",
        id="crlf-equivalent",
    ),
    pytest.param(
        "# Example\n\nContent.",
        "# Example\n\nContent.\n\n\n",
        id="trailing-blank-newlines",
    ),
]


def _clock(monkeypatch, module_name):
    clock = {"now": INITIAL_TIME}
    monkeypatch.setattr(import_module(module_name), "utc_now", lambda: clock["now"])
    return clock


def _context_content_time(session_factory, page_id):
    with session_factory() as session:
        return session.get(ContextPage, page_id).content_changed_at


def _publish_context_change(
    page_service,
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    clock = _clock(monkeypatch, "backend.services.context_lifecycle")
    original = page_service.write_page(
        workspace_slug="default",
        slug="semantic-content-clock",
        title="Semantic content clock",
        body_md=original_body,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    assert _context_content_time(session_factory, original.id) == INITIAL_TIME

    clock["now"] = CHANGED_TIME
    staged = page_service.autosave_page(
        page_id=original.id,
        workspace_id=original.workspace_id,
        title=original.title,
        body_md=updated_body,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=original.version_id,
        base_content_hash=original.content_hash,
        session_version_id=original.version_id,
        idempotency_key="semantic-content-autosave",
    )
    assert _context_content_time(session_factory, original.id) == INITIAL_TIME
    # These edits deliberately collide under the established CAS hash. The
    # browser still stores their distinct bytes before explicit publication.
    assert staged.content_hash == original.content_hash
    assert staged.version_id != original.version_id
    with session_factory() as session:
        staged_version = session.get(ContextPageVersion, staged.version_id)
        assert staged_version.body_md == updated_body

    published = page_service.publish_draft(
        page_id=original.id,
        version_id=staged.version_id,
        reason="publish semantic whitespace change",
        publish_despite_findings=False,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        expected_content_hash=staged.content_hash,
    )
    assert published.page.version_id == staged.version_id
    with session_factory() as session:
        page = session.get(ContextPage, original.id)
        assert page.current_published_version_id == staged.version_id
        published_version = session.get(ContextPageVersion, staged.version_id)
        assert published_version.body_md == updated_body
    return _context_content_time(session_factory, original.id)


@pytest.mark.parametrize(("original_body", "updated_body"), SEMANTIC_CHANGES)
def test_context_publication_renews_for_meaningful_whitespace(
    page_service,
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    assert (
        _publish_context_change(
            page_service,
            session_factory,
            monkeypatch,
            original_body,
            updated_body,
        )
        == CHANGED_TIME
    )


@pytest.mark.parametrize(("original_body", "updated_body"), TRANSPORT_CHANGES)
def test_context_publication_does_not_renew_for_transport_spelling(
    page_service,
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    assert (
        _publish_context_change(
            page_service,
            session_factory,
            monkeypatch,
            original_body,
            updated_body,
        )
        == INITIAL_TIME
    )


def _activate_agent_change(
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    clock = _clock(monkeypatch, "backend.services.agent_page_service")
    service = AgentPageService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

        def draft(body, *, base=None):
            return service.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=AgentPageDraftRequest(
                    category="skill",
                    title="Semantic agent clock",
                    slug="semantic-agent-clock",
                    body_markdown=body,
                    base_version_id=base,
                    reason="stage semantic whitespace change",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )

        def activate(result):
            service.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=result.agent_page_id,
                version_id=result.draft_version_id,
                reason="activate semantic whitespace change",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )

        first = draft(original_body)
        page = session.get(AgentPage, first.agent_page_id)
        assert page.content_changed_at is None
        activate(first)
        assert page.content_changed_at == INITIAL_TIME

        clock["now"] = CHANGED_TIME
        changed = draft(updated_body, base=first.draft_version_id)
        assert page.content_changed_at == INITIAL_TIME
        activate(changed)
        assert page.current_active_version_id == changed.draft_version_id
        assert (
            session.get(AgentPageVersion, changed.draft_version_id).body_markdown
            == updated_body
        )
        return page.content_changed_at


@pytest.mark.parametrize(("original_body", "updated_body"), SEMANTIC_CHANGES)
def test_agent_activation_renews_for_meaningful_whitespace(
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    assert (
        _activate_agent_change(
            session_factory,
            monkeypatch,
            original_body,
            updated_body,
        )
        == CHANGED_TIME
    )


@pytest.mark.parametrize(("original_body", "updated_body"), TRANSPORT_CHANGES)
def test_agent_activation_does_not_renew_for_transport_spelling(
    session_factory,
    monkeypatch,
    original_body,
    updated_body,
):
    assert (
        _activate_agent_change(
            session_factory,
            monkeypatch,
            original_body,
            updated_body,
        )
        == INITIAL_TIME
    )

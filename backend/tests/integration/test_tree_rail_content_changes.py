"""Recency follows visible content events rather than generic row updates."""

from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.agent import AgentPage
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.agent import AgentPageDraftRequest
from backend.services.agent_page_service import AgentPageService
from backend.services.library_service import LibraryService
from backend.services.tree_projection import TreeProjectionService
from backend.tests.conftest import authenticate_test_client_in_workspace

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
INITIAL_TIME = datetime(2026, 10, 1, 9, tzinfo=UTC)


def _clock(monkeypatch, module):
    clock = {"now": INITIAL_TIME}
    monkeypatch.setattr(import_module(module), "utc_now", lambda: clock["now"])
    return clock


def _content_time(session_factory, model, target_id):
    with session_factory() as session:
        return session.get(model, target_id).content_changed_at


@pytest.mark.parametrize("role", ["viewer", "owner"])
def test_tree_visibility_does_not_load_context_bodies(
    page_service,
    session_factory,
    role,
):
    published = page_service.write_page(
        workspace_slug="default",
        slug="tree-body-budget",
        title="Body budget",
        body_md="# Body\n\n" + "Published content. " * 5000,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    page_service.autosave_page(
        page_id=published.id,
        workspace_id=published.workspace_id,
        title=published.title,
        body_md="# Draft\n\n" + "Private work. " * 5000,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        session_version_id=published.version_id,
        idempotency_key="tree-body-budget",
    )
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    with session_factory() as session:
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            projection = TreeProjectionService().load(
                session,
                published.workspace_id,
                ActorContext(
                    actor_kind="human",
                    actor_id=ACTOR_ID,
                    token_kind="session",
                    scopes=("read",),
                    workspace_role=role,
                ),
                sections=("context",),
            )
            assert any(
                node.target_id == published.id
                for node_id, node in projection.nodes.items()
                if projection.is_visible(node_id)
            )
            assert not any(
                isinstance(value, ContextPageVersion)
                for value in session.identity_map.values()
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert statements
    assert all("body_md" not in statement for statement in statements)


def test_context_publication_tracks_content_but_not_rename_or_autosave(
    page_service,
    session_factory,
    monkeypatch,
):
    clock = _clock(monkeypatch, "backend.services.context_lifecycle")
    original = page_service.write_page(
        workspace_slug="default",
        slug="content-event-clock",
        title="Before rename",
        body_md="# Body\n\nOriginal content.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    assert _content_time(session_factory, ContextPage, original.id) == INITIAL_TIME

    clock["now"] += timedelta(days=1)
    renamed = page_service.update_page(
        page_id=original.id,
        title="After rename",
        body_md=original.body_md,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=original.version_id,
        base_content_hash=original.content_hash,
    )
    assert _content_time(session_factory, ContextPage, original.id) == INITIAL_TIME

    changed = page_service.update_page(
        page_id=original.id,
        title=renamed.title,
        body_md="# Body\n\nA meaningful content change.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=renamed.version_id,
        base_content_hash=renamed.content_hash,
    )
    assert _content_time(session_factory, ContextPage, original.id) == clock["now"]
    published_time = clock["now"]

    clock["now"] += timedelta(days=1)
    page_service.autosave_page(
        page_id=original.id,
        workspace_id=original.workspace_id,
        title=changed.title,
        body_md="# Body\n\nPrivate autosaved work.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=changed.version_id,
        base_content_hash=changed.content_hash,
        session_version_id=changed.version_id,
        idempotency_key="content-event-autosave",
    )
    assert _content_time(session_factory, ContextPage, original.id) == published_time
    # Reading the document must not clear or renew the publication timestamp.
    page_service.read_page(workspace_slug="default", slug="content-event-clock")
    assert _content_time(session_factory, ContextPage, original.id) == published_time


def test_agent_activation_only_renews_for_visible_content(
    session_factory,
    monkeypatch,
):
    clock = _clock(monkeypatch, "backend.services.agent_page_service")
    service = AgentPageService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

        def draft(body, *, base=None, title="Agent content clock"):
            return service.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=AgentPageDraftRequest(
                    category="skill",
                    title=title,
                    slug="agent-content-clock",
                    body_markdown=body,
                    base_version_id=base,
                    reason="test content event",
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
                reason="activate content event",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )

        first = draft("# Skill\n\nOriginal instructions.")
        page = session.get(AgentPage, first.agent_page_id)
        assert page.content_changed_at is None
        activate(first)
        assert page.content_changed_at == INITIAL_TIME

        clock["now"] += timedelta(days=1)
        renamed = draft(
            "# Skill\n\nOriginal instructions.",
            base=first.draft_version_id,
            title="Renamed agent",
        )
        activate(renamed)
        assert page.content_changed_at == INITIAL_TIME

        changed = draft("# Skill\n\nNew instructions.", base=renamed.draft_version_id)
        assert page.content_changed_at == INITIAL_TIME
        activate(changed)
        assert page.content_changed_at == clock["now"]
        last_changed = page.content_changed_at

        clock["now"] += timedelta(days=1)
        restored = service.restore_version(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=page.id,
            version_id=first.draft_version_id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        assert page.content_changed_at == last_changed
        activate(restored)
        assert page.content_changed_at == clock["now"]


def test_library_registration_does_not_renew_for_duplicate_content(
    session_factory,
    monkeypatch,
):
    clock = _clock(monkeypatch, "backend.services.library_service")
    service = LibraryService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

        def register(body, title="Source"):
            return service.register_library_item(
                session=session,
                workspace_id=workspace_id,
                canonical_uri="https://example.com/tree-content-clock",
                title=title,
                raw_text=body,
            )

        first = register("# Source\n\nOriginal.")
        assert first.library_item.content_changed_at == INITIAL_TIME
        clock["now"] += timedelta(days=1)
        duplicate = register("# Source\n\nOriginal.", title="Metadata rename")
        assert not duplicate.created_version
        assert duplicate.library_item.content_changed_at == INITIAL_TIME
        changed = register("# Source\n\nChanged.")
        assert changed.created_version
        assert changed.library_item.content_changed_at == clock["now"]


def test_binary_source_restore_is_content_change_even_when_version_is_reused(
    page_service,
    session_factory,
    tmp_path,
    monkeypatch,
):
    clock = _clock(monkeypatch, "backend.services.library_binary_registration")
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        library_blob_root=str(tmp_path / "blobs"),
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        workspace_id = authenticate_test_client_in_workspace(
            client,
            session_factory,
            email="tree-content-owner@example.com",
            workspace_slug="default",
        )
        root = f"/api/v1/workspaces/{workspace_id}/library/items"

        def upload(url, body):
            response = client.post(
                url,
                files={"file": ("source.txt", body, "text/plain")},
            )
            assert response.status_code in (200, 201), response.text
            return response.json()["data"]

        first = upload(root, b"First source body.")
        item_id = UUID(first["id"])
        first_version = first["current_version"]["id"]
        assert _content_time(session_factory, LibraryItem, item_id) == INITIAL_TIME
        clock["now"] += timedelta(days=1)
        second = upload(f"{root}/{item_id}/versions", b"Second source body.")
        assert second["current_version"]["id"] != first_version
        assert _content_time(session_factory, LibraryItem, item_id) == clock["now"]

        clock["now"] += timedelta(days=1)
        restored = upload(f"{root}/{item_id}/versions", b"First source body.")
        assert restored["current_version"]["id"] == first_version
        assert _content_time(session_factory, LibraryItem, item_id) == clock["now"]
        restore_time = clock["now"]
        clock["now"] += timedelta(days=1)
        upload(f"{root}/{item_id}/versions", b"First source body.")
        assert _content_time(session_factory, LibraryItem, item_id) == restore_time

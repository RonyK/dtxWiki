from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.models import Base
from backend.models.context import ContextPage
from backend.repositories import context_page_repo
from backend.schemas.context_ingress import ContextIngressAcceptRequest
from backend.schemas.context_write import (
    PublishContextPageRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.scripts.seed_dev import DEFAULT_WORKSPACE_SLUG, seed_default_workspace
from backend.services import context_ingress_service, context_write_service
from backend.services.context_ingress_service import ContextIngressService
from backend.services.context_write_service import ContextWriteService
from backend.services.page_service import PageService


class _ReachedPageLock(Exception):
    pass


class _UsageIntentOrderRepository:
    def __init__(self, events: list[tuple[str, UUID]]) -> None:
        self.events = events

    def lock_workspace_for_usage(self, workspace_id: UUID) -> None:
        self.events.append(("media", workspace_id))


def _database_with_page():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="lock-order-page",
            title="Lock order page",
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        workspace_id = workspace.id
        page_id = page.id
    return engine, factory, workspace_id, page_id


def _repository_factory(
    events: list[tuple[str, UUID]],
) -> Callable[[Session], _UsageIntentOrderRepository]:
    repository = _UsageIntentOrderRepository(events)
    return lambda _session: repository


def _invoke_page_service_write(
    service: PageService,
    operation: str,
    page_id: UUID,
    workspace_id: UUID,
) -> None:
    actor_id = str(uuid4())
    if operation == "publish_draft":
        service.publish_draft(
            page_id=page_id,
            version_id=uuid4(),
            reason="publish",
            publish_despite_findings=False,
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )
    elif operation == "restore_version":
        service.restore_version(
            page_id=page_id,
            version_id=uuid4(),
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )
    elif operation == "write_page":
        service.write_page(
            workspace_slug=DEFAULT_WORKSPACE_SLUG,
            slug="new-lock-order-page",
            title="New lock order page",
            body_md="# New lock order page\n",
            actor_kind="human",
            actor_id=actor_id,
            actor_provenance="browser_session",
        )
    elif operation == "update_page":
        service.update_page(
            page_id=page_id,
            title="Updated",
            body_md="# Updated\n",
            actor_kind="human",
            actor_id=actor_id,
            base_version_id=uuid4(),
            base_content_hash="0" * 64,
            actor_provenance="browser_session",
        )
    elif operation == "autosave_page":
        service.autosave_page(
            page_id=page_id,
            workspace_id=workspace_id,
            title="Autosaved",
            body_md="# Autosaved\n",
            actor_kind="human",
            actor_id=actor_id,
            base_version_id=uuid4(),
            base_content_hash="0" * 64,
            actor_provenance="browser_session",
            idempotency_key="lock-order-autosave",
        )
    else:  # pragma: no cover - test table is closed above
        raise AssertionError(operation)


@pytest.mark.parametrize(
    "operation",
    [
        "publish_draft",
        "restore_version",
        "write_page",
        "update_page",
        "autosave_page",
    ],
)
def test_page_service_locks_media_before_context_page(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, workspace_id, page_id = _database_with_page()
    events: list[tuple[str, UUID]] = []

    def stop_at_page_lock(_session, identity, *_args, **_kwargs):
        events.append(("page", identity))
        raise _ReachedPageLock

    page_lock = (
        "get_page_by_slug_for_update"
        if operation == "write_page"
        else "get_page_by_id_for_update"
    )
    monkeypatch.setattr(context_page_repo, page_lock, stop_at_page_lock)
    service = PageService(
        factory,
        context_media_repository_factory=_repository_factory(events),
    )

    try:
        with pytest.raises(_ReachedPageLock):
            _invoke_page_service_write(service, operation, page_id, workspace_id)
        assert events[0] == ("media", workspace_id)
        assert events[1][0] == "page"
    finally:
        engine.dispose()


@pytest.mark.parametrize("operation", ["write_draft", "update_draft", "publish_draft"])
def test_context_write_service_locks_media_before_context_page(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, workspace_id, page_id = _database_with_page()
    events: list[tuple[str, UUID]] = []

    def stop_at_page_lock(*_args, **_kwargs):
        events.append(("page", page_id))
        raise _ReachedPageLock

    if operation == "write_draft":
        monkeypatch.setattr(
            ContextWriteService,
            "_get_or_create_page",
            stop_at_page_lock,
        )
    else:
        monkeypatch.setattr(
            context_write_service,
            "get_page_by_id_for_update",
            stop_at_page_lock,
        )
    service = ContextWriteService(
        context_media_repository_factory=_repository_factory(events)
    )
    actor_id = str(uuid4())

    try:
        with factory.begin() as session, pytest.raises(_ReachedPageLock):
            if operation == "write_draft":
                service.write_draft(
                    session=session,
                    workspace_id=workspace_id,
                    request=WriteDraftRequest(
                        title="Lock order draft",
                        page_type="overview",
                        body_markdown="# Lock order draft\n",
                        reason="test lock order",
                    ),
                    actor_kind="mcp_client",
                    actor_id=actor_id,
                    actor_role="editor",
                )
            elif operation == "update_draft":
                service.update_draft(
                    session=session,
                    workspace_id=workspace_id,
                    request=UpdateDraftRequest(
                        page_id=page_id,
                        base_version_id=uuid4(),
                        base_content_hash="0" * 64,
                        update_mode="full_rewrite",
                        body_markdown="# Updated draft\n",
                        reason="test lock order",
                    ),
                    actor_kind="mcp_client",
                    actor_id=actor_id,
                )
            else:
                service.publish_draft(
                    session=session,
                    workspace_id=workspace_id,
                    page_id=page_id,
                    request=PublishContextPageRequest(
                        version_id=uuid4(),
                        reason="test lock order",
                    ),
                    actor_kind="human",
                    actor_id=actor_id,
                )
        assert events == [("media", workspace_id), ("page", page_id)]
    finally:
        engine.dispose()


def test_context_ingress_locks_media_before_accept_page_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory, workspace_id, _page_id = _database_with_page()
    events: list[tuple[str, UUID]] = []
    proposal_id = uuid4()

    def stop_at_proposal_lock(*_args, **_kwargs):
        events.append(("page", proposal_id))
        raise _ReachedPageLock

    monkeypatch.setattr(
        context_ingress_service,
        "require_proposal_for_update",
        stop_at_proposal_lock,
    )
    service = ContextIngressService(
        context_media_repository_factory=_repository_factory(events)
    )

    try:
        with factory.begin() as session, pytest.raises(_ReachedPageLock):
            service.accept_proposal(
                session=session,
                workspace_id=workspace_id,
                proposal_id=proposal_id,
                request=ContextIngressAcceptRequest(
                    final_save_mode="create_page",
                    final_parent_node_id=uuid4(),
                    final_title="Accepted",
                    body_markdown="# Accepted\n",
                ),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
        assert events == [("media", workspace_id), ("page", proposal_id)]
    finally:
        engine.dispose()

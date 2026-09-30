"""A confirmed workspace deletion must actually remove the workspace's data.

These tests deliberately run against an engine with ``PRAGMA foreign_keys=ON``.
The shared ``session_factory`` fixture (backend/tests/conftest.py) does not
enable it, so a delete ordered wrongly would pass there and fail in production
-- the exact shape of green-but-wrong this repo has been bitten by before. Here
a mis-ordered delete raises IntegrityError and the test fails loudly.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models import (
    BlobIncarnationRow,
    ChangeSet,
    ContextPage,
    Job,
    LibraryItem,
    LibraryItemVersion,
    LintRun,
    ReviewItem,
    StoredBlobRow,
    User,
    Workspace,
    WorkspaceMediaWriteIntent,
)
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.context import ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.models.workspace import MCPClient
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.read import WorkspaceCreateRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.read_errors import RejectedByPolicyError
from backend.services.workspace_purge import (
    GLOBAL_TABLES,
    PRESERVED_TABLES,
    _build_plan,
    purge_workspace_data,
)
from backend.services.workspace_write import (
    WorkspaceDeleteRejectedError,
    WorkspaceWriteService,
)


def _with_purge_scope(actor: ActorContext) -> ActorContext:
    """PURGE_AUTHZ_PLAN S-2: the service now requires this scope on the actor
    for any direct ``purge_contents=True`` call -- the REST route adds it
    itself after validating a grant (see ``backend/api/read.py``), but a
    service-level test bypasses the route and must add it explicitly, or add
    nothing and expect ``purge_grant_required`` (see
    ``test_direct_service_call_without_purge_scope_is_rejected`` below).
    """

    return replace(actor, scopes=actor.scopes + ("workspace-purge",))


@pytest.fixture
def fk_session_factory():
    """SQLite with foreign keys enforced, matching the production pragma."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_default_workspace(session)
    return factory


def _owner_and_workspace(session):
    default_workspace = session.scalar(
        select(Workspace).where(Workspace.slug == "default")
    )
    user = User(
        email="purge-owner@example.com",
        password_hash="not-used",
        display_name="Purge Owner",
        default_workspace_id=default_workspace.id,
    )
    session.add(user)
    session.flush()
    grant_workspace_membership(
        session,
        user_id=user.id,
        workspace_id=default_workspace.id,
        role="owner",
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user.id),
        user_id=user.id,
        token_kind="session",
        provenance="browser_session",
        scopes=("*",),
        workspace_role="owner",
        role="owner",
    )
    created = WorkspaceWriteService().create_workspace(
        session=session,
        request=WorkspaceCreateRequest(title="Purge Me", slug="purge-me"),
        actor=actor,
    )
    return actor, created.id


def _seed_contents(session, workspace_id, *, public: bool = False):
    """Populate the aggregates whose foreign keys make ordering hazardous."""

    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug="purge-page",
        title="Purge Page",
        aliases=[],
        tags=[],
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="# Purge",
        content_hash="hash-1",
        actor_type="human",
        actor_id=uuid4(),
        created_by_actor_kind="human",
        created_by_actor_id="seed",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    # The parent<->child cycle the purge has to break before it can delete.
    page.current_published_version_id = version.id
    page.latest_draft_version_id = version.id
    if public:
        page.is_public = True
        page.public_exposure_scope = "indexed_web"
        page.public_exposed_version_id = version.id

    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="upload",
        uri="upload://purge.md",
        title="Purge Doc",
    )
    session.add(item)
    session.flush()
    item_version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="lib-hash-1",
        raw_storage_ref="inline://raw",
        processing_status="completed",
    )
    session.add(item_version)
    session.flush()
    item.current_version_id = item_version.id
    item.current_ready_version_id = item_version.id

    change_set = ChangeSet(
        workspace_id=workspace_id,
        trigger_type="human",
        actor_type="human",
    )
    session.add(change_set)
    session.flush()
    session.add(
        ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="content",
        )
    )
    session.add(
        Job(
            workspace_id=workspace_id,
            job_type="lint_scan",
            trigger_type="manual",
        )
    )
    session.add(
        LintRun(
            workspace_id=workspace_id,
            trigger_type="manual",
        )
    )
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace_id)
    )
    if client is not None:
        session.add(
            McpChatSession(
                workspace_id=workspace_id,
                client_id=client.id,
                mcp_session_id="purge-chat",
            )
        )
    session.flush()


def _seed_blob_backed_library_version(session, workspace_id):
    """Create the source-blob shape that must survive until its version deletes."""

    blob_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id,
            id=blob_id,
            sha256="c" * 64,
            next_epoch=2,
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=5,
            storage_key=f"workspace/{workspace_id}/blob/{blob_id}/1",
            state="available",
        )
    )
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="upload",
        uri="upload://purge-blob.pdf",
        title="Blob-backed purge document",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="d" * 64,
        raw_storage_ref=None,
        source_blob_workspace_id=workspace_id,
        source_blob_id=blob_id,
        source_blob_epoch=1,
        original_filename="purge.pdf",
        declared_media_type="application/pdf",
        detected_media_type="application/pdf",
        format_id="pdf",
        byte_size=5,
        detection_json={"source": "purge-regression"},
        processing_status="completed",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    item.current_ready_version_id = version.id
    session.flush()


def _workspace_row_counts(session, workspace_id) -> dict[str, int]:
    """Rows still claiming this workspace, per table."""

    counts: dict[str, int] = {}
    for name, table in Base.metadata.tables.items():
        if name in GLOBAL_TABLES or "workspace_id" not in table.c:
            continue
        total = int(
            session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.workspace_id == workspace_id)
            )
            or 0
        )
        if total:
            counts[name] = total
    return counts


def test_delete_without_purge_still_refuses_and_reports_blockers(fk_session_factory):
    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id)

    with pytest.raises(WorkspaceDeleteRejectedError) as exc_info:
        with fk_session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
            )

    blockers = exc_info.value.blockers
    assert blockers["context_pages"] == 1
    assert blockers["library"] == 1
    assert blockers["jobs"] == 1
    with fk_session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None


def test_confirmed_purge_leaves_no_row_for_the_seeded_content(
    fk_session_factory,
):
    """260912 review, P6b: `_seed_contents` only populates 9 of the 79
    purge-scoped tables, so this proves purge is complete for what it seeds
    -- not, despite the name this test used to carry, for every table the
    purge plan owns. That broader completeness guarantee belongs to the
    static gate over the purge plan (`test_purge_plan_orders_every_purgeable_
    table` and friends), not to a hand-seeded integration test: a table added
    here would need to be added there too, in lockstep, forever, for a test
    that still would not have caught the table being silently skipped by the
    plan itself.
    """
    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id, public=True)

    with fk_session_factory() as session:
        assert _workspace_row_counts(session, workspace_id)

    with fk_session_factory.begin() as session:
        deleted = WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=_with_purge_scope(actor),
            purge_contents=True,
        )

    assert deleted == workspace_id
    with fk_session_factory() as session:
        assert session.get(Workspace, workspace_id) is None
        leftovers = _workspace_row_counts(session, workspace_id)
        # audit_event is the one table deliberately kept; its rows detach
        # instead of being deleted, so they must not still claim the workspace.
        assert leftovers == {}, f"rows survived the purge: {leftovers}"


def test_confirmed_purge_keeps_blob_backed_library_versions_valid_until_delete(
    fk_session_factory,
):
    """A source blob is an acyclic dependency, never a pre-delete NULL target."""

    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id)
        _seed_blob_backed_library_version(session, workspace_id)

    updates, order = _build_plan()
    library_version_update = next(
        planned_update
        for planned_update in updates
        if planned_update.table_name == "library_item_version"
    )
    assert not {
        "source_blob_workspace_id",
        "source_blob_id",
        "source_blob_epoch",
    } & set(library_version_update.as_values())
    assert order.index("library_item_version") < order.index("blob_incarnation")

    with fk_session_factory.begin() as session:
        WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=_with_purge_scope(actor),
            purge_contents=True,
        )

    with fk_session_factory() as session:
        assert session.get(Workspace, workspace_id) is None
        assert _workspace_row_counts(session, workspace_id) == {}


def test_purge_detaches_audit_history_instead_of_erasing_it(fk_session_factory):
    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id)

    with fk_session_factory.begin() as session:
        WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=_with_purge_scope(actor),
            purge_contents=True,
        )

    with fk_session_factory() as session:
        purge_events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.purge")
        ).all()
        assert len(purge_events) == 1
        event_row = purge_events[0]
        # The record outlives the workspace, detached by ON DELETE SET NULL.
        assert event_row.workspace_id is None
        assert event_row.target_id == workspace_id
        assert event_row.payload["deleted_row_total"] > 0
        assert "audit_event" not in event_row.payload["deleted_rows"]
    assert "audit_event" in PRESERVED_TABLES


def test_confirmed_purge_clears_retained_media_and_its_write_fence(
    fk_session_factory,
):
    """The media ledger is where the RESTRICT foreign keys cluster.

    stored_blob and media_staging_lease RESTRICT-reference the workspace's
    media write-intent row, which _delete_empty_media_scope deletes last as its
    generation fence. A purge that removed the fence early, or the blob rows
    late, would fail here and nowhere else -- and retained media is exactly the
    state that used to make deletion impossible.
    """

    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id)
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id,
                id=uuid4(),
                sha256="b" * 64,
                next_epoch=1,
            )
        )

    with pytest.raises(WorkspaceDeleteRejectedError) as exc_info:
        with fk_session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
            )
    assert exc_info.value.blockers["media"] == 1

    with fk_session_factory.begin() as session:
        WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=_with_purge_scope(actor),
            purge_contents=True,
        )

    with fk_session_factory() as session:
        assert session.get(Workspace, workspace_id) is None
        assert session.get(WorkspaceMediaWriteIntent, workspace_id) is None
        assert _workspace_row_counts(session, workspace_id) == {}


def test_purge_leaves_other_workspaces_untouched(fk_session_factory):
    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)
        _seed_contents(session, workspace_id)
        neighbour = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        neighbour_id = neighbour.id
        _seed_contents(session, neighbour_id)

    with fk_session_factory() as session:
        before = _workspace_row_counts(session, neighbour_id)

    with fk_session_factory.begin() as session:
        purge_workspace_data(session, workspace_id)

    with fk_session_factory() as session:
        assert _workspace_row_counts(session, neighbour_id) == before


def test_direct_service_call_without_purge_scope_is_rejected(fk_session_factory):
    """PURGE_AUTHZ_PLAN T-3b: the REST route validates a purge grant and adds
    ``workspace-purge`` to the actor it passes in (backend/api/read.py), but
    that is enforcement at the caller. Giving every other test in this file a
    scoped actor (see ``_with_purge_scope``) only proves the service accepts
    one -- it does not prove the service actually requires it. This is the
    one test that removes the scope and confirms the service itself refuses.
    """

    with fk_session_factory.begin() as session:
        actor, workspace_id = _owner_and_workspace(session)

    with pytest.raises(RejectedByPolicyError) as exc_info:
        with fk_session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
                purge_contents=True,
            )

    assert exc_info.value.error_code == "purge_grant_required"
    with fk_session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None

"""Every serialized write entry point must hold SQLite's writer lock by the time
it performs its guarded read.

SQLAlchemy compiles ``with_for_update()`` away on SQLite and pysqlite defers
``BEGIN`` until the first DML statement, so a transaction whose first statement
is a guarded ``SELECT`` holds no lock at all and its read is stale the moment a
competing writer commits.  ``acquire_write_intent`` closes that window; these
tests pin it at each call site.

Two properties of the harness are load-bearing:

* The database is **file-backed** (``tmp_path``).  Separate ``:memory:``
  connections do not contend for one file lock, so the shared ``session_factory``
  fixture would let all of these pass with the fix reverted.
* Each test probes at the **first collaborator the entry point calls after the
  acquire**, and aborts there.  That both keeps the fixtures small and pins the
  property precisely: write intent is held on entry into the body, before any
  decision is read.  A probe placed after some unrelated DML would report
  "locked" for the wrong reason and would not fail on revert.
"""

import sqlite3
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.db import make_engine, make_session_factory
from backend.models.ask import AskSession
from backend.models.base import Base
from backend.models.library import LibraryItem
from backend.models.tree import WorkspaceTreeNode
from backend.repositories import context_page_repo
from backend.schemas.agent import AgentPageDraftRequest
from backend.schemas.ask import AskTurnCreate
from backend.schemas.context_write import (
    PublishContextPageRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.scripts.seed_dev import DEFAULT_WORKSPACE_SLUG, seed_default_workspace
from backend.services import context_ingress_service, context_write_service
from backend.services.agent_page_service import AgentPageService
from backend.services.ask_service import AskService
from backend.services.context_write_service import ContextWriteService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService

ANY_ID = UUID("00000000-0000-0000-0000-0000000000ff")


class _ReachedGuardedRead(Exception):
    """Raised by the probe at the guarded read, carrying the lock observation."""

    def __init__(self, outside_writer_blocked: bool) -> None:
        super().__init__("reached guarded read")
        self.outside_writer_blocked = outside_writer_blocked


def _outside_writer_blocked(database_path) -> bool:
    """True when a separate connection cannot take the write lock right now."""

    contender = sqlite3.connect(database_path, timeout=0)
    try:
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        contender.close()


@pytest.fixture
def db(tmp_path):
    database_path = tmp_path / "write-intent-call-sites.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    try:
        yield SimpleNamespace(
            path=database_path,
            factory=session_factory,
            workspace_id=workspace_id,
        )
    finally:
        engine.dispose()


@pytest.fixture
def probe(db, monkeypatch):
    """Replace a collaborator with a probe that samples the lock, then aborts."""

    def _install(target, attribute):
        def _probe(*_args, **_kwargs):
            raise _ReachedGuardedRead(_outside_writer_blocked(db.path))

        monkeypatch.setattr(target, attribute, _probe)

    return _install


def _assert_locked_at_guarded_read(call):
    with pytest.raises(_ReachedGuardedRead) as excinfo:
        call()
    assert excinfo.value.outside_writer_blocked, (
        "the guarded read ran without SQLite's writer lock held -- a competing "
        "writer could commit between this read and the write that depends on it"
    )


# --- page_service: the five transactions that own a context page write --------


def test_publish_draft_holds_write_intent(db, probe):
    probe(context_page_repo, "get_page_by_id_for_update")
    _assert_locked_at_guarded_read(
        lambda: PageService(db.factory).publish_draft(
            page_id=ANY_ID,
            version_id=ANY_ID,
            reason="publish",
            publish_despite_findings=False,
            actor_kind="human",
            actor_id=str(uuid4()),
            actor_provenance="browser_session",
            expected_content_hash="deadbeef",
        )
    )


def test_restore_version_holds_write_intent(db, probe):
    probe(context_page_repo, "get_page_by_id_for_update")
    _assert_locked_at_guarded_read(
        lambda: PageService(db.factory).restore_version(
            page_id=ANY_ID,
            version_id=ANY_ID,
            actor_kind="human",
            actor_id=str(uuid4()),
            actor_provenance="browser_session",
        )
    )


def test_update_page_holds_write_intent(db, probe):
    probe(context_page_repo, "get_page_by_id_for_update")
    _assert_locked_at_guarded_read(
        lambda: PageService(db.factory).update_page(
            page_id=ANY_ID,
            title="Title",
            body_md="# Title\n\nbody\n",
            actor_kind="human",
            actor_id=str(uuid4()),
            base_version_id=ANY_ID,
            base_content_hash="deadbeef",
            actor_provenance="browser_session",
        )
    )


def test_autosave_page_holds_write_intent(db, probe):
    """The in-place draft rewrite that a concurrent publish is hash-checking."""

    probe(context_page_repo, "get_page_by_id_for_update")
    _assert_locked_at_guarded_read(
        lambda: PageService(db.factory).autosave_page(
            page_id=ANY_ID,
            workspace_id=db.workspace_id,
            title="Title",
            body_md="# Title\n\nbody\n",
            actor_kind="human",
            actor_id=str(uuid4()),
            base_version_id=ANY_ID,
            base_content_hash="deadbeef",
            actor_provenance="browser_session",
            idempotency_key="write-intent-autosave",
        )
    )


def test_write_page_holds_write_intent(db, probe):
    probe(context_page_repo, "get_page_by_slug_for_update")
    _assert_locked_at_guarded_read(
        lambda: PageService(db.factory).write_page(
            workspace_slug=DEFAULT_WORKSPACE_SLUG,
            slug="write-intent",
            title="Write intent",
            body_md="# Write intent\n\nbody\n",
            actor_kind="human",
            actor_id=str(uuid4()),
            actor_provenance="browser_session",
        )
    )


# --- context_write_service: the MCP + REST context write lane -----------------


def test_context_write_write_draft_holds_write_intent(db, probe):
    probe(ContextWriteService, "_require_workspace")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: ContextWriteService().write_draft(
                session=session,
                workspace_id=db.workspace_id,
                request=WriteDraftRequest(
                    title="Write intent",
                    page_type="overview",
                    body_markdown="# Write intent\n\nbody\n",
                    reason="write",
                ),
                actor_kind="mcp_client",
                actor_id=str(uuid4()),
                actor_role="editor",
            )
        )


def test_context_write_update_draft_holds_write_intent(db, probe):
    probe(context_write_service, "get_page_by_id_for_update")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: ContextWriteService().update_draft(
                session=session,
                workspace_id=db.workspace_id,
                request=UpdateDraftRequest(
                    page_id=ANY_ID,
                    base_version_id=ANY_ID,
                    base_content_hash="0" * 64,
                    update_mode="full_rewrite",
                    body_markdown="# Write intent\n\nbody\n",
                    reason="update",
                ),
                actor_kind="mcp_client",
                actor_id=str(uuid4()),
            )
        )


def test_context_write_publish_draft_holds_write_intent(db, probe):
    probe(context_write_service, "get_page_by_id_for_update")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: ContextWriteService().publish_draft(
                session=session,
                workspace_id=db.workspace_id,
                page_id=ANY_ID,
                request=PublishContextPageRequest(
                    version_id=ANY_ID,
                    reason="publish",
                ),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
        )


# --- context_ingress_service: the accept/reject decision ----------------------


def test_accept_proposal_holds_write_intent(db, probe):
    probe(context_ingress_service, "require_proposal_for_update")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: context_ingress_service.ContextIngressService().accept_proposal(
                session=session,
                workspace_id=db.workspace_id,
                proposal_id=ANY_ID,
                request=_accept_request(),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
        )


def test_reject_proposal_holds_write_intent(db, probe):
    probe(context_ingress_service, "require_proposal_for_update")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: context_ingress_service.ContextIngressService().reject_proposal(
                session=session,
                workspace_id=db.workspace_id,
                proposal_id=ANY_ID,
                request=_reject_request(),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
        )


def _accept_request():
    from backend.schemas.context_ingress import ContextIngressAcceptRequest

    return ContextIngressAcceptRequest(
        final_title="Accepted",
        final_parent_node_id=ANY_ID,
        final_save_mode="create_page",
    )


def _reject_request():
    from backend.schemas.context_ingress import ContextIngressRejectRequest

    return ContextIngressRejectRequest(reason="not useful")


# --- ask_service: turn_no allocation ------------------------------------------


def test_create_turn_holds_write_intent(db, probe, monkeypatch):
    """Pinned on the branch that emits no DML before the allocation.

    ``trace`` is non-empty with no citable evidence, so ``create_turn`` takes the
    ``evidence_not_citable`` path -- neither ``record_coverage_gap`` nor
    ``resolve_coverage_gap`` runs, and nothing has opened the transaction.
    """

    with db.factory.begin() as session:
        tree_node_id = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == db.workspace_id
            )
        ).first().id
        ask_session = AskSession(
            workspace_id=db.workspace_id,
            title="Write intent",
            actor_kind="human",
            placement_tree_node_id=tree_node_id,
        )
        session.add(ask_session)
        session.flush()
        ask_session_id = ask_session.id

    monkeypatch.setattr(
        AskService,
        "_answer",
        lambda *_args, **_kwargs: (
            "answer",
            [],
            [],
            [{"lane": "keyword"}],
            ["keyword"],
            False,
            [],
            [],
        ),
    )
    probe(AskService, "_lock_session_for_turn_insert")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: AskService().create_turn(
                session=session,
                workspace_id=db.workspace_id,
                ask_session_id=ask_session_id,
                request=AskTurnCreate(user_message="what is write intent?"),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
        )


# --- library_service: version_no allocation -----------------------------------


def test_register_library_item_holds_write_intent(db, probe):
    """Pinned on the already-exists path, which is the contended one.

    The create path inserts the LibraryItem first, and that INSERT takes the
    writer lock on its own -- a test that took it would report "locked" even
    with the fix reverted.  Seeding the item forces the path that reaches the
    guarded read with no DML emitted.
    """

    uri = "https://example.invalid/write-intent"
    with db.factory.begin() as session:
        session.add(
            LibraryItem(
                workspace_id=db.workspace_id,
                connector_type="web",
                format_hint="unknown",
                uri=uri,
                title="Seeded",
                metadata_json={},
                trust_level="unknown",
                lifecycle_status="extract_pending",
                created_by_actor_type="system",
            )
        )

    probe(LibraryService, "_lock_library_item_for_version_insert")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: LibraryService().register_library_item(
                session=session,
                workspace_id=db.workspace_id,
                canonical_uri=uri,
                title="Write intent",
                raw_text="body",
            )
        )


# --- agent_page_service: activation and draft writes --------------------------


def test_agent_page_activate_holds_write_intent(db, probe):
    """Without this lock two activations both land as ``active_snapshot``.

    Nothing in the schema catches that, so this is the one site in the sweep
    whose failure mode is durable corruption rather than an IntegrityError.
    """

    probe(AgentPageService, "_lock_page_by_id")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: AgentPageService().activate(
                session=session,
                workspace_id=db.workspace_id,
                agent_page_id=ANY_ID,
                version_id=ANY_ID,
                reason="activate",
                actor_kind="human",
                actor_id=str(uuid4()),
                actor_role="admin",
            )
        )


def test_agent_page_write_draft_holds_write_intent(db, probe):
    probe(AgentPageService, "_resolve_or_create_page")
    with db.factory.begin() as session:
        _assert_locked_at_guarded_read(
            lambda: AgentPageService().write_draft(
                session=session,
                workspace_id=db.workspace_id,
                request=AgentPageDraftRequest(
                    category="skill",
                    title="Write intent",
                    slug="write-intent",
                    body_markdown="# Write intent\n\nbody\n",
                    reason="draft",
                ),
                actor_kind="human",
                actor_id=str(uuid4()),
                actor_provenance="browser_session",
            )
        )

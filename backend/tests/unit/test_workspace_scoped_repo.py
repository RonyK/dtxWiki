"""Epic-3 D-6: get_in_workspace must return the row only when it belongs to
the requested workspace, and must not distinguish "wrong workspace" from
"no such id" (Phase A D-5 -- both are a scoped not_found)."""

from uuid import uuid4

import pytest

from backend.app.db import make_engine, make_session_factory
from backend.models.api_token import ApiToken
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.context_media import ContextMediaAsset
from backend.models.library import LibraryItem
from backend.models.user import User
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.scripts.seed_dev import seed_default_workspace


def _session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_get_in_workspace_returns_the_row_for_its_own_workspace():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="manual",
            format_hint="unknown",
            uri="file:///a",
            metadata_json={},
            trust_level="unknown",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()

        found = get_in_workspace(
            session, LibraryItem, workspace_id=workspace.id, resource_id=item.id
        )
        assert found is item


def test_get_in_workspace_hides_a_row_owned_by_a_different_workspace():
    factory = _session_factory()
    with factory() as session:
        home = seed_default_workspace(session, slug="home-workspace")
        other = seed_default_workspace(session, slug="other-workspace")
        item = LibraryItem(
            workspace_id=other.id,
            connector_type="manual",
            format_hint="unknown",
            uri="file:///b",
            metadata_json={},
            trust_level="unknown",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()

        # The row exists (a bare session.get() would return it) but does not
        # belong to `home` -- get_in_workspace must not leak it.
        assert session.get(LibraryItem, item.id) is not None
        assert (
            get_in_workspace(
                session, LibraryItem, workspace_id=home.id, resource_id=item.id
            )
            is None
        )


def test_get_in_workspace_returns_none_for_a_nonexistent_id():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        assert (
            get_in_workspace(
                session, LibraryItem, workspace_id=workspace.id, resource_id=uuid4()
            )
            is None
        )


def test_scoped_batch_excludes_other_workspaces_and_handles_empty_ids():
    factory = _session_factory()
    with factory() as session:
        home = seed_default_workspace(session, slug="batch-home")
        other = seed_default_workspace(session, slug="batch-other")
        items = [
            LibraryItem(
                workspace_id=workspace.id,
                connector_type="manual",
                format_hint="unknown",
                uri=f"file:///{index}",
                metadata_json={},
                trust_level="unknown",
                lifecycle_status="registered",
            )
            for index, workspace in enumerate([home, other])
        ]
        session.add_all(items)
        session.flush()
        statement = select_in_workspace_by_ids(
            LibraryItem, workspace_id=home.id, resource_ids=[item.id for item in items]
        )
        assert list(session.scalars(statement)) == [items[0]]
        assert list(
            session.scalars(
                select_in_workspace(
                    LibraryItem,
                    workspace_id=home.id,
                    criteria=[LibraryItem.id.in_([item.id for item in items])],
                )
            )
        ) == [items[0]]
        # Caller-provided predicates can narrow the scope, never replace it.
        assert (
            list(
                session.scalars(
                    select_in_workspace(
                        LibraryItem,
                        workspace_id=home.id,
                        criteria=[LibraryItem.workspace_id == other.id],
                    )
                )
            )
            == []
        )
        assert (
            list(
                session.scalars(
                    select_in_workspace_by_ids(
                        LibraryItem, workspace_id=home.id, resource_ids=[]
                    )
                )
            )
            == []
        )


@pytest.mark.parametrize(
    "model", [ApiToken, WorkspaceJoinRequest, AuditEvent, User, ContextMediaAsset]
)
def test_scoped_batch_rejects_unsupported_model_shapes(model):
    with pytest.raises(TypeError):
        select_in_workspace_by_ids(model, workspace_id=uuid4(), resource_ids=[uuid4()])
    with pytest.raises(TypeError):
        select_in_workspace(model, workspace_id=uuid4())


def test_get_in_workspace_rejects_workspace_plane_composite_identity():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(TypeError, match=r"exact primary key \(id\)"):
            get_in_workspace(
                session,
                ContextMediaAsset,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )


def test_get_in_workspace_rejects_control_plane_api_token_with_workspace_column():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(TypeError, match="WORKSPACE_PLANE_TABLES"):
            get_in_workspace(
                session,
                ApiToken,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )


def test_get_in_workspace_rejects_control_plane_join_request_with_workspace_column():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(TypeError, match="WORKSPACE_PLANE_TABLES"):
            get_in_workspace(
                session,
                WorkspaceJoinRequest,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )


def test_get_in_workspace_rejects_mixed_plane_audit_event():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(TypeError, match="WORKSPACE_PLANE_TABLES"):
            get_in_workspace(
                session,
                AuditEvent,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )


def test_get_in_workspace_rejects_control_plane_user_without_workspace_column():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(TypeError, match="WORKSPACE_PLANE_TABLES"):
            get_in_workspace(
                session,
                User,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )


def test_get_in_workspace_rejects_nullable_workspace_scope(monkeypatch):
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        monkeypatch.setattr(LibraryItem.__table__.c.workspace_id, "nullable", True)

        with pytest.raises(TypeError, match="non-null workspace_id"):
            get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace.id,
                resource_id=uuid4(),
            )

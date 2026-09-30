"""PostgreSQL lock-order contracts for workspace lifecycle writes."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend.models import User, Workspace, WorkspaceMediaWriteIntent
from backend.models.workspace import WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.services.workspace_write import (
    WorkspaceWriteService,
    set_user_default_workspace,
)


class _PostgresDialect:
    name = "postgresql"


class _PostgresBind:
    dialect = _PostgresDialect()


class _LockOrderSession:
    def __init__(self) -> None:
        self.intent = SimpleNamespace(
            state="open",
            delete_generation=0,
            updated_at=None,
        )
        self.workspace = SimpleNamespace(id=uuid4())
        self.user = SimpleNamespace(status="active", default_workspace_id=None)
        self.membership = SimpleNamespace(role="owner")
        self.events: list[tuple[type, object]] = []

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        self.events.append((entity, statement))
        return {
            WorkspaceMediaWriteIntent: self.intent,
            Workspace: self.workspace,
            User: self.user,
            WorkspaceMembership: self.membership,
        }[entity]

    def flush(self) -> None:
        return None


class _SessionFactory:
    def __init__(self, session: _LockOrderSession) -> None:
        self.session = session

    @contextmanager
    def begin(self):
        yield self.session


def _compiled_sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).rstrip()


def test_delete_locks_media_intent_before_workspace_user_and_membership() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    session = _LockOrderSession()
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        token_kind="session",
        provenance="browser_session",
        scopes=("*",),
        workspace_role="owner",
        role="owner",
    )

    WorkspaceWriteService.require_delete_authorization(
        session=cast(Session, session),
        workspace_id=workspace_id,
        actor=actor,
    )
    WorkspaceWriteService._begin_media_delete(
        session=cast(Session, session),
        workspace_id=workspace_id,
    )

    assert [entity for entity, _ in session.events] == [
        WorkspaceMediaWriteIntent,
        Workspace,
        User,
        WorkspaceMembership,
        WorkspaceMediaWriteIntent,
    ]
    assert all(
        _compiled_sql(statement).endswith("FOR UPDATE")
        for _, statement in session.events
    )


def test_set_default_locks_workspace_before_user_and_membership() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    session = _LockOrderSession()

    set_user_default_workspace(
        _SessionFactory(session),
        user_id=user_id,
        workspace_id=workspace_id,
    )

    assert [entity for entity, _ in session.events] == [
        Workspace,
        User,
        WorkspaceMembership,
    ]
    assert all(
        _compiled_sql(statement).endswith("FOR UPDATE")
        for _, statement in session.events
    )
    assert session.user.default_workspace_id == workspace_id

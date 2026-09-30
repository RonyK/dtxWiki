from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.workspace import MCPClient, Workspace
from backend.scripts.seed_dev import seed_default_workspace


def test_seed_default_workspace_is_idempotent():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        first = seed_default_workspace(session)
    with session_factory.begin() as session:
        second = seed_default_workspace(session)
        workspaces = session.scalars(select(Workspace)).all()

    assert first.slug == "default"
    assert second.slug == "default"
    assert len(workspaces) == 1
    assert workspaces[0].title == "Default Workspace"


def test_seed_default_workspace_does_not_create_an_unauthenticated_mcp_client():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        seed_default_workspace(session)

    with session_factory.begin() as session:
        clients = session.scalars(select(MCPClient)).all()

    assert clients == []

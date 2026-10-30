from collections.abc import Callable
from typing import Literal, TypedDict

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler
from sqlalchemy.orm import Session, sessionmaker

from backend.app.settings import Settings


class DatabasePing(TypedDict):
    status: Literal["ok", "not_configured", "error"]


class _HintingSQLiteCompiler(SQLiteCompiler):
    def get_from_hint_text(self, _table, hint_text: str | None) -> str | None:
        return hint_text


def database_ping(session_factory: Callable[[], Session]) -> DatabasePing:
    """SELECT 1 over the shared session factory.

    Takes the app-wide factory (not Settings) so frequently polled callers —
    the readiness probe in particular — reuse the cached engine/pool instead
    of constructing and disposing a throwaway engine per call.
    """
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return {"status": "error"}


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
        connect_args["isolation_level"] = "IMMEDIATE"
    # hide_parameters keeps bound values (password hashes, token digests,
    # page text) out of the DBAPI errors that reach logs and tracebacks.
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
        hide_parameters=True,
    )
    if database_url.startswith("sqlite"):
        engine.dialect.statement_compiler = _HintingSQLiteCompiler
        if ":memory:" not in database_url:
            _configure_sqlite_pragmas(engine)
    return engine


def _configure_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def make_session_factory_from_settings(settings: Settings) -> Callable[[], Session]:
    if not settings.database_url:
        raise RuntimeError("DTXWIKI_DATABASE_URL is required for database sessions")
    return make_session_factory(make_engine(settings.database_url))

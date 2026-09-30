"""SQLite FTS5 projection DDL shared by migrations and test metadata setup.

The two virtual tables are rebuildable indexes, not systems of record, so they
are intentionally absent from SQLAlchemy's table metadata.  Registering the
DDL on ``Base.metadata`` keeps ``create_all``-based tests behaviorally aligned
with an Alembic-bootstrapped application.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from backend.models.base import Base

SEARCH_DOC_FTS_TABLE = "search_doc_fts"
PAGE_NAME_TRGM_TABLE = "page_name_trgm"

SEARCH_DOC_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_doc_fts USING fts5(
    context_header,
    body,
    workspace_id UNINDEXED,
    source_kind UNINDEXED,
    entity_id UNINDEXED,
    version_id UNINDEXED,
    visibility_scope UNINDEXED,
    category UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

PAGE_NAME_TRGM_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS page_name_trgm USING fts5(
    name,
    workspace_id UNINDEXED,
    page_id UNINDEXED,
    source_version_id UNINDEXED,
    visibility_scope UNINDEXED,
    name_kind UNINDEXED,
    name_norm UNINDEXED,
    tokenize = 'trigram'
)
"""

_FTS5_TABLE_PREFIXES = (SEARCH_DOC_FTS_TABLE, PAGE_NAME_TRGM_TABLE)
_FTS5_PROBE_INFO_KEY = "dtxwiki_fts5_available"
_FTS5_PROBE_TABLE = "__dtxwiki_fts5_probe"


class Fts5UnavailableError(RuntimeError):
    """Raised when the configured SQLite build does not provide FTS5."""


def require_fts5_available(connection: Connection) -> None:
    """Probe FTS5 once per pooled SQLite connection with an actionable error."""

    if connection.dialect.name != "sqlite":
        return
    if connection.info.get(_FTS5_PROBE_INFO_KEY):
        return
    created = False
    try:
        connection.exec_driver_sql(
            f"CREATE VIRTUAL TABLE temp.{_FTS5_PROBE_TABLE} USING fts5(content)"
        )
        created = True
    except SQLAlchemyError as exc:
        raise Fts5UnavailableError(
            "SQLite FTS5 support is required for Tier-B search; install a "
            "Python/SQLite build compiled with ENABLE_FTS5"
        ) from exc
    finally:
        if created:
            connection.exec_driver_sql(
                f"DROP TABLE IF EXISTS temp.{_FTS5_PROBE_TABLE}"
            )
    connection.info[_FTS5_PROBE_INFO_KEY] = True


def is_fts5_projection_table(name: str | None) -> bool:
    """Return whether ``name`` is a virtual table or one of its shadow tables."""

    return bool(name) and any(
        name == prefix or name.startswith(f"{prefix}_")
        for prefix in _FTS5_TABLE_PREFIXES
    )


def include_non_fts5_object(
    _object,
    name: str | None,
    type_: str,
    reflected: bool,
    _compare_to,
) -> bool:
    """Alembic include_object hook for rebuildable FTS virtual/shadow tables."""

    return not (
        reflected and type_ == "table" and is_fts5_projection_table(name)
    )


def ensure_fts5_tables(connection: Connection) -> None:
    """Create the Tier-B search projections on SQLite connections."""

    if connection.dialect.name != "sqlite":
        return
    require_fts5_available(connection)
    connection.exec_driver_sql(SEARCH_DOC_FTS_DDL)
    connection.exec_driver_sql(PAGE_NAME_TRGM_DDL)


def drop_fts5_tables(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {PAGE_NAME_TRGM_TABLE}")
    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {SEARCH_DOC_FTS_TABLE}")


def _after_create(_target, connection: Connection, **_kwargs) -> None:
    ensure_fts5_tables(connection)


def _before_drop(_target, connection: Connection, **_kwargs) -> None:
    drop_fts5_tables(connection)


event.listen(Base.metadata, "after_create", _after_create)
event.listen(Base.metadata, "before_drop", _before_drop)

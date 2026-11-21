"""Keep v0.2 Context parents across the frozen v0.3.1 draft conversion.

env.py stages them around that conversion; revision 002398e0fcdc turns the
staging table into History rows. A database stopped at c3d2e4f601ab keeps
the staging table until that revision runs.
"""

from __future__ import annotations

import re
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

# app_metadata names the staging table between the two steps, so only a
# table this capture created is ever read or dropped.
MARKER_KEY = "context_history_parent_capture"
_NAME = re.compile(r"_dtxwiki_history_parent_[0-9a-f]{32}")

_versions = sa.table(
    "context_page_version",
    sa.column("id"),
    sa.column("workspace_id"),
    sa.column("page_id"),
    sa.column("version_no"),
    sa.column("parent_version_id"),
)
_metadata = sa.table("app_metadata", sa.column("key"), sa.column("value"))


def staging_table(name: str) -> sa.Table:
    return sa.Table(
        name,
        sa.MetaData(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("parent_version_id", sa.Uuid()),
        sa.Column("converted_parent_version_id", sa.Uuid()),
    )


def capture(connection: Connection) -> str | None:
    """Stage each version's v0.2 parent before the conversion rewrites it."""
    if connection.dialect.name == "postgresql":
        # Take the lock the conversion's ALTER TABLE needs before reading the
        # source, so a version writer cannot change parents after the capture.
        connection.exec_driver_sql(
            "LOCK TABLE context_page_version IN ACCESS EXCLUSIVE MODE"
        )
    child, parent = _versions.alias("child"), _versions.alias("parent")
    # v0.2 allocated a version number after resolving an existing same-page
    # parent. An edge to the same or a newer version was already inconsistent.
    source = (
        sa.select(
            child.c.workspace_id,
            child.c.page_id,
            child.c.id,
            child.c.parent_version_id,
        )
        .select_from(
            child.outerjoin(
                parent,
                sa.and_(
                    parent.c.workspace_id == child.c.workspace_id,
                    parent.c.page_id == child.c.page_id,
                    parent.c.id == child.c.parent_version_id,
                    parent.c.version_no < child.c.version_no,
                ),
            )
        )
        .where(sa.or_(child.c.parent_version_id.is_(None), parent.c.id.is_not(None)))
    )
    # An empty database (a fresh install or the schema generator) keeps no
    # staging table.
    if connection.scalar(sa.select(sa.exists(source))) is not True:
        return None
    name = f"_dtxwiki_history_parent_{uuid4().hex}"
    staged = staging_table(name)
    staged.create(connection)
    connection.execute(
        staged.insert().from_select(
            ["workspace_id", "page_id", "version_id", "parent_version_id"], source
        )
    )
    return name


def record_converted(connection: Connection, name: str) -> None:
    """Keep the first Git parent the conversion assigned, then mark the capture."""
    staged = staging_table(name)
    converted = _versions.alias("converted")
    connection.execute(
        staged.update().values(
            converted_parent_version_id=sa.select(converted.c.parent_version_id)
            .where(
                converted.c.workspace_id == staged.c.workspace_id,
                converted.c.page_id == staged.c.page_id,
                converted.c.id == staged.c.version_id,
            )
            .scalar_subquery()
        )
    )
    connection.execute(_metadata.insert().values(key=MARKER_KEY, value=name))


def pending(connection: Connection) -> sa.Table | None:
    """The staging table a capture marked, if one awaits its History rows."""
    name = connection.scalar(
        sa.select(_metadata.c.value).where(_metadata.c.key == MARKER_KEY)
    )
    if name is None:
        return None
    if not _NAME.fullmatch(name) or not sa.inspect(connection).has_table(name):
        raise RuntimeError(
            f"Context History capture marker names no capture table: {name!r}. "
            "Restore the pre-upgrade dtxWiki backup."
        )
    return staging_table(name)


def clear(connection: Connection, staged: sa.Table) -> None:
    staged.drop(connection)
    connection.execute(_metadata.delete().where(_metadata.c.key == MARKER_KEY))

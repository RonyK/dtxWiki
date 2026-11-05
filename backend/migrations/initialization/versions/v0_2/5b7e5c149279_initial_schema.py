"""Create the complete v0.2 schema directly for a proven-empty installation.

The immutable SQL snapshots were captured from the actual release migrations
on empty SQLite and PostgreSQL databases. This revision executes final CREATE
statements once; it neither replays historical migrations nor imports today's
ORM metadata. Existing databases use the preserved upgrade/downgrade graphs.

Revision ID: 5b7e5c149279
Revises: None
"""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision = "5b7e5c149279"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("initial schema requires verification of an empty database")
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if tables - {"alembic_version"} or inspector.get_view_names():
        raise RuntimeError("initial schema refuses a database with existing objects")
    if "alembic_version" in tables and connection.scalar(
        sa.text("SELECT count(*) FROM alembic_version")
    ):
        raise RuntimeError("initial schema refuses an already versioned database")
    dialect = connection.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("initial schema supports only SQLite and PostgreSQL")
    snapshot = json.loads(
        (Path(__file__).resolve().parents[2] / "schema" / f"{dialect}.json").read_text(
            encoding="utf-8"
        )
    )
    if snapshot["revision"] != revision or snapshot["dialect"] != dialect:
        raise RuntimeError("initial schema snapshot does not match this revision")
    for statement in snapshot["statements"]:
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    # This alternate graph is selected only for an empty installation. Once
    # initialized, ordinary Alembic commands select the historical graph,
    # whose downgrade guards protect retained credentials and media evidence.
    raise RuntimeError("use the preserved historical graph to downgrade this database")

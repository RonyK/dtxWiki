"""Upgrade the published 0.1.2 schema directly to the final 0.1.3 schema.

Input checks and the new retention/content-event columns share each table's
DDL operation. SQLite copies each affected table once; PostgreSQL adds its
columns and validates its checks together in one ALTER TABLE per table.
Only proven published/active content events are backfilled afterwards.

The final development revision ID is retained because its resulting schema
and data contract are unchanged; the unpublished intermediate branches are
replaced by this single release boundary.

Revision ID: 19db79d3e622
Revises: efcbcf724889
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_1.content_event_backfill_v1 import (
    backfill_content_event_times,
)
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES,
    audit,
    rule_sql,
)

revision = "19db79d3e622"
down_revision = "efcbcf724889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # Every checked field already exists in the published parent. Refuse bad
    # legacy values before copying tables, adding columns or reading history.
    violations = audit(connection)
    if violations:
        raise RuntimeError(
            "input restrictions preflight failed; use inputs audit and explicit "
            f"UUID maintenance repairs before upgrading: {violations}"
        )

    _change_tables(add=True)
    op.create_table(
        "user_favorite",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('context_page', 'library_item', 'agent_page')",
            name="user_favorite_target_kind_check",
        ),
        sa.PrimaryKeyConstraint("user_id", "workspace_id", "target_kind", "target_id"),
    )
    backfill_content_event_times(connection)


def downgrade() -> None:
    op.drop_table("user_favorite")
    _change_tables(add=False)


def _new_columns(table: str) -> tuple[sa.Column, ...]:
    if table == "workspace":
        return (
            sa.Column(
                "context_media_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
    if table in {"context_page", "agent_page", "library_item"}:
        return (sa.Column("content_changed_at", sa.DateTime(timezone=True)),)
    return ()


def _change_tables(*, add: bool) -> None:
    connection = op.get_bind()
    dialect = connection.dialect
    for table in dict.fromkeys(rule.table for rule in RULES):
        rules = [rule for rule in RULES if rule.table == table]
        columns = _new_columns(table)
        if dialect.name == "sqlite":
            # Include new columns in the existing CHECK rebuild instead of
            # altering each table first and copying that intermediate shape.
            # Downgrade likewise removes columns and checks in the same copy.
            with op.batch_alter_table(table, recreate="always") as batch:
                if add:
                    for column in columns:
                        batch.add_column(column)
                    for rule in rules:
                        batch.create_check_constraint(
                            op.f(rule.name), rule_sql(rule, dialect.name)
                        )
                else:
                    for rule in rules:
                        batch.drop_constraint(op.f(rule.name), type_="check")
                    for column in columns:
                        batch.drop_column(column.name)
        else:
            # All identifiers and expressions are revision-owned constants.
            # One PostgreSQL ALTER validates multiple new CHECKs in one scan,
            # rather than scanning e.g. workspace once for each of its rules.
            quote = dialect.identifier_preparer.quote
            if add:
                changes = [
                    "ADD COLUMN "
                    + str(sa.schema.CreateColumn(column).compile(dialect=dialect))
                    for column in columns
                ]
                changes.extend(
                    f"ADD CONSTRAINT {quote(rule.name)} "
                    f"CHECK ({rule_sql(rule, dialect.name)})"
                    for rule in rules
                )
            else:
                changes = [f"DROP CONSTRAINT {quote(rule.name)}" for rule in rules]
                changes.extend(
                    f"DROP COLUMN {quote(column.name)}" for column in columns
                )
            op.execute(sa.text(f"ALTER TABLE {quote(table)} " + ", ".join(changes)))

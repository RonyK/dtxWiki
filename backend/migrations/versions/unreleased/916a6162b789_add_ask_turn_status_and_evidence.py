"""Add Ask turn status and the evidence each answer was given.

A question is stored as a ``pending`` turn when it is accepted, so it survives
an interrupted answer. ``input_evidence`` records every source the model was
given for the turn, including the evidence of the earlier turns it was shown,
and ``generation`` records which model produced the answer. Existing turns are
complete answers without that record.

Revision ID: 916a6162b789
Revises: dafc5339b3b1
Create Date: 2026-11-25 13:06:39.276788
"""

import sqlalchemy as sa
from alembic import op

revision = "916a6162b789"
down_revision = "dafc5339b3b1"
branch_labels = None
depends_on = None

_TABLE = "ask_turn"
_PENDING_INDEX = "uq_ask_turn_session_pending"
_PENDING = sa.text("status = 'pending'")


def upgrade() -> None:
    # A column CHECK, so SQLite can add it without rebuilding the table.
    op.add_column(
        _TABLE,
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('pending', 'completed', 'failed', 'stopped')",
                name="ck_ask_turn_status",
            ),
            nullable=False,
            server_default="completed",
        ),
    )
    op.add_column(_TABLE, sa.Column("error_code", sa.Text()))
    op.add_column(_TABLE, sa.Column("input_evidence", sa.JSON(none_as_null=True)))
    op.add_column(_TABLE, sa.Column("generation", sa.JSON(none_as_null=True)))
    op.create_index(
        _PENDING_INDEX,
        _TABLE,
        ["ask_session_id"],
        unique=True,
        sqlite_where=_PENDING,
        postgresql_where=_PENDING,
    )


def downgrade() -> None:
    # Plain column drops: a SQLite batch rebuild would drop and recreate
    # ask_turn, and the ON DELETE CASCADE children (citations) would go with it.
    op.drop_index(_PENDING_INDEX, table_name=_TABLE)
    for column in ("generation", "input_evidence", "error_code", "status"):
        op.drop_column(_TABLE, column)

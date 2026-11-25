"""Store Ask requests before they acquire the session's generation slot.

Revision ID: 837a0fa25ed1
Revises: 916a6162b789
"""

import sqlalchemy as sa
from alembic import op

revision = "837a0fa25ed1"
down_revision = "916a6162b789"
branch_labels = None
depends_on = None

_TABLE = "ask_turn"
_ACTIVE = "uq_ask_turn_session_pending"
_REQUEST = "uq_ask_turn_request"


def _create_active_index(predicate: str) -> None:
    where = sa.text(predicate)
    op.create_index(
        _ACTIVE,
        _TABLE,
        ["ask_session_id"],
        unique=True,
        sqlite_where=where,
        postgresql_where=where,
    )


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("request_id", sa.Uuid()))
    op.add_column(_TABLE, sa.Column("selection", sa.JSON(none_as_null=True)))
    op.add_column(
        _TABLE,
        sa.Column("steering_messages", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "steer_supported", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute("UPDATE ask_turn SET started_at = created_at WHERE status = 'pending'")
    op.drop_index(_ACTIVE, table_name=_TABLE)
    _create_active_index("status = 'pending' AND started_at IS NOT NULL")
    op.create_index(_REQUEST, _TABLE, ["ask_session_id", "request_id"], unique=True)


def downgrade() -> None:
    op.execute(
        "UPDATE ask_turn SET status = 'failed', error_code = 'server_restarted' "
        "WHERE status = 'pending' AND started_at IS NULL"
    )
    op.drop_index(_REQUEST, table_name=_TABLE)
    op.drop_index(_ACTIVE, table_name=_TABLE)
    _create_active_index("status = 'pending'")
    for column in (
        "steer_supported",
        "steering_messages",
        "selection",
        "request_id",
        "started_at",
    ):
        op.drop_column(_TABLE, column)

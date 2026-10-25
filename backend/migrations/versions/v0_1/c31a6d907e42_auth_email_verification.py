"""Add purpose-bound email confirmation for sensitive account operations.

Revision ID: c31a6d907e42
Revises: 7e2b9c4d6a18
"""

import sqlalchemy as sa
from alembic import op

revision = "c31a6d907e42"
down_revision = "7e2b9c4d6a18"
branch_labels = None
depends_on = None

_OLD_MESSAGE_KINDS = (
    "'password_reset', 'email_verification', 'password_changed', "
    "'job_failed', 'reconciliation_reminder'"
)
_RECIPIENT_CHECK = (
    "(message_kind = 'email_reauthentication' "
    "AND reauthentication_challenge_id IS NOT NULL) OR "
    "(message_kind != 'email_reauthentication' "
    "AND reauthentication_challenge_id IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "email_reauthentication_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_generation", sa.Integer(), nullable=False),
        sa.Column("session_digest", sa.Text(), nullable=False),
        sa.Column("binding_digest", sa.Text(), nullable=False),
        sa.Column("recipient_digest", sa.Text(), nullable=False),
        sa.Column("token_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope IN ('account', 'admin')", name="email_reauth_scope_check"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_email_reauth_expires_at",
        "email_reauthentication_challenges",
        ["expires_at"],
    )
    op.create_index(
        "ix_email_reauth_user_created_at",
        "email_reauthentication_challenges",
        ["user_id", "created_at"],
    )
    with op.batch_alter_table("email_outbox") as batch:
        batch.add_column(
            sa.Column("reauthentication_challenge_id", sa.Uuid(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_email_outbox_reauthentication_challenge",
            "email_reauthentication_challenges",
            ["reauthentication_challenge_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint("email_outbox_message_kind_check", type_="check")
        batch.create_check_constraint(
            "email_outbox_message_kind_check",
            f"message_kind IN ({_OLD_MESSAGE_KINDS}, 'email_reauthentication')",
        )
        batch.create_check_constraint("email_outbox_recipient_check", _RECIPIENT_CHECK)


def downgrade() -> None:
    # Even consumed/expired rows are evidence. Refuse before any DDL.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM email_reauthentication_challenges")
    ) or connection.scalar(
        sa.text(
            "SELECT count(*) FROM email_outbox "
            "WHERE message_kind = 'email_reauthentication'"
        )
    ):
        raise RuntimeError("auth email verification downgrade would discard data")
    with op.batch_alter_table("email_outbox") as batch:
        batch.drop_constraint("email_outbox_recipient_check", type_="check")
        batch.drop_constraint("email_outbox_message_kind_check", type_="check")
        batch.create_check_constraint(
            "email_outbox_message_kind_check", f"message_kind IN ({_OLD_MESSAGE_KINDS})"
        )
        batch.drop_constraint(
            "fk_email_outbox_reauthentication_challenge", type_="foreignkey"
        )
        batch.drop_column("reauthentication_challenge_id")
    op.drop_index(
        "ix_email_reauth_user_created_at",
        table_name="email_reauthentication_challenges",
    )
    op.drop_index(
        "ix_email_reauth_expires_at", table_name="email_reauthentication_challenges"
    )
    op.drop_table("email_reauthentication_challenges")

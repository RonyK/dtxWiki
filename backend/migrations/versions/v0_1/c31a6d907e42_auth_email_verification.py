"""Add browser-bound external signup and scoped email reauthentication.

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
    "(message_kind = 'pending_signup' AND recipient_user_id IS NULL "
    "AND pending_signup_id IS NOT NULL AND reauthentication_challenge_id IS NULL) OR "
    "(message_kind = 'email_reauthentication' AND recipient_user_id IS NOT NULL "
    "AND pending_signup_id IS NULL AND reauthentication_challenge_id IS NOT NULL) OR "
    "(message_kind NOT IN ('pending_signup', 'email_reauthentication') "
    "AND recipient_user_id IS NOT NULL AND pending_signup_id IS NULL "
    "AND reauthentication_challenge_id IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "auth_pending_signups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("binding_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("provider_slug", sa.Text(), nullable=False),
        sa.Column("assertion_data", sa.JSON(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column(
            "remember_me", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("token_digest", sa.Text(), nullable=True, unique=True),
        sa.Column("email_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auth_pending_signups_expires_at", "auth_pending_signups", ["expires_at"]
    )
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
    with op.batch_alter_table("email_outbox") as batch:
        batch.add_column(sa.Column("pending_signup_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("reauthentication_challenge_id", sa.Uuid(), nullable=True)
        )
        batch.alter_column("recipient_user_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_foreign_key(
            "fk_email_outbox_pending_signup",
            "auth_pending_signups",
            ["pending_signup_id"],
            ["id"],
            ondelete="CASCADE",
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
            f"message_kind IN ({_OLD_MESSAGE_KINDS}, "
            "'pending_signup', 'email_reauthentication')",
        )
        batch.create_check_constraint("email_outbox_recipient_check", _RECIPIENT_CHECK)


def downgrade() -> None:
    # Refuse before any DDL. Even consumed/expired rows are authentication
    # evidence; a downgrade must not silently erase that persisted state.
    connection = op.get_bind()
    populated = [
        table
        for table in ("auth_pending_signups", "email_reauthentication_challenges")
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
    ]
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM email_outbox WHERE message_kind IN "
            "('pending_signup', 'email_reauthentication')"
        )
    ):
        populated.append("email_outbox")
    if populated:
        raise RuntimeError(
            "auth email verification downgrade would discard data in "
            + ", ".join(populated)
        )
    with op.batch_alter_table("email_outbox") as batch:
        batch.drop_constraint("email_outbox_recipient_check", type_="check")
        batch.drop_constraint("email_outbox_message_kind_check", type_="check")
        batch.create_check_constraint(
            "email_outbox_message_kind_check",
            f"message_kind IN ({_OLD_MESSAGE_KINDS})",
        )
        batch.drop_constraint("fk_email_outbox_pending_signup", type_="foreignkey")
        batch.drop_constraint(
            "fk_email_outbox_reauthentication_challenge", type_="foreignkey"
        )
        batch.drop_column("pending_signup_id")
        batch.drop_column("reauthentication_challenge_id")
        batch.alter_column("recipient_user_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_index(
        "ix_email_reauth_expires_at", table_name="email_reauthentication_challenges"
    )
    op.drop_table("email_reauthentication_challenges")
    op.drop_index(
        "ix_auth_pending_signups_expires_at", table_name="auth_pending_signups"
    )
    op.drop_table("auth_pending_signups")

"""Add native external identities and single-use login transactions.

Revision ID: 7e2b9c4d6a18
Revises: ef53a0a7061c
"""

from dataclasses import replace

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES,
    rule_sql,
)

revision = "7e2b9c4d6a18"
down_revision = "ef53a0a7061c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _native_credentials_nullable(True)
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_slug", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email_at_link", sa.Text(), nullable=True),
        sa.Column("last_email", sa.Text(), nullable=True),
        sa.Column("directory_tenant", sa.Text(), nullable=True),
        sa.Column(
            "last_email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("linked_via", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "issuer", "subject", name="uq_user_identities_issuer_subject"
        ),
        sa.UniqueConstraint(
            "user_id", "provider_slug", name="uq_user_identities_user_provider"
        ),
        sa.CheckConstraint(
            "linked_via IN ('jit', 'user_link', 'admin', 'scim')",
            name="user_identity_linked_via_check",
        ),
    )
    op.create_index(
        "ix_user_identities_provider_slug", "user_identities", ["provider_slug"]
    )
    op.create_table(
        "auth_login_transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state_digest", sa.Text(), nullable=False),
        sa.Column("binding_digest", sa.Text(), nullable=False),
        sa.Column("provider_slug", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("session_generation", sa.Integer(), nullable=True),
        sa.Column("session_digest", sa.Text(), nullable=True),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column(
            "remember_me", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("protocol_request_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "state_digest", name="uq_auth_login_transactions_state_digest"
        ),
        sa.CheckConstraint(
            "purpose IN ('login', 'link', 'reauth')",
            name="auth_login_transaction_purpose_check",
        ),
    )
    op.create_index(
        "ix_auth_login_transactions_expires_at",
        "auth_login_transactions",
        ["expires_at"],
    )


def downgrade() -> None:
    # Refuse before any DDL: the parent cannot represent these accounts or
    # preserve external identity/transaction evidence. Never invent credentials.
    connection = op.get_bind()
    incompatible = connection.scalar(
        sa.text(
            "SELECT count(*) FROM users WHERE password_hash IS NULL "
            "OR email IS NULL OR pending_email IS NOT NULL"
        )
    )
    if incompatible:
        raise RuntimeError(
            "external login downgrade requires native email/password and no "
            f"pending email; {incompatible} incompatible account(s) remain"
        )
    populated = [
        table
        for table in ("user_identities", "auth_login_transactions")
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
    ]
    if populated:
        raise RuntimeError(
            "external login downgrade would discard data in " + ", ".join(populated)
        )
    op.drop_index(
        "ix_auth_login_transactions_expires_at", table_name="auth_login_transactions"
    )
    op.drop_table("auth_login_transactions")
    op.drop_index("ix_user_identities_provider_slug", table_name="user_identities")
    op.drop_table("user_identities")
    _native_credentials_nullable(False)


def _native_credentials_nullable(nullable: bool) -> None:
    email_rule = next(
        rule for rule in RULES if rule.table == "users" and rule.field == "email"
    )
    email_check = rule_sql(
        replace(email_rule, nullable=nullable), op.get_bind().dialect.name
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("users") as batch:
            batch.drop_constraint(email_rule.name, type_="check")
            batch.create_check_constraint(email_rule.name, email_check)
            batch.alter_column(
                "password_hash", existing_type=sa.Text(), nullable=nullable
            )
            batch.alter_column("email", existing_type=sa.Text(), nullable=nullable)
            if nullable:
                batch.add_column(sa.Column("pending_email", sa.Text(), nullable=True))
            else:
                batch.drop_column("pending_email")
    else:
        op.drop_constraint(email_rule.name, "users", type_="check")
        op.create_check_constraint(email_rule.name, "users", email_check)
        op.alter_column(
            "users", "password_hash", existing_type=sa.Text(), nullable=nullable
        )
        op.alter_column("users", "email", existing_type=sa.Text(), nullable=nullable)
        if nullable:
            op.add_column("users", sa.Column("pending_email", sa.Text(), nullable=True))
        else:
            op.drop_column("users", "pending_email")

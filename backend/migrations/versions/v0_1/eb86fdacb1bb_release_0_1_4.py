"""Upgrade the published 0.1.3 schema directly to the final 0.1.4 schema.

0.1.4 adds external login (nullable native credentials, linked identities,
login transactions, email reauthentication and email conflict review),
retained account deletion, and lookup indexes for change operations and
review decision history. The new account's personal workspace pointer is
dropped; its workspaces and memberships stay and are ordinary workspaces.

All ``users`` changes share one DDL operation: SQLite copies the table once,
instead of once per feature. Nullable additions need no backfill. Downgrade
checks every refusal before its first DDL, so a refused downgrade leaves the
complete 0.1.4 schema and stamp in place.

The final development revision ID is retained because its resulting schema
and data contract are unchanged; the unpublished intermediate revisions are
replaced by this single release boundary.

Revision ID: eb86fdacb1bb
Revises: 19db79d3e622
"""

from dataclasses import replace

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES,
    rule_sql,
)

revision = "eb86fdacb1bb"
down_revision = "19db79d3e622"
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
_EXTERNAL_LOGIN_TABLES = (
    "user_identities",
    "auth_login_transactions",
    "email_reauthentication_challenges",
    "auth_email_conflict_requests",
)


def _deletion_columns() -> tuple[sa.Column, ...]:
    # Existing users stay active and have no deletion deadline.
    return (
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("deletion_retention_days", sa.Integer()),
        sa.Column("deletion_prior_status", sa.Text()),
        sa.Column("deletion_failure_count", sa.Integer()),
        sa.Column("deletion_last_error", sa.Text()),
    )


def upgrade() -> None:
    op.create_index(
        "ix_change_operation_change_set_id",
        "change_operation",
        ["change_set_id"],
        unique=False,
    )
    _change_users(upgrade=True)
    op.create_index("ix_users_purge_after", "users", ["purge_after"], unique=False)
    op.add_column(
        "workspace", sa.Column("account_deletion_user_id", sa.Uuid(as_uuid=True))
    )
    op.create_index(
        "ix_workspace_account_deletion_user_id",
        "workspace",
        ["account_deletion_user_id"],
        unique=False,
    )
    _create_external_login_tables()
    _create_email_verification_tables()
    # Review detail reads history by review, newest first, with id as the
    # tie-breaker; undo reads the same order with LIMIT 2. Revision-owned
    # expressions keep replays independent of future ORM changes.
    op.create_index(
        "ix_review_decision_review_created_id",
        "review_decision",
        ["review_item_id", sa.column("created_at").desc(), sa.column("id").desc()],
        unique=False,
    )


def downgrade() -> None:
    _assert_downgrade_safe()
    op.drop_index("ix_review_decision_review_created_id", table_name="review_decision")
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
    op.drop_index(
        "ix_auth_email_conflict_target", table_name="auth_email_conflict_requests"
    )
    op.drop_index(
        "ix_auth_email_conflict_status_created",
        table_name="auth_email_conflict_requests",
    )
    op.drop_table("auth_email_conflict_requests")
    op.drop_index(
        "ix_auth_login_transactions_expires_at", table_name="auth_login_transactions"
    )
    op.drop_table("auth_login_transactions")
    op.drop_index("ix_user_identities_provider_slug", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_index("ix_workspace_account_deletion_user_id", table_name="workspace")
    with op.batch_alter_table("workspace") as batch:
        batch.drop_column("account_deletion_user_id")
    op.drop_index("ix_users_purge_after", table_name="users")
    _change_users(upgrade=False)
    op.drop_index("ix_change_operation_change_set_id", table_name="change_operation")


def _change_users(*, upgrade: bool) -> None:
    """Apply the pointer, native-credential and deletion changes together.

    Batch operations run in order on PostgreSQL and in one table copy on
    SQLite. The restored pointer is nullable and never guessed back.
    """

    email_rule = next(
        rule for rule in RULES if rule.table == "users" and rule.field == "email"
    )
    email_check = rule_sql(
        replace(email_rule, nullable=upgrade), op.get_bind().dialect.name
    )
    with op.batch_alter_table("users") as batch:
        if upgrade:
            batch.drop_constraint("uq_users_personal_workspace_id", type_="unique")
            batch.drop_constraint(
                "fk_users_personal_workspace_id_workspace", type_="foreignkey"
            )
            batch.drop_column("personal_workspace_id")
        else:
            for column in reversed(_deletion_columns()):
                batch.drop_column(column.name)
        batch.drop_constraint(email_rule.name, type_="check")
        batch.create_check_constraint(email_rule.name, email_check)
        batch.alter_column("password_hash", existing_type=sa.Text(), nullable=upgrade)
        batch.alter_column("email", existing_type=sa.Text(), nullable=upgrade)
        if upgrade:
            batch.add_column(sa.Column("pending_email", sa.Text(), nullable=True))
            for column in _deletion_columns():
                batch.add_column(column)
        else:
            batch.drop_column("pending_email")
            batch.add_column(
                sa.Column("personal_workspace_id", sa.Uuid(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_users_personal_workspace_id_workspace",
                "workspace",
                ["personal_workspace_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_unique_constraint(
                "uq_users_personal_workspace_id", ["personal_workspace_id"]
            )


def _create_external_login_tables() -> None:
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


def _create_email_verification_tables() -> None:
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
    op.create_table(
        "auth_email_conflict_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("provider_slug", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'resolved', "
            "'auto_released')",
            name="auth_email_conflict_status_check",
        ),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_auth_email_conflict_status_created",
        "auth_email_conflict_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_auth_email_conflict_target",
        "auth_email_conflict_requests",
        ["target_user_id"],
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


def _assert_downgrade_safe() -> None:
    # Refuse before any DDL: 0.1.3 cannot represent pending deletions,
    # accounts without native email/password, or external identity and
    # transaction evidence. Never invent credentials or discard evidence.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE status = 'deletion_pending')")
    ):
        raise RuntimeError(
            "cannot remove account deletion deadlines while deletion requests "
            "are pending; finish account cleanup before downgrading"
        )
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
        for table in _EXTERNAL_LOGIN_TABLES
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
    ]
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM email_outbox "
            "WHERE message_kind = 'email_reauthentication'"
        )
    ):
        populated.append("email_outbox")
    if populated:
        raise RuntimeError(
            "external login downgrade would discard data in " + ", ".join(populated)
        )

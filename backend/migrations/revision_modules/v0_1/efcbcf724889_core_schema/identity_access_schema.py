"""Enforce MCP client lifecycle identity and D-01v2/D-01v3 credential planes.

Originally bound each Access Key to exactly one Workspace (D-01). P10.3
redefined that as an allowed-workspace SET per credential (D-01v2) split
across a control-plane index and a workspace-plane replica
(D-01v3, .claude/plan/260909-p10-3-multi-workspace-credential.md section 2).
Rewritten in place rather than adding a new revision (central plan rule 2):
this is still the one pre-release dev build with no real deployed data to
migrate around, so the target shape is created directly rather than layered
through the row's original singular-binding history.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection


def upgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.add_column(sa.Column("catalog_app_id", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("catalog_app_label_snapshot", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("catalog_revision", sa.Text(), nullable=True))
    op.create_index(
        "ix_api_tokens_user_created_id",
        "api_tokens",
        ["user_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "api_token_workspaces",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("desired_scopes", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sync_state", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["token_id"], ["api_tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_id", "workspace_id"),
    )
    op.create_index(
        "ix_api_token_workspaces_workspace_token",
        "api_token_workspaces",
        ["workspace_id", "token_id"],
        unique=False,
    )

    _backfill_access_key_catalog_metadata(bind)

    with op.batch_alter_table("oauth_codes") as batch_op:
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("oauth_refresh_tokens") as batch_op:
        batch_op.drop_column("workspace_id")
        batch_op.add_column(sa.Column("grant_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("rotated_from_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_oauth_refresh_tokens_user_client_expires_at",
        "oauth_refresh_tokens",
        ["user_id", "client_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_refresh_tokens_grant_id",
        "oauth_refresh_tokens",
        ["grant_id"],
        unique=False,
    )

    with op.batch_alter_table("oauth_consents") as batch_op:
        # These are part of the final consent shape, not an intermediate
        # lifetime phase. NULL uses the instance refresh-lifetime default.
        batch_op.add_column(
            sa.Column("refresh_ttl_seconds", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "sliding_enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("desired_scopes", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column(
                "sync_state",
                sa.Text(),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_sync_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("catalog_app_id", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("catalog_app_label_snapshot", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("catalog_revision", sa.Text(), nullable=True))

    op.create_table(
        "workspace_credential_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("credential_kind", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("synced_revision", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "credential_kind",
            "credential_id",
            name="uq_workspace_credential_grant_target",
        ),
    )
    op.create_index(
        "ix_workspace_credential_grant_workspace_user",
        "workspace_credential_grant",
        ["workspace_id", "user_id"],
        unique=False,
    )

    with op.batch_alter_table("oauth_clients") as batch_op:
        batch_op.add_column(
            sa.Column("registration_identity", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("catalog_identity_fingerprint", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("catalog_app_id", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("catalog_app_label_snapshot", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("catalog_revision", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("catalog_association_provenance", sa.Text(), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_oauth_client_registration_identity",
            ["registration_identity"],
        )

    with op.batch_alter_table("mcp_client") as batch_op:
        batch_op.add_column(sa.Column("runtime_binding_key", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

    _backfill_runtime_binding_keys(bind)

    with op.batch_alter_table("mcp_client") as batch_op:
        batch_op.create_unique_constraint(
            "mcp_client_workspace_runtime_binding_uq",
            ["workspace_id", "runtime_binding_key"],
        )
        batch_op.create_unique_constraint(
            "uq_mcp_client_workspace_id",
            ["workspace_id", "id"],
        )
    op.create_index(
        "ix_mcp_client_workspace_deleted_status_created_id",
        "mcp_client",
        ["workspace_id", "deleted_at", "status", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    _refuse_downgrade_with_existing_oauth_sessions(op.get_bind())
    op.drop_index(
        "ix_mcp_client_workspace_deleted_status_created_id",
        table_name="mcp_client",
    )
    with op.batch_alter_table("mcp_client") as batch_op:
        batch_op.drop_constraint(
            "uq_mcp_client_workspace_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "mcp_client_workspace_runtime_binding_uq",
            type_="unique",
        )
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("runtime_binding_key")

    op.drop_index(
        "ix_workspace_credential_grant_workspace_user",
        table_name="workspace_credential_grant",
    )
    op.drop_table("workspace_credential_grant")

    op.drop_index(
        "ix_oauth_refresh_tokens_user_client_expires_at",
        table_name="oauth_refresh_tokens",
    )
    op.drop_index(
        "ix_oauth_refresh_tokens_grant_id",
        table_name="oauth_refresh_tokens",
    )
    with op.batch_alter_table("oauth_consents") as batch_op:
        batch_op.drop_column("last_sync_error")
        batch_op.drop_column("removed_at")
        batch_op.drop_column("sync_state")
        batch_op.drop_column("revision")
        batch_op.drop_column("desired_scopes")
        batch_op.drop_column("catalog_revision")
        batch_op.drop_column("catalog_app_label_snapshot")
        batch_op.drop_column("catalog_app_id")
        batch_op.drop_column("sliding_enabled")
        batch_op.drop_column("refresh_ttl_seconds")
    with op.batch_alter_table("oauth_refresh_tokens") as batch_op:
        batch_op.drop_column("grant_id")
        batch_op.drop_column("rotated_from_id")
        batch_op.drop_column("rotated_at")
        batch_op.add_column(sa.Column("workspace_id", sa.Uuid(), nullable=False))
        batch_op.create_foreign_key(
            "fk_oauth_refresh_tokens_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("oauth_codes") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Uuid(), nullable=False))
        batch_op.create_foreign_key(
            "fk_oauth_codes_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("oauth_clients") as batch_op:
        batch_op.drop_constraint(
            "uq_oauth_client_registration_identity",
            type_="unique",
        )
        batch_op.drop_column("catalog_association_provenance")
        batch_op.drop_column("catalog_revision")
        batch_op.drop_column("catalog_app_label_snapshot")
        batch_op.drop_column("catalog_app_id")
        batch_op.drop_column("catalog_identity_fingerprint")
        batch_op.drop_column("registration_identity")

    op.drop_index(
        "ix_api_token_workspaces_workspace_token",
        table_name="api_token_workspaces",
    )
    op.drop_table("api_token_workspaces")

    op.drop_index(
        "ix_api_tokens_user_created_id",
        table_name="api_tokens",
    )
    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.drop_column("catalog_revision")
        batch_op.drop_column("catalog_app_label_snapshot")
        batch_op.drop_column("catalog_app_id")


def _backfill_runtime_binding_keys(connection: Connection) -> None:
    """Backfill only credential identities recorded by the server.

    Duplicate evidence is quarantined by disabling every conflicting row. One
    deterministic canonical row retains the binding so the same credential is
    denied on its next call instead of creating a replacement active row; the
    other historical duplicates remain unbound to satisfy the unique key.
    """

    clients = sa.table(
        "mcp_client",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("name", sa.Text()),
        sa.column("client_type", sa.Text()),
        sa.column("status", sa.Text()),
        sa.column("token_metadata", sa.JSON()),
        sa.column("runtime_binding_key", sa.Text()),
    )
    rows = connection.execute(
        sa.select(
            clients.c.id,
            clients.c.workspace_id,
            clients.c.name,
            clients.c.client_type,
            clients.c.token_metadata,
        )
    ).mappings()
    candidates = []
    for row in rows:
        key = _runtime_binding_key_from_legacy_row(row)
        if key is not None:
            candidates.append((row["id"], row["workspace_id"], key))
    grouped_ids: dict[tuple[UUID, str], list[UUID]] = {}
    for client_id, workspace_id, key in candidates:
        grouped_ids.setdefault((workspace_id, key), []).append(client_id)
    for client_id, workspace_id, key in candidates:
        duplicate_ids = grouped_ids[(workspace_id, key)]
        if len(duplicate_ids) > 1:
            canonical_id = min(duplicate_ids, key=str)
            connection.execute(
                clients.update()
                .where(clients.c.id == client_id)
                .values(
                    status="disabled",
                    runtime_binding_key=(key if client_id == canonical_id else None),
                )
            )
            continue
        connection.execute(
            clients.update()
            .where(clients.c.id == client_id)
            .values(runtime_binding_key=key)
        )


def _backfill_access_key_catalog_metadata(connection: Connection) -> None:
    """Copy only legacy display metadata that is already explicit.

    The migration is intentionally independent of the mutable packaged catalog.
    The two legacy values below were the complete pre-catalog allowlist, so their
    IDs are stable evidence for a display-only association. ``catalog_revision``
    remains null because old rows were not issued from a versioned catalog.
    """

    api_tokens = sa.table(
        "api_tokens",
        sa.column("mcp_client_type", sa.Text()),
        sa.column("mcp_client_label", sa.Text()),
        sa.column("catalog_app_id", sa.Text()),
        sa.column("catalog_app_label_snapshot", sa.Text()),
        sa.column("catalog_revision", sa.Text()),
    )
    connection.execute(
        api_tokens.update()
        .where(api_tokens.c.mcp_client_type.in_(("claude-code", "codex")))
        .values(
            catalog_app_id=api_tokens.c.mcp_client_type,
            catalog_app_label_snapshot=api_tokens.c.mcp_client_label,
        )
    )
    connection.execute(
        api_tokens.update()
        .where(
            api_tokens.c.catalog_app_label_snapshot.is_(None),
            api_tokens.c.mcp_client_label.is_not(None),
        )
        .values(catalog_app_label_snapshot=api_tokens.c.mcp_client_label)
    )


def _runtime_binding_key_from_legacy_row(row) -> str | None:
    metadata = row["token_metadata"]
    if not isinstance(metadata, dict):
        metadata = {}
    token_kind = metadata.get("token_kind")
    token_id = _canonical_uuid(metadata.get("token_id"))
    if token_kind == "oauth_jwt" and token_id is not None:
        return f"oauth_grant:{token_id}"
    if token_kind in {"api_token", "pat"} and token_id is not None:
        return f"api_token:{token_id}"
    if row["client_type"] in {
        "internal",
        "system",
        "internal_agent",
        "system_managed",
    }:
        return f"mcp_client_name:{row['name']}"
    return None


def _canonical_uuid(value) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _refuse_downgrade_with_existing_oauth_sessions(
    connection: sa.engine.Connection,
) -> None:
    """Fail loud rather than invent a workspace for a live OAuth session.

    0001 pins each refresh token and authorization code to exactly one
    workspace. D-01v2/v3 replaced that with a per-credential workspace SET,
    and ``upgrade()`` drops ``workspace_id`` outright -- the old single value
    is preserved nowhere, so ``downgrade()`` has nothing to restore it from.
    Re-adding the column NOT NULL therefore aborts the moment either table
    holds a row (PostgreSQL: "contains null values"; SQLite: "NOT NULL
    constraint failed" during the batch copy), and it did so silently in
    testing only because ``test_postgres_migration.py`` seeds users and one
    workspace but never an OAuth session.

    Picking one workspace out of the consent's set would be a guess that
    silently re-scopes a live credential, which is the thing this revision
    refuses to do elsewhere too (see context_schema's module docstring:
    invalid legacy grants are revoked, not guessed). So this refuses instead,
    the same deliberately-irreversible posture
    ``library_media_schema._refuse_lossy_integrated_media_downgrade`` and
    ``context_schema._refuse_downgrade_with_existing_editing_rows`` already
    take. Both row kinds are re-obtainable by logging in again, so the
    operator's recovery is to revoke rather than to migrate.
    """

    for table_name in ("oauth_refresh_tokens", "oauth_codes"):
        remaining = connection.scalar(
            sa.select(sa.func.count()).select_from(sa.table(table_name))
        )
        if remaining:
            raise RuntimeError(
                f"{table_name} still holds {remaining} row(s). 0002 dropped "
                "the single-workspace binding these rows carried under 0001 "
                "and it cannot be reconstructed, so this downgrade would "
                "either fail on the NOT NULL column or invent a workspace. "
                "Revoke the affected OAuth sessions -- they are re-obtainable "
                "by logging in again -- then retry the downgrade."
            )

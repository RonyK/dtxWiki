from importlib import import_module
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import func, select

from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.services.mcp_client_service import evaluate_mcp_client_runtime

migration = import_module(
    "backend.migrations.revision_0002.identity_access_schema"
)


# P10.3 T1: `_revoke_unbound_access_keys` no longer exists. It belonged to
# the old D-01 upgrade() path (add a nullable api_tokens.workspace_id column,
# then revoke any row that came out unbound because there was nothing to
# infer it from). The rewritten migration never creates that column at all --
# it goes straight to the D-01v2 api_token_workspaces shape -- so there is no
# unbound-legacy-row state left to revoke, and no equivalent helper to test.


def test_legacy_catalog_metadata_backfill_preserves_snapshot_without_fake_revision():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    tokens = sa.Table(
        "api_tokens",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("mcp_client_type", sa.Text(), nullable=True),
        sa.Column("mcp_client_label", sa.Text(), nullable=True),
        sa.Column("catalog_app_id", sa.Text(), nullable=True),
        sa.Column("catalog_app_label_snapshot", sa.Text(), nullable=True),
        sa.Column("catalog_revision", sa.Text(), nullable=True),
    )
    metadata.create_all(engine)
    codex_id = uuid4()
    generic_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            tokens.insert(),
            [
                {
                    "id": codex_id,
                    "mcp_client_type": "codex",
                    "mcp_client_label": "Old Codex label",
                },
                {
                    "id": generic_id,
                    "mcp_client_type": "generic",
                    "mcp_client_label": "Research client",
                },
            ],
        )
        migration._backfill_access_key_catalog_metadata(connection)
        rows = {
            row.id: row
            for row in connection.execute(sa.select(tokens)).mappings()
        }

    assert rows[codex_id]["catalog_app_id"] == "codex"
    assert rows[codex_id]["catalog_app_label_snapshot"] == "Old Codex label"
    assert rows[codex_id]["catalog_revision"] is None
    assert rows[generic_id]["catalog_app_id"] is None
    assert rows[generic_id]["catalog_app_label_snapshot"] == "Research client"


def test_runtime_binding_backfill_quarantines_duplicate_credential_evidence():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    clients = sa.Table(
        "mcp_client",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("token_metadata", sa.JSON(), nullable=False),
        sa.Column("runtime_binding_key", sa.Text(), nullable=True),
    )
    metadata.create_all(engine)
    workspace_id = uuid4()
    grant_id = uuid4()
    unique_token_id = uuid4()
    ids = [uuid4(), uuid4(), uuid4()]
    with engine.begin() as connection:
        connection.execute(
            clients.insert(),
            [
                {
                    "id": ids[0],
                    "workspace_id": workspace_id,
                    "name": "OAuth A",
                    "client_type": "oauth",
                    "status": "active",
                    "token_metadata": {
                        "token_kind": "oauth_jwt",
                        "token_id": str(grant_id),
                    },
                },
                {
                    "id": ids[1],
                    "workspace_id": workspace_id,
                    "name": "OAuth duplicate",
                    "client_type": "oauth",
                    "status": "active",
                    "token_metadata": {
                        "token_kind": "oauth_jwt",
                        "token_id": str(grant_id),
                    },
                },
                {
                    "id": ids[2],
                    "workspace_id": workspace_id,
                    "name": "PAT",
                    "client_type": "api_token",
                    "status": "active",
                    "token_metadata": {
                        "token_kind": "api_token",
                        "token_id": str(unique_token_id),
                    },
                },
            ],
        )
        migration._backfill_runtime_binding_keys(connection)
        rows = {
            row.id: row
            for row in connection.execute(sa.select(clients)).mappings()
        }

    canonical_id = min(ids[:2], key=str)
    duplicate_id = next(client_id for client_id in ids[:2] if client_id != canonical_id)
    assert rows[canonical_id]["status"] == "disabled"
    assert rows[canonical_id]["runtime_binding_key"] == f"oauth_grant:{grant_id}"
    assert rows[duplicate_id]["status"] == "disabled"
    assert rows[duplicate_id]["runtime_binding_key"] is None
    assert rows[ids[2]]["runtime_binding_key"] == f"api_token:{unique_token_id}"


def test_duplicate_backfill_binding_prevents_runtime_auto_resurrection(page_service):
    workspace_id = None
    grant_id = uuid4()
    duplicate_ids = [uuid4(), uuid4()]
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        workspace_id = workspace.id
        for client_id, name in zip(
            duplicate_ids,
            ("Legacy OAuth A", "Legacy OAuth duplicate"),
            strict=True,
        ):
            session.add(
                MCPClient(
                    id=client_id,
                    workspace_id=workspace_id,
                    name=name,
                    client_type="oauth",
                    scopes=["mcp:read"],
                    status="active",
                    token_metadata={
                        "token_kind": "oauth_jwt",
                        "token_id": str(grant_id),
                    },
                )
            )
        session.flush()
        migration._backfill_runtime_binding_keys(session.connection())

    actor = ActorContext(
        actor_kind="human",
        actor_id="legacy-oauth-user",
        user_id=uuid4(),
        scopes=("mcp:read",),
        workspace_role="viewer",
        workspace_id=workspace_id,
        role="viewer",
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id="legacy-oauth-registration",
        token_workspace_ids=(workspace_id,),
        provenance="oauth_client",
    )
    with page_service.session_factory.begin() as session:
        count_before = session.scalar(select(func.count()).select_from(MCPClient))
        denial = evaluate_mcp_client_runtime(
            session,
            workspace_id=workspace_id,
            actor=actor,
        )
        count_after = session.scalar(select(func.count()).select_from(MCPClient))
        bound_rows = session.scalars(
            select(MCPClient).where(
                MCPClient.workspace_id == workspace_id,
                MCPClient.runtime_binding_key == f"oauth_grant:{grant_id}",
            )
        ).all()

    assert denial is not None
    assert denial.error_code == "mcp_client_disabled"
    assert count_after == count_before
    assert len(bound_rows) == 1
    assert bound_rows[0].status == "disabled"

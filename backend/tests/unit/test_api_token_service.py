import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.catalog.mcp_apps import (
    CatalogRepository,
    get_mcp_app_catalog_repository,
)
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.policy.auth import _actor_from_user
from backend.policy.rbac import grant_workspace_membership
from backend.policy.trust_boundaries import actor_with_target_workspace_scope
from backend.services.api_token_service import (
    ApiTokenNotFoundError,
    ApiTokenService,
    ApiTokenWorkspaceAccessError,
    InvalidApiTokenCursorError,
    InvalidApiTokenError,
    InvalidApiTokenRequestError,
    WorkspaceGrantRequest,
    api_token_by_id_for_update_statement,
    api_token_by_lookup_hash_for_update_statement,
)
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import SessionService, SessionTokenCodec


def fast_hasher() -> CredentialHasher:
    return CredentialHasher(
        PasswordHasher(
            time_cost=1,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


@pytest.fixture
def credential_hasher() -> CredentialHasher:
    return fast_hasher()


@pytest.fixture
def signed_up_user(session_factory, credential_hasher):
    service = SessionService(
        session_factory=session_factory,
        credential_hasher=credential_hasher,
        token_codec=SessionTokenCodec(secret="test-session-secret"),
        # AUTH-09: signup_enabled now defaults to False (fail closed); opt in
        # explicitly since this fixture only needs a signed-up user, not a
        # signup-disabled scenario.
        signup_enabled=True,
    )
    return service.signup(email="token-owner@example.com", password="owner-pass").user


@pytest.fixture
def token_service(session_factory, credential_hasher) -> ApiTokenService:
    return ApiTokenService(
        session_factory=session_factory,
        credential_hasher=credential_hasher,
        cursor_secret="test-access-key-cursor-secret",
    )


@pytest.fixture
def signed_up_workspace_id(session_factory, signed_up_user):
    with session_factory() as session:
        user = session.get(User, signed_up_user.user_id)
        assert user is not None
        assert user.personal_workspace_id is not None
        return user.personal_workspace_id


def _grant(
    workspace_id, scopes: list[str] | None = None
) -> list[WorkspaceGrantRequest]:
    return [WorkspaceGrantRequest(workspace_id=workspace_id, scopes=scopes)]


def test_create_token_returns_plaintext_once_and_stores_only_hashes(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name=" CI token ",
        scopes=["mcp:write", "mcp:read"],
    )

    assert issued.plain_token.startswith("dtxw_")
    assert issued.api_token.name == "CI token"
    assert issued.api_token.token_preview == issued.plain_token[:9]
    assert issued.api_token.scopes == ["mcp:read", "mcp:write"]

    with session_factory() as session:
        model = session.get(ApiToken, issued.api_token.id)
        assert model is not None
        assert model.token_preview == issued.plain_token[:9]
        assert model.token_preview != issued.plain_token
        assert issued.plain_token not in model.token_lookup_hash
        assert issued.plain_token not in model.hashed_token
        assert model.token_preview not in model.token_lookup_hash
        assert model.token_preview not in model.hashed_token
        assert model.token_lookup_hash.startswith("sha256:")
        assert model.hashed_token.startswith("$argon2id$")


def test_token_authenticates_updates_last_used_and_revoke_blocks_reuse(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="script",
        scopes=["mcp:read"],
    )

    actor = token_service.authenticate_token(issued.plain_token)

    assert actor.user == signed_up_user
    assert actor.token_id == issued.api_token.id
    assert actor.scopes == ("mcp:read",)
    assert token_service.list_tokens(signed_up_user.user_id)[0].last_used_at is not None

    revoked = token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    assert revoked.revoked_at is not None
    with pytest.raises(InvalidApiTokenError, match="revoked"):
        token_service.authenticate_token(issued.plain_token)


def test_token_mutation_queries_lock_rows_for_update():
    token_id = UUID("00000000-0000-0000-0000-000000000001")
    dialect = postgresql.dialect()

    by_id = str(
        api_token_by_id_for_update_statement(token_id).compile(dialect=dialect)
    )
    by_lookup = str(
        api_token_by_lookup_hash_for_update_statement("sha256:test").compile(
            dialect=dialect
        )
    )

    assert "FOR UPDATE" in by_id
    assert "FOR UPDATE" in by_lookup


def test_create_token_disambiguates_active_names_and_persists_mcp_client(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    first = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Claude Code MCP key",
        scopes=["mcp:read"],
        mcp_client_type="claude-code",
    )
    second = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Claude Code MCP key",
        scopes=["mcp:read"],
        mcp_client_type="codex",
    )

    assert first.api_token.name == "Claude Code MCP key"
    assert first.api_token.mcp_client_type == "claude-code"
    assert first.api_token.mcp_client_label == "Claude Code"
    assert first.api_token.catalog_app_id == "claude-code"
    assert first.api_token.catalog_app_label_snapshot == "Claude Code"
    assert first.api_token.catalog_revision == (
        get_mcp_app_catalog_repository().snapshot().catalog_revision
    )
    assert second.api_token.name == "Claude Code MCP key 2"
    assert second.api_token.mcp_client_type == "codex"
    assert second.api_token.mcp_client_label == "Codex CLI"
    assert second.api_token.catalog_app_id == "codex"
    assert second.api_token.catalog_app_label_snapshot == "Codex CLI"

    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=first.api_token.id,
    )
    reused = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Claude Code MCP key",
        scopes=["mcp:read"],
    )
    assert reused.api_token.name == "Claude Code MCP key"


def test_custom_access_key_has_null_catalog_association_and_label_snapshot(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Research client",
        scopes=["mcp:read"],
        custom_app_label="Research workstation",
    )

    assert issued.api_token.catalog_app_id is None
    assert issued.api_token.catalog_app_label_snapshot == "Research workstation"
    assert issued.api_token.catalog_revision == (
        get_mcp_app_catalog_repository().snapshot().catalog_revision
    )
    assert issued.api_token.mcp_client_type == "generic"
    assert issued.api_token.mcp_client_label == "Research workstation"


def test_catalog_association_is_display_only_for_authentication(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Codex key",
        scopes=["mcp:read"],
        catalog_app_id="codex",
    )

    class ExplodingCatalogRepository:
        def snapshot(self):
            raise AssertionError("authentication must not consult app catalog")

    authentication_service = replace(
        token_service,
        catalog_repository=ExplodingCatalogRepository(),
    )
    actor = authentication_service.authenticate_token(issued.plain_token)

    assert actor.token_id == issued.api_token.id
    assert actor.workspace_ids == (signed_up_workspace_id,)


def test_synthetic_catalog_app_can_be_associated_without_service_branch(
    tmp_path,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    source = Path(__file__).resolve().parents[2] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    app = json.loads(json.dumps(payload["apps"][1]))
    app.update(
        {
            "id": "synthetic-cli",
            "labels": {"en": "Synthetic CLI", "ko": "Synthetic CLI"},
            "aliases": ["fixture-access-key"],
            "featuredRank": None,
        }
    )
    for variant in app["variants"]:
        old_variant_id = variant["id"]
        new_variant_id = old_variant_id.replace("codex-cli", "synthetic-cli")
        old_recipe_id = variant["recipeIds"][0]
        new_recipe_id = old_recipe_id.replace("codex-cli", "synthetic-cli")
        variant["id"] = new_variant_id
        variant["recipeIds"] = [new_recipe_id]
        recipe = json.loads(
            json.dumps(
                next(
                    item
                    for item in payload["recipes"]
                    if item["id"] == old_recipe_id
                )
            )
        )
        recipe["id"] = new_recipe_id
        recipe["variantId"] = new_variant_id
        payload["recipes"].append(recipe)
    payload["apps"].append(app)
    payload["catalogRevision"] = "2026-08-04.2"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    service = replace(
        token_service,
        catalog_repository=CatalogRepository(target),
    )
    issued = service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Synthetic app key",
        scopes=["mcp:read"],
        catalog_app_id="synthetic-cli",
    )

    assert issued.api_token.catalog_app_id == "synthetic-cli"
    assert issued.api_token.catalog_app_label_snapshot == "Synthetic CLI"
    assert issued.api_token.catalog_revision == "2026-08-04.2"


def test_update_token_renames_and_disambiguates_active_names(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    first = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Laptop",
        scopes=["mcp:read"],
    )
    second = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Desktop",
        scopes=["mcp:read"],
    )

    updated = token_service.update_token(
        user_id=signed_up_user.user_id,
        token_id=second.api_token.id,
        name="Laptop",
    )

    assert updated.name == "Laptop 2"
    # Order-independent: the rename must be reflected in the list, but which row
    # is index 0 must not depend on a created_at tie order (merged I14).
    listed_names = {t.name for t in token_service.list_tokens(signed_up_user.user_id)}
    assert listed_names == {"Laptop", "Laptop 2"}

    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=first.api_token.id,
    )
    renamed_revoked = token_service.update_token(
        user_id=signed_up_user.user_id,
        token_id=first.api_token.id,
        name="Laptop 2",
    )
    assert renamed_revoked.name == "Laptop 2"


def test_update_token_rejects_an_empty_body(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="empty-patch",
        scopes=["mcp:read"],
    )

    with pytest.raises(
        InvalidApiTokenRequestError,
        match="at least one of name or workspace_grants",
    ):
        token_service.update_token(
            user_id=signed_up_user.user_id,
            token_id=issued.api_token.id,
        )


def test_invalid_scope_name_and_unknown_revoke_are_rejected(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with pytest.raises(InvalidApiTokenRequestError, match="unsupported"):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=_grant(signed_up_workspace_id),
            name="bad",
            scopes=["workspace-admin"],
        )

    with pytest.raises(InvalidApiTokenRequestError, match="unsupported MCP client"):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=_grant(signed_up_workspace_id),
            name="bad",
            scopes=["mcp:read"],
            mcp_client_type="unknown-client",
        )

    with pytest.raises(ApiTokenNotFoundError):
        token_service.revoke_token(
            user_id=signed_up_user.user_id,
            token_id=signed_up_user.user_id,
        )


def test_expired_token_is_rejected(
    signed_up_user, signed_up_workspace_id, token_service
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="expired",
        scopes=["mcp:read"],
        expires_at=utc_now() - timedelta(seconds=1),
    )

    with pytest.raises(InvalidApiTokenError, match="expired"):
        token_service.authenticate_token(issued.plain_token)


def test_wrong_token_value_is_rejected(
    signed_up_user, signed_up_workspace_id, token_service
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="valid",
        scopes=["mcp:read"],
    )
    wrong = issued.plain_token[:-4] + "abcd"

    with pytest.raises(InvalidApiTokenError):
        token_service.authenticate_token(wrong)
    with pytest.raises(InvalidApiTokenError):
        token_service.authenticate_token("not-prefixed")


def test_create_requires_explicit_workspace_and_role_compatible_scopes(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with pytest.raises(
        InvalidApiTokenRequestError, match="at least one workspace grant"
    ):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=[],
            name="unbound",
            scopes=["mcp:read"],
        )

    with session_factory.begin() as session:
        membership = session.get(
            WorkspaceMembership,
            (signed_up_user.user_id, signed_up_workspace_id),
        )
        assert membership is not None
        membership.role = "viewer"

    with pytest.raises(InvalidApiTokenRequestError, match="editor Workspace role"):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=_grant(signed_up_workspace_id),
            name="viewer write",
            scopes=["mcp:write"],
        )

    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="viewer read",
        scopes=["mcp:read"],
    )
    assert [grant.workspace_id for grant in issued.api_token.workspaces] == [
        signed_up_workspace_id
    ]


def test_create_token_rejects_duplicate_workspace_grants(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with pytest.raises(InvalidApiTokenRequestError, match="duplicate workspace_id"):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=[
                WorkspaceGrantRequest(workspace_id=signed_up_workspace_id),
                WorkspaceGrantRequest(workspace_id=signed_up_workspace_id),
            ],
            name="dup",
            scopes=["mcp:read"],
        )


def test_create_token_rejects_grant_scopes_wider_than_ceiling(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with pytest.raises(
        InvalidApiTokenRequestError, match="subset of the token's scopes"
    ):
        token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=_grant(signed_up_workspace_id, scopes=["mcp:write"]),
            name="widened",
            scopes=["mcp:read"],
        )


def test_create_token_accepts_multiple_workspace_grants_with_per_grant_scopes(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with session_factory.begin() as session:
        from backend.models.workspace import Workspace

        second_workspace = Workspace(
            slug="second-grant-workspace",
            title="Second Grant Workspace",
            name="Second Grant Workspace",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=signed_up_user.user_id,
            workspace_id=second_workspace.id,
            role="owner",
        )
        second_workspace_id = second_workspace.id

    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=[
            WorkspaceGrantRequest(workspace_id=signed_up_workspace_id),
            WorkspaceGrantRequest(
                workspace_id=second_workspace_id, scopes=["mcp:read"]
            ),
        ],
        name="multi-workspace",
        scopes=["mcp:read", "mcp:write"],
    )

    grants = {grant.workspace_id: grant for grant in issued.api_token.workspaces}
    assert set(grants) == {signed_up_workspace_id, second_workspace_id}
    assert grants[signed_up_workspace_id].granted_scopes == ["mcp:read", "mcp:write"]
    assert grants[second_workspace_id].granted_scopes == ["mcp:read"]
    assert grants[signed_up_workspace_id].membership_role == "owner"
    assert grants[second_workspace_id].membership_role == "owner"
    assert grants[signed_up_workspace_id].sync_state == "pending"

    with session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.created",
                AuditEvent.target_id == issued.api_token.id,
            )
        )
        assert event is not None
        assert event.workspace_id is None
        assert set(event.payload["workspace_ids"]) == {
            str(signed_up_workspace_id),
            str(second_workspace_id),
        }


def test_update_token_replaces_workspace_grants_and_audits_added_removed(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    with session_factory.begin() as session:
        from backend.models.workspace import Workspace

        second_workspace = Workspace(
            slug="second-update-workspace",
            title="Second Update Workspace",
            name="Second Update Workspace",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=signed_up_user.user_id,
            workspace_id=second_workspace.id,
            role="owner",
        )
        second_workspace_id = second_workspace.id

    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="grant-replace",
        scopes=["mcp:read"],
    )

    updated = token_service.update_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=second_workspace_id)],
    )

    assert [grant.workspace_id for grant in updated.workspaces] == [
        second_workspace_id
    ]

    with session_factory() as session:
        removed = session.get(
            ApiTokenWorkspace, (issued.api_token.id, signed_up_workspace_id)
        )
        assert removed is not None
        assert removed.removed_at is not None
        added = session.get(
            ApiTokenWorkspace, (issued.api_token.id, second_workspace_id)
        )
        assert added is not None
        assert added.removed_at is None
        assert added.revision == 1
        assert added.sync_state == "pending"

        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.workspaces_changed",
                AuditEvent.target_id == issued.api_token.id,
            )
        )
        assert event is not None
        assert event.payload == {
            "added": [str(second_workspace_id)],
            "removed": [str(signed_up_workspace_id)],
        }

    # A no-op replacement (same set, same scopes) must not re-fire the audit
    # event or bump the surviving row's revision.
    token_service.update_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=second_workspace_id)],
    )
    with session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.api_token.workspaces_changed",
                    AuditEvent.target_id == issued.api_token.id,
                )
            )
        )
        assert len(events) == 1
        unchanged = session.get(
            ApiTokenWorkspace, (issued.api_token.id, second_workspace_id)
        )
        assert unchanged.revision == 1


def test_authentication_reloads_live_workspace_membership_and_role(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    """D-01v2: ``authenticate_token`` only ever resolves the token's identity
    and its allowed-workspace SET -- it no longer confirms a role for any one
    workspace up front (that would be stale the moment membership changes
    mid-lifetime). Role/membership are resolved live, per request, by
    ``actor_with_target_workspace_scope`` -- this test exercises that real
    two-step production flow (``policy.auth._authenticate_api_token`` wires
    them together the same way) rather than asserting on ``authenticate_token``
    alone.
    """

    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="live role",
        scopes=["mcp:read", "mcp:write"],
    )

    with session_factory.begin() as session:
        membership = session.get(
            WorkspaceMembership,
            (signed_up_user.user_id, signed_up_workspace_id),
        )
        assert membership is not None
        membership.role = "viewer"

    primary = token_service.authenticate_token(issued.plain_token)
    assert primary.workspace_ids == (signed_up_workspace_id,)
    assert primary.scopes == ("mcp:read", "mcp:write")
    assert not hasattr(primary, "role")  # authenticate_token's job ends at identity

    bare_actor = _actor_from_user(
        user=primary.user,
        workspace_id=None,
        role=None,
        token_kind="api_token",
        token_id=primary.token_id,
        scopes=primary.scopes,
        token_workspace_ids=primary.workspace_ids,
    )
    with session_factory() as session:
        scoped = actor_with_target_workspace_scope(
            bare_actor, signed_up_workspace_id, session=session
        )
    assert scoped.role == "viewer"
    assert scoped.workspace_role == "viewer"
    assert scoped.scopes == ("mcp:read", "mcp:write")

    with session_factory.begin() as session:
        membership = session.get(
            WorkspaceMembership,
            (signed_up_user.user_id, signed_up_workspace_id),
        )
        assert membership is not None
        session.delete(membership)

    with session_factory() as session:
        with pytest.raises(PermissionError, match="workspace_membership_missing"):
            actor_with_target_workspace_scope(
                bare_actor, signed_up_workspace_id, session=session
            )


def test_legacy_unbound_token_fails_closed_on_authentication(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="legacy",
        scopes=["mcp:read"],
    )
    with session_factory.begin() as session:
        grant = session.get(
            ApiTokenWorkspace, (issued.api_token.id, signed_up_workspace_id)
        )
        assert grant is not None
        grant.removed_at = utc_now()

    with pytest.raises(InvalidApiTokenError, match="no allowed Workspace"):
        token_service.authenticate_token(issued.plain_token)


def test_user_list_uses_status_facets_and_query_bound_cursor(
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    now = utc_now()
    active_ids = []
    for name in ("Alpha one", "Alpha two", "Beta active"):
        issued = token_service.create_token(
            user_id=signed_up_user.user_id,
            workspace_grants=_grant(signed_up_workspace_id),
            name=name,
            scopes=["mcp:read"],
        )
        active_ids.append(issued.api_token.id)
    expired = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Alpha expired",
        scopes=["mcp:read"],
        expires_at=now - timedelta(seconds=1),
    )
    revoked = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="Alpha revoked and expired",
        scopes=["mcp:read"],
        expires_at=now - timedelta(seconds=1),
    )
    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=revoked.api_token.id,
    )

    first = token_service.list_user_tokens(
        user_id=signed_up_user.user_id,
        status="active",
        limit=2,
    )
    assert first.total_count == 5
    assert first.filtered_count == 3
    assert first.status_counts.active == 3
    assert first.status_counts.expired == 1
    assert first.status_counts.revoked == 1
    assert len(first.data) == 2
    assert first.next_cursor is not None
    assert all(item.status == "active" for item in first.data)

    second = token_service.list_user_tokens(
        user_id=signed_up_user.user_id,
        status="active",
        limit=2,
        cursor=first.next_cursor,
    )
    assert second.evaluated_at == first.evaluated_at
    assert len(second.data) == 1
    assert second.next_cursor is None
    assert {item.api_token.id for item in first.data + second.data} == set(active_ids)

    alpha_inactive = token_service.list_user_tokens(
        user_id=signed_up_user.user_id,
        status="inactive",
        q="  ALPHA   ",
    )
    assert alpha_inactive.total_count == 5
    assert alpha_inactive.filtered_count == 2
    assert alpha_inactive.status_counts.active == 2
    assert alpha_inactive.status_counts.expired == 1
    assert alpha_inactive.status_counts.revoked == 1
    assert {item.status for item in alpha_inactive.data} == {"expired", "revoked"}
    assert {item.api_token.id for item in alpha_inactive.data} == {
        expired.api_token.id,
        revoked.api_token.id,
    }

    with pytest.raises(InvalidApiTokenCursorError):
        token_service.list_user_tokens(
            user_id=signed_up_user.user_id,
            status="inactive",
            limit=2,
            cursor=first.next_cursor,
        )


def test_unique_active_token_name_is_scoped_to_user_not_workspace(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    """D-01v2/T4: a name colliding with another active PAT of the SAME user
    is disambiguated regardless of which workspace(s) either key names --
    the old ``(user, workspace)`` carve-out is gone."""

    with session_factory.begin() as session:
        from backend.models.workspace import Workspace

        other_workspace = Workspace(
            slug="name-uniqueness-workspace",
            title="Name Uniqueness Workspace",
            name="Name Uniqueness Workspace",
        )
        session.add(other_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=signed_up_user.user_id,
            workspace_id=other_workspace.id,
            role="owner",
        )
        other_workspace_id = other_workspace.id

    token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="shared name",
        scopes=["mcp:read"],
    )
    second = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(other_workspace_id),
        name="shared name",
        scopes=["mcp:read"],
    )

    assert second.api_token.name == "shared name 2"


def test_token_workspace_grant_shows_null_role_after_membership_is_lost(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    """D-01v2/T4: listing a user's own tokens is account-scoped -- losing
    membership in a granted workspace does not hide the token or the row,
    it just surfaces as ``membership_role: None`` for display (Settings can
    then show "membership lost" instead of erroring)."""

    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="membership removed",
        scopes=["mcp:read"],
    )
    with session_factory.begin() as session:
        membership = session.get(
            WorkspaceMembership,
            (signed_up_user.user_id, signed_up_workspace_id),
        )
        assert membership is not None
        session.delete(membership)

    listed = token_service.list_tokens(signed_up_user.user_id)
    row = next(item for item in listed if item.id == issued.api_token.id)
    assert [grant.workspace_id for grant in row.workspaces] == [
        signed_up_workspace_id
    ]
    assert row.workspaces[0].membership_role is None


def test_legacy_revoked_delete_requires_live_membership_and_preserves_record(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="legacy membership removed",
        scopes=["mcp:read"],
    )
    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    with session_factory.begin() as session:
        membership = session.get(
            WorkspaceMembership,
            (signed_up_user.user_id, signed_up_workspace_id),
        )
        assert membership is not None
        session.delete(membership)

    with pytest.raises(ApiTokenWorkspaceAccessError):
        token_service.delete_revoked_token(
            user_id=signed_up_user.user_id,
            token_id=issued.api_token.id,
        )

    with session_factory() as session:
        assert session.get(ApiToken, issued.api_token.id) is not None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == issued.api_token.id,
            )
        )
        assert event is None


def test_legacy_revoked_delete_is_owner_only_and_writes_secret_free_audit(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="legacy revoked owner only",
        scopes=["mcp:read"],
    )
    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    other = SessionService(
        session_factory=session_factory,
        credential_hasher=token_service.credential_hasher,
        token_codec=SessionTokenCodec(secret="test-session-secret"),
        signup_enabled=True,
    ).signup(email="other-legacy-key-member@example.com", password="owner-pass").user
    with session_factory.begin() as session:
        grant_workspace_membership(
            session,
            user_id=other.user_id,
            workspace_id=signed_up_workspace_id,
            role="viewer",
        )

    with pytest.raises(ApiTokenNotFoundError):
        token_service.delete_revoked_token(
            user_id=other.user_id,
            token_id=issued.api_token.id,
        )

    deleted_id = token_service.delete_revoked_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    assert deleted_id == issued.api_token.id

    with session_factory() as session:
        assert session.get(ApiToken, deleted_id) is None
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.api_token.record_deleted",
                    AuditEvent.target_id == deleted_id,
                )
            )
        )
        assert len(events) == 1
        event = events[0]
        assert event.workspace_id == signed_up_workspace_id
        assert event.actor_id == str(signed_up_user.user_id)
        assert event.payload == {
            "workspace_id": str(signed_up_workspace_id),
            "inactive_reason": "revoked",
        }
        serialized = str(event.payload)
        assert issued.plain_token not in serialized
        assert issued.api_token.token_preview not in serialized


def test_record_delete_accepts_expired_token_without_requiring_revoke_first(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    """T4: DELETE /auth/tokens/{id}/record is the only REST deletion path
    left after the workspace-scoped access-keys surface was removed, and
    must keep accepting an expired-but-not-yet-revoked token exactly as the
    deleted route did -- not narrow deletion to revoked-only."""
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="expired not revoked",
        scopes=["mcp:read"],
        expires_at=utc_now() - timedelta(seconds=1),
    )

    deleted_id = token_service.delete_revoked_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    assert deleted_id == issued.api_token.id

    with session_factory() as session:
        assert session.get(ApiToken, deleted_id) is None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == deleted_id,
            )
        )
        assert event is not None
        assert event.payload["inactive_reason"] == "expired"


def test_legacy_unbound_revoked_delete_fails_closed_without_audit(
    session_factory,
    signed_up_user,
    signed_up_workspace_id,
    token_service,
):
    issued = token_service.create_token(
        user_id=signed_up_user.user_id,
        workspace_grants=_grant(signed_up_workspace_id),
        name="legacy unbound delete",
        scopes=["mcp:read"],
    )
    token_service.revoke_token(
        user_id=signed_up_user.user_id,
        token_id=issued.api_token.id,
    )
    with session_factory.begin() as session:
        grant = session.get(
            ApiTokenWorkspace, (issued.api_token.id, signed_up_workspace_id)
        )
        assert grant is not None
        grant.removed_at = utc_now()

    with pytest.raises(InvalidApiTokenRequestError, match="Unbound legacy"):
        token_service.delete_revoked_token(
            user_id=signed_up_user.user_id,
            token_id=issued.api_token.id,
        )

    with session_factory() as session:
        assert session.get(ApiToken, issued.api_token.id) is not None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == issued.api_token.id,
            )
        )
        assert event is None

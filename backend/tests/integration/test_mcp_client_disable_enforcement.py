"""MCP-01: MCP client "Disable" must be a real credential boundary.

Settings > Access lets a workspace owner PATCH a connected MCP client's
status to "disabled" (``PATCH /workspaces/{id}/settings/mcp-clients/{id}``).
Before this fix, nothing on the bearer-verification path -- OAuth JWT
(``verify_access_token``) or PAT (``ApiTokenService.authenticate_token``) --
read ``MCPClient.status``, so a connection the owner believed was disabled
kept authenticating every call until its already-issued access/refresh
tokens (or the PAT itself) expired on their own.

Design note (team-lead review, superseding an earlier destructive version of
this fix): "disabled" is one of exactly two ``MCPClient.status`` values
(``MCP_CLIENT_STATUSES = {"active", "disabled"}`` -- there is no
"revoked" member), and the codebase already keeps derived connection health
separate from stored status (``derive_oauth_connection_state``'s docstring:
"without mutating MCPClient.status"). Revoking the underlying OAuth grant /
PAT on disable made the toggle lie in the OTHER direction after a
disable->enable cycle: status would read "active" while the connection was
actually dead. The fix instead enforces ``MCPClient.status`` at the single
seam both REST (``rest_scopes.scope_actor_to_request_workspace``) and MCP
(``mcp.router.resolve_target_actor``) already call once an OAuth/PAT bearer
actor's target workspace is known --
``backend.policy.rbac.require_workspace_role``. Disable is reversible: the
grant/PAT stays intact, so re-enabling the SAME MCPClient row restores
access with the SAME bearer token.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import select

from backend.models.api_token import ApiToken
from backend.models.oauth import OAuthClient
from backend.models.user import User
from backend.models.workspace import MCPClient
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import require_workspace_role
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.read_errors import RejectedByPolicyError
from backend.tests.integration.test_oauth_remote_hardening import (
    _current_workspace_id,
    _seed_oauth_tokens,
    make_client,
)


def _fast_hasher() -> CredentialHasher:
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


def _guide_call(client, token: str, workspace_id: str, request_id: int):
    """``guide`` requires the "viewer" role (TOOL_REQUIRED_ROLES), so it
    routes through resolve_target_actor -> require_workspace_role -- unlike
    list_my_workspaces, which has no required role and never reaches that
    seam."""

    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "guide",
                "arguments": {
                    "workspace_id": workspace_id,
                    "detail_level": "brief",
                },
            },
        },
    )


def test_disable_blocks_oauth_workspace_calls_without_revoking_the_grant(
    page_service,
):
    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="disable-oauth-AAA")
    workspace_id = _current_workspace_id(client)

    # A workspace-scoped call materializes the MCPClient trace row Settings >
    # Access shows, and confirms the bearer token authenticates before disable.
    before = _guide_call(client, tokens["access_token"], workspace_id, 1)
    assert before.status_code == 200, before.text
    before_envelope = before.json()["result"]["structuredContent"]["envelope"]
    assert before_envelope["result_status"] == "succeeded", before_envelope

    with page_service.session_factory() as session:
        mcp_client = session.scalar(
            select(MCPClient).where(
                MCPClient.workspace_id == UUID(workspace_id),
                MCPClient.client_type == "oauth",
            )
        )
        assert mcp_client is not None, "the MCP call must materialize a trace row"
        mcp_client_id = str(mcp_client.id)

    disabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{mcp_client_id}",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text

    # MCP-01: the same access JWT that worked a moment ago must now be
    # rejected for this workspace -- not merely display as "disabled" while
    # continuing to work.
    after = _guide_call(client, tokens["access_token"], workspace_id, 2)
    assert after.status_code == 200, after.text
    after_envelope = after.json()["result"]["structuredContent"]["envelope"]
    assert after_envelope["result_status"] == "rejected_by_policy", after_envelope
    assert after_envelope["error_code"] == "mcp_client_disabled", after_envelope

    # Non-destructive: refresh exchange still succeeds -- disable does not
    # touch the OAuth grant family.
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text

    # MCP-01: refresh minting a NEW access token must not be a back door.
    # The enforcement check re-derives from the bearer actor's client_id
    # (a server-signed JWT claim carried through refresh, not a cached
    # claim from issue time), so a token minted AFTER disable must be
    # rejected on its very first use -- not merely until the OLD token's
    # own claims are re-checked.
    new_access_token = refreshed.json()["access_token"]
    assert new_access_token != tokens["access_token"]
    after_refresh = _guide_call(client, new_access_token, workspace_id, 4)
    assert after_refresh.status_code == 200, after_refresh.text
    after_refresh_envelope = after_refresh.json()["result"]["structuredContent"][
        "envelope"
    ]
    assert (
        after_refresh_envelope["result_status"] == "rejected_by_policy"
    ), after_refresh_envelope
    assert (
        after_refresh_envelope["error_code"] == "mcp_client_disabled"
    ), after_refresh_envelope

    # Re-enable restores access with the ORIGINAL access token -- proving the
    # credential itself was never touched.
    enabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{mcp_client_id}",
        json={"status": "active"},
    )
    assert enabled.status_code == 200, enabled.text
    restored = _guide_call(client, tokens["access_token"], workspace_id, 3)
    assert restored.status_code == 200, restored.text
    restored_envelope = restored.json()["result"]["structuredContent"]["envelope"]
    assert restored_envelope["result_status"] == "succeeded", restored_envelope


def test_same_prefix_oauth_clients_keep_distinct_disable_boundaries(page_service):
    client = make_client(page_service)
    workspace_id = _current_workspace_id(client)
    client_id_a = "oauth-client-prefix-that-is-identical-alpha"
    client_id_b = "oauth-client-prefix-that-is-identical-bravo"
    assert client_id_a[:24] == client_id_b[:24]

    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        user_id = user.id
        for suffix, oauth_client_id in (("a", client_id_a), ("b", client_id_b)):
            session.add(
                OAuthClient(
                    id=oauth_client_id,
                    client_name=f"Collision client {suffix}",
                    redirect_uris=["http://127.0.0.1:53682/callback"],
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    application_type="native",
                    scope="mcp:read",
                    created_via="dcr",
                    registration_identity=f"collision-test-{suffix}",
                )
            )

        # This is the exact row shape/name written before durable names gained
        # their full-identity suffix. It must remain authoritative, including
        # its Settings status, rather than being shadowed by a replacement row.
        legacy_a = MCPClient(
            workspace_id=UUID(workspace_id),
            name=(f"OAuth client {client_id_a[:24]} (user {str(user_id)[:8]})"),
            client_type="oauth",
            scopes=["mcp:read"],
            status="active",
            created_by=user_id,
            issued_to_user_id=user_id,
            token_metadata={
                "token_kind": "oauth_jwt",
                "token_id": None,
                "oauth_client_id": client_id_a,
            },
        )
        session.add(legacy_a)
        session.flush()
        legacy_a_id = legacy_a.id

    tokens_a = _seed_oauth_tokens(
        client,
        state="same-prefix-oauth-a",
        client_id=client_id_a,
        workspace_id=workspace_id,
    )
    tokens_b = _seed_oauth_tokens(
        client,
        state="same-prefix-oauth-b",
        client_id=client_id_b,
        workspace_id=workspace_id,
    )

    assert (
        _guide_call(client, tokens_a["access_token"], workspace_id, 10).json()[
            "result"
        ]["structuredContent"]["envelope"]["result_status"]
        == "succeeded"
    )
    assert (
        _guide_call(client, tokens_b["access_token"], workspace_id, 11).json()[
            "result"
        ]["structuredContent"]["envelope"]["result_status"]
        == "succeeded"
    )

    with page_service.session_factory() as session:
        rows = session.scalars(
            select(MCPClient).where(
                MCPClient.workspace_id == UUID(workspace_id),
                MCPClient.client_type == "oauth",
                MCPClient.issued_to_user_id == user_id,
            )
        ).all()
        by_oauth_client_id = {
            (row.token_metadata or {}).get("oauth_client_id"): row for row in rows
        }
        row_a = by_oauth_client_id[client_id_a]
        row_b = by_oauth_client_id[client_id_b]
        assert row_a.id == legacy_a_id
        assert row_a.id != row_b.id
        assert row_a.name != row_b.name
        row_a_id = row_a.id
        row_b_id = row_b.id

    disabled_a = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{row_a_id}",
        json={"status": "disabled"},
    )
    assert disabled_a.status_code == 200, disabled_a.text
    blocked_a = _guide_call(client, tokens_a["access_token"], workspace_id, 12)
    allowed_b = _guide_call(client, tokens_b["access_token"], workspace_id, 13)
    blocked_a_envelope = blocked_a.json()["result"]["structuredContent"]["envelope"]
    assert blocked_a_envelope["result_status"] == "rejected_by_policy"
    assert blocked_a_envelope["error_code"] == "mcp_client_disabled"
    assert (
        allowed_b.json()["result"]["structuredContent"]["envelope"]["result_status"]
        == "succeeded"
    )

    enabled_a = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{row_a_id}",
        json={"status": "active"},
    )
    disabled_b = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{row_b_id}",
        json={"status": "disabled"},
    )
    assert enabled_a.status_code == 200, enabled_a.text
    assert disabled_b.status_code == 200, disabled_b.text
    allowed_a = _guide_call(client, tokens_a["access_token"], workspace_id, 14)
    blocked_b = _guide_call(client, tokens_b["access_token"], workspace_id, 15)
    assert (
        allowed_a.json()["result"]["structuredContent"]["envelope"]["result_status"]
        == "succeeded"
    )
    blocked_b_envelope = blocked_b.json()["result"]["structuredContent"]["envelope"]
    assert blocked_b_envelope["result_status"] == "rejected_by_policy"
    assert blocked_b_envelope["error_code"] == "mcp_client_disabled"


def test_rest_disable_gate_uses_exact_oauth_grant_binding(page_service):
    client = make_client(page_service)
    workspace_id = UUID(_current_workspace_id(client))
    old_grant_id = uuid4()
    new_grant_id = uuid4()
    # P10.3 GAP-2: seed and COMMIT in its own transaction, then reopen a
    # fresh session for the require_workspace_role() calls below. Both used
    # to share ONE still-open `session_factory.begin()` transaction with the
    # calls that read it -- harmless before this change (the only prior read
    # was a first-call-only, same-transaction MCPClient lookup), but
    # require_workspace_role's new grant-replica read (D-01v3-b) means the
    # FIRST call below now also opens its own workspace_session_factory()
    # session to read MCPClient there; on this fixture's shared-connection
    # StaticPool engine, closing that nested session rolled back the
    # OUTER session's still-pending uncommitted inserts (including this
    # test's own WorkspaceCredentialGrant seed) before the SECOND call ever
    # got to read it. Committing first removes the dependency on the
    # nesting order entirely.
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        user_id = user.id
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name="Old disabled OAuth grant",
                client_type="oauth",
                scopes=["mcp:read"],
                status="disabled",
                issued_to_user_id=user_id,
                runtime_binding_key=f"oauth_grant:{old_grant_id}",
                token_metadata={"oauth_client_id": "shared-public-client"},
            )
        )
        # require_workspace_role's grant-replica intersection (D-01v3-b)
        # needs a live workspace_credential_grant row for any oauth_jwt/pat
        # actor -- these two actors are hand-built below (never went through
        # real OAuth issuance/fan-out), so only new_grant_id (the one
        # expected to SUCCEED) gets one; old_grant_id is expected to fail on
        # the disabled-connection check first regardless (that check runs
        # before the grant-replica read in require_workspace_role, see its
        # own short-circuit), so it deliberately gets none.
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=new_grant_id,
                user_id=user_id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )

    def actor_for(grant_id):
        return ActorContext(
            actor_kind="human",
            actor_id=str(user_id),
            scopes=("mcp:read",),
            workspace_role="owner",
            user_id=user_id,
            workspace_id=workspace_id,
            role="owner",
            token_kind="oauth_jwt",
            token_id=grant_id,
            client_id="shared-public-client",
            token_workspace_ids=(workspace_id,),
            provenance="oauth_client",
        )

    with page_service.session_factory() as session:
        with pytest.raises(
            RejectedByPolicyError,
            match="disabled for this workspace",
        ):
            require_workspace_role(
                session,
                actor=actor_for(old_grant_id),
                workspace_id=workspace_id,
                required_role="viewer",
                workspace_session_factory=page_service.session_factory,
            )
        access = require_workspace_role(
            session,
            actor=actor_for(new_grant_id),
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=page_service.session_factory,
        )
        assert access.role == "owner"


def test_disable_blocks_pat_workspace_calls_without_revoking_the_token(
    page_service,
):
    client = make_client(page_service)
    workspace_id = _current_workspace_id(client)
    with page_service.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        user_id = user.id

    token_service = ApiTokenService(
        session_factory=page_service.session_factory,
        credential_hasher=_fast_hasher(),
    )
    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=UUID(workspace_id))],
        name="disable-test-pat",
        scopes=["mcp:read"],
    )
    plain_token = issued.plain_token
    token_id = issued.api_token.id

    # Mirrors the token_metadata shape resolve_trace_mcp_client would write
    # for a PAT-authenticated MCP call (backend/services/mcp_client_service.py).
    with page_service.session_factory.begin() as session:
        mcp_client = MCPClient(
            workspace_id=UUID(workspace_id),
            name=f"PAT trace ({str(token_id)[:8]})",
            client_type="api_token",
            scopes=["mcp:read"],
            status="active",
            issued_to_user_id=user_id,
            runtime_binding_key=f"api_token:{token_id}",
            token_metadata={
                "token_kind": "api_token",
                "token_id": str(token_id),
                "oauth_client_id": None,
            },
        )
        session.add(mcp_client)
        session.flush()
        mcp_client_id = str(mcp_client.id)

    # A {workspace_id}-path REST route: exercises the OTHER call site of the
    # same seam, rest_scopes.scope_actor_to_request_workspace (a route keyed
    # on a query string, like /api/v1/pages?workspace=..., never reaches this
    # check -- request.path_params has no "workspace_id" there).
    settings_url = f"/api/v1/workspaces/{workspace_id}/settings/policies"
    before = client.get(
        settings_url,
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert before.status_code == 200, before.text

    disabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{mcp_client_id}",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text

    # MCP-01: the same PAT that worked a moment ago must now be rejected for
    # this workspace.
    after = client.get(
        settings_url,
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert after.status_code == 403, after.text
    assert after.json()["error_code"] == "mcp_connection_disabled", after.text

    # Non-destructive: the PAT itself is never revoked.
    with page_service.session_factory() as session:
        api_token = session.get(ApiToken, token_id)
        assert api_token is not None
        assert api_token.revoked_at is None

    # Re-enable restores access with the ORIGINAL PAT.
    enabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{mcp_client_id}",
        json={"status": "active"},
    )
    assert enabled.status_code == 200, enabled.text
    restored = client.get(
        settings_url,
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert restored.status_code == 200, restored.text

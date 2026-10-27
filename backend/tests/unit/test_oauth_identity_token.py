"""End-to-end coverage for the allowed-workspace-SET OAuth access token.

D-01v2/D-01v3 (P10.3 T2/T3) replaces the prior D-01 single-workspace design
(an OAuth access token embedding the ONE real ``workspace_id`` claim it was
issued for, final and authoritative -- reaching a second workspace required
its own separate ``/oauth/authorize`` round that minted a separate token).

An OAuth access token now embeds NO workspace claim at all. A credential
(the (user, client) pair) instead carries an ALLOWED-WORKSPACE SET, held as
one ``oauth_consents`` row per allowed workspace (``removed_at IS NULL``),
read LIVE on every request by ``backend.policy.auth._authenticate_oauth_token``
into ``ActorContext.token_workspace_ids``. Reaching a workspace no longer
needs its own token: consenting to an additional workspace for the SAME
client (a second ``/oauth/authorize`` round, or a Settings ``PATCH
/oauth/consents`` workspace_ids expansion) makes the ALREADY-ISSUED token
reach it immediately, since the set is re-read from the database on every
call, not cached on the token. Losing a workspace (Settings removal, or a
membership revoke) takes effect on the very next call the same way.

These tests exercise the real service methods (register -> authorize ->
exchange -> verify -> check_actor_can_access), not mocks, because the whole
point is the interaction between oauth_service.py's issuance and
trust_boundaries.py's access check.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.oauth import OAuthConsent
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import grant_workspace_membership
from backend.policy.trust_boundaries import check_actor_can_access
from backend.services.oauth_service import OAuthService, challenge_s256

pytestmark = pytest.mark.unit

_VERIFIER = "v" * 64
_REDIRECT_URI = "http://127.0.0.1:53682/callback"
_SCOPE_PROBE_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c"
    "020000000b4944415478da6364f80f00010501012718e3660000000049454e44"
    "ae426082"
)


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
    )


def _seed_user_and_workspaces(session_factory, *, count: int = 2):
    with session_factory.begin() as session:
        user = User(
            email=f"{uuid4()}@example.com",
            password_hash="hash",
            display_name="User",
            status="active",
        )
        session.add(user)
        workspace_ids: list = []
        for index in range(count):
            label = chr(ord("a") + index)
            workspace = Workspace(
                slug=f"{label}-{uuid4().hex[:8]}",
                title=label.upper(),
                name=label.upper(),
                goal=label,
            )
            session.add(workspace)
            session.flush()
            grant_workspace_membership(
                session, user_id=user.id, workspace_id=workspace.id, role="owner"
            )
            # Normal signup provisions this row per workspace; manual seeding
            # here must too, or any Context media write (register()'s first
            # lock) fails closed with "workspace_media_intent_not_configured".
            session.add(WorkspaceMediaWriteIntent(workspace_id=workspace.id))
            workspace_ids.append(workspace.id)
        user_id = user.id
    return user_id, workspace_ids


def _register_and_authorize(
    service,
    *,
    user_id,
    workspace_ids,
    scope="mcp:read mcp:write",
):
    client = service.register_client(
        payload={
            "client_name": "Identity token test client",
            "redirect_uris": [_REDIRECT_URI],
            "scope": scope,
        },
        client_ip="127.0.0.1",
    )
    code = service.issue_authorization_code(
        user_id=user_id,
        client_id=client.client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=challenge_s256(_VERIFIER),
        code_challenge_method="S256",
        scope=scope,
        state="state-" + uuid4().hex,
        workspace_ids=list(workspace_ids),
    )
    result = service.exchange_authorization_code(
        code=code,
        client_id=client.client_id,
        redirect_uri=_REDIRECT_URI,
        code_verifier=_VERIFIER,
    )
    return client, result


def _consent_workspaces(service, client_id, *, user_id, workspace_ids, scope):
    """Run a SECOND /oauth/authorize round for an already-registered client,
    submitting the full desired workspace_ids set for this round (mirroring
    what the checklist screen would submit if the user (re)checked exactly
    these boxes -- issue_authorization_code is a full-set REPLACE of the
    selected round, section 2.3, so a caller that wants to ADD a workspace
    while KEEPING an existing one must re-list it here too)."""
    code = service.issue_authorization_code(
        user_id=user_id,
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=challenge_s256(_VERIFIER),
        code_challenge_method="S256",
        scope=scope,
        state="state-" + uuid4().hex,
        workspace_ids=list(workspace_ids),
    )
    service.exchange_authorization_code(
        code=code,
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_verifier=_VERIFIER,
    )


def _oauth_ctx_actor(actor, session_factory):
    """Build the policy-layer ActorContext for a verified OAuthAccessTokenActor.

    Mirrors what ``backend/policy/auth.py::_authenticate_oauth_token`` builds
    for a real request (P10.3 T3): the allowed-workspace SET is read LIVE
    from ``oauth_consents`` (``removed_at IS NULL``) for this (user, client)
    pair on every call -- the token itself carries no workspace claim any
    more, so there is nothing to mirror off the token.
    """
    from backend.policy.auth import ActorContext

    with session_factory() as session:
        token_workspace_ids = tuple(
            session.scalars(
                select(OAuthConsent.workspace_id)
                .where(
                    OAuthConsent.user_id == actor.user.user_id,
                    OAuthConsent.client_id == actor.client_id,
                    OAuthConsent.removed_at.is_(None),
                )
                .order_by(OAuthConsent.workspace_id)
            ).all()
        )

    return ActorContext(
        actor_kind="human",
        actor_id=str(actor.user.user_id),
        user_id=actor.user.user_id,
        client_id=actor.client_id,
        token_kind="oauth_jwt",
        scopes=actor.scopes,
        workspace_id=None,
        token_workspace_ids=token_workspace_ids,
    )


def test_access_token_carries_no_workspace_claim(page_service):
    """D-01v2/D-01v3: an OAuth access token embeds NO workspace claim at all
    -- OAuthAccessTokenActor has no workspace_id field (verify_access_token
    stopped requiring/parsing one; _issue_tokens stopped writing one)."""
    import dataclasses

    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    actor = service.verify_access_token(result.access_token)
    field_names = {field.name for field in dataclasses.fields(actor)}
    assert "workspace_id" not in field_names


def test_oauth_actor_live_consent_authorizes_its_allowed_workspace(page_service):
    """D-01v2/D-01v3: the credential's LIVE allowed-workspace set (read from
    oauth_consents at request time) authorizes a workspace it names -- no
    claim on the token itself is consulted, because there is none."""
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    actor = service.verify_access_token(result.access_token)
    ctx_actor = _oauth_ctx_actor(actor, page_service.session_factory)
    with page_service.session_factory() as session:
        check_actor_can_access(ctx_actor, workspace_a, "write", session=session)


def test_token_denied_for_a_workspace_it_is_not_bound_to(page_service):
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    actor = service.verify_access_token(result.access_token)
    ctx_actor = _oauth_ctx_actor(actor, page_service.session_factory)
    with page_service.session_factory() as session:
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(ctx_actor, workspace_b, "read", session=session)


def test_consenting_a_second_workspace_expands_the_same_tokens_live_reach(
    page_service,
):
    """D-01v2/D-01v3 inverts D-01's old step-up goal (this test replaces
    ``test_reaching_a_second_workspace_requires_its_own_authorize_round``,
    which asserted the now-removed "one token, one immutable workspace"
    design): there is no more per-token workspace claim to step up FROM, and
    no separate token to mint. Expanding the (user, client) consent group to
    include a second workspace -- via a second /oauth/authorize round for
    the SAME client -- makes the SAME, already-issued access token reach
    that workspace on its very next call, because the allowed set is read
    live from oauth_consents every time, never cached on the token."""
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    actor = service.verify_access_token(result.access_token)

    with page_service.session_factory() as session:
        ctx_actor = _oauth_ctx_actor(actor, page_service.session_factory)
        check_actor_can_access(ctx_actor, workspace_a, "write", session=session)
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(ctx_actor, workspace_b, "write", session=session)

    # A second authorize round for the SAME client, checklist now covering
    # both A (kept) and B (added) -- exactly what re-visiting the consent
    # screen with B additionally checked would submit.
    _consent_workspaces(
        service,
        client.client_id,
        user_id=user_id,
        workspace_ids=[workspace_a, workspace_b],
        scope="mcp:read mcp:write",
    )

    # No new token was minted -- re-derive the actor context (a fresh live
    # read, exactly what happens on the next real request) from the SAME
    # original access token.
    with page_service.session_factory() as session:
        ctx_actor_after = _oauth_ctx_actor(actor, page_service.session_factory)
        check_actor_can_access(ctx_actor_after, workspace_a, "write", session=session)
        check_actor_can_access(ctx_actor_after, workspace_b, "write", session=session)


def test_actor_workspace_filter_returns_the_live_allowed_set(page_service):
    """D-01v2/D-01v3: actor_workspace_filter reads the credential's LIVE
    allowed-workspace set (both A and B once the same client has been
    consented for both), not a single immutable token claim."""
    from backend.policy.trust_boundaries import actor_workspace_filter

    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b, workspace_c) = _seed_user_and_workspaces(
        page_service.session_factory, count=3
    )
    client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a, workspace_b]
    )

    actor = service.verify_access_token(result.access_token)
    ctx_actor = _oauth_ctx_actor(actor, page_service.session_factory)
    candidates = [
        (Workspace(id=workspace_a), None),
        (Workspace(id=workspace_b), None),
        (Workspace(id=workspace_c), None),
    ]

    filtered = actor_workspace_filter(ctx_actor, candidates)
    filtered_ids = {workspace.id for workspace, _role in filtered}

    assert filtered_ids == {workspace_a, workspace_b}


def _list_my_workspaces(client, token: str):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_my_workspaces", "arguments": {}},
        },
    )


def test_list_my_workspaces_oauth_stays_credential_scoped(page_service):
    """Real-Codex review finding (2026-09-06), still true under D-01v2:
    full-roster discovery is a PAT-only bootstrap affordance, not a
    pat/oauth_jwt-wide one. An OAuth access token's credential is the (user,
    client) pair's allowed-workspace SET (both A and B, both consented to the
    SAME client) -- a THIRD workspace the human separately manages via plain
    membership, with no consent for this client at all, is not named."""
    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b, _workspace_c) = _seed_user_and_workspaces(
        page_service.session_factory, count=3
    )
    _oauth_client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a, workspace_b]
    )

    response = _list_my_workspaces(client, result.access_token)

    assert response.status_code == 200
    envelope = response.json()["result"]["structuredContent"]
    assert envelope["envelope"]["result_status"] == "succeeded"
    listed_ids = {item["id"] for item in envelope["data"]["workspaces"]}
    assert listed_ids == {str(workspace_a), str(workspace_b)}
    assert envelope["data"]["other_membership_count"] == 1


def test_list_my_workspaces_pat_stays_credential_scoped(page_service):
    """D-01v2 (P10.3 T2, section 2.2) removed the D-01-era PAT bootstrap-
    discovery exception: a PAT now sees its credential's live allowed set
    (api_token_workspaces, removed_at IS NULL) intersected with membership,
    exactly like an OAuth actor -- not the user's full WorkspaceMembership
    roster. A membership outside that allowed set is not named, only
    counted (``other_membership_count``); expanding the allowed set to
    reach it is an explicit MCP Access UI action, not something this tool
    discovers for free."""
    from backend.services.api_token_service import (
        ApiTokenService,
        WorkspaceGrantRequest,
    )
    from backend.services.credential_hashing import CredentialHasher

    client = _make_client(page_service)
    user_id, (workspace_a, _workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    token = ApiTokenService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    ).create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_a)],
        name="pat-credential-scoped",
        scopes=["mcp:read"],
    ).plain_token

    response = _list_my_workspaces(client, token)

    assert response.status_code == 200
    envelope = response.json()["result"]["structuredContent"]
    assert envelope["envelope"]["result_status"] == "succeeded"
    listed_ids = {item["id"] for item in envelope["data"]["workspaces"]}
    assert listed_ids == {str(workspace_a)}
    assert envelope["data"]["other_membership_count"] == 1


def test_list_my_workspaces_oauth_hides_default_workspace_id_outside_roster(
    page_service,
):
    """A credential-scoped OAuth caller must not learn the id of the user's
    persisted default workspace when that workspace falls outside its own
    visible roster."""
    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        user.default_workspace_id = workspace_b
    _oauth_client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )

    response = _list_my_workspaces(client, result.access_token)

    assert response.status_code == 200
    data = response.json()["result"]["structuredContent"]["data"]
    assert {item["id"] for item in data["workspaces"]} == {str(workspace_a)}
    assert data["default_workspace_id"] is None


def test_revoke_consent_invalidates_the_access_token_immediately(page_service):
    """revoke_consent revokes the token itself (jti denylist; see
    ``OAuthService.revoke_consent``) AND deletes the whole (user, client)
    consent group -- either one alone would already deny the next call, but
    the token-level revoke is what makes a verify fail outright rather than
    a live-consent check downstream of it."""
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    oauth_client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    service.verify_access_token(result.access_token)

    assert service.revoke_consent(
        user_id=user_id,
        client_id=oauth_client.client_id,
    )

    from backend.services.oauth_service import InvalidOAuthGrantError

    with pytest.raises(InvalidOAuthGrantError):
        service.verify_access_token(result.access_token)


def _make_client(page_service) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        mcp_http_mode="json_stateless",
    )
    client = TestClient(create_app(settings=settings, page_service=page_service))
    # P10.3 T4.5/GAP-2: backend.app.container.DatabaseProvider.session_factory
    # is a cached_property that stamps session.info["database_provider"] onto
    # page_service.session_factory (the SAME sessionmaker object, mutated in
    # place) the first time anything touches it -- normally the app's first
    # real HTTP request. This file's tests construct an OAuthService directly
    # against page_service.session_factory (bypassing HTTP) to issue/exchange
    # BEFORE making their first real request through `client`, so without
    # this, credential_replica_service.register_fan_out silently no-ops for
    # every one of those direct calls (its own documented behavior for an
    # unstamped factory -- see its docstring), and a workspace granted that
    # way never gets a workspace_credential_grant replica. Force the stamp
    # here, eagerly, once, so the direct-service calls below see the exact
    # same fan-out behavior a real request would have triggered.
    from backend.app.container import DatabaseProvider

    provider = DatabaseProvider(None, session_factory=page_service.session_factory)
    _ = provider.session_factory
    return client


def _set_default_workspace(session_factory, *, user_id, workspace_id) -> None:
    with session_factory.begin() as session:
        session.get(User, user_id).default_workspace_id = workspace_id


def _guide_call(client: TestClient, token: str, workspace_id, *, headers=None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "guide",
                "arguments": {"workspace_id": str(workspace_id)},
            },
        },
    )


def test_oauth_bearer_reaches_mcp_over_http(page_service):
    """Regression for the 79420555..3ea8580d review's Critical #1: auth.py's
    ``_authenticate_oauth_token`` must call the REAL, committed
    ``OAuthService(session_factory=...)`` constructor. Every other test in
    this file constructs ``OAuthService`` directly and never touches
    ``authenticate_bearer_token`` at all, which is exactly how the original
    signature mismatch (``database_provider=`` against a session_factory-only
    constructor) went undetected -- this one goes through the real HTTP
    entry point."""
    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )

    response = _guide_call(client, result.access_token, workspace_a)

    assert response.status_code == 200
    envelope = response.json()["result"]["structuredContent"]["envelope"]
    assert envelope["result_status"] == "succeeded"


def test_pin_header_cannot_redirect_a_token_to_an_unrelated_default_workspace(
    page_service,
):
    """The X-Dtxwiki-Pin header is deleted outright (fully inert) -- it must
    not let a credential whose allowed set names only workspace B reach the
    user's own unrelated default workspace A just by naming A's slug."""
    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    # A is merely the user's own default workspace membership, unrelated to
    # this OAuth client/token, whose allowed set names only B.
    _set_default_workspace(
        page_service.session_factory, user_id=user_id, workspace_id=workspace_a
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_b]
    )
    with page_service.session_factory() as session:
        slug_a = session.get(Workspace, workspace_a).slug

    response = _guide_call(
        client,
        result.access_token,
        workspace_a,
        headers={"X-Dtxwiki-Pin": slug_a},
    )

    assert response.status_code == 400


def test_pin_header_has_no_effect_on_oauth_actor_routing(page_service):
    """DECISION ON THE RECORD: the X-Dtxwiki-Pin header is deleted outright,
    not preserved behind a flag. An oauth_jwt token routes by its
    credential's live allowed-workspace set alone; a pin header -- matching
    or not -- must not change the outcome."""
    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a, workspace_b) = _seed_user_and_workspaces(
        page_service.session_factory
    )
    _client, result = _register_and_authorize(
        service, user_id=user_id, workspace_ids=[workspace_a]
    )
    with page_service.session_factory() as session:
        slug_a = session.get(Workspace, workspace_a).slug

    allowed = _guide_call(
        client, result.access_token, workspace_a, headers={"X-Dtxwiki-Pin": slug_a}
    )
    blocked = _guide_call(
        client, result.access_token, workspace_b, headers={"X-Dtxwiki-Pin": slug_a}
    )

    assert allowed.status_code == 200
    assert blocked.status_code == 400


def _feedback_call(client: TestClient, token: str, workspace_id):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "submit_feedback",
                "arguments": {
                    "workspace_id": str(workspace_id),
                    "target_type": "answer",
                    "target_id": str(uuid4()),
                    "body": "Workspace-specific scope regression.",
                    "severity": "minor",
                    "conversation_token": "new",
                },
            },
        },
    )


def test_oauth_token_scope_narrows_to_the_live_consent_for_its_own_workspace(
    page_service,
):
    """``actor_with_target_workspace_scope`` still consults live consent for
    the target workspace: if a later grant for that SAME workspace narrows
    the consent, the already-issued access token's effective scope narrows
    to match, without the token itself being reissued."""

    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    oauth_client, result = _register_and_authorize(
        service,
        user_id=user_id,
        workspace_ids=[workspace_a],
        scope="mcp:read mcp:write",
    )

    allowed = _feedback_call(client, result.access_token, workspace_a)
    allowed_envelope = allowed.json()["result"]["structuredContent"]["envelope"]
    assert allowed_envelope["result_status"] == "succeeded"

    with page_service.session_factory.begin() as session:
        consent = session.get(
            OAuthConsent, (user_id, oauth_client.client_id, workspace_a)
        )
        consent.scope = "mcp:read"
        consent.desired_scopes = ["mcp:read"]

    denied = _feedback_call(client, result.access_token, workspace_a)
    denied_envelope = denied.json()["result"]["structuredContent"]["envelope"]
    assert denied_envelope["error_code"] == "scope_denied"


def test_rest_oauth_token_scope_narrows_to_the_live_consent_for_its_own_workspace(
    page_service,
):
    """The same live-consent narrowing must hold over REST, not just MCP: a
    stale wide token cannot keep writing after the user re-authorizes
    read-only for its own consented workspace
    (``scope_actor_to_request_workspace`` in backend/policy/rest_scopes.py
    calls the same ``actor_with_target_workspace_scope``)."""

    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    oauth_client, result = _register_and_authorize(
        service,
        user_id=user_id,
        workspace_ids=[workspace_a],
        scope="mcp:read mcp:write",
    )

    def submit_feedback():
        return client.post(
            f"/api/v1/workspaces/{workspace_a}/feedback",
            headers={"Authorization": f"Bearer {result.access_token}"},
            json={
                "target_type": "answer",
                "target_id": str(uuid4()),
                "body": "Workspace-specific REST scope regression.",
                "severity": "minor",
            },
        )

    allowed = submit_feedback()
    assert allowed.status_code == 200

    with page_service.session_factory.begin() as session:
        consent = session.get(
            OAuthConsent, (user_id, oauth_client.client_id, workspace_a)
        )
        consent.scope = "mcp:read"
        consent.desired_scopes = ["mcp:read"]

    denied = submit_feedback()
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"


def test_rest_oauth_scope_rechecks_the_workspace_replica_intersection(page_service):
    """A stale-narrow workspace replica must win over a wider control grant.

    The first REST scope check sees the control-plane consent. The workspace
    role check then narrows the actor to the replica that actually landed on
    the target plane, so the route capability must be checked again against
    that rebound actor before the handler runs.
    """

    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    _oauth_client, result = _register_and_authorize(
        service,
        user_id=user_id,
        workspace_ids=[workspace_a],
        scope="mcp:read mcp:write mcp:agent",
    )

    with page_service.session_factory.begin() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_a,
                WorkspaceCredentialGrant.credential_kind == "oauth",
                WorkspaceCredentialGrant.user_id == user_id,
                WorkspaceCredentialGrant.revoked_at.is_(None),
            )
        )
        assert replica is not None
        replica.granted_scopes = ["mcp:read", "mcp:write"]

    denied = client.post(
        f"/api/v1/workspaces/{workspace_a}/agent/pages",
        headers={"Authorization": f"Bearer {result.access_token}"},
        json={
            "category": "persona",
            "title": "Replica scope regression",
            "slug": f"replica-scope-{uuid4().hex[:8]}",
            "body_markdown": "# Workspace replica scope regression",
            "reason": "prove the final REST capability gate",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"


def test_late_resolved_rest_routes_recheck_the_target_exact_scope(page_service):
    """Slug/body, page-id, and page-media-upload routes recheck LIVE consent
    scope for the token's target workspace, not just the token's ceiling
    scope claim frozen at issuance."""

    client = _make_client(page_service)
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=_settings(),
        public_base_url="http://testserver",
    )
    user_id, (workspace_a,) = _seed_user_and_workspaces(
        page_service.session_factory, count=1
    )
    oauth_client, result = _register_and_authorize(
        service,
        user_id=user_id,
        workspace_ids=[workspace_a],
        scope="mcp:write mcp:agent",
    )
    with page_service.session_factory.begin() as session:
        workspace_a_slug = session.get(Workspace, workspace_a).slug
        session.add(
            WorkspaceTreeNode(
                workspace_id=workspace_a,
                section="context",
                node_kind="folder",
                parent_id=None,
                sort_key="0001",
                title="Context",
                slug="context",
                is_system_node=True,
                can_have_children=True,
                primary_placement=True,
                visibility_status="visible",
                created_by_actor_type="system",
                created_by_actor_id=user_id,
            )
        )
    seeded = page_service.write_page(
        workspace_slug=workspace_a_slug,
        slug="exact-scope-seeded",
        title="Exact scope seeded",
        body_md="# Exact scope seeded",
        actor_kind="human",
        actor_id=str(user_id),
        actor_provenance="oauth_client",
    )
    # A separate page-first page just for the media-upload probe below: it
    # only needs a valid, open editing_version_id in this same workspace, not
    # one belonging to `seeded` specifically (which the read/write scope
    # checks above use instead).
    editing = page_service.create_page_first(
        workspace_slug=workspace_a_slug,
        actor_kind="human",
        actor_id=str(user_id),
        actor_provenance="oauth_client",
    )

    # Narrow live consent to a scope this token's OWN claim ("mcp:write
    # mcp:agent") does contain, so the intersection is non-empty ({"mcp:agent"})
    # and the denial below is a genuine "insufficient tier" (mcp:agent alone
    # satisfies neither "read" nor "draft-write"), not a vacuous empty-set one.
    with page_service.session_factory.begin() as session:
        consent = session.get(
            OAuthConsent, (user_id, oauth_client.client_id, workspace_a)
        )
        consent.scope = "mcp:agent"
        consent.desired_scopes = ["mcp:agent"]

    pages_url = f"/api/v1/workspaces/{workspace_a}/context/pages"
    headers = {"Authorization": f"Bearer {result.access_token}"}
    denied_read = client.get(f"{pages_url}/{seeded.id}", headers=headers)
    denied_write = client.post(
        pages_url,
        headers=headers,
        json={"title": "Exact scope created", "body_md": "# Exact scope created"},
    )
    denied_media_session = client.post(
        f"{pages_url}/{editing.id}/media",
        headers=headers,
        data={
            "marker_uuid": str(uuid4()),
            "editing_version_id": str(editing.version_id),
        },
        files={"file": ("scope-probe.png", _SCOPE_PROBE_PNG_BYTES, "image/png")},
    )

    with page_service.session_factory.begin() as session:
        consent = session.get(
            OAuthConsent, (user_id, oauth_client.client_id, workspace_a)
        )
        consent.scope = "mcp:write"
        consent.desired_scopes = ["mcp:write"]

    allowed_read = client.get(f"{pages_url}/{seeded.id}", headers=headers)
    allowed_media_session = client.post(
        f"{pages_url}/{editing.id}/media",
        headers=headers,
        data={
            "marker_uuid": str(uuid4()),
            "editing_version_id": str(editing.version_id),
        },
        files={"file": ("scope-probe.png", _SCOPE_PROBE_PNG_BYTES, "image/png")},
    )

    assert denied_read.status_code == 403
    assert denied_read.json()["error_code"] == "scope_denied"
    assert denied_write.status_code == 403
    assert denied_write.json()["error_code"] == "scope_denied"
    assert denied_media_session.status_code == 403
    assert denied_media_session.json()["error_code"] == "scope_denied"
    assert allowed_read.status_code == 200
    assert allowed_media_session.status_code == 200


# The former test_pin_narrows_scope_to_the_pinned_workspaces_own_consent,
# test_pin_scope_gate_uses_the_pinned_workspaces_consent_scope, and
# test_pin_denies_a_write_scoped_tool_on_a_read_only_consented_workspace all
# tested "a pin narrows OAuth scope to the pinned workspace's own consent" --
# a mechanism that no longer exists at all now that the pin header itself is
# deleted outright. Their equivalent invariant under the new design ("the
# pin header has zero effect") is already covered by
# test_pin_header_has_no_effect_on_oauth_actor_routing above; repeating it
# three more times with different tool names would be duplicate scaffolding
# for a removed feature, not additional coverage.

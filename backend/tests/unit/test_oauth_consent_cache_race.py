"""D-01: OAuth consent authorization is a direct, uncached read.

This file used to test a process-local, TTL-cached positive-consent lookup
plus a live "consent directory" (multi-workspace enumeration) against a
synthetic slow-reader race: a store landing after an explicit invalidation
call had to be rejected by an epoch guard. Both the cache and the directory
are deleted outright -- every OAuth access token now embeds the one real
workspace it was issued for, so ``oauth_consented_workspace_scope`` is a
single uncached row read per call (see its docstring in
``backend/policy/trust_boundaries.py``), which is what actually closes the
revoke-then-stale-read race (F-15) this file used to guard with cache-
invalidation timing tricks.

With no cache in the loop there is nothing left to race: the invariant
collapses to "a revoked consent is invisible on the very next read," which a
synchronous test can assert directly without threads.
"""

from uuid import uuid4

from backend.models.base import utc_now
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy import trust_boundaries
from backend.policy.rbac import grant_workspace_membership


def test_desired_scope_and_soft_revoke_are_live_on_the_very_next_read(
    session_factory,
):
    client_id = f"consent-race-client-{uuid4().hex[:8]}"
    with session_factory.begin() as session:
        user = User(
            email=f"{uuid4()}@example.com", password_hash="hash", display_name="User"
        )
        workspace = Workspace(
            slug=f"consent-race-{uuid4().hex[:8]}",
            title="Consent Race",
            name="Consent Race",
            goal="consent race",
        )
        session.add_all([user, workspace])
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=workspace.id, role="editor"
        )
        session.add(
            OAuthClient(
                id=client_id,
                client_name="Consent race client",
                redirect_uris=["http://127.0.0.1:53682/callback"],
                grant_types=["authorization_code"],
                response_types=["code"],
                scope="mcp:read mcp:write",
            )
        )
        session.flush()
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=client_id,
                workspace_id=workspace.id,
                scope="mcp:read mcp:write",
                desired_scopes=["mcp:read"],
            )
        )
        session.flush()
        user_id, workspace_id = user.id, workspace.id

    with session_factory.begin() as session:
        assert trust_boundaries.oauth_consented_workspace_scope(
            session, user_id=user_id, client_id=client_id, workspace_id=workspace_id
        ) == ("mcp:read",)

        consent = session.get(OAuthConsent, (user_id, client_id, workspace_id))
        assert consent is not None
        consent.removed_at = utc_now()
        session.flush()

        # No cache to keep serving the soft-removed row -- denied immediately,
        # in the same transaction, with no TTL lag.
        assert (
            trust_boundaries.oauth_consented_workspace_scope(
                session,
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
            )
            is None
        )

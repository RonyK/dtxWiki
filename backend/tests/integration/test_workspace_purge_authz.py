"""PURGE_AUTHZ_PLAN Section 5: end-to-end authorization tests for workspace
deletion/purge, run through the real HTTP/auth stack.

``backend/tests/integration/test_workspace_purge.py`` proves the 80-table
purge itself is complete by calling ``WorkspaceWriteService`` directly. That
never exercises ``REST_SESSION_ONLY_ROUTES``, the bearer-scope gate, or the
purge-grant issue/consume cycle -- this file is the part of the plan's own
motivation ("that destructive path has never been verified through the real
HTTP/auth stack") those service-level tests structurally cannot cover.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.models.workspace_purge_grant import WorkspacePurgeGrant
from backend.policy.rbac import grant_workspace_membership
from backend.services.credential_hashing import CredentialHasher
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.workspace_purge_grant import issue_purge_grant
from backend.services.workspace_write import WorkspaceWriteService
from backend.tests.conftest import authenticate_test_client


def _client(page_service, email: str) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="workspace-purge-authz-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client, email=email)
    return client


def _user_id(page_service, email: str) -> UUID:
    with page_service.session_factory() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.id


def _workspace_with_blocker(client: TestClient, title: str) -> str:
    created = client.post("/api/v1/workspaces", json={"title": title}).json()["data"]
    workspace_id = created["id"]
    page = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={"title": "Keep Me", "body_md": "# Keep Me"},
    )
    assert page.status_code == 201
    return workspace_id


def _issue_grant(client: TestClient, workspace_id: str) -> str:
    refused = client.delete(f"/api/v1/workspaces/{workspace_id}")
    assert refused.status_code == 409, refused.text
    grant = refused.json()["purge_grant"]
    assert grant.startswith("wpg_")
    return grant


def _seed_workspace_admin_pat(page_service, *, email: str, workspace_id: str) -> str:
    """A PAT carrying the literal ``workspace-admin`` scope.

    Neither ``POST /api/v1/auth/tokens`` nor the ``users issue-pat`` CLI can
    mint this: both validate requested scopes against
    ``ALLOWED_API_TOKEN_SCOPES`` (``mcp:read``/``mcp:write``/``mcp:agent``
    only). T-1's whole point is that S-1 must hold regardless of how a token
    came to carry this scope -- a legacy credential, a future issuance
    surface, direct DB repair -- so this constructs the row directly, the way
    the authorization CHECK itself sees no difference from a "real" token.

    Also carries ``mcp:write``: the fixture's own "this PAT really is
    workspace-admin, not just a token nobody would issue" proof step (a PATCH
    against a DIFFERENT workspace-admin route) needs it to clear a second,
    independent gate -- ``trust_boundaries._check_scope`` maps a pat/oauth_jwt
    actor's role-derived action to hardcoded ``mcp:*`` umbrella scopes only
    and has no notion of ``workspace-admin`` at all. That gate sits entirely
    below the bearer-scope registry this plan's S-1 changes, so it does not
    affect what T-1 itself is proving.
    """

    hasher = CredentialHasher()
    plain_token = hasher.generate_api_token()
    scopes = ["workspace-admin", "mcp:write"]
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        token = ApiToken(
            user_id=user.id,
            name="legacy workspace-admin PAT",
            token_lookup_hash=hasher.token_lookup_hash(plain_token),
            hashed_token=hasher.hash_api_token(plain_token),
            scopes=scopes,
        )
        session.add(token)
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=token.id,
                workspace_id=UUID(workspace_id),
                desired_scopes=scopes,
                revision=1,
                sync_state="synced",
            )
        )
        # The workspace-plane replica D-01v3-b's grant-replica intersection
        # requires -- without it every OTHER workspace-admin route this PAT
        # tries would 503 grant_replica_missing before ever reaching a scope
        # decision, which would make the fixture's own "this PAT really does
        # carry workspace-admin" proof step meaningless.
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=UUID(workspace_id),
                credential_kind="pat",
                credential_id=token.id,
                user_id=user.id,
                granted_scopes=scopes,
                synced_revision=1,
            )
        )
    return plain_token


def test_workspace_admin_pat_cannot_purge_delete(page_service):
    """T-1: S-1's core claim. The message assertion is load-bearing -- a PAT
    with NO scope at all gets the same 403 scope_denied from a different
    check ("insufficient token scope"), so without pinning the message this
    test would still pass against a route that only forgot to register the
    scope, not one that is genuinely session-only.
    """

    owner_email = "purge-t1-owner@example.com"
    owner = _client(page_service, owner_email)
    workspace_id = _workspace_with_blocker(owner, "T1 Workspace")
    token = _seed_workspace_admin_pat(
        page_service, email=owner_email, workspace_id=workspace_id
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Fixture proof: this PAT's workspace-admin scope is real and reaches
    # another workspace-admin route (not merely a token nobody would issue).
    # The revision lookup itself needs "workspace-read", not "workspace-admin"
    # (a different scope), so it goes through the owner's own session --
    # only the PATCH below, the thing actually under test, uses the PAT.
    policies = owner.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    assert policies.status_code == 200, policies.text
    revision = policies.json()["data"]["library"]["revision"]
    proof = owner.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": revision,
            "changes": {"max_upload_bytes": 999_000},
        },
        headers=headers,
    )
    assert proof.status_code == 200, proof.text

    denied = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true", headers=headers
    )
    assert denied.status_code == 403
    body = denied.json()
    assert body["error_code"] == "scope_denied"
    assert "requires a user session" in body["message"]


def test_repeated_refusal_reissues_the_grant_without_double_counting(page_service):
    """T-2: two consecutive un-purged DELETEs both 409 with the same
    blockers (the grant row itself is not one of them) and each mints its
    own grant; the earlier one stops working once the later one lands
    (S-5's upsert -- at most one live grant per (user, workspace))."""

    owner = _client(page_service, "purge-t2@example.com")
    workspace_id = _workspace_with_blocker(owner, "T2 Workspace")

    first = owner.delete(f"/api/v1/workspaces/{workspace_id}")
    assert first.status_code == 409
    first_body = first.json()

    second = owner.delete(f"/api/v1/workspaces/{workspace_id}")
    assert second.status_code == 409
    second_body = second.json()

    assert second_body["blockers"] == first_body["blockers"]
    assert first_body["purge_grant"] != second_body["purge_grant"]

    stale = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": first_body["purge_grant"]},
    )
    assert stale.status_code == 403
    assert stale.json()["error_code"] == "purge_grant_required"


@pytest.mark.parametrize(
    ("late_outcome", "expected_status", "expected_error_code"),
    [
        ("workspace_deleted", 404, "not_found"),
        ("owner_demoted", 403, "workspace_role_insufficient"),
    ],
)
def test_refusal_revalidates_authorization_before_issuing_a_grant(
    page_service,
    monkeypatch,
    late_outcome,
    expected_status,
    expected_error_code,
):
    """A stale blocker refusal must not mint a grant for a changed target."""

    owner_email = f"purge-revalidate-{late_outcome}@example.com"
    owner = _client(page_service, owner_email)
    owner_id = _user_id(page_service, owner_email)
    workspace_id = _workspace_with_blocker(owner, f"Revalidate {late_outcome}")

    original = WorkspaceWriteService.require_delete_authorization
    authorization_calls = 0

    def fail_post_rollback_revalidation(*, session, workspace_id, actor):
        nonlocal authorization_calls
        authorization_calls += 1
        if authorization_calls == 2:
            if late_outcome == "workspace_deleted":
                raise ReadNotFoundError("workspace not found")
            raise RejectedByPolicyError(
                "an active workspace owner is required",
                error_code="workspace_role_insufficient",
            )
        return original(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )

    monkeypatch.setattr(
        WorkspaceWriteService,
        "require_delete_authorization",
        staticmethod(fail_post_rollback_revalidation),
    )

    response = owner.delete(f"/api/v1/workspaces/{workspace_id}")

    assert response.status_code == expected_status, response.text
    assert response.json()["error_code"] == expected_error_code
    assert authorization_calls == 2
    with page_service.session_factory() as session:
        assert (
            session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id))) is None
        )


def test_purge_without_a_grant_is_refused_and_touches_no_grant_row(page_service):
    """T-3: the forcing point -- no grant was ever issued for this
    workspace, so the missing-header case must 403, and the grant table for
    this workspace must remain empty (nothing to create or consume)."""

    owner = _client(page_service, "purge-t3@example.com")
    owner_id = _user_id(page_service, "purge-t3@example.com")
    workspace_id = _workspace_with_blocker(owner, "T3 Workspace")

    with page_service.session_factory() as session:
        before = session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id)))
    assert before is None

    denied = owner.delete(f"/api/v1/workspaces/{workspace_id}?purge=true")
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "purge_grant_required"

    still_there = owner.get(f"/api/v1/workspaces/{workspace_id}")
    assert still_there.status_code == 200
    with page_service.session_factory() as session:
        after = session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id)))
    assert after is None


def test_valid_grant_purges_the_workspace_end_to_end(page_service):
    """T-4: the only end-to-end verification of the purge path through the
    real HTTP/auth stack -- a route that purged before consuming the grant
    (S-4's ordering) would 403 here instead of returning 200."""

    owner = _client(page_service, "purge-t4@example.com")
    owner_id = _user_id(page_service, "purge-t4@example.com")
    workspace_id = _workspace_with_blocker(owner, "T4 Workspace")
    grant = _issue_grant(owner, workspace_id)

    purged = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": grant},
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["data"]["deleted_workspace_id"] == workspace_id

    gone = owner.get(f"/api/v1/workspaces/{workspace_id}")
    assert gone.status_code == 404
    with page_service.session_factory() as session:
        assert session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id))) is None


def test_a_grant_already_consumed_once_cannot_be_reused(page_service):
    """T-5: single use, via the conditional-DELETE rowcount path. A second
    purge attempt destroys the workspace, so a literal second HTTP call
    would 404 (workspace gone) rather than exercise this specifically --
    instead, this simulates "already consumed once" the same way the route
    itself would (one call to ``consume_purge_grant``), then proves the
    route's OWN attempt at the same grant, against the still-live
    workspace, is refused."""

    from backend.services.workspace_purge_grant import consume_purge_grant

    owner = _client(page_service, "purge-t5@example.com")
    owner_id = _user_id(page_service, "purge-t5@example.com")
    workspace_id = _workspace_with_blocker(owner, "T5 Workspace")
    grant = _issue_grant(owner, workspace_id)

    with page_service.session_factory.begin() as session:
        used = consume_purge_grant(
            session,
            user_id=owner_id,
            workspace_id=UUID(workspace_id),
            token=grant,
        )
    assert used is True

    reused = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": grant},
    )
    assert reused.status_code == 403
    assert reused.json()["error_code"] == "purge_grant_required"
    still_there = owner.get(f"/api/v1/workspaces/{workspace_id}")
    assert still_there.status_code == 200


def test_an_expired_grant_is_refused(page_service, monkeypatch):
    """T-6: expiry is the conditional DELETE's own ``expires_at > now``
    clause, not a separate check -- backdate the row directly rather than
    waiting out the real 5-minute TTL.

    The grant is backdated one second before a pinned "now", and the DELETE's
    clause is judged against that same "now". Backdating against one read of
    the host clock and judging against another lets a clock step (NTP, a
    resumed VM) put the grant on either side of its own deadline."""

    now = utc_now()
    monkeypatch.setattr("backend.services.workspace_purge_grant.utc_now", lambda: now)
    owner = _client(page_service, "purge-t6@example.com")
    owner_id = _user_id(page_service, "purge-t6@example.com")
    workspace_id = _workspace_with_blocker(owner, "T6 Workspace")
    grant = _issue_grant(owner, workspace_id)

    with page_service.session_factory.begin() as session:
        row = session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id)))
        assert row is not None
        row.expires_at = now - timedelta(seconds=1)

    expired = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": grant},
    )
    assert expired.status_code == 403
    assert expired.json()["error_code"] == "purge_grant_required"


def test_grant_binding_mismatches_and_malformed_tokens_are_refused_uniformly(
    page_service,
):
    """T-7: (a) right user, wrong workspace; (b) right workspace, wrong
    user; (c) a malformed token string. S-8 requires all three, and a
    missing/expired/reused grant, to be indistinguishable to the caller."""

    owner_email = "purge-t7-owner@example.com"
    other_email = "purge-t7-other@example.com"
    owner = _client(page_service, owner_email)
    other = _client(page_service, other_email)

    workspace_a = _workspace_with_blocker(owner, "T7 Workspace A")
    workspace_b = _workspace_with_blocker(owner, "T7 Workspace B")
    grant_for_a = _issue_grant(owner, workspace_a)

    # (a) A's grant used against B.
    wrong_workspace = owner.delete(
        f"/api/v1/workspaces/{workspace_b}?purge=true",
        headers={"X-Purge-Grant": grant_for_a},
    )

    # (b) owner's grant (for A) used by a different user who is also an
    # owner of A.
    grant_for_owner_a = _issue_grant(owner, workspace_a)
    other_id = _user_id(page_service, other_email)
    with page_service.session_factory.begin() as session:
        grant_workspace_membership(
            session, user_id=other_id, workspace_id=UUID(workspace_a), role="owner"
        )
    wrong_user = other.delete(
        f"/api/v1/workspaces/{workspace_a}?purge=true",
        headers={"X-Purge-Grant": grant_for_owner_a},
    )

    # (c) malformed token string (wrong prefix entirely).
    malformed = owner.delete(
        f"/api/v1/workspaces/{workspace_a}?purge=true",
        headers={"X-Purge-Grant": "not-a-real-grant"},
    )

    for response in (wrong_workspace, wrong_user, malformed):
        assert response.status_code == 403
        assert response.json()["error_code"] == "purge_grant_required"
    bodies = [response.json() for response in (wrong_workspace, wrong_user, malformed)]
    assert bodies[0]["message"] == bodies[1]["message"] == bodies[2]["message"]


def test_a_purge_failure_rolls_back_grant_consumption_so_a_retry_can_succeed(
    page_service, monkeypatch
):
    """T-8: purge and grant-consumption share one transaction. A mid-purge
    exception must roll back BOTH, so the grant the client is still holding
    remains usable for a retry -- otherwise a transient failure would
    permanently strand the workspace with no way to complete deletion
    (the retry would always hit T-3's "no grant" refusal)."""

    owner = _client(page_service, "purge-t8@example.com")
    workspace_id = _workspace_with_blocker(owner, "T8 Workspace")
    grant = _issue_grant(owner, workspace_id)

    from backend.services.workspace_purge import purge_workspace_data as real_purge

    calls = {"count": 0}

    def flaky_purge(session, target_workspace_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated purge failure")
        return real_purge(session, target_workspace_id)

    monkeypatch.setattr(
        "backend.services.workspace_write.purge_workspace_data", flaky_purge
    )

    with pytest.raises(RuntimeError, match="simulated purge failure"):
        owner.delete(
            f"/api/v1/workspaces/{workspace_id}?purge=true",
            headers={"X-Purge-Grant": grant},
        )
    assert calls["count"] == 1

    still_there = owner.get(f"/api/v1/workspaces/{workspace_id}")
    assert still_there.status_code == 200

    retried = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": grant},
    )
    assert retried.status_code == 200, retried.text
    assert calls["count"] == 2


def test_purge_workspace_data_runs_only_after_the_grant_row_is_gone(
    page_service, monkeypatch
):
    """T-9: pins the ORDER T-8 cannot -- the grant's conditional DELETE must
    execute before ``purge_workspace_data`` is even called, in the same
    session. Confirmed by reverting the order during implementation: since
    ``workspace_purge_grant`` is itself workspace-plane and purged along with
    everything else, purging first makes the grant row gone by the time
    consumption is attempted, so a reversed implementation gets 403
    ``purge_grant_required`` on what should be a normal, valid purge -- not
    merely a failed ``observed`` assertion, but the request failing outright.
    """

    owner = _client(page_service, "purge-t9@example.com")
    owner_id = _user_id(page_service, "purge-t9@example.com")
    workspace_id = _workspace_with_blocker(owner, "T9 Workspace")
    grant = _issue_grant(owner, workspace_id)

    from backend.services.workspace_purge import purge_workspace_data as real_purge

    observed = {}

    def wrapped_purge(session, target_workspace_id):
        observed["grant_already_gone"] = (
            session.get(WorkspacePurgeGrant, (owner_id, UUID(workspace_id))) is None
        )
        return real_purge(session, target_workspace_id)

    monkeypatch.setattr(
        "backend.services.workspace_write.purge_workspace_data", wrapped_purge
    )

    response = owner.delete(
        f"/api/v1/workspaces/{workspace_id}?purge=true",
        headers={"X-Purge-Grant": grant},
    )
    assert response.status_code == 200, response.text
    assert observed["grant_already_gone"] is True


def test_reissuing_a_grant_is_one_conflict_tolerant_statement(page_service):
    """Issuing must not read the row and then decide whether to insert it.

    Two refusals for the same (user, workspace) can overlap. Under
    read-then-write both observe no row, both INSERT, and the second commit
    raises ``IntegrityError`` on the composite primary key -- which reaches the
    DELETE route, where nothing catches it, as a 500 instead of the 409 the
    caller is owed.

    The guarantee is structural, so this asserts the structure. A two-session
    test cannot express it: these suites run SQLite on one shared connection,
    so the sessions serialise and the losing racer's re-read simply finds the
    committed row -- a read-then-write implementation passes that test while
    still being broken in production. What distinguishes the two is that the
    fixed version never issues the read at all.
    """

    owner = _client(page_service, "purge-upsert@example.com")
    owner_id = _user_id(page_service, "purge-upsert@example.com")
    workspace_id = UUID(_workspace_with_blocker(owner, "Upsert Workspace"))

    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _executemany):
        if "workspace_purge_grant" in statement:
            statements.append(" ".join(statement.split()))

    factory = page_service.session_factory
    with factory.begin() as session:
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            issue_purge_grant(session, user_id=owner_id, workspace_id=workspace_id)
        finally:
            event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1, statements
    assert statements[0].upper().startswith("INSERT INTO WORKSPACE_PURGE_GRANT")
    assert "ON CONFLICT" in statements[0].upper()
    # And it really did land, whether or not a row was already there.
    with factory() as session:
        assert session.get(WorkspacePurgeGrant, (owner_id, workspace_id)) is not None

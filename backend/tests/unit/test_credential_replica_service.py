"""P10.3 T4.5: control-plane -> workspace-plane credential-grant fan-out.

Two groups of tests, deliberately kept apart:

* "apply" tests call ``apply_credential_replica_fan_out`` directly against a
  control-index row seeded through the RAW (unstamped) ``session_factory``
  fixture -- i.e. through ``ApiTokenService``/``OAuthService`` instances that
  do NOT carry a ``DatabaseProvider`` stamp, so ``register_fan_out`` no-ops
  on creation and the row is left exactly at ``revision=1``/
  ``sync_state="pending"``. This isolates the apply function's own
  idempotent upsert/tombstone/failure-recording logic from the after_commit
  wiring.
* "wiring" tests build the same services against a STAMPED session_factory
  (``backend.app.container.DatabaseProvider(None,
  session_factory=session_factory).session_factory``) and call the real
  service methods (``create_token``, ``update_token``,
  ``update_consent_settings``) end to end, asserting the immediate
  best-effort fan-out actually ran with no extra step.

Not covered here, and deliberately not claimed as covered (see the T4.5
task's own verification note (e)): the REQUEST-TIME read side --
``grant_replica_missing``/the ceiling ∩ desired ∩ granted intersection --
is not implemented yet.
``backend.policy.trust_boundaries.actor_with_target_workspace_scope``'s own
docstring says so as of this commit ("D-01v3's workspace-plane grant-replica
intersection ... is deliberately NOT implemented here yet"). So this file
proves the REPLICA gets written/tombstoned/marked-failed correctly, not that
a real MCP/REST request is denied or allowed by it end to end -- that half
of plan section 5's R11 cannot be exercised until that read side exists.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.container import DatabaseProvider
from backend.app.settings import Settings
from backend.models.api_token import ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import grant_workspace_membership
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.credential_replica_service import (
    CredentialReplicaFanOutRequest,
    apply_credential_replica_fan_out,
    resolve_oauth_replica_credential_id,
)
from backend.services.oauth_service import OAuthService


def _default_workspace_id(session) -> object:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None, "default workspace must be seeded"
    return workspace.id


def _fast_hasher() -> CredentialHasher:
    from argon2 import PasswordHasher, Type

    return CredentialHasher(
        PasswordHasher(
            time_cost=1, memory_cost=1024, parallelism=1, hash_len=16, salt_len=16,
            type=Type.ID,
        )
    )


def _unstamped_token_service(session_factory) -> ApiTokenService:
    """Deliberately built WITHOUT routing through DatabaseProvider.session_
    factory, so no session it opens carries session.info["database_provider"]
    -- register_fan_out no-ops (see backend/services/job_hints.py's own
    documented fallback), leaving whatever control row it writes exactly at
    revision=1/sync_state="pending" for an "apply" test to act on."""

    return ApiTokenService(
        session_factory=session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="credential-replica-test-cursor-secret",
    )


def _stamped_provider(session_factory) -> DatabaseProvider:
    return DatabaseProvider(None, session_factory=session_factory)


class _RaisingWorkspaceSessionFactory:
    """The .begin()-based analogue of backend.tests.unit.test_mcp_router.
    _FailingWorkspacePlaneProvider's bare raising function -- this module's
    own workspace write goes through session_for(workspace_id).begin(), not
    a bare call, so the fake needs a .begin() that raises instead."""

    def begin(self):
        raise OperationalError(
            "SELECT 1", {}, Exception("could not connect to workspace database")
        )


class _FailingWorkspacePlaneDatabaseProvider(DatabaseProvider):
    def __init__(self, session_factory, *, unavailable_workspace_id):
        super().__init__(None, session_factory=session_factory)
        self._unavailable_workspace_id = unavailable_workspace_id

    def session_for(self, workspace_id):
        if workspace_id == self._unavailable_workspace_id:
            return _RaisingWorkspaceSessionFactory()
        return super().session_for(workspace_id)


def _seed_signed_up_user(session) -> User:
    user = User(
        email=f"replica-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        display_name="Replica Test User",
        status="active",
    )
    session.add(user)
    session.flush()
    return user


def _seed_user_with_default_workspace_membership(session) -> tuple[User, object]:
    """create_token/update_token validate EVERY requested grant's membership
    (unlike _replace_consent_workspace_grants, which only validates newly-
    added rows) -- see require_api_token_workspace_access -- so every PAT
    test below needs a real WorkspaceMembership row on whatever workspace it
    grants, not just an existing Workspace row."""

    user = _seed_signed_up_user(session)
    workspace_id = _default_workspace_id(session)
    grant_workspace_membership(
        session, user_id=user.id, workspace_id=workspace_id, role="owner"
    )
    return user, workspace_id


# ---------------------------------------------------------------------------
# "apply" tests -- apply_credential_replica_fan_out called directly
# ---------------------------------------------------------------------------


def test_expansion_creates_replica_with_scopes_and_revision(session_factory):
    """(a) An expansion (new workspace on a PAT) creates a
    workspace_credential_grant row with the right granted_scopes/
    synced_revision, and marks the control row synced."""

    token_service = _unstamped_token_service(session_factory)
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_default_workspace_membership(session)
        user_id = user.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="expansion",
        scopes=["mcp:read", "mcp:write"],
    )
    token_id = issued.api_token.id

    with session_factory() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "pending"

    provider = _stamped_provider(session_factory)
    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read", "mcp:write"),
            revision=1,
            removed_at=None,
        ),
    )

    with session_factory() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == token_id,
            )
        )
        assert replica is not None
        assert replica.granted_scopes == ["mcp:read", "mcp:write"]
        assert replica.synced_revision == 1
        assert replica.revoked_at is None

        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "synced"
        assert control_row.last_sync_error is None


def test_scope_change_bumps_revision_without_duplicating_the_replica_row(
    session_factory,
):
    """(b) A scope-only change bumps synced_revision and updates
    granted_scopes without duplicating the replica row (the unique
    constraint (workspace_id, credential_kind, credential_id) holds)."""

    token_service = _unstamped_token_service(session_factory)
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_default_workspace_membership(session)
        user_id = user.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="scope-change",
        scopes=["mcp:read", "mcp:write"],
    )
    token_id = issued.api_token.id
    provider = _stamped_provider(session_factory)

    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read",),
            revision=1,
            removed_at=None,
        ),
    )
    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read", "mcp:write"),
            revision=2,
            removed_at=None,
        ),
    )

    with session_factory() as session:
        replicas = session.scalars(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == token_id,
            )
        ).all()
        assert len(replicas) == 1
        assert replicas[0].granted_scopes == ["mcp:read", "mcp:write"]
        assert replicas[0].synced_revision == 2


def test_shrink_revokes_replica_and_hard_deletes_control_row(session_factory):
    """(c) A shrink revokes the replica (revoked_at set) AND hard-deletes
    the control index row, per section 2.6's asymmetry ("성공 시 control 행
    sync_state = synced(축소면 행 하드 삭제)")."""

    token_service = _unstamped_token_service(session_factory)
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_default_workspace_membership(session)
        user_id = user.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="shrink",
        scopes=["mcp:read"],
    )
    token_id = issued.api_token.id
    provider = _stamped_provider(session_factory)

    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read",),
            revision=1,
            removed_at=None,
        ),
    )

    with session_factory.begin() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        # Simulate _replace_workspace_grants' soft-remove: bump revision,
        # set removed_at, reset sync_state to pending.
        control_row.removed_at = utc_now()
        control_row.revision = 2
        control_row.sync_state = "pending"
        removed_at = control_row.removed_at

    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read",),
            revision=2,
            removed_at=removed_at,
        ),
    )

    with session_factory() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == token_id,
            )
        )
        assert replica is not None
        assert replica.revoked_at is not None
        assert replica.synced_revision == 2

        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row is None


def test_workspace_plane_failure_marks_control_row_failed_without_raising(
    session_factory,
):
    """(d) A simulated workspace-plane failure leaves the control row
    sync_state="failed" with last_sync_error set, emits the
    credential_replica.sync_failed audit event, and does NOT raise out of
    apply_credential_replica_fan_out (the caller -- PAT/consent creation --
    must still succeed)."""

    token_service = _unstamped_token_service(session_factory)
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_default_workspace_membership(session)
        user_id = user.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="failure-injection",
        scopes=["mcp:read"],
    )
    token_id = issued.api_token.id

    failing_provider = _FailingWorkspacePlaneDatabaseProvider(
        session_factory, unavailable_workspace_id=workspace_id
    )

    # Must not raise.
    apply_credential_replica_fan_out(
        failing_provider,
        CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read",),
            revision=1,
            removed_at=None,
        ),
    )

    with session_factory() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "failed"
        assert control_row.last_sync_error
        assert "workspace_plane_unavailable" in control_row.last_sync_error

        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == token_id,
            )
        )
        assert replica is None

        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "credential_replica.sync_failed",
                AuditEvent.target_id == token_id,
            )
        )
        assert event is not None
        assert event.payload["credential_kind"] == "pat"
        assert event.payload["workspace_id"] == str(workspace_id)
        assert event.payload["revision"] == 1


def test_oauth_replica_credential_id_is_none_without_a_refresh_token(session_factory):
    with session_factory() as session:
        user = _seed_signed_up_user(session)
        session.commit()
        credential_id = resolve_oauth_replica_credential_id(
            session, user_id=user.id, client_id="no-token-yet-client"
        )
    assert credential_id is None


def test_oauth_replica_credential_id_uses_the_family_grant_id(session_factory):
    with session_factory.begin() as session:
        user = _seed_signed_up_user(session)
        grant_id = uuid4()
        session.add(
            OAuthRefreshToken(
                token_lookup_hash=f"hash-{uuid4().hex}",
                grant_id=grant_id,
                hashed_token="hashed",
                user_id=user.id,
                client_id="family-client",
                scope="mcp:read",
                expires_at=user.created_at,
            )
        )
        user_id = user.id

    with session_factory() as session:
        credential_id = resolve_oauth_replica_credential_id(
            session, user_id=user_id, client_id="family-client"
        )
    assert credential_id == grant_id


def test_credential_id_none_marks_failed_without_a_workspace_session(session_factory):
    """A well-formed 'not ready yet' request (OAuth grant_id unresolved)
    must be recorded as a failure WITHOUT ever calling session_for -- if it
    did, this test's provider would raise AttributeError, since it exposes
    no session_for override for this scenario's workspace id besides the
    default single-DB one."""

    with session_factory() as session:
        user = _seed_signed_up_user(session)
        workspace_id = _default_workspace_id(session)
        session.commit()
        user_id = user.id
        client_id = "not-ready-client"

    with session_factory.begin() as session:
        session.add(
            OAuthClient(
                id=client_id,
                client_name="Not Ready Client",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="pending",
            )
        )

    provider = _stamped_provider(session_factory)
    apply_credential_replica_fan_out(
        provider,
        CredentialReplicaFanOutRequest(
            credential_kind="oauth",
            credential_id=None,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=("mcp:read",),
            revision=1,
            removed_at=None,
            client_id=client_id,
        ),
    )

    with session_factory() as session:
        control_row = session.get(OAuthConsent, (user_id, client_id, workspace_id))
        assert control_row.sync_state == "failed"
        assert "no stable credential identity" in control_row.last_sync_error
        replica_count = session.scalar(
            select(WorkspaceCredentialGrant.id).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "oauth",
            )
        )
        assert replica_count is None


# ---------------------------------------------------------------------------
# "wiring" tests -- the real service methods, stamped session_factory
# ---------------------------------------------------------------------------


def test_create_token_wiring_triggers_immediate_fan_out(session_factory):
    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="wiring-test-cursor-secret",
    )
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_default_workspace_membership(session)
        user_id = user.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="wiring-create",
        scopes=["mcp:read"],
    )

    with session_factory() as session:
        control_row = session.get(
            ApiTokenWorkspace, (issued.api_token.id, workspace_id)
        )
        assert control_row.sync_state == "synced"
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica is not None
        assert replica.granted_scopes == ["mcp:read"]


def test_replace_workspace_grants_wiring_fans_out_add_and_remove(session_factory):
    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="wiring-test-cursor-secret-2",
    )
    with session_factory.begin() as session:
        user, first_workspace_id = _seed_user_with_default_workspace_membership(
            session
        )
        second_workspace = Workspace(
            slug="wiring-second-workspace",
            title="Wiring Second Workspace",
            name="Wiring Second Workspace",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=second_workspace.id, role="owner"
        )
        user_id = user.id
        second_workspace_id = second_workspace.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=first_workspace_id)],
        name="wiring-replace",
        scopes=["mcp:read"],
    )
    token_service.update_token(
        user_id=user_id,
        token_id=issued.api_token.id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=second_workspace_id)],
    )

    with session_factory() as session:
        removed_control_row = session.get(
            ApiTokenWorkspace, (issued.api_token.id, first_workspace_id)
        )
        assert removed_control_row is None, (
            "a synced shrink hard-deletes the control row (section 2.6)"
        )
        removed_replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == first_workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert removed_replica is not None
        assert removed_replica.revoked_at is not None

        added_control_row = session.get(
            ApiTokenWorkspace, (issued.api_token.id, second_workspace_id)
        )
        assert added_control_row.sync_state == "synced"
        added_replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == second_workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert added_replica is not None
        assert added_replica.granted_scopes == ["mcp:read"]


def test_replace_consent_workspace_grants_wiring_fans_out_add(session_factory):
    provider = _stamped_provider(session_factory)
    oauth_service = OAuthService(
        session_factory=provider.session_factory,
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        public_base_url="https://test.local",
        database_provider=provider,
    )
    client_id = f"wiring-oauth-client-{uuid4().hex[:8]}"
    with session_factory.begin() as session:
        user = _seed_signed_up_user(session)
        first_workspace_id = _default_workspace_id(session)
        second_workspace = Workspace(
            slug=f"wiring-oauth-second-{uuid4().hex[:8]}",
            title="Wiring OAuth Second Workspace",
            name="Wiring OAuth Second Workspace",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=second_workspace.id, role="owner"
        )
        session.add(
            OAuthClient(
                id=client_id,
                client_name="Wiring OAuth Client",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=client_id,
                workspace_id=first_workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        grant_id = uuid4()
        session.add(
            OAuthRefreshToken(
                token_lookup_hash=f"hash-{uuid4().hex}",
                grant_id=grant_id,
                hashed_token="hashed",
                user_id=user.id,
                client_id=client_id,
                scope="mcp:read",
                expires_at=user.created_at,
            )
        )
        user_id = user.id
        second_workspace_id = second_workspace.id

    oauth_service.update_consent_settings(
        user_id=user_id,
        client_id=client_id,
        refresh_ttl_seconds=None,
        update_refresh_ttl=False,
        sliding_enabled=None,
        update_sliding=False,
        workspace_ids=[first_workspace_id, second_workspace_id],
    )

    with session_factory() as session:
        added_control_row = session.get(
            OAuthConsent, (user_id, client_id, second_workspace_id)
        )
        assert added_control_row.sync_state == "synced"
        added_replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == second_workspace_id,
                WorkspaceCredentialGrant.credential_kind == "oauth",
                WorkspaceCredentialGrant.credential_id == grant_id,
            )
        )
        assert added_replica is not None
        assert added_replica.granted_scopes == ["mcp:read"]


def test_replace_consent_workspace_grants_wiring_without_refresh_token_marks_failed(
    session_factory,
):
    """A workspace added to a consent that has never exchanged a token yet
    (no grant_id) must fail the fan-out cleanly, not crash update_consent_
    settings or write a replica under a fabricated identity."""

    provider = _stamped_provider(session_factory)
    oauth_service = OAuthService(
        session_factory=provider.session_factory,
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        public_base_url="https://test.local",
        database_provider=provider,
    )
    client_id = f"wiring-oauth-no-token-{uuid4().hex[:8]}"
    with session_factory.begin() as session:
        user = _seed_signed_up_user(session)
        first_workspace_id = _default_workspace_id(session)
        second_workspace = Workspace(
            slug=f"wiring-oauth-no-token-second-{uuid4().hex[:8]}",
            title="No Token Second Workspace",
            name="No Token Second Workspace",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=second_workspace.id, role="owner"
        )
        session.add(
            OAuthClient(
                id=client_id,
                client_name="No Token Client",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=client_id,
                workspace_id=first_workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        user_id = user.id
        second_workspace_id = second_workspace.id

    result = oauth_service.update_consent_settings(
        user_id=user_id,
        client_id=client_id,
        refresh_ttl_seconds=None,
        update_refresh_ttl=False,
        sliding_enabled=None,
        update_sliding=False,
        workspace_ids=[first_workspace_id, second_workspace_id],
    )
    assert result is not None  # the control write itself still succeeds

    with session_factory() as session:
        added_control_row = session.get(
            OAuthConsent, (user_id, client_id, second_workspace_id)
        )
        assert added_control_row.sync_state == "failed"
        assert "no stable credential identity" in added_control_row.last_sync_error
        replica = session.scalar(
            select(WorkspaceCredentialGrant.id).where(
                WorkspaceCredentialGrant.workspace_id == second_workspace_id,
                WorkspaceCredentialGrant.credential_kind == "oauth",
            )
        )
        assert replica is None

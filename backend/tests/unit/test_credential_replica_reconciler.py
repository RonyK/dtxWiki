"""P10.3 T4.6: the credential-replica reconciler (plan section 2.6's
"reconciler" row) and its worker job type ``credential_replica_reconcile``.

Covers plan section 5's regression R12 clause by clause:

* "``failed`` 행 재시도 성공"  -> test_failed_control_row_is_retried_*
* "index 없는 고아 복제본 tombstone" -> test_orphan_*  (plus its inverse,
  test_live_oauth_replica_is_not_tombstoned, which is the assertion that
  fails loudly if the OAuth grant_id -> consent mapping is ever inverted)
* "복제본 없는 index 행은 재생성" -> test_synced_control_row_with_a_missing_
  replica_is_repaired
* "멱등(같은 revision 두 번 적용해도 결과 동일)" -> test_reconcile_is_
  idempotent_* and test_reapplying_the_same_revision_does_not_touch_the_row

Same fixture idiom as ``test_credential_replica_service.py`` (T4.5): control
rows are seeded through an UNSTAMPED ``ApiTokenService`` so the after_commit
fan-out no-ops and leaves the row at ``sync_state="pending"``, which is
exactly the state a reconciler has to be able to pick up.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select

from backend.app.container import DatabaseProvider
from backend.models.api_token import ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.job import Job
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import (
    WorkspacePlaneUnavailableError,
    grant_workspace_membership,
)
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.credential_replica_service import (
    CredentialReplicaFanOutRequest,
    apply_credential_replica_fan_out,
    reconcile_workspace_credential_replicas,
)
from backend.services.job_queue import JobQueueService
from backend.worker.runtime import OneShotWorker

# ---------------------------------------------------------------------------
# helpers (mirroring test_credential_replica_service.py's)
# ---------------------------------------------------------------------------


def _default_workspace_id(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None, "default workspace must be seeded"
    return workspace.id


def _fast_hasher() -> CredentialHasher:
    from argon2 import PasswordHasher, Type

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


def _unstamped_token_service(session_factory) -> ApiTokenService:
    return ApiTokenService(
        session_factory=session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="reconciler-test-cursor-secret",
    )


def _stamped_provider(session_factory) -> DatabaseProvider:
    return DatabaseProvider(None, session_factory=session_factory)


class _RaisingWorkspaceSessionFactory:
    def begin(self):
        from sqlalchemy.exc import OperationalError

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


def _seed_user(session) -> User:
    user = User(
        email=f"reconcile-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        display_name="Reconciler Test User",
        status="active",
    )
    session.add(user)
    session.flush()
    return user


def _seed_user_with_membership(session):
    user = _seed_user(session)
    workspace_id = _default_workspace_id(session)
    grant_workspace_membership(
        session, user_id=user.id, workspace_id=workspace_id, role="owner"
    )
    return user, workspace_id


def _issue_pat(session_factory, *, scopes=("mcp:read",), name="reconcile"):
    """Create a PAT with one workspace grant, left at pending/revision=1 and
    with NO replica (the unstamped service fires no fan-out)."""

    token_service = _unstamped_token_service(session_factory)
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
        user_id = user.id
    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name=f"{name}-{uuid4().hex[:6]}",
        scopes=list(scopes),
    )
    return issued.api_token.id, user_id, workspace_id


def _replica(session, workspace_id, credential_kind, credential_id):
    return session.scalar(
        select(WorkspaceCredentialGrant).where(
            WorkspaceCredentialGrant.workspace_id == workspace_id,
            WorkspaceCredentialGrant.credential_kind == credential_kind,
            WorkspaceCredentialGrant.credential_id == credential_id,
        )
    )


def _audit_count(session, action) -> int:
    return session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
    )


# ---------------------------------------------------------------------------
# R12: "failed 행 재시도 성공"
# ---------------------------------------------------------------------------


def test_failed_control_row_is_retried_and_reaches_synced(session_factory):
    token_id, _user_id, workspace_id = _issue_pat(session_factory)
    with session_factory.begin() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        control_row.sync_state = "failed"
        control_row.last_sync_error = "workspace_plane_unavailable"

    provider = _stamped_provider(session_factory)
    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.reapplied_count == 1
    with session_factory() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "synced"
        assert control_row.last_sync_error is None
        replica = _replica(session, workspace_id, "pat", token_id)
        assert replica is not None
        assert replica.granted_scopes == ["mcp:read"]
        assert replica.synced_revision == 1
        assert replica.revoked_at is None


def test_a_failed_retry_that_fails_again_records_the_failure_and_does_not_raise(
    session_factory,
):
    """A reconcile that CAN read the workspace plane but whose per-credential
    apply fails must still record ``credential_replica.sync_failed`` (the
    audit action T4.5 already owns -- no new action for a retry) rather than
    crashing the worker job."""

    token_id, user_id, workspace_id = _issue_pat(session_factory)

    class _ApplyOnlyFailingProvider(DatabaseProvider):
        """session_for works for the reconciler's own bulk read, then starts
        failing -- so the pass gets far enough to attempt an apply."""

        def __init__(self, factory):
            super().__init__(None, session_factory=factory)
            self.calls = 0

        def session_for(self, target_workspace_id):
            self.calls += 1
            if self.calls == 1:
                return super().session_for(target_workspace_id)
            return _RaisingWorkspaceSessionFactory()

    provider = _ApplyOnlyFailingProvider(session_factory)
    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.reapplied_count == 1
    with session_factory() as session:
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "failed"
        assert "workspace_plane_unavailable" in control_row.last_sync_error
        assert _audit_count(session, "credential_replica.sync_failed") == 1
        assert _replica(session, workspace_id, "pat", token_id) is None
    assert user_id is not None


# ---------------------------------------------------------------------------
# R12: "index 없는 고아 복제본 tombstone"
# ---------------------------------------------------------------------------


def test_orphan_pat_replica_without_a_control_row_is_tombstoned(session_factory):
    """A replica left behind by a restored backup / re-attached shard: no
    api_token_workspaces row names it, so nothing in the control plane
    authorizes it any more."""

    orphan_credential_id = uuid4()
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="pat",
                credential_id=orphan_credential_id,
                user_id=user.id,
                granted_scopes=["mcp:read", "mcp:write"],
                synced_revision=7,
            )
        )

    provider = _stamped_provider(session_factory)
    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.orphan_tombstoned_count == 1
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", orphan_credential_id)
        assert replica.revoked_at is not None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "credential_replica.orphan_tombstoned"
            )
        )
        assert event is not None
        assert event.payload["credential_kind"] == "pat"
        assert event.payload["credential_id"] == str(orphan_credential_id)
        assert event.payload["workspace_id"] == str(workspace_id)
        assert event.target_id == replica.id


def test_orphan_oauth_replica_whose_family_is_gone_is_tombstoned(session_factory):
    """An OAuth replica is keyed by OAuthRefreshToken.grant_id. When no
    refresh row carries that family key at all, there is no (user, client)
    pair to look a consent up under -- orphan."""

    orphan_grant_id = uuid4()
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=orphan_grant_id,
                user_id=user.id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )

    provider = _stamped_provider(session_factory)
    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.orphan_tombstoned_count == 1
    with session_factory() as session:
        replica = _replica(session, workspace_id, "oauth", orphan_grant_id)
        assert replica.revoked_at is not None


def test_live_oauth_replica_is_not_tombstoned(session_factory):
    """THE anti-inversion assertion. ``credential_id`` for an OAuth replica
    is a grant_id, which is NOT a column on OAuthConsent -- resolving it the
    wrong way round (or not at all) would tombstone every live OAuth grant on
    the very first reconcile tick. A live consent + refresh family + replica
    must come through a full pass completely untouched."""

    client_id = f"live-oauth-{uuid4().hex[:8]}"
    grant_id = uuid4()
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
        session.add(
            OAuthClient(
                id=client_id,
                client_name="Live OAuth Client",
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
                workspace_id=workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        session.add(
            OAuthRefreshToken(
                token_lookup_hash=f"hash-{uuid4().hex}",
                grant_id=grant_id,
                hashed_token="hashed",
                user_id=user.id,
                client_id=client_id,
                scope="mcp:read",
                expires_at=utc_now(),
            )
        )
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=grant_id,
                user_id=user.id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )

    provider = _stamped_provider(session_factory)
    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.orphan_tombstoned_count == 0
    assert result.reapplied_count == 0
    with session_factory() as session:
        replica = _replica(session, workspace_id, "oauth", grant_id)
        assert replica.revoked_at is None
        assert _audit_count(session, "credential_replica.orphan_tombstoned") == 0


def test_a_replica_whose_control_row_is_only_soft_removed_is_not_an_orphan(
    session_factory,
):
    """``removed_at`` marks a shrink whose fan-out has not finished, and the
    RETRY pass is what finishes it -- through the full section 2.6 protocol
    (tombstone the replica at the shrink's revision AND hard delete the
    control row), not through the orphan sweep, which knows nothing about
    control rows and would leave the soft-removed one behind.

    Verified to bind: making ``_needs_apply`` return False for
    ``removed_at is not None`` leaves the control row alive and fails the
    assertion below. (The complementary question -- whether the orphan sweep
    ALSO treats a soft-removed row as an orphan -- turns out to be
    unobservable in the end state, because the retry pass runs first and
    ``_tombstone_orphan_replicas`` re-filters on ``revoked_at IS NULL``; the
    exclusion in ``_orphan_replica_ids`` is there to keep the two mechanisms
    from overlapping at all, not because a test can tell them apart here.)"""

    token_id, user_id, workspace_id = _issue_pat(session_factory)
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
        control_row.removed_at = utc_now()
        control_row.revision = 2
        control_row.sync_state = "pending"

    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    # The shrink went through the retry pass, not the orphan pass.
    assert result.reapplied_count == 1
    assert result.orphan_tombstoned_count == 0
    with session_factory() as session:
        assert session.get(ApiTokenWorkspace, (token_id, workspace_id)) is None
        replica = _replica(session, workspace_id, "pat", token_id)
        assert replica.revoked_at is not None
        assert replica.synced_revision == 2
        assert _audit_count(session, "credential_replica.orphan_tombstoned") == 0


# ---------------------------------------------------------------------------
# R12: "복제본 없는 index 행은 재생성"
# ---------------------------------------------------------------------------


def test_synced_control_row_with_a_missing_replica_is_repaired(session_factory):
    """The drift case the two sentences of section 2.6's reconciler row only
    jointly cover: a row that already reached ``synced`` and whose replica
    later vanished (restored backup, rebuilt shard, manual delete). Confining
    the pass to ``sync_state != synced`` would leave it denying every request
    forever behind a green chip."""

    token_id, user_id, workspace_id = _issue_pat(session_factory)
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
        assert control_row.sync_state == "synced"
        session.delete(_replica(session, workspace_id, "pat", token_id))

    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.reapplied_count == 1
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", token_id)
        assert replica is not None
        assert replica.granted_scopes == ["mcp:read"]
        assert replica.synced_revision == 1
        assert replica.revoked_at is None


def test_readd_after_a_shrink_revives_the_tombstoned_replica(session_factory):
    """A workspace removed and then re-added. The re-add creates a FRESH
    control row whose revision restarts at 1, while the replica still carries
    the shrink's higher synced_revision and a tombstone -- so a pure
    ``synced_revision >= revision`` guard would skip it and leave the
    re-granted workspace permanently denied."""

    token_id, user_id, workspace_id = _issue_pat(session_factory)
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
        control_row.removed_at = utc_now()
        control_row.revision = 2
        control_row.sync_state = "pending"
    reconcile_workspace_credential_replicas(provider, workspace_id=workspace_id)

    with session_factory.begin() as session:
        assert session.get(ApiTokenWorkspace, (token_id, workspace_id)) is None
        session.add(
            ApiTokenWorkspace(
                token_id=token_id,
                workspace_id=workspace_id,
                desired_scopes=["mcp:read", "mcp:write"],
                revision=1,
                sync_state="pending",
            )
        )

    result = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert result.reapplied_count == 1
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", token_id)
        assert replica.revoked_at is None
        assert replica.granted_scopes == ["mcp:read", "mcp:write"]
        assert replica.synced_revision == 1
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "synced"


# ---------------------------------------------------------------------------
# R12: idempotency
# ---------------------------------------------------------------------------


def test_reconcile_is_idempotent_across_two_consecutive_passes(session_factory):
    token_id, _user_id, workspace_id = _issue_pat(session_factory)
    orphan_credential_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="pat",
                credential_id=orphan_credential_id,
                user_id=uuid4(),
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )

    provider = _stamped_provider(session_factory)
    first = reconcile_workspace_credential_replicas(provider, workspace_id=workspace_id)
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", token_id)
        first_synced_at = replica.synced_at
        first_revision = replica.synced_revision
        orphan_revoked_at = _replica(
            session, workspace_id, "pat", orphan_credential_id
        ).revoked_at

    second = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert (first.reapplied_count, first.orphan_tombstoned_count) == (1, 1)
    # Second pass finds nothing left to do and writes nothing.
    assert (second.reapplied_count, second.orphan_tombstoned_count) == (0, 0)
    assert second.index_row_count == first.index_row_count
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", token_id)
        assert replica.synced_at == first_synced_at
        assert replica.synced_revision == first_revision
        assert (
            _replica(session, workspace_id, "pat", orphan_credential_id).revoked_at
            == orphan_revoked_at
        )
        assert _audit_count(session, "credential_replica.orphan_tombstoned") == 1


def test_reapplying_the_same_revision_does_not_touch_the_row(session_factory):
    """The other half of R12's idempotency clause, stated at the apply level:
    ``_write_replica``'s synced_revision guard means a duplicated apply of
    the SAME revision converges rather than rewriting the row."""

    token_id, user_id, workspace_id = _issue_pat(session_factory)
    provider = _stamped_provider(session_factory)
    request = CredentialReplicaFanOutRequest(
        credential_kind="pat",
        credential_id=token_id,
        workspace_id=workspace_id,
        user_id=user_id,
        desired_scopes=("mcp:read",),
        revision=1,
        removed_at=None,
    )
    apply_credential_replica_fan_out(provider, request)
    with session_factory() as session:
        before = _replica(session, workspace_id, "pat", token_id)
        before_synced_at = before.synced_at
        before_id = before.id

    apply_credential_replica_fan_out(provider, request)

    with session_factory() as session:
        rows = session.scalars(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == token_id,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].id == before_id
        assert rows[0].synced_at == before_synced_at


# ---------------------------------------------------------------------------
# "not ready" OAuth families and infrastructure failures
# ---------------------------------------------------------------------------


def test_oauth_consent_without_a_token_exchange_is_not_churned(session_factory):
    """resolve_oauth_replica_credential_id returns None for a (user, client)
    family that has never exchanged a code for a token. Already recorded as
    failed, it must be left alone -- otherwise every minute would re-emit the
    same credential_replica.sync_failed audit row for a consent that is
    simply waiting on its first token exchange."""

    client_id = f"not-ready-{uuid4().hex[:8]}"
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
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
                user_id=user.id,
                client_id=client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="pending",
            )
        )
        user_id = user.id

    provider = _stamped_provider(session_factory)
    first = reconcile_workspace_credential_replicas(provider, workspace_id=workspace_id)
    second = reconcile_workspace_credential_replicas(
        provider, workspace_id=workspace_id
    )

    assert first.not_ready_count == 1
    assert second.not_ready_count == 1
    assert (first.reapplied_count, second.reapplied_count) == (0, 0)
    with session_factory() as session:
        control_row = session.get(OAuthConsent, (user_id, client_id, workspace_id))
        assert control_row.sync_state == "failed"
        assert "no stable credential identity" in control_row.last_sync_error
        # Recorded exactly ONCE across two passes -- the second pass saw the
        # row already at "failed" and did not re-audit it.
        assert _audit_count(session, "credential_replica.sync_failed") == 1


def test_unreachable_workspace_plane_raises_before_any_apply_attempt(session_factory):
    """The reconciler reads the workspace plane FIRST so an outage surfaces
    once, as a failed job, instead of as one sync_failed audit row per
    credential per minute."""

    token_id, _user_id, workspace_id = _issue_pat(session_factory)
    provider = _FailingWorkspacePlaneDatabaseProvider(
        session_factory, unavailable_workspace_id=workspace_id
    )

    try:
        reconcile_workspace_credential_replicas(provider, workspace_id=workspace_id)
    except WorkspacePlaneUnavailableError as exc:
        assert exc.workspace_id == workspace_id
    else:  # pragma: no cover - the assertion below reports it either way
        raise AssertionError("expected WorkspacePlaneUnavailableError")

    with session_factory() as session:
        assert _audit_count(session, "credential_replica.sync_failed") == 0
        control_row = session.get(ApiTokenWorkspace, (token_id, workspace_id))
        assert control_row.sync_state == "pending"


# ---------------------------------------------------------------------------
# worker wiring
# ---------------------------------------------------------------------------


def test_worker_dispatches_credential_replica_reconcile(session_factory):
    """End-to-end through the registered job handler: the seeded job type
    reaches the reconciler, its counts come back as the job detail, and the
    job completes."""

    orphan_credential_id = uuid4()
    provider = _stamped_provider(session_factory)
    queue = JobQueueService()
    with session_factory.begin() as session:
        user, workspace_id = _seed_user_with_membership(session)
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="pat",
                credential_id=orphan_credential_id,
                user_id=user.id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )
        queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="credential_replica_reconcile",
            trigger_type="scheduled",
            idempotency_key=f"{workspace_id}:reconcile-test",
            target_ref={},
        )

    worker = OneShotWorker(job_queue=queue, database_provider=provider)
    result = worker.dispatch_once_with_session_factory(
        provider.session_factory,
        workspace_id=workspace_id,
        job_types={"credential_replica_reconcile"},
    )

    assert result is not None
    assert result.status == "completed"
    assert result.detail["orphan_tombstoned_count"] == 1
    assert result.detail["replica_count"] == 1
    with session_factory() as session:
        replica = _replica(session, workspace_id, "pat", orphan_credential_id)
        assert replica.revoked_at is not None
        job = session.scalar(
            select(Job).where(Job.job_type == "credential_replica_reconcile")
        )
        assert job.status == "completed"

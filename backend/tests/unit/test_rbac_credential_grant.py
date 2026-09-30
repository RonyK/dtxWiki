"""P10.3 GAP-2: request-time ceiling ∩ desired ∩ granted intersection.

R11 (.claude/plan/260909-p10-3-multi-workspace-credential.md section 5):
"복제본 fan-out: B 확장 직후 복제본 생성 전 요청 -> grant_replica_missing, 생성 후 ->
허용. B 축소 직후(fan-out 전) -> 즉시 거부. fan-out 실패 주입 -> control
sync_state=failed + 감사 이벤트 + chip 상태. 복제본 granted_scopes가 desired보다
넓게 조작돼도 유효 scope는 교집합."

``backend.tests.unit.test_credential_replica_service`` proves the REPLICA
gets written/tombstoned/marked-failed correctly (T4.5's own scope). This
file proves the OTHER half: that ``backend.policy.rbac.require_workspace_
role``'s new ``_effective_scopes_from_workspace_grant`` check (GAP-2) reads
that replica back correctly and enforces it -- end to end, through the REAL
issuance and authentication paths (``ApiTokenService.create_token`` /
``backend.policy.auth._authenticate_api_token``, ``OAuthService.issue_
authorization_code`` + ``exchange_authorization_code`` / ``_authenticate_
oauth_token``), not hand-built ``ActorContext.token_id`` values. That is
deliberate: it is the only way to actually prove the credential_id
identity-space claim in ``_effective_scopes_from_workspace_grant``'s own
docstring -- that the id ``credential_replica_service`` writes a replica
under is the EXACT id a real authenticated actor's ``token_id`` carries, not
merely an id that happens to look right in a hand-built fixture.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.container import DatabaseProvider
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import _authenticate_api_token, _authenticate_oauth_token
from backend.policy.rbac import (
    GrantReplicaMissingError,
    RejectedByPolicyError,
    grant_workspace_membership,
    require_workspace_role,
)
from backend.policy.trust_boundaries import actor_with_target_workspace_scope
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.credential_replica_service import (
    CredentialReplicaFanOutRequest,
    apply_credential_replica_fan_out,
)
from backend.services.oauth_service import OAuthService, challenge_s256

_PUBLIC_BASE_URL = "https://test.local"
_VERIFIER = "v" * 64
_REDIRECT_URI = "http://127.0.0.1:53682/callback"


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


def _stamped_provider(session_factory) -> DatabaseProvider:
    return DatabaseProvider(None, session_factory=session_factory)


def _settings() -> Settings:
    return Settings(database_url="sqlite+pysqlite:///:memory:")


def _seed_user_with_workspace(session, *, slug: str) -> tuple[object, object]:
    user = User(
        email=f"grant-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        display_name="Grant Test User",
        status="active",
    )
    session.add(user)
    workspace = Workspace(slug=slug, title=slug, name=slug)
    session.add(workspace)
    session.flush()
    grant_workspace_membership(
        session, user_id=user.id, workspace_id=workspace.id, role="owner"
    )
    return user.id, workspace.id


class _RaisingWorkspaceSessionFactory:
    def begin(self):
        raise OperationalError(
            "SELECT 1", {}, Exception("could not connect to workspace database")
        )


class _FailingWorkspacePlaneDatabaseProvider(DatabaseProvider):
    """Mirrors test_credential_replica_service's own fake exactly -- the
    fan-out's own workspace-plane apply goes through session_for(workspace_
    id).begin(), so the fake's .session_for() must return a factory whose
    .begin() raises for the target workspace only."""

    def __init__(self, session_factory, *, unavailable_workspace_id):
        super().__init__(None, session_factory=session_factory)
        self._unavailable_workspace_id = unavailable_workspace_id

    def session_for(self, workspace_id):
        if workspace_id == self._unavailable_workspace_id:
            return _RaisingWorkspaceSessionFactory()
        return super().session_for(workspace_id)


def test_pat_expansion_then_immediate_request_succeeds(session_factory):
    """R11 "생성 후 -> 허용" (PAT half) + the credential_id identity-space
    claim: create_token's real fan-out (T4.5, already wired) must leave a
    replica whose credential_id the REAL authenticated actor's token_id
    (backend.policy.auth._authenticate_api_token) actually matches."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-pat-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(session, slug="r11-pat-ws")

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-pat",
        scopes=["mcp:read"],
    )

    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    assert actor.token_id == issued.api_token.id

    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        access = require_workspace_role(
            session=session,
            actor=scoped_actor,
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=provider.session_for(workspace_id),
        )
    assert access.role == "owner"
    assert access.granted_scopes == ("mcp:read",)


def test_pat_workspace_desired_scopes_narrow_the_token_ceiling(session_factory):
    """The control-plane workspace grant is part of the effective scope."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-pat-desired-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(
            session, slug="r11-pat-desired-ws"
        )

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[
            WorkspaceGrantRequest(workspace_id=workspace_id, scopes=["mcp:read"])
        ],
        name="r11-pat-desired",
        scopes=["mcp:read", "mcp:agent"],
    )
    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    assert set(actor.scopes) == {"mcp:read", "mcp:agent"}

    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )

    assert scoped_actor.scopes == ("mcp:read",)


def test_oauth_expansion_then_immediate_request_succeeds(session_factory):
    """R11 "생성 후 -> 허용" (OAuth half), and the same credential_id
    identity-space proof for oauth_jwt: this exercises the interactive-
    consent-screen fan-out (GAP-1, this change) AND the exchange-time
    re-fan-out (this change's same-request-timing fix) together -- the
    very first authorize+exchange round for a brand-new (user, client)
    family, immediately used."""

    provider = _stamped_provider(session_factory)
    oauth_service = OAuthService(
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
        database_provider=provider,
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(session, slug="r11-oauth-ws")

    client = oauth_service.register_client(
        payload={
            "client_name": "R11 OAuth Client",
            "redirect_uris": [_REDIRECT_URI],
            "scope": "mcp:read",
        },
        client_ip="127.0.0.1",
    )
    code = oauth_service.issue_authorization_code(
        user_id=user_id,
        client_id=client.client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=challenge_s256(_VERIFIER),
        code_challenge_method="S256",
        scope="mcp:read",
        state="r11-oauth-state-0123456789",
        workspace_ids=[workspace_id],
    )
    result = oauth_service.exchange_authorization_code(
        code=code,
        client_id=client.client_id,
        redirect_uri=_REDIRECT_URI,
        code_verifier=_VERIFIER,
    )

    actor = _authenticate_oauth_token(
        token=result.access_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )

    with session_factory() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "oauth",
            )
        )
        assert replica is not None, (
            "GAP-1/exchange-time fan-out must have created the replica"
        )
        assert replica.credential_id == actor.token_id, (
            "the fan-out's credential_id and the real authenticated actor's "
            "token_id must be the exact same identifier space"
        )

        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        access = require_workspace_role(
            session=session,
            actor=scoped_actor,
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=provider.session_for(workspace_id),
        )
    assert access.role == "owner"
    assert access.granted_scopes == ("mcp:read",)


def test_grant_replica_missing_before_fan_out_and_allowed_after(session_factory):
    """R11 "B 확장 직후 복제본 생성 전 요청 -> grant_replica_missing, 생성 후 ->
    허용" -- the SAME control row, checked before and after its replica
    exists. The UNSTAMPED token_service mirrors credential_replica_service's
    own documented no-op fallback (register_fan_out no-ops when session.info
    lacks a database_provider stamp), which is the cleanest way to get a
    control row deterministically stuck at revision=1/sync_state="pending"
    with NO replica -- section 2.6's own "생성 전" state."""

    unstamped_token_service = ApiTokenService(
        session_factory=session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-missing-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(
            session, slug="r11-missing-ws"
        )

    issued = unstamped_token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-missing",
        scopes=["mcp:read"],
    )

    provider = _stamped_provider(session_factory)
    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )

    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        try:
            require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role="viewer",
                workspace_session_factory=provider.session_for(workspace_id),
            )
            raise AssertionError("expected GrantReplicaMissingError before fan-out")
        except GrantReplicaMissingError as exc:
            assert exc.error_code == "grant_replica_missing"
            assert not isinstance(exc, RejectedByPolicyError), (
                "must not be foldable into a stable 403 policy denial -- see "
                "rbac.py's GrantReplicaMissingError docstring"
            )

    # "생성 후" -- apply the SAME already-committed control row's fan-out
    # (T4.5's own apply function, exactly what the reconciler or a retried
    # after_commit attempt would do), then the identical request succeeds.
    with session_factory() as session:
        from backend.models.api_token import ApiTokenWorkspace

        control_row = session.get(
            ApiTokenWorkspace, (issued.api_token.id, workspace_id)
        )
        assert control_row is not None
        assert control_row.sync_state == "pending"
        request = CredentialReplicaFanOutRequest(
            credential_kind="pat",
            credential_id=issued.api_token.id,
            workspace_id=workspace_id,
            user_id=user_id,
            desired_scopes=tuple(control_row.desired_scopes),
            revision=control_row.revision,
            removed_at=None,
        )
    apply_credential_replica_fan_out(provider, request)

    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        access = require_workspace_role(
            session=session,
            actor=scoped_actor,
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=provider.session_for(workspace_id),
        )
    assert access.role == "owner"
    assert access.granted_scopes == ("mcp:read",)


def test_shrink_denies_immediately_even_before_fan_out_ever_runs(session_factory):
    """R11 "B 축소 직후(fan-out 전) -> 즉시 거부" -- this is the EXISTING
    control-plane token_workspace_ids guard in actor_with_target_workspace_
    scope, confirmed still doing its job additively: a failing fan-out
    (workspace plane unreachable, so the tombstone write never lands and the
    replica is left exactly as it was, still live/un-revoked) must not
    matter, because the control-plane index row itself is already gone --
    the request never even reaches require_workspace_role's grant-replica
    read."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-shrink-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(session, slug="r11-shrink-ws")
        kept_workspace = Workspace(
            slug="r11-shrink-ws-kept", title="kept", name="kept"
        )
        session.add(kept_workspace)
        session.flush()
        grant_workspace_membership(
            session, user_id=user_id, workspace_id=kept_workspace.id, role="owner"
        )
        kept_workspace_id = kept_workspace.id

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[
            WorkspaceGrantRequest(workspace_id=workspace_id),
            WorkspaceGrantRequest(workspace_id=kept_workspace_id),
        ],
        name="r11-shrink",
        scopes=["mcp:read"],
    )
    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    # Confirm the replica really is live before the shrink (sanity: R11's
    # "before fan-out" claim below is about the SHRINK's own fan-out, not
    # about there never having been a replica at all).
    with session_factory() as session:
        replica_before = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica_before is not None and replica_before.revoked_at is None

    failing_provider = _FailingWorkspacePlaneDatabaseProvider(
        provider.session_factory, unavailable_workspace_id=workspace_id
    )
    failing_token_service = ApiTokenService(
        session_factory=failing_provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-shrink-cursor-secret",
    )
    # Shrink to just the OTHER (kept) workspace, dropping this one. The
    # control row is hard-deleted synchronously (section 2.6: a synced
    # shrink deletes outright once its fan-out succeeds) -- but here the
    # fan-out itself FAILS (workspace plane unreachable), so the control row
    # instead stays at removed_at set / sync_state="failed", and the replica
    # is never actually tombstoned. R11's claim is that none of that matters
    # for the very next request.
    failing_token_service.update_token(
        user_id=user_id,
        token_id=issued.api_token.id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=kept_workspace_id)],
    )

    # token_workspace_ids is read live at authentication time (D-01v2) --
    # re-authenticate to get the actor a genuinely NEXT request would carry,
    # reflecting the shrink that just happened, not the stale pre-shrink
    # snapshot captured above.
    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )

    with session_factory() as session:
        try:
            actor_with_target_workspace_scope(actor, workspace_id, session=session)
            raise AssertionError("expected token_workspace_denied immediately")
        except PermissionError as exc:
            assert "token_workspace_denied" in str(exc)

        # The (un-tombstoned, still-live) replica really is still sitting
        # there, proving the denial above came from the control-plane index
        # alone, not from the replica happening to already be gone too.
        replica_after = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica_after is not None and replica_after.revoked_at is None


def test_fan_out_failure_marks_control_row_failed_with_audit_and_denies_cleanly(
    session_factory,
):
    """R11 "fan-out 실패 주입 -> control sync_state=failed + 감사 이벤트 + chip
    상태" (the chip itself is frontend/T5 scope, not asserted here) plus the
    "no 500, no silent allow" requirement: a workspace whose fan-out never
    landed (workspace plane unreachable at grant time) must deny the very
    next request with grant_replica_missing, cleanly -- never an unclassified
    500 and never an optimistic allow on the control-plane check alone."""

    provider = _stamped_provider(session_factory)
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(
            session, slug="r11-failure-ws"
        )

    failing_provider = _FailingWorkspacePlaneDatabaseProvider(
        provider.session_factory, unavailable_workspace_id=workspace_id
    )
    failing_token_service = ApiTokenService(
        session_factory=failing_provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-failure-cursor-secret",
    )
    issued = failing_token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-failure",
        scopes=["mcp:read"],
    )

    with session_factory() as session:
        from backend.models.api_token import ApiTokenWorkspace

        control_row = session.get(
            ApiTokenWorkspace, (issued.api_token.id, workspace_id)
        )
        assert control_row is not None
        assert control_row.sync_state == "failed"
        assert control_row.last_sync_error
        sync_failed_event = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "credential_replica.sync_failed")
            .order_by(AuditEvent.created_at.desc())
        )
        assert sync_failed_event is not None
        assert sync_failed_event.payload["credential_kind"] == "pat"
        assert sync_failed_event.payload["workspace_id"] == str(workspace_id)

    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        try:
            require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role="viewer",
                # Note: a WORKING provider here (not the failing one) -- the
                # workspace plane itself is reachable now, it simply never
                # received the replica write, which is the scenario this
                # test is isolating from workspace_plane_unavailable.
                workspace_session_factory=provider.session_for(workspace_id),
            )
            raise AssertionError("expected GrantReplicaMissingError")
        except GrantReplicaMissingError as exc:
            assert exc.error_code == "grant_replica_missing"


def test_replica_granted_scopes_wider_than_ceiling_still_intersects(session_factory):
    """R11 "복제본 granted_scopes가 desired보다 넓게 조작돼도 유효 scope는 교집합"
    -- even a corrupted/stale-wide replica cannot grant the actor more than
    its own token ceiling already allows. A plain set intersection is the
    entire safety property (no "trust desired over granted" special-casing
    needed)."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-wide-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(session, slug="r11-wide-ws")

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-wide",
        scopes=["mcp:read"],
    )
    # Corrupt/widen the replica's granted_scopes directly -- simulating a
    # stale or tampered workspace-plane row that claims more than the
    # control plane's own ceiling ever authorized for this token.
    with session_factory.begin() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica is not None
        replica.granted_scopes = ["mcp:read", "mcp:write", "mcp:agent"]

    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    assert actor.scopes == ("mcp:read",), "the token's own ceiling is unchanged"

    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        access = require_workspace_role(
            session=session,
            actor=scoped_actor,
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=provider.session_for(workspace_id),
        )
    assert access.granted_scopes == ("mcp:read",), (
        "the wider replica scopes must NOT widen the effective grant past "
        "the token's own ceiling"
    )


def test_narrow_replica_rechecks_the_required_workspace_action(session_factory):
    """Callers that only consume WorkspaceAccess must still fail closed."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-narrow-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(
            session, slug="r11-narrow-ws"
        )

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-narrow",
        scopes=["mcp:read", "mcp:write"],
    )
    with session_factory.begin() as session:
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica is not None
        replica.granted_scopes = ["mcp:read"]

    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        with pytest.raises(
            RejectedByPolicyError,
            match="scope insufficient for action",
        ):
            require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role="editor",
                workspace_session_factory=provider.session_for(workspace_id),
            )


def test_revoked_replica_denies_even_though_control_index_still_allows(
    session_factory,
):
    """A tombstoned (revoked_at set) replica must deny, even when the
    control-plane index row was left untouched -- distinct from the "no row
    at all" case, and distinct from a control-plane shrink (which denies at
    actor_with_target_workspace_scope instead, before this check ever
    runs)."""

    provider = _stamped_provider(session_factory)
    token_service = ApiTokenService(
        session_factory=provider.session_factory,
        credential_hasher=_fast_hasher(),
        cursor_secret="r11-revoked-cursor-secret",
    )
    with session_factory.begin() as session:
        user_id, workspace_id = _seed_user_with_workspace(
            session, slug="r11-revoked-ws"
        )

    issued = token_service.create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="r11-revoked",
        scopes=["mcp:read"],
    )
    with session_factory.begin() as session:
        from backend.models.base import utc_now

        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == "pat",
                WorkspaceCredentialGrant.credential_id == issued.api_token.id,
            )
        )
        assert replica is not None
        replica.revoked_at = utc_now()

    actor = _authenticate_api_token(
        token=issued.plain_token,
        session_factory=provider.session_factory,
        settings=_settings(),
        public_base_url=_PUBLIC_BASE_URL,
    )
    with session_factory() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor, workspace_id, session=session
        )
        # actor_with_target_workspace_scope's own control-plane check still
        # passes -- the index row was left untouched -- proving the denial
        # below comes specifically from the tombstoned replica.
        assert scoped_actor.workspace_id == workspace_id
        try:
            require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role="viewer",
                workspace_session_factory=provider.session_for(workspace_id),
            )
            raise AssertionError("expected GrantReplicaMissingError")
        except GrantReplicaMissingError as exc:
            assert exc.error_code == "grant_replica_missing"

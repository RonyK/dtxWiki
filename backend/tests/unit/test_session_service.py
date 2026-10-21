import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import select

from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import (
    SESSION_TOKEN_KIND,
    SESSION_TOKEN_VERSION,
    DuplicateEmailError,
    InvalidCredentialsError,
    InvalidSessionError,
    PasswordPolicyError,
    RecentReauthenticationRequiredError,
    SessionService,
    SessionTokenCodec,
    SignupDisabledError,
    SuspendedUserError,
    _base64url_encode,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 5, 14, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


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
def clock() -> Clock:
    return Clock()


@pytest.fixture
def session_service(session_factory, clock) -> SessionService:
    return SessionService(
        session_factory=session_factory,
        credential_hasher=fast_hasher(),
        token_codec=SessionTokenCodec(
            secret="test-session-secret",
            ttl_seconds=3600,
            now=clock.now,
        ),
        # AUTH-09: signup_enabled now defaults to False (fail closed); this
        # fixture exercises .signup() throughout the file, so it must opt in
        # explicitly rather than lean on a permissive default.
        signup_enabled=True,
    )


def membership_roles(session_factory, user_id: UUID) -> dict[str, str]:
    with session_factory() as session:
        return dict(
            session.execute(
                select(Workspace.slug, WorkspaceMembership.role)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.workspace_id == Workspace.id,
                )
                .where(WorkspaceMembership.user_id == user_id)
            ).all()
        )


def test_signup_creates_user_and_session_without_plaintext_password(
    session_factory,
    session_service,
):
    result = session_service.signup(
        email=" User@Example.COM ",
        password="correct-password",
        display_name="  Example User  ",
    )

    assert result.user.email == "user@example.com"
    assert result.user.display_name == "Example User"
    assert result.session_token
    assert session_service.authenticate_session(result.session_token) == result.user

    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "user@example.com"))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert "correct-password" not in user.password_hash
        assert user.personal_workspace_id is not None
        assert user.default_workspace_id == user.personal_workspace_id
        personal = session.get(Workspace, user.personal_workspace_id)
        assert personal is not None
        assert personal.title == "Personal Workspace"
        assert personal.visibility == "private"
        assert personal.join_policy == "invite_only"


def test_signup_rejects_duplicate_email(session_service):
    session_service.signup(email="same@example.com", password="first-password-123")

    with pytest.raises(DuplicateEmailError):
        session_service.signup(
            email=" SAME@example.com ", password="second-password-123"
        )


def test_solo_local_signup_is_personal_only_for_every_user(
    session_factory,
    session_service,
):
    first = session_service.signup(
        email="solo-one@example.com", password="solo-password-1"
    )
    second = session_service.signup(
        email="solo-two@example.com", password="solo-password-2"
    )

    assert list(membership_roles(session_factory, first.user.user_id).values()) == [
        "owner"
    ]
    assert list(membership_roles(session_factory, second.user.user_id).values()) == [
        "owner"
    ]
    assert "default" not in membership_roles(session_factory, first.user.user_id)
    assert "default" not in membership_roles(session_factory, second.user.user_id)


def test_solo_remote_signup_is_personal_owner_only(
    session_factory,
    session_service,
):
    remote_service = replace(session_service, app_mode="solo_remote")

    first = remote_service.signup(
        email="remote-one@example.com", password="remote-password-1"
    )
    second = remote_service.signup(
        email="remote-two@example.com", password="remote-password-2"
    )

    assert list(membership_roles(session_factory, first.user.user_id).values()) == [
        "owner"
    ]
    assert list(membership_roles(session_factory, second.user.user_id).values()) == [
        "owner"
    ]


def test_team_mode_signup_is_personal_owner_only(session_factory, session_service):
    team_service = replace(session_service, app_mode="team")

    first = team_service.signup(
        email="team-one@example.com", password="team-password-1"
    )
    second = team_service.signup(
        email="team-two@example.com", password="team-password-2"
    )

    assert list(membership_roles(session_factory, first.user.user_id).values()) == [
        "owner"
    ]
    assert list(membership_roles(session_factory, second.user.user_id).values()) == [
        "owner"
    ]


def test_signup_disabled_rejects_new_signup(session_factory, session_service):
    disabled_service = replace(session_service, signup_enabled=False)

    with pytest.raises(SignupDisabledError):
        disabled_service.signup(
            email="blocked@example.com", password="blocked-password-123"
        )

    with session_factory() as session:
        assert session.scalar(
            select(User).where(User.email == "blocked@example.com")
        ) is None


def test_signup_enabled_defaults_to_false(session_factory):
    # AUTH-09: signup_enabled is a security parameter and must fail closed
    # by default. HEAD's only production factory always passes it
    # explicitly, so this default is unreachable today — but a call site
    # that forgets the argument should silently disable signup, not
    # silently expose it.
    service = SessionService(
        session_factory=session_factory,
        credential_hasher=fast_hasher(),
        token_codec=SessionTokenCodec(secret="signup-default-test-secret"),
    )

    with pytest.raises(SignupDisabledError):
        service.assert_signup_enabled()


def test_signup_rolls_back_user_when_personal_provisioning_fails(
    session_factory,
    session_service,
    monkeypatch,
):
    def fail_provisioning(*args, **kwargs):
        raise RuntimeError("simulated Personal provisioning failure")

    monkeypatch.setattr(
        "backend.services.session_service.ensure_personal_workspace",
        fail_provisioning,
    )

    with pytest.raises(RuntimeError, match="Personal provisioning failure"):
        session_service.signup(
            email="rolled-back@example.com",
            password="correct-password",
        )

    with session_factory() as session:
        assert session.scalar(
            select(User).where(User.email == "rolled-back@example.com")
        ) is None


def test_login_rejects_unknown_or_wrong_password(session_service):
    session_service.signup(email="login@example.com", password="right-password-123")

    with pytest.raises(InvalidCredentialsError):
        session_service.login(
            email="missing@example.com", password="right-password-123"
        )
    with pytest.raises(InvalidCredentialsError):
        session_service.login(email="login@example.com", password="wrong-pass")


def test_login_and_password_change_rotate_credentials(session_service):
    signed_up = session_service.signup(
        email="change@example.com",
        password="old-password-123",
    )
    logged_in = session_service.login(
        email="CHANGE@example.com",
        password="old-password-123",
    )

    assert logged_in.user == signed_up.user

    changed = session_service.change_password(
        user_id=signed_up.user.user_id,
        current_password="old-password-123",
        new_password="new-password-123",
    )

    assert changed.user == signed_up.user
    with pytest.raises(InvalidSessionError):
        session_service.authenticate_session(signed_up.session_token)
    with pytest.raises(InvalidSessionError):
        session_service.authenticate_session(logged_in.session_token)
    assert session_service.authenticate_session(changed.session_token) == changed.user
    with pytest.raises(InvalidCredentialsError):
        session_service.login(email="change@example.com", password="old-password-123")
    assert (
        session_service.login(
            email="change@example.com",
            password="new-password-123",
        ).user
        == signed_up.user
    )


def test_session_token_rejects_tamper_and_expiry(session_service, clock):
    result = session_service.signup(
        email="token@example.com", password="token-password-123"
    )
    payload, signature = result.session_token.split(".", maxsplit=1)

    with pytest.raises(InvalidSessionError):
        session_service.authenticate_session(f"{payload}x.{signature}")

    clock.advance(timedelta(seconds=3600))
    with pytest.raises(InvalidSessionError):
        session_service.authenticate_session(result.session_token)


def test_remembered_and_session_only_tokens_use_distinct_absolute_ttls(
    session_service,
    clock,
):
    session_only = session_service.signup(
        email="ttl@example.com",
        password="correct-password",
        remember_me=False,
    )
    remembered = session_service.login(
        email="ttl@example.com",
        password="correct-password",
        remember_me=True,
    )

    session_only_verified = session_service.token_codec.verify_session(
        session_only.session_token
    )
    remembered_verified = session_service.token_codec.verify_session(
        remembered.session_token
    )
    assert session_only.remember_me is False
    assert session_only.expires_at - session_only.issued_at == timedelta(hours=1)
    assert session_only_verified.remember_me is False
    assert session_only_verified.authenticated_at is None
    assert remembered.remember_me is True
    assert remembered.expires_at - remembered.issued_at == timedelta(days=30)
    assert remembered_verified.remember_me is True
    assert remembered_verified.authenticated_at == clock.value


def test_remembered_session_refresh_window_overlap_and_absolute_cap(
    session_service,
    clock,
):
    service = replace(
        session_service,
        token_codec=SessionTokenCodec(
            secret="remembered-refresh-test-secret",
            ttl_seconds=10,
            remembered_ttl_seconds=100,
            remembered_refresh_threshold_seconds=20,
            remembered_absolute_ttl_seconds=250,
            now=clock.now,
        ),
    )
    initial = service.signup(
        email="remembered-refresh@example.com",
        password="correct-password",
        remember_me=True,
    )
    original_authentication_time = initial.issued_at

    clock.advance(timedelta(seconds=79))
    _, not_due = service.authenticate_session_with_refresh(initial.session_token)
    assert not_due is None

    clock.advance(timedelta(seconds=1))
    _, first_refresh = service.authenticate_session_with_refresh(initial.session_token)
    assert first_refresh is not None
    assert first_refresh.expires_at == clock.value + timedelta(seconds=100)
    first_verified = service.token_codec.verify_session(first_refresh.session_token)
    assert first_verified.authenticated_at == original_authentication_time

    # Stateless cookies cannot revoke the prior token during rotation. Renewal
    # starts only inside the final threshold window, so this overlap is <=20s.
    assert service.authenticate_session(initial.session_token) == initial.user
    assert initial.expires_at - clock.value == timedelta(seconds=20)

    clock.advance(timedelta(seconds=81))
    _, capped_refresh = service.authenticate_session_with_refresh(
        first_refresh.session_token
    )
    assert capped_refresh is not None
    assert capped_refresh.expires_at == original_authentication_time + timedelta(
        seconds=250
    )

    clock.advance(timedelta(seconds=70))
    _, no_rotation_at_cap = service.authenticate_session_with_refresh(
        capped_refresh.session_token
    )
    assert no_rotation_at_cap is None


def test_password_change_preserves_remembered_flag_and_resets_auth_time(
    session_service,
    clock,
):
    remembered = session_service.signup(
        email="remembered-password-change@example.com",
        password="old-password-123",
        remember_me=True,
    )
    before = session_service.token_codec.verify_session(remembered.session_token)
    assert before.remember_me is True

    clock.advance(timedelta(days=1))
    changed = session_service.change_password(
        user_id=remembered.user.user_id,
        current_password="old-password-123",
        new_password="new-password-123",
        remember_me=before.remember_me,
    )
    after = session_service.token_codec.verify_session(changed.session_token)

    assert changed.remember_me is True
    assert after.remember_me is True
    assert after.authenticated_at == clock.value
    assert after.authenticated_at != before.authenticated_at
    assert after.expires_at == clock.value + timedelta(days=30)


def test_legacy_v1_session_without_renewal_claims_stays_valid_without_refresh(
    session_service,
    clock,
):
    signed_up = session_service.signup(
        email="legacy-session@example.com",
        password="correct-password",
    )
    issued_at = int(clock.value.timestamp())
    base_payload = {
        "v": SESSION_TOKEN_VERSION,
        "kind": SESSION_TOKEN_KIND,
        "sub": str(signed_up.user.user_id),
        "email": signed_up.user.email,
        "iat": issued_at,
        "exp": issued_at + 3600,
        "jti": "legacy-v1-session-test",
        "sg": 0,
    }
    claim_variants = (
        {},
        {"remembered": True},
        {"auth_time": issued_at},
        {"remembered": True, "auth_time": "not-a-timestamp"},
    )
    for claims in claim_variants:
        payload = {**base_payload, **claims}
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _base64url_encode(
            session_service.token_codec._sign(encoded_payload.encode())
        )
        token = f"{encoded_payload}.{signature}"

        verified = session_service.token_codec.verify_session(token)
        user, refreshed = session_service.authenticate_session_with_refresh(token)
        assert verified.remember_me is False
        assert verified.authenticated_at is None
        assert user == signed_up.user
        assert refreshed is None


def test_logout_revokes_both_old_and_refreshed_remembered_tokens(
    session_service,
    clock,
):
    service = replace(
        session_service,
        token_codec=SessionTokenCodec(
            secret="remembered-logout-test-secret",
            ttl_seconds=10,
            remembered_ttl_seconds=100,
            remembered_refresh_threshold_seconds=20,
            remembered_absolute_ttl_seconds=250,
            now=clock.now,
        ),
    )
    initial = service.signup(
        email="remembered-logout@example.com",
        password="correct-password",
        remember_me=True,
    )
    clock.advance(timedelta(seconds=80))
    _, refreshed = service.authenticate_session_with_refresh(initial.session_token)
    assert refreshed is not None

    service.logout(initial.session_token)

    with pytest.raises(InvalidSessionError, match="revoked"):
        service.authenticate_session(initial.session_token)
    with pytest.raises(InvalidSessionError, match="revoked"):
        service.authenticate_session(refreshed.session_token)


def test_session_token_without_sg_claim_is_rejected_not_treated_as_generation_zero():
    # AUTH-08: the sibling RecentReauthenticationCodec.verify already fails
    # closed on a missing `sg` claim (payload.get("sg"), no default). This
    # codec used payload.get("sg", 0) instead, so a token missing `sg`
    # silently passed as generation 0 — which matches every user who has
    # never logged out/changed/reset their password. `issue` always sets
    # `sg`, so the only real path to a token missing it is a build that
    # predates the claim; there is no reason for the fail-open default to
    # sit right next to its fail-closed twin.
    codec = SessionTokenCodec(secret="sg-fallback-test-secret", ttl_seconds=3600)
    issued_at = int(datetime.now(UTC).timestamp())
    payload = {
        "v": SESSION_TOKEN_VERSION,
        "kind": SESSION_TOKEN_KIND,
        "sub": str(uuid4()),
        "email": "sg-missing@example.com",
        "iat": issued_at,
        "exp": issued_at + 3600,
        "jti": "no-sg-claim-test",
        # "sg" intentionally omitted — simulates a pre-AUTH-08 build's token.
    }
    encoded_payload = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _base64url_encode(codec._sign(encoded_payload.encode()))
    token = f"{encoded_payload}.{signature}"

    with pytest.raises(InvalidSessionError, match="generation is invalid"):
        codec.verify_session(token)


def test_recent_reauthentication_proof_rejects_tamper_expiry_and_wrong_password(
    session_service,
    clock,
):
    result = session_service.signup(
        email="reauth@example.com",
        password="correct-password",
    )
    with pytest.raises(InvalidCredentialsError):
        session_service.reauthenticate(
            user_id=result.user.user_id,
            current_password="wrong-password",
        )

    proof = session_service.reauthenticate(
        user_id=result.user.user_id,
        current_password="correct-password",
    )
    verified = session_service.verify_recent_reauthentication(
        proof_token=proof.proof_token,
        user_id=result.user.user_id,
    )
    assert verified.user_id == result.user.user_id
    assert verified.session_generation == 0
    payload, signature = proof.proof_token.split(".", maxsplit=1)
    with pytest.raises(RecentReauthenticationRequiredError):
        session_service.verify_recent_reauthentication(
            proof_token=f"{payload}x.{signature}",
            user_id=result.user.user_id,
        )

    clock.advance(timedelta(minutes=5))
    with pytest.raises(RecentReauthenticationRequiredError):
        session_service.verify_recent_reauthentication(
            proof_token=proof.proof_token,
            user_id=result.user.user_id,
        )


def test_logout_revokes_presented_session_token(session_service):
    result = session_service.signup(
        email="logout@example.com", password="logout-password-123"
    )

    session_service.logout(result.session_token)

    with pytest.raises(InvalidSessionError):
        session_service.authenticate_session(result.session_token)


def test_suspended_user_cannot_login_or_use_existing_session(
    session_factory,
    session_service,
):
    result = session_service.signup(
        email="suspend@example.com", password="secret-password-123"
    )
    with session_factory.begin() as session:
        user = session.get(User, result.user.user_id)
        assert user is not None
        user.status = "suspended"

    with pytest.raises(InvalidCredentialsError):
        session_service.login(email="suspend@example.com", password="wrong-pass")
    with pytest.raises(SuspendedUserError):
        session_service.login(
            email="suspend@example.com", password="secret-password-123"
        )
    with pytest.raises(SuspendedUserError):
        session_service.authenticate_session(result.session_token)


def test_short_password_is_rejected(session_service):
    with pytest.raises(PasswordPolicyError):
        session_service.signup(email="short@example.com", password="short")

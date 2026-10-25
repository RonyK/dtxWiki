# Budget exception: nullable native credentials and revocation need the existing
# session factory, but are verified here without repeating SSO HTTP round trips.
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.models.user import User
from backend.services.auth.password_method import DUMMY_PASSWORD_HASH
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import (
    InvalidCredentialsError,
    RecentReauthenticationRequiredError,
    SessionService,
    SessionTokenCodec,
)


def test_passwordless_user_requires_external_auth_and_generation_bound_step_up(
    session_factory,
):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    service = SessionService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=SessionTokenCodec(
            secret="external-login-session-test-secret",
            now=lambda: now,
        ),
    )
    with session_factory.begin() as session:
        user = service.provision_user(
            session, email="external@example.com", email_verified_at=now
        )
        result = service.issue_session(user, amr=("oidc",), idp="example")
        assert user.personal_workspace_id == user.default_workspace_id
        assert user.personal_workspace_id is not None
        proof = service.issue_external_reauthentication(
            session,
            user_id=user.id,
            session_generation=0,
            session_token=result.session_token,
        )
    assert result.user.has_password is False
    service.verify_recent_reauthentication(
        proof_token=proof.proof_token,
        user_id=result.user.user_id,
        scope="account",
        session_token=result.session_token,
    )

    hashes = []

    class AcceptingHasher:
        def verify_password(self, _password, password_hash):
            # Even a matching dummy hash must never authenticate a NULL row.
            hashes.append(password_hash)
            return True

    accepting = replace(service, credential_hasher=AcceptingHasher())
    with pytest.raises(InvalidCredentialsError):
        accepting.login(email="external@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        accepting.login(email="missing@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        accepting.change_password(
            user_id=result.user.user_id,
            current_password="correct-password",
            new_password="replacement-password",
        )
    with pytest.raises(InvalidCredentialsError):
        accepting.reauthenticate(
            user_id=result.user.user_id, current_password="correct-password"
        )
    assert hashes == [DUMMY_PASSWORD_HASH] * 4
    service.logout(result.session_token)
    with pytest.raises(RecentReauthenticationRequiredError):
        service.verify_recent_reauthentication(
            proof_token=proof.proof_token,
            user_id=result.user.user_id,
            scope="account",
            session_token=result.session_token,
        )
    with (
        pytest.raises(RecentReauthenticationRequiredError),
        session_factory.begin() as session,
    ):
        service.issue_external_reauthentication(
            session, user_id=result.user.user_id, session_generation=0
        )
    with session_factory() as session:
        assert session.get(User, result.user.user_id).password_hash is None

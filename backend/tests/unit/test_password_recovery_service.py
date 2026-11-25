import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import event, select

from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.models import Base
from backend.models.api_token import ApiToken
from backend.models.audit import AuditEvent
from backend.models.oauth import (
    OAuthAccessTokenJti,
    OAuthClient,
    OAuthCode,
    OAuthRefreshToken,
)
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.api_token_service import (
    ApiTokenService,
    InvalidApiTokenError,
    WorkspaceGrantRequest,
)
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService, OutboundMail
from backend.services.password_recovery_service import (
    ExpiredPasswordResetTokenError,
    InvalidPasswordResetTokenError,
    PasswordRecoveryService,
    UsedPasswordResetTokenError,
    password_reset_token_digest,
)
from backend.services.session_service import (
    InvalidSessionError,
    SessionService,
    SessionTokenCodec,
)
from backend.services.user_admin_service import UserAdminService

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
PUBLIC_BASE_URL = "https://wiki.example.com"


class _NoopMailProvider:
    def send(self, _message: OutboundMail) -> None:
        return None


def _recovery_service(session_factory, now=lambda: NOW):
    return PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=PUBLIC_BASE_URL,
        now=now,
    )


def _create_user(session_factory, email="recover@example.com"):
    return UserAdminService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
    ).add_user(
        email=email,
        password="original-password",
    )


def _default_workspace_id(session_factory, user_id):
    with session_factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        assert user.default_workspace_id is not None
        return user.default_workspace_id


def _token_from_url(reset_url: str) -> str:
    # M-2: an issued link carries its token in the fragment and nowhere else.
    parts = urlsplit(reset_url)
    assert parts.query == ""
    return parse_qs(parts.fragment, strict_parsing=True)["token"][0]


def _session_service(session_factory):
    return SessionService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=SessionTokenCodec(secret="password-recovery-test-secret"),
    )


def test_issue_stores_only_digest_and_reissue_revokes_previous(session_factory):
    user = _create_user(session_factory)
    service = _recovery_service(session_factory)

    first = service.issue_password_reset(email=user.email, actor_id="cli:operator")
    first_token = _token_from_url(first.reset_url)
    second = service.issue_password_reset(email=user.email, actor_id="cli:operator")
    second_token = _token_from_url(second.reset_url)

    assert first_token != second_token
    with session_factory() as session:
        model_user = session.get(User, user.user_id)
        first_row = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_digest
                == password_reset_token_digest(first_token)
            )
        )
        second_row = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_digest
                == password_reset_token_digest(second_token)
            )
        )
        audit_rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.password.reset.issued"
            )
        ).all()
        assert model_user is not None
        assert model_user.email_verified_at is None
        assert first_row is not None and first_row.revoked_at == NOW
        assert second_row is not None and second_row.revoked_at is None
        assert first_token not in first_row.token_digest
        assert second_token not in second_row.token_digest
        assert len(audit_rows) == 2
        assert all(
            first_token not in json.dumps(row.payload)
            and second_token not in json.dumps(row.payload)
            for row in audit_rows
        )


def test_issued_links_carry_the_token_only_in_the_fragment(session_factory):
    """M-2: the browser never sends a fragment, so it cannot reach a log.

    A `?token=` link put a live recovery credential into every uvicorn and
    reverse-proxy access log on the path. These links are what the CLI prints
    and a server admin copies, so their shape is the contract, not just the
    token the tests pull out of them.
    """

    user = _create_user(session_factory, "fragment-links@example.com")
    service = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=PUBLIC_BASE_URL,
        now=lambda: NOW,
        mail_delivery_service=EmailDeliveryService(
            settings=Settings(
                public_base_url=PUBLIC_BASE_URL,
                session_secret="fragment-link-secret-with-enough-entropy",
                mail_provider="smtp",
                mail_from_address="wiki@example.com",
                smtp_host="smtp.example.com",
            ),
            provider=_NoopMailProvider(),
        ),
    )

    reset = service.issue_password_reset(email=user.email)
    verification = service.issue_email_verification(
        user_id=user.user_id,
        actor_id=str(user.user_id),
    )

    for issued, path, prefix in (
        (reset, "/reset-password", "dtxr_"),
        (verification, "/verify-email", "dtxe_"),
    ):
        parts = urlsplit(issued.reset_url)
        assert (parts.scheme, parts.netloc, parts.path) == (
            "https",
            "wiki.example.com",
            path,
        )
        assert parts.query == ""
        assert parse_qs(parts.fragment, strict_parsing=True).keys() == {"token"}
        assert parse_qs(parts.fragment)["token"][0].startswith(prefix)

    # The fragment carries the live credential, not a decoy.
    service.peek_password_reset_token(token=_token_from_url(reset.reset_url))
    service.consume_email_verification(token=_token_from_url(verification.reset_url))


@pytest.mark.parametrize("purpose", ["password_reset", "email_verification"])
def test_consume_selects_user_between_token_lookup_and_token_claim(
    session_factory,
    purpose,
):
    user = _create_user(session_factory, f"lock-order-{purpose}@example.com")
    service = _recovery_service(session_factory)
    if purpose == "password_reset":
        issued = service.issue_password_reset(email=user.email)
    else:
        service = PasswordRecoveryService(
            session_factory=session_factory,
            credential_hasher=CredentialHasher(),
            public_base_url=PUBLIC_BASE_URL,
            now=lambda: NOW,
            mail_delivery_service=EmailDeliveryService(
                settings=Settings(
                    public_base_url=PUBLIC_BASE_URL,
                    session_secret="sqlite-lock-order-secret-with-enough-entropy",
                    mail_provider="smtp",
                    mail_from_address="wiki@example.com",
                    smtp_host="smtp.example.com",
                ),
                provider=_NoopMailProvider(),
            ),
        )
        issued = service.issue_email_verification(
            user_id=user.user_id,
            actor_id=str(user.user_id),
        )
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def capture_statement(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        token = _token_from_url(issued.reset_url)
        if purpose == "password_reset":
            service.consume_password_reset(
                token=token,
                new_password="replacement-password",
            )
        else:
            service.consume_email_verification(token=token)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    token_selects = [
        index
        for index, statement in enumerate(statements)
        if "password_reset_token" in statement
    ]
    user_selects = [
        index
        for index, statement in enumerate(statements)
        if 'from "user"' in statement or "from user" in statement
    ]
    assert len(token_selects) >= 2
    assert user_selects
    assert token_selects[0] < user_selects[0] < token_selects[1]


@pytest.mark.parametrize(
    ("revoke_api_tokens", "expected_prefix", "tampered_prefix"),
    [
        (False, "dtxr_0_", "dtxr_1_"),
        (True, "dtxr_1_", "dtxr_0_"),
    ],
)
def test_token_self_describes_issue_scope_and_flag_tampering_is_rejected(
    session_factory,
    revoke_api_tokens: bool,
    expected_prefix: str,
    tampered_prefix: str,
):
    user = _create_user(session_factory, f"scope-{int(revoke_api_tokens)}@example.com")
    service = _recovery_service(session_factory)
    issued = service.issue_password_reset(
        email=user.email,
        revoke_api_tokens=revoke_api_tokens,
    )
    token = _token_from_url(issued.reset_url)
    tampered = tampered_prefix + token[len(expected_prefix) :]

    assert token.startswith(expected_prefix)
    assert len(token[len(expected_prefix) :]) >= 43
    assert password_reset_token_digest(tampered) != password_reset_token_digest(token)
    with pytest.raises(InvalidPasswordResetTokenError):
        service.consume_password_reset(
            token=tampered,
            new_password="replacement-password",
        )


def test_public_requested_via_forces_revocation_regardless_of_caller_supplied_flag(
    session_factory,
):
    # AUTH-01: requested_via="public" is the "I may be compromised" lane.
    # _issue_password_reset_serialized must force revoke_api_tokens=True at
    # the service boundary — ignoring whatever the caller passed — so that
    # (a) every current/future caller using requested_via="public" is
    # protected without having to remember the flag, and (b) a caller that
    # explicitly asks for revoke_api_tokens=False on the public lane cannot
    # opt an attacker's stolen PAT/OAuth grants out of revocation. The
    # server_admin/cli lanes are untouched by this branch (see the
    # parametrized test above, which pins the cli-default lane still honors
    # the caller-supplied flag).
    user = _create_user(session_factory, "public-force@example.com")
    service = _recovery_service(session_factory)

    issued = service.issue_password_reset(
        email=user.email,
        requested_via="public",
        revoke_api_tokens=False,  # explicitly opting OUT — must be ignored
    )
    token = _token_from_url(issued.reset_url)

    assert token.startswith("dtxr_1_")
    with session_factory() as session:
        token_row = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user.user_id,
                PasswordResetToken.purpose == "password_reset",
            )
        )
        assert token_row is not None
        assert token_row.revoke_api_tokens is True


def test_consume_is_single_use_revokes_sessions_and_keeps_api_tokens_for_cli_default(
    session_factory,
):
    # Scope note (AUTH-01): this pins the CLI/operator-tool lane specifically
    # (requested_via="cli", made explicit below rather than left as an
    # implicit default) — an operator running `dtxwiki users reset-password`
    # without asking for token revocation should not silently nuke every
    # PAT/MCP integration for that user. That default is unchanged and
    # correct. It must NOT be read as covering the public self-service lane:
    # requested_via="public" now force-revokes unconditionally regardless of
    # the caller-supplied flag — see
    # test_public_requested_via_forces_revocation_regardless_of_caller_supplied_flag
    # above, which pins that lane separately.
    user = _create_user(session_factory)
    session_service = _session_service(session_factory)
    existing_session = session_service.login(
        email=user.email,
        password="original-password",
    )
    api_service = ApiTokenService(session_factory, CredentialHasher())
    pat = api_service.create_token(
        user_id=user.user_id,
        workspace_grants=[
            WorkspaceGrantRequest(
                workspace_id=_default_workspace_id(session_factory, user.user_id)
            )
        ],
        name="recovery-default",
        scopes=["mcp:read"],
    )
    service = _recovery_service(session_factory)
    issued = service.issue_password_reset(email=user.email, requested_via="cli")
    token = _token_from_url(issued.reset_url)

    consumed = service.consume_password_reset(
        token=token,
        new_password="new-pass",
    )

    assert consumed.revoked_credentials == {
        "api_tokens": 0,
        "oauth_refresh_tokens": 0,
        "oauth_access_tokens": 0,
        "oauth_authorization_codes": 0,
    }
    with pytest.raises(InvalidSessionError, match="session token has been revoked"):
        session_service.authenticate_session(existing_session.session_token)
    assert session_service.login(
        email=user.email,
        password="new-pass",
    ).user.user_id == user.user_id
    assert api_service.authenticate_token(pat.plain_token).user.user_id == user.user_id
    with pytest.raises(UsedPasswordResetTokenError):
        service.consume_password_reset(
            token=token,
            new_password="another-password",
        )


@pytest.mark.parametrize(
    ("forced_at_issue", "requested_at_consume"),
    [(True, False), (False, True)],
)
def test_consume_optionally_revokes_pat_and_oauth_credentials(
    session_factory,
    forced_at_issue: bool,
    requested_at_consume: bool,
):
    user = _create_user(session_factory, "compromised@example.com")
    api_service = ApiTokenService(session_factory, CredentialHasher())
    pat = api_service.create_token(
        user_id=user.user_id,
        workspace_grants=[
            WorkspaceGrantRequest(
                workspace_id=_default_workspace_id(session_factory, user.user_id)
            )
        ],
        name="compromised-token",
        scopes=["mcp:read", "mcp:write"],
    )
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        client = OAuthClient(
            id="recovery-test-client",
            client_name="Recovery test",
            redirect_uris=["http://127.0.0.1/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="mcp:read",
        )
        session.add(client)
        refresh = OAuthRefreshToken(
            token_lookup_hash="recovery-refresh-lookup",
            hashed_token="recovery-refresh-hash",
            user_id=user.user_id,
            client_id=client.id,
            scope="mcp:read",
            expires_at=NOW + timedelta(hours=1),
        )
        session.add(refresh)
        session.flush()
        session.add_all(
            [
                OAuthAccessTokenJti(
                    jti="recovery-access-jti",
                    refresh_token_id=refresh.id,
                    user_id=user.user_id,
                    client_id=client.id,
                    expires_at=NOW + timedelta(hours=1),
                ),
                OAuthCode(
                    code_hash="recovery-code-hash",
                    client_id=client.id,
                    user_id=user.user_id,
                    redirect_uri="http://127.0.0.1/callback",
                    code_challenge="challenge",
                    scope="mcp:read",
                    expires_at=NOW + timedelta(minutes=10),
                ),
            ]
        )

    service = _recovery_service(session_factory)
    issued = service.issue_password_reset(
        email=user.email,
        revoke_api_tokens=forced_at_issue,
    )
    consumed = service.consume_password_reset(
        token=_token_from_url(issued.reset_url),
        new_password="replacement-password",
        revoke_api_tokens=requested_at_consume,
    )

    assert consumed.revoked_credentials == {
        "api_tokens": 1,
        "oauth_refresh_tokens": 1,
        "oauth_access_tokens": 1,
        "oauth_authorization_codes": 1,
    }
    with pytest.raises(InvalidApiTokenError):
        api_service.authenticate_token(pat.plain_token)
    with session_factory() as session:
        assert session.get(ApiToken, pat.api_token.id).revoked_at == NOW
        assert session.get(OAuthRefreshToken, refresh.id).revoked_at == NOW
        assert session.get(OAuthAccessTokenJti, "recovery-access-jti").revoked_at == NOW
        assert session.get(OAuthCode, "recovery-code-hash").consumed_at == NOW


def test_expired_token_does_not_change_password_or_generation(session_factory):
    current = [NOW]
    user = _create_user(session_factory, "expired@example.com")
    service = _recovery_service(session_factory, now=lambda: current[0])
    issued = service.issue_password_reset(
        email=user.email,
        expires_in_seconds=5 * 60,
    )
    current[0] = NOW + timedelta(minutes=5, seconds=1)

    with pytest.raises(ExpiredPasswordResetTokenError):
        service.consume_password_reset(
            token=_token_from_url(issued.reset_url),
            new_password="replacement-password",
        )

    with session_factory() as session:
        model_user = session.get(User, user.user_id)
        token_row = session.get(PasswordResetToken, issued.token_id)
        assert model_user is not None and model_user.session_generation == 0
        assert CredentialHasher().verify_password(
            "original-password",
            model_user.password_hash,
        )
        assert token_row is not None and token_row.used_at is None


def test_concurrent_consume_has_exactly_one_winner(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'concurrent-reset.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    user = _create_user(session_factory, "concurrent@example.com")
    service = _recovery_service(session_factory)
    token = _token_from_url(
        service.issue_password_reset(email=user.email).reset_url
    )
    barrier = Barrier(2, timeout=30)

    def attempt(password: str) -> str:
        barrier.wait()
        try:
            _recovery_service(session_factory).consume_password_reset(
                token=token,
                new_password=password,
            )
            return "succeeded"
        except InvalidPasswordResetTokenError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(attempt, ["first-winner-password", "second-winner-password"])
        )

    assert sorted(outcomes) == ["rejected", "succeeded"]


def test_concurrent_reissue_keeps_exactly_one_active_link(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'concurrent-reissue.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    user = _create_user(session_factory, "concurrent-reissue@example.com")
    barrier = Barrier(2, timeout=30)

    def issue() -> str:
        barrier.wait()
        issued = _recovery_service(session_factory).issue_password_reset(
            email=user.email,
        )
        return _token_from_url(issued.reset_url)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _index: issue(), range(2)))

    with session_factory() as session:
        rows = list(
            session.scalars(
                select(PasswordResetToken).where(
                    PasswordResetToken.user_id == user.user_id,
                    PasswordResetToken.purpose == "password_reset",
                )
            ).all()
        )
    active_rows = [
        row for row in rows if row.used_at is None and row.revoked_at is None
    ]
    assert len(rows) == 2
    assert len(active_rows) == 1
    active_digest = active_rows[0].token_digest
    active_token = next(
        token for token in tokens if password_reset_token_digest(token) == active_digest
    )
    replaced_token = next(token for token in tokens if token != active_token)

    with pytest.raises(InvalidPasswordResetTokenError):
        _recovery_service(session_factory).consume_password_reset(
            token=replaced_token,
            new_password="replacement-password",
        )
    _recovery_service(session_factory).consume_password_reset(
        token=active_token,
        new_password="replacement-password",
    )

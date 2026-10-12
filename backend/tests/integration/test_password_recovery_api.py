from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.api_token import ApiToken
from backend.models.mail import EmailOutbox, PasswordRecoveryRateLimitSlot
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService, OutboundMail
from backend.services.password_recovery_service import PasswordRecoveryService


class RecordingProvider:
    def __init__(self) -> None:
        self.messages: list[OutboundMail] = []

    def send(self, message: OutboundMail) -> None:
        self.messages.append(message)


def recovery_client(page_service):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="password-recovery-api-test-secret",
        public_base_url="https://wiki.example.com",
        session_cookie_secure=False,
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )
    app = create_app(settings=settings, page_service=page_service)
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=settings, provider=provider)
    app.state.password_recovery_service = PasswordRecoveryService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
    )
    return TestClient(app), delivery, provider


def signup(client: TestClient, email: str) -> str:
    result = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-password"},
    )
    assert result.status_code == 201, result.text
    return result.json()["data"]["user"]["user_id"]


def link_token(url: str) -> str:
    """The token of an issued recovery link, which carries it in the fragment
    and nowhere else (M-2)."""

    parts = urlsplit(url)
    assert parts.query == ""
    return parse_qs(parts.fragment, strict_parsing=True)["token"][0]


def check_reset_status(client: TestClient, token: str):
    return client.post("/api/v1/auth/password-reset/status", json={"token": token})


def pin_recovery_clock(client: TestClient, now: datetime) -> PasswordRecoveryService:
    """The app's recovery service with its clock held at ``now``.

    The host clock steps (NTP, a resumed VM), so a test that backdates a link
    against one read of it and has the service judge the link against another
    leaves the link on either side of its own deadline. The service reads its
    clock through a ``now`` callable bound when it is built, so patching
    ``utc_now`` afterwards would not reach it; the pinned copy replaces the
    instance the routes resolve from ``app.state``.
    """

    service = replace(client.app.state.password_recovery_service, now=lambda: now)
    client.app.state.password_recovery_service = service
    return service


def test_public_request_is_uniform_and_only_verified_active_user_gets_token(
    page_service,
):
    client, _, _ = recovery_client(page_service)
    signup(client, "verified@example.com")
    signup(client, "unverified@example.com")
    signup(client, "inactive@example.com")
    with page_service.session_factory.begin() as session:
        verified = session.scalar(
            select(User).where(User.email == "verified@example.com")
        )
        inactive = session.scalar(
            select(User).where(User.email == "inactive@example.com")
        )
        assert verified is not None and inactive is not None
        verified.email_verified_at = datetime.now(UTC)
        inactive.status = "inactive"

    responses = [
        client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": email},
        )
        for email in (
            "verified@example.com",
            "unverified@example.com",
            "inactive@example.com",
            "missing@example.com",
        )
    ]

    assert {response.status_code for response in responses} == {202}
    assert len({str(response.json()["data"]) for response in responses}) == 1
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in responses
    )
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 1


def test_public_request_forces_credential_revocation_regardless_of_confirm_body(
    page_service,
):
    # AUTH-01: the public self-service lane is the "I may be compromised" lane.
    # It must revoke PAT/OAuth credentials unconditionally at issue time
    # (encoded in the dtxr_1_ token prefix), not merely default a UI checkbox —
    # a caller can always send revoke_api_tokens=False explicitly, and the
    # attacker's stolen PAT must still die.
    client, delivery, _ = recovery_client(page_service)
    user_id = signup(client, "compromised@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "compromised@example.com")
        )
        assert user is not None
        assert user.default_workspace_id is not None
        workspace_id = user.default_workspace_id
        user.email_verified_at = datetime.now(UTC)
    pat = ApiTokenService(
        page_service.session_factory, CredentialHasher()
    ).create_token(
        user_id=UUID(user_id),
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name="attacker-held-token",
        scopes=["mcp:read"],
    )

    requested = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "compromised@example.com"},
    )
    assert requested.status_code == 202

    with page_service.session_factory() as session:
        token_row = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == UUID(user_id),
                PasswordResetToken.purpose == "password_reset",
            )
        )
        assert token_row is not None
        assert token_row.revoke_api_tokens is True
        outbox = session.scalar(
            select(EmailOutbox).where(
                EmailOutbox.message_kind == "password_reset"
            )
        )
        assert outbox is not None
        raw_token = delivery.cipher.decrypt(
            outbox_id=outbox.id,
            message_kind=outbox.message_kind,
            envelope=outbox.encrypted_token,
        )
    assert raw_token.startswith("dtxr_1_")

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": raw_token,
            "new_password": "replacement-password",
            # A caller that explicitly opts OUT must still be overridden by the
            # issue-time force — this is the crux of the fix.
            "revoke_api_tokens": False,
        },
    )
    assert confirmed.status_code == 200

    with page_service.session_factory() as session:
        revoked = session.get(ApiToken, pat.api_token.id)
        assert revoked is not None
        assert revoked.revoked_at is not None


def test_confirm_request_schema_defaults_to_revoking_credentials():
    # AUTH-01 defense in depth: a caller that omits revoke_api_tokens entirely
    # (bypassing the SPA, which always sends an explicit value) must fail
    # closed to revocation, not silently preserve credentials.
    from backend.schemas.auth import PasswordResetConfirmRequest

    request = PasswordResetConfirmRequest(
        token="a" * 20,
        new_password="replacement-password",
    )
    assert request.revoke_api_tokens is True


def test_public_request_ignores_host_header_when_rendering_reset_link(
    page_service, monkeypatch
):
    # Let the foreign name past the Host allowlist (which would refuse it
    # with 400): even an accepted host name must not reach the reset link.
    monkeypatch.setenv("DTXWIKI_ALLOWED_HOSTS", "testserver,attacker.example")
    client, delivery, provider = recovery_client(page_service)
    signup(client, "canonical-url@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "canonical-url@example.com")
        )
        assert user is not None
        user.email_verified_at = datetime.now(UTC)

    requested = client.post(
        "/api/v1/auth/password-reset/request",
        headers={"host": "attacker.example"},
        json={"email": "canonical-url@example.com"},
    )

    assert requested.status_code == 202
    with page_service.session_factory() as session:
        outbox = session.scalar(
            select(EmailOutbox).where(
                EmailOutbox.message_kind == "password_reset"
            )
        )
        assert outbox is not None
        outbox_id = outbox.id
    # deliver() owns its own transactions now (MAIL-01): the provider send
    # must not happen inside one.
    delivery.deliver(
        session_factory=page_service.session_factory, outbox_id=outbox_id
    )

    assert len(provider.messages) == 1
    body = provider.messages[0].text_body
    assert "https://wiki.example.com/reset-password#token=dtxr_" in body
    assert "?token=" not in body
    assert "attacker.example" not in body


def test_email_verification_request_and_single_use_confirm(page_service):
    client, delivery, _ = recovery_client(page_service)
    user_id = signup(client, "verify-me@example.com")

    requested = client.post("/api/v1/auth/email-verification/request")

    assert requested.status_code == 202
    assert requested.json()["data"] == {"requested": True, "verified": False}
    assert "dtxe_" not in requested.text
    with page_service.session_factory() as session:
        outbox = session.scalar(
            select(EmailOutbox).where(
                EmailOutbox.message_kind == "email_verification"
            )
        )
        assert outbox is not None and outbox.encrypted_token is not None
        raw_token = delivery.cipher.decrypt(
            outbox_id=outbox.id,
            message_kind=outbox.message_kind,
            envelope=outbox.encrypted_token,
        )

    confirmed = client.post(
        "/api/v1/auth/email-verification/confirm",
        json={"token": raw_token},
    )
    replay = client.post(
        "/api/v1/auth/email-verification/confirm",
        json={"token": raw_token},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["data"] == {"requested": False, "verified": True}
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "invalid_password_reset_token"
    with page_service.session_factory() as session:
        user = session.get(User, UUID(user_id))
        assert user is not None and user.email_verified_at is not None


def _outbox_count(page_service, message_kind: str) -> int:
    with page_service.session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(EmailOutbox)
                .where(EmailOutbox.message_kind == message_kind)
            )
            or 0
        )


def test_email_verification_request_is_rate_limited_per_account(page_service):
    # AUTH-02: unlike its twin /password-reset/request, this endpoint used to
    # consume no rate-limit bucket at all — an authenticated caller could
    # flood EmailOutbox/Job rows (and mail-bomb a third-party address for an
    # open-signup deployment) with unlimited resend calls. The tighter
    # per-account resend cap (3/hour) is the binding constraint here — it is
    # stricter than the shared ip/email throttle (4/15min) mirrored from the
    # reset lane, so it trips first.
    from backend.services.password_recovery_service import (
        EMAIL_VERIFICATION_RESEND_RATE_LIMIT_PER_EMAIL,
    )

    client, _, _ = recovery_client(page_service)
    signup(client, "resend-flood@example.com")

    for _ in range(EMAIL_VERIFICATION_RESEND_RATE_LIMIT_PER_EMAIL):
        attempt = client.post("/api/v1/auth/email-verification/request")
        assert attempt.status_code == 202

    before_blocked = _outbox_count(page_service, "email_verification")
    blocked = client.post("/api/v1/auth/email-verification/request")

    assert blocked.status_code == 422
    assert blocked.json()["error_code"] == "password_recovery_rate_limited"
    # The excess call must not have created another EmailOutbox/Job row.
    assert _outbox_count(page_service, "email_verification") == before_blocked


def test_email_verification_rate_limit_lane_is_isolated_from_password_reset(
    page_service,
):
    # AUTH-02/AUTH-06: reusing the reset limiter's bucket digest verbatim
    # (without a `purpose` dimension) would let an anonymous attacker drain a
    # victim's email-keyed reset-lane quota by spraying
    # /password-reset/request, and that would also silently exhaust the
    # victim's own verification-resend quota. The two lanes must stay on
    # disjoint buckets.
    from backend.services.password_recovery_service import (
        PASSWORD_RECOVERY_RATE_LIMIT_PER_EMAIL,
    )

    client, _, _ = recovery_client(page_service)
    signup(client, "lane-isolation@example.com")
    # The victim's email is intentionally left unverified: the reset-lane
    # rate-limit bucket is consumed before eligibility is checked, so an
    # attacker can drain it against any address regardless of eligibility —
    # and leaving it unverified keeps this user's own verification-request
    # path live (an already-verified user short-circuits without queuing
    # mail, which would make this test assert the wrong thing).

    # Drain the reset lane's per-email bucket for this victim address.
    for _ in range(PASSWORD_RECOVERY_RATE_LIMIT_PER_EMAIL):
        reset_attempt = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "lane-isolation@example.com"},
        )
        assert reset_attempt.status_code == 202

    before = _outbox_count(page_service, "email_verification")
    verification_attempt = client.post("/api/v1/auth/email-verification/request")

    assert verification_attempt.status_code == 202
    assert _outbox_count(page_service, "email_verification") == before + 1


def test_unregistered_address_spray_never_touches_global_backstop(page_service):
    # AUTH-06: the global backstop must only be claimed once a request is
    # known to produce a delivery. An implementation that still claims it
    # unconditionally lets an attacker who stays under the per-ip/per-email
    # caps (by spraying many distinct *unregistered* addresses) burn through
    # the single global key and silently starve every real user's recovery
    # for the rest of the window.
    from backend.services.password_recovery_service import (
        PASSWORD_RECOVERY_RATE_LIMIT_PER_IP,
    )

    client, _, _ = recovery_client(page_service)

    request_count = min(6, PASSWORD_RECOVERY_RATE_LIMIT_PER_IP - 1)
    for index in range(request_count):
        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": f"unregistered-{index}@example.com"},
        )
        assert response.status_code == 202

    with page_service.session_factory() as session:
        global_rows = session.scalar(
            select(func.count())
            .select_from(PasswordRecoveryRateLimitSlot)
            .where(PasswordRecoveryRateLimitSlot.bucket_kind == "global")
        )
    assert global_rows == 0


def test_password_reset_confirm_distinguishes_expired_and_used_links(page_service):
    client, _, _ = recovery_client(page_service)
    signup(client, "reset-state@example.com")
    # The expired link is backdated one second before this "now" and judged
    # against the same "now"; the same pin keeps the 30-minute link issued for
    # the "used" half live until its first use.
    now = datetime.now(UTC)
    service = pin_recovery_clock(client, now)

    expired_issue = service.issue_password_reset(email="reset-state@example.com")
    expired_token = link_token(expired_issue.reset_url)
    with page_service.session_factory.begin() as session:
        token_row = session.get(PasswordResetToken, expired_issue.token_id)
        assert token_row is not None
        token_row.expires_at = now - timedelta(seconds=1)

    expired = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": expired_token,
            "new_password": "replacement-password",
        },
    )

    assert expired.status_code == 400
    assert expired.json()["error_code"] == "password_reset_token_expired"
    assert expired.headers["cache-control"] == "no-store"

    used_issue = service.issue_password_reset(email="reset-state@example.com")
    used_token = link_token(used_issue.reset_url)
    first_use = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": used_token,
            "new_password": "replacement-password",
        },
    )
    replay = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": used_token,
            "new_password": "another-password",
        },
    )

    assert first_use.status_code == 200
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "password_reset_token_used"
    assert replay.headers["cache-control"] == "no-store"


def test_password_reset_status_distinguishes_valid_expired_used_and_malformed(
    page_service,
):
    # M-23 (260908 UI audit): the reset screen used to show the new-password
    # form for a dead link and only surface that after the visitor filled it
    # out and submitted. This read-only endpoint lets the screen check first.
    client, _, _ = recovery_client(page_service)
    signup(client, "reset-status@example.com")
    # Expiry is judged against the same "now" the expired link is backdated
    # from, and the fresh link cannot lapse between issue and use.
    now = datetime.now(UTC)
    service = pin_recovery_clock(client, now)

    fresh_issue = service.issue_password_reset(email="reset-status@example.com")
    fresh_token = link_token(fresh_issue.reset_url)
    fresh = check_reset_status(client, fresh_token)
    assert fresh.status_code == 200
    assert fresh.json()["data"] == {"valid": True}
    assert fresh.headers["cache-control"] == "no-store"

    # The status check must not itself consume the token -- confirming with
    # the real password afterward has to still work.
    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": fresh_token, "new_password": "replacement-password"},
    )
    assert confirmed.status_code == 200

    expired_issue = service.issue_password_reset(email="reset-status@example.com")
    expired_token = link_token(expired_issue.reset_url)
    with page_service.session_factory.begin() as session:
        token_row = session.get(PasswordResetToken, expired_issue.token_id)
        assert token_row is not None
        token_row.expires_at = now - timedelta(seconds=1)
    expired = check_reset_status(client, expired_token)
    assert expired.status_code == 400
    assert expired.json()["error_code"] == "password_reset_token_expired"
    assert expired.headers["cache-control"] == "no-store"

    used_issue = service.issue_password_reset(email="reset-status@example.com")
    used_token = link_token(used_issue.reset_url)
    client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": used_token, "new_password": "another-password"},
    )
    used = check_reset_status(client, used_token)
    assert used.status_code == 400
    assert used.json()["error_code"] == "password_reset_token_used"

    malformed = check_reset_status(client, "not-a-real-token")
    assert malformed.status_code == 400
    assert malformed.json()["error_code"] == "invalid_password_reset_token"


def _issue_recovery_tokens(client: TestClient, email: str):
    """A live reset token and a live email-verification token for one user."""

    user_id = signup(client, email)
    service = client.app.state.password_recovery_service
    reset_token = link_token(service.issue_password_reset(email=email).reset_url)
    verification_token = link_token(
        service.issue_email_verification(
            user_id=UUID(user_id),
            actor_id=user_id,
        ).reset_url
    )
    return UUID(user_id), reset_token, verification_token


def _assert_query_token_refused(response) -> None:
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "invalid_password_reset_request"
    assert response.headers["cache-control"] == "no-store"


def test_reset_status_is_post_only_and_never_reads_a_query_token(page_service):
    """M-2: `GET /password-reset/status?token=` wrote the token into every
    access log on the path. The check is a POST with a JSON body now; the GET
    is gone, and a POST that also carries the token in the URL is refused
    rather than quietly answered."""

    client, _, _ = recovery_client(page_service)
    _, reset_token, _ = _issue_recovery_tokens(client, "status-query@example.com")
    status_path = "/api/v1/auth/password-reset/status"

    # The /api fallback answers an unrouted method with not_found.
    legacy_get = client.get(status_path, params={"token": reset_token})
    assert legacy_get.status_code in {404, 405}
    assert "valid" not in legacy_get.json().get("data", {})

    _assert_query_token_refused(
        client.post(status_path, params={"token": reset_token})
    )
    # Not even alongside a valid body: a client that leaks the token into the
    # URL must fail, not work.
    _assert_query_token_refused(
        client.post(
            status_path,
            params={"token": reset_token},
            json={"token": reset_token},
        )
    )

    # Anti-vacuity: the token itself is live, so the refusals above are about
    # where it was sent and nothing else.
    assert check_reset_status(client, reset_token).status_code == 200


def test_recovery_submissions_refuse_a_token_in_the_query_string(page_service):
    """M-2: the confirm endpoints take the token from the JSON body only. One
    sent in the URL is refused before anything is consumed, so it neither
    resets the password nor verifies the address."""

    client, _, _ = recovery_client(page_service)
    user_id, reset_token, verification_token = _issue_recovery_tokens(
        client, "submit-query@example.com"
    )

    _assert_query_token_refused(
        client.post(
            "/api/v1/auth/password-reset/confirm",
            params={"token": reset_token},
            json={"new_password": "replacement-password"},
        )
    )
    _assert_query_token_refused(
        client.post(
            "/api/v1/auth/password-reset/confirm",
            params={"token": reset_token},
            json={"token": reset_token, "new_password": "replacement-password"},
        )
    )
    _assert_query_token_refused(
        client.post(
            "/api/v1/auth/email-verification/confirm",
            params={"token": verification_token},
        )
    )
    _assert_query_token_refused(
        client.post(
            "/api/v1/auth/email-verification/confirm",
            params={"token": verification_token},
            json={"token": verification_token},
        )
    )

    with page_service.session_factory() as session:
        user = session.get(User, user_id)
        assert user is not None and user.email_verified_at is None
        assert session.scalar(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_not(None),
            )
        ) == 0

    # Both tokens still redeem through the body, so nothing above was a
    # rejection of the token itself.
    assert client.post(
        "/api/v1/auth/email-verification/confirm",
        json={"token": verification_token},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "replacement-password"},
    ).status_code == 200


@pytest.mark.parametrize("message_kind", ["password_reset", "email_verification"])
def test_emailed_recovery_links_carry_the_token_only_in_the_fragment(
    page_service,
    message_kind,
):
    client, delivery, provider = recovery_client(page_service)
    email = f"emailed-{message_kind}@example.com"
    signup(client, email)
    if message_kind == "password_reset":
        with page_service.session_factory.begin() as session:
            user = session.scalar(select(User).where(User.email == email))
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
        requested = client.post(
            "/api/v1/auth/password-reset/request", json={"email": email}
        )
        path = "/reset-password"
    else:
        requested = client.post("/api/v1/auth/email-verification/request")
        path = "/verify-email"
    assert requested.status_code == 202
    with page_service.session_factory() as session:
        outbox = session.scalar(
            select(EmailOutbox).where(EmailOutbox.message_kind == message_kind)
        )
        assert outbox is not None
        outbox_id = outbox.id
        raw_token = delivery.cipher.decrypt(
            outbox_id=outbox.id,
            message_kind=outbox.message_kind,
            envelope=outbox.encrypted_token,
        )

    delivery.deliver(session_factory=page_service.session_factory, outbox_id=outbox_id)

    assert len(provider.messages) == 1
    links = [
        word
        for word in provider.messages[0].text_body.split()
        if word.startswith("https://")
    ]
    assert len(links) == 1
    parts = urlsplit(links[0])
    assert (parts.netloc, parts.path, parts.query) == ("wiki.example.com", path, "")
    assert parse_qs(parts.fragment, strict_parsing=True) == {"token": [raw_token]}


def test_capability_distinguishes_email_admin_assisted_and_disabled(page_service):
    # The route projects app.state.settings at request time, so one app serves
    # every mode; the email mode comes from the real recovery_client wiring.
    client, _, _ = recovery_client(page_service)
    email = client.get("/api/v1/auth/capabilities")
    client.app.state.settings = Settings(
        session_secret="admin-assisted-test-secret",
        public_base_url="https://wiki.example.com",
    )
    admin_only = client.get("/api/v1/auth/capabilities")
    client.app.state.settings = Settings(session_secret="disabled-recovery-test-secret")
    disabled = client.get("/api/v1/auth/capabilities")

    assert email.json()["data"]["password_recovery"] == {
        "mode": "email",
        "admin_assisted": True,
    }
    assert admin_only.json()["data"]["password_recovery"] == {
        "mode": "admin_assisted",
        "admin_assisted": True,
    }
    assert disabled.json()["data"]["password_recovery"] == {
        "mode": "disabled",
        "admin_assisted": False,
    }


def test_production_recovery_factory_accepts_secure_canonical_url(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="secure-recovery-factory-test-secret",
                public_base_url="https://wiki.example.com",
                session_cookie_secure=False,
            ),
            page_service=page_service,
        )
    )

    capabilities = client.get("/api/v1/auth/capabilities")
    invalid_reset = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": f"dtxr_0_{'a' * 43}",
            "new_password": "replacement-password",
        },
    )

    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["password_recovery"]["mode"] == (
        "admin_assisted"
    )
    assert invalid_reset.status_code == 400


def test_production_recovery_factory_disables_links_for_allowed_http(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="insecure-recovery-factory-test-secret",
                public_base_url="http://wiki.example.com",
                allow_insecure_http=True,
                session_cookie_secure=False,
            ),
            page_service=page_service,
        )
    )

    capabilities = client.get("/api/v1/auth/capabilities")
    requested = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "nobody@example.com"},
    )
    invalid_reset = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": f"dtxr_0_{'a' * 43}",
            "new_password": "replacement-password",
        },
    )
    signup_result = client.post(
        "/api/v1/auth/signup",
        json={"email": "http-recovery@example.com", "password": "correct-password"},
    )
    verification_request = client.post("/api/v1/auth/email-verification/request")
    invalid_verification = client.post(
        "/api/v1/auth/email-verification/confirm",
        json={"token": f"dtxe_{'a' * 43}"},
    )

    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["password_recovery"] == {
        "mode": "disabled",
        "admin_assisted": False,
    }
    assert requested.status_code == 202
    assert invalid_reset.status_code == 400
    assert signup_result.status_code == 201
    assert verification_request.status_code == 422
    assert invalid_verification.status_code == 400

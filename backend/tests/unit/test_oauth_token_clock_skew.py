"""An access JWT must survive a verifier clock that trails its issuer's.

``_issue_tokens`` stamps ``iat`` from the wall clock when it mints a token.
PyJWT refuses a token whose ``iat`` is ahead of the verifier's clock, so after
a backward wall-clock step (an NTP step correction, a VM resume), or on an app
node whose clock trails the issuer's, a token minted a moment earlier failed
with "The token is not yet valid (iat)": ``verify_access_token`` answered 401
and ``/oauth/revoke`` answered "revoke auth failed".

The policy under test: ``iat`` is issuance metadata, not a not-before gate. The
server mints tokens that are valid at once and writes no ``nbf``, so its three
decode sites skip ``iat`` verification and nothing else. ``exp`` still ends
authentication with no leeway, ``nbf`` is still honoured when a token carries
one, and signature, algorithm, issuer and audience checks are unchanged.

Every test moves ONE controlled wall clock that both the issuer (``utc_now``)
and PyJWT's verifier read, so nothing here sleeps or depends on the host
clock. ``T0`` lies years ahead of the real clock on purpose: if PyJWT ever
stopped reading the patched clock, the real clock would never reach an ``exp``
and would always trail an ``nbf``, so the expiry and not-before tests would
fail loudly instead of passing for the wrong reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from backend.app.settings import Settings
from backend.models.oauth import OAuthAccessTokenJti
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.services import oauth_service
from backend.services.oauth_service import (
    InvalidOAuthGrantError,
    OAuthService,
    OAuthTokenResult,
    challenge_s256,
)

# An integral epoch second: tokens minted "at T0" get iat == T0 exactly.
T0 = 2_000_000_000
ACCESS_TTL_SECONDS = 3600
SCOPE = "mcp:read mcp:write"
REDIRECT_URI = "http://127.0.0.1:53682/callback"
CODE_VERIFIER = "v" * 64


class WallClock:
    """The single wall clock the issuer and the verifier both read."""

    def __init__(self, epoch: float) -> None:
        self.set(epoch)

    def set(self, epoch: float) -> None:
        self.epoch = epoch
        self._now = datetime.fromtimestamp(epoch, UTC)

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def clock(monkeypatch) -> WallClock:
    wall = WallClock(T0)

    class VerifierNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall.now() if tz is None else wall.now().astimezone(tz)

    # The issuer stamps iat/exp from oauth_service.utc_now. PyJWT's verifier
    # reads its own clock (the ``datetime`` imported into jwt.api_jwt), which a
    # test cannot otherwise move short of sleeping or stepping the host clock.
    monkeypatch.setattr(oauth_service, "utc_now", wall.now)
    monkeypatch.setattr(jwt.api_jwt, "datetime", VerifierNow)
    return wall


@pytest.fixture(scope="module")
def attacker_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class Issuer:
    """A real OAuthService over an in-memory database: one active user, one
    registered client and a live consent, minting through the production
    authorize + exchange path so signing, JTI persistence and denylist checks
    are all the real ones."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.service = OAuthService(
            session_factory=session_factory,
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                oauth_access_token_ttl_seconds=ACCESS_TTL_SECONDS,
            ),
            public_base_url="http://testserver",
        )
        with session_factory.begin() as session:
            workspace = session.scalars(select(Workspace)).first()
            assert workspace is not None
            user = User(
                email=f"{uuid4()}@example.com",
                password_hash="hash",
                display_name="User",
                status="active",
            )
            session.add(user)
            session.flush()
            grant_workspace_membership(
                session, user_id=user.id, workspace_id=workspace.id, role="owner"
            )
            self.user_id: UUID = user.id
            self.workspace_id: UUID = workspace.id
        self.client_id = self.service.register_client(
            payload={
                "client_name": "Clock skew test client",
                "redirect_uris": [REDIRECT_URI],
                "scope": SCOPE,
            },
            client_ip="127.0.0.1",
        ).client_id

    def mint_result(self) -> OAuthTokenResult:
        code = self.service.issue_authorization_code(
            user_id=self.user_id,
            client_id=self.client_id,
            redirect_uri=REDIRECT_URI,
            code_challenge=challenge_s256(CODE_VERIFIER),
            code_challenge_method="S256",
            scope=SCOPE,
            state="state-" + uuid4().hex,
            workspace_ids=[self.workspace_id],
        )
        return self.service.exchange_authorization_code(
            code=code,
            client_id=self.client_id,
            redirect_uri=REDIRECT_URI,
            code_verifier=CODE_VERIFIER,
        )

    def mint(self) -> str:
        return self.mint_result().access_token

    @staticmethod
    def claims(token: str) -> dict:
        return jwt.decode(token, options={"verify_signature": False})

    def resign(self, token: str, **overrides) -> str:
        """Re-sign ``token`` with the service's own key, changing only the
        named claims (a value of ``None`` drops the claim). The real jti is
        kept, so the token still has a persisted denylist row."""

        claims = self.claims(token)
        for name, value in overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return jwt.encode(
            claims,
            oauth_service._private_key_pem(self.service.settings),
            algorithm="RS256",
            headers={"kid": oauth_service.JWT_KID},
        )

    def revoke(self, token: str) -> str | None:
        return self.service.revoke_token(
            token=token,
            token_type_hint="access_token",
            revoker_user_id=self.user_id,
            revoker_client_id=self.client_id,
        )

    def jti_revoked_at(self, token: str):
        with self.session_factory() as session:
            row = session.get(OAuthAccessTokenJti, self.claims(token)["jti"])
            assert row is not None
            return row.revoked_at


@pytest.fixture
def issuer(session_factory, clock) -> Issuer:
    return Issuer(session_factory)


def _authenticates(issuer: Issuer, token: str) -> bool:
    try:
        issuer.service.verify_access_token(token)
    except InvalidOAuthGrantError:
        return False
    return True


def _identifies(issuer: Issuer, token: str) -> bool:
    return issuer.service.get_access_token_client_id(token) == issuer.client_id


def _revokes(issuer: Issuer, token: str) -> bool:
    return issuer.revoke(token) == "access_token"


# The three production decode sites, as the caller sees them. Revoking mutates
# the denylist, so a test that applies several sites to one token revokes last.
DECODE_SITES = pytest.mark.parametrize(
    "site",
    [_authenticates, _identifies, _revokes],
    ids=["verify_access_token", "get_access_token_client_id", "revoke_token"],
)

# Seconds the verifier's clock reads BEHIND the token's iat: sub-second to
# 1.4 s NTP step corrections, a few seconds, a few minutes of node skew, and a
# four-hour jump. The policy is "iat is not consulted", not "iat may lead by
# some budget", so there is deliberately no value at which this stops passing.
CLOCK_BEHIND_SECONDS = pytest.mark.parametrize(
    "behind", [0.4, 1.4, 5, 180, 14_400], ids=lambda seconds: f"{seconds}s"
)


@CLOCK_BEHIND_SECONDS
def test_authentication_accepts_a_token_minted_ahead_of_the_verifier_clock(
    clock, issuer, behind
):
    token = issuer.mint()
    claims = issuer.claims(token)
    clock.set(T0 - behind)
    assert claims["iat"] > clock.epoch, "setup must put iat ahead of the verifier"

    actor = issuer.service.verify_access_token(token)

    assert actor.user.user_id == issuer.user_id
    assert actor.client_id == issuer.client_id
    assert actor.scopes == tuple(SCOPE.split())
    assert actor.grant_id == UUID(claims["grant_id"])


@CLOCK_BEHIND_SECONDS
def test_client_lookup_accepts_a_token_minted_ahead_of_the_verifier_clock(
    clock, issuer, behind
):
    token = issuer.mint()
    clock.set(T0 - behind)
    assert issuer.claims(token)["iat"] > clock.epoch

    assert issuer.service.get_access_token_client_id(token) == issuer.client_id


@CLOCK_BEHIND_SECONDS
def test_revocation_accepts_a_token_minted_ahead_of_the_verifier_clock(
    clock, issuer, behind
):
    token = issuer.mint()
    clock.set(T0 - behind)
    assert issuer.claims(token)["iat"] > clock.epoch

    assert issuer.revoke(token) == "access_token"

    assert issuer.jti_revoked_at(token) is not None
    with pytest.raises(InvalidOAuthGrantError, match="revoked"):
        issuer.service.verify_access_token(token)


def test_revocation_ownership_is_still_enforced_while_the_clock_trails(clock, issuer):
    """Ownership is enforced by the revoke path itself: a revoker that is not
    the token's user/client is still refused, and the genuine revoker succeeds,
    so the refusals are not just the old iat rejection under another name."""

    token = issuer.mint()
    clock.set(T0 - 1.4)

    other_user = issuer.service.revoke_token(
        token=token,
        token_type_hint="access_token",
        revoker_user_id=uuid4(),
        revoker_client_id=issuer.client_id,
    )
    other_client = issuer.service.revoke_token(
        token=token,
        token_type_hint="access_token",
        revoker_user_id=issuer.user_id,
        revoker_client_id="some-other-client",
    )

    assert (other_user, other_client) == (None, None)
    assert issuer.jti_revoked_at(token) is None
    assert issuer.revoke(token) == "access_token"


def test_authentication_expires_exactly_at_exp_with_no_leeway(clock, issuer):
    """Ignoring iat must not buy extra life on exp: a token is refused from its
    exp second on, with no leeway, and the refusal names expiry."""

    token = issuer.mint()
    exp = issuer.claims(token)["exp"]
    assert exp == T0 + ACCESS_TTL_SECONDS

    clock.set(exp - 0.001)
    issuer.service.verify_access_token(token)

    for expired_at in (exp, exp + 0.001, exp + 1):
        clock.set(expired_at)
        with pytest.raises(InvalidOAuthGrantError, match="invalid") as refused:
            issuer.service.verify_access_token(token)
        assert isinstance(refused.value.__cause__, jwt.ExpiredSignatureError)


def test_expired_token_is_still_identifiable_and_revocable(clock, issuer):
    """Only authentication enforces ``exp``; the two revoke-path decodes opt
    out of it, so an expired token can still be identified and denylisted (for
    instance after a forward clock jump). Editing their options for ``iat``
    must not drop that opt-out."""

    token = issuer.mint()
    clock.set(T0 + ACCESS_TTL_SECONDS + 14_400)

    with pytest.raises(InvalidOAuthGrantError) as refused:
        issuer.service.verify_access_token(token)
    assert isinstance(refused.value.__cause__, jwt.ExpiredSignatureError)
    assert issuer.service.get_access_token_client_id(token) == issuer.client_id
    assert issuer.revoke(token) == "access_token"
    assert issuer.jti_revoked_at(token) is not None


@DECODE_SITES
def test_not_before_is_still_enforced_at_every_decode_site(clock, issuer, site):
    """Dropping iat verification must not drop nbf: a token that says it is
    not yet valid stays refused until its nbf, and is accepted at nbf."""

    not_before = T0 + 10
    token = issuer.resign(issuer.mint(), nbf=not_before)

    clock.set(not_before - 0.001)
    assert not site(issuer, token)

    clock.set(not_before)
    assert site(issuer, token)


# forgery -> the PyJWT error authentication must still refuse it with
REFUSED_FOR = {
    "wrong-signing-key": jwt.InvalidSignatureError,
    "wrong-issuer": jwt.InvalidIssuerError,
    "wrong-audience": jwt.InvalidAudienceError,
    "hs256-instead-of-rs256": jwt.InvalidAlgorithmError,
    "alg-none": jwt.InvalidAlgorithmError,
}


@pytest.mark.parametrize("forgery", REFUSED_FOR)
def test_forged_tokens_stay_refused_while_the_clock_trails(
    clock, issuer, attacker_key_pem, forgery
):
    """The genuine token is accepted at this clock (controls first), so each
    refusal below is for the forgery and not for the verifier clock."""

    token = issuer.mint()
    clock.set(T0 - 1.4)
    assert _authenticates(issuer, token)
    assert _identifies(issuer, token)

    claims = issuer.claims(token)
    headers = {"kid": oauth_service.JWT_KID}
    if forgery == "wrong-signing-key":
        forged = jwt.encode(
            claims, attacker_key_pem, algorithm="RS256", headers=headers
        )
    elif forgery == "wrong-issuer":
        forged = issuer.resign(token, iss="http://attacker.example")
    elif forgery == "wrong-audience":
        forged = issuer.resign(token, aud="http://testserver/not-the-resource")
    elif forgery == "hs256-instead-of-rs256":
        forged = jwt.encode(claims, "s" * 48, algorithm="HS256", headers=headers)
    else:
        forged = jwt.encode(claims, None, algorithm="none")

    with pytest.raises(InvalidOAuthGrantError, match="invalid") as refused:
        issuer.service.verify_access_token(forged)
    assert isinstance(refused.value.__cause__, REFUSED_FOR[forgery])
    assert issuer.service.get_access_token_client_id(forged) is None
    assert issuer.revoke(forged) is None

    assert issuer.jti_revoked_at(token) is None, "a forgery must not revoke"
    assert _revokes(issuer, token)


# case -> the refusal authentication must still give once iat no longer masks it
DOWNSTREAM_REFUSALS = {
    "revoked": "revoked",
    "missing-jti": "revoked",
    "inactive-user": "user is invalid",
}


@pytest.mark.parametrize("case", DOWNSTREAM_REFUSALS)
def test_denylist_and_user_checks_still_run_while_the_clock_trails(clock, issuer, case):
    """With the iat gate gone, authentication now reaches the checks that used
    to sit behind it, and they must still refuse."""

    token = issuer.mint()
    if case == "revoked":
        assert issuer.revoke(token) == "access_token"
    elif case == "missing-jti":
        token = issuer.resign(token, jti=None)
    else:
        with issuer.session_factory.begin() as session:
            user = session.get(User, issuer.user_id)
            assert user is not None
            user.status = "disabled"
    clock.set(T0 - 1.4)
    assert issuer.claims(token)["iat"] > clock.epoch

    with pytest.raises(InvalidOAuthGrantError, match=DOWNSTREAM_REFUSALS[case]):
        issuer.service.verify_access_token(token)


def test_minted_claims_keep_integral_iat_exp_and_no_not_before(clock, issuer):
    """The fix lives in the verifier. Backdating iat or adding an nbf would be
    a different (and weaker) contract, so minting must not move."""

    clock.set(T0 + 0.75)

    result = issuer.mint_result()

    claims = issuer.claims(result.access_token)
    assert claims["iat"] == T0
    assert claims["exp"] == T0 + ACCESS_TTL_SECONDS
    assert isinstance(claims["iat"], int)
    assert isinstance(claims["exp"], int)
    assert "nbf" not in claims
    assert result.expires_in == ACCESS_TTL_SECONDS

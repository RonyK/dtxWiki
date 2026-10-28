from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from backend.api.cookie_budget import set_bounded_cookie
from backend.api.oauth import _try_bearer_revoker, parse_request_values
from backend.domain.oauth_input import validate_identifier
from backend.policy import auth
from backend.schemas.auth import OAuthConsentSettingsUpdateRequest
from backend.services import oauth_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.oauth_cimd import CIMDFetchError, _parse_metadata
from backend.services.oauth_service import (
    InvalidClientRegistrationError,
    InvalidOAuthGrantError,
    InvalidOAuthRequestError,
    OAuthService,
    challenge_s256,
    normalize_scope,
    validate_code_challenge,
    validate_redirect_uri,
    validate_state,
)
from backend.services.password_recovery_service import (
    EMAIL_VERIFICATION_TOKEN_PREFIX,
    InvalidPasswordResetTokenError,
    recovery_token_digest,
)
from backend.services.session_service import (
    InvalidSessionError,
    RecentReauthenticationCodec,
    RecentReauthenticationRequiredError,
    SessionTokenCodec,
)


def fail(*args, **kwargs):
    pytest.fail("invalid input reached parsing, crypto, a lock, or a database")


def blocked_service() -> OAuthService:
    return OAuthService(
        session_factory=SimpleNamespace(begin=fail),
        settings=SimpleNamespace(oauth_allow_dcr=True),
        public_base_url="https://wiki.example",
    )


def test_pat_rejected_before_hash_or_verify():
    hasher = CredentialHasher(SimpleNamespace(hash=fail, verify=fail))
    # Keep the dtxw_ prefix so the prefix check cannot mask the envelope.
    for value in ("dtxw_" + "a" * 252, "dtxw_a\n", None):
        with pytest.raises(ValueError):
            hasher.hash_api_token(value)
        assert hasher.verify_api_token(value, "encoded") is False


def test_token_generator_maximum_round_trips_and_refuses_excess_entropy():
    hasher = CredentialHasher()
    for generate, lookup, maximum_random_bytes in (
        (hasher.generate_api_token, hasher.token_lookup_hash, 188),
        (hasher.generate_purge_grant_token, hasher.purge_grant_lookup_hash, 189),
    ):
        token = generate(maximum_random_bytes)
        assert len(token) == 256
        assert lookup(token).startswith("sha256:")
        with pytest.raises(ValueError):
            generate(maximum_random_bytes + 1)
        with pytest.raises(ValueError):
            lookup(token + "a")


def test_recovery_token_limit_precedes_digest(monkeypatch):
    tokens = {
        purpose: prefix + "a" * (1024 - len(prefix))
        for purpose, prefix in (
            ("password_reset", "dtxr_0_"),
            ("email_verification", EMAIL_VERIFICATION_TOKEN_PREFIX),
        )
    }
    for purpose, token in tokens.items():
        assert recovery_token_digest(token, purpose=purpose).startswith("sha256:")
    monkeypatch.setattr(
        "backend.services.password_recovery_service.hashlib.sha256", fail
    )
    for purpose, token in tokens.items():
        for invalid in (token + "a", token[:-1] + " ", None):
            with pytest.raises(InvalidPasswordResetTokenError):
                recovery_token_digest(invalid, purpose=purpose)


def test_cookie_credentials_reject_before_signature(monkeypatch):
    for codec_type, error, method in (
        (SessionTokenCodec, InvalidSessionError, "verify_session"),
        (RecentReauthenticationCodec, RecentReauthenticationRequiredError, "verify"),
    ):
        monkeypatch.setattr(codec_type, "_sign", fail)
        verify = getattr(codec_type(secret="test secret"), method)
        # Dotted, so the malformed-token split cannot mask the envelope.
        for token in ("a." + "a" * 4095, "a.b\n", None):
            with pytest.raises(error):
                verify(token)


def test_serialized_cookie_limit_includes_escaping_name_and_attributes():
    response = Response()
    set_bounded_cookie(response, key="state", value=";" * 512, httponly=True)
    assert len(response.headers["set-cookie"].encode("latin-1")) < 4096
    before = list(response.raw_headers)
    with pytest.raises(ValueError, match="4096"):
        set_bounded_cookie(response, key="state", value=";" * 1024, httponly=True)
    assert response.raw_headers == before


def test_dcr_metadata_invalid_before_persistence():
    valid = {"client_name": "Test", "redirect_uris": ["https://client.example/cb"]}
    for payload in (
        [],
        {**valid, "client_name": 123},
        {**valid, "client_name": "x" * 201},
        {**valid, "redirect_uris": ["https://client.example/cb"] * 21},
        {**valid, "redirect_uris": ["https://client.example/" + "a" * 2048]},
        {**valid, "scope": ["mcp:read"]},
        {**valid, "grant_types": ["client_credentials"]},
        {**valid, "response_types": ["code", "code"]},
        {**valid, "application_type": {"native": True}},
    ):
        with pytest.raises(InvalidClientRegistrationError):
            blocked_service().register_client(
                payload=payload, client_ip="bad-input-test"
            )


def test_uri_and_identifier_count_utf8_bytes_without_normalization():
    prefix = "https://client.example/"
    remaining = 2048 - len(prefix)
    uri = prefix + "한" * (remaining // 3) + "a" * (remaining % 3)
    assert len(uri.encode()) == 2048
    assert validate_redirect_uri(uri) == uri
    assert validate_identifier(uri) == uri
    # urlsplit raises ValueError for these; token exchange must not turn it
    # into a 500.
    for value in (uri + "a", "https://[bad/cb", "https://exa\uff03mple.com/cb"):
        with pytest.raises(InvalidClientRegistrationError):
            validate_redirect_uri(value)
    with pytest.raises(ValueError):
        validate_identifier(uri + "a")


def test_cimd_same_redirect_budget_and_original_body_limit():
    client_id = "https://client.example/client.json"
    payload = {
        "client_id": client_id,
        "client_name": "x" * 200,
        "redirect_uris": ["https://client.example/cb"] * 20,
    }
    assert (
        len(_parse_metadata(client_id, json.dumps(payload).encode()).redirect_uris)
        == 20
    )
    payload["redirect_uris"].append("https://client.example/other")
    with pytest.raises(CIMDFetchError):
        _parse_metadata(client_id, json.dumps(payload).encode())
    payload["redirect_uris"] = ["https://client.example/" + "a" * 2048]
    with pytest.raises(CIMDFetchError):
        _parse_metadata(client_id, json.dumps(payload).encode())
    with pytest.raises(CIMDFetchError):
        _parse_metadata(client_id, b" " * 5121)
    # Unlike DCR, CIMD rejects a confidential client instead of storing "none".
    payload["redirect_uris"] = ["https://client.example/cb"]
    payload["token_endpoint_auth_method"] = "client_secret_basic"
    with pytest.raises(CIMDFetchError):
        _parse_metadata(client_id, json.dumps(payload).encode())


def test_pkce_s256_and_verifier_boundaries_before_hash(monkeypatch):
    for length in (43, 128):
        challenge = challenge_s256("v" * length)
        assert len(challenge) == 43
        validate_code_challenge(challenge)
    monkeypatch.setattr(oauth_service.hashlib, "sha256", fail)
    for verifier in ("v" * 42, "v" * 129, "v" * 42 + "!", None):
        with pytest.raises(InvalidOAuthRequestError):
            challenge_s256(verifier)
    for challenge in ("a" * 42, "a" * 44, "a" * 42 + ".", None):
        with pytest.raises(InvalidOAuthRequestError):
            validate_code_challenge(challenge)


def test_state_and_scope_limits_preserve_opaque_values():
    validate_state(";" * 512)
    validate_state(" " + "s" * 21)
    for state in ("s" * 21, "s" * 513, "s" * 21 + "\n"):
        with pytest.raises(InvalidOAuthRequestError):
            validate_state(state)
    assert normalize_scope("mcp:read " + " " * 247) == "mcp:read"
    with pytest.raises(InvalidOAuthRequestError):
        normalize_scope("mcp:read " + " " * 248)


def test_oauth_invalid_credentials_never_reach_database_lock_or_jwt_decode(monkeypatch):
    service = blocked_service()
    monkeypatch.setattr(oauth_service.jwt, "decode", fail)

    class BlockedLock:
        def __enter__(self):
            fail()

        def __exit__(self, *args):
            fail()

    monkeypatch.setattr(oauth_service, "_OAUTH_REFRESH_LOCK", BlockedLock())
    with pytest.raises(InvalidOAuthGrantError):
        service.verify_access_token("a" * 16385)
    assert service.get_access_token_client_id("a" * 16385) is None
    assert service.get_refresh_token_client_id("a" * 1025) is None
    assert service.revoke_token(token="a" * 16385) is None
    with pytest.raises(InvalidOAuthGrantError):
        service.refresh_access_token(refresh_token="a" * 1025, client_id="test")
    with pytest.raises(InvalidOAuthGrantError):
        service.exchange_authorization_code(
            code="a" * 1025,
            client_id="test",
            redirect_uri="https://client.example/cb",
            code_verifier="v" * 43,
        )
    with pytest.raises(InvalidOAuthRequestError):
        service.issue_authorization_code(
            user_id=uuid4(),
            client_id="test",
            redirect_uri="https://client.example/cb",
            code_challenge="v" * 43,
            code_challenge_method="S256",
            scope="mcp:read",
            state="s" * 22,
            workspace_ids=[uuid4()] * 257,
        )
    with pytest.raises(ValidationError) as failure:
        OAuthConsentSettingsUpdateRequest(workspace_ids=["not-a-uuid"] * 257)
    assert [error["type"] for error in failure.value.errors()] == ["too_long"]


def test_rest_and_revoke_bearer_padding_preserves_the_token(monkeypatch):
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"bEaReR  \tclient-token\t ")],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(public_base_url="https://wiki.example")
                )
            ),
        }
    )
    actor = SimpleNamespace(
        workspace_id=None, token_kind="oauth_jwt", user_id=uuid4(), client_id="client"
    )
    received = []

    def authenticate(*, token, **kwargs):
        received.append(token)
        return actor

    monkeypatch.setattr(auth, "_session_factory", lambda request: None)
    monkeypatch.setattr(auth, "_authenticate_api_token", authenticate)
    assert (
        auth.get_rest_actor(
            request,
            HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=" \tclient-token\t "
            ),
        )
        is actor
    )

    def revoke_authenticate(request, token):
        received.append(token)
        return actor

    monkeypatch.setattr(
        "backend.api.oauth.authenticate_bearer_token", revoke_authenticate
    )
    revoker = _try_bearer_revoker(request=request, session_factory=None, settings=None)
    assert revoker.client_id == "client"
    assert received == ["client-token", "client-token"]


def test_oauth_form_rejects_duplicate_singletons_and_oversized_id():
    for body in (b"client_id=one&client_id=two", b"client_id=" + b"a" * 2049):

        async def receive(body=body):
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/oauth/token",
                "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            },
            receive,
        )
        with pytest.raises(InvalidOAuthRequestError):
            asyncio.run(parse_request_values(request))

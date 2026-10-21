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


@pytest.mark.parametrize("value", ["a" * 257, "a\n", "한", None, 123])
def test_pat_rejected_before_hash_or_verify(value):
    hasher = CredentialHasher(SimpleNamespace(hash=fail, verify=fail))
    with pytest.raises(ValueError):
        hasher.hash_api_token(value)
    assert hasher.verify_api_token(value, "encoded") is False


@pytest.mark.parametrize("kind", ["api", "purge_grant"])
def test_token_generator_maximum_round_trips_and_refuses_excess_entropy(kind):
    hasher = CredentialHasher()
    generate = getattr(hasher, f"generate_{kind}_token")
    lookup = (
        hasher.token_lookup_hash if kind == "api" else hasher.purge_grant_lookup_hash
    )
    maximum_random_bytes = 188 if kind == "api" else 189
    token = generate(maximum_random_bytes)
    assert len(token) == 256
    assert lookup(token).startswith("sha256:")
    with pytest.raises(ValueError):
        generate(maximum_random_bytes + 1)
    with pytest.raises(ValueError):
        lookup(token + "a")


@pytest.mark.parametrize(
    "purpose,prefix",
    [
        ("password_reset", "dtxr_0_"),
        ("email_verification", EMAIL_VERIFICATION_TOKEN_PREFIX),
    ],
)
def test_recovery_token_limit_precedes_digest(monkeypatch, purpose, prefix):
    token = prefix + "a" * (1024 - len(prefix))
    assert recovery_token_digest(token, purpose=purpose).startswith("sha256:")
    monkeypatch.setattr(
        "backend.services.password_recovery_service.hashlib.sha256", fail
    )
    for invalid in (token + "a", token[:-1] + " ", token[:-1] + "한", None):
        with pytest.raises(InvalidPasswordResetTokenError):
            recovery_token_digest(invalid, purpose=purpose)


@pytest.mark.parametrize(
    "codec_type,error",
    [
        (SessionTokenCodec, InvalidSessionError),
        (RecentReauthenticationCodec, RecentReauthenticationRequiredError),
    ],
)
def test_cookie_credentials_reject_before_signature(codec_type, error, monkeypatch):
    codec = codec_type(secret="test secret")
    monkeypatch.setattr(codec_type, "_sign", fail)
    verify = codec.verify_session if codec_type is SessionTokenCodec else codec.verify
    for token in ("a" * 4097, "a.b\n", "한", None):
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_name", 123),
        ("client_name", None),
        ("client_name", "x" * 201),
        ("redirect_uris", ["https://client.example/cb"] * 21),
        ("redirect_uris", [123]),
        ("redirect_uris", ["https://client.example/" + "a" * 2048]),
        ("scope", ["mcp:read"]),
        ("scope", "mcp:read " * 30),
        ("grant_types", "authorization_code"),
        ("grant_types", ["client_credentials"]),
        ("response_types", ["code", "code"]),
        ("application_type", {"native": True}),
    ],
)
def test_dcr_metadata_invalid_before_persistence(field, value):
    payload = {
        "client_name": "Test",
        "redirect_uris": ["https://client.example/cb"],
        field: value,
    }
    with pytest.raises(InvalidClientRegistrationError):
        blocked_service().register_client(payload=payload, client_ip="bad-input-test")


def test_dcr_rejects_non_object_metadata():
    with pytest.raises(InvalidClientRegistrationError):
        blocked_service().register_client(payload=[], client_ip="bad-input-test")


def test_uri_and_identifier_count_utf8_bytes_without_normalization():
    prefix = "https://client.example/"
    remaining = 2048 - len(prefix)
    uri = prefix + "한" * (remaining // 3) + "a" * (remaining % 3)
    assert len(uri.encode()) == 2048
    assert validate_redirect_uri(uri) == uri
    assert validate_identifier(uri) == uri
    with pytest.raises(InvalidClientRegistrationError):
        validate_redirect_uri(uri + "a")
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
    payload["redirect_uris"] = ["https://client.example/cb"]
    for field, value in (
        ("token_endpoint_auth_method", "client_secret_basic"),
        ("token_endpoint_auth_method", "private_key_jwt"),
        ("application_type", "custom"),
    ):
        with pytest.raises(CIMDFetchError):
            _parse_metadata(client_id, json.dumps({**payload, field: value}).encode())


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
    for state in ("s" * 21, "s" * 513, "s" * 21 + "한", "s" * 21 + "\n"):
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


@pytest.mark.parametrize(
    "body",
    [
        b"client_id=one&client_id=two",
        b"state=one&state=two",
        b"client_id=" + b"a" * 2049,
    ],
)
def test_oauth_form_rejects_duplicate_singletons_and_oversized_id(body):
    async def receive():
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

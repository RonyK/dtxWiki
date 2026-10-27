"""Protocol security and bounded network behavior for unregistered OIDC helpers."""

# Budget exception: two unit cases separate signed-claim/key validation from
# HTTP/cache/SSRF behavior. Mutation loops are logical inputs, not one free case;
# no real IdP, application DB or browser is required for these failure paths.

from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from backend.spi.auth import (
    AuthMethodError,
    BeginRequest,
    CallbackInput,
    MethodCapabilities,
    ProviderContext,
)
from backend.spi.auth_oidc import JsonTransport, OidcClient, OidcMethodBase
from backend.testing.spi_conformance.auth import assert_auth_method_contract

pytestmark = pytest.mark.unit
ISSUER = "https://idp.example.test/tenant"
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "jwks_uri": ISSUER + "/keys",
}


def _key(kid="one"):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**public, "kid": kid, "use": "sig", "alg": "RS256"}


def _claims():
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": "stable-subject",
        "aud": "client-id",
        "iat": now,
        "exp": now + 300,
        "nonce": "transaction-nonce",
    }


def test_signed_id_token_validation_rejects_untrusted_claims_and_keys():
    private, public = _key()
    elliptic = ec.generate_private_key(ec.SECP256R1())
    ec_public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(elliptic.public_key()))
    ec_public.update({"kid": "ec", "use": "sig", "alg": "ES256"})
    client = OidcClient(
        transport=JsonTransport(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [public, ec_public]})
            )
        )
    )

    def validate(claims=None, *, token=None, nonce="transaction-nonce", **kwargs):
        return client.validate_id_token(
            token
            or jwt.encode(
                claims or _claims(), private, algorithm="RS256", headers={"kid": "one"}
            ),
            METADATA,
            issuer=ISSUER,
            client_id="client-id",
            nonce=nonce,
            **kwargs,
        )

    assert validate()["sub"] == "stable-subject"
    assert validate(
        token=jwt.encode(_claims(), elliptic, algorithm="ES256", headers={"kid": "ec"})
    )
    # A provider may omit kid and alg while publishing multiple key types.
    # Only compatible keys count toward ambiguity; two RSA keys still fail.
    mixed_keys = [
        {name: value for name, value in key.items() if name not in {"kid", "alg"}}
        for key in (public, ec_public)
    ]
    mixed_client = OidcClient(
        transport=JsonTransport(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": mixed_keys})
            )
        )
    )
    for key, algorithm in ((private, "RS256"), (elliptic, "ES256")):
        assert mixed_client.validate_id_token(
            jwt.encode(_claims(), key, algorithm=algorithm),
            METADATA,
            issuer=ISSUER,
            client_id="client-id",
            nonce="transaction-nonce",
        )
    mixed_keys.append({**mixed_keys[0]})
    ambiguous_client = OidcClient(
        transport=JsonTransport(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": mixed_keys})
            )
        )
    )
    with pytest.raises(AuthMethodError, match="signing_key_not_found"):
        ambiguous_client.validate_id_token(
            jwt.encode(_claims(), private, algorithm="RS256"),
            METADATA,
            issuer=ISSUER,
            client_id="client-id",
            nonce="transaction-nonce",
        )
    assert validate(
        {**_claims(), "aud": ["client-id", "trusted-api"], "azp": "client-id"},
        trusted_audiences=("trusted-api",),
    )
    assert validate(
        {**_claims(), "iss": "explicit-legacy-alias"},
        accepted_issuers=(ISSUER, "explicit-legacy-alias"),
    )
    mutations = [
        {"iss": "https://other.example.test"},
        {"aud": "other-client"},
        {"aud": ["client-id", "other-client"]},
        {"aud": ["client-id", "other-client"], "azp": "client-id"},
        {"azp": "other-client"},
        {"nonce": "other-nonce"},
        {"nonce": 123},
        {"exp": int(time.time()) - 120},
        {"iat": int(time.time()) + 120},
        {"sub": ""},
        {"sub": 123},
        {"sub": "non-ascii-사용자"},
        {"sub": "line\nbreak"},
        {"iat": True},
        {"exp": "9999999999"},
        {"auth_time": True},
        {"auth_time": time.time() + 120},
        {"auth_time": float("inf")},
    ]
    for mutation in mutations:
        with pytest.raises(AuthMethodError):
            validate({**_claims(), **mutation})
    for required in ("iss", "sub", "aud", "iat", "exp", "nonce"):
        claims = _claims()
        del claims[required]
        with pytest.raises(AuthMethodError):
            validate(claims)
    for token in (
        jwt.encode(_claims(), None, algorithm="none"),
        jwt.encode(_claims(), b"secret" * 8, algorithm="HS256"),
        jwt.encode(_claims(), _key()[0], algorithm="RS256", headers={"kid": "one"}),
        jwt.encode(
            _claims(),
            private,
            algorithm="RS256",
            headers={"kid": "one", "crit": ["custom"]},
        ),
        "malformed-token",
    ):
        with pytest.raises(AuthMethodError):
            validate(token=token)
    with pytest.raises(AuthMethodError):
        validate(nonce="")
    claims_without_nonce = _claims()
    del claims_without_nonce["nonce"]
    assert validate(claims_without_nonce, nonce=None)
    with pytest.raises(AuthMethodError):
        validate(max_age=0)
    with pytest.raises(AuthMethodError):
        validate({**_claims(), "auth_time": time.time() - 120}, max_age=0)


def _assert_slow_discovery_is_single_flight_without_blocking_other_providers():
    # Independent failure path: sequential transport tests cannot detect a
    # client-wide lock held across network I/O or duplicate concurrent fetches.
    entered = Event()
    release = Event()
    requests = []
    slow = "https://slow.example.test"
    fast = "https://fast.example.test"

    def handler(request):
        requests.append(str(request.url))
        if str(request.url).startswith(slow):
            entered.set()
            assert release.wait(5), "test did not release discovery"
        issuer = str(request.url).removesuffix("/.well-known/openid-configuration")
        return httpx.Response(200, json={**METADATA, "issuer": issuer})

    client = OidcClient(
        transport=JsonTransport(transport=httpx.MockTransport(handler)),
        max_cache_entries=2,
    )
    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(client.discovery, slow)
        try:
            assert entered.wait(2)
            duplicate = workers.submit(client.discovery, slow)
            assert (
                workers.submit(client.discovery, fast).result(timeout=2)["issuer"]
                == fast
            )
        finally:
            release.set()
        assert first.result(timeout=2) == duplicate.result(timeout=2)
    assert sum(url.startswith(slow) for url in requests) == 1
    assert sum(url.startswith(fast) for url in requests) == 1

    # All active slots stay pinned. A bounded client refuses another URL
    # instead of allocating detached locks or evicting an in-flight request.
    entered.clear()
    release.clear()
    bounded = OidcClient(
        transport=client.transport,
        max_cache_entries=1,
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(bounded.discovery, slow)
        try:
            assert entered.wait(2)
            with pytest.raises(AuthMethodError, match="keys_unavailable"):
                bounded.discovery(fast)
        finally:
            release.set()
        assert pending.result(timeout=2)["issuer"] == slow
    assert bounded.discovery(fast)["issuer"] == fast


def test_oidc_code_flow_and_bounded_discovery_jwks_transport(monkeypatch):
    _assert_slow_discovery_is_single_flight_without_blocking_other_providers()
    private, public = _key()
    now = [0.0]
    requests: list[httpx.Request] = []
    unavailable_documents = set()
    token_changes = {}
    signing_key = [private]
    userinfo_endpoint = ISSUER + "/userinfo"
    profile = {"sub": "other-subject"}
    documents = {
        ISSUER + "/.well-known/openid-configuration": {
            **METADATA,
            "userinfo_endpoint": userinfo_endpoint,
        },
        METADATA["jwks_uri"]: {"keys": [public]},
    }

    def handler(request):
        requests.append(request)
        if str(request.url) in unavailable_documents:
            return httpx.Response(503)
        if str(request.url) == METADATA["token_endpoint"]:
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["v" * 43]
            assert form["redirect_uri"] == ["https://wiki.example.test/callback"]
            assert (
                request.headers["authorization"]
                == "Basic " + base64.b64encode(b"client-id:secret%3A+value").decode()
            )
            return httpx.Response(
                200,
                json={
                    "id_token": jwt.encode(
                        {
                            **_claims(),
                            "email": "person@example.test",
                            "email_verified": True,
                            "name": "Signed Person",
                            **token_changes,
                        },
                        signing_key[0],
                        algorithm="RS256",
                        headers={"kid": "one"},
                    ),
                    "access_token": "private-access-token",
                },
            )
        if str(request.url) == userinfo_endpoint:
            assert request.headers["authorization"] == "Bearer private-access-token"
            return httpx.Response(200, json=profile)
        return httpx.Response(200, json=documents[str(request.url)])

    transport = JsonTransport(transport=httpx.MockTransport(handler))
    client = OidcClient(transport=transport, clock=lambda: now[0], max_cache_entries=1)
    adapter = OidcMethodBase(client=client)
    provider = ProviderContext(
        slug="corporate",
        callback_url="https://wiki.example.test/callback",
        config={
            "issuer": ISSUER,
            "client_id": "client-id",
            "client_secret_ref": "idp-secret",
        },
        secrets=lambda reference: "secret: value" if reference == "idp-secret" else "",
    )
    redirect = adapter.begin(
        provider,
        BeginRequest(
            purpose="login",
            state="core-state",
            nonce="transaction-nonce",
            code_challenge="c" * 43,
            force_reauthentication=True,
        ),
    )
    params = parse_qs(httpx.URL(redirect.url).query.decode())
    assert params["state"] == ["core-state"] and params["nonce"] == [
        "transaction-nonce"
    ]
    assert params["code_challenge_method"] == ["S256"] and params["prompt"] == ["login"]
    callback = CallbackInput(
        http_method="GET",
        query={"code": "one-use-code"},
        expected_nonce="transaction-nonce",
        pkce_verifier="v" * 43,
    )
    assertion = adapter.complete(provider, callback)
    assert assertion.subject == "stable-subject" and assertion.nonce_verified
    assert assertion.email_verified and assertion.email == "person@example.test"
    assert not any(str(request.url) == userinfo_endpoint for request in requests)

    # Budget exception: extend the existing code flow, with no new collected
    # case. These five inputs cover independent UserInfo boundaries: claim
    # provenance, signed-email precedence, optional failure, subject mismatch,
    # and consent scopes. A DB or browser adds no coverage for these RPC claims.
    profile.update(
        sub="stable-subject",
        email="profile@example.test",
        email_verified=False,
        name="Profile Person",
    )
    token_changes.update(email=None, name=None)
    enriched = adapter.complete(provider, callback)
    assert enriched.email == "profile@example.test" and not enriched.email_verified
    assert enriched.display_name == "Profile Person"
    token_changes.update(email="person@example.test")
    enriched = adapter.complete(provider, callback)
    assert enriched.email == "person@example.test" and enriched.email_verified
    assert enriched.display_name == "Profile Person"

    token_changes.update(email=None)
    unavailable_documents.add(userinfo_endpoint)
    unavailable = adapter.complete(provider, callback)
    assert unavailable.subject == "stable-subject"
    assert unavailable.email is None and not unavailable.email_verified
    unavailable_documents.clear()
    profile["sub"] = "other-subject"
    with pytest.raises(AuthMethodError, match="userinfo_subject_mismatch"):
        adapter.complete(provider, callback)
    before = len(requests)
    identity_only = replace(provider, config={**provider.config, "scopes": ["openid"]})
    assert adapter.complete(identity_only, callback).email is None
    assert all(str(request.url) != userinfo_endpoint for request in requests[before:])
    token_changes.clear()

    def callback_for(changes=None, key=None):
        def factory(redirect):
            token_changes.clear()
            token_changes.update(changes or {})
            signing_key[0] = key or private
            return CallbackInput(
                http_method="GET",
                query={"code": "conformance-code"},
                expected_nonce="transaction-nonce",
                pkce_verifier="v" * 43,
            )

        return factory

    assert_auth_method_contract(
        adapter,
        provider,
        begin=BeginRequest(
            purpose="login",
            state="conformance-state",
            nonce="transaction-nonce",
            code_challenge="c" * 43,
        ),
        successful_callback=callback_for(),
        rejected_callbacks={
            "signature": callback_for(key=_key()[0]),
            "audience": callback_for({"aud": "other-client"}),
        },
        expected_issuer=ISSUER,
        expected_subject="stable-subject",
    )
    token_changes.clear()
    signing_key[0] = private
    nonce_free_adapter = OidcMethodBase(client=client)
    nonce_free_adapter.descriptor = OidcMethodBase.describe(
        capabilities=MethodCapabilities(nonce="unsupported")
    )
    no_nonce_redirect = nonce_free_adapter.begin(
        provider,
        BeginRequest(
            purpose="login", state="nonce-free-state", code_challenge="c" * 43
        ),
    )
    assert "nonce" not in parse_qs(httpx.URL(no_nonce_redirect.url).query.decode())
    assert not nonce_free_adapter.complete(
        provider,
        CallbackInput(
            http_method="GET", query={"code": "nonce-free-code"}, pkce_verifier="v" * 43
        ),
    ).nonce_verified
    assert sum(".well-known" in str(request.url) for request in requests) == 1
    before = len(requests)
    for _ in range(3):
        with pytest.raises(AuthMethodError):
            client.validate_id_token(
                jwt.encode(
                    _claims(), private, algorithm="RS256", headers={"kid": "new"}
                ),
                METADATA,
                issuer=ISSUER,
                client_id="client-id",
                nonce="transaction-nonce",
            )
    assert len(requests) == before  # Unknown IDs cannot amplify JWKS requests.
    now[0] = 31
    documents[METADATA["jwks_uri"]] = {"keys": [{**public, "kid": "new"}]}
    rotated_token = jwt.encode(
        _claims(), private, algorithm="RS256", headers={"kid": "new"}
    )

    def validate_rotated_token(using_client=client):
        return using_client.validate_id_token(
            rotated_token,
            METADATA,
            issuer=ISSUER,
            client_id="client-id",
            nonce="transaction-nonce",
        )

    assert validate_rotated_token()
    assert len(requests) == before + 1
    now[0] = 301
    client.discovery(ISSUER)
    assert sum(".well-known" in str(request.url) for request in requests) == 2
    other_issuer = "https://other.example.test"
    documents[other_issuer + "/.well-known/openid-configuration"] = {
        **METADATA,
        "issuer": other_issuer,
    }
    client.discovery(other_issuer)
    client.discovery(ISSUER)
    assert sum(".well-known" in str(request.url) for request in requests) == 4
    # An expired document must fail closed while a failed refresh is throttled.
    now[0] = 332
    unavailable_documents.add(METADATA["jwks_uri"])
    before = len(requests)
    for _ in range(3):
        with pytest.raises(AuthMethodError):
            validate_rotated_token()
    assert len(requests) == before + 1
    now[0] = 362
    unavailable_documents.clear()
    assert validate_rotated_token()
    assert len(requests) == before + 2
    cold_client = OidcClient(transport=transport, clock=lambda: now[0])
    cold_requests = (
        lambda: cold_client.discovery(ISSUER),
        lambda: validate_rotated_token(cold_client),
    )
    unavailable_documents.update(
        {ISSUER + "/.well-known/openid-configuration", METADATA["jwks_uri"]}
    )
    # Both document caches retain failed first attempts without returning an
    # absent document or repeatedly contacting an unavailable provider.
    for fetch_document in cold_requests:
        before = len(requests)
        for _ in range(3):
            with pytest.raises(AuthMethodError):
                fetch_document()
        assert len(requests) == before + 1
    unavailable_documents.clear()
    before = len(requests)
    for fetch_document in cold_requests:
        with pytest.raises(AuthMethodError):
            fetch_document()
    assert len(requests) == before
    now[0] = 392
    for fetch_document in cold_requests:
        assert fetch_document()
    assert len(requests) == before + 2
    with pytest.raises(AuthMethodError):
        client.fetch_profile(
            userinfo_endpoint, "private-access-token", expected_subject="stable-subject"
        )
    for max_bytes, response in (
        (
            16,
            httpx.Response(
                302, headers={"location": "https://other.example.test/keys"}
            ),
        ),
        (
            16,
            httpx.Response(
                200, content=b"x" * 20, headers={"content-type": "application/json"}
            ),
        ),
        (
            64,
            httpx.Response(
                200,
                content=b'{"duplicate":1,"duplicate":2}',
                headers={"content-type": "application/json"},
            ),
        ),
    ):
        with pytest.raises(AuthMethodError):
            JsonTransport(
                max_bytes=max_bytes,
                transport=httpx.MockTransport(
                    lambda request, _response=response: _response
                ),
            ).request("GET", ISSUER)
    with pytest.raises(AuthMethodError):
        OidcClient(
            transport=JsonTransport(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={**METADATA, "issuer": ISSUER + "/"}
                    )
                )
            )
        ).discovery(ISSUER)
    with pytest.raises(AuthMethodError):
        adapter.validate_config(
            {
                **provider.config,
                "authorization_params": {"redirect_uri": "https://evil.test"},
            }
        )
    # Login start validates the normalized config again (issuer first), so the
    # alias limit must count the same distinct extra aliases both times.
    aliases = [f"https://alias-{index}.example.test" for index in range(17)]
    normalized = adapter.validate_config(
        {**provider.config, "accepted_issuers": [ISSUER, *aliases[:16], aliases[0]]}
    )
    assert adapter.validate_config(normalized) == normalized
    with pytest.raises(AuthMethodError):
        adapter.validate_config({**provider.config, "accepted_issuers": aliases})
    with pytest.raises(AuthMethodError):
        client.discovery("http://idp.example.test")
    token_requests = []

    def capture_token_request(request):
        token_requests.append(request)
        return httpx.Response(200, json={"id_token": "fixture"})

    auth_client = OidcClient(
        transport=JsonTransport(transport=httpx.MockTransport(capture_token_request))
    )
    for client_auth in ("client_secret_post", "none"):
        auth_client.exchange_code(
            METADATA,
            code="code",
            client_id="client-id",
            redirect_uri=provider.callback_url,
            code_verifier="v" * 43,
            client_secret="private-secret",
            token_endpoint_auth_method=client_auth,
        )
        request = token_requests[-1]
        assert "authorization" not in request.headers
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["client-id"]
        assert ("client_secret" in form) == (client_auth == "client_secret_post")
    auth_client.exchange_code(
        METADATA,
        code="code",
        client_id="client-id",
        redirect_uri=provider.callback_url,
        code_verifier=None,
        client_secret="private-secret",
    )
    assert "code_verifier" not in parse_qs(token_requests[-1].content.decode())
    timeout_clock = iter([0.0, 6.0])
    with pytest.raises(AuthMethodError, match="timeout"):
        JsonTransport(
            clock=lambda: next(timeout_clock),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        ).request("GET", ISSUER)
    monkeypatch.setattr(
        "backend.spi.auth_oidc.transport.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(AuthMethodError, match="address_forbidden"):
        JsonTransport().request("GET", ISSUER)
    pinned = []

    def pinned_transport(**kwargs):
        pinned.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    monkeypatch.setattr(
        "backend.spi.auth_oidc.transport._PinnedHTTPTransport", pinned_transport
    )
    assert JsonTransport().request("GET", ISSUER, allow_private_network=True) == {}
    assert pinned[0]["addresses"] == ("127.0.0.1",)
    monkeypatch.setattr(
        "backend.spi.auth_oidc.transport.socket.getaddrinfo",
        lambda *args, **kwargs: [(10, 1, 6, "", ("fd00:ec2::254", 443))],
    )
    with pytest.raises(AuthMethodError, match="address_forbidden"):
        JsonTransport().request("GET", ISSUER, allow_private_network=True)

"""Opt-in fake IdP adapter for plugin and broker tests; never auto-registered."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlencode

from backend.spi.auth import (
    AuthMethodDescriptor,
    AuthMethodError,
    BeginRequest,
    CallbackInput,
    ExternalAssertion,
    MethodCapabilities,
    ProviderContext,
    RedirectInstruction,
)

FAKE_ISSUER = "https://idp.example.test"


class FakeAuthMethod:
    """Canned successful codes with real state/nonce binding for core tests.

    This adapter does not implement OIDC signatures. Use ``OidcClient`` with an
    HTTPX ``MockTransport`` IdP when testing signed-token protocol validation.
    """

    descriptor = AuthMethodDescriptor(
        method_id="test_oidc",
        capabilities=MethodCapabilities(reauthentication=True),
        config_schema={
            "type": "object",
            "properties": {
                "issuer": {"type": "string"},
                "accepted_issuers": {"type": "array", "items": {"type": "string"}},
            },
        },
    )

    def __init__(self, assertions: Mapping[str, ExternalAssertion] | None = None):
        self.assertions = (
            dict(assertions)
            if assertions is not None
            else {
                "valid": ExternalAssertion(
                    issuer=FAKE_ISSUER,
                    subject="fake-subject",
                    email="fake@example.test",
                    email_verified=True,
                    display_name="Fake User",
                    authenticated_at=datetime.now(UTC),
                    nonce_verified=True,
                )
            }
        )
        self.requests: list[BeginRequest] = []
        self.callbacks: list[CallbackInput] = []
        self._pending: dict[str, str] = {}

    def validate_config(self, raw: Mapping) -> Mapping:
        return {
            **raw,
            "issuer": raw.get("issuer", FAKE_ISSUER),
            "accepted_issuers": raw.get(
                "accepted_issuers", [raw.get("issuer", FAKE_ISSUER)]
            ),
        }

    def begin(
        self, provider: ProviderContext, request: BeginRequest
    ) -> RedirectInstruction:
        if not request.nonce or not request.code_challenge:
            raise AuthMethodError("fake_missing_transaction_binding")
        self.requests.append(request)
        self._pending[request.state] = request.nonce
        return RedirectInstruction(
            url=FAKE_ISSUER
            + "/authorize?"
            + urlencode(
                {
                    "state": request.state,
                    "nonce": request.nonce,
                    "code_challenge": request.code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def complete(
        self, provider: ProviderContext, callback: CallbackInput
    ) -> ExternalAssertion:
        self.callbacks.append(callback)
        payload = callback.query if callback.http_method == "GET" else callback.form
        expected = self._pending.pop(payload.get("state", ""), None)
        if (
            not expected
            or not callback.expected_nonce
            or not hmac.compare_digest(expected, callback.expected_nonce)
            or not callback.pkce_verifier
            or payload.get("error")
        ):
            raise AuthMethodError("fake_invalid_transaction_binding")
        assertion = self.assertions.get(payload.get("code", ""))
        if assertion is None:
            raise AuthMethodError("fake_invalid_code")
        return replace(assertion, nonce_verified=True)


class FakeSamlMethod:
    """Canned POST assertions exercising SAML correlation, without SAML crypto."""

    descriptor = AuthMethodDescriptor(
        method_id="test_saml",
        protocol="saml2",
        capabilities=MethodCapabilities(
            pkce="unsupported",
            nonce="unsupported",
            id_token="none",
            reauthentication=True,
        ),
    )

    def __init__(self):
        self.requests: list[BeginRequest] = []
        self.callbacks: list[CallbackInput] = []
        self._pending: dict[str, str] = {}

    def validate_config(self, raw: Mapping) -> Mapping:
        return {**raw, "issuer": raw.get("issuer", FAKE_ISSUER)}

    def begin(
        self, provider: ProviderContext, request: BeginRequest
    ) -> RedirectInstruction:
        if request.nonce is not None or request.code_challenge is not None:
            raise AuthMethodError("fake_saml_received_oidc_secrets")
        self.requests.append(request)
        request_id = "_" + secrets.token_urlsafe(24)
        self._pending[request_id] = request.state
        return RedirectInstruction(
            url=FAKE_ISSUER + "/sso?" + urlencode({"RelayState": request.state}),
            protocol_request_id=request_id,
        )

    def complete(
        self, provider: ProviderContext, callback: CallbackInput
    ) -> ExternalAssertion:
        self.callbacks.append(callback)
        request_id = callback.protocol_request_id
        state = self._pending.pop(request_id, None)
        if (
            callback.http_method != "POST"
            or callback.expected_nonce is not None
            or callback.pkce_verifier is not None
            or state is None
            or callback.form.get("RelayState") != state
            or callback.form.get("InResponseTo") != request_id
            or callback.form.get("SAMLResponse") != "valid"
        ):
            raise AuthMethodError("fake_saml_invalid_response")
        return ExternalAssertion(
            issuer=provider.config["issuer"],
            subject="fake-saml-subject",
            email="fake@example.test",
            email_verified=True,
            authenticated_at=datetime.now(UTC),
        )
